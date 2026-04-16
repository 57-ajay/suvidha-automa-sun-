"""
Orchestrator: picks jobs from Redis, assigns slots, spawns agent subprocesses.
Each agent runs in its own process with its own DISPLAY environment.
"""

import os
import sys
import asyncio
import signal
import urllib.request

import redis

from slots import SlotPool

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "10"))
DOMAIN = os.environ.get("DOMAIN", "localhost")
JOB_TTL = 60 * 60 * 24

# Hostname discovery (GCE metadata, fallback for local)
WORKER_HOSTNAME = os.environ.get("WORKER_HOSTNAME")
if not WORKER_HOSTNAME:
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/name",
            headers={"Metadata-Flavor": "Google"},
        )
        WORKER_HOSTNAME = urllib.request.urlopen(
            req, timeout=1).read().decode()
    except Exception:
        WORKER_HOSTNAME = "worker"
print(f"[main] worker hostname = {WORKER_HOSTNAME}")

draining = False
pool: SlotPool


async def monitor_job(slot, r: redis.Redis):
    """Watch a running agent subprocess. Release slot when done or cancelled."""
    proc = slot.agent_proc
    job_id = slot.job_id

    try:
        while True:
            status_raw = r.hget(f"job:{job_id}", "status")
            if status_raw:
                status = status_raw.decode() if isinstance(status_raw, bytes) else status_raw
                if status == "cancelled":
                    print(
                        f"[{job_id}] Cancelled — killing agent (slot {slot.index})")
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        proc.kill()
                    return

            if proc.returncode is not None:
                return

            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        pool.release(slot)


async def worker_loop(r: redis.Redis):
    while True:

        if draining or r.get(f"vm:{WORKER_HOSTNAME}:draining") == b"1":
            await asyncio.sleep(2)
            continue

        if pool.available_count() == 0:
            await asyncio.sleep(1)
            continue

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: r.brpop("job:queue", timeout=1)
        )

        if result is None:
            continue

        _, job_id_bytes = result
        job_id = job_id_bytes.decode()

        job_raw = r.hgetall(f"job:{job_id}")
        if not job_raw:
            print(f"[{job_id}] Not found in Redis, skipping")
            continue

        job = {k.decode(): v.decode() for k, v in job_raw.items()}

        if job.get("status") == "cancelled":
            print(f"[{job_id}] Already cancelled, skipping")
            continue

        task_id = job.get("taskId", "?")
        print(f"[{job_id}] Picked up (task: {task_id})")

        slot = pool.try_acquire(job_id)
        if slot is None:
            r.lpush("job:queue", job_id)
            print(f"[{job_id}] No free slot, re-queued")
            await asyncio.sleep(1)
            continue

        live_url = (
            f"https://{DOMAIN}/vm/{WORKER_HOSTNAME}/vnc.html"
            f"?autoconnect=true&resize=scale"
            f"&path=vm/{WORKER_HOSTNAME}/websockify%3Ftoken%3D{job_id}"
        )

        r.hset(f"job:{job_id}", mapping={
            "status": "running",
            "liveUrl": live_url,
            "slotIndex": str(slot.index),
            "vmHostname": WORKER_HOSTNAME,
        })
        r.expire(f"job:{job_id}", JOB_TTL)

        print(f"[{job_id}] → slot {
              slot.index} (display :{slot.display}) | {live_url}")

        env = {**os.environ, "DISPLAY": f":{slot.display}"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "src/run_job.py", job_id,
            env=env,
            cwd="/app",
        )
        slot.agent_proc = proc

        asyncio.create_task(monitor_job(slot, r))


def request_drain():
    """Signal handler — flip local flag and set Redis key."""
    global draining
    if draining:
        return  # already draining, ignore repeat signals
    draining = True
    print(f"[drain] SIGTERM received — entering drain mode on {
          WORKER_HOSTNAME}")
    try:
        r = redis.from_url(REDIS_URL)
        r.set(f"vm:{WORKER_HOSTNAME}:draining", "1", ex=86400)
        print(f"[drain] Redis drain flag set")
    except Exception as e:
        print(f"[drain] Warning: could not set Redis flag: {e}")


async def wait_for_drain_complete():
    """Block until all slots are free. Called after drain starts."""
    print(f"[drain] Waiting for {
          MAX_SLOTS - pool.available_count()} active slots to clear")
    # 9 min max, leaves buffer for 10min grace
    deadline = asyncio.get_event_loop().time() + 9 * 60

    while True:
        active = [s for s in pool.slots if s.job_id is not None]
        if not active:
            print("[drain] all slots clear")
            return

        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            print(f"[drain] timeout — {len(active)
                                       } slots still active, forcing exit")
            return

        print(f"[drain] {len(active)} active (jobs: {
              [s.job_id for s in active]}), {int(remaining)}s remaining")
        await asyncio.sleep(10)


async def main():
    global pool

    r = redis.from_url(REDIS_URL)
    pool = SlotPool(max_slots=MAX_SLOTS)

    # Start metric publisher (no-op locally)
    from metrics import publish_loop
    asyncio.create_task(publish_loop(pool, WORKER_HOSTNAME))

    # SIGTERM → drain, SIGINT → drain (Ctrl+C in dev)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_drain)

    queued = r.llen("job:queue")
    print(f"Orchestrator started: max_slots={MAX_SLOTS}, queued={queued}")
    print(f"Slot displays: :{pool.slots[0].display} – :{
          pool.slots[-1].display}")

    # Run worker loop until drain starts
    worker_task = asyncio.create_task(worker_loop(r))

    # Watch for drain trigger
    while not draining:
        await asyncio.sleep(1)

    # Drain initiated — stop accepting new work, wait for active to finish
    print("[main] drain started, worker_loop will stop claiming new jobs")
    await wait_for_drain_complete()

    # Clean up display stacks and exit
    print("[main] cleaning up slot pool")
    pool.cleanup_all()
    worker_task.cancel()
    print("[main] shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())

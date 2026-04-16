"""
Orchestrator: picks jobs from Redis, assigns slots, spawns agent subprocesses.
Each agent runs in its own process with its own DISPLAY environment.
"""

import os
import sys
import asyncio
import signal

import redis

from slots import SlotPool

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "10"))
DOMAIN = os.environ.get("DOMAIN", "localhost")
JOB_TTL = 60 * 60 * 24

pool: SlotPool


async def monitor_job(slot, r: redis.Redis):
    """Watch a running agent subprocess. Release slot when done or cancelled."""
    proc = slot.agent_proc
    job_id = slot.job_id

    try:
        while True:
            # Check for cancellation
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

            # Check if process exited
            if proc.returncode is not None:
                return

            # Wait up to 2s for exit, then loop to re-check cancellation
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        pool.release(slot)


async def worker_loop(r: redis.Redis):
    while True:
        # Don't pop from queue if all slots are busy
        if pool.available_count() == 0:
            await asyncio.sleep(1)
            continue

        # Blocking pop with 1s timeout (run in executor since redis-py is sync)
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

        # Skip if already cancelled before we picked it up
        if job.get("status") == "cancelled":
            print(f"[{job_id}] Already cancelled, skipping")
            continue

        task_id = job.get("taskId", "?")
        print(f"[{job_id}] Picked up (task: {task_id})")

        # Claim a slot
        slot = pool.try_acquire(job_id)
        if slot is None:
            # All slots filled between check and acquire — put back
            r.lpush("job:queue", job_id)
            print(f"[{job_id}] No free slot, re-queued")
            await asyncio.sleep(1)
            continue

        live_url = (
            f"https://{DOMAIN}/vnc.html"
            f"?autoconnect=true&resize=scale&path=websockify%3Ftoken%3D{
                job_id}"
        )

        r.hset(f"job:{job_id}", mapping={
            "status": "running",
            "liveUrl": live_url,
            "slotIndex": str(slot.index),
        })
        r.expire(f"job:{job_id}", JOB_TTL)

        print(f"[{job_id}] → slot {
              slot.index} (display :{slot.display}) | {live_url}")

        # Spawn agent as a subprocess with its own DISPLAY
        env = {**os.environ, "DISPLAY": f":{slot.display}"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "src/run_job.py", job_id,
            env=env,
            cwd="/app",
        )
        slot.agent_proc = proc

        # Monitor in background (releases slot when done)
        asyncio.create_task(monitor_job(slot, r))


async def main():
    global pool

    r = redis.from_url(REDIS_URL)
    pool = SlotPool(max_slots=MAX_SLOTS)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    queued = r.llen("job:queue")
    print(f"Orchestrator started: max_slots={MAX_SLOTS}, queued={queued}")
    print(f"Slot displays: :{pool.slots[0].display} – :{
          pool.slots[-1].display}")

    await worker_loop(r)


async def shutdown():
    print("Shutting down — killing all agents and display stacks...")
    pool.cleanup_all()  # why the hell it is giving error, it is defined as gloabal var
    await asyncio.sleep(1)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())

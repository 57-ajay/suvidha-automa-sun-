"""
Orchestrator: picks jobs from Redis, assigns slots, spawns agent
subprocesses.
"""

import os
import sys
import socket
import asyncio
import signal
import uuid

import redis

from slots import SlotPool

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "8"))
EDGE_DOMAIN = os.environ.get(
    "EDGE_DOMAIN", "automation-agent.cabswale.in"
)
WORKER_PORT = int(os.environ.get("WORKER_PORT", "6080"))
JOB_TTL = 60 * 60 * 24
HEARTBEAT_INTERVAL = 10
WORKER_TTL = 30

pool: SlotPool
worker_id: str
worker_ip: str


def detect_private_ip() -> str:
    """Best-effort detection of this VM's primary internal IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


async def heartbeat_loop(r: redis.Redis):
    """Refresh this worker's presence every HEARTBEAT_INTERVAL sec."""
    meta = {
        "ip": worker_ip,
        "port": str(WORKER_PORT),
        "maxSlots": str(MAX_SLOTS),
    }
    while True:
        try:
            now = asyncio.get_event_loop().time()
            r.zadd("workers:active", {worker_id: now})
            r.hset(f"worker:{worker_id}:meta", mapping=meta)
            r.expire(f"worker:{worker_id}:meta", WORKER_TTL * 3)
            r.hset(
                f"worker:{worker_id}:meta",
                "activeJobs",
                str(MAX_SLOTS - pool.available_count()),
            )
        except Exception as e:
            print(f"[heartbeat] WARN: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def monitor_job(slot, r: redis.Redis):
    """Watch a running agent subprocess. Release slot when done."""
    proc = slot.agent_proc
    job_id = slot.job_id

    try:
        while True:
            status_raw = r.hget(f"job:{job_id}", "status")
            if status_raw:
                status = (
                    status_raw.decode()
                    if isinstance(status_raw, bytes)
                    else status_raw
                )
                if status == "cancelled":
                    print(
                        f"[{job_id}] Cancelled — killing agent "
                        f"(slot {slot.index})"
                    )
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
            f"https://{EDGE_DOMAIN}/vnc/{job_id}/vnc.html"
            f"?autoconnect=true&resize=scale"
            f"&path=vnc/{job_id}/websockify%3Ftoken%3D{job_id}"
        )

        r.hset(f"job:{job_id}", mapping={
            "status": "running",
            "liveUrl": live_url,
            "slotIndex": str(slot.index),
        })
        r.expire(f"job:{job_id}", JOB_TTL)

        print(
            f"[{job_id}] → slot {slot.index} "
            f"(display :{slot.display}) | {live_url}"
        )

        env = {**os.environ, "DISPLAY": f":{slot.display}"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "src/run_job.py", job_id,
            env=env,
            cwd="/app",
        )
        slot.agent_proc = proc

        asyncio.create_task(monitor_job(slot, r))


async def main():
    global pool, worker_id, worker_ip

    worker_ip = detect_private_ip()
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

    r = redis.from_url(REDIS_URL)
    pool = SlotPool(
        max_slots=MAX_SLOTS,
        redis_client=r,
        worker_id=worker_id,
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(shutdown(r)),
        )

    queued = r.llen("job:queue")
    print(
        f"Orchestrator started: worker_id={worker_id} "
        f"ip={worker_ip} max_slots={MAX_SLOTS} queued={queued}"
    )
    print(
        f"Slot displays: :{pool.slots[0].display} – "
        f":{pool.slots[-1].display}"
    )

    asyncio.create_task(heartbeat_loop(r))
    await worker_loop(r)


async def shutdown(r: redis.Redis | None = None):
    global pool, worker_id
    print("Shutting down — killing all agents and display stacks...")
    pool.cleanup_all()
    if r is not None:
        try:
            r.zrem("workers:active", worker_id)
            r.delete(f"worker:{worker_id}:meta")
        except Exception:
            pass
    await asyncio.sleep(1)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())

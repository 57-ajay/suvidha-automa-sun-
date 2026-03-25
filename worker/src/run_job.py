"""
Entry point for a single agent session.
Spawned as a subprocess by the orchestrator with DISPLAY already set.
"""

import sys
import os
import json
import asyncio

import redis

from agent import run_agent

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


async def main():
    if len(sys.argv) < 2:
        print("Usage: run_job.py <job_id>")
        sys.exit(1)

    job_id = sys.argv[1]
    display = os.environ.get("DISPLAY", "?")
    r = redis.from_url(REDIS_URL)

    job_raw = r.hgetall(f"job:{job_id}")
    if not job_raw:
        print(f"[{job_id}] Job not found in Redis")
        sys.exit(1)

    job = {k.decode(): v.decode() for k, v in job_raw.items()}

    prompt = job.get("prompt", "")
    if not prompt:
        r.hset(f"job:{job_id}", mapping={
               "status": "failed", "error": "No prompt"})
        sys.exit(1)

    try:
        tool_defs = json.loads(job.get("tools", "[]"))
    except json.JSONDecodeError:
        tool_defs = []

    try:
        job_params = json.loads(job.get("params", "{}"))
    except json.JSONDecodeError:
        job_params = {}

    print(f"""[{job_id}] Agent starting on DISPLAY={display}, {
          len(tool_defs)} tools, params={list(job_params.keys())}""")

    try:
        result = await run_agent(prompt, job_id, job_params, tool_defs, r)
        r.hset(f"job:{job_id}", mapping={"status": "done", "result": result})
        print(f"[{job_id}] Done")
    except Exception as e:
        r.hset(f"job:{job_id}", mapping={"status": "failed", "error": str(e)})
        print(f"[{job_id}] Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

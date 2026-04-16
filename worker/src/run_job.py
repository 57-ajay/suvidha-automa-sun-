"""
Entry point for a single agent session.
Spawned as a subprocess by the orchestrator with DISPLAY already set.
"""

import sys
import os
import json
import asyncio

import httpx
import redis

from agent import run_agent

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
API_URL = os.environ.get("API_URL", "http://api:3000")
JOB_TTL = 60 * 60 * 24


async def notify_job_completed(job_id: str, request_id: str | None, cost_data: dict | None = None, source: str = "web"):
    """Fire-and-forget: tell the API the job is done so it can release the agent config slot."""
    try:
        payload: dict = {"jobId": job_id, "requestId": request_id}
        if cost_data:
            payload["costData"] = cost_data
        if source:
            payload["source"] = source
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{API_URL}/api/internal/job-completed",
                json=payload,
            )
        print(f"[{job_id}] Notified API of job completion (cost included: {
              cost_data is not None})")
    except Exception as e:
        print(f"[{job_id}] Warning: failed to notify job completion: {e}")


def extract_cost_data(result) -> dict | None:
    """Extract cost data from agent result's usage info."""
    try:
        usage = result.usage
        if not usage:
            return None

        return {
            "totalPromptTokens": usage.total_prompt_tokens or 0,
            "totalCompletionTokens": usage.total_completion_tokens or 0,
            "totalTokens": usage.total_tokens or 0,
            "totalCost": usage.total_cost or 0,
            "totalPromptCost": usage.total_prompt_cost or 0,
            "totalCompletionCost": usage.total_completion_cost or 0,
            "totalCachedTokens": usage.total_prompt_cached_tokens or 0,
            "totalCachedCost": usage.total_prompt_cached_cost or 0,
            "entryCount": usage.entry_count or 0,
        }
    except Exception as e:
        print(f"Warning: failed to extract cost data: {e}")
        return None


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
        r.expire(f"job:{job_id}", JOB_TTL)
        sys.exit(1)

    try:
        tool_defs = json.loads(job.get("tools", "[]"))
    except json.JSONDecodeError:
        tool_defs = []

    try:
        job_params = json.loads(job.get("params", "{}"))
    except json.JSONDecodeError:
        job_params = {}

    request_id = job_params.get("requestId")
    source = job.get("source", "web")

    print(f"""[{job_id}] Agent starting on DISPLAY={display}, {
          len(tool_defs)} tools, params={list(job_params.keys())}""")

    try:
        result = await run_agent(prompt, job_id, job_params, tool_defs, r)

        cost_data = extract_cost_data(result)

        final_result = result.final_result() or "No result returned"
        r.hset(f"job:{job_id}", mapping={
               "status": "done", "result": final_result})
        r.expire(f"job:{job_id}", JOB_TTL)
        print(f"[{job_id}] Done")

        await notify_job_completed(job_id, request_id, cost_data, source)

    except Exception as e:
        r.hset(f"job:{job_id}", mapping={"status": "failed", "error": str(e)})
        r.expire(f"job:{job_id}", JOB_TTL)
        print(f"[{job_id}] Failed: {e}")

        await notify_job_completed(job_id, request_id, source)

        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

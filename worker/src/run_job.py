"""
Entry point for a single agent session.
Spawned as a subprocess by the orchestrator with DISPLAY already set.
"""

import sys
import os
import json
import re
import asyncio

import httpx
import redis

from agent import run_agent

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
API_URL = os.environ.get("API_URL", "http://api:3000")
JOB_TTL = 60 * 60 * 24


async def notify_job_completed(
    job_id: str,
    request_id: str | None,
    status: str,
    summary: str | None = None,
    error: str | None = None,
    cost_data: dict | None = None,
    source: str = "web",
    partial_reasons: list[str] | None = None,
):
    """Fire-and-forget: tell the API the job is done so it can release the
    agent config slot and persist the agent work summary."""
    try:
        payload: dict = {
            "jobId": job_id,
            "requestId": request_id,
            "status": status,
            "source": source,
        }
        if summary is not None:
            payload["summary"] = summary
        if error is not None:
            payload["error"] = error
        if cost_data:
            payload["costData"] = cost_data
        if partial_reasons:
            payload["partialReasons"] = partial_reasons

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{API_URL}/api/internal/job-completed",
                json=payload,
            )
        print(
            f"[{job_id}] Notified API of job completion "
            f"(status={status}, cost_included={cost_data is not None}, "
            f"summary_len={len(summary) if summary else 0}, "
            f"has_error={error is not None}, "
            f"partial_reasons={len(partial_reasons)
                               if partial_reasons else 0})"
        )
    except Exception as e:
        print(f"[{job_id}] Warning: failed to notify job completion: {e}")


def resolve_final_status(result, job_id: str, r: redis.Redis) -> tuple[str, list[str]]:
    """
    Determine whether a successfully-returned agent run is actually 'done' or 'partial'.

    Partial signals, in priority order:
      1. Any entries in job:{id}:partial_reasons (pushed by wait_for_human on timeout,
         and potentially other runtime events in the future).
      2. Agent ran out of its max_steps budget without calling done.
      3. Agent's final_result self-reports "Status: partial".

    Returns (status, reasons). status is either 'done' or 'partial'.
    """
    reasons: list[str] = []

    # 1. Runtime reasons pushed to Redis during the run
    try:
        raw = r.lrange(f"job:{job_id}:partial_reasons", 0, -1)
        for item in raw:
            reasons.append(item.decode() if isinstance(item, bytes) else item)
    except Exception as e:
        print(f"[{job_id}] Warning: could not read partial_reasons list: {e}")

    # 2. Agent hit max_steps without calling done
    is_done = True
    try:
        attr = getattr(result, "is_done", None)
        if attr is not None:
            is_done = attr() if callable(attr) else bool(attr)
    except Exception as e:
        print(f"[{job_id}] Warning: could not inspect is_done on result: {e}")
    if not is_done:
        reasons.append("max_steps_exceeded")

    # 3. Safety-net: agent self-reported partial in its final summary
    try:
        final = (result.final_result() or "")
        if re.search(r"status\s*:\s*partial", final, re.IGNORECASE):
            if not any(x.startswith("agent_reported") for x in reasons):
                reasons.append("agent_reported_partial")
    except Exception as e:
        print(f"[{job_id}] Warning: could not inspect final_result: {e}")

    # Dedupe while preserving order
    seen = set()
    deduped = []
    for item in reasons:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    status = "partial" if deduped else "done"
    return status, deduped


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
    task_id = job.get("taskId", "")

    print(
        f"[{job_id}] Agent starting on DISPLAY={display}, task={task_id}, "
        f"{len(tool_defs)} dynamic tools, params={list(job_params.keys())}, "
        f"source={source}"
    )

    try:
        result = await run_agent(prompt, job_id, job_params, tool_defs, r, task_id)

        cost_data = extract_cost_data(result)
        final_result = result.final_result() or "No result returned"

        status, partial_reasons = resolve_final_status(result, job_id, r)

        mapping = {"status": status, "result": final_result}
        if partial_reasons:
            mapping["partialReasons"] = json.dumps(partial_reasons)
        r.hset(f"job:{job_id}", mapping=mapping)
        r.expire(f"job:{job_id}", JOB_TTL)
        print(f"[{job_id}] {status} (reasons={partial_reasons or 'none'})")

        await notify_job_completed(
            job_id=job_id,
            request_id=request_id,
            status=status,
            summary=final_result,
            cost_data=cost_data,
            source=source,
            partial_reasons=partial_reasons or None,
        )

    except Exception as e:
        err_msg = str(e)
        r.hset(f"job:{job_id}", mapping={"status": "failed", "error": err_msg})
        r.expire(f"job:{job_id}", JOB_TTL)
        print(f"[{job_id}] Failed: {err_msg}")

        await notify_job_completed(
            job_id=job_id,
            request_id=request_id,
            status="failed",
            error=err_msg,
            source=source,
        )

        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

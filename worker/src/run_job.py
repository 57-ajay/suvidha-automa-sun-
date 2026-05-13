# worker/src/run_job.py
"""Entry point for a single agent session.
Spawned as a subprocess by the orchestrator with DISPLAY already set.

Two execution paths converge at the same notify_job_completed call:

  SCRIPTED PATH (taken iff ALL of these hold)
    - task_id == "border-tax"
    - the request's state is in SCRIPTED_BORDER_TAX_STATES env var
    - paymentMethod == "upi" (v1; net_banking still goes through AI)
    Implementation: scripted.runner.run_border_tax -> RunOutcome

  AI PATH (everything else, including border-tax when scripted is off)
    Implementation: agent.run_agent -> AgentHistoryList

Whichever path runs, this file:
  1. Maps the result into (status, summary, partial_reasons, cost_data, run_log).
  2. Writes status/result/partialReasons back to the job hash in Redis.
  3. Fire-and-forget POSTs to /api/internal/job-completed with the unified
     payload (the API persists costData + runLog to Firestore).
"""

import asyncio
import json
import os
import re
import sys

import httpx
import redis

from agent import run_agent
from cost_calculator import fill_missing_cost
from scripted.runner import run_border_tax, state_is_scripted_enabled
from scripted.types import RunOutcome


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
API_URL = os.environ.get("API_URL", "http://api:3000")
JOB_TTL = 60 * 60 * 24


# ─── notify_job_completed (gains run_log) ──────────────────────────────


async def notify_job_completed(
    job_id: str,
    request_id: str | None,
    status: str,
    summary: str | None = None,
    error: str | None = None,
    cost_data: dict | None = None,
    source: str = "web",
    partial_reasons: list[str] | None = None,
    run_log: list[dict] | None = None,
):
    """Fire-and-forget: tell the API the job is done so it can release the
    agent config slot, persist the agent work summary, and (if run_log is
    present) write per-step entries to borderTaxRequests/<rid>/agentAutomationLog."""
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
        if run_log:
            payload["runLog"] = run_log

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{API_URL}/api/internal/job-completed",
                json=payload,
            )
        print(
            f"[{job_id}] Notified API of job completion "
            f"(status={status}, "
            f"cost_included={cost_data is not None}, "
            f"summary_len={len(summary) if summary else 0}, "
            f"has_error={error is not None}, "
            f"partial_reasons={len(partial_reasons)
                               if partial_reasons else 0}, "
            f"run_log_steps={len(run_log) if run_log else 0})"
        )
    except Exception as e:
        print(f"[{job_id}] Warning: failed to notify job completion: {e}")


# ─── AI-path helpers (unchanged) ───────────────────────────────────────


def resolve_final_status(
    result, job_id: str, r: redis.Redis
) -> tuple[str, list[str]]:
    """For the AI path. Determines whether a successful agent run is
    actually 'done' or should be downgraded to 'partial'.

    Partial signals, in priority order:
      1. Entries in job:{id}:partial_reasons (pushed by wait_for_human
         timeout, etc).
      2. Agent ran out of its max_steps budget without calling done.
      3. Agent's final_result self-reports 'Status: partial'.
    """
    reasons: list[str] = []

    try:
        raw = r.lrange(f"job:{job_id}:partial_reasons", 0, -1)
        for item in raw:
            reasons.append(item.decode() if isinstance(item, bytes) else item)
    except Exception as e:
        print(f"[{job_id}] Warning: could not read partial_reasons list: {e}")

    is_done = True
    try:
        attr = getattr(result, "is_done", None)
        if attr is not None:
            is_done = attr() if callable(attr) else bool(attr)
    except Exception as e:
        print(f"[{job_id}] Warning: could not inspect is_done on result: {e}")
    if not is_done:
        reasons.append("max_steps_exceeded")

    try:
        final = (result.final_result() or "")
        if re.search(r"status\s*:\s*partial", final, re.IGNORECASE):
            if not any(x.startswith("agent_reported") for x in reasons):
                reasons.append("agent_reported_partial")
    except Exception as e:
        print(f"[{job_id}] Warning: could not inspect final_result: {e}")

    seen = set()
    deduped = []
    for item in reasons:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    status = "partial" if deduped else "done"
    return status, deduped


def extract_cost_data(result) -> dict | None:
    """For the AI path. Delegates to cost_calculator.fill_missing_cost.

    - Passes through browser-use's numbers when total_cost > 0.
    - Computes cost locally from per-model token counts when total_cost == 0.
    To support a new model, edit GEMINI_PRICING in worker/src/cost_calculator.py.
    """
    try:
        usage = getattr(result, "usage", None)
        if not usage:
            return None
        return fill_missing_cost(usage)
    except Exception as e:
        print(f"Warning: failed to extract cost data: {e}")
        return None


# ─── scripted-path helpers ─────────────────────────────────────────────


def _should_use_scripted(task_id: str, params: dict) -> bool:
    """Scripted runner takes over only when ALL conditions hold."""
    if task_id != "border-tax":
        return False
    state = params.get("state", "") or ""
    if not state_is_scripted_enabled(state):
        return False
    pm = ((params.get("paymentMethod") or "upi") or "").lower()
    return pm == "upi"


def _scripted_cost_data(outcome: RunOutcome) -> dict | None:
    """Adapt RunOutcome.total_cost_usd into the same dict shape that
    fill_missing_cost produces, so the API's saveAgentCost path is happy.
    Token counts are zero because scripted runs don't have a single
    aggregated usage object -- cost is summed across captcha + handoff
    LLM calls and reported as such."""
    if not outcome.total_cost_usd or outcome.total_cost_usd <= 0:
        return None
    entries_with_cost = sum(
        1 for e in outcome.run_log if e.handoff_cost_usd
    )
    return {
        "totalPromptTokens": 0,
        "totalCompletionTokens": 0,
        "totalTokens": 0,
        "totalCost": outcome.total_cost_usd,
        "totalPromptCost": 0.0,
        "totalCompletionCost": 0.0,
        "totalCachedTokens": 0,
        "totalCachedCost": 0.0,
        "entryCount": entries_with_cost,
        "costSource": "scripted_aggregate",
    }


async def _run_scripted(
    job_id: str,
    job_params: dict,
    r: redis.Redis,
) -> tuple[str, str, list[str], dict | None, list[dict] | None]:
    """Run the scripted path. Returns the same shape the AI path produces
    so the downstream notify code is identical:
      (status, summary, partial_reasons, cost_data, run_log_dump)
    """
    state = job_params.get("state", "")
    outcome = await run_border_tax(state, job_params, job_id, r)

    run_log_dump = [
        entry.model_dump(mode="json") for entry in outcome.run_log
    ] or None

    partial_reasons: list[str] = []
    if outcome.status == "partial":
        partial_reasons = list(outcome.partial_reasons)

    cost_data = _scripted_cost_data(outcome)
    return outcome.status, outcome.summary, partial_reasons, cost_data, run_log_dump


# ─── main ──────────────────────────────────────────────────────────────


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
        r.hset(f"job:{job_id}", mapping={"status": "failed", "error": "No prompt"})
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

    use_scripted = _should_use_scripted(task_id, job_params)

    print(
        f"[{job_id}] Agent starting on DISPLAY={display}, task={task_id}, "
        f"path={'scripted' if use_scripted else 'ai'}, "
        f"{len(tool_defs)} dynamic tools, params={list(job_params.keys())}, "
        f"source={source}"
    )

    try:
        if use_scripted:
            (
                status,
                final_result,
                partial_reasons,
                cost_data,
                run_log_dump,
            ) = await _run_scripted(job_id, job_params, r)
        else:
            result = await run_agent(
                prompt, job_id, job_params, tool_defs, r, task_id
            )
            cost_data = extract_cost_data(result)
            final_result = result.final_result() or "No result returned"
            status, partial_reasons = resolve_final_status(result, job_id, r)
            run_log_dump = None

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
            run_log=run_log_dump,
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


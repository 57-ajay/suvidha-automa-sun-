"""Entry point for challan-SETTLEMENT scripted jobs (invoked by run_job.py).

Mirrors scripted.challan.runner.run_challan_payment for browser lifecycle, but:
  1. Fetch the department list for requestId from the API (the worker has no
     Firestore access) — this replaces the removed Delhi Traffic Police visit.
  2. Loop each department through vcourts.run_department (extract → save_discounts).
  3. Reconcile per-department results into a single RunOutcome (done | partial).

env SCRIPTED_CHALLAN_SETTLEMENT gates the whole thing (checked in run_job.py),
so the AI path stays the default. Always returns a RunOutcome (never raises).
"""

from __future__ import annotations

import os
import traceback

import httpx
import redis

from browser_config import make_browser

from ..log import StepLogger
from ..types import RunOutcome
from . import vcourts
from .params import ChallanSettlementParams


API_URL = os.environ.get("API_URL", "http://api:3000")

# Skip reasons that mean "data genuinely absent" — these do NOT force partial.
# Anything else (site error, proceed_failed, captcha failed, no response,
# save failed, search_form_error) is a failure → partial.
_LEGIT_SKIP_PREFIXES = ("not found", "0 records", "no valid records")


def challan_settlement_scripted_enabled() -> bool:
    """True iff env SCRIPTED_CHALLAN_SETTLEMENT is a truthy toggle."""
    raw = os.environ.get("SCRIPTED_CHALLAN_SETTLEMENT", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _fetch_departments(request_id: str) -> list[str]:
    """GET the department list for this request from the API. Raises on
    transport/HTTP error so the caller can translate to a failed RunOutcome."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{API_URL}/api/internal/challans/departments",
            params={"requestId": request_id},
        )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("error") or "departments endpoint returned ok:false")
    return list(body.get("departments") or [])


def _is_failure_skip(reason: str | None) -> bool:
    r = (reason or "").lower()
    return not any(r.startswith(p) for p in _LEGIT_SKIP_PREFIXES)


def _reconcile(department_results: list[dict]) -> RunOutcome:
    confirmed = [d for d in department_results if d["status"] == "confirmed"]
    failed = [d for d in department_results if d["status"] == "failed"]
    skipped = [d for d in department_results if d["status"] == "skipped"]
    skipped_failure = [d for d in skipped if _is_failure_skip(d.get("reason"))]
    skipped_legit = [d for d in skipped if not _is_failure_skip(d.get("reason"))]

    total_saved = sum(d.get("saved", 0) for d in confirmed)

    partial_reasons: list[str] = []
    for d in failed:
        partial_reasons.append(f"{d['department']} failed ({d.get('reason')})")
    for d in skipped_failure:
        partial_reasons.append(f"{d['department']} skipped ({d.get('reason')})")

    status = "partial" if partial_reasons else "done"

    lines = [
        f"Departments queried: {len(department_results)}",
        f"Confirmed: {len(confirmed)} (discount records saved: {total_saved})",
        f"Skipped (legitimate): {len(skipped_legit)}"
        + (
            " — " + ", ".join(f"{d['department']} ({d.get('reason')})" for d in skipped_legit)
            if skipped_legit
            else ""
        ),
        f"Skipped (failure): {len(skipped_failure)}",
        f"Failed: {len(failed)}",
        "Status: complete" if status == "done" else "Status: partial — " + "; ".join(partial_reasons),
    ]
    return RunOutcome(
        status=status,
        summary="\n".join(lines),
        partial_reasons=partial_reasons,
    )


async def run_challan_settlement(
    job_params: dict,
    job_id: str,
    r: redis.Redis,
) -> RunOutcome:
    """Run the scripted challan-settlement flow. Always returns a RunOutcome."""

    # 1. Validate params.
    try:
        params = ChallanSettlementParams(**job_params)
    except Exception as e:
        return RunOutcome(
            status="failed",
            summary="Param validation failed before browser launch.",
            abort_reason=f"invalid_params: {e}",
        )

    # 2. Departments from the DB (via API) — replaces the DTP scrape.
    try:
        departments = await _fetch_departments(params.requestId)
    except Exception as e:
        return RunOutcome(
            status="failed",
            summary=f"Could not fetch departments for requestId {params.requestId}: {e}",
            abort_reason=f"departments_fetch_failed:{type(e).__name__}",
        )

    if not departments:
        return RunOutcome(
            status="done",
            summary=(
                f"0 departments in DB for requestId {params.requestId}; nothing "
                f"to settle. (The app-populated `challans` field was empty or "
                f"had no state-prefixed ids.)\nStatus: complete"
            ),
        )

    # 3. Browser (honors EGRESS_PROXY for the .gov.in portal).
    browser = make_browser(keep_alive=True)
    log = StepLogger(job_id=job_id, r=r)

    try:
        try:
            await browser.start()
        except Exception as e:
            return RunOutcome(
                status="failed",
                summary=f"Browser failed to start: {e}",
                abort_reason="browser_start_failed",
                run_log=log.dump(),
            )

        department_results: list[dict] = []
        for department in departments:
            try:
                result = await vcourts.run_department(
                    browser, params, log, r, job_id, department
                )
            except Exception as e:
                # run_department is defensive, but never let one dept kill the run.
                tb = traceback.format_exc()
                print(f"[{job_id}] department {department!r} crashed:\n{tb}")
                result = {
                    "department": department,
                    "status": "failed",
                    "reason": f"crash:{type(e).__name__}",
                    "saved": 0,
                    "dropped": 0,
                }
            department_results.append(result)

        outcome = _reconcile(department_results)
        outcome.total_cost_usd = log.total_handoff_cost()
        outcome.run_log = log.dump()
        return outcome

    finally:
        try:
            await browser.stop()
        except Exception as e:
            print(f"[{job_id}] browser.stop error: {e}")

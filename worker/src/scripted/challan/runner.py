"""Entry point for challan-payment scripted jobs (invoked by run_job.py).

Mirrors scripted.runner.run_border_tax:
  1. Validate params (ChallanPaymentParams) — fail fast on bad input.
  2. Derive the Virtual Courts department from the challan number.
  3. Spin up a browser-use Browser (SAME args as run_border_tax / agent.py,
     so the live-VNC slot behaves identically).
  4. Run vcourts.run, translating ScriptedAbort / HandoffNeeded / unexpected
     exceptions into a RunOutcome.
  5. Attach handoff cost + step log; tear the browser down.

Always returns a RunOutcome (never raises).
"""

from __future__ import annotations

import traceback

import redis
from browser_use import Browser

from ..handoff import run_ai_rescue
from ..log import StepLogger
from ..types import HandoffNeeded, RunOutcome, ScriptedAbort
from .dispatch import department_from_challan
from .params import ChallanPaymentParams
from . import vcourts


async def run_challan_payment(
    job_params: dict,
    job_id: str,
    r: redis.Redis,
) -> RunOutcome:
    """Run the scripted challan-payment flow. Always returns a RunOutcome."""

    # 1. Validate params.
    try:
        params = ChallanPaymentParams(**job_params)
    except Exception as e:  # pydantic ValidationError or anything else
        return RunOutcome(
            status="failed",
            summary="Param validation failed before browser launch.",
            abort_reason=f"invalid_params: {e}",
        )

    # 2. Resolve department from the challan number (explicit param wins).
    department = department_from_challan(params.challanNo, params.department)
    if not department:
        return RunOutcome(
            status="failed",
            summary=(
                f"Could not map challan {params.challanNo!r} to a Virtual "
                f"Courts department. Pass an explicit 'department' param or "
                f"extend scripted/challan/dispatch.py."
            ),
            abort_reason="department_unresolved",
        )

    # 3. Browser (mirrors run_border_tax exactly).
    #    NOTE: if vcourts.gov.in starts rejecting the VM's IP, swap this for
    #    make_browser() from browser_config to route via EGRESS_PROXY (that
    #    is what the AI path / agent.py uses for per-IP-filtered portals).
    browser = Browser(
        headless=False,
        chromium_sandbox=False,
        args=["--disable-dev-shm-usage", "--disable-gpu"],
        keep_alive=True,
    )
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

        try:
            outcome = await vcourts.run(
                browser,
                params,
                log,
                r,
                job_id,
                department,
            )

        except ScriptedAbort as abort:
            outcome = RunOutcome(
                status="failed",
                summary=abort.reason,
                abort_reason=abort.reason,
            )

        except HandoffNeeded as handoff:
            # vcourts.run asked for AI rescue (not used in v1, but wired so
            # it works the moment you raise HandoffNeeded somewhere).
            try:
                summary, cost = await run_ai_rescue(
                    browser,
                    goal=handoff.goal,
                    reason=handoff.reason,
                    page_context_hint=None,
                )
                outcome = RunOutcome(
                    status="partial",
                    summary=(
                        "Challan runner handed off to AI mid-flight. "
                        f"Rescue summary: {summary}"
                    ),
                    partial_reasons=[f"ai_rescue:{handoff.scope}"],
                    total_cost_usd=cost,
                )
            except Exception as rescue_err:
                outcome = RunOutcome(
                    status="failed",
                    summary=(
                        f"Challan runner handed off, but rescue agent "
                        f"crashed: {type(rescue_err).__name__}: {rescue_err}"
                    ),
                    abort_reason=f"rescue_crashed:{type(rescue_err).__name__}",
                )

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[{job_id}] challan-payment scripted run crashed:\n{tb}")
            outcome = RunOutcome(
                status="failed",
                summary=f"Challan runner crashed: {type(e).__name__}: {e}",
                abort_reason=f"runner_crashed:{type(e).__name__}",
            )

        outcome.total_cost_usd = (
            outcome.total_cost_usd or 0.0
        ) + log.total_handoff_cost()
        outcome.run_log = log.dump()
        return outcome

    finally:
        try:
            await browser.stop()
        except Exception as e:
            print(f"[{job_id}] browser.stop error: {e}")

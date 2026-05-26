# worker/src/scripted/runner.py
"""Scripted runner: entry point invoked by run_job.py for border-tax jobs.

Responsibilities:
  - Look up the state-specific runner module (e.g. scripted.border_tax.up).
  - Validate params against BorderTaxParams (catch bad inputs before any
    browser work).
  - Spin up a browser-use Browser session (same args as the AI path in
    agent.py, so the live-VNC slot behaves identically).
  - Run the state function, catching ScriptedAbort and HandoffNeeded for
    clean translation into RunOutcome.
  - Attach the in-memory step log + total handoff cost to the outcome.
  - Tear down the browser.

Enable mechanism:
  Env var SCRIPTED_BORDER_TAX_STATES is a comma-separated list of state
  short codes (UP, HR, RJ) OR full names. When empty (default), nothing is
  scripted and run_job.py falls through to the existing agent.py path.
"""

from __future__ import annotations

import importlib
import os
import traceback
from typing import Awaitable, Callable

import redis
from browser_use import Browser
from pydantic import ValidationError

from .border_tax.params import BorderTaxParams
from .handoff import run_ai_rescue
from .fetch_receipt.params import FetchReceiptParams
from .log import StepLogger
from .types import HandoffNeeded, RunOutcome, ScriptedAbort


# Map state short codes -> dotted module path. Each module must expose
#   async def run(session, params: BorderTaxParams, log: StepLogger) -> RunOutcome
# Empty in the foundation branch; populated by per-state branches.
_BORDER_TAX_STATE_MODULES: dict[str, str] = {
    "UP": "scripted.border_tax.up",
    "HR": "scripted.border_tax.hr",
    "RJ": "scripted.border_tax.rj",
    "PB": "scripted.border_tax.pb",
    "MP": "scripted.border_tax.mp",
}


# Accept either short codes or full names anywhere (env var, API param).
_STATE_TO_CODE: dict[str, str] = {
    "UP": "UP",
    "U.P.": "UP",
    "UTTAR PRADESH": "UP",
    "HR": "HR",
    "HARYANA": "HR",
    "RJ": "RJ",
    "RAJASTHAN": "RJ",
    "PUNJAB": "PB",
    "PB": "PB",
    "MP": "MP",
    "M.P.": "MP",
    "MADHYA PRADESH": "MP",
}


def normalize_state_code(state_or_name: str) -> str:
    """Map any state input to its 2-letter code (or pass through if unknown)."""
    key = (state_or_name or "").strip().upper()
    return _STATE_TO_CODE.get(key, key)


def state_is_scripted_enabled(state_or_name: str) -> bool:
    """Returns True iff the state appears in SCRIPTED_BORDER_TAX_STATES."""
    raw = os.environ.get("SCRIPTED_BORDER_TAX_STATES", "").strip()
    if not raw:
        return False
    enabled_codes = {normalize_state_code(s) for s in raw.split(",") if s.strip()}
    return normalize_state_code(state_or_name) in enabled_codes


def _load_state_runner(
    state_code: str,
) -> Callable[..., Awaitable[RunOutcome]] | None:
    mod_path = _BORDER_TAX_STATE_MODULES.get(state_code)
    if not mod_path:
        return None
    try:
        mod = importlib.import_module(mod_path)
    except Exception as e:
        print(f"[scripted] failed to import {mod_path}: {e}")
        return None
    fn = getattr(mod, "run", None)
    if not callable(fn):
        print(f"[scripted] {mod_path} has no callable `run`")
        return None
    return fn


async def run_border_tax(
    state_or_name: str,
    job_params: dict,
    job_id: str,
    r: redis.Redis,
) -> RunOutcome:
    """Run a scripted border-tax flow. Always returns a RunOutcome (never raises)."""

    state_code = normalize_state_code(state_or_name)

    # 1. Validate params -- fail fast on bad inputs.
    try:
        params = BorderTaxParams(**job_params)
    except ValidationError as e:
        return RunOutcome(
            status="failed",
            summary="Param validation failed before browser launch.",
            abort_reason=f"invalid_params: {e}",
        )

    # 2. Resolve the state-specific runner.
    state_runner = _load_state_runner(state_code)
    if state_runner is None:
        return RunOutcome(
            status="failed",
            summary=f"No scripted runner registered for state {state_code}.",
            abort_reason=f"scripted_runner_not_implemented:{state_code}",
        )

    # 3. Spin up the browser (mirrors agent.py defaults).
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
            outcome = await state_runner(browser, params, log)

        except ScriptedAbort as abort:
            outcome = RunOutcome(
                status="failed",
                summary=abort.reason,
                abort_reason=abort.reason,
            )

        except HandoffNeeded as handoff:
            # State runner asked for rescue. Run the scoped Agent.
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
                        "Scripted runner handed off to AI mid-flight. "
                        f"Rescue summary: {summary}"
                    ),
                    partial_reasons=[f"ai_rescue:{handoff.scope}"],
                    total_cost_usd=cost,
                )
            except Exception as rescue_err:
                outcome = RunOutcome(
                    status="failed",
                    summary=(
                        f"Scripted runner handed off, but rescue agent "
                        f"crashed: {type(rescue_err).__name__}: {rescue_err}"
                    ),
                    abort_reason=f"rescue_crashed:{type(rescue_err).__name__}",
                )

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[{job_id}] scripted run crashed:\n{tb}")
            outcome = RunOutcome(
                status="failed",
                summary=f"Scripted runner crashed: {type(e).__name__}: {e}",
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


# ─── Fetch-receipt path ─────────────────────────────────────────────────


async def run_fetch_receipt(
    job_params: dict,
    source: str,
    job_id: str,
    r: redis.Redis,
) -> RunOutcome:
    """Run the fetch-receipt scripted flow. Always returns a RunOutcome
    (never raises). The Browser session lifecycle mirrors run_border_tax
    so live-VNC behaves identically."""

    # 1. Inject source from the job hash into params, then validate.
    enriched = dict(job_params)
    enriched["source"] = source  # always honor the hash; ignore any
    # accidental source in params
    try:
        params = FetchReceiptParams(**enriched)
    except ValidationError as e:
        return RunOutcome(
            status="failed",
            summary="Param validation failed before browser launch.",
            abort_reason=f"invalid_params: {e}",
        )

    # 2. Resolve the runner (single module — no state dispatch).
    try:
        from .fetch_receipt.runner import run as fetch_receipt_run
    except Exception as e:
        return RunOutcome(
            status="failed",
            summary="Failed to import scripted.fetch_receipt.runner.",
            abort_reason=f"runner_import_failed: {e}",
        )

    # 3. Spin up the browser (mirrors run_border_tax exactly).
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
            outcome = await fetch_receipt_run(browser, params, log)
        except ScriptedAbort as abort:
            outcome = RunOutcome(
                status="failed",
                summary=abort.reason,
                abort_reason=abort.reason,
                run_log=log.dump(),
            )
        except Exception as e:
            traceback.print_exc()
            outcome = RunOutcome(
                status="failed",
                summary=f"Unhandled exception in fetch-receipt runner: {e}",
                abort_reason=f"unhandled:{type(e).__name__}",
                run_log=log.dump(),
            )

        # Attach total handoff cost (captcha LLM cost) to the outcome
        # the same way run_border_tax does, so saveAgentCost gets the
        # right number for this task.
        outcome.total_cost_usd = log.total_handoff_cost()

        return outcome

    finally:
        try:
            await browser.stop()
        except Exception:
            pass

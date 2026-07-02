"""Virtual Courts (vcourts.gov.in) — per-department SETTLEMENT extraction.

One department at a time (looped by runner.py). Mirrors the AI flow's Phase 2
STEP A→E (api/src/tasks/challanSettlement/prompt.ts:629-744), but scripted:

  A  VC_HOME → select department → Proceed Now → verify VC_SEARCH.
  B  Click "Challan/Vehicle No." tab, fill VEHICLE number, solve CAPTCHA
     (automated via scripted.captcha.solve_image_captcha), submit.
  C  Read VC_RESULTS records → discount records (extract.py).
  D  save_discounts (require ok:true, one retry).
  E  return a per-department result dict; runner aggregates.

No payment, no receipt, no View click — STEP C reads results only (see the
prompt: "never click View" in extraction).

=============================  SELECTORS  ==================================
All selectors below are confirmed from the live DOM. The settlement flow
searches by VEHICLE (#vehicle_no) — distinct from the payment flow's challan
search (#challan_no) — and solves the CAPTCHA itself. A wrong selector still
fails its step with a clear, named error in the step log, so a first live run
over noVNC is worth doing to confirm the police-pane scoping and the exact
department-dropdown option text.

CAPTCHA is a securimage <img> (NOT a canvas), so we use
scripted.captcha.solve_image_captcha (screenshot the <img> + OCR), not
solve_canvas_captcha.
===========================================================================
"""

from __future__ import annotations

import asyncio
import json
import time

import redis

from actions import save_discounts
from ..captcha import solve_image_captcha
from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    click,
    click_by_text,
    fill,
    navigate,
    select_by_text,
)
from ..types import ScriptedAbort, StepLog, StepStatus
from .extract import (
    NOT_FOUND_MARKER,
    RESULTS_MARKER,
    build_discount_records,
    extract_raw_records,
)
from .params import ChallanSettlementParams


VC_HOME = "https://vcourts.gov.in/virtualcourt/index.php"

# ── Selectors ────────────────────────────────────────────────────────────
# Confirmed live:
SEL_DEPARTMENT_DROPDOWN = "select"                    # select_by_text scans all <select>s
SEL_TAB_CHALLAN_VEHICLE = "a#mainMenuActive_police"   # "Challan/Vehicle No." tab
SEL_POLICE_PANE = "#nav-sbPoliceStation"              # pane activated by the tab

# CAPTCHA (confirmed): securimage <img> (id has a random suffix, so match by
# alt), input #fcaptcha_code_police, refresh <img class="refresh-btn">. Scoped
# to the police pane so we don't grab another tab's hidden captcha.
SEL_CAPTCHA_IMAGE = f"{SEL_POLICE_PANE} img[alt='CAPTCHA Image']"
SEL_CAPTCHA_INPUT = "#fcaptcha_code_police"
SEL_CAPTCHA_REFRESH: str | None = f"{SEL_POLICE_PANE} img.refresh-btn"

# Confirmed: a dedicated vehicle-number field (separate from the payment flow's
# #challan_no), and a Submit <button type="button" onclick="submitpoliceForm()">
# — matched by its onclick since it has no id.
SEL_VEHICLE_INPUT = "input#vehicle_no"
SEL_SUBMIT = "button[onclick*='submitpoliceForm']"

# ── Tunables ─────────────────────────────────────────────────────────────
SEARCH_HEADER_TIMEOUT = 20   # wait for VC_SEARCH after Proceed Now
RESULTS_POLL_TIMEOUT = 30    # wait for results after captcha+submit
CAPTCHA_MAX_ATTEMPTS = 10    # app: no human fallback; web: attempts + human


def _dept_result(
    department: str,
    status: str,
    *,
    reason: str | None = None,
    saved: int = 0,
    dropped: int = 0,
) -> dict:
    """One department's outcome. status ∈ {confirmed, skipped, failed}."""
    return {
        "department": department,
        "status": status,
        "reason": reason,
        "saved": saved,
        "dropped": dropped,
    }


async def _page_has_text(session, needle: str) -> bool:
    expr = (
        "(function(n){return ((document.body&&document.body.innerText)||'')"
        ".toUpperCase().indexOf(n.toUpperCase())>=0;})(" + json.dumps(needle) + ")"
    )
    return bool(await _cdp_eval(session, expr))


async def run_department(
    session,
    params: ChallanSettlementParams,
    log: StepLogger,
    r: redis.Redis,
    job_id: str,
    department: str,
) -> dict:
    """Extract + save one department's settlement discounts. Never raises —
    returns a _dept_result dict (a per-department failure must not kill the
    whole run)."""
    veh = params.vehicleNumber
    prefix = department.replace("(", " ").replace(")", " ")

    # ── STEP A: VC_HOME → department → Proceed Now ─────────────────────────
    try:
        await navigate(session, VC_HOME, log=log, name=f"{prefix}.home")
        await select_by_text(
            session,
            SEL_DEPARTMENT_DROPDOWN,
            department,
            log=log,
            name=f"{prefix}.select_department",
        )
        await click_by_text(session, "Proceed Now", log=log, name=f"{prefix}.proceed_now")
    except Exception as e:
        return _dept_result(department, "skipped", reason=f"site error: {type(e).__name__}")

    dept_head = department.split("(")[0].strip()
    deadline = time.monotonic() + SEARCH_HEADER_TIMEOUT
    reached = False
    while time.monotonic() < deadline:
        if await _page_has_text(session, dept_head):
            reached = True
            break
        await asyncio.sleep(0.5)
    if not reached:
        return _dept_result(department, "skipped", reason="proceed_failed")

    # ── STEP B: tab → fill vehicle → solve captcha → submit ────────────────
    try:
        await click(session, SEL_TAB_CHALLAN_VEHICLE, log=log, name=f"{prefix}.tab")
        await fill(session, SEL_VEHICLE_INPUT, veh, log=log, name=f"{prefix}.fill_vehicle")
    except Exception as e:
        return _dept_result(department, "failed", reason=f"search_form_error: {type(e).__name__}")

    async def _submit_and_check() -> bool:
        """Click Submit; return True once the results (or not-found) page
        appears — i.e. the captcha was accepted and the form advanced."""
        try:
            await click(session, SEL_SUBMIT, log=log, name=f"{prefix}.submit", retries=1)
        except Exception:
            return False
        poll_deadline = time.monotonic() + RESULTS_POLL_TIMEOUT
        while time.monotonic() < poll_deadline:
            if await _page_has_text(session, RESULTS_MARKER):
                return True
            if await _page_has_text(session, NOT_FOUND_MARKER):
                return True
            await asyncio.sleep(1)
        return False

    try:
        await solve_image_captcha(
            session,
            image_selector=SEL_CAPTCHA_IMAGE,
            input_selector=SEL_CAPTCHA_INPUT,
            refresh_selector=SEL_CAPTCHA_REFRESH,
            submit_action=_submit_and_check,
            job_id=job_id,
            r=r,
            source=params.source,
            log=log,
            name=f"{prefix}.captcha",
            max_ai_attempts=CAPTCHA_MAX_ATTEMPTS,
        )
    except ScriptedAbort as e:
        return _dept_result(department, "skipped", reason=f"captcha failed: {e.reason}")

    # ── STEP C: results → discount records ─────────────────────────────────
    if await _page_has_text(session, NOT_FOUND_MARKER):
        return _dept_result(department, "skipped", reason="not found")
    if not await _page_has_text(session, RESULTS_MARKER):
        return _dept_result(department, "skipped", reason="no response")

    raws = await extract_raw_records(session)
    valid, dropped = build_discount_records(raws)
    log.record(
        StepLog(
            index=log.next_index(),
            name=f"{prefix}.extract",
            status=StepStatus.OK,
            value=f"raw={len(raws)} valid={len(valid)} dropped={len(dropped)}",
        )
    )

    if not valid:
        # No settleable records (all paid/disposed/unreadable, or 0 records).
        reason = "0 records" if not raws else "no valid records"
        return _dept_result(department, "skipped", reason=reason, dropped=len(dropped))

    # ── STEP D: save_discounts (require ok:true, one retry) ────────────────
    resp = await save_discounts(job_id, params.model_dump(), valid)
    if not resp.get("ok"):
        resp = await save_discounts(job_id, params.model_dump(), valid)
    saved_ok = bool(resp.get("ok"))
    log.record(
        StepLog(
            index=log.next_index(),
            name=f"{prefix}.save_discounts",
            status=StepStatus.OK if saved_ok else StepStatus.FAILED,
            value=json.dumps(resp)[:300],
        )
    )
    if not saved_ok:
        return _dept_result(
            department, "failed", reason=f"save failed: {resp.get('error', 'unknown')}",
            dropped=len(dropped),
        )
    return _dept_result(
        department, "confirmed", saved=len(valid), dropped=len(dropped)
    )

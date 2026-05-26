# worker/src/scripted/border_tax/up.py
"""Uttar Pradesh border-tax scripted runner.

Walks the 10 phases mapped against api/src/tasks/borderTax/states/up.ts:

    Phase 1  - parivahan.gov.in/en/node/579, select UP
    Phase 2  - service selection, click Go
    Phase 3  - Owner Information: vehicle, Get Details, district, checkpoint
    Phase 4  - Vehicle Information: Permit Type (if empty) + Service Type
    Phase 5  - Tax Information: Tax Mode, Tax From, Tax Upto, Calculate
    Phase 6  - Disclaimer: CAPTCHA + checkbox + popups + Pay Online + Yes
    Phase 7  - Payment Gateway: select SBI, accept terms, Submit
    Phase 8  - SBIePay Lite: click UPI option
    Phase 8b - SBIePay Lite: click yellow CONFIRM (input#Go)
    Phase 9  - QR page: save_qr_code + wait_for_human
    Phase 10 - Receipt: poll for receipt page, extract fields, save_receipt PDF

UPI-only. NetBanking falls back to the AI agent path (run_job.py decides).

Phase 4 NOTE: Some vehicles arrive with Permit Type pre-filled from RC data
(visible as a populated select). Others arrive with Permit Type empty -- and
when that happens, Service Type's options never populate because they are
conditional on Permit Type. We detect this and set Permit Type ourselves
using a two-tier fallback: params.permitType, then params.permitTypeFallback.

Selectors are centralized at the top of this file so the first test-run
fixups are find-one-fix-one.
"""

from __future__ import annotations

import asyncio
import re
import time
import json

from actions import save_qr_code, save_receipt
from ._payment_wait import PaymentCaptureConfig, wait_for_payment_and_capture_receipt

from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    _current_url,
    abort_if_popup_text,
    click,
    click_by_text,
    fill,
    get_select_value,
    navigate,
    select_by_text,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from ..types import RunOutcome, StepLog, StepStatus, ScriptedAbort
from .params import BorderTaxParams
from ._extract_amount import extract_and_save_border_tax_amount
from ..handoff import run_ai_rescue
from ._web_handover import web_handover_and_capture


# ─── Selectors ─────────────────────────────────────────────────────────

# Phase 1 — parivahan landing
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"

# Phase 2 — service selection
SEL_SERVICE_DROPDOWN = "select[name='serviceName']"

# Phase 3 — owner info
SEL_VEHICLE_INPUT = "input#floatingvehicle"
SEL_DISTRICT = "select#floatingDistrict"
# best guess; will be confirmed live
SEL_CHECKPOINT = "select#floatingCheckpost"

# Phase 4 — vehicle info
# Note the parivahan typo: "floatingPrmit" (missing 'e'). Confirmed from DOM.
SEL_PERMIT_TYPE = "select#floatingPrmit"
SEL_SERVICE_TYPE = "select#floatingService"

# Phase 5 — tax info
SEL_TAX_FROM = "input#floatingTaxfrom"
SEL_TAX_UPTO = "input#uptpDate"
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingTaxMode",
    "select[name='taxmode']",
    "select[name='taxMode']",
]

# Phase 6 — disclaimer / captcha
SEL_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_CAPTCHA_INPUT = "input#inputcap"
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"

# Phase 7 — payment gateway
SEL_PG_DROPDOWN = "select#dropOperator"

# Phase 8 — SBIePay UPI
# SEL_UPI_LINK = "a#paymoderadio"
SEL_UPI_LINK = "a[aria-label='UPI']"

# Phase 8b — SBIePay confirm payment details
SEL_SBIEPAY_CONFIRM = "input#Go.btn-Yellow"

# Phase 9 — QR page
SEL_QR_IMG = "img#qrcodeImg"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5  # polite breath between phases
PERMIT_SET_TIMEOUT_SECS = 15  # per-attempt timeout when setting permit type
CHECKPOINT_POPULATE_TIMEOUT = 10

_UP_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Uttar Pradesh",
    qr_selector=SEL_QR_IMG,
    receipt_markers=[
        "GOVERNMENT OF UTTAR PRADESH",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*uttar\s*pradesh",
        r"checkpost\s*tax\s*e-?receipt",
    ],
)


# ─── Helpers ───────────────────────────────────────────────────────────


async def _select_checkpoint_with_fallback(
    session,
    selector: str,
    desired: str,
    *,
    log: StepLogger,
    name: str,
    populate_timeout: int = CHECKPOINT_POPULATE_TIMEOUT,
) -> dict:
    """Select an Entry Checkpost option.

    Logic:
      1. Wait until the dropdown has at least one non-placeholder option
         (Angular populates these after the district is chosen).
      2. If `desired` is provided and matches an option's text (exact ->
         case-insensitive equal), select it.
      3. Otherwise pick the FIRST non-empty option.

    Returns {"ok": True, "selected": "<text>", "value": "<v>",
             "fallback_used": bool} on success, {"ok": False, "reason": "..."}
    on failure.

    UP's district and checkpoint share the same name (e.g. GHAZIABAD →
    GHAZIABAD), so the desired value typically matches. The fallback
    covers edge cases where the checkpoint list has renamed entries.
    """
    started = time.monotonic()
    deadline = time.monotonic() + populate_timeout

    expr_template = (
        "(function(s, d) {"
        "  var e = document.querySelector(s);"
        "  if (!(e instanceof HTMLSelectElement)) return {ok:false, reason:'not_a_select'};"
        "  var trimmed = (d || '').trim();"
        "  var upper = trimmed.toUpperCase();"
        "  var nonEmptyCount = 0;"
        "  for (var c = 0; c < e.options.length; c++) {"
        "    if (e.options[c].value && e.options[c].value !== '') nonEmptyCount++;"
        "  }"
        "  if (nonEmptyCount === 0) return {ok:false, reason:'not_populated_yet'};"
        "  var matchedIdx = -1;"
        "  if (trimmed) {"
        "    for (var i = 0; i < e.options.length; i++) {"
        "      if ((e.options[i].text || '').trim() === trimmed) { matchedIdx = i; break; }"
        "    }"
        "    if (matchedIdx < 0) {"
        "      for (var j = 0; j < e.options.length; j++) {"
        "        if ((e.options[j].text || '').trim().toUpperCase() === upper) { matchedIdx = j; break; }"
        "      }"
        "    }"
        "  }"
        "  var fallbackUsed = false;"
        "  if (matchedIdx < 0) {"
        "    fallbackUsed = true;"
        "    for (var k = 0; k < e.options.length; k++) {"
        "      if (e.options[k].value && e.options[k].value !== '') { matchedIdx = k; break; }"
        "    }"
        "  }"
        "  if (matchedIdx < 0) return {ok:false, reason:'no_options'};"
        "  var opt = e.options[matchedIdx];"
        "  e.value = opt.value;"
        "  e.dispatchEvent(new Event('input', {bubbles:true}));"
        "  e.dispatchEvent(new Event('change', {bubbles:true}));"
        "  return {ok:true, selected:(opt.text||'').trim(), value:opt.value, fallback_used:fallbackUsed};"
        "})(" + json.dumps(selector) + ", " + json.dumps(desired or "") + ")"
    )

    last_reason = "no_result"
    while True:
        res = await _cdp_eval(session, expr_template)
        if res and res.get("ok"):
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    value=(
                        f"{res.get('selected')!r} (value={res.get('value')!r}, "
                        f"fallback={res.get('fallback_used')})"
                    ),
                )
            )
            return res
        if res:
            last_reason = res.get("reason") or "unknown"
        if time.monotonic() > deadline:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    error=f"checkpoint_select_timeout: {last_reason}",
                )
            )
            return {"ok": False, "reason": last_reason}
        await asyncio.sleep(0.5)


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run UP scripted border-tax. Always returns a RunOutcome."""

    job_id = log.job_id
    r = log.r
    job_params = params.model_dump()

    # ─── Phase 1: parivahan landing ────────────────────────────────────
    await navigate(
        session,
        "https://parivahan.gov.in/en/node/579",
        log=log,
        name="phase1.open_parivahan",
    )
    await wait_for_selector(
        session,
        SEL_STATE_DROPDOWN,
        log=log,
        name="phase1.wait_state_dropdown",
    )
    await select_by_value(
        session,
        SEL_STATE_DROPDOWN,
        "UP",
        log=log,
        name="phase1.select_state_up",
    )
    await wait_for_url(
        session,
        "checkpostv4",
        log=log,
        name="phase1.wait_service_page",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase1.settle")

    # ─── Phase 2: service selection ────────────────────────────────────
    await wait_for_selector(
        session,
        SEL_SERVICE_DROPDOWN,
        log=log,
        name="phase2.wait_service_dropdown",
        timeout=45,
    )
    await select_by_text(
        session,
        SEL_SERVICE_DROPDOWN,
        "VEHICLE TAX COLLECTION (OTHER STATE)",
        log=log,
        name="phase2.select_service",
    )
    await click_by_text(
        session,
        "Go",
        log=log,
        name="phase2.click_go",
        tag="button",
    )
    await wait_for_url(
        session,
        "taxCollectionOnline",
        log=log,
        name="phase2.wait_owner_info_page",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase2.settle")

    # ─── Phase 3: owner information ────────────────────────────────────
    await wait_for_selector(
        session,
        SEL_VEHICLE_INPUT,
        log=log,
        name="phase3.wait_vehicle_input",
    )
    await fill(
        session,
        SEL_VEHICLE_INPUT,
        params.vehicleNumber,
        log=log,
        name="phase3.fill_vehicle",
    )
    await click_by_text(
        session,
        "Get Details",
        log=log,
        name="phase3.click_get_details",
        tag="button",
    )
    await wait_for_selector(
        session,
        SEL_DISTRICT,
        log=log,
        name="phase3.wait_details_loaded",
        timeout=30,
    )

    if not params.entryDistrict:
        return RunOutcome(
            status="failed",
            summary="entryDistrict param is required for UP",
            abort_reason="missing_param:entryDistrict",
            run_log=log.dump(),
        )

    await select_by_text(
        session,
        SEL_DISTRICT,
        params.entryDistrict,
        log=log,
        name="phase3.select_district",
    )

    await asyncio.sleep(1.0)

    desired_checkpoint = params.entryCheckpoint or params.entryDistrict
    cp_result = await _select_checkpoint_with_fallback(
        session,
        SEL_CHECKPOINT,
        desired_checkpoint,
        log=log,
        name="phase3.select_checkpoint",
    )
    if not cp_result.get("ok"):
        return RunOutcome(
            status="failed",
            summary=(
                f"Could not select an Entry Checkpost for district "
                f"{params.entryDistrict!r}: {cp_result.get('reason')}. "
                f"The checkpost dropdown was empty or never populated."
            ),
            abort_reason="checkpoint_select_failed",
            run_log=log.dump(),
        )

    await click_by_text(
        session,
        "Next",
        log=log,
        name="phase3.click_next",
        tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase3.settle")

    # ─── Phase 4: vehicle information ──────────────────────────────────

    _validity_keywords = ["INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW"]
    _validity_abort = (
        f"Vehicle {params.vehicleNumber} has no valid insurance/fitness/"
        f"PUCC. Please renew before attempting border tax payment."
    )

    await abort_if_popup_text(
        session,
        _validity_keywords,
        _validity_abort,
        log=log,
        name="phase4.check_validity_on_load",
        close_selector=(
            "button.swal2-confirm, .modal-footer button, .swal-button--confirm"
        ),
    )

    # Wait for the Permit Type dropdown to appear -- this is the earliest
    # interactive control on the vehicle-info step and a reliable signal
    # that the form is ready.
    await wait_for_selector(
        session,
        SEL_PERMIT_TYPE,
        log=log,
        name="phase4.wait_vehicle_info_page",
        timeout=30,
    )

    # Some vehicles arrive with Permit Type pre-filled from RC data; others
    # arrive empty. If empty, Service Type's options can't populate (they're
    # conditional on Permit Type). Set it ourselves with a two-tier fallback.
    current_permit = await get_select_value(session, SEL_PERMIT_TYPE)
    if current_permit:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase4.permit_already_set",
                status=StepStatus.SKIPPED,
                selector=SEL_PERMIT_TYPE,
                value=current_permit,
                duration_ms=0,
            )
        )
    else:
        permit_set = False
        attempts: list[tuple[str, str]] = []  # (label, text)
        if params.permitType:
            attempts.append(("primary", params.permitType))
        if params.permitTypeFallback and params.permitTypeFallback != params.permitType:
            attempts.append(("fallback", params.permitTypeFallback))

        last_err: Exception | None = None
        for label, permit_text in attempts:
            try:
                await select_by_text(
                    session,
                    SEL_PERMIT_TYPE,
                    permit_text,
                    log=log,
                    name=f"phase4.set_permit_type.{label}",
                    timeout=PERMIT_SET_TIMEOUT_SECS,
                )
                permit_set = True
                break
            except Exception as e:
                last_err = e
                continue

        if not permit_set:
            tried = " / ".join(t for _, t in attempts) or "(none)"
            return RunOutcome(
                status="failed",
                summary=(
                    f"Permit Type was empty for vehicle "
                    f"{params.vehicleNumber} and none of the configured "
                    f"options [{tried}] were available in the dropdown. "
                    f"Last error: "
                    f"{type(last_err).__name__ if last_err else 'n/a'}: "
                    f"{last_err if last_err else 'n/a'}"
                ),
                abort_reason="permit_type_not_settable",
                run_log=log.dump(),
            )

        # Give Angular a beat to react to the Permit Type change before
        # Service Type's options are queried. select_by_text below also
        # polls with wake-up events, so this sleep is belt-and-suspenders.
        await asyncio.sleep(1.5)

    await abort_if_popup_text(
        session,
        _validity_keywords,
        _validity_abort,
        log=log,
        name="phase4.check_validity_after_permit",
        close_selector=(
            "button.swal2-confirm, .modal-footer button, .swal-button--confirm"
        ),
    )

    await select_by_text(
        session,
        SEL_SERVICE_TYPE,
        params.serviceType,
        log=log,
        name="phase4.select_service_type",
    )
    await asyncio.sleep(1.0)
    await abort_if_popup_text(
        session,
        _validity_keywords,
        _validity_abort,
        log=log,
        name="phase4.check_validity_after_service",
        close_selector=(
            "button.swal2-confirm, .modal-footer button, .swal-button--confirm"
        ),
    )
    await click_by_text(
        session,
        "Next",
        log=log,
        name="phase4.click_next",
        tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase4.settle")

    # ─── Phase 5: tax information ──────────────────────────────────────
    await wait_for_selector(
        session,
        SEL_TAX_FROM,
        log=log,
        name="phase5.wait_tax_info_page",
    )

    tax_mode_set = False
    last_err = None
    for candidate in SEL_TAX_MODE_CANDIDATES:
        try:
            await select_by_text(
                session,
                candidate,
                params.taxMode,
                log=log,
                name=f"phase5.select_tax_mode[{candidate}]",
                timeout=5,
            )
            tax_mode_set = True
            break
        except Exception as e:
            last_err = e
            continue
    if not tax_mode_set:
        return RunOutcome(
            status="failed",
            summary=(
                f"Could not find Tax Mode dropdown. Last attempt error: "
                f"{type(last_err).__name__}: {last_err}"
            ),
            abort_reason="selector_not_found:tax_mode",
            run_log=log.dump(),
        )

    await fill(
        session,
        SEL_TAX_FROM,
        params.taxFrom,
        log=log,
        name="phase5.fill_tax_from",
    )
    await fill(
        session,
        SEL_TAX_UPTO,
        params.taxUpto,
        log=log,
        name="phase5.fill_tax_upto",
    )

    await sleep_seconds(1, log=log, name="phase5.wait_calculation")

    await click_by_text(
        session,
        "Calculate Fee/Tax",
        log=log,
        name="phase5.click_calculate",
        tag="button",
    )
    await sleep_seconds(4, log=log, name="phase5.wait_calculation")

    await extract_and_save_border_tax_amount(
        session,
        log=log,
        name="phase5.extract_border_tax_amount",
    )

    await click_by_text(
        session,
        "Next",
        log=log,
        name="phase5.click_next",
        tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase5.settle")

    # assert params.source == 'app'
    if params.source == "web":
        return await web_handover_and_capture(
            session, log, r, job_id, job_params,
            vehicle_number=params.vehicleNumber,
            config=_UP_PAYMENT_CONFIG,
            extract_receipt_fields=_extract_receipt_fields,
        )

    phase6_goal = (
        "You are on (or seconds away from) the UP border tax Disclaimer page "
        "(Step 4 of 4). Required actions, in order:\n"
        "1. Read the captcha image — a small canvas with distorted characters "
        "(~145x36 px), next to a blue refresh button.\n"
        "2. Type those characters exactly into the captcha input field "
        "(id='inputcap', case-sensitive, max 8 chars).\n"
        "3. Tick the 'I confirm that above information are correct as per my "
        "knowledge' checkbox.\n"
        "4. If a popup appears with 'Receipt valid for X Days' / vehicle "
        "summary, click its Close button.\n"
        "5. Click the green 'Pay Online' button at the bottom-right.\n"
        "6. A confirmation popup will appear: 'Are you sure? You want to pay "
        "online?'. Click the green 'Yes' button.\n"
        "7. Wait for navigation. Call done when the URL contains 'etranspgi' "
        "or 'paymentgateway'.\n\n"
        "Hard rules: Do NOT click 'Previous'. Do NOT navigate away. If the "
        "captcha is rejected ('Invalid Captcha' popup), close the popup, "
        "click the blue refresh button next to the captcha image, and retry "
        "with the new image."
    )

    phase6_started = time.monotonic()
    rescue_summary: str = ""
    rescue_cost: float = 0.0
    try:
        rescue_summary, rescue_cost = await run_ai_rescue(
            session,
            goal=phase6_goal,
            reason="phase6_ai_owned",
            page_context_hint=(
                "UP border-tax disclaimer page. Vehicle/tax summary displayed. "
                "Two popups expected: 'Receipt valid for X Days' (after checkbox) "
                "and 'Are you sure?' (after Pay Online)."
            ),
            max_steps=15,
        )
    except Exception as e:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase6.ai_handoff",
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - phase6_started) * 1000),
                error=f"{type(e).__name__}: {e}",
                handoff_reason="phase6_ai_owned",
            )
        )
        raise ScriptedAbort(f"Phase 6 AI handoff crashed: {type(e).__name__}: {e}")

    # Poll for the payment gateway URL — AI may call done a moment before
    # the navigation actually settles.
    on_gateway = False
    deadline = time.monotonic() + 10
    final_url = ""
    while time.monotonic() < deadline:
        final_url = (await _current_url(session)).lower()
        if (
            "etranspgi" in final_url
            or "vahanpgi" in final_url
            or "paymentgateway" in final_url
        ):
            on_gateway = True
            break
        await asyncio.sleep(0.5)

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase6.ai_handoff",
            status=StepStatus.HANDED_OFF if on_gateway else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - phase6_started) * 1000),
            url=final_url,
            handoff_reason="phase6_ai_owned",
            handoff_summary=rescue_summary,
            handoff_cost_usd=rescue_cost,
        )
    )

    if not on_gateway:
        raise ScriptedAbort(
            f"Phase 6 AI handoff did not reach payment gateway. "
            f"Final URL: {final_url or '<empty>'}. AI summary: {rescue_summary}"
        )

    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase6.settle_after_handoff")

    # ─── Phase 7: payment gateway ──────────────────────────────────────
    await wait_for_selector(
        session,
        SEL_PG_DROPDOWN,
        log=log,
        name="phase7.wait_payment_gateway",
        timeout=20,
    )
    await select_by_value(
        session,
        SEL_PG_DROPDOWN,
        "SBI",
        log=log,
        name="phase7.select_sbi",
    )
    await _cdp_eval(
        session,
        """
        (function() {
            var cbs = document.querySelectorAll('input[type=checkbox]');
            for (var i = 0; i < cbs.length; i++) {
                if (!cbs[i].checked) { cbs[i].click(); break; }
            }
        })()
    """,
    )

    await click(
        session,
        "input#sendSubmit",
        log=log,
        name="phase7.click_submit",
    )

    await sleep_seconds(2, log=log, name="phase7.settle")

    # ─── Phase 8: SBIePay Lite welcome -> click UPI ────────────────────
    await wait_for_selector(
        session,
        SEL_UPI_LINK,
        log=log,
        name="phase8.wait_sbiepay_welcome",
        timeout=30,
    )
    await click(
        session,
        SEL_UPI_LINK,
        log=log,
        name="phase8.click_upi",
        timeout=10,
    )
    await sleep_seconds(2, log=log, name="phase8.settle")

    # ─── Phase 8b: SBIePay confirm payment details ─────────────────────
    await wait_for_selector(
        session,
        SEL_SBIEPAY_CONFIRM,
        log=log,
        name="phase8b.wait_confirm_page",
        timeout=30,
    )
    await click(
        session,
        SEL_SBIEPAY_CONFIRM,
        log=log,
        name="phase8b.click_confirm",
    )
    await sleep_seconds(2, log=log, name="phase8b.settle")

    # ─── Phase 9: QR page + human pays via UPI ─────────────────────────
    await wait_for_selector(
        session,
        SEL_QR_IMG,
        log=log,
        name="phase9.wait_qr_page",
        timeout=30,
    )

    qr_started = time.monotonic()
    qr_result = await save_qr_code(session, job_id, job_params)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase9.save_qr_code",
            status=(StepStatus.OK if qr_result.get("ok") else StepStatus.FAILED),
            duration_ms=int((time.monotonic() - qr_started) * 1000),
            error=None if qr_result.get("ok") else qr_result.get("error"),
        )
    )
    return await wait_for_payment_and_capture_receipt(
        session, log, r, job_id, job_params,
        vehicle_number=params.vehicleNumber,
        config=_UP_PAYMENT_CONFIG,
        extract_receipt_fields=_extract_receipt_fields,
    )


# ─── Receipt parsing helpers ───────────────────────────────────────────

_MONTH_TO_NUM = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def _normalize_receipt_date(s: str) -> str | None:
    """26-Apr-2026 -> 2026-04-26."""
    m = re.match(r"(\d{1,2})-(\w{3})-(\d{4})", s.strip(), re.IGNORECASE)
    if not m:
        return None
    day, mon_abbr, year = m.group(1).zfill(2), m.group(2).upper(), m.group(3)
    mon_num = _MONTH_TO_NUM.get(mon_abbr)
    if not mon_num:
        return None
    return f"{year}-{mon_num}-{day}"


async def _extract_receipt_fields(session, vehicle_number: str) -> dict | None:
    """Scrape receiptNumber, amount, paymentDate from the receipt page text."""

    text = await _cdp_eval(session, "document.body.innerText || ''")
    if not text:
        return None

    receipt_match = re.search(
        r"Receipt\s*No\.?\s*:?\s*([A-Z0-9]+)",
        text,
        re.IGNORECASE,
    )
    receipt_number = receipt_match.group(1) if receipt_match else None

    amount_match = re.search(
        r"Grand\s*Total\s*:?\s*(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    amount = float(amount_match.group(1)) if amount_match else None

    date_match = re.search(
        r"Payment\s*Confirmation\s*Date\s*:?\s*(\d{1,2}-\w{3}-\d{4})",
        text,
        re.IGNORECASE,
    )
    payment_date = _normalize_receipt_date(date_match.group(1)) if date_match else None

    if not (receipt_number and amount is not None and payment_date):
        print(
            f"[up] receipt parse incomplete: "
            f"receipt_number={receipt_number} "
            f"amount={amount} "
            f"payment_date={payment_date}"
        )
        return None

    return {
        "vehicleNumber": vehicle_number,
        "receiptNumber": receipt_number,
        "amount": amount,
        "paymentDate": payment_date,
    }

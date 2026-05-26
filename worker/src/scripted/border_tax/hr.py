# worker/src/scripted/border_tax/hr.py
"""Haryana border-tax scripted runner.

Walks the 12 phases mapped against api/src/tasks/borderTax/states/hr.ts:

    Phase 1   - parivahan.gov.in/en/node/579, select HR
    Phase 2   - service selection, click Go
    Phase 3   - Owner Information: vehicle, Get Details, district, checkpost
    Phase 4   - Vehicle Information: Vehicle Category / Permit Type /
                Service Type / Distance — set only if EMPTY (RC data often
                pre-fills these). Validity-popup check before & after.
    Phase 5   - Tax Information: Tax Mode, Tax From, Tax Upto (datetime-local
                YYYY-MM-DDTHH:MM, NOT plain date), Calculate, Next.
    Phase 6   - Disclaimer (AI handoff): CAPTCHA + "Receipt valid for X Days"
                popup + Pay Online + "Are you sure?" → reach payment gateway.
    Phase 7   - Payment Gateway: select EGRAS-SBIA, accept terms, click
                submit (input#sendSubmit — it's an <input>, not a <button>).
    Phase 8   - eGRAS Haryana (egrashry.nic.in):
                  • SweetAlert2 popup "Charges for Online transaction!" → OK
                  • Override window.alert/confirm so the next native dialogs
                    auto-accept without blocking.
                  • Click "Continue" (input#ctl00_ContentPlaceHolder1_btnGo).
                  • Native "Please verify..." confirm → auto-accepted.
                  • Native "Please note down GRN..." alert → auto-accepted.
                  • Page redirects to merchant.sbi.bank.in (SBIePay Lite).
    Phase 9   - SBIePay Lite: click UPI option (a[aria-label='UPI']).
    Phase 9b  - SBIePay Lite Cyber Treasury confirm: click yellow CONFIRM
                (input#Go) — same selector UP uses.
    Phase 10  - QR page: save_qr_code + wait_for_human.
    Phase 11  - Receipt: poll for receipt page, extract HRR fields,
                save_receipt PDF.

UPI-only. Net banking falls back to the AI agent path (run_job.py decides).

Phase 4 NOTE: HR vehicles arrive from RC with most fields populated
(Vehicle Type, Vehicle Class, Vehicle Category, Permit Type, Seating
Capacity, Service Type, Distance, validities). User spec says "if any of
these fields are already filled, we keep em as it is". We read each
target field's value first and only fill the empty ones — matches UP's
permit-type approach.

Phase 5 NOTE: HR's date inputs are datetime-local, NOT date. They reject
plain "YYYY-MM-DD"; the accepted DOM value is "YYYY-MM-DDTHH:MM" (literal
T). We append T00:00 to the ISO date the API normalized.

Phase 7 NOTE: HR payment gateway dropdown lists THREE EGRAS aggregators
(IDBI, PNB, SBI). We pick value="EGRAS-SBIA" so the rest of the SBIePay
flow stays the same as UP.

Phase 8 NOTE (new vs UP): UP goes directly from sendSubmit → SBIePay.
HR inserts an eGRAS intermediate page (egrashry.nic.in) with a
SweetAlert2 popup and two native browser confirm/alert dialogs.
SweetAlert2 = DOM, we click .swal2-confirm. Native dialogs = we
neutralize window.alert/confirm before triggering them.

Selectors are centralized at the top so first-test-run fixups stay
find-one-fix-one.
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
SEL_CHECKPOINT = "select#floatingCheckpost"

# Phase 4 — vehicle info
# Note the parivahan typo carried over from UP: "floatingPrmit" (missing 'e').
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"
SEL_PERMIT_TYPE = "select#floatingPrmit"
SEL_SERVICE_TYPE = "select#floatingService"
SEL_DISTANCE = "input#floatingDistance"

# Phase 5 — tax info
# HR's id is lowercase 'floatingtaxmode' (different from UP's variations).
# We still keep a candidate list because Angular re-renders sometimes shuffle
# IDs between portal versions.
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select[name='taxMode']",
    "select[name='taxmode']",
]
SEL_TAX_FROM = "input#floatingTaxfrom"
# Yes, "uptpDate" — the page's id is a typo of "uptoDate" and the label's
# for-attribute (floatingTaxupto) points to a non-existent id. The real
# input id is "uptpDate".
SEL_TAX_UPTO = "input#uptpDate"

# Phase 6 — disclaimer / captcha (AI-owned, kept for reference)
SEL_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_CAPTCHA_INPUT = "input#inputcap"
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"

# Phase 7 — payment gateway
SEL_PG_DROPDOWN = "select#dropOperator"
SEL_PG_TERMS_CHECKBOX = "input#checkme"
SEL_PG_SUBMIT = "input#sendSubmit"

# Phase 8 — eGRAS Haryana intermediate
SEL_EGRAS_SWAL_OK = "button.swal2-confirm"
SEL_EGRAS_CONTINUE = "input#ctl00_ContentPlaceHolder1_btnGo"

# Phase 9 — SBIePay UPI
SEL_UPI_LINK = "a[aria-label='UPI']"

# Phase 9b — SBIePay Cyber Treasury confirm
SEL_SBIEPAY_CONFIRM = "input#Go.btn-Yellow"

# Phase 10 — QR page
SEL_QR_IMG = "img#qrcodeImg"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5
PERMIT_SET_TIMEOUT_SECS = 15         # per-attempt timeout when setting permit type
EGRAS_NAVIGATION_TIMEOUT = 45        # post-submit → eGRAS page load
EGRAS_REDIRECT_TIMEOUT = 60          # eGRAS Continue → SBIePay redirect
CHECKPOINT_POPULATE_TIMEOUT = 10


_HR_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Haryana",
    qr_selector=SEL_QR_IMG,
    receipt_markers=[
        "GOVERNMENT OF HARYANA",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*haryana",
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

    HR's district and checkpoint share the same name (e.g. FARIDABAD →
    FARIDABAD), so the desired value typically matches. The fallback
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
    """Run HR scripted border-tax. Always returns a RunOutcome."""

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
        "HR",
        log=log,
        name="phase1.select_state_hr",
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
            summary="entryDistrict param is required for HR",
            abort_reason="missing_param:entryDistrict",
            run_log=log.dump(),
        )

    # Entry District MUST be selected before Entry Checkpost — the
    # Checkpost dropdown's options are conditional on the district choice.
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
    #
    # User spec: "if any of these fields are already filled, we keep em as
    # it is". So for each of Vehicle Category, Permit Type, Service Type,
    # Distance: read current value first, fill only when empty.

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

    # Wait for the Permit Type dropdown to appear — earliest interactive
    # control on the vehicle-info step, reliable "form is ready" signal.
    await wait_for_selector(
        session,
        SEL_PERMIT_TYPE,
        log=log,
        name="phase4.wait_vehicle_info_page",
        timeout=30,
    )

    # 4a. Vehicle Category — we don't have a param default, but it's
    # almost always pre-filled from RC ("LIGHT PASSENGER VEHICLE"). Just
    # log if empty and continue; the form's Next click will surface the
    # error if it actually matters for this vehicle.
    try:
        veh_cat_val = await get_select_value(session, SEL_VEHICLE_CATEGORY)
    except Exception:
        veh_cat_val = None
    if not veh_cat_val:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase4.vehicle_category_empty",
                status=StepStatus.RETRIED,
                value="<empty>",
                error=(
                    "Vehicle Category dropdown is empty (no RC pre-fill and "
                    "no param). Proceeding — Next may fail if HR requires it."
                ),
            )
        )

    # 4b. Permit Type — fill if empty, with primary + fallback chain
    # (mirrors UP's approach).
    permit_val = await get_select_value(session, SEL_PERMIT_TYPE)
    if not permit_val:
        permit_candidates = [
            params.permitType,
            params.permitTypeFallback,
            "NOT APPLICABLE",
        ]
        # de-dupe while preserving order
        seen: set[str] = set()
        permit_candidates = [
            p for p in permit_candidates
            if p and not (p in seen or seen.add(p))
        ]

        last_err = None
        permit_set = False
        for cand in permit_candidates:
            try:
                await select_by_text(
                    session,
                    SEL_PERMIT_TYPE,
                    cand,
                    log=log,
                    name=f"phase4.select_permit_type[{cand}]",
                    timeout=PERMIT_SET_TIMEOUT_SECS,
                )
                permit_set = True
                break
            except Exception as e:
                last_err = e
                continue
        if not permit_set:
            return RunOutcome(
                status="failed",
                summary=(
                    f"Could not set Permit Type on HR vehicle-info page. "
                    f"Tried {permit_candidates}. "
                    f"Last error: "
                    f"{type(last_err).__name__ if last_err else 'n/a'}: "
                    f"{last_err if last_err else 'n/a'}"
                ),
                abort_reason="permit_type_not_settable",
                run_log=log.dump(),
            )
        # Let Angular react before Service Type's options are queried.
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

    # 4c. Service Type — fill if empty.
    service_val = await get_select_value(session, SEL_SERVICE_TYPE)
    if not service_val:
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

    # 4d. Distance — text input, not a select. Read its current .value
    # via CDP; fill only when empty. Default 1000 if param missing.
    distance_value = await _cdp_eval(
        session,
        "(function(){var e=document.querySelector("
        "'input#floatingDistance');return e?e.value:'';})()",
    ) or ""
    if not distance_value.strip():
        distance_to_set = (params.distance or "1000").strip()
        await fill(
            session,
            SEL_DISTANCE,
            distance_to_set,
            log=log,
            name="phase4.fill_distance",
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

    # 5a. Tax Mode — try candidate selectors.
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
                f"Could not find Tax Mode dropdown on HR tax-info page. "
                f"Last attempt error: "
                f"{type(last_err).__name__}: {last_err}"
            ),
            abort_reason="selector_not_found:tax_mode",
            run_log=log.dump(),
        )

    # 5b/5c. Tax From / Tax Upto — datetime-local inputs.
    # The DOM .value the input ACCEPTS is "YYYY-MM-DDTHH:MM" (literal T).
    # Plain YYYY-MM-DD will SILENTLY FAIL on these fields.
    tf_dtlocal = f"{params.taxFrom}T00:00"
    tu_dtlocal = f"{params.taxUpto}T00:00"

    await fill(
        session,
        SEL_TAX_FROM,
        tf_dtlocal,
        log=log,
        name="phase5.fill_tax_from",
    )
    await fill(
        session,
        SEL_TAX_UPTO,
        tu_dtlocal,
        log=log,
        name="phase5.fill_tax_upto",
    )

    # Verify both fields actually accepted the value. If either is empty,
    # the form's "min" attribute likely rejected the date as out-of-range.
    tf_actual = await _cdp_eval(
        session,
        "(function(){var e=document.querySelector("
        "'input#floatingTaxfrom');return e?e.value:'';})()",
    ) or ""
    tu_actual = await _cdp_eval(
        session,
        "(function(){var e=document.querySelector("
        "'input#uptpDate');return e?e.value:'';})()",
    ) or ""

    if not tf_actual or not tf_actual.startswith(params.taxFrom):
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax From date {params.taxFrom} did not stick in the "
                f"datetime-local input (got {tf_actual!r}). Most likely the "
                f"date is before the field's min attribute (HR rejects past "
                f"dates)."
            ),
            abort_reason="tax_from_before_min",
            run_log=log.dump(),
        )
    if not tu_actual or not tu_actual.startswith(params.taxUpto):
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax Upto date {params.taxUpto} did not stick in the "
                f"datetime-local input (got {tu_actual!r}). Most likely the "
                f"date is before the field's min attribute (often forced to "
                f">= Tax From)."
            ),
            abort_reason="tax_upto_before_min",
            run_log=log.dump(),
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

    if params.source == "web":
        return await web_handover_and_capture(
            session, log, r, job_id, job_params,
            vehicle_number=params.vehicleNumber,
            config=_HR_PAYMENT_CONFIG,
            extract_receipt_fields=_extract_receipt_fields,
        )

    # ─── Phase 6: disclaimer (AI handoff) ──────────────────────────────
    # Captcha + checkbox + "Receipt valid for X Days" popup + Pay Online +
    # "Are you sure?" popup → reach payment gateway. Identical surface to
    # UP, just HARYANA wording. AI exits when URL contains etranspgi.
    phase6_goal = (
        "You are on (or seconds away from) the HR border tax Disclaimer page "
        "(Step 4 of 4) for HARYANA. Required actions, in order:\n"
        "1. Read the captcha image — a small canvas with distorted characters "
        "(~145x36 px), next to a blue refresh button.\n"
        "2. Type those characters exactly into the captcha input field "
        "(id='inputcap', case-sensitive, max 8 chars).\n"
        "3. Tick the 'I confirm that above information are correct as per my "
        "knowledge' checkbox.\n"
        "4. If a popup appears with 'Vehicle Number: ...' / 'Tax From' / "
        "'Tax Upto' / 'Receipt valid for X Days' / vehicle summary, click "
        "its Close button.\n"
        "5. Click the green 'Pay Online' button at the bottom-right.\n"
        "6. A confirmation popup will appear: 'Are you sure? You want to pay "
        "online ?'. Click the green 'Yes' button.\n"
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
                "HR border-tax disclaimer page. Vehicle/tax summary displayed. "
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
    # Same page structure as UP, but the aggregator dropdown lists 3 EGRAS
    # options (IDBI, PNB, SBI) and submit is an <input>, not a <button>.
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
        "EGRAS-SBIA",
        log=log,
        name="phase7.select_sbi_egras",
    )

    # Tick the "I accept terms and conditions" checkbox.
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
        SEL_PG_SUBMIT,
        log=log,
        name="phase7.click_submit",
    )
    await sleep_seconds(2, log=log, name="phase7.settle")

    # ─── Phase 8: eGRAS Haryana intermediate ───────────────────────────
    # Sequence:
    #   1. Page lands on egrashry.nic.in with a SweetAlert2 "Charges for
    #      Online transaction!" popup → click .swal2-confirm (OK).
    #   2. Neutralize window.alert/confirm so the two upcoming native
    #      dialogs don't block.
    #   3. Click Continue (input#ctl00_ContentPlaceHolder1_btnGo).
    #   4. Page fires window.confirm "Please verify..." → auto-accept
    #      (override returns true).
    #   5. Page fires window.alert "Please note down GRN..." → no-op
    #      (override).
    #   6. Page redirects to merchant.sbi.bank.in (SBIePay Lite).

    await wait_for_url(
        session,
        "egrashry",
        log=log,
        name="phase8.wait_egras_page",
        timeout=EGRAS_NAVIGATION_TIMEOUT,
    )

    # Step 8a: dismiss the "Charges for Online transaction!" SweetAlert2.
    # Use a generous timeout — the page sometimes takes a beat to mount.
    await wait_for_selector(
        session,
        SEL_EGRAS_SWAL_OK,
        log=log,
        name="phase8a.wait_charges_popup",
        timeout=30,
    )
    await click(
        session,
        SEL_EGRAS_SWAL_OK,
        log=log,
        name="phase8a.click_charges_ok",
    )
    await sleep_seconds(1.0, log=log, name="phase8a.settle")

    # Step 8b: neutralize native browser dialogs BEFORE clicking Continue.
    # We override window.alert/confirm/prompt right here. This works even
    # though the page is already loaded because we inject before the user
    # gesture (Continue click) that triggers the dialogs. The page's own
    # handler is queued behind the .click() listener, and by the time it
    # calls confirm()/alert() our overrides are in place.
    await _cdp_eval(
        session,
        """
        (function() {
            window.alert = function() { return undefined; };
            window.confirm = function() { return true; };
            window.prompt = function() { return ''; };
        })()
        """,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase8b.override_native_dialogs",
            status=StepStatus.OK,
            value="alert/confirm/prompt -> no-op/true/''",
        )
    )

    # Step 8c: click Continue. The page will fire the two native dialogs
    # internally; both are silently accepted by the overrides above. Then
    # it redirects to merchant.sbi.bank.in.
    await wait_for_selector(
        session,
        SEL_EGRAS_CONTINUE,
        log=log,
        name="phase8c.wait_continue_button",
        timeout=20,
    )
    await click(
        session,
        SEL_EGRAS_CONTINUE,
        log=log,
        name="phase8c.click_continue",
    )

    # Step 8d: wait for the SBIePay Lite redirect. We accept either the
    # SBIePay payment-method-selection page (full URL contains
    # merchant.sbi.bank.in) OR the older SBMOPS hostname.
    await wait_for_url(
        session,
        "sbi.bank.in",
        log=log,
        name="phase8d.wait_sbiepay_redirect",
        timeout=EGRAS_REDIRECT_TIMEOUT,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase8.settle")

    # ─── Phase 9: SBIePay Lite UPI selection ───────────────────────────
    # Same UI surface as UP. Click the UPI link in "Other Payment Modes".
    await wait_for_selector(
        session,
        SEL_UPI_LINK,
        log=log,
        name="phase9.wait_sbiepay_welcome",
        timeout=30,
    )
    await click(
        session,
        SEL_UPI_LINK,
        log=log,
        name="phase9.click_upi",
        timeout=10,
    )
    await sleep_seconds(2, log=log, name="phase9.settle")

    # ─── Phase 9b: SBIePay confirm payment details ─────────────────────
    # Cyber Treasury / Haryana payment details page → yellow CONFIRM.
    await wait_for_selector(
        session,
        SEL_SBIEPAY_CONFIRM,
        log=log,
        name="phase9b.wait_confirm_page",
        timeout=30,
    )
    await click(
        session,
        SEL_SBIEPAY_CONFIRM,
        log=log,
        name="phase9b.click_confirm",
    )
    await sleep_seconds(2, log=log, name="phase9b.settle")

    # ─── Phase 10: QR page + human pays via UPI ────────────────────────
    await wait_for_selector(
        session,
        SEL_QR_IMG,
        log=log,
        name="phase10.wait_qr_page",
        timeout=30,
    )

    qr_started = time.monotonic()
    qr_result = await save_qr_code(session, job_id, job_params)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase10.save_qr_code",
            status=(StepStatus.OK if qr_result.get("ok") else StepStatus.FAILED),
            duration_ms=int((time.monotonic() - qr_started) * 1000),
            error=None if qr_result.get("ok") else qr_result.get("error"),
        )
    )

    return await wait_for_payment_and_capture_receipt(
        session, log, r, job_id, job_params,
        vehicle_number=params.vehicleNumber,
        config=_HR_PAYMENT_CONFIG,
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
    """12-May-2026 -> 2026-05-12. Whitespace and trailing comma tolerated."""
    m = re.match(r"(\d{1,2})-(\w{3})-(\d{4})", s.strip(), re.IGNORECASE)
    if not m:
        return None
    day, mon_abbr, year = m.group(1).zfill(2), m.group(2).upper(), m.group(3)
    mon_num = _MONTH_TO_NUM.get(mon_abbr)
    if not mon_num:
        return None
    return f"{year}-{mon_num}-{day}"


async def _extract_receipt_fields(session, vehicle_number: str) -> dict | None:
    """Scrape receiptNumber, amount, paymentDate from the HR receipt page.

    Sample format from a confirmed receipt (12-May-2026 print):
      Receipt No.            : HRR2605120014548
      Grand Total : 100/- One Hundred Rupees Only
      Payment Confirmation Date  : 12-May-2026, 9:46:54 PM
    """
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

    # HR's Payment Confirmation Date includes a time component
    # ("12-May-2026, 9:46:54 PM"), but we only need the date portion.
    date_match = re.search(
        r"Payment\s*Confirmation\s*Date\s*:?\s*(\d{1,2}-\w{3}-\d{4})",
        text,
        re.IGNORECASE,
    )
    payment_date = _normalize_receipt_date(date_match.group(1)) if date_match else None

    if not (receipt_number and amount is not None and payment_date):
        print(
            f"[hr] receipt parse incomplete: "
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

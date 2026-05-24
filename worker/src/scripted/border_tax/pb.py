# worker/src/scripted/border_tax/pb.py
"""Punjab border-tax scripted runner.

Walks the 12 phases mapped against api/src/tasks/borderTax/states/pb.ts:

    Phase 1   - parivahan.gov.in/en/node/579, select PB
    Phase 2   - service selection, click Go
    Phase 3   - Owner Information: vehicle, Get Details, district,
                checkpost (param if it matches; else first non-empty option).
    Phase 4   - Vehicle Information: Vehicle Category / Permit Type /
                Service Type — set only if EMPTY (RC data often pre-fills
                these). Validity-popup check before & after. NO Distance
                field on PB (unlike HR).
    Phase 5   - Tax Information: Tax Mode (DAYS or QUARTERLY ONLY — no
                MONTHLY on PB), Tax From, Tax Upto (datetime-local
                YYYY-MM-DDTHH:MM, NOT plain date), Calculate, Next.
    Phase 6   - Disclaimer (AI handoff): CAPTCHA + "Receipt valid for X Days"
                popup + Pay Online + "Are you sure?" → reach payment gateway.
    Phase 7   - Payment Gateway: select IFMS, accept terms, click submit
                (input#sendSubmit — same id as HR, but only one aggregator
                option exists for PB).
    Phase 8   - IFMS Bank Selection (Punjab-specific intermediate):
                  • Wait for select#Bank to mount.
                  • Select value="1001509" (SBI BANK). PB's IFMS lists three
                    options (PNB Aggregate / SBI BANK / UNION BANK OF INDIA);
                    SBI BANK is what continues into the SBIePay flow.
                  • Click the Continue submit input.
                  • Page redirects to merchant.onlinesbi.sbi (SBIePay Lite).
    Phase 9   - SBIePay Lite: click UPI option (a[aria-label='UPI']).
    Phase 9b  - SBIePay Lite Cyber Treasury confirm: click yellow CONFIRM
                (input#Go) — same selector UP/HR use.
    Phase 10  - QR page: save_qr_code + wait_for_human.
    Phase 11  - Receipt: poll for receipt page, extract fields (generic —
                we don't pin to a specific receipt-no prefix yet), save_receipt
                PDF.

UPI-only. Net banking falls back to the AI agent path (run_job.py decides).

DIFFERENCES vs HR
=================
1. Phase 3: PB's Entry Checkpost list often has just one or two compound
   names that don't match the district name (e.g. district MOHALI ->
   checkpost KHARAR). Per spec, we try params.entryCheckpoint first; if
   that doesn't match an option, we fall back to picking the FIRST
   non-empty option. The fallback mirrors RJ's approach for the same
   class of unstable / compound checkpost names.
2. Phase 4: NO Distance input on PB (HR has input#floatingDistance,
   PB doesn't). The Vehicle Category / Permit Type / Service Type
   surface is otherwise identical to HR.
3. Phase 5 Tax Mode: PB dropdown lists DAYS and QUARTERLY ONLY. If a
   job ships in with taxMode="MONTHLY", select_by_text will fail at
   option-not-found and the run aborts. That's acceptable -- the API
   should not be normalizing to MONTHLY for PB.
4. Phase 7: aggregator dropdown has a single option, value="IFMS"
   (vs HR's three EGRAS aggregators). Same select#dropOperator
   selector, same submit input#sendSubmit.
5. Phase 8: completely different intermediate page vs HR.
   - HR -> egrashry.nic.in (SweetAlert2 + native dialogs + Continue).
   - PB -> IFMS bank-selection page (select#Bank with three banks +
     Continue submit). NO popups; the only action is select-bank+continue.
   We don't know the exact host, so we detect the page by the bank
   selector mounting rather than waiting on a specific URL.
6. Phase 9 redirect target: SBI's host string on PB is
   "merchant.onlinesbi.sbi" (the user-shared screenshot). HR observed
   "merchant.sbi.bank.in". We accept either via the broad token "sbi"
   in the URL (combined with the UPI link selector arming on the next
   step as a tighter sanity check).
7. Phase 11 receipt: we use a generic regex (no PBR/PBT prefix lock)
   because we don't have a confirmed PB receipt sample yet. The marker
   "GOVERNMENT OF PUNJAB" + "CHECKPOST TAX E-RECEIPT" + "RECEIPT NO" +
   "GRAND TOTAL" gates the extraction the same way HR/UP do.

Selectors are centralized at the top so first-test-run fixups stay
find-one-fix-one.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from actions import save_qr_code, save_receipt

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
from ..handoff import run_ai_rescue
from ._payment_wait import PaymentCaptureConfig, wait_for_payment_and_capture_receipt

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
# Note the parivahan typo carried over from UP/HR: "floatingPrmit" (missing 'e').
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"
SEL_PERMIT_TYPE = "select#floatingPrmit"
SEL_SERVICE_TYPE = "select#floatingService"
# (No SEL_DISTANCE on PB.)

# Phase 5 — tax info
# PB's id is lowercase 'floatingtaxmode' (matches HR). Keep candidate list
# in case Angular re-renders shuffle IDs between portal versions.
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select[name='taxMode']",
    "select[name='taxmode']",
]
SEL_TAX_FROM = "input#floatingTaxfrom"
# Yes, "uptpDate" — same typo as UP/HR. The label's for-attribute
# (floatingTaxupto) points to a non-existent id; the real input id is
# "uptpDate".
SEL_TAX_UPTO = "input#uptpDate"

# Phase 6 — disclaimer / captcha (AI-owned, kept for reference)
SEL_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_CAPTCHA_INPUT = "input#inputcap"
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"

# Phase 7 — payment gateway
SEL_PG_DROPDOWN = "select#dropOperator"
SEL_PG_TERMS_CHECKBOX = "input#checkme"
SEL_PG_SUBMIT = "input#sendSubmit"

# Phase 8 — IFMS bank selection (Punjab-specific)
# The IFMS intermediate page has no stable URL signal we trust, so we wait
# for the bank dropdown itself. The Continue input has no id; we identify
# it by the class+attribute combination from the live page HTML.
SEL_BANK_DROPDOWN = "select#Bank"
SEL_BANK_CONTINUE = "input.btnsubmit[type='submit'][value='Continue']"
# Option values inside select#Bank (used directly in case text matching
# breaks on whitespace/case quirks of the rendered options).
BANK_VALUE_SBI = "1001509"

# Phase 9 — SBIePay UPI
SEL_UPI_LINK = "a[aria-label='UPI']"

# Phase 9b — SBIePay Cyber Treasury confirm
SEL_SBIEPAY_CONFIRM = "input#Go.btn-Yellow"

# Phase 10 — QR page
SEL_QR_IMG = "img#qrcodeImg"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5
PERMIT_SET_TIMEOUT_SECS = 15         # per-attempt timeout when setting permit type
IFMS_BANK_PAGE_TIMEOUT = 45          # post-submit -> IFMS bank-selection page mount
SBIEPAY_REDIRECT_TIMEOUT = 60        # IFMS Continue -> SBIePay redirect
CHECKPOINT_POPULATE_TIMEOUT = 10     # district -> checkpost options populated

_PB_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Punjab",
    qr_selector=SEL_QR_IMG,
    receipt_markers=[
        "GOVERNMENT",
        "GOVERNMENT OF PUNJAB",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*punjab",
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

    Mirrors RJ's "pick the first checkpost we see" fallback because PB's
    checkpost names don't always match a sensible param ("KHARAR" for the
    MOHALI district, etc.).
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
    """Run PB scripted border-tax. Always returns a RunOutcome."""

    job_id = log.job_id
    r = log.r
    # params.serviceType = "NOT APPLICABLE"
    # params.permitType = "NOT APPLICABLE"
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
        "PB",
        log=log,
        name="phase1.select_state_pb",
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
            summary="entryDistrict param is required for PB",
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

    # Give Angular a beat for the checkpost options to populate after the
    # district change before we read them.
    await asyncio.sleep(1.0)

    # PB-specific: try params.entryCheckpoint first; if no match, pick the
    # first non-empty option from the dropdown. We never abort on an
    # unmatched checkpoint name here — the spec is "use the param if it
    # matches, otherwise default to the first available checkpoint".
    cp_result = await _select_checkpoint_with_fallback(
        session,
        SEL_CHECKPOINT,
        params.entryCheckpoint or "",
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
    # it is". For each of Vehicle Category, Permit Type, Service Type:
    # read current value first, fill only when empty. NO Distance input on
    # PB (HR has one; PB doesn't).

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

    # 4a. Vehicle Category — usually pre-filled from RC ("LIGHT PASSENGER
    # VEHICLE"). If empty, log a warning and continue; the form's Next
    # click will surface the error if it actually matters for this vehicle.
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
                    "no param). Proceeding — Next may fail if PB requires it."
                ),
            )
        )

    # 4b. Permit Type — fill if empty, with primary + fallback chain
    # (mirrors UP/HR's approach).
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
                    f"Could not set Permit Type on PB vehicle-info page. "
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

    # NO Distance input on PB (unlike HR's input#floatingDistance).
    # Skip straight to Next.

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
    # PB dropdown only has DAYS and QUARTERLY. If params.taxMode is
    # "MONTHLY", this fails at option_not_found and aborts. That's the
    # right behavior: the API should not be normalizing to MONTHLY for PB.
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
                f"Could not set Tax Mode {params.taxMode!r} on PB tax-info "
                f"page (PB only supports DAYS and QUARTERLY). Last attempt "
                f"error: {type(last_err).__name__ if last_err else 'n/a'}: "
                f"{last_err if last_err else 'n/a'}"
            ),
            abort_reason="selector_not_found:tax_mode",
            run_log=log.dump(),
        )

    # 5b/5c. Tax From / Tax Upto — datetime-local inputs.
    # The DOM .value the input ACCEPTS is "YYYY-MM-DDTHH:MM" (literal T).
    # Plain YYYY-MM-DD will SILENTLY FAIL on these fields. Same as HR.
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
                f"date is before the field's min attribute (PB rejects past "
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

    await click_by_text(
        session,
        "Calculate Fee/Tax",
        log=log,
        name="phase5.click_calculate",
        tag="button",
    )
    await sleep_seconds(4, log=log, name="phase5.wait_calculation")
    await click_by_text(
        session,
        "Next",
        log=log,
        name="phase5.click_next",
        tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase5.settle")

    # ─── Phase 6: disclaimer (AI handoff) ──────────────────────────────
    # Same surface as UP/HR — captcha, checkbox, "Receipt valid for X
    # Days" popup, Pay Online, "Are you sure?" popup → reach payment
    # gateway. AI exits when URL contains etranspgi/vahanpgi/paymentgateway.
    phase6_goal = (
        "You are on (or seconds away from) the PB border tax Disclaimer page "
        "(Step 4 of 4) for PUNJAB. Required actions, in order:\n"
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
                "PB border-tax disclaimer page. Vehicle/tax summary displayed. "
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

    # ─── Phase 7: payment gateway (eTransPgi) ──────────────────────────
    # Same selectors as HR (select#dropOperator + input#sendSubmit), but
    # PB's aggregator dropdown has a single option, value="IFMS". Submit
    # is an <input>, not a <button>.
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
        "IFMS",
        log=log,
        name="phase7.select_ifms",
    )

    # Tick the "I accept terms and conditions" checkbox. The input has
    # id="checkme" but we tick the first unchecked checkbox defensively,
    # mirroring HR — accommodates the layout drift where the label's
    # 'for' attribute doesn't always point at the right id.
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

    # ─── Phase 8: IFMS bank selection (Punjab-specific) ────────────────
    # Sequence:
    #   1. Wait for select#Bank to mount (the IFMS intermediate page).
    #   2. Select SBI BANK by value="1001509". PB's IFMS lists three banks
    #      (PNB Aggregate / SBI BANK / UNION BANK OF INDIA); we always
    #      pick SBI so the downstream SBIePay flow is identical to UP/HR.
    #   3. Click the Continue submit input.
    #   4. Page redirects to SBIePay Lite (merchant.onlinesbi.sbi or
    #      merchant.sbi.bank.in — we match on "sbi" in the URL).
    #
    # Unlike HR's eGRAS intermediate, PB's IFMS bank-selection page has
    # NO SweetAlert popups and NO native browser dialogs. Just select +
    # click.
    await wait_for_selector(
        session,
        SEL_BANK_DROPDOWN,
        log=log,
        name="phase8.wait_ifms_bank_page",
        timeout=IFMS_BANK_PAGE_TIMEOUT,
    )

    await select_by_value(
        session,
        SEL_BANK_DROPDOWN,
        BANK_VALUE_SBI,
        log=log,
        name="phase8.select_sbi_bank",
    )

    await click(
        session,
        SEL_BANK_CONTINUE,
        log=log,
        name="phase8.click_continue",
    )

    # Wait for redirect to SBIePay Lite. PB observed
    # "merchant.onlinesbi.sbi"; HR observed "merchant.sbi.bank.in".
    # Both contain "sbi" in the host -- match on that.
    await wait_for_url(
        session,
        "sbi",
        log=log,
        name="phase8.wait_sbiepay_redirect",
        timeout=SBIEPAY_REDIRECT_TIMEOUT,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase8.settle")

    # ─── Phase 9: SBIePay Lite UPI selection ───────────────────────────
    # Same UI surface as UP/HR. Click the UPI link in "Other Payment Modes".
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
    # Cyber Treasury / state payment details page → yellow CONFIRM.
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
        config=_PB_PAYMENT_CONFIG,
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
    """15-May-2026 -> 2026-05-15. Whitespace and trailing comma tolerated."""
    m = re.match(r"(\d{1,2})-(\w{3})-(\d{4})", s.strip(), re.IGNORECASE)
    if not m:
        return None
    day, mon_abbr, year = m.group(1).zfill(2), m.group(2).upper(), m.group(3)
    mon_num = _MONTH_TO_NUM.get(mon_abbr)
    if not mon_num:
        return None
    return f"{year}-{mon_num}-{day}"


async def _extract_receipt_fields(session, vehicle_number: str) -> dict | None:
    """Scrape receiptNumber, amount, paymentDate from the receipt page text.

    Identical regex set to UP/HR. No PB-specific receipt-number prefix
    constraint -- we accept any alphanumeric receipt number the page
    surfaces. If/when we have a confirmed PB receipt sample, we can
    tighten this to a "PBR" / "PBT" / whatever prefix.
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

    date_match = re.search(
        r"Payment\s*Confirmation\s*Date\s*:?\s*(\d{1,2}-\w{3}-\d{4})",
        text,
        re.IGNORECASE,
    )
    payment_date = _normalize_receipt_date(date_match.group(1)) if date_match else None

    if not (receipt_number and amount is not None and payment_date):
        print(
            f"[pb] receipt parse incomplete: "
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

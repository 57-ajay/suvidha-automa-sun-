# worker/src/scripted/border_tax/mp.py
"""Madhya Pradesh border-tax scripted runner.

Walks the 11 phases mapped against api/src/tasks/borderTax/states/mp.ts:

    Phase 1   - parivahan.gov.in/en/node/579, select MP
    Phase 2   - service selection, click Go
    Phase 3   - Owner Information: vehicle, Get Details, district,
                checkpost (param if it matches; else first non-empty option,
                mirrors PB/RJ).
    Phase 4   - Vehicle Information: Vehicle Category / Permit Type /
                Service Type — set only if EMPTY (RC data often pre-fills
                these). Validity-popup check before & after. NO Distance
                field on MP (like PB).
    Phase 5   - Tax Information: Tax Mode (DAYS ONLY — no MONTHLY or
                QUARTERLY on MP), Tax From, Tax Upto (plain type="date"
                YYYY-MM-DD — NOT datetime-local like HR/PB). Calculate, Next.
    Phase 6   - Disclaimer (AI handoff): CAPTCHA + "Receipt valid for X Days"
                popup + Pay Online + "Are you sure?" → reach payment gateway.
    Phase 7   - Payment Gateway: select SBIe (Direct Payment(SBIePay)),
                accept terms, click submit (input#sendSubmit — same id as
                HR/PB, but only one aggregator option exists for MP).
    Phase 8   - SBIePay (epay.sbi.bank.in — NOT SBIePay Lite):
                  • Wait for the left-sidebar UPI tab (li#activeUPI).
                  • Click the UPI tab's inner <a>.
                  • The right panel updates to show "Please select UPI
                    payment option" with a "UPI QR" radio.
                  • Click input#upiQR1 (the UPI QR radio).
                  • A yellow "Pay Now" button (button#upiButton) appears.
                  • Click "Pay Now" → navigates to the QR page.
                  • NO Cyber Treasury confirm page (unlike UP/HR/PB).
    Phase 9   - QR page: save_qr_code + wait_for_human.
                MP QR has a ~3 minute timer (similar to RJ), so we use a
                shorter HUMAN_PAYMENT_TIMEOUT than UP/HR/PB (200s vs 600s).
                Page marker: div#countDownTimer (QR <img> has NO id).
    Phase 10  - Receipt: poll for receipt page, extract fields (generic —
                no confirmed MP receipt sample yet), save_receipt PDF.

UPI-only. Net banking falls back to the AI agent path (run_job.py decides).

DIFFERENCES vs PB
=================
1. Phase 5 Tax Mode: MP supports DAYS ONLY (PB has DAYS + QUARTERLY).
   If a job ships with taxMode != "DAYS", select_by_text fails at
   option-not-found and the run aborts. The API should always send
   taxMode="DAYS" for MP.
2. Phase 5 date inputs: MP uses plain type="date" (YYYY-MM-DD), like UP.
   PB and HR use type="datetime-local" (YYYY-MM-DDTHH:MM). We DO NOT
   append T00:00 on MP — the input rejects datetime-local strings.
3. Phase 7: aggregator dropdown has a single option, value="SBIe"
   ("Direct Payment(SBIePay)"). Compare HR's EGRAS-SBIA / PB's IFMS /
   UP's SBI. Same select#dropOperator selector, same submit input#sendSubmit.
4. Phase 8: NO intermediate page between gateway submit and SBIePay
   (unlike HR's eGRAS or PB's IFMS bank-select). Redirect goes straight
   from eTransPgi to epay.sbi.bank.in.
5. Phase 8 SBIePay variant: MP uses a DIFFERENT SBIePay product than
   UP/HR/PB. Where UP/HR/PB get "SBIePay Lite" with a Cyber Treasury
   confirm page (yellow CONFIRM input#Go), MP gets a sidebar-based
   "SBIePay" at epay.sbi.bank.in with:
     - left sidebar of payment categories
     - UPI tab: li#activeUPI > a (NOT a[aria-label='UPI'])
     - UPI QR radio: input#upiQR1
     - Pay Now button: button#upiButton (NOT a yellow CONFIRM input)
   There is NO yellow CONFIRM page between UPI selection and the QR.
6. Phase 9 QR page: NOT the SBIePay Lite Remittance Information page.
   The QR <img> has NO id ("qrcodeImg" does NOT exist on MP). We
   detect the page by div#countDownTimer (the minute counter) instead.
   The QR <img> uses an inline data:image/png;base64 src.
7. Phase 9 timeout: MP QR has ~3:40 expiry (similar to RJ). We use 200s
   HUMAN_PAYMENT_TIMEOUT (UP/HR/PB use 600s).
8. Phase 10 receipt: generic regex (no MPR/MPT prefix lock — no confirmed
   MP receipt sample yet). The marker "GOVERNMENT OF MADHYA PRADESH" +
   "CHECKPOST TAX E-RECEIPT" + "RECEIPT NO" + "GRAND TOTAL" gates the
   extraction the same way HR/UP/PB do.

NOTE on save_qr_code compatibility
==================================
save_qr_code (in actions.py) was originally written for SBIePay Lite's
img#qrcodeImg. MP's QR has NO id. If save_qr_code fails on MP, the QR
image can be found via the more generic selector:
  img[src^="data:image/png;base64"]
located inside div.row.justify-content-center.align-items-center.
We log the failure but do NOT abort — the human can still pay via the
visible QR even if our capture fails.

Selectors are centralized at the top so first-test-run fixups stay
find-one-fix-one.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

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
from ._pending_clear import (
    auto_clear_enabled,
    clear_pending_transaction,
    navigate_to_owner_info_page,
    wait_for_owner_info_outcome,
)


# ─── Selectors ─────────────────────────────────────────────────────────

# Phase 1 — parivahan landing (shared with UP/HR/PB)
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"

# Phase 2 — service selection (shared)
SEL_SERVICE_DROPDOWN = "select[name='serviceName']"

# Phase 3 — owner info (shared)
SEL_VEHICLE_INPUT = "input#floatingvehicle"
SEL_DISTRICT = "select#floatingDistrict"
SEL_CHECKPOINT = "select#floatingCheckpost"

# Phase 4 — vehicle info (shared — typo "floatingPrmit" carried over)
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"
SEL_PERMIT_TYPE = "select#floatingPrmit"
SEL_SERVICE_TYPE = "select#floatingService"
# Server-populated (disabled in DOM) — the canary signal for "RC AJAX has
# landed". Until this has a non-empty .value, dependent dropdowns can't
# be filled correctly.
SEL_VEHICLE_CLASS = "select#floatingVehicletype"
# (No SEL_DISTANCE on MP — like PB.)

# Phase 5 — tax info
# MP's id is lowercase 'floatingtaxmode' (matches HR/PB). Keep candidate
# list in case Angular re-renders shuffle IDs between portal versions.
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select[name='taxMode']",
    "select[name='taxmode']",
]
SEL_TAX_FROM = "input#floatingTaxfrom"
# Yes, "uptpDate" — same typo as UP/HR/PB.
SEL_TAX_UPTO = "input#uptpDate"

# Phase 6 — disclaimer / captcha (AI-owned, kept for reference)
SEL_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_CAPTCHA_INPUT = "input#inputcap"
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"

# Phase 7 — payment gateway (shared selectors, MP-specific value)
SEL_PG_DROPDOWN = "select#dropOperator"
SEL_PG_TERMS_CHECKBOX = "input#checkme"
SEL_PG_SUBMIT = "input#sendSubmit"
PG_VALUE_MP = "SBIe"  # MP's only aggregator option (Direct Payment(SBIePay))

# Phase 8 — SBIePay (epay.sbi.bank.in — NOT SBIePay Lite!)
# DIFFERENT FROM UP/HR/PB. The UPI link selector "a[aria-label='UPI']"
# does NOT exist on this variant. We click the sidebar list-item's
# inner anchor.
SEL_SBIEPAY_UPI_TAB = "li#activeUPI a"
SEL_SBIEPAY_UPI_QR_RADIO = "input#upiQR1"
SEL_SBIEPAY_PAY_NOW = "button#upiButton"

# Phase 9 — QR page (different from SBIePay Lite — NO img#qrcodeImg)
# The QR <img> has no id; we detect the page via the countdown timer div.
SEL_QR_TIMER = "div#countDownTimer"
# Fallback selector if save_qr_code needs to find the image manually:
SEL_QR_IMG_FALLBACK = "img[src^='data:image/png;base64']"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5
# MP QR has ~3:40 expiry (similar to RJ). Shorter than UP/HR/PB's 600s.
PERMIT_SET_TIMEOUT_SECS = 15
SBIEPAY_REDIRECT_TIMEOUT = 60  # gateway submit -> epay.sbi.bank.in
SBIEPAY_UPI_PANEL_TIMEOUT = 20  # click UPI tab -> UPI QR radio mounts
SBIEPAY_PAY_NOW_TIMEOUT = 15  # click UPI QR radio -> Pay Now button enabled
QR_PAGE_NAV_TIMEOUT = 45  # click Pay Now -> QR page mounts
CHECKPOINT_POPULATE_TIMEOUT = 10  # district -> checkpost options populated
RC_DATA_TIMEOUT_SECS = 30


_MP_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Madhya Pradesh",
    qr_selector=SEL_QR_TIMER,  # div#countDownTimer — disappears w/ QR
    receipt_markers=[
        "GOVERNMENT OF MADHYA PRADESH",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*madhya\s*pradesh",
        r"checkpost\s*tax\s*e-?receipt",
    ],
    negative_markers_regex=[
        r"transaction\s*status\s*[:\-]?\s*pending",
        r"your\s*transaction\s*status\s*is\s*pending",
        r"transaction\s*confirmation\s*pending",
        r"transaction\s*status\s*[:\-]?\s*failed",
        r"transaction\s*failed",
        r"payment\s*failed",
        r"payment\s*timeout",
        r"session\s*(?:has\s*)?expired",
        r"qr\s*(?:code\s*)?expired",
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

    Mirrors PB's helper of the same name — MP also has compact district-
    specific checkpost names (e.g. SHEOPUR -> KARAHAL/NAHAR/SAMRASA) that
    don't necessarily match a sensible param.
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
    """Run MP scripted border-tax. Always returns a RunOutcome."""

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
        "MP",
        log=log,
        name="phase1.select_state_mp",
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
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase2.settle")

    # ─── Phase 3: owner information ────────────────────────────────────
    await wait_for_selector(
        session,
        SEL_VEHICLE_INPUT,
        log=log,
        name="phase3.wait_vehicle_input",
        timeout=30,
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

    outcome = await wait_for_owner_info_outcome(
        session, log, name="phase3.wait_owner_outcome"
    )

    if outcome == "pending_popup":
        if not auto_clear_enabled():
            return RunOutcome(
                status="failed",
                summary=(
                    f"Vehicle {params.vehicleNumber} has a pending transaction "
                    f"at the parivahan portal. Set "
                    f"SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING=true to attempt "
                    f"auto-clear."
                ),
                abort_reason="pending_transaction_popup",
                run_log=log.dump(),
            )

        print(f"[mp.phase3] pending-tx popup detected; attempting auto-clear")
        clear_status, hint = await clear_pending_transaction(
            session,
            vehicle_number=params.vehicleNumber,
            job_id=job_id,
            r=r,
            source=params.source,
            log=log,
        )

        if clear_status == "still_on_hold":
            return RunOutcome(
                status="failed",
                summary=(
                    f"Vehicle {params.vehicleNumber} has a pending transaction "
                    f"at the parivahan portal that cannot be cleared yet. "
                    f"Please retry in ~{hint}."
                ),
                abort_reason="pending_transaction_still_on_hold",
                run_log=log.dump(),
            )
        if clear_status == "failed":
            return RunOutcome(
                status="failed",
                summary=(
                    f"Auto-clear of pending transaction for vehicle "
                    f"{params.vehicleNumber} did not succeed. Reason: {hint}."
                ),
                abort_reason="pending_clear_failed",
                run_log=log.dump(),
            )

        print(f"[mp.phase3] pending tx cleared; restarting phases 1+2")
        # MP's first-run phase 2 doesn't wait_for_url('taxCollectionOnline')
        # after Go (mp.py L:phase2.click_go → straight to phase2.settle).
        # We preserve that by passing wait_for_tax_collection_url=False so
        # we don't change MP's working behavior.
        await navigate_to_owner_info_page(
            session,
            state_code="MP",
            log=log,
            wait_for_tax_collection_url=False,
        )
        await fill(
            session,
            SEL_VEHICLE_INPUT,
            params.vehicleNumber,
            log=log,
            name="phase3.fill_vehicle_retry",
        )
        await click_by_text(
            session,
            "Get Details",
            log=log,
            name="phase3.click_get_details_retry",
            tag="button",
        )
        outcome = await wait_for_owner_info_outcome(
            session, log, name="phase3.wait_owner_outcome_retry"
        )
        if outcome != "district_ready":
            return RunOutcome(
                status="failed",
                summary=(
                    f"After clearing pending transaction for vehicle "
                    f"{params.vehicleNumber}, phase-3 retry yielded "
                    f"outcome={outcome!r} (expected 'district_ready')."
                ),
                abort_reason=f"after_pending_clear:{outcome}",
                run_log=log.dump(),
            )

    elif outcome == "validity_popup":
        return RunOutcome(
            status="failed",
            summary=(
                f"Vehicle {params.vehicleNumber} has no valid insurance/"
                f"fitness/PUCC. Please renew before attempting border tax "
                f"payment."
            ),
            abort_reason="vehicle_validity_expired_phase3",
            run_log=log.dump(),
        )

    elif outcome == "timeout":
        return RunOutcome(
            status="failed",
            summary=(
                f"Owner-info page did not respond within 30s after Get "
                f"Details click for vehicle {params.vehicleNumber}."
            ),
            abort_reason="get_details_timeout",
            run_log=log.dump(),
        )

    # outcome == "district_ready" — continue with the existing flow.

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

    # MP-specific (mirrors PB): try params.entryCheckpoint first; if no
    # match, pick the first non-empty option.
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

    await sleep_seconds(3, log=log, name="phase3.settle")
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
    # read current value first, fill only when empty. NO Distance input
    # on MP (matches PB; differs from HR).

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

    # Wait for Permit Type to appear — earliest interactive control on
    # this step.
    await wait_for_selector(
        session,
        SEL_PERMIT_TYPE,
        log=log,
        name="phase4.wait_vehicle_info_page",
        timeout=30,
    )

    rc_data_loaded = False
    rc_started = time.monotonic()
    rc_deadline = rc_started + RC_DATA_TIMEOUT_SECS
    veh_class_val = ""
    while time.monotonic() < rc_deadline:
        veh_class_val = (
            await _cdp_eval(
                session,
                "(function(){var e=document.querySelector("
                "'select#floatingVehicletype');return e?(e.value||''):'';})()",
            )
            or ""
        )
        if veh_class_val and veh_class_val not in ("0", "-1"):
            rc_data_loaded = True
            break
        await asyncio.sleep(0.5)

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase4.wait_rc_data_loaded",
            status=StepStatus.OK if rc_data_loaded else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - rc_started) * 1000),
            value=f"vehicleClass={veh_class_val!r}",
        )
    )

    if not rc_data_loaded:
        return RunOutcome(
            status="failed",
            summary=(
                f"Vehicle {params.vehicleNumber} RC data did not load on "
                f"Phase 4 within {RC_DATA_TIMEOUT_SECS}s (Vehicle Class "
                f"field stayed empty). Usually means parivahan's central "
                f"RC service was slow or returned no data for this "
                f"vehicle. Retry the job — this is typically transient."
            ),
            abort_reason="rc_data_not_loaded",
            run_log=log.dump(),
        )

    # Re-check for validity popup that may have surfaced together with
    # the RC data (rare, but parivahan does this when fitness/insurance/
    # PUCC has expired AND the data lookup succeeded).
    await abort_if_popup_text(
        session,
        _validity_keywords,
        _validity_abort,
        log=log,
        name="phase4.check_validity_after_rc_load",
        close_selector=(
            "button.swal2-confirm, .modal-footer button, .swal-button--confirm"
        ),
    )

    # 4a. Vehicle Category — usually pre-filled from RC ("LIGHT PASSENGER
    # VEHICLE"; MP's dropdown literally only has this one real option).
    # If empty, log and continue; Next will surface a real error if it
    # actually matters.
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
                    "no param). Proceeding — Next may fail if MP requires it."
                ),
            )
        )

    # 4b. Permit Type — fill if empty.
    # NOTE on MP defaults: per user spec, MP defaults to TEMPORARY PERMIT
    # (not ALL INDIA TOURIST PERMIT like UP/HR). The fallback chain still
    # honors params.permitType first.
    # NOTE on portal typo: MP's dropdown lists "SEPECIAL PERMIT" (sic).
    # We never select that — fallback chain stops at TEMPORARY PERMIT.
    permit_val = await get_select_value(session, SEL_PERMIT_TYPE)
    if not permit_val:
        permit_candidates = [
            params.permitType,
            params.permitTypeFallback,
            "TEMPORARY PERMIT",  # MP-preferred fallback (different from UP/HR's NOT APPLICABLE)
        ]
        # de-dupe while preserving order
        seen: set[str] = set()
        permit_candidates = [
            p for p in permit_candidates if p and not (p in seen or seen.add(p))
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
                    f"Could not set Permit Type on MP vehicle-info page. "
                    f"Tried {permit_candidates}. "
                    f"Last error: "
                    f"{type(last_err).__name__ if last_err else 'n/a'}: "
                    f"{last_err if last_err else 'n/a'}"
                ),
                abort_reason="permit_type_not_settable",
                run_log=log.dump(),
            )
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

    # NO Distance input on MP. Skip straight to Next.

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
    # MP dropdown ONLY has DAYS. If params.taxMode is anything else,
    # this fails at option_not_found and aborts. That's the right
    # behavior — the API should always send taxMode="DAYS" for MP.
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
                f"Could not set Tax Mode {params.taxMode!r} on MP tax-info "
                f"page (MP only supports DAYS). Last attempt error: "
                f"{type(last_err).__name__ if last_err else 'n/a'}: "
                f"{last_err if last_err else 'n/a'}"
            ),
            abort_reason="selector_not_found:tax_mode",
            run_log=log.dump(),
        )

    # 5b/5c. Tax From / Tax Upto — PLAIN type="date" inputs (NOT
    # datetime-local like HR/PB). The DOM .value the input ACCEPTS is
    # the plain ISO "YYYY-MM-DD". Same as UP.
    # The field has min="<today>"; past dates are silently rejected.
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

    # Verify both fields actually accepted the value. If either is empty,
    # the form's "min" attribute likely rejected the date as out-of-range.
    tf_actual = (
        await _cdp_eval(
            session,
            "(function(){var e=document.querySelector("
            "'input#floatingTaxfrom');return e?e.value:'';})()",
        )
        or ""
    )
    tu_actual = (
        await _cdp_eval(
            session,
            "(function(){var e=document.querySelector("
            "'input#uptpDate');return e?e.value:'';})()",
        )
        or ""
    )

    if not tf_actual or tf_actual != params.taxFrom:
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax From date {params.taxFrom} did not stick in the date "
                f"input (got {tf_actual!r}). Most likely the date is before "
                f"the field's min attribute (MP rejects past dates)."
            ),
            abort_reason="tax_from_before_min",
            run_log=log.dump(),
        )
    if not tu_actual or tu_actual != params.taxUpto:
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax Upto date {params.taxUpto} did not stick in the date "
                f"input (got {tu_actual!r}). Most likely the date is before "
                f"the field's min attribute (often forced to >= Tax From)."
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
            config=_MP_PAYMENT_CONFIG,
            extract_receipt_fields=_extract_receipt_fields,
        )

    # ─── Phase 6: disclaimer (AI handoff) ──────────────────────────────
    # Same surface as UP/HR/PB — captcha, checkbox, "Receipt valid for X
    # Days" popup, Pay Online, "Are you sure?" popup → reach payment
    # gateway. AI exits when URL contains etranspgi/vahanpgi/paymentgateway.
    phase6_goal = (
        "You are on (or seconds away from) the MP border tax Disclaimer page "
        "(Step 4 of 4) for MADHYA PRADESH. Required actions, in order:\n"
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
                "MP border-tax disclaimer page. Vehicle/tax summary displayed. "
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
    # Same selectors as UP/HR/PB, but MP's aggregator dropdown has a
    # SINGLE option, value="SBIe" (Direct Payment(SBIePay)).
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
        PG_VALUE_MP,
        log=log,
        name="phase7.select_sbie",
    )

    # Tick the "I accept terms and conditions" checkbox. Same defensive
    # behavior as HR/PB: tick the first unchecked checkbox in case the
    # label's `for` attribute points at a different id than expected.
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

    # ─── Phase 8: SBIePay (epay.sbi.bank.in — sidebar variant) ─────────
    #
    # Unlike UP/HR/PB which all redirect to "SBIePay Lite" (merchant.sbi.*
    # with the a[aria-label='UPI'] link), MP redirects to a different
    # SBIePay product at epay.sbi.bank.in with a sidebar-based UI:
    #
    #   1. Wait for li#activeUPI (sidebar UPI tab) to mount.
    #   2. Click its inner <a> to switch the right panel to UPI.
    #   3. Wait for input#upiQR1 (UPI QR radio) and click it.
    #   4. Wait for button#upiButton (Pay Now) to appear, then click it.
    #   5. Page navigates to the QR display page.
    #
    # NO Cyber Treasury confirm. NO yellow CONFIRM button. Just sidebar
    # → radio → Pay Now → QR.
    await wait_for_url(
        session,
        "sbi.bank.in",
        log=log,
        name="phase8.wait_sbiepay_redirect",
        timeout=SBIEPAY_REDIRECT_TIMEOUT,
    )

    await wait_for_selector(
        session,
        SEL_SBIEPAY_UPI_TAB,
        log=log,
        name="phase8.wait_upi_tab",
        timeout=SBIEPAY_UPI_PANEL_TIMEOUT,
    )

    await click(
        session,
        SEL_SBIEPAY_UPI_TAB,
        log=log,
        name="phase8.click_upi_tab",
    )

    # wait for page to load fully so we can properly click on `qr_code` option
    await sleep_seconds(15, log=log, name="phase8.settle_after_upi_tab")

    # The right panel should now show "Please select UPI payment option".
    await wait_for_selector(
        session,
        SEL_SBIEPAY_UPI_QR_RADIO,
        log=log,
        name="phase8.wait_upi_qr_radio",
        timeout=SBIEPAY_UPI_PANEL_TIMEOUT,
    )

    await click(
        session,
        SEL_SBIEPAY_UPI_QR_RADIO,
        log=log,
        name="phase8.click_upi_qr_radio",
    )

    # Clicking the radio reveals the yellow "Pay Now" button. Wait for it.
    await wait_for_selector(
        session,
        SEL_SBIEPAY_PAY_NOW,
        log=log,
        name="phase8.wait_pay_now",
        timeout=SBIEPAY_PAY_NOW_TIMEOUT,
    )

    await click(
        session,
        SEL_SBIEPAY_PAY_NOW,
        log=log,
        name="phase8.click_pay_now",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase8.settle")

    # ─── Phase 9: QR page + human pays via UPI ─────────────────────────
    #
    # The QR page has heading "Scan UPI QR" and contains an <img> with
    # an inline base64 src. The <img> itself has NO id — we detect the
    # page by div#countDownTimer (the minute counter).
    await wait_for_selector(
        session,
        SEL_QR_TIMER,
        log=log,
        name="phase9.wait_qr_page",
        timeout=QR_PAGE_NAV_TIMEOUT,
    )

    # Give the QR <img> a beat to fully render before save_qr_code reads it.
    await asyncio.sleep(1.0)

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
        config=_MP_PAYMENT_CONFIG,
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

    Identical regex set to UP/HR/PB. No MP-specific receipt-number prefix
    constraint -- we accept any alphanumeric receipt number the page
    surfaces. If/when we have a confirmed MP receipt sample, we can
    tighten this to a "MPR" / "MPT" / whatever prefix.
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
            f"[mp] receipt parse incomplete: "
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

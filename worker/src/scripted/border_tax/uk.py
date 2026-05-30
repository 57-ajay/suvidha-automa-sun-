# worker/src/scripted/border_tax/uk.py
"""Uttarakhand border-tax scripted runner.

Walks the phases mapped against api/src/tasks/borderTax/states/uk.ts:

    Phase 1  - parivahan.gov.in/en/node/579, select UTTARAKHAND (value "UK")
    Phase 2  - service selection ("VEHICLE TAX COLLECTION (OTHER STATE)"), Go
    Phase 3  - Owner Information: vehicle, Get Details, Entry District,
               Entry Checkpost (first option), Next
    Phase 4  - Vehicle Information: Vehicle Category (first if empty) +
               Permit Type (first if empty) + Service Type (force AC), Next
    Phase 5  - Tax Information: Tax Mode (DAYS), Tax From, Tax Upto,
               Calculate Fee/Tax, Next
    Phase 6  - Disclaimer (AI handoff): CAPTCHA + confirm checkbox + popups
               + Pay Online + "Are you sure?" -> reach payment gateway
    Phase 7  - Payment Gateway: select SBI (Multi Bank Payment), accept
               terms, Submit -> SBI multi-bank payment page
    Phase 8  - Human handover + background receipt capture. UK has NO UPI
               option (net banking only), so there is no scripted-completable
               payment step. We set status=waiting_for_human, start a 15-min
               timeout, and poll the page for the receipt every 3 seconds in
               the background via web_handover_and_capture(poll_qr=False).
               The moment the human completes payment and the receipt page
               renders we capture it and finish automatically -- we never
               block on an explicit "done" from the human, since they may pay
               and never notify us.

NET-BANKING-ONLY NOTE
=====================
Unlike UP/HR/PB/MP (UPI-capable, where the scripted path drives the QR flow
end-to-end), UK's portal only offers "SBI (Multi Bank Payment)" net banking.
So this runner ALWAYS hands the payment itself to a human -- regardless of
source ("app" or "web") and regardless of params.paymentMethod. run_job.py
treats UK as scripted-eligible even though paymentMethod != "upi" (see
scripted.runner.state_is_net_banking_scripted).

Phase 4 NOTE: Some vehicles arrive with Vehicle Category / Permit Type
pre-filled from RC data; others arrive empty. Per the UK spec we leave those
two as-is when pre-filled and pick the first available option when empty.
Service Type is always forced to "Air Conditioned Service" (AC) per spec.

Selectors are centralized at the top so first-test-run fixups stay
find-one-fix-one. The parivahan Angular DOM (and its typos -- "floatingPrmit",
"uptpDate") is identical to UP/HR/PB/MP, so these match.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

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
from ._payment_wait import PaymentCaptureConfig
from ._web_handover import web_handover_and_capture
from ._pending_clear import (
    auto_clear_enabled,
    clear_pending_transaction,
    navigate_to_owner_info_page,
    wait_for_owner_info_outcome,
)


# ─── Selectors ─────────────────────────────────────────────────────────

# Phase 1 — parivahan landing (shared with UP/HR/PB/MP)
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

# Phase 5 — tax info
# UK matches UP exactly: plain type=date inputs, lowercase 'floatingtaxmode'
# tax-mode select, and the "uptpDate" typo on the Tax Upto field.
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select[name='taxMode']",
    "select[name='taxmode']",
]
SEL_TAX_FROM = "input#floatingTaxfrom"
SEL_TAX_UPTO = "input#uptpDate"

# Phase 6 — disclaimer / captcha (handled by AI)
SEL_CAPTCHA_INPUT = "input#inputcap"

# Phase 7 — payment gateway (shared selectors with UP)
SEL_PG_DROPDOWN = "select#dropOperator"
SEL_PG_SUBMIT = "input#sendSubmit"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5            # polite breath between phases
PERMIT_SET_TIMEOUT_SECS = 15    # per-attempt timeout when setting permit type
CHECKPOINT_POPULATE_TIMEOUT = 10
HANDOVER_TIMEOUT_SECS = 900     # 15 minutes (net-banking human payment window)
RECEIPT_POLL_SECS = 3.0         # poll the page for the receipt every 3s


# Receipt-page detection + payment outcome markers for the background poll.
# poll_qr=False is passed at the call site (UK has no UPI/QR), so qr_selector
# is unused but the dataclass field is required.
_UK_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Uttarakhand",
    qr_selector="img#qrcodeImg",  # unused (poll_qr=False)
    receipt_markers=[
        "GOVERNMENT OF UTTARAKHAND",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*uttarakhand",
        r"checkpost\s*tax\s*e-?receipt",
    ],
    negative_markers_regex=[
        r"transaction\s*status\s*[:\-]?\s*pending",
        r"your\s*transaction\s*status\s*is\s*pending",
        r"transaction\s*confirmation\s*pending",
        r"transaction\s*status\s*[:\-]?\s*failed",
        r"transaction\s*failed",
        r"payment\s*failed",
    ],
)


# ─── Small DOM helpers ─────────────────────────────────────────────────


async def _read_input_value(session, selector: str) -> str:
    """Return the current .value of an input, or '' if missing."""
    js = (
        "(function(s){var e=document.querySelector(s);"
        "return e?(e.value||''):'';})(%s)" % json.dumps(selector)
    )
    try:
        v = await _cdp_eval(session, js)
        return str(v or "")
    except Exception:
        return ""


async def _first_non_placeholder_option(
    session,
    selector: str,
    *,
    timeout: int,
) -> dict:
    """Poll until `selector` has at least one non-placeholder <option>
    (value not '' / '-1' / '0'). Returns {"ok": True, "value", "text"} or
    {"ok": False, "reason": ...} on timeout.

    Angular populates these dropdowns asynchronously after upstream
    selections (e.g. checkpost after district, vehicle-info after RC fetch),
    so we have to wait rather than read once."""
    js = (
        "(function(s){"
        "  var sel=document.querySelector(s);"
        "  if(!sel) return {ok:false, reason:'no_select'};"
        "  for(var i=0;i<sel.options.length;i++){"
        "    var o=sel.options[i];"
        "    var v=(o.value||'').trim();"
        "    if(v && v!=='-1' && v!=='0'){"
        "      return {ok:true, value:v, text:(o.textContent||'').trim()};"
        "    }"
        "  }"
        "  return {ok:false, reason:'not_populated'};"
        "})(%s)" % json.dumps(selector)
    )
    deadline = time.monotonic() + timeout
    last_reason = "not_populated"
    while True:
        res = await _cdp_eval(session, js)
        if res and res.get("ok"):
            return res
        if res:
            last_reason = res.get("reason") or last_reason
        if time.monotonic() > deadline:
            return {"ok": False, "reason": last_reason}
        await asyncio.sleep(0.5)


async def _set_date(session, selector: str, iso: str, *, log: StepLogger, name: str) -> bool:
    """Set a type=date input to an ISO (YYYY-MM-DD) value. Tries the normal
    fill() helper first; if the value doesn't stick (date inputs can be
    finicky), falls back to a CDP value-set + input/change dispatch.

    Returns True iff the field ends up holding exactly `iso`."""
    await fill(session, selector, iso, log=log, name=name)
    if (await _read_input_value(session, selector)) == iso:
        return True

    # CDP fallback — set value directly and fire the events Angular listens for.
    await _cdp_eval(
        session,
        "(function(s,v){var e=document.querySelector(s);if(!e)return false;"
        "e.value=v;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));"
        "return e.value;})(%s,%s)" % (json.dumps(selector), json.dumps(iso)),
    )
    return (await _read_input_value(session, selector)) == iso


async def _select_first_tax_mode(session, *, log: StepLogger) -> bool:
    """Select the (only) tax mode — 'DAYS' — on whichever tax-mode select id
    the portal rendered. Returns True on success."""
    for sel in SEL_TAX_MODE_CANDIDATES:
        present = await _cdp_eval(
            session,
            "(function(s){return !!document.querySelector(s);})(%s)" % json.dumps(sel),
        )
        if not present:
            continue
        # Prefer the DAYS option by text; fall back to first non-placeholder value.
        try:
            await select_by_text(
                session, sel, "DAYS", log=log, name="phase5.select_tax_mode_days"
            )
            return True
        except Exception:
            opt = await _first_non_placeholder_option(session, sel, timeout=5)
            if opt.get("ok"):
                await select_by_value(
                    session, sel, opt["value"], log=log,
                    name="phase5.select_tax_mode_first",
                )
                return True
    return False


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run UK scripted border-tax. Always returns a RunOutcome."""

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
        "UK",
        log=log,
        name="phase1.select_state_uk",
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

    # Owner-info outcome routing. `wait_for_owner_info_outcome` returns ONE
    # of: "district_ready" (happy path — district select is up), "pending_popup"
    # (stale pending tx from a prior attempt), "validity_popup" (insurance /
    # fitness / PUCC expired), or "timeout" (nothing decisive in 30s). The
    # branch structure mirrors UP/HR/PB/MP — keep it byte-for-byte aligned.
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

        print(f"[uk.phase3] pending-tx popup detected; attempting auto-clear")
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

        # clear_status == "cleared": restart phases 1+2 via the navigation
        # helper, re-fill the vehicle, re-click Get Details, re-evaluate.
        # ONE retry only — if the second pass still doesn't yield
        # district_ready we bail.
        print(f"[uk.phase3] pending tx cleared; restarting phases 1+2")
        await navigate_to_owner_info_page(
            session,
            state_code="UK",
            log=log,
            wait_for_tax_collection_url=True,
        )
        await fill(
            session, SEL_VEHICLE_INPUT, params.vehicleNumber, log=log,
            name="phase3.fill_vehicle_retry",
        )
        await click_by_text(
            session, "Get Details", log=log,
            name="phase3.click_get_details_retry", tag="button",
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
                    f"outcome={outcome!r} (expected 'district_ready'). "
                    f"Cannot continue."
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
                f"Details click for vehicle {params.vehicleNumber}. The "
                f"portal may be slow or the central RC fetch failed silently."
            ),
            abort_reason="get_details_timeout",
            run_log=log.dump(),
        )

    # outcome == "district_ready" — fall through to district + checkpost.

    # 3a. Entry District — try the configured/default district by text,
    #     fall back to the first available option for robustness.
    district_set = False
    if params.entryDistrict:
        try:
            await select_by_text(
                session, SEL_DISTRICT, params.entryDistrict,
                log=log, name="phase3.select_district",
            )
            district_set = True
        except Exception:
            district_set = False
    if not district_set:
        opt = await _first_non_placeholder_option(
            session, SEL_DISTRICT, timeout=CHECKPOINT_POPULATE_TIMEOUT
        )
        if not opt.get("ok"):
            return RunOutcome(
                status="failed",
                summary="Entry District dropdown had no selectable options.",
                abort_reason="district_not_populated",
                run_log=log.dump(),
            )
        await select_by_value(
            session, SEL_DISTRICT, opt["value"],
            log=log, name="phase3.select_district_first",
        )

    # Checkpost options populate only after a district is chosen.
    await asyncio.sleep(1.0)

    # 3b. Entry Checkpost — UK lists several (ASHARODI / KULHAL / TIMLI /
    #     TUNI); per spec we pick the FIRST available option.
    cp = await _first_non_placeholder_option(
        session, SEL_CHECKPOINT, timeout=CHECKPOINT_POPULATE_TIMEOUT
    )
    if not cp.get("ok"):
        return RunOutcome(
            status="failed",
            summary="Entry Checkpost dropdown had no selectable options.",
            abort_reason="checkpost_not_populated",
            run_log=log.dump(),
        )
    await select_by_value(
        session, SEL_CHECKPOINT, cp["value"],
        log=log, name="phase3.select_checkpost_first",
    )
    log.record(StepLog(
        index=log.next_index(),
        name="phase3.checkpost_selected",
        status=StepStatus.OK,
        selector=SEL_CHECKPOINT,
        value=cp.get("text"),
    ))

    await click_by_text(
        session, "Next", log=log, name="phase3.click_next", tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase3.settle")

    # ─── Phase 4: vehicle information ──────────────────────────────────
    _validity_keywords = ["INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW"]
    _validity_abort = (
        f"Vehicle {params.vehicleNumber} has no valid insurance/fitness/"
        f"PUCC. Please renew before attempting border tax payment."
    )
    _close_sel = "button.swal2-confirm, .modal-footer button, .swal-button--confirm"

    await abort_if_popup_text(
        session, _validity_keywords, _validity_abort,
        log=log, name="phase4.check_validity_on_load", close_selector=_close_sel,
    )

    # Permit Type select is the earliest reliable "form ready" signal.
    await wait_for_selector(
        session, SEL_PERMIT_TYPE, log=log,
        name="phase4.wait_vehicle_info_page", timeout=30,
    )

    # 4a. Vehicle Category — leave if pre-filled, else pick the first option.
    try:
        veh_cat_val = await get_select_value(session, SEL_VEHICLE_CATEGORY)
    except Exception:
        veh_cat_val = None
    if not veh_cat_val:
        opt = await _first_non_placeholder_option(
            session, SEL_VEHICLE_CATEGORY, timeout=8
        )
        if opt.get("ok"):
            await select_by_value(
                session, SEL_VEHICLE_CATEGORY, opt["value"],
                log=log, name="phase4.select_vehicle_category_first",
            )
            await asyncio.sleep(1.0)
        else:
            log.record(StepLog(
                index=log.next_index(),
                name="phase4.vehicle_category_empty",
                status=StepStatus.RETRIED,
                value="<empty>",
                error="Vehicle Category empty and no options; proceeding.",
            ))

    # 4b. Permit Type — leave if pre-filled, else first option via the
    #     primary + fallback chain (mirrors UP/HR/PB/MP).
    permit_val = await get_select_value(session, SEL_PERMIT_TYPE)
    if not permit_val:
        permit_candidates = [
            params.permitType,
            params.permitTypeFallback,
            "TEMPORARY PERMIT",
        ]
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
                    session, SEL_PERMIT_TYPE, cand, log=log,
                    name=f"phase4.select_permit_type[{cand}]",
                    timeout=PERMIT_SET_TIMEOUT_SECS,
                )
                permit_set = True
                break
            except Exception as e:
                last_err = e
                continue
        if not permit_set:
            # Last resort: first available option (UK always lists at least
            # TEMPORARY PERMIT).
            opt = await _first_non_placeholder_option(
                session, SEL_PERMIT_TYPE, timeout=5
            )
            if opt.get("ok"):
                await select_by_value(
                    session, SEL_PERMIT_TYPE, opt["value"], log=log,
                    name="phase4.select_permit_type_first",
                )
                permit_set = True
        if not permit_set:
            return RunOutcome(
                status="failed",
                summary=(
                    f"Could not set Permit Type on UK vehicle-info page. "
                    f"Tried {permit_candidates}. Last error: "
                    f"{type(last_err).__name__ if last_err else 'n/a'}: "
                    f"{last_err if last_err else 'n/a'}"
                ),
                abort_reason="permit_type_not_settable",
                run_log=log.dump(),
            )
        # Let Angular react before Service Type's options are queried.
        await asyncio.sleep(1.5)
        await abort_if_popup_text(
            session, _validity_keywords, _validity_abort,
            log=log, name="phase4.check_validity_after_permit",
            close_selector=_close_sel,
        )

    # 4c. Service Type — UK spec: ALWAYS select the AC option as default.
    try:
        await select_by_text(
            session, SEL_SERVICE_TYPE, params.serviceType,  # "Air Conditioned Service"
            log=log, name="phase4.select_service_type_ac",
        )
    except Exception as e:
        # Fall back to first option so the form can still proceed.
        opt = await _first_non_placeholder_option(session, SEL_SERVICE_TYPE, timeout=5)
        if opt.get("ok"):
            await select_by_value(
                session, SEL_SERVICE_TYPE, opt["value"], log=log,
                name="phase4.select_service_type_first",
            )
        else:
            return RunOutcome(
                status="failed",
                summary=(
                    f"Could not set Service Type on UK vehicle-info page "
                    f"(wanted {params.serviceType!r}): {type(e).__name__}: {e}"
                ),
                abort_reason="service_type_not_settable",
                run_log=log.dump(),
            )
    await asyncio.sleep(1.0)
    await abort_if_popup_text(
        session, _validity_keywords, _validity_abort,
        log=log, name="phase4.check_validity_after_service",
        close_selector=_close_sel,
    )

    await click_by_text(
        session, "Next", log=log, name="phase4.click_next", tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase4.settle")

    # ─── Phase 5: tax information ──────────────────────────────────────
    await wait_for_selector(
        session, SEL_TAX_FROM, log=log,
        name="phase5.wait_tax_page", timeout=30,
    )

    # 5a. Tax Mode (DAYS).
    if not await _select_first_tax_mode(session, log=log):
        return RunOutcome(
            status="failed",
            summary="Tax Mode dropdown not found / no selectable option on UK.",
            abort_reason="tax_mode_not_settable",
            run_log=log.dump(),
        )
    await asyncio.sleep(0.5)

    # 5b. Tax From / Tax Upto (plain ISO date inputs, same as UP).
    if not await _set_date(session, SEL_TAX_FROM, params.taxFrom, log=log,
                           name="phase5.set_tax_from"):
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax From date {params.taxFrom} did not stick in the date "
                f"input (got {(await _read_input_value(session, SEL_TAX_FROM))!r}). "
                f"Most likely it is before the field's min (UK rejects past dates)."
            ),
            abort_reason="tax_from_before_min",
            run_log=log.dump(),
        )
    if not await _set_date(session, SEL_TAX_UPTO, params.taxUpto, log=log,
                           name="phase5.set_tax_upto"):
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax Upto date {params.taxUpto} did not stick in the date "
                f"input (got {(await _read_input_value(session, SEL_TAX_UPTO))!r}). "
                f"Most likely it is before the field's min (often >= Tax From)."
            ),
            abort_reason="tax_upto_before_min",
            run_log=log.dump(),
        )

    await sleep_seconds(1, log=log, name="phase5.wait_before_calc")
    await click_by_text(
        session, "Calculate Fee/Tax", log=log,
        name="phase5.click_calculate", tag="button",
    )
    await sleep_seconds(4, log=log, name="phase5.wait_calculation")

    await extract_and_save_border_tax_amount(
        session, log=log, name="phase5.extract_border_tax_amount",
    )

    await click_by_text(
        session, "Next", log=log, name="phase5.click_next", tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase5.settle")

    # ─── Web source: stop here and hand over to the human ──────────────
    # When source == "web" the operator is watching via VNC and completes the
    # rest themselves: the Disclaimer-page captcha + confirm checkbox, Pay
    # Online + "Are you sure?", the payment-gateway selection (SBI Multi Bank
    # Payment), and the net-banking payment. We do NOT run the Phase 6 AI
    # captcha handoff in this case — we just poll for the receipt in the
    # background. This mirrors UP/HR/PB/MP, which all branch to
    # web_handover_and_capture right after Phase 5. The only UK-specific
    # difference is poll_qr=False (UK has no UPI/QR).
    if params.source == "web":
        return await web_handover_and_capture(
            session, log, r, job_id, job_params,
            vehicle_number=params.vehicleNumber,
            config=_UK_PAYMENT_CONFIG,
            extract_receipt_fields=_extract_receipt_fields,
            poll_qr=False,
            timeout_secs=HANDOVER_TIMEOUT_SECS,
            receipt_poll_secs=RECEIPT_POLL_SECS,
        )

    # ─── Phase 6: disclaimer (AI handoff) — APP source only ────────────
    # Same surface as UP/HR/PB/MP — captcha, confirm checkbox, "Receipt valid
    # for X Days" popup, Pay Online, "Are you sure?" popup → payment gateway.
    # AI exits when the URL reaches the payment gateway.
    phase6_goal = (
        "You are on (or seconds away from) the UK border tax Disclaimer page "
        "(Step 4 of 4) for UTTARAKHAND. Required actions, in order:\n"
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
                "UK border-tax disclaimer page. Vehicle/tax summary displayed. "
                "Two popups expected: 'Receipt valid for X Days' (after checkbox) "
                "and 'Are you sure?' (after Pay Online)."
            ),
            max_steps=15,
        )
    except Exception as e:
        log.record(StepLog(
            index=log.next_index(),
            name="phase6.ai_handoff",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - phase6_started) * 1000),
            error=f"{type(e).__name__}: {e}",
            handoff_reason="phase6_ai_owned",
        ))
        raise ScriptedAbort(f"Phase 6 AI handoff crashed: {type(e).__name__}: {e}")

    # AI may call done a moment before navigation settles — poll the URL.
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

    log.record(StepLog(
        index=log.next_index(),
        name="phase6.ai_handoff",
        status=StepStatus.HANDED_OFF if on_gateway else StepStatus.FAILED,
        duration_ms=int((time.monotonic() - phase6_started) * 1000),
        url=final_url,
        handoff_reason="phase6_ai_owned",
        handoff_summary=rescue_summary,
        handoff_cost_usd=rescue_cost,
    ))

    if not on_gateway:
        raise ScriptedAbort(
            f"Phase 6 AI handoff did not reach payment gateway. "
            f"Final URL: {final_url or '<empty>'}. AI summary: {rescue_summary}"
        )

    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase6.settle_after_handoff")

    # ─── Phase 7: payment gateway (select SBI, accept terms, Submit) ───
    await wait_for_selector(
        session, SEL_PG_DROPDOWN, log=log,
        name="phase7.wait_payment_gateway", timeout=20,
    )
    # UK's only option is "SBI (Multi Bank Payment)" with value "SBI".
    await select_by_value(
        session, SEL_PG_DROPDOWN, "SBI", log=log, name="phase7.select_sbi",
    )
    # Tick the "I accept terms and conditions." checkbox (id may vary —
    # check the first unchecked checkbox, same approach as UP).
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
        session, SEL_PG_SUBMIT, log=log, name="phase7.click_submit",
    )
    await sleep_seconds(2, log=log, name="phase7.settle")

    # ─── Phase 8: human handover + background receipt capture ──────────
    # No UPI on UK — the human completes net banking on the SBI multi-bank
    # page. We hand over (status=waiting_for_human), start a 15-min timeout,
    # and poll for the receipt page every 3s in the background. The moment
    # the receipt renders we capture it and finish automatically; we never
    # wait for an explicit human "done", because the human may pay and never
    # notify us. poll_qr=False because there is no QR in this flow.
    return await web_handover_and_capture(
        session, log, r, job_id, job_params,
        vehicle_number=params.vehicleNumber,
        config=_UK_PAYMENT_CONFIG,
        extract_receipt_fields=_extract_receipt_fields,
        poll_qr=False,
        timeout_secs=HANDOVER_TIMEOUT_SECS,
    )


# ─── Receipt parsing helpers ───────────────────────────────────────────

_MONTH_TO_NUM = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
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
    """Scrape receiptNumber, amount, paymentDate from the receipt page text.

    Identical regex set to UP/HR/PB/MP. No UK-specific receipt-number prefix
    constraint yet — we accept any alphanumeric receipt number. Tighten to a
    confirmed prefix once we have a real UK receipt sample.
    """
    text = await _cdp_eval(session, "document.body.innerText || ''")
    if not text:
        return None

    receipt_match = re.search(
        r"Receipt\s*No\.?\s*:?\s*([A-Z0-9]+)", text, re.IGNORECASE,
    )
    receipt_number = receipt_match.group(1) if receipt_match else None

    amount_match = re.search(
        r"Grand\s*Total\s*:?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE,
    )
    amount = float(amount_match.group(1)) if amount_match else None

    # UK receipt may use "Payment Confirmation Date" or "Payment Initialization
    # Date" — accept either.
    date_match = re.search(
        r"Payment\s*(?:Confirmation|Initiali[sz]ation|Initiation)\s*Date\s*:?\s*"
        r"(\d{1,2}-\w{3}-\d{4})",
        text, re.IGNORECASE,
    )
    payment_date = _normalize_receipt_date(date_match.group(1)) if date_match else None

    if not (receipt_number and amount is not None and payment_date):
        print(
            f"[uk] receipt parse incomplete: "
            f"receipt_number={receipt_number} amount={amount} "
            f"payment_date={payment_date}"
        )
        return None

    return {
        "vehicleNumber": vehicle_number,
        "receiptNumber": receipt_number,
        "amount": amount,
        "paymentDate": payment_date,
    }

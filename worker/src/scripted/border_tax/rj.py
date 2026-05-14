# worker/src/scripted/border_tax/rj.py
"""Rajasthan border-tax scripted runner.

Walks the 9 phases mapped against api/src/tasks/borderTax/states/rj.ts:

    Phase 1  - parivahan.gov.in/en/node/579, select RAJASTHAN
    Phase 2  - Service Name select + Go (e-Vahan landing)
    Phase 3  - Mega-form: Vehicle, Get Details, Permit Type, District,
               Checkpost, Tax From/Upto, Calculate Tax. ONE big page,
               NO wizard / Next buttons between rows.
    Phase 4  - Pay Tax -> Confirmation modal -> Confirm
    Phase 5  - e-Vahan Payment Gateway: select E-GRAS, accept terms, Continue
    Phase 6  - eGRAS Rajasthan splash: click big CONTINUE (input#txtGo, image input)
    Phase 7  - eGRAS Payment Details: click UPI tab -> Proceed
    Phase 8  - QR page: click QR CODE radio (ASP.NET postback) -> save_qr_code
               -> wait_for_human (150s timeout, RJ QR expires in ~3 min)
    Phase 9  - Receipt: poll for receipt page, extract RJT fields, save_receipt PDF

UPI-only. Net banking falls back to the AI agent path (run_job.py decides).

DIFFERENCES vs UP/HR
====================
1. No CAPTCHA anywhere on RJ -- fully scripted, no run_ai_rescue calls.
2. Different portal stack: vahan.parivahan.gov.in/checkpost/ runs a
   PrimeFaces JSF form (NOT Angular like UP/HR). Payment pages are
   ASP.NET WebForms (ct/ctl00 prefixes).
3. Mega-form vs wizard -- Phase 3 fills everything top-to-bottom on one page.
4. Date inputs are plain text DD-MM-YYYY (not HTML5 date / datetime-local).
5. Receipt date label is "Payment Initiation Date" (not "Confirmation"),
   receipt-no prefix is "RJT...", Grand Total has a leading rupee symbol.
6. UPI QR page initially shows the UPI ID radio active; we MUST click the
   QR CODE radio to trigger an ASP.NET postback that renders the QR. The
   rj.ts AI prompt incorrectly claimed QR was pre-selected.

PRIMEFACES ID NOTE
==================
Several IDs on the mega-form are PrimeFaces-generated (j_idt*) and may
shift on portal redeploys:
  - Vehicle No input:     j_idt57
  - Owner Name input:     j_idt67  (readiness marker only)
  - District through Entering:  j_idt540_input
  - Check Post Name Through Entering:  j_idt551_input
  - Gateway dropdown on the e-Vahan PG page:  j_idt13:j_idt42_input
  - Terms checkbox on the PG page:  j_idt13:b_input
  - Continue button on the PG page:  j_idt13:bt_payment

For unstable SELECTS we use option-content matching: select_by_text /
select_by_value already iterate every <select> matching the CSS selector
and pick the first one whose options contain the target. Passing a broad
fallback ('select') survives ID shifts as long as no two selects on the
page share the same option text.

For unstable BUTTONS we use click_by_text -- text content is stable
across redeploys, IDs are not.

For unstable INPUTS we use attribute-based fallback selectors when the
primary ID query fails.

NOTE on the QR container ID
===========================
The user-provided HTML for the QR page shows the container is
'ctl00_ContentPlaceHolder1_divQRCode' (lowercase L, two zeros) -- standard
ASP.NET WebForms convention. An earlier version of actions.py used
'ct100_...' (digit 1) which was a typo. actions.py has been updated to
try the correct 'ctl00' first and fall back to 'ct100' for safety.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from actions import save_qr_code, save_receipt

from ..captcha import (
    _wait_for_human_via_redis as wait_for_human_via_redis,
)
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


# ─── Selectors ─────────────────────────────────────────────────────────

# Phase 1 — parivahan landing (same as UP/HR)
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"

# Phase 2 — e-Vahan service selection
# Underlying PrimeFaces select; option value "5003" = VEHICLE TAX COLLECTION (OTHER STATE)
SEL_SERVICE_DROPDOWN = "select#operation_code_input"

# Phase 3 — mega-form
# Vehicle No input: PrimeFaces auto-id (j_idt57). Fallback is
# attribute-based: the ONLY text input on this page combining
# maxlength=10 (vehicle plates are <= 10 chars) and onkeyup='makeCaps'.
SEL_VEHICLE_INPUT = "input#j_idt57"
SEL_VEHICLE_INPUT_FALLBACK = "input[type='text'][maxlength='10'][onkeyup*='makeCaps']"

# Owner Name input: same j_idt instability. Used as readiness marker
# after Get Details click. Fallback uses maxlength=50 (long enough for
# owner names) + onkeyup='makeCaps' as the distinguisher.
SEL_OWNER_NAME = "input#j_idt67"
SEL_OWNER_NAME_FALLBACK = "input[type='text'][maxlength='50'][onkeyup*='makeCaps']"

# Stable, name-based selectors
SEL_PERMIT_TYPE = "select#cmb_permit_type_input"
# AITP fields are read-only (disabled). We only read their values to log.
SEL_AITP_VALIDITY = "input#aitp_permit_upto_input"
SEL_AITP_AUTH_VALIDITY = "input#permit_auth_validity_input"
SEL_TAX_MODE = "select#cmb_payment_mode_input"  # disabled / auto-fills
SEL_NO_OF_PERIODS = "input#txt_no_of_weeks"  # disabled / auto-fills
SEL_TAX_FROM = "input#cal_tax_from_input"
SEL_TAX_UPTO = "input#cal_tax_to_input"
SEL_TOTAL_AMOUNT = "input#txt_tax_amount"  # disabled / populated by Calculate Tax

# Unstable j_idt selects — primary first, broad 'select' fallback. The
# select_by_text primitive scans every element matching the selector and
# picks the first <select> with a matching option.
SEL_DISTRICT = "select#j_idt540_input"
SEL_DISTRICT_FALLBACK = "select"
SEL_CHECKPOINT = "select#j_idt551_input"
SEL_CHECKPOINT_FALLBACK = "select"

# Phase 4 — confirmation modal
SEL_PAYMENT_DIALOG = "div#payment_dialog"

# Phase 5 — e-Vahan Payment Gateway
# Gateway dropdown id is j_idt13:j_idt42_input — unstable. We just scan
# all selects for one with the "EGRAS" option value.
SEL_PG_DROPDOWN = "select"

# Phase 6 — eGRAS Rajasthan splash
SEL_EGRAS_SPLASH_CONTINUE = "input#txtGo"

# Phase 7 — eGRAS Payment Details
SEL_UPI_TAB = "li#upi"
SEL_EGRAS_PROCEED = "input#btnSubmit"

# Phase 8 — QR page (ASP.NET WebForms — 'ctl00' is correct, lowercase L)
SEL_QR_RADIO = "input#ctl00_ContentPlaceHolder1_rblUpi_1"
SEL_QR_CONTAINER = "div#ctl00_ContentPlaceHolder1_divQRCode"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5
HUMAN_PAYMENT_TIMEOUT = 150  # RJ QR expires in ~3 min (~180s)
RECEIPT_POLL_TIMEOUT_SECS = 90  # RJ redirect chain is longer than UP/HR
GET_DETAILS_TIMEOUT_SECS = 30  # Get Details AJAX -> Owner Name populated
PERMIT_SET_TIMEOUT_SECS = 15
PAYMENT_GATEWAY_NAV_TIMEOUT = 45  # Confirm click -> e-Vahan PG page
EGRAS_SPLASH_NAV_TIMEOUT = 45  # PG Continue -> eGRAS splash
QR_PAGE_NAV_TIMEOUT = 45  # Proceed -> QR page (UPI ID radio visible)
CALCULATE_TAX_TIMEOUT_SECS = 15  # Calculate Tax click -> Total Amount populated


# ─── Helpers ───────────────────────────────────────────────────────────


def _dd_mm_yyyy(iso: str) -> str:
    """Convert YYYY-MM-DD to DD-MM-YYYY. RJ date inputs are plain text."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", iso)
    if not m:
        return iso
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


async def _read_input_value(session, selector: str) -> str:
    """Read .value of an input via CDP. Returns '' if not found."""
    expr = (
        "(function(s){var e=document.querySelector(s);"
        "return e?(e.value||''):'';})(" + json.dumps(selector) + ")"
    )
    return (await _cdp_eval(session, expr)) or ""


async def _wait_for_get_details_filled(
    session,
    timeout: int,
) -> str:
    """Poll for the Owner Name field becoming populated -- our readiness
    signal that Get Details has finished. Tries the known PrimeFaces id
    first, then attribute-based fallback, then any disabled+filled text
    input as a last resort.

    Returns the owner-name string if populated, '' on timeout."""
    expr = (
        "(function(){"
        "var sels = ["
        + json.dumps(SEL_OWNER_NAME)
        + ","
        + json.dumps(SEL_OWNER_NAME_FALLBACK)
        + ","
        '"input.ui-state-filled[disabled]"'
        "];"
        "for (var i = 0; i < sels.length; i++) {"
        "  var nodes = document.querySelectorAll(sels[i]);"
        "  for (var j = 0; j < nodes.length; j++) {"
        "    var v = nodes[j].value || '';"
        "    if (v.trim().length > 0) return v.trim();"
        "  }"
        "}"
        "return '';"
        "})()"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = await _cdp_eval(session, expr) or ""
        if val:
            return val
        await asyncio.sleep(0.5)
    return ""


async def _select_first_checkpoint(
    session,
    primary_selector: str,
    district_anchor: str,
) -> str | None:
    """Pick the first non-placeholder option in the checkpoint select.

    Tries `primary_selector` first. If that select doesn't exist or has
    no non-placeholder option (would happen if PrimeFaces j_idt ids
    shifted), falls back to scanning every <select> on the page for one
    whose option labels contain `district_anchor` (RJ checkpost labels
    embed the district name, e.g. 'DANPUR, BANSWARA(ON ...)' so we use
    the district as the recognition anchor).

    Returns the selected option text, or None if nothing was selectable."""
    expr = (
        "(function(sel, match){"
        "function pickFirst(e){"
        "  if(!(e instanceof HTMLSelectElement)) return null;"
        "  for(var i=0;i<e.options.length;i++){"
        "    var opt=e.options[i];"
        "    var v=(opt.value||'').trim();"
        "    if(v && v!=='-1' && v!=='0'){"
        "      e.value=opt.value;"
        "      e.dispatchEvent(new Event('input',{bubbles:true}));"
        "      e.dispatchEvent(new Event('change',{bubbles:true}));"
        "      return (opt.text||'').trim();"
        "    }"
        "  }"
        "  return null;"
        "}"
        "var primary = document.querySelector(sel);"
        "if (primary){"
        "  var picked = pickFirst(primary);"
        "  if (picked) return picked;"
        "}"
        "var sels = document.querySelectorAll('select');"
        "for (var si=0; si<sels.length; si++){"
        "  var e = sels[si];"
        "  if(!(e instanceof HTMLSelectElement)) continue;"
        "  var hasMatch = false;"
        "  for (var i=0; i<e.options.length; i++){"
        "    if ((e.options[i].text||'').indexOf(match) >= 0){"
        "      hasMatch = true; break;"
        "    }"
        "  }"
        "  if (!hasMatch) continue;"
        "  var picked = pickFirst(e);"
        "  if (picked) return picked;"
        "}"
        "return null;"
        "})(" + json.dumps(primary_selector) + "," + json.dumps(district_anchor) + ")"
    )
    return await _cdp_eval(session, expr)


async def _click_first_unchecked_checkbox(session) -> bool:
    """Click the first unchecked checkbox in the document. Returns whether
    a click was made. Same pattern UP uses for the PG terms checkbox."""
    expr = (
        "(function(){"
        "var cbs=document.querySelectorAll('input[type=checkbox]');"
        "for(var i=0;i<cbs.length;i++){"
        "  if(!cbs[i].checked){cbs[i].click();return true;}"
        "}"
        "return false;"
        "})()"
    )
    return bool(await _cdp_eval(session, expr))


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run RJ scripted border-tax. Always returns a RunOutcome."""

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
        "RJ",
        log=log,
        name="phase1.select_state_rj",
    )
    # RJ navigates to vahan.parivahan.gov.in (DIFFERENT host than UP/HR's
    # services.parivahan.gov.in). We accept any URL containing the
    # vahan.parivahan host -- the form is the readiness check below.
    await wait_for_url(
        session,
        "vahan.parivahan.gov.in",
        log=log,
        name="phase1.wait_evahan_landing",
        timeout=45,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase1.settle")

    # ─── Phase 2: service selection + Go ───────────────────────────────
    await wait_for_selector(
        session,
        SEL_SERVICE_DROPDOWN,
        log=log,
        name="phase2.wait_service_dropdown",
        timeout=30,
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

    # ─── Phase 3: mega-form (Vehicle No -> Calculate Tax) ──────────────

    # 3a. Vehicle No input + Get Details click.
    # Primary selector is the live PrimeFaces id (j_idt57). If that
    # query times out (id drifted on a redeploy), the attribute-based
    # fallback locks onto the same input.
    vehicle_filled = False
    last_err: Exception | None = None
    for sel in (SEL_VEHICLE_INPUT, SEL_VEHICLE_INPUT_FALLBACK):
        try:
            await wait_for_selector(
                session,
                sel,
                log=log,
                name=f"phase3.wait_vehicle_input[{sel}]",
                timeout=15,
            )
            await fill(
                session,
                sel,
                params.vehicleNumber,
                log=log,
                name=f"phase3.fill_vehicle[{sel}]",
            )
            vehicle_filled = True
            break
        except Exception as e:
            last_err = e
            continue
    if not vehicle_filled:
        return RunOutcome(
            status="failed",
            summary=(
                f"Could not locate the Vehicle No. input on the RJ mega-form. "
                f"Tried {SEL_VEHICLE_INPUT!r} and {SEL_VEHICLE_INPUT_FALLBACK!r}. "
                f"Last error: "
                f"{type(last_err).__name__ if last_err else 'n/a'}: "
                f"{last_err if last_err else 'n/a'}"
            ),
            abort_reason="selector_not_found:vehicle_input",
            run_log=log.dump(),
        )

    await click_by_text(
        session,
        "Get Details",
        log=log,
        name="phase3.click_get_details",
        tag="button",
    )

    # 3b. Wait for Get Details AJAX to finish -- signal is Owner Name
    # populated. If it never populates, check for a blocking popup and
    # surface the underlying error.
    owner_started = time.monotonic()
    owner_value = await _wait_for_get_details_filled(
        session,
        GET_DETAILS_TIMEOUT_SECS,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.wait_get_details",
            status=StepStatus.OK if owner_value else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - owner_started) * 1000),
            value=owner_value or "<empty>",
            error=None if owner_value else "owner_name_not_populated",
        )
    )
    if not owner_value:
        # Possibly a validity popup is blocking. Let abort_if_popup_text
        # surface a clearer reason if it finds one.
        await abort_if_popup_text(
            session,
            [
                "INSURANCE",
                "FITNESS",
                "PUCC",
                "EXPIRED",
                "RENEW",
                "NOT VALID",
                "FAILED",
                "ERROR",
            ],
            (
                f"Vehicle {params.vehicleNumber} details could not be "
                f"fetched (Owner Name did not populate within "
                f"{GET_DETAILS_TIMEOUT_SECS}s and a blocking popup is "
                f"present)."
            ),
            log=log,
            name="phase3.get_details_timeout_popup_check",
        )
        return RunOutcome(
            status="failed",
            summary=(
                f"Vehicle {params.vehicleNumber} details could not be "
                f"fetched within {GET_DETAILS_TIMEOUT_SECS}s after Get "
                f"Details click. No recognized error popup visible."
            ),
            abort_reason="get_details_timeout",
            run_log=log.dump(),
        )

    # Defensive validity-popup check after successful Get Details (some
    # vehicles get details AND a non-blocking-looking popup).
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
        name="phase3.check_validity_post_get_details",
    )

    # 3c. Read AITP date fields and log (non-blocking).
    # The HR receipt screenshot showed AITP Permit Validity can be empty
    # and the submission still succeeds, so we just record their state.
    aitp_validity_val = await _read_input_value(session, SEL_AITP_VALIDITY)
    aitp_auth_validity_val = await _read_input_value(
        session,
        SEL_AITP_AUTH_VALIDITY,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.read_aitp_fields",
            status=StepStatus.OK,
            value=(
                f"permit_validity={aitp_validity_val or '<empty>'}, "
                f"auth_validity={aitp_auth_validity_val or '<empty>'}"
            ),
        )
    )

    # 3d. Permit Type -- check if pre-filled. RJ's only options are
    # TEMPORARY PERMIT / TOURIST PERMIT, so we always have a sane
    # hard-default to fall back to.
    permit_val = await get_select_value(session, SEL_PERMIT_TYPE)
    if not permit_val or permit_val == "-1":
        permit_candidates: list[str] = []
        if params.permitType:
            permit_candidates.append(params.permitType)
        if params.permitTypeFallback and params.permitTypeFallback != params.permitType:
            permit_candidates.append(params.permitTypeFallback)
        # Hard default if neither param matches RJ's two options.
        if "TEMPORARY PERMIT" not in permit_candidates:
            permit_candidates.append("TEMPORARY PERMIT")

        permit_set = False
        last_err = None
        for cand in permit_candidates:
            try:
                await select_by_text(
                    session,
                    SEL_PERMIT_TYPE,
                    cand,
                    log=log,
                    name=f"phase3.select_permit_type[{cand}]",
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
                    f"Could not set Permit Type on RJ mega-form. "
                    f"Tried {permit_candidates}. Last error: "
                    f"{type(last_err).__name__ if last_err else 'n/a'}: "
                    f"{last_err if last_err else 'n/a'}"
                ),
                abort_reason="permit_type_not_settable",
                run_log=log.dump(),
            )
        # Let PrimeFaces react before next dropdown query.
        await asyncio.sleep(1.0)
    else:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase3.permit_already_set",
                status=StepStatus.SKIPPED,
                selector=SEL_PERMIT_TYPE,
                value=permit_val,
            )
        )

    # 3e. District through Entering -- required.
    if not params.entryDistrict:
        return RunOutcome(
            status="failed",
            summary="entryDistrict param is required for RJ",
            abort_reason="missing_param:entryDistrict",
            run_log=log.dump(),
        )
    try:
        await select_by_text(
            session,
            SEL_DISTRICT,
            params.entryDistrict,
            log=log,
            name="phase3.select_district[primary]",
            timeout=10,
        )
    except Exception:
        # PrimeFaces j_idt drifted -- broad scan via select_by_text
        # (which already iterates all matches and picks first with the
        # target option).
        await select_by_text(
            session,
            SEL_DISTRICT_FALLBACK,
            params.entryDistrict,
            log=log,
            name="phase3.select_district[fallback]",
            timeout=15,
        )

    # Let checkpost options populate (cascades from district selection).
    await asyncio.sleep(1.5)

    # 3f. Purpose of visit -- SKIPPED per spec. The dropdown has only
    # fair-related options (RAMDEVRA FAIR / URS FAIR) that don't apply
    # to normal commercial traffic. RJ accepts submissions without it
    # in practice.

    # 3g. Check Post Name Through Entering.
    if params.entryCheckpoint:
        try:
            await select_by_text(
                session,
                SEL_CHECKPOINT,
                params.entryCheckpoint,
                log=log,
                name="phase3.select_checkpoint[primary]",
                timeout=10,
            )
        except Exception:
            await select_by_text(
                session,
                SEL_CHECKPOINT_FALLBACK,
                params.entryCheckpoint,
                log=log,
                name="phase3.select_checkpoint[fallback]",
                timeout=15,
            )
    else:
        # No param -- pick the first non-placeholder option. RJ checkpost
        # labels embed the district name as a stable recognition anchor.
        cp_started = time.monotonic()
        picked = await _select_first_checkpoint(
            session,
            SEL_CHECKPOINT,
            params.entryDistrict,
        )
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase3.select_checkpoint_auto",
                status=StepStatus.OK if picked else StepStatus.FAILED,
                duration_ms=int((time.monotonic() - cp_started) * 1000),
                selector=SEL_CHECKPOINT,
                value=picked or "<none>",
                error=None if picked else "no_non_placeholder_option",
            )
        )
        if not picked:
            return RunOutcome(
                status="failed",
                summary=(
                    f"Could not auto-pick a Check Post Name for district "
                    f"{params.entryDistrict!r} -- no non-placeholder "
                    f"option found in the dropdown."
                ),
                abort_reason="checkpoint_auto_pick_failed",
                run_log=log.dump(),
            )

    # 3h. Tax From / Tax Upto -- plain DD-MM-YYYY text inputs.
    tax_from_ddmm = _dd_mm_yyyy(params.taxFrom)
    tax_upto_ddmm = _dd_mm_yyyy(params.taxUpto)

    await fill(
        session,
        SEL_TAX_FROM,
        tax_from_ddmm,
        log=log,
        name="phase3.fill_tax_from",
    )
    await fill(
        session,
        SEL_TAX_UPTO,
        tax_upto_ddmm,
        log=log,
        name="phase3.fill_tax_upto",
    )

    # Let Tax Mode + No of Periods auto-populate. Per rj.ts spec, this
    # can take ~2s after both dates are typed.
    await sleep_seconds(2.5, log=log, name="phase3.wait_tax_autofill")

    # If Tax Mode hasn't filled, nudge with a blur dispatch on the
    # second date input (sometimes the focus state matters).
    tax_mode_val = await get_select_value(session, SEL_TAX_MODE)
    if not tax_mode_val or tax_mode_val == "-1":
        await _cdp_eval(
            session,
            "(function(s){var e=document.querySelector(s);"
            "if(e){e.dispatchEvent(new Event('blur',{bubbles:true}));}})("
            + json.dumps(SEL_TAX_UPTO)
            + ")",
        )
        await sleep_seconds(
            2.0,
            log=log,
            name="phase3.wait_tax_autofill_retry",
        )
        tax_mode_val = await get_select_value(session, SEL_TAX_MODE)

    if not tax_mode_val or tax_mode_val == "-1":
        # Soft warning -- Calculate Tax might still work, or it'll fail
        # with a clearer message we'll catch below.
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase3.tax_mode_not_autofilled",
                status=StepStatus.RETRIED,
                value="<empty>",
                error=(
                    "Tax Mode did not auto-fill after typing both dates. "
                    "Proceeding -- Calculate Tax may fail."
                ),
            )
        )

    # 3i. Calculate Tax.
    await click_by_text(
        session,
        "Calculate Tax",
        log=log,
        name="phase3.click_calculate_tax",
        tag="button",
    )

    # Poll for Total Amount to be populated. The Particulars table
    # renders along with the value -- a non-empty/non-zero value is the
    # canonical success signal.
    total_amount = ""
    deadline = time.monotonic() + CALCULATE_TAX_TIMEOUT_SECS
    while time.monotonic() < deadline:
        total_amount = (
            await _read_input_value(
                session,
                SEL_TOTAL_AMOUNT,
            )
        ).strip()
        if total_amount and total_amount not in ("", "0", "0.0", "0.00"):
            break
        await asyncio.sleep(0.5)

    if not total_amount or total_amount in ("", "0", "0.0", "0.00"):
        return RunOutcome(
            status="failed",
            summary=(
                f"Calculate Tax did not produce a positive Total Amount "
                f"within {CALCULATE_TAX_TIMEOUT_SECS}s. Read value: "
                f"{total_amount!r}. Likely the date range was rejected "
                f"or a required field is missing."
            ),
            abort_reason="calculate_tax_zero",
            run_log=log.dump(),
        )

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.total_amount_calculated",
            status=StepStatus.OK,
            selector=SEL_TOTAL_AMOUNT,
            value=total_amount,
        )
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase3.settle")

    # ─── Phase 4: Pay Tax + Confirmation modal ─────────────────────────
    await click_by_text(
        session,
        "Pay Tax",
        log=log,
        name="phase4.click_pay_tax",
        tag="button",
    )
    await wait_for_selector(
        session,
        SEL_PAYMENT_DIALOG,
        log=log,
        name="phase4.wait_confirmation_dialog",
        timeout=10,
    )
    # click_by_text finds the first visible button with the text. The
    # dialog's Confirm is the only visible Confirm button at this point.
    await click_by_text(
        session,
        "Confirm",
        log=log,
        name="phase4.click_confirm",
        tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase4.settle")

    # ─── Phase 5: e-Vahan Payment Gateway ──────────────────────────────
    # Wait until a checkbox appears (the terms-and-conditions one) --
    # a stable signal that we've landed on the PG page. The page also
    # contains a "Payment ID" beginning with "RJL..." but the checkbox
    # is the cheapest readiness probe.
    pg_ready = False
    deadline = time.monotonic() + PAYMENT_GATEWAY_NAV_TIMEOUT
    while time.monotonic() < deadline:
        has_cb = await _cdp_eval(
            session,
            "document.querySelectorAll('input[type=checkbox]').length > 0",
        )
        if has_cb:
            pg_ready = True
            break
        await asyncio.sleep(0.5)
    if not pg_ready:
        return RunOutcome(
            status="failed",
            summary=(
                f"e-Vahan Payment Gateway page did not load within "
                f"{PAYMENT_GATEWAY_NAV_TIMEOUT}s after clicking Confirm."
            ),
            abort_reason="payment_gateway_nav_timeout",
            run_log=log.dump(),
        )

    # Select E-GRAS in the gateway dropdown. select_by_value scans all
    # <select> elements and picks the first one with value="EGRAS" --
    # the unstable j_idt id of the dropdown is irrelevant.
    await select_by_value(
        session,
        SEL_PG_DROPDOWN,
        "EGRAS",
        log=log,
        name="phase5.select_egras",
        timeout=10,
    )

    # Tick the terms checkbox.
    cb_started = time.monotonic()
    cb_clicked = await _click_first_unchecked_checkbox(session)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase5.accept_terms",
            status=StepStatus.OK if cb_clicked else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - cb_started) * 1000),
            value="clicked" if cb_clicked else "no_unchecked_cb_found",
            error=None if cb_clicked else "no_unchecked_checkbox",
        )
    )
    if not cb_clicked:
        return RunOutcome(
            status="failed",
            summary=(
                "Could not find the 'I accept terms and conditions' "
                "checkbox on the e-Vahan Payment Gateway page."
            ),
            abort_reason="terms_checkbox_not_found",
            run_log=log.dump(),
        )

    # Continue button has unstable id (j_idt13:bt_payment) -- click by text.
    await click_by_text(
        session,
        "Continue",
        log=log,
        name="phase5.click_continue",
        tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase5.settle")

    # ─── Phase 6: eGRAS Rajasthan splash ───────────────────────────────
    # The single big CONTINUE is an <input type="image" id="txtGo">, not
    # a <button>, so click_by_text won't match it. Use direct CSS click.
    await wait_for_selector(
        session,
        SEL_EGRAS_SPLASH_CONTINUE,
        log=log,
        name="phase6.wait_egras_splash",
        timeout=EGRAS_SPLASH_NAV_TIMEOUT,
    )
    await click(
        session,
        SEL_EGRAS_SPLASH_CONTINUE,
        log=log,
        name="phase6.click_egras_continue",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase6.settle")

    # ─── Phase 7: eGRAS Payment Details (tab picker) ───────────────────
    await wait_for_selector(
        session,
        SEL_UPI_TAB,
        log=log,
        name="phase7.wait_payment_details",
        timeout=30,
    )
    await click(
        session,
        SEL_UPI_TAB,
        log=log,
        name="phase7.click_upi_tab",
    )
    # Let the right-side UPI panel render before clicking Proceed.
    await asyncio.sleep(1.0)
    await click(
        session,
        SEL_EGRAS_PROCEED,
        log=log,
        name="phase7.click_proceed",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase7.settle")

    # ─── Phase 8: QR page (UPI ID -> postback -> QR CODE visible) ──────
    # IMPORTANT: the page initially shows the UPI ID radio active. We
    # must click the QR CODE radio (rblUpi_1), which fires an ASP.NET
    # __doPostBack and re-renders the page with the QR <img>.
    await wait_for_selector(
        session,
        SEL_QR_RADIO,
        log=log,
        name="phase8.wait_qr_radio",
        timeout=QR_PAGE_NAV_TIMEOUT,
    )
    await click(
        session,
        SEL_QR_RADIO,
        log=log,
        name="phase8.click_qr_radio",
    )
    await wait_for_selector(
        session,
        SEL_QR_CONTAINER,
        log=log,
        name="phase8.wait_qr_container",
        timeout=30,
    )
    await sleep_seconds(1.0, log=log, name="phase8.qr_settle")

    # Save QR. actions.save_qr_code already knows the RJ container id.
    qr_started = time.monotonic()
    qr_result = await save_qr_code(session, job_id, job_params)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase8.save_qr_code",
            status=(StepStatus.OK if qr_result.get("ok") else StepStatus.FAILED),
            duration_ms=int((time.monotonic() - qr_started) * 1000),
            error=None if qr_result.get("ok") else qr_result.get("error"),
        )
    )
    # Non-blocking: even if upload failed, we still wait for the human.

    # Wait for human payment confirmation.
    human_started = time.monotonic()
    payment_reason = (
        f"UPI payment required for border tax of vehicle "
        f"{params.vehicleNumber} entering Rajasthan. A QR code is displayed "
        f"on screen -- please scan with your UPI app and complete the "
        f"payment within ~3 minutes (the RJ portal QR has a short expiry). "
        f"After payment is successful, reply 'done' to continue."
    )
    human_reply = await wait_for_human_via_redis(
        job_id,
        r,
        payment_reason,
        timeout=HUMAN_PAYMENT_TIMEOUT,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase8.wait_for_human_payment",
            status=(StepStatus.HANDED_OFF if human_reply else StepStatus.FAILED),
            duration_ms=int((time.monotonic() - human_started) * 1000),
            value=human_reply[:80] if human_reply else None,
            handoff_reason="upi_payment",
            handoff_summary=(human_reply[:300] if human_reply else "human_timeout"),
        )
    )
    if not human_reply:
        return RunOutcome(
            status="partial",
            summary=(
                "UPI payment wait timed out. Money may have been deducted; "
                "receipt was not captured. Manual reconciliation needed."
            ),
            partial_reasons=["human_timeout:upi_payment"],
            run_log=log.dump(),
        )

    # ─── Phase 9: poll for receipt, extract, save PDF ──────────────────
    # RJ receipt markers (from a confirmed receipt sample):
    #   - "GOVERNMENT OF RAJASTHAN" heading
    #   - "Checkpost Tax e-Receipt" subheading
    #   - "Receipt No." with an "RJT..." value
    #   - "Grand Total :" near the bottom
    receipt_ready = False
    deadline = time.monotonic() + RECEIPT_POLL_TIMEOUT_SECS
    while time.monotonic() < deadline:
        markers = await _cdp_eval(
            session,
            """
            (function() {
                var text = (document.body.innerText || '').toUpperCase();
                return {
                    govHeader:     text.indexOf('GOVERNMENT OF RAJASTHAN') >= 0,
                    receiptHeader: text.indexOf('CHECKPOST TAX E-RECEIPT') >= 0,
                    receiptNo:     text.indexOf('RECEIPT NO') >= 0,
                    grandTotal:    text.indexOf('GRAND TOTAL') >= 0
                };
            })()
            """,
        )
        if markers and all(markers.values()):
            receipt_ready = True
            break
        await asyncio.sleep(3)

    if not receipt_ready:
        return RunOutcome(
            status="partial",
            summary=(
                "Payment confirmed but receipt page did not render within "
                f"{RECEIPT_POLL_TIMEOUT_SECS}s. Money was deducted; "
                "receipt PDF was not captured."
            ),
            partial_reasons=["receipt_page_timeout"],
            run_log=log.dump(),
        )

    receipt_data = await _extract_receipt_fields(session, params.vehicleNumber)
    if not receipt_data:
        return RunOutcome(
            status="partial",
            summary=(
                "Receipt page rendered but key fields could not be parsed. "
                "Money was deducted; receipt PDF was not captured."
            ),
            partial_reasons=["receipt_parse_failed"],
            run_log=log.dump(),
        )

    save_started = time.monotonic()
    save_result = await save_receipt(
        session,
        job_id,
        job_params,
        receipt_data,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase9.save_receipt",
            status=(StepStatus.OK if save_result.get("ok") else StepStatus.FAILED),
            duration_ms=int((time.monotonic() - save_started) * 1000),
            value=receipt_data.get("receiptNumber"),
            error=None if save_result.get("ok") else save_result.get("error"),
        )
    )

    if save_result.get("ok") and save_result.get("pdfUploaded", True):
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax paid for vehicle {params.vehicleNumber} into "
                f"Rajasthan. Receipt {receipt_data['receiptNumber']}, "
                f"amount ₹{receipt_data['amount']}, "
                f"payment date {receipt_data['paymentDate']}."
            ),
            receipt_number=receipt_data["receiptNumber"],
            amount=float(receipt_data["amount"]),
            run_log=log.dump(),
        )

    return RunOutcome(
        status="partial",
        summary=(
            f"Payment successful but receipt PDF upload failed: "
            f"{save_result.get('error', 'unknown error')}. "
            f"Receipt number {receipt_data['receiptNumber']} captured."
        ),
        partial_reasons=["receipt_pdf_upload_failed"],
        receipt_number=receipt_data["receiptNumber"],
        amount=float(receipt_data["amount"]),
        run_log=log.dump(),
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
    """14-MAY-2026 -> 2026-05-14. RJ prints uppercase month abbreviations
    (where UP/HR use mixed case); we uppercase-normalize either way."""
    m = re.match(r"(\d{1,2})-(\w{3})-(\d{4})", s.strip(), re.IGNORECASE)
    if not m:
        return None
    day, mon_abbr, year = m.group(1).zfill(2), m.group(2).upper(), m.group(3)
    mon_num = _MONTH_TO_NUM.get(mon_abbr)
    if not mon_num:
        return None
    return f"{year}-{mon_num}-{day}"


async def _extract_receipt_fields(
    session,
    vehicle_number: str,
) -> dict | None:
    """Scrape receiptNumber, amount, paymentDate from the RJ receipt page.

    Sample format from a confirmed receipt (14-MAY-2026 print):
        Receipt No.              : RJT2605145702835
        Grand Total : ₹ 224/- ( TWO HUNDRED TWENTY FOUR ONLY)
        Payment Initiation Date  : 14-MAY-2026 12:42 AM

    Differences vs UP/HR's receipt regexes:
      - prefix "RJT..." (the generic [A-Z0-9]+ pattern still captures it)
      - Grand Total has a leading ₹ symbol (not present in UP/HR);
        we add ₹? to allow it.
      - date label is "Payment Initiation Date" (not "Payment Confirmation
        Date" like UP/HR); we accept either word so the same parser
        survives a future portal rename in either direction.
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

    # Grand Total : ₹ 224/-   -- ₹ is U+20B9, not whitespace, so explicit.
    amount_match = re.search(
        r"Grand\s*Total\s*:?\s*₹?\s*(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    amount = float(amount_match.group(1)) if amount_match else None

    date_match = re.search(
        r"Payment\s+(?:Initiation|Confirmation)\s+Date\s*:?\s*"
        r"(\d{1,2}-\w{3}-\d{4})",
        text,
        re.IGNORECASE,
    )
    payment_date = _normalize_receipt_date(date_match.group(1)) if date_match else None

    if not (receipt_number and amount is not None and payment_date):
        print(
            f"[rj] receipt parse incomplete: "
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

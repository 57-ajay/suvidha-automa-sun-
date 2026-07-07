# worker/src/scripted/border_tax/_handover_runner.py
"""Shared scripted runner for *form-fill-then-human-handover* states.

Some states (HP, BR, ...) are driven WITHOUT any AI. The script:

    Phase 1  - parivahan.gov.in/en/node/579, select the state
    Phase 2  - service selection ("VEHICLE TAX COLLECTION (OTHER STATE)"), Go
    Phase 3  - Owner Information: vehicle, Get Details, Entry District,
               Entry Checkpost, Next
               -> if a "pending transaction" popup appears we ABORT
                  immediately with a clear "clear it first" error (NO
                  auto-clear for these states, unlike UP/HR/PB/MP/UK).
    Phase 4  - Vehicle Information: Vehicle Category / Permit Type /
               Service Type — set only if EMPTY (RC data often pre-fills
               these), then Next.
    Phase 5  - Tax Information: Tax Mode, Tax From, Tax Upto, Calculate
               Fee/Tax, Next.

…and that's it. We stop the moment we land on the Disclaimer page (the
captcha + Pay Online step, i.e. JUST BEFORE the payment-gateway page).
From there we set status=waiting_for_human, start a 15-minute timeout, and
poll the page for the receipt every few seconds in the background via
`web_handover_and_capture(poll_qr=False)`. The human solves the captcha,
picks a payment method, and pays; the moment the receipt page renders we
capture the PDF and finish automatically — we never block on an explicit
human "done", because the human may pay and never notify us.

This is intentionally the same surface as UK's `source == "web"` branch
(handover right after Phase 5), but applied UNCONDITIONALLY — there is no
AI captcha handoff for these states at all, for any source.

Per-state differences are expressed via `StateHandoverConfig`:
  - state_code         parivahan <option value> on the landing dropdown
  - state_name         human-readable label (used in messages)
  - checkpost_strategy how Phase 3 picks the Entry Checkpost:
                         "match_district" → the checkpost name equals the
                            selected district name (HP).
                         "first_option"   → just pick the first available
                            checkpost (BR).
  - payment_config     PaymentCaptureConfig for the background receipt poll
  - extract_receipt_fields  async (session, vehicle_number) -> dict | None

The parivahan Angular DOM (and its typos — "floatingPrmit", "uptpDate") is
identical to UP/HR/PB/MP/UK, so the selectors here match those states.

Tax-date NOTE: parivahan is inconsistent here — HP's Tax From / Tax Upto
are `type="datetime-local"` (accepted value "YYYY-MM-DDTHH:MM") while BR's
are `type="date"` (accepted value "YYYY-MM-DD"). Setting the wrong format
makes the browser silently drop the value, so `_set_date_like` reads each
input's `type` and formats to match — one code path serves both. Either way
the Tax Upto field's `min` carries today's date, so a same-day Tax Upto is
rejected, which is why HP/BR are registered as NO_SAME_DAY states on the API
side (taxUpto = taxFrom + duration, i.e. at least tomorrow), keeping the
value above the min.

Tax-mode NOTE: the parivahan portal renders different Tax Mode options per
RC (decided at the Phase 3 RC lookup), so a requested mode may legitimately
not be offered for a given vehicle. When the requested mode is absent we
abort with abort_reason "tax_mode_not_offered_for_rc" and a summary listing
the modes the portal did offer, so the client can retry with a valid one. In
DAYS mode Tax Upto is editable and we fill it; in QUARTERLY / YEARLY mode the
portal disables Tax Upto (auto-derived), so we DO NOT fill it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from ..log import StepLogger
from ..steps import (
    _cdp_eval,
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
from ..types import RunOutcome, StepLog, StepStatus
from .params import BorderTaxParams
from ._extract_amount import extract_and_save_border_tax_amount
from ._payment_wait import PaymentCaptureConfig
from ._web_handover import web_handover_and_capture
from ._pending_clear import wait_for_owner_info_outcome
from ._manual_entry import (
    dismiss_no_data_popup,
    fill_owner_info_manual,
    fill_vehicle_info_manual,
)
from ._tax_time import ist_hhmm, stamp_dtlocal


# ─── Selectors (shared parivahan Angular DOM) ──────────────────────────

# Phase 1 — parivahan landing
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"

# Phase 2 — service selection
SEL_SERVICE_DROPDOWN = "select[name='serviceName']"

# Phase 3 — owner info
SEL_VEHICLE_INPUT = "input#floatingvehicle"
SEL_DISTRICT = "select#floatingDistrict"
SEL_CHECKPOINT = "select#floatingCheckpost"

# Phase 4 — vehicle info (note the parivahan typo "floatingPrmit")
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"
SEL_PERMIT_TYPE = "select#floatingPrmit"
SEL_SERVICE_TYPE = "select#floatingService"

# Phase 5 — tax info. HP/BR match UP/UK ids: lowercase 'floatingtaxmode'
# select and the "uptpDate" typo on the Tax Upto field. Inputs are
# datetime-local for HP/BR (see module docstring).
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingtaxmode",
    "select#floatingTaxMode",
    "select[name='taxMode']",
    "select[name='taxmode']",
]
SEL_TAX_FROM = "input#floatingTaxfrom"
SEL_TAX_UPTO = "input#uptpDate"

# Known parivahan Tax Mode <option> values (stable across states). The
# option *text* carries a leading space (" DAYS"), so matching by value is
# more robust than matching by text.
_TAX_MODE_VALUES = {
    "DAYS": "1",
    "MONTHLY": "3",  # best-effort; not every state exposes monthly
    "QUARTERLY": "5",
    "YEARLY": "7",
}


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5  # polite breath between phases
PERMIT_SET_TIMEOUT_SECS = 15  # per-attempt timeout when setting permit type
CHECKPOINT_POPULATE_TIMEOUT = 10
HANDOVER_TIMEOUT_SECS = 900  # 15 minutes (human captcha + payment window)


# ─── Config ────────────────────────────────────────────────────────────


@dataclass
class StateHandoverConfig:
    """Per-state knobs for the form-fill-then-handover flow."""

    state_code: str  # parivahan <option value>, e.g. "HP"
    state_name: str  # human-readable, e.g. "Himachal Pradesh"
    checkpost_strategy: Literal["match_district", "first_option"]
    payment_config: PaymentCaptureConfig
    extract_receipt_fields: Callable[..., Awaitable[dict | None]]

    # ── Manual-entry fallback (VAHAN "No data found" → fill from DB) ──
    # Forwarded to fill_vehicle_info_manual. Defaults match the CheckPost
    # V4 norm UK/PB/MP share (Vehicle Category present, no Distance field,
    # plain type="date" validity inputs). Override per state if the DOM
    # differs — e.g. set manual_datetime_local_dates=True where the
    # vehicle-info validity dates are <input type="datetime-local">.
    manual_has_category: bool = True
    manual_has_distance: bool = False
    manual_datetime_local_dates: bool = False


# ─── Small DOM helpers ─────────────────────────────────────────────────


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
    so we poll rather than read once.
    """
    expr = (
        "(function(s){"
        "  var e=document.querySelector(s);"
        "  if(!(e instanceof HTMLSelectElement)) return {ok:false, reason:'not_a_select'};"
        "  for(var i=0;i<e.options.length;i++){"
        "    var v=e.options[i].value;"
        "    if(v && v!=='-1' && v!=='0'){"
        "      return {ok:true, value:v, text:(e.options[i].text||'').trim()};"
        "    }"
        "  }"
        "  return {ok:false, reason:'no_non_placeholder_option'};"
        "})(" + json.dumps(selector) + ")"
    )
    deadline = time.monotonic() + timeout
    last = {"ok": False, "reason": "timeout"}
    while time.monotonic() < deadline:
        res = await _cdp_eval(session, expr)
        if res and res.get("ok"):
            return res
        if res:
            last = res
        await asyncio.sleep(0.5)
    return last


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

    1. Wait until the dropdown has at least one non-placeholder option
       (Angular populates these only after a district is chosen).
    2. If `desired` is non-empty and matches an option's text (exact ->
       case-insensitive equal), select it. (HP: checkpost == district.)
    3. Otherwise pick the FIRST non-empty option. (BR, or HP fallback.)

    Returns {"ok": True, "selected", "value", "fallback_used"} or
    {"ok": False, "reason": ...}.
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
                        f"{res.get('selected')} (value={res.get('value')}, "
                        f"fallback={res.get('fallback_used')})"
                    ),
                )
            )
            return res
        if res:
            last_reason = res.get("reason", "no_result")
        if time.monotonic() > deadline:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    error=f"checkpost not selectable: {last_reason}",
                )
            )
            return {"ok": False, "reason": last_reason}
        await asyncio.sleep(0.5)


async def _select_tax_mode(
    session,
    desired: str,
    *,
    log: StepLogger,
    timeout: int = 45,
) -> tuple[bool, list[str]]:
    """Select the Tax Mode option robustly and return (ok, available_labels).

    Matches by known <option> value first (DAYS=1, QUARTERLY=5, YEARLY=7),
    then by trimmed/case-insensitive text, re-querying the live <select>
    each poll iteration — the parivahan Tax Mode control is populated
    asynchronously and the DAYS option can appear a beat after the others
    (and its text carries a leading space). If the option has not shown up
    after a few seconds we dispatch focus/mousedown to nudge any lazy
    Angular population. `available_labels` is the trimmed text of the
    non-placeholder options seen on the final poll, so the caller can report
    exactly which modes were offered if `desired` never appears.
    """
    want_value = _TAX_MODE_VALUES.get(desired.strip().upper(), "")
    want_text = desired.strip().upper()

    select_js = (
        "(function(sels, wantValue, wantText){"
        "  for (var si=0; si<sels.length; si++){"
        "    var e=document.querySelector(sels[si]);"
        "    if(!(e instanceof HTMLSelectElement)) continue;"
        "    var labels=[]; var hitIdx=-1;"
        "    for(var i=0;i<e.options.length;i++){"
        "      var o=e.options[i]; var t=(o.text||'').trim(); var v=(o.value||'');"
        "      if(v!==''){ labels.push(t); }"
        "      var hit=false;"
        "      if(wantValue && v===wantValue){ hit=true; }"
        "      else if(v!=='' && t.toUpperCase()===wantText){ hit=true; }"
        "      if(hit){ hitIdx=i; }"
        "    }"
        "    if(hitIdx>=0){"
        "      e.selectedIndex=hitIdx; e.value=e.options[hitIdx].value;"
        "      e.dispatchEvent(new Event('input',{bubbles:true}));"
        "      e.dispatchEvent(new Event('change',{bubbles:true}));"
        "      return {ok:true, value:e.options[hitIdx].value,"
        "              text:(e.options[hitIdx].text||'').trim(), labels:labels};"
        "    }"
        "    return {ok:false, labels:labels};"
        "  }"
        "  return {ok:false, labels:[]};"
        "})("
        + json.dumps(SEL_TAX_MODE_CANDIDATES)
        + ", "
        + json.dumps(want_value)
        + ", "
        + json.dumps(want_text)
        + ")"
    )
    nudge_js = (
        "(function(sels){"
        "  for (var si=0; si<sels.length; si++){"
        "    var e=document.querySelector(sels[si]);"
        "    if(e instanceof HTMLSelectElement){"
        "      try{ e.focus(); }catch(_){}"
        "      e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
        "      e.dispatchEvent(new Event('click',{bubbles:true}));"
        "      return true;"
        "    }"
        "  }"
        "  return false;"
        "})(" + json.dumps(SEL_TAX_MODE_CANDIDATES) + ")"
    )

    started = time.monotonic()
    deadline = started + timeout
    nudged = False
    last_labels: list[str] = []
    while time.monotonic() < deadline:
        res = await _cdp_eval(session, select_js)
        if res and res.get("ok"):
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=f"phase5.select_tax_mode[{desired}]",
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=SEL_TAX_MODE_CANDIDATES[0],
                    value=f"{res.get('text')} (value={res.get('value')}); "
                    f"offered={res.get('labels')}",
                )
            )
            return True, list(res.get("labels") or [])
        if res:
            last_labels = list(res.get("labels") or []) or last_labels
        # Nudge once if the option still isn't there after a few seconds —
        # focus/mousedown can trigger lazy Angular option population.
        if (not nudged) and (time.monotonic() - started) > 6:
            await _cdp_eval(session, nudge_js)
            nudged = True
        await asyncio.sleep(0.5)

    log.record(
        StepLog(
            index=log.next_index(),
            name=f"phase5.select_tax_mode[{desired}]",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            selector=SEL_TAX_MODE_CANDIDATES[0],
            error=f"tax mode {desired!r} not found; available={last_labels}",
        )
    )
    return False, last_labels


async def _set_date_like(
    session,
    selector: str,
    iso_date: str,
    *,
    hhmm: str | None,
    log: StepLogger,
    name: str,
) -> str:
    """Fill a date-ish input, formatting by its `type`, and return the DOM
    .value the field actually accepted (so the caller can verify it stuck
    above the field's `min`).

    datetime-local inputs (e.g. HP) get the caller's IST `hhmm` stamped;
    type="date" (or text/unknown, e.g. Bihar) get the bare ISO date. Setting
    the wrong format makes the browser silently drop the value, so we read the
    element's type first and format to match.
    """
    input_type = await _cdp_eval(
        session,
        "(function(s){var e=document.querySelector(s);"
        "return e?((e.getAttribute('type')||e.type||'')).toLowerCase():'';})("
        + json.dumps(selector)
        + ")",
    )
    use_time = (input_type or "") == "datetime-local"
    # datetime-local → "YYYY-MM-DDTHH:MM" with the shared IST time; date/text/
    # unknown → plain "YYYY-MM-DD" (hhmm ignored). See _tax_time.py.
    value = stamp_dtlocal(iso_date, hhmm if use_time else None)
    await fill(session, selector, value, log=log, name=name)
    actual = await _cdp_eval(
        session,
        "(function(s){var e=document.querySelector(s);return e?(e.value||''):'';})("
        + json.dumps(selector)
        + ")",
    )
    return str(actual or "")


#
# async def _set_date_like(
#     session,
#     selector: str,
#     iso_date: str,
#     *,
#     log: StepLogger,
#     name: str,
# ) -> str:
#     """Fill a date-ish input, formatting by its `type`, and return the DOM
#     .value the field actually accepted (so the caller can verify it stuck
#     above the field's `min`).
#
#     parivahan is inconsistent across states: some Tax From / Tax Upto inputs
#     are `type="date"` (canonical value "YYYY-MM-DD", e.g. Bihar) and some are
#     `type="datetime-local"` (value "YYYY-MM-DDTHH:MM", e.g. HP). Setting the
#     wrong format makes the browser silently drop the value, so we read the
#     element's type first and format to match.
#     """
#     input_type = await _cdp_eval(
#         session,
#         "(function(s){var e=document.querySelector(s);"
#         "return e?((e.getAttribute('type')||e.type||'')).toLowerCase():'';})("
#         + json.dumps(selector)
#         + ")",
#     )
#     if (input_type or "") == "datetime-local":
#         value = f"{iso_date}T00:00"
#     else:
#         # type="date" (or text/unknown) → plain ISO date.
#         value = iso_date
#     await fill(session, selector, value, log=log, name=name)
#     actual = await _cdp_eval(
#         session,
#         "(function(s){var e=document.querySelector(s);return e?(e.value||''):'';})("
#         + json.dumps(selector)
#         + ")",
#     )
#     return str(actual or "")
#

# ─── Public entrypoint ─────────────────────────────────────────────────


async def run_handover_flow(
    session,
    params: BorderTaxParams,
    log: StepLogger,
    *,
    config: StateHandoverConfig,
) -> RunOutcome:
    """Drive parivahan Phases 1-5 for `config.state_code`, then hand the
    captcha + payment to a human and poll for the receipt in the
    background. Always returns a RunOutcome.
    """
    job_id = log.job_id
    r = log.r
    job_params = params.model_dump()
    sc = config.state_code
    tag = sc.lower()

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
        sc,
        log=log,
        name=f"phase1.select_state_{tag}",
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

    # Owner-info outcome routing. For these states we do NOT auto-clear a
    # pending transaction — we abort with a clear "clear it first" message.
    outcome = await wait_for_owner_info_outcome(
        session, log, name="phase3.wait_owner_outcome"
    )
    if outcome == "pending_popup":
        return RunOutcome(
            status="failed",
            summary=(
                f"Vehicle {params.vehicleNumber} already has a pending "
                f"transaction at the parivahan portal. Please clear the "
                f"pending transaction first, then retry."
            ),
            abort_reason="pending_transaction_popup",
            run_log=log.dump(),
        )
    if outcome == "validity_popup":
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
    if outcome == "timeout":
        return RunOutcome(
            status="failed",
            summary=(
                f"Owner-info page did not respond within 30s after Get "
                f"Details for vehicle {params.vehicleNumber}. The portal may "
                f"be slow or the central RC fetch failed silently."
            ),
            abort_reason="get_details_timeout",
            run_log=log.dump(),
        )
    # outcome == "district_ready" or "manual_entry".
    #
    # manual_entry = VAHAN had no data ("No data found for this vehicle
    # number…"). Dismiss the popup and fill Chassis / Owner / Mobile /
    # From-State from the DB record before picking District + Checkpost
    # (those are physical route attributes the manual fill deliberately
    # leaves to the code below, exactly as on the VAHAN-success path).
    is_manual = outcome == "manual_entry"
    if is_manual:
        if not params.vehicleDetails:
            return RunOutcome(
                status="failed",
                summary=(
                    f"VAHAN returned no data for {params.vehicleNumber} and "
                    f"no vehicleDetails record was attached to the job, so "
                    f"the owner/vehicle forms can't be auto-filled."
                ),
                abort_reason="manual_entry_no_db_record",
                run_log=log.dump(),
            )
        await dismiss_no_data_popup(
            session, log=log, name="phase3.dismiss_no_data"
        )
        await fill_owner_info_manual(
            session,
            params.vehicleDetails,
            log=log,
            name_prefix="phase3.manual",
            mobile_number=(
                params.mobileNumber
                if params.mobileNumber is not None
                else "0000000000"
            ),
        )

    # 3a. Entry District — try the configured/default district by text,
    #     fall back to the first available option for robustness.
    district_text = params.entryDistrict
    district_set = False
    if params.entryDistrict:
        try:
            await select_by_text(
                session,
                SEL_DISTRICT,
                params.entryDistrict,
                log=log,
                name="phase3.select_district",
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
            session,
            SEL_DISTRICT,
            opt["value"],
            log=log,
            name="phase3.select_district_first",
        )
        district_text = opt.get("text", "") or district_text

    # Checkpost options populate only after a district is chosen.
    await asyncio.sleep(1.0)

    # 3b. Entry Checkpost — strategy depends on the state.
    #     HP: checkpost name == district name → match district text.
    #     BR: checkpost names differ from districts → first option.
    desired_checkpost = (
        district_text if config.checkpost_strategy == "match_district" else ""
    )
    cp = await _select_checkpoint_with_fallback(
        session,
        SEL_CHECKPOINT,
        desired_checkpost,
        log=log,
        name="phase3.select_checkpost",
    )
    if not cp.get("ok"):
        return RunOutcome(
            status="failed",
            summary="Entry Checkpost dropdown had no selectable options.",
            abort_reason="checkpost_not_populated",
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
    # RC data often pre-fills these. Spec: leave anything already filled;
    # set only the empty ones. Service Type's options are conditional on
    # Permit Type, so we set Permit Type first and let Angular react.
    _validity_keywords = ["INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW"]
    _validity_abort = (
        f"Vehicle {params.vehicleNumber} has no valid insurance/fitness/"
        f"PUCC. Please renew before attempting border tax payment."
    )
    _close_sel = "button.swal2-confirm, .modal-footer button, .swal-button--confirm"

    await abort_if_popup_text(
        session,
        _validity_keywords,
        _validity_abort,
        log=log,
        name="phase4.check_validity_on_load",
        close_selector=_close_sel,
    )

    # Permit Type select is the earliest reliable "form ready" signal.
    await wait_for_selector(
        session,
        SEL_PERMIT_TYPE,
        log=log,
        name="phase4.wait_vehicle_info_page",
        timeout=30,
    )

    if is_manual:
        # VAHAN had no data — fill the entire vehicle-info step from the DB
        # record now. The "leave if filled, else first option" blocks below
        # then no-op because every field is already populated. Per-state
        # field-set differences are absorbed by _fill_if_present inside.
        await fill_vehicle_info_manual(
            session,
            params.vehicleDetails,
            params,
            log=log,
            name_prefix="phase4.manual",
            has_category=config.manual_has_category,
            has_distance=config.manual_has_distance,
            datetime_local_dates=config.manual_datetime_local_dates,
        )

    # 4a. Vehicle Category — leave if pre-filled, else first option.
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
                session,
                SEL_VEHICLE_CATEGORY,
                opt["value"],
                log=log,
                name="phase4.select_vehicle_category_first",
            )
            await asyncio.sleep(1.0)
        else:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name="phase4.vehicle_category_empty",
                    status=StepStatus.RETRIED,
                    value="<empty>",
                    error="Vehicle Category empty and no options; proceeding.",
                )
            )

    # 4b. Permit Type — leave if pre-filled, else primary + fallback chain.
    permit_val = await get_select_value(session, SEL_PERMIT_TYPE)
    if not permit_val:
        permit_candidates = [
            params.permitType,
            params.permitTypeFallback,
            "TEMPORARY PERMIT",
        ]
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
            opt = await _first_non_placeholder_option(
                session, SEL_PERMIT_TYPE, timeout=5
            )
            if opt.get("ok"):
                await select_by_value(
                    session,
                    SEL_PERMIT_TYPE,
                    opt["value"],
                    log=log,
                    name="phase4.select_permit_type_first",
                )
                permit_set = True
        if not permit_set:
            return RunOutcome(
                status="failed",
                summary=(
                    f"Could not set Permit Type on {config.state_name} "
                    f"vehicle-info page. Tried {permit_candidates}. Last "
                    f"error: {type(last_err).__name__ if last_err else 'n/a'}: "
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
            close_selector=_close_sel,
        )

    # 4c. Service Type — leave if pre-filled, else desired + first fallback.
    #     (Options only exist once Permit Type is set, handled above.)
    try:
        service_val = await get_select_value(session, SEL_SERVICE_TYPE)
    except Exception:
        service_val = None
    if not service_val:
        try:
            await select_by_text(
                session,
                SEL_SERVICE_TYPE,
                params.serviceType,
                log=log,
                name="phase4.select_service_type",
            )
        except Exception as e:
            opt = await _first_non_placeholder_option(
                session, SEL_SERVICE_TYPE, timeout=5
            )
            if opt.get("ok"):
                await select_by_value(
                    session,
                    SEL_SERVICE_TYPE,
                    opt["value"],
                    log=log,
                    name="phase4.select_service_type_first",
                )
            else:
                return RunOutcome(
                    status="failed",
                    summary=(
                        f"Could not set Service Type on {config.state_name} "
                        f"vehicle-info page (wanted {params.serviceType!r}): "
                        f"{type(e).__name__}: {e}"
                    ),
                    abort_reason="service_type_not_settable",
                    run_log=log.dump(),
                )
        await asyncio.sleep(1.0)
        await abort_if_popup_text(
            session,
            _validity_keywords,
            _validity_abort,
            log=log,
            name="phase4.check_validity_after_service",
            close_selector=_close_sel,
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
        name="phase5.wait_tax_page",
        timeout=30,
    )

    # Diagnostic: record exactly what taxMode reached the runner (so the
    # runlog disambiguates a genuine "mode absent" from an upstream override
    # of the caller's taxMode). The available modes are vehicle-specific —
    # the parivahan portal renders different Tax Mode options per RC — so a
    # requested mode may legitimately not be offered for a given vehicle.
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase5.requested_tax_mode",
            status=StepStatus.OK,
            value=(
                f"requested taxMode={params.taxMode!r}, "
                f"taxFrom={params.taxFrom}, taxUpto={params.taxUpto}"
            ),
        )
    )

    # 5a. Tax Mode. The parivahan portal renders different Tax Mode options
    #     per RC (DAYS=1, QUARTERLY=5, YEARLY=7 when offered), so the
    #     requested mode may legitimately not exist for this vehicle. Match
    #     by <option> value (robust to the leading space in the option text),
    #     re-querying the live <select> for up to 30s to absorb async render.
    mode_ok, available_modes = await _select_tax_mode(
        session,
        params.taxMode,
        log=log,
        timeout=30,
    )
    if not mode_ok:
        offered = ", ".join(available_modes) if available_modes else "none"
        return RunOutcome(
            status="failed",
            summary=(
                f"The portal does not offer {params.taxMode} tax mode for "
                f"vehicle {params.vehicleNumber} at the {config.state_name} "
                f"border. Tax modes available for this RC: {offered}. Retry "
                f"with one of the offered modes."
            ),
            abort_reason="tax_mode_not_offered_for_rc",
            run_log=log.dump(),
        )
    await asyncio.sleep(0.5)

    # 5b. Tax From. The input may be type="date" (value "YYYY-MM-DD", e.g.
    #     Bihar) or type="datetime-local" (value "YYYY-MM-DDTHH:MM", e.g.
    #     HP); `_set_date_like` detects the type and formats accordingly.
    #     Verify it stuck (a value below the field's min is silently
    #     rejected). Re-filling here also re-asserts the value in case the
    #     tax-mode selection reset it.
    # datetime-local Tax From/Upto need a time; use the caller-requested
    # taxTime when provided and still usable (a same-day past time is
    # clamped to now — the portal pins min there), else the current IST
    # time — resolved ONCE and reused for both ends so the span stays an
    # exact 24h multiple (date-type states ignore it inside
    # _set_date_like). See _tax_time.py.
    from ._tax_time import resolve_hhmm
    tax_hhmm = resolve_hhmm(params.taxTime, params.taxFrom)

    tf_actual = await _set_date_like(
        session,
        SEL_TAX_FROM,
        params.taxFrom,
        hhmm=tax_hhmm,
        log=log,
        name="phase5.fill_tax_from",
    )
    if not tf_actual or not tf_actual.startswith(params.taxFrom):
        return RunOutcome(
            status="failed",
            summary=(
                f"Tax From date {params.taxFrom} did not stick in the date "
                f"input (got {tf_actual!r}). Most likely it is before the "
                f"field's min ({config.state_name} rejects past dates)."
            ),
            abort_reason="tax_from_before_min",
            run_log=log.dump(),
        )

    # 5c. Tax Upto — ONLY in DAYS mode. In QUARTERLY / YEARLY the portal
    #     auto-derives (and disables) this field. Same type-aware fill.
    if params.taxMode == "DAYS":
        tu_actual = await _set_date_like(
            session,
            SEL_TAX_UPTO,
            params.taxUpto,
            hhmm=tax_hhmm,
            log=log,
            name="phase5.fill_tax_upto",
        )
        if not tu_actual or not tu_actual.startswith(params.taxUpto):
            return RunOutcome(
                status="failed",
                summary=(
                    f"Tax Upto date {params.taxUpto} did not stick in the "
                    f"date input (got {tu_actual!r}). The Tax Upto min carries "
                    f"today's date, so a same-day value is rejected — taxUpto "
                    f"should be at least the next day."
                ),
                abort_reason="tax_upto_before_min",
                run_log=log.dump(),
            )
    else:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase5.skip_tax_upto",
                status=StepStatus.OK,
                value=f"taxMode={params.taxMode}: Tax Upto auto-derived, not filled",
            )
        )

    await sleep_seconds(1, log=log, name="phase5.wait_before_calc")
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

    # ─── Human handover + background receipt capture ───────────────────
    # We are now on (or seconds from) the Disclaimer page — the captcha +
    # Pay Online step, JUST BEFORE the payment-gateway page. There is no AI
    # for these states: a human solves the captcha, picks a payment method,
    # and pays. We set status=waiting_for_human, start a 15-minute timeout,
    # and poll the page for the receipt every few seconds. The moment the
    # receipt page renders we capture the PDF and finish automatically — we
    # never block on an explicit human "done". poll_qr=False: the human
    # operates the browser directly, so there is no in-app QR to surface.
    return await web_handover_and_capture(
        session,
        log,
        r,
        job_id,
        job_params,
        vehicle_number=params.vehicleNumber,
        config=config.payment_config,
        extract_receipt_fields=config.extract_receipt_fields,
        poll_qr=False,
        timeout_secs=HANDOVER_TIMEOUT_SECS,
    )

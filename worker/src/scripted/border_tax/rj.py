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
2. Different portal stack: checkpost.parivahan.gov.in/checkpost/faces/
   runs a PrimeFaces JSF form (NOT Angular like UP/HR). Payment pages
   are ASP.NET WebForms (ctl00 prefixes). Note: rj.ts referred to this
   host as 'vahan.parivahan.gov.in' but the live portal is actually
   under 'checkpost.parivahan.gov.in'.
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
SEL_SERVICE_WIDGET = "#operation_code"
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
# Note: the Checkpost dropdown also lives on an unstable j_idt id (was
# j_idt551, observed drifted to j_idt114). We handle it differently —
# _select_checkpost_option identifies the right <select> by scanning
# for the stable placeholder text 'checkpost' / 'barrier'. No selector
# constant needed.

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


async def _wait_for_tax_mode_filled(
    session,
    timeout: int = 12,
) -> str:
    """Poll until cmb_payment_mode_input has a real (non-placeholder) value.
    The select is disabled (server fills it) so .value is the only signal —
    its aria-label stays as the placeholder text even after fill, but
    .value reflects the actual model state.

    Returns the value, or '' on timeout.
    """
    expr = """
    (function(){
      var sel = document.querySelector('select#cmb_payment_mode_input');
      if (!sel) return '';
      var v = sel.value || '';
      return (v && v !== '-1' && v !== '0') ? v : '';
    })()
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = await _cdp_eval(session, expr)
        if val and str(val).strip():
            return str(val).strip()
        await asyncio.sleep(0.5)
    return ""


async def _pf_calendar_pick_date(
    session,
    calendar_id: str,
    iso_date: str,
) -> dict:
    """Pick a date in a PrimeFaces Calendar by opening the popup,
    navigating to the right month, and clicking the date cell. This
    fires jQuery UI Datepicker's onSelect callback naturally, which
    PrimeFaces wires to its dateSelect AJAX behavior — the only path
    that properly updates server state and triggers cascading auto-fills
    like Tax Mode and No of Periods.

    calendar_id: PrimeFaces Calendar wrapper id, e.g. 'cal_tax_from'
    iso_date:    'YYYY-MM-DD'
    """
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", iso_date)
    if not m:
        return {"ok": False, "reason": f"bad_date_format: {iso_date}"}
    year, month_1, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    month_0 = month_1 - 1  # jQuery UI uses 0-indexed months

    # 1. Show the datepicker popup
    open_result = await _cdp_eval(
        session,
        f"""
        (function() {{
          if (typeof jQuery === 'undefined') return {{ok: false, reason: 'no_jquery'}};
          var $ = jQuery;
          var input = document.querySelector('input#{calendar_id}_input');
          if (!input) return {{ok: false, reason: 'input_not_found'}};
          try {{
            $(input).datepicker('show');
            return {{ok: true}};
          }} catch (e) {{
            return {{ok: false, reason: 'show_threw: ' + (e && e.message)}};
          }}
        }})()
        """,
    )
    if not open_result or not open_result.get("ok"):
        return open_result or {"ok": False, "reason": "open_eval_failed"}

    # 2. Wait for the popup to render
    await asyncio.sleep(0.4)

    # 3. Navigate + click target day
    return await _cdp_eval(
        session,
        f"""
        (function() {{
          var $ = jQuery;
          var $dp = $('#ui-datepicker-div');
          if ($dp.length === 0 || !$dp.is(':visible')) {{
            return {{ok: false, reason: 'datepicker_not_visible'}};
          }}

          var targetYear = {year}, targetMonth = {month_0}, targetDay = {day};
          var monthNames = ['January','February','March','April','May','June',
                            'July','August','September','October','November','December'];

          function readCurrent() {{
            var $title = $dp.find('.ui-datepicker-title');
            var t = ($title.text() || '').trim();
            var cm = null, cy = null;
            for (var i = 0; i < monthNames.length; i++) {{
              if (t.indexOf(monthNames[i]) >= 0) {{ cm = i; break; }}
            }}
            var ym = t.match(/\\d{{4}}/);
            if (ym) cy = parseInt(ym[0], 10);
            return {{m: cm, y: cy, title: t}};
          }}

          // Navigate (max 24 clicks = 2 years either direction)
          for (var step = 0; step < 24; step++) {{
            var cur = readCurrent();
            if (cur.m === null || cur.y === null) {{
              return {{ok: false, reason: 'cant_read_title', title: cur.title}};
            }}
            if (cur.m === targetMonth && cur.y === targetYear) break;
            var forward = (cur.y < targetYear) ||
                          (cur.y === targetYear && cur.m < targetMonth);
            var $nav = forward ? $dp.find('.ui-datepicker-next')
                               : $dp.find('.ui-datepicker-prev');
            if ($nav.length === 0 || $nav.hasClass('ui-state-disabled')) {{
              return {{ok: false, reason: 'nav_blocked',
                       direction: forward ? 'next' : 'prev',
                       currentTitle: cur.title}};
            }}
            $nav[0].click();
          }}

          // Find and click the day cell
          var $days = $dp.find('td[data-handler="selectDay"] a');
          for (var d = 0; d < $days.length; d++) {{
            if (($days.eq(d).text() || '').trim() === String(targetDay)) {{
              $days[d].click();
              return {{ok: true, day: targetDay, month: targetMonth, year: targetYear}};
            }}
          }}
          return {{ok: false, reason: 'day_not_in_calendar', day: targetDay}};
        }})()
        """,
    ) or {"ok": False, "reason": "pick_eval_failed"}


async def _pf_select_service_option(session) -> dict:
    """Select 'VEHICLE TAX COLLECTION (OTHER STATE)' by simulating a real
    user click sequence on the PrimeFaces SelectOneMenu:

        1. Click the dropdown trigger (this opens the panel and lazily
           renders the <li> items into #operation_code_panel).
        2. Wait for the panel items to be present.
        3. Click the <li> that contains the target text.

    This lets PrimeFaces handle ALL its own state sync (widget.selectedOption,
    aria-activedescendant, label text, hidden <select>.value, etc.) — the
    same way it does when a human clicks the dropdown. Patching the state
    directly via JS races and misses internal book-keeping.
    """
    TARGET_TEXT = "VEHICLE TAX COLLECTION (OTHER STATE)"

    # Step 1: click the trigger
    open_result = await _cdp_eval(
        session,
        """
        (function(){
          var wrapper = document.getElementById('operation_code');
          if (!wrapper) return {ok: false, reason: 'wrapper_not_found'};
          var trigger = wrapper.querySelector('.ui-selectonemenu-trigger');
          if (!trigger) return {ok: false, reason: 'trigger_not_found'};
          // PrimeFaces binds on mousedown for the trigger, not click —
          // fire both to be safe.
          var r = trigger.getBoundingClientRect();
          var opts = {bubbles: true, cancelable: true,
                      clientX: r.left + r.width/2,
                      clientY: r.top + r.height/2,
                      button: 0};
          trigger.dispatchEvent(new MouseEvent('mousedown', opts));
          trigger.dispatchEvent(new MouseEvent('mouseup',   opts));
          trigger.click();
          return {ok: true};
        })()
        """,
    )
    if not open_result or not open_result.get("ok"):
        return open_result or {"ok": False, "reason": "open_eval_failed"}

    # Step 2: poll for the panel items to render (lazy)
    items_ready = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        count = await _cdp_eval(
            session,
            """
            (function(){
              var panel = document.getElementById('operation_code_panel');
              if (!panel) return 0;
              var items = panel.querySelectorAll('li');
              return items.length;
            })()
            """,
        )
        if count and int(count) > 0:
            items_ready = True
            break
        await asyncio.sleep(0.2)

    if not items_ready:
        return {"ok": False, "reason": "panel_items_never_rendered"}

    # Step 3: click the <li> with the target text
    click_result = await _cdp_eval(
        session,
        """
        (function(target){
          var panel = document.getElementById('operation_code_panel');
          if (!panel) return {ok: false, reason: 'panel_gone'};
          var items = panel.querySelectorAll('li');
          var upper = target.toUpperCase();
          for (var i = 0; i < items.length; i++){
            var t = (items[i].textContent || '').trim();
            if (t.toUpperCase().indexOf(upper) >= 0){
              var r = items[i].getBoundingClientRect();
              var opts = {bubbles: true, cancelable: true,
                          clientX: r.left + r.width/2,
                          clientY: r.top + r.height/2,
                          button: 0};
              items[i].dispatchEvent(new MouseEvent('mousedown', opts));
              items[i].dispatchEvent(new MouseEvent('mouseup',   opts));
              items[i].click();
              return {ok: true, method: 'click', itemText: t,
                      itemIndex: i, itemId: items[i].id || ''};
            }
          }
          var seen = [];
          for (var j = 0; j < items.length; j++){
            seen.push((items[j].textContent || '').trim());
          }
          return {ok: false, reason: 'item_not_found', itemsSeen: seen};
        })("""
        + json.dumps(TARGET_TEXT)
        + """)
        """,
    )

    if not click_result or not click_result.get("ok"):
        return click_result or {"ok": False, "reason": "click_eval_failed"}

    # Verify the selection actually stuck — read the underlying <select>
    verify = await _cdp_eval(
        session,
        """
        (function(){
          var sel = document.querySelector('select#operation_code_input');
          if (!sel) return {value: null};
          return {
            value: sel.value,
            selectedIndex: sel.selectedIndex,
            labelText: (document.getElementById('operation_code_label')
                         || {}).textContent || ''
          };
        })()
        """,
    )
    click_result["verify"] = verify
    if not verify or verify.get("value") != "5003":
        click_result["ok"] = False
        click_result["reason"] = (
            f"selection_didnt_stick: select.value={verify.get('value') if verify else None}"
        )

    return click_result


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


async def _select_checkpost_option(
    session,
    target_text: str | None,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 15,
) -> str | None:
    """Select an option in the Check Post Name Through Entering dropdown.

    Handles both modes:
      - target_text is None  -> pick the first non-placeholder option
                                (auto-pick fallback for when no
                                 entryCheckpoint param was provided).
      - target_text is a str -> pick the option whose text matches
                                (exact / ci-equal / ci-contains).

    Identifies the checkpost <select> via its placeholder text — the
    placeholder '---Select CheckpostName/Barrier---' is stable across
    portal renders, while the PrimeFaces j_idt* id of the wrapping
    element drifts between deploys (we've now confirmed it has moved
    from j_idt551 -> j_idt114 on at least one run).

    Polls every 500ms for up to `timeout` seconds. The District ->
    Checkpost AJAX cascade can take a few seconds, so the dropdown may
    have only the placeholder when we first query it; we keep retrying
    until either a matching option appears or the timeout fires.

    EARLIER BUG (now fixed): a previous version identified the checkpost
    select by scanning all <select>s for any option whose text contained
    the district name (e.g. 'ALWAR'). That false-matched the District
    dropdown itself (which contains ALWAR among its options), so the
    helper "succeeded" by re-selecting the district -- silently leaving
    the actual Checkpost field empty. Placeholder-based identification
    avoids that whole class of false match.

    Returns the option text that was selected, or None on timeout.
    Logs option-list dumps on failure for debuggability.
    """
    started = time.monotonic()
    target_json = json.dumps(target_text)  # python None -> JS null
    expr = (
        "(function(target){"
        "function isCheckpost(e){"
        "  if(!(e instanceof HTMLSelectElement)) return false;"
        "  for(var i=0;i<e.options.length;i++){"
        "    var t=(e.options[i].text||'').toLowerCase();"
        "    if(t.indexOf('checkpost')>=0||t.indexOf('barrier')>=0){return true;}"
        "  }"
        "  return false;"
        "}"
        "function pickMatching(e, t){"
        "  if(!(e instanceof HTMLSelectElement)) return null;"
        "  var tLower = t ? t.toLowerCase() : null;"
        "  for(var i=0;i<e.options.length;i++){"
        "    var opt=e.options[i];"
        "    var v=(opt.value||'').trim();"
        "    if(!v || v==='-1' || v==='0') continue;"
        "    var oText=(opt.text||'').trim();"
        "    if(t===null){"
        "      e.value=opt.value;"
        "      e.dispatchEvent(new Event('input',{bubbles:true}));"
        "      e.dispatchEvent(new Event('change',{bubbles:true}));"
        "      return oText;"
        "    }"
        "    var oLower=oText.toLowerCase();"
        "    if(oText===t || oLower===tLower || oLower.indexOf(tLower)>=0){"
        "      e.value=opt.value;"
        "      e.dispatchEvent(new Event('input',{bubbles:true}));"
        "      e.dispatchEvent(new Event('change',{bubbles:true}));"
        "      return oText;"
        "    }"
        "  }"
        "  return null;"
        "}"
        "var sels = document.querySelectorAll('select');"
        "var checkpostSeen = [];"
        "for (var si=0; si<sels.length; si++){"
        "  var e = sels[si];"
        "  if (!isCheckpost(e)) continue;"
        "  var opts = [];"
        "  for (var i=0; i<e.options.length; i++){"
        "    opts.push((e.options[i].text||'').trim());"
        "  }"
        "  checkpostSeen.push({id:e.id||'', options:opts});"
        "  var picked = pickMatching(e, target);"
        "  if (picked) return {ok:true, text:picked, elementId:e.id||''};"
        "}"
        "return {ok:false, checkpostSelects:checkpostSeen};"
        "})(" + target_json + ")"
    )
    deadline = time.monotonic() + timeout
    last_dump = None
    while time.monotonic() < deadline:
        result = await _cdp_eval(session, expr)
        if result and result.get("ok"):
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    value=(f"{result.get('text')} (el=#{result.get('elementId')})"),
                )
            )
            return result.get("text")
        if result:
            last_dump = result.get("checkpostSelects")
        await asyncio.sleep(0.5)

    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=(target_text or "<first non-placeholder>"),
            error=(
                f"checkpost option not found within {timeout}s; "
                f"checkpost_selects_seen={last_dump}"
            ),
        )
    )
    return None


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


async def _wait_for_checkpost_options(
    session,
    timeout: int = 15,
) -> int:
    """Poll until the Check Post Name dropdown has at least one
    non-placeholder option. Returns the count, or 0 on timeout.

    The checkpost dropdown is populated by a PrimeFaces.ab cascade
    fired from District's onchange. On a loaded portal this can take
    2–5s, so the old `await asyncio.sleep(1.5)` is sometimes a race.
    """
    expr = """
    (function(){
      var labels = document.querySelectorAll('span.ui-outputlabel-label');
      for (var i = 0; i < labels.length; i++){
        var text = (labels[i].textContent || '').trim().toLowerCase();
        if (text.indexOf('check post name through entering') < 0) continue;
        var col = labels[i].closest('div.ui-grid-col-6, div.ui-grid-col-12');
        if (!col) continue;
        var sel = col.querySelector('select');
        if (!sel) continue;
        var count = 0;
        for (var j = 0; j < sel.options.length; j++){
          var v = (sel.options[j].value || '').trim();
          if (v && v !== '-1' && v !== '0') count++;
        }
        return count;
      }
      return 0;
    })()
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        count = await _cdp_eval(session, expr)
        if count and int(count) > 0:
            return int(count)
        await asyncio.sleep(0.5)
    return 0


async def _pf_select_first_checkpoint_by_label(session) -> dict:
    """Find the Check Post Name Through Entering dropdown by its LABEL
    (not by id, not by option-text matching), then pick the first
    non-placeholder option. Updates the underlying <select> AND the
    PrimeFaces visible widget state (label text, aria-activedescendant,
    aria-disabled, aria-owns) AND fires change so PrimeFaces.ab cascade
    runs to populate Tax Mode + No of Periods.
    """
    return await _cdp_eval(
        session,
        """
        (function(){
          // 1) Locate the checkpost dropdown by its label text.
          var labels = document.querySelectorAll('span.ui-outputlabel-label');
          var sel = null, wrapper = null, lbl = null;
          for (var i = 0; i < labels.length; i++){
            var t = (labels[i].textContent || '').trim().toLowerCase();
            if (t.indexOf('check post name through entering') < 0) continue;
            var col = labels[i].closest('div.ui-grid-col-6, div.ui-grid-col-12');
            if (!col) continue;
            sel     = col.querySelector('select');
            wrapper = col.querySelector('div.ui-selectonemenu');
            lbl     = col.querySelector('span.ui-selectonemenu-label');
            if (sel) break;
          }
          if (!sel) return {ok: false, reason: 'checkpost_select_not_found'};

          // 2) First non-placeholder option.
          var target = null, targetIdx = -1;
          for (var j = 0; j < sel.options.length; j++){
            var v = (sel.options[j].value || '').trim();
            if (v && v !== '-1' && v !== '0'){
              target = sel.options[j]; targetIdx = j; break;
            }
          }
          if (!target){
            return {ok: false, reason: 'no_non_placeholder_option',
                    optionCount: sel.options.length};
          }

          // 3) Try the PrimeFaces widget API first.
          if (wrapper && window.PrimeFaces && PrimeFaces.widgets){
            try {
              for (var key in PrimeFaces.widgets){
                var w = PrimeFaces.widgets[key];
                if (w && w.jq && w.jq[0] === wrapper
                    && typeof w.selectValue === 'function'){
                  w.selectValue(target.value);
                  return {ok: true, method: 'widget',
                          value: target.value, text: target.text};
                }
              }
            } catch (e) { /* fall through */ }
          }

          // 4) Manual sync — same pattern that fixed Phase 2.
          var proto = window.HTMLSelectElement.prototype;
          var d = Object.getOwnPropertyDescriptor(proto, 'value');
          if (d && d.set) { d.set.call(sel, target.value); }
          else            { sel.value = target.value; }
          sel.selectedIndex = targetIdx;
          for (var k = 0; k < sel.options.length; k++){
            sel.options[k].selected = (k === targetIdx);
          }
          if (lbl){
            lbl.textContent = target.text;
            if (wrapper && wrapper.id){
              lbl.setAttribute('aria-activedescendant',
                               wrapper.id + '_' + targetIdx);
            }
            lbl.setAttribute('aria-disabled', 'false');
            lbl.classList.remove('ui-state-disabled');
          }
          if (wrapper && wrapper.id){
            wrapper.setAttribute('aria-owns', wrapper.id + '_items');
          }

          // 5) Fire change so the inline onchange="PrimeFaces.ab(...)"
          //    on this <select> runs and updates the rjtaxcollection
          //    panel (which is what fills Tax Mode + No of Periods).
          sel.dispatchEvent(new Event('input',  {bubbles: true}));
          sel.dispatchEvent(new Event('change', {bubbles: true}));

          return {ok: true, method: 'manual',
                  value: target.value, text: target.text,
                  index: targetIdx,
                  wrapperId: wrapper ? wrapper.id : null};
        })()
        """,
    )


# ── PrimeFaces-aware select helpers ────────────────────────────────────
#
# RJ's mega-form is rendered by PrimeFaces JSF. Every native <select> is
# wrapped inside <div class="ui-helper-hidden-accessible"> which applies
# clip/zero-size styling for accessibility -- semantically the element
# is present and screen-reader-readable, but it has no bounding rect.
#
# The standard select_by_text / select_by_value primitives in steps.py
# call _wait_visible first, which rejects zero-size elements and times
# out. We don't actually need the select to be "visible" -- we set
# .value via JS and dispatch change, neither of which requires layout.
# So we provide PrimeFaces-flavored variants here that just poll for
# DOM presence + iterate options.
#
# Iteration semantics match the originals: querySelectorAll(selector)
# returns ALL matches, we iterate them in document order, and pick the
# first <select> whose options contain the target. Passing a broad
# fallback like 'select' lets us survive PrimeFaces j_idt id drift.


async def _pf_select_by_text(
    session,
    selector: str,
    text: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 15,
) -> None:
    """PrimeFaces-aware select_by_text. Poll for the option (it may be
    populated by an AJAX response, e.g. Checkpost options after District
    is set). Match order: exact -> ci-equal -> ci-contains.
    """
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    expr = (
        "(function(s, t){"
        "var els = document.querySelectorAll(s);"
        "var tLower = (t || '').toLowerCase();"
        "var dump = [];"
        "for (var ei = 0; ei < els.length; ei++) {"
        "  var e = els[ei];"
        "  if (!(e instanceof HTMLSelectElement)) continue;"
        "  var texts = [];"
        "  for (var oi = 0; oi < e.options.length; oi++) {"
        "    texts.push((e.options[oi].text || '').trim());"
        "  }"
        "  dump.push({idx: ei, id: e.id || '', optionTexts: texts});"
        "  for (var i = 0; i < e.options.length; i++) {"
        "    var oText = (e.options[i].text || '').trim();"
        "    var oLower = oText.toLowerCase();"
        "    if (oText === t || oLower === tLower || oLower.indexOf(tLower) >= 0) {"
        "      e.value = e.options[i].value;"
        "      e.dispatchEvent(new Event('input', {bubbles: true}));"
        "      e.dispatchEvent(new Event('change', {bubbles: true}));"
        "      return {ok: true, selectedText: oText, selectedValue: e.options[i].value, elementId: e.id || ''};"
        "    }"
        "  }"
        "}"
        "return {ok: false, elements: dump};"
        "})(" + json.dumps(selector) + "," + json.dumps(text) + ")"
    )
    last_dump = None
    while True:
        result = await _cdp_eval(session, expr)
        if result and result.get("ok"):
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    value=(
                        f"{result.get('selectedText')} "
                        f"(value={result.get('selectedValue')}, "
                        f"el=#{result.get('elementId')})"
                    ),
                )
            )
            return
        if result:
            last_dump = result.get("elements")
        if time.monotonic() > deadline:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    value=text,
                    error=(
                        f"option {text!r} not found in any select matching "
                        f"{selector!r} after {timeout}s; options_seen={last_dump}"
                    ),
                )
            )
            raise RuntimeError(
                f"_pf_select_by_text: option {text!r} not found in any "
                f"select matching {selector!r} after {timeout}s. "
                f"Available: {last_dump}"
            )
        await asyncio.sleep(0.5)


async def _pf_select_by_value(
    session,
    selector: str,
    value: str,
    *,
    log: StepLogger,
    name: str,
    timeout: int = 15,
) -> None:
    """PrimeFaces-aware select_by_value. See _pf_select_by_text for
    rationale on bypassing the visibility check."""
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    expr = (
        "(function(s, v){"
        "var els = document.querySelectorAll(s);"
        "var dump = [];"
        "for (var ei = 0; ei < els.length; ei++) {"
        "  var e = els[ei];"
        "  if (!(e instanceof HTMLSelectElement)) continue;"
        "  var values = [];"
        "  for (var oi = 0; oi < e.options.length; oi++) {"
        "    values.push(e.options[oi].value);"
        "  }"
        "  dump.push({idx: ei, id: e.id || '', optionValues: values});"
        "  for (var i = 0; i < e.options.length; i++) {"
        "    if (e.options[i].value === v) {"
        "      e.value = v;"
        "      e.dispatchEvent(new Event('input', {bubbles: true}));"
        "      e.dispatchEvent(new Event('change', {bubbles: true}));"
        "      return {ok: true, selectedText: (e.options[i].text || '').trim(), elementId: e.id || ''};"
        "    }"
        "  }"
        "}"
        "return {ok: false, elements: dump};"
        "})(" + json.dumps(selector) + "," + json.dumps(value) + ")"
    )
    last_dump = None
    while True:
        result = await _cdp_eval(session, expr)
        if result and result.get("ok"):
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.OK,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    value=(
                        f"{result.get('selectedText')} "
                        f"(value={value}, el=#{result.get('elementId')})"
                    ),
                )
            )
            return
        if result:
            last_dump = result.get("elements")
        if time.monotonic() > deadline:
            log.record(
                StepLog(
                    index=log.next_index(),
                    name=name,
                    status=StepStatus.FAILED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    selector=selector,
                    value=value,
                    error=(
                        f"value {value!r} not found in any select matching "
                        f"{selector!r} after {timeout}s; values_seen={last_dump}"
                    ),
                )
            )
            raise RuntimeError(
                f"_pf_select_by_value: value {value!r} not found in any "
                f"select matching {selector!r} after {timeout}s"
            )
        await asyncio.sleep(0.5)


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
    # RJ navigates to checkpost.parivahan.gov.in/checkpost/faces/.../TaxCollection.xhtml
    # (DIFFERENT host than UP/HR's services.parivahan.gov.in/checkpostv4).
    # We match on 'checkpost.parivahan.gov.in/checkpost' which is unique
    # to the destination -- the source URL (parivahan.gov.in/en/node/579)
    # does not contain '/checkpost'.
    await wait_for_url(
        session,
        "checkpost.parivahan.gov.in/checkpost",
        log=log,
        name="phase1.wait_checkpost_landing",
        timeout=45,
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase1.settle")

    # ─── Phase 2: service selection + Go ───────────────────────────────
    await wait_for_selector(
        session,
        SEL_SERVICE_WIDGET,
        # SEL_SERVICE_DROPDOWN,
        log=log,
        name="phase2.wait_service_widget",
        timeout=30,
    )

    sel_started = time.monotonic()
    result = await _pf_select_service_option(session)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase2.select_service",
            status=StepStatus.OK
            if (result and result.get("ok"))
            else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - sel_started) * 1000),
            selector=SEL_SERVICE_DROPDOWN,
            value=str(result)[:200],
            error=None
            if (result and result.get("ok"))
            else (result.get("reason") if result else "no_result"),
        )
    )
    if not result or not result.get("ok"):
        return RunOutcome(
            status="failed",
            summary=(
                f"RJ Phase 2: could not select VEHICLE TAX COLLECTION "
                f"(OTHER STATE) — {result}"
            ),
            abort_reason="phase2_service_select_failed",
            run_log=log.dump(),
        )

    # Let PrimeFaces register the selection internally before we trigger
    # the AJAX. Without this beat, some PF builds reject the submit.
    await asyncio.sleep(1.5)

    await click_by_text(
        session,
        "Go",
        log=log,
        name="phase2.click_go",
        tag="button",
    )

    # PrimeFaces.ab does an AJAX panel update — URL may not change.
    # Wait on the mega-form's vehicle input as the real readiness signal.
    megaform_ready = False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready = await _cdp_eval(
            session,
            """
            (function(){
              if (document.querySelector('input#j_idt57')) return true;
              var fb = document.querySelectorAll(
                "input[type='text'][maxlength='10'][onkeyup*='makeCaps']"
              );
              return fb.length > 0;
            })()
            """,
        )
        if ready:
            megaform_ready = True
            break
        await asyncio.sleep(0.5)

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase2.wait_megaform",
            status=StepStatus.OK if megaform_ready else StepStatus.FAILED,
        )
    )
    if not megaform_ready:
        return RunOutcome(
            status="failed",
            summary=(
                "RJ Phase 2: clicked Go but the mega-form (Vehicle No input) "
                "did not appear within 30s. PrimeFaces AJAX likely never fired "
                "or the dropdown selection didn't stick."
            ),
            abort_reason="phase2_megaform_not_appeared",
            run_log=log.dump(),
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
                await _pf_select_by_text(
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
        await _pf_select_by_text(
            session,
            SEL_DISTRICT,
            params.entryDistrict,
            log=log,
            name="phase3.select_district[primary]",
            timeout=10,
        )
    except Exception:
        # PrimeFaces j_idt drifted -- broad scan via _pf_select_by_text
        # (iterates all matches and picks first with the target option).
        await _pf_select_by_text(
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

    # 3g. Check Post Name Through Entering — always auto-pick first option.
    # (params.entryCheckpoint is intentionally ignored per the new spec:
    #  District comes from params, Checkpost is whatever's first under it.)
    #
    # First wait for the cascade from District to actually populate the
    # checkpost options. The old `await asyncio.sleep(1.5)` above was racy
    # on slow days.
    opt_count = await _wait_for_checkpost_options(session, timeout=15)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.wait_checkpost_options",
            status=StepStatus.OK if opt_count > 0 else StepStatus.FAILED,
            value=f"{opt_count} options available",
        )
    )
    if opt_count == 0:
        return RunOutcome(
            status="failed",
            summary=(
                f"Checkpost options never populated after selecting "
                f"district {params.entryDistrict!r}. The District -> "
                f"Checkpost PrimeFaces cascade did not fire or returned "
                f"empty."
            ),
            abort_reason="checkpost_options_not_populated",
            run_log=log.dump(),
        )

    cp_started = time.monotonic()
    cp_result = await _pf_select_first_checkpoint_by_label(session)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.select_checkpoint_auto",
            status=StepStatus.OK
            if (cp_result and cp_result.get("ok"))
            else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - cp_started) * 1000),
            value=str(cp_result)[:200],
            error=None
            if (cp_result and cp_result.get("ok"))
            else (cp_result.get("reason") if cp_result else "no_result"),
        )
    )
    if not cp_result or not cp_result.get("ok"):
        return RunOutcome(
            status="failed",
            summary=(
                f"Could not auto-pick a Check Post Name for district "
                f"{params.entryDistrict!r} — {cp_result}"
            ),
            abort_reason="checkpoint_auto_pick_failed",
            run_log=log.dump(),
        )

    # The checkpost's onchange fires PrimeFaces.ab(p:'rjtaxcollection',
    # u:'rjtaxcollection') which re-renders the panel and populates Tax
    # Mode + No of Periods. Give it a beat before we type dates.
    await sleep_seconds(2.0, log=log, name="phase3.wait_checkpost_cascade")

    # 3h. Tax From / Tax Upto -- pick via calendar UI, NOT fill().
    # See _pf_calendar_pick_date docstring: only the calendar UI click
    # fires jQuery UI Datepicker's onSelect, which is what PrimeFaces
    # wires to the dateSelect AJAX that triggers Tax Mode auto-fill.

    pick_from_started = time.monotonic()
    pick_from = await _pf_calendar_pick_date(
        session,
        "cal_tax_from",
        params.taxFrom,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.pick_tax_from",
            status=StepStatus.OK
            if (pick_from and pick_from.get("ok"))
            else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - pick_from_started) * 1000),
            value=str(pick_from)[:200],
            error=None
            if (pick_from and pick_from.get("ok"))
            else (pick_from.get("reason") if pick_from else "no_result"),
        )
    )
    if not pick_from or not pick_from.get("ok"):
        return RunOutcome(
            status="failed",
            summary=f"Tax From calendar pick failed: {pick_from}",
            abort_reason="tax_from_pick_failed",
            run_log=log.dump(),
        )

    # The first dateSelect re-renders rjtaxcollection. Wait for the
    # cal_tax_to input to be present in the new DOM before targeting it.
    await asyncio.sleep(1.5)

    pick_to_started = time.monotonic()
    pick_to = await _pf_calendar_pick_date(
        session,
        "cal_tax_to",
        params.taxUpto,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.pick_tax_upto",
            status=StepStatus.OK
            if (pick_to and pick_to.get("ok"))
            else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - pick_to_started) * 1000),
            value=str(pick_to)[:200],
            error=None
            if (pick_to and pick_to.get("ok"))
            else (pick_to.get("reason") if pick_to else "no_result"),
        )
    )
    if not pick_to or not pick_to.get("ok"):
        return RunOutcome(
            status="failed",
            summary=f"Tax Upto calendar pick failed: {pick_to}",
            abort_reason="tax_upto_pick_failed",
            run_log=log.dump(),
        )

    await asyncio.sleep(1.5)

    # Tax Mode + No of Periods should now be auto-filled by the server-side
    # cascade triggered from the dateSelect AJAX on cal_tax_to.
    tax_mode_val = await _wait_for_tax_mode_filled(session, timeout=10)
    log.record(
        StepLog(
            index=log.next_index(),
            name="phase3.wait_tax_mode_autofill",
            status=StepStatus.OK if tax_mode_val else StepStatus.RETRIED,
            value=tax_mode_val or "<empty>",
        )
    )

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

    # Select E-GRAS in the gateway dropdown. _pf_select_by_value scans all
    # <select> elements and picks the first one with value="EGRAS" -- the
    # unstable j_idt id of the dropdown is irrelevant, and the underlying
    # select is PrimeFaces-hidden so we bypass the visibility check.
    await _pf_select_by_value(
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

# worker/src/scripted/border_tax/_pending_clear.py
"""Auto-clear "pending transaction" blocker on the parivahan checkpostv4 portal.

When phase 3 of a border-tax flow clicks "Get Details" for a vehicle that
already has an in-flight tax transaction at the portal, the page shows a
SweetAlert2 popup:

    "Your transaction with this registration no is pending for verification.
     Please clear this first using 'Check Pending Transaction' link from
     'Border Tax Payment' menu on home screen."

With the popup up, the Owner Information form never finishes loading — the
district <select> stays absent and the scripted runner used to die with
TimeoutError: selector not visible within 30s: select#floatingDistrict.

This module:
  - Detects that popup as one of the outcomes of phase 3's
    `click Get Details` (alongside district-ready / validity-popup / timeout).
  - When the popup is detected AND SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING is
    truthy, walks the user-documented recovery flow:
        1. Dismiss the popup.
        2. Navigate home (https://services.parivahan.gov.in/checkpostv4/#/).
           (Direct nav to ChecklTransactionStatus gets rewritten to "/" by
           the Angular router — we have to click the dropdown link instead.)
        3. Click the "Check Pending Transaction" routerLink anchor inside
           the "Border Tax Payment" dropdown in the page header.
        4. Fill the vehicle number on the Check Transaction Status page.
        5. Solve the canvas captcha (reuses scripted.captcha.solve_canvas_captcha
           — direct Gemini-on-Vertex LLM OCR, no AI handoff agent).
        6. Click "Go". Poll for either:
              - the results table populating (captcha accepted), or
              - the "Captcha mismatch" SweetAlert2 popup (captcha rejected;
                solve_canvas_captcha auto-refreshes and retries).
        7. Click the bank/university icon in the first row.
        8. Poll for either outcome:
              - "Please try after X Minutes Y Seconds" SweetAlert2 → portal
                still has the transaction locked; we can NOT clear it yet.
                Return ("still_on_hold", "<X Minutes Y Seconds>").
              - URL contains /bank/DoubleVerification/ OR body text contains
                "transaction is fail" → portal accepted the clear. Return
                ("cleared", "").

  The caller (each state's phase 3) then:
    - on "still_on_hold" → RunOutcome failed with abort_reason
      "pending_transaction_still_on_hold", includes the time hint.
    - on "cleared" → re-runs phases 1 + 2 + fill vehicle + Get Details. Direct
      nav to /taxCollectionOnline is rewritten to "/" by the router (same
      reason we use the dropdown link above), so re-doing the full state
      selection from parivahan.gov.in/en/node/579 is the only working path.
    - on "failed" → RunOutcome failed with abort_reason "pending_clear_failed",
      includes the reason hint.

  Heavy `print("[pending_clear] …")` logging at every branch so we can grep
  docker logs while we burn this in.

Env toggle:
  SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING=true   (default: false)

  When false: phase 3 detects the popup the same way but bails immediately
  with abort_reason="pending_transaction_popup" — matches the pre-feature
  behavior so existing alerting / on-call runbooks keep working.

Scope:
  Used by UP / HR / PB / MP. RJ has a completely different portal surface
  (egras direct flow) and does NOT call this helper.

Selectors come from the live HTML in the user-shared screenshots (May 2026
portal version, "CheckPost V4.7.2"). All are state-agnostic — they hang off
the checkpostv4 SPA shell, not the per-state owner-info template.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Literal

import redis

from ..captcha import solve_canvas_captcha
from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    click_by_text,
    fill,
    navigate,
    select_by_text,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from ..types import ScriptedAbort, StepLog, StepStatus


# ─── Selectors ─────────────────────────────────────────────────────────

# Portal home — the only URL the Angular router lets us load fresh.
# Both /public/payment/taxCollectionOnline and
# /public/payment/ChecklTransactionStatus get rewritten to "/" if we hit
# them as the first navigation. That's why we ALWAYS land here first and
# then click through the dropdown.
URL_PORTAL_HOME = "https://services.parivahan.gov.in/checkpostv4/#/"

# The routerLink anchor inside the "Border Tax Payment" header dropdown.
# Identified by its href fragment so we're robust to Angular re-renders
# that shuffle classes/ids around the link.
SEL_PENDING_TX_LINK = 'a[href="#/public/payment/ChecklTransactionStatus"]'

# Check Pending Transaction page controls. The canvas captcha + input
# selectors are identical to the fetch_receipt flow's
# /public/reports/PaymentReceipt page — same template, different route.
SEL_PENDING_VEHICLE_INPUT = "input#inputVehicleNo"
SEL_PENDING_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_PENDING_CAPTCHA_INPUT = "input#inputcaptcha"
# Refresh is a SIBLING of div#captcha (NOT a child). The adjacent-sibling
# combinator is the primary form; the className fallback covers minor
# template tweaks.
SEL_PENDING_CAPTCHA_REFRESH = "div#captcha + button, button.btn-primary.m-left"
SEL_PENDING_GO_BTN = "button.go-but"

# Owner-info-page outcome detection (used by wait_for_owner_info_outcome).
# All four scripted states (UP/HR/PB/MP) use the same selector for the
# district dropdown — Angular template is shared across them.
SEL_DISTRICT = "select#floatingDistrict"


# ─── Tunables ──────────────────────────────────────────────────────────

# How long to wait after Get Details for ONE of: district visible /
# pending popup / validity popup. 30s matches the old wait_for_selector
# budget so we don't change worst-case latency on the happy path.
OWNER_OUTCOME_POLL_SECS = 30.0
OWNER_OUTCOME_POLL_TICK_SECS = 0.5

# How long to wait after clicking Go for the captcha submit to land
# (results row OR captcha-mismatch popup). Mirrors fetch_receipt.
GO_SUBMIT_POLL_SECS = 10.0

# How long to wait after clicking the bank icon for either outcome
# popup (try-after) or the recovery navigation/text.
BANK_OUTCOME_POLL_SECS = 12.0
BANK_OUTCOME_POLL_TICK_SECS = 0.5

# Max captcha attempts before giving up. The solver auto-refreshes the
# canvas between attempts via SEL_PENDING_CAPTCHA_REFRESH.
MAX_CAPTCHA_ATTEMPTS = 5

# Navigation timeouts inside the clear flow.
LINK_WAIT_TIMEOUT_SECS = 20
PAGE_LOAD_TIMEOUT_SECS = 20


# ─── Env toggle ────────────────────────────────────────────────────────


def auto_clear_enabled() -> bool:
    """Read SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING from env.

    Truthy values: '1', 'true', 'yes', 'on' (case-insensitive).
    Everything else (including unset) is false. The state runner consults
    this BEFORE entering the clear flow — when false, the pending-popup
    branch returns RunOutcome failed immediately with abort_reason
    'pending_transaction_popup'.
    """
    raw = os.environ.get("SCRIPTED_BORDER_TAX_AUTO_CLEAR_PENDING", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ─── JS expressions ────────────────────────────────────────────────────


# Inspect the owner-info page right after "Get Details" was clicked.
# Priority order matters — if a blocking popup is up, the district select
# may never render, so we check popups FIRST.
#
# Returns one of:
#   {state: "pending_popup"}            initial popup from a stale pending tx
#   {state: "validity_popup", text:""}  insurance/fitness/pucc/expired/renew
#   {state: "district_ready"}           district <select> visible
#   {state: "pending"}                  nothing decisive yet (keep polling)
_OWNER_INFO_OUTCOME_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var text = (popups[i].textContent || '');
    var lower = text.toLowerCase();
    if (lower.indexOf('pending for verification') !== -1
        || (lower.indexOf('check pending transaction') !== -1
            && lower.indexOf('pending') !== -1)) {
      return {state: 'pending_popup', text: text.substring(0, 240)};
    }
    if (/(insurance|fitness|pucc|expired|renew|not\\s*valid)/i.test(text)) {
      return {state: 'validity_popup', text: text.substring(0, 240)};
    }
  }
  var district = document.querySelector('select#floatingDistrict');
  if (district) {
    var rect = district.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      return {state: 'district_ready'};
    }
  }
  return {state: 'pending'};
})()
"""


# Dismiss any visible SweetAlert2 popup by clicking its confirm button.
# Returns {dismissed: bool, reason?: string}.
_DISMISS_SWAL_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  var clicked = 0;
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var btn = popups[i].querySelector('button.swal2-confirm');
    if (!btn) continue;
    try { btn.click(); clicked++; } catch (e) {}
  }
  return {dismissed: clicked > 0, count: clicked};
})()
"""


# Classify the page after the "Go" submit on the Check Pending Transaction
# page. Three states:
#   {state: "results", count: N}             rows mounted — captcha accepted
#   {state: "captcha_mismatch", text: "..."} swal2 popup says try again
#   {state: "pending"}                        keep polling
_GO_SUBMIT_STATE_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var text = (popups[i].textContent || '').toLowerCase();
    if (text.indexOf('captcha') !== -1 &&
        (text.indexOf('mismatch') !== -1
         || text.indexOf('invalid') !== -1
         || text.indexOf('correct captcha') !== -1
         || text.indexOf('wrong') !== -1)) {
      return {state: 'captcha_mismatch', text: popups[i].textContent.trim().substring(0, 200)};
    }
  }
  var rows = document.querySelectorAll('table.table-bordered tbody tr');
  if (rows && rows.length > 0) {
    var first = rows[0].querySelectorAll('td');
    // The pending-tx table has 7 columns. >= 6 tds protects against
    // partial mounts where Angular has rendered the row shell but not
    // the data cells yet.
    if (first.length >= 6) {
      return {state: 'results', count: rows.length};
    }
  }
  return {state: 'pending'};
})()
"""


# Click the bank/university icon in the first row of the results table.
# The Angular click handler is bound on the parent <td>, not the <i>
# itself, so we click the closest <td>.
_CLICK_BANK_ICON_JS = """
(function() {
  var rows = document.querySelectorAll('table.table-bordered tbody tr');
  if (!rows || rows.length === 0) return {ok: false, reason: 'no_rows'};
  var icon = rows[0].querySelector('i.fa-university');
  if (!icon) return {ok: false, reason: 'no_bank_icon'};
  var clickable = icon.closest('td') || icon;
  try { clickable.click(); return {ok: true}; }
  catch (e) { return {ok: false, reason: 'click_threw:' + e.message}; }
})()
"""


# After clicking the bank icon, classify the outcome:
#   {state: "still_on_hold", hint: "Please try after X Minutes Y Seconds"}
#     → portal still has the tx locked; we can't clear it yet.
#   {state: "cleared", via: "url"|"body_text"}
#     → portal redirected to /bank/DoubleVerification with "transaction is
#       fail" — we're free to re-initiate the original tax flow.
#   {state: "pending"}
#     → keep polling.
_BANK_OUTCOME_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var container = popups[i].querySelector('.swal2-html-container');
    var text = ((container && container.textContent) || popups[i].textContent || '').trim();
    if (/try\\s*after/i.test(text)) {
      return {state: 'still_on_hold', hint: text.substring(0, 240)};
    }
  }
  var url = (window.location && window.location.href) || '';
  if (url.indexOf('DoubleVerification') !== -1
      || url.indexOf('doubleVerification') !== -1
      || url.toLowerCase().indexOf('doubleverification') !== -1) {
    return {state: 'cleared', via: 'url'};
  }
  var body = (document.body && document.body.innerText) || '';
  if (/transaction\\s*is\\s*fail/i.test(body)) {
    return {state: 'cleared', via: 'body_text'};
  }
  return {state: 'pending'};
})()
"""


# ─── Public: owner-info outcome polling ────────────────────────────────


async def wait_for_owner_info_outcome(
    session,
    log: StepLogger,
    *,
    name: str = "phase3.wait_owner_outcome",
    timeout: float = OWNER_OUTCOME_POLL_SECS,
    tick: float = OWNER_OUTCOME_POLL_TICK_SECS,
) -> Literal["district_ready", "pending_popup", "validity_popup", "timeout"]:
    """Poll the owner-info page after Get Details was clicked.

    Replaces the old `wait_for_selector(SEL_DISTRICT, timeout=30)` in
    every state's phase 3. The single `_OWNER_INFO_OUTCOME_JS` evaluation
    distinguishes the three meaningful outcomes; the caller branches.

    Always records a StepLog (status OK for district_ready, FAILED for
    everything else) so the run-log shows the routing decision. Never
    raises — caller decides what to do with the timeout case.
    """
    started = time.monotonic()
    deadline = started + timeout
    last_state = "pending"
    last_text = ""

    while time.monotonic() < deadline:
        res = await _cdp_eval(session, _OWNER_INFO_OUTCOME_JS)
        state = (res or {}).get("state", "pending")
        last_state = state
        last_text = (res or {}).get("text", "") or last_text

        if state == "district_ready":
            log.record(StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.OK,
                duration_ms=int((time.monotonic() - started) * 1000),
                value="district_ready",
            ))
            return "district_ready"
        if state == "pending_popup":
            print(f"[pending_clear] owner-info outcome: pending_popup detected")
            log.record(StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - started) * 1000),
                value=f"pending_popup: {last_text[:120]}",
            ))
            return "pending_popup"
        if state == "validity_popup":
            print(f"[pending_clear] owner-info outcome: validity_popup detected")
            log.record(StepLog(
                index=log.next_index(),
                name=name,
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - started) * 1000),
                value=f"validity_popup: {last_text[:120]}",
            ))
            return "validity_popup"

        await asyncio.sleep(tick)

    print(
        f"[pending_clear] owner-info outcome: timeout after {timeout}s "
        f"(last_state={last_state!r}, last_text={last_text[:80]!r})"
    )
    log.record(StepLog(
        index=log.next_index(),
        name=name,
        status=StepStatus.FAILED,
        duration_ms=int((time.monotonic() - started) * 1000),
        value=f"timeout (last_state={last_state})",
        error=f"no decisive outcome within {timeout}s",
    ))
    return "timeout"


# ─── Public: pending-tx clear flow ─────────────────────────────────────


async def clear_pending_transaction(
    session,
    *,
    vehicle_number: str,
    job_id: str,
    r: redis.Redis,
    source: str,
    log: StepLogger,
) -> tuple[Literal["cleared", "still_on_hold", "failed"], str]:
    """Walk the auto-clear-pending-transaction flow.

    Args:
      session: browser-use Browser session (same one phase 3 was driving).
      vehicle_number: the registration number being processed.
      job_id, r: pass-through to solve_canvas_captcha for the human
        fallback (source != 'app').
      source: 'web' or 'app' — controls whether captcha can fall back
        to wait_for_human after AI attempts are exhausted.
      log: StepLogger — every branch records exactly one entry.

    Returns:
      ("cleared", "")               — re-initiate the original flow.
      ("still_on_hold", "X Min Y S")— portal won't let us clear yet; the
                                       hint is the parsed time string.
      ("failed", "<reason>")         — something went wrong; the reason is
                                       a short token for abort_reason.

    Never raises — every exception path returns ("failed", reason).
    """
    print(f"[pending_clear] starting clear flow for vehicle {vehicle_number} (source={source})")
    flow_started = time.monotonic()
    log.record(StepLog(
        index=log.next_index(),
        name="pending_clear.start",
        status=StepStatus.OK,
        value=vehicle_number,
    ))

    # ─── Step 1: dismiss the initial pending-tx popup ──────────────────
    # The owner-info page still has the popup up; navigating away with the
    # popup unhandled sometimes leaves a stale overlay on the destination.
    try:
        dismiss_res = await _cdp_eval(session, _DISMISS_SWAL_JS)
        await asyncio.sleep(0.5)
        if dismiss_res and dismiss_res.get("dismissed"):
            print(f"[pending_clear] dismissed {dismiss_res.get('count')} initial popup(s)")
        else:
            print(f"[pending_clear] no visible popup to dismiss ({dismiss_res})")
    except Exception as e:
        print(f"[pending_clear] popup dismiss raised: {type(e).__name__}: {e}")
        # Non-fatal — continue to navigation.

    # ─── Step 2: navigate home + click the dropdown link ───────────────
    # Direct nav to /public/payment/ChecklTransactionStatus gets rewritten
    # to "/" by the Angular router. Confirmed by the user. So we land on
    # the home page first, then click the routerLink anchor.
    try:
        await navigate(
            session,
            URL_PORTAL_HOME,
            log=log,
            name="pending_clear.nav_home",
        )
        print(f"[pending_clear] navigated to portal home")

        await wait_for_selector(
            session,
            SEL_PENDING_TX_LINK,
            log=log,
            name="pending_clear.wait_pending_link",
            timeout=LINK_WAIT_TIMEOUT_SECS,
        )

        click_res = await _cdp_eval(
            session,
            "(function(){var a=document.querySelector("
            + json.dumps(SEL_PENDING_TX_LINK)
            + ");if(!a) return {ok:false,reason:'not_found'};"
            "a.click();return {ok:true};})()",
        )
        if not click_res or not click_res.get("ok"):
            reason = (click_res or {}).get("reason", "unknown")
            print(f"[pending_clear] failed to click pending-tx link: {reason}")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.click_pending_link",
                status=StepStatus.FAILED,
                error=f"link_click_failed:{reason}",
            ))
            return ("failed", f"pending_tx_link_click_failed:{reason}")

        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.click_pending_link",
            status=StepStatus.OK,
        ))

        await wait_for_selector(
            session,
            SEL_PENDING_VEHICLE_INPUT,
            log=log,
            name="pending_clear.wait_pending_page",
            timeout=PAGE_LOAD_TIMEOUT_SECS,
        )
        print(f"[pending_clear] arrived at Check Pending Transaction page")
    except Exception as e:
        print(f"[pending_clear] navigation step failed: {type(e).__name__}: {e}")
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.navigation",
            status=StepStatus.FAILED,
            error=f"{type(e).__name__}: {e}",
        ))
        return ("failed", f"navigation_failed:{type(e).__name__}")

    # ─── Step 3: fill vehicle number ───────────────────────────────────
    try:
        await fill(
            session,
            SEL_PENDING_VEHICLE_INPUT,
            vehicle_number,
            log=log,
            name="pending_clear.fill_vehicle",
        )
    except Exception as e:
        print(f"[pending_clear] vehicle fill failed: {type(e).__name__}: {e}")
        return ("failed", f"vehicle_fill_failed:{type(e).__name__}")

    # ─── Step 4: solve canvas captcha + click Go ───────────────────────
    # solve_canvas_captcha:
    #   - reads the canvas as a base64 PNG via CDP (canvas.toDataURL),
    #   - sends it to Gemini on Vertex AI via direct llm.ainvoke (NOT
    #     an agent loop — this is the cheap path, not the AI handoff one),
    #   - fills SEL_PENDING_CAPTCHA_INPUT,
    #   - calls our submit_action(),
    #   - if submit_action() returns False, clicks the refresh button to
    #     get a fresh canvas and retries (up to MAX_CAPTCHA_ATTEMPTS).
    #
    # Our submit_action clicks "Go" and polls the page for the deciding
    # state (results / captcha_mismatch / pending). Returns True when the
    # results table populates (captcha was accepted), False on rejection
    # so the solver refreshes and tries again.

    async def _submit_go() -> bool:
        print(f"[pending_clear] clicking Go to submit captcha")
        await _cdp_eval(
            session,
            "(function(){var b=document.querySelector("
            + json.dumps(SEL_PENDING_GO_BTN)
            + ");if(b)b.click();return !!b;})()",
        )
        deadline = time.monotonic() + GO_SUBMIT_POLL_SECS
        while time.monotonic() < deadline:
            res = await _cdp_eval(session, _GO_SUBMIT_STATE_JS)
            state = (res or {}).get("state", "pending")
            if state == "results":
                count = (res or {}).get("count", 0)
                print(f"[pending_clear] table populated ({count} row(s)) — captcha accepted")
                return True
            if state == "captcha_mismatch":
                text = ((res or {}).get("text") or "")[:120]
                print(f"[pending_clear] captcha mismatch popup: {text!r}")
                try:
                    await _cdp_eval(session, _DISMISS_SWAL_JS)
                except Exception:
                    pass
                return False
            await asyncio.sleep(0.5)
        print(f"[pending_clear] Go submit timed out ({GO_SUBMIT_POLL_SECS}s) — no rows, no popup")
        return False

    try:
        await solve_canvas_captcha(
            session,
            canvas_selector=SEL_PENDING_CAPTCHA_CANVAS,
            input_selector=SEL_PENDING_CAPTCHA_INPUT,
            refresh_selector=SEL_PENDING_CAPTCHA_REFRESH,
            submit_action=_submit_go,
            job_id=job_id,
            r=r,
            source=source,
            log=log,
            name="pending_clear.solve_captcha",
            max_ai_attempts=MAX_CAPTCHA_ATTEMPTS,
        )
    except ScriptedAbort as abort:
        print(f"[pending_clear] captcha solver gave up: {abort.reason}")
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.captcha_exhausted",
            status=StepStatus.FAILED,
            error=str(abort.reason),
        ))
        return ("failed", f"captcha_exhausted:{abort.reason}")
    except Exception as e:
        print(f"[pending_clear] captcha solver crashed: {type(e).__name__}: {e}")
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.captcha_crashed",
            status=StepStatus.FAILED,
            error=f"{type(e).__name__}: {e}",
        ))
        return ("failed", f"captcha_crashed:{type(e).__name__}")

    # ─── Step 5: click the bank/university icon ────────────────────────
    # Brief settle — Angular sometimes finishes the row mount a frame
    # after the captcha solver returns.
    await asyncio.sleep(0.5)
    click_res = await _cdp_eval(session, _CLICK_BANK_ICON_JS)
    if not click_res or not click_res.get("ok"):
        reason = (click_res or {}).get("reason", "unknown")
        if reason == "no_rows":
            # Captcha succeeded but the table has no rows. Either the
            # original popup was stale (the pending tx had already
            # cleared by the time we checked) or there's never been one
            # to clear. Either way, we're unblocked.
            print(f"[pending_clear] no rows visible — treating as cleared (no pending tx to act on)")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.no_rows",
                status=StepStatus.OK,
                value="no_pending_tx_visible",
            ))
            return ("cleared", "")
        print(f"[pending_clear] bank-icon click failed: {reason}")
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.click_bank_icon",
            status=StepStatus.FAILED,
            error=f"click_failed:{reason}",
        ))
        return ("failed", f"bank_icon_click_failed:{reason}")

    log.record(StepLog(
        index=log.next_index(),
        name="pending_clear.click_bank_icon",
        status=StepStatus.OK,
    ))
    print(f"[pending_clear] bank icon clicked, polling for outcome…")

    # ─── Step 6: poll for the outcome ──────────────────────────────────
    poll_started = time.monotonic()
    poll_deadline = poll_started + BANK_OUTCOME_POLL_SECS
    while time.monotonic() < poll_deadline:
        res = await _cdp_eval(session, _BANK_OUTCOME_JS)
        state = (res or {}).get("state", "pending")

        if state == "still_on_hold":
            hint_full = ((res or {}).get("hint") or "").strip()
            # Pull just the "X Minutes Y Seconds" / "X Seconds" / "X
            # Minutes" portion if present.
            m = re.search(
                r"(\d+\s*Minutes?\s*\d+\s*Seconds?|\d+\s*Seconds?|\d+\s*Minutes?)",
                hint_full,
                re.IGNORECASE,
            )
            hint = (m.group(0).strip() if m else hint_full)[:80]
            print(f"[pending_clear] outcome: still_on_hold (hint={hint!r})")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.outcome",
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - flow_started) * 1000),
                value=f"still_on_hold:{hint}",
            ))
            # Be polite: close the popup so the next navigation isn't
            # blocked by a stale overlay.
            try:
                await _cdp_eval(session, _DISMISS_SWAL_JS)
            except Exception:
                pass
            return ("still_on_hold", hint)

        if state == "cleared":
            via = (res or {}).get("via", "")
            print(f"[pending_clear] outcome: cleared (via={via})")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.outcome",
                status=StepStatus.OK,
                duration_ms=int((time.monotonic() - flow_started) * 1000),
                value=f"cleared:{via}",
            ))
            return ("cleared", "")

        await asyncio.sleep(BANK_OUTCOME_POLL_TICK_SECS)

    print(f"[pending_clear] outcome polling timed out after {BANK_OUTCOME_POLL_SECS}s")
    log.record(StepLog(
        index=log.next_index(),
        name="pending_clear.outcome",
        status=StepStatus.FAILED,
        duration_ms=int((time.monotonic() - flow_started) * 1000),
        value="poll_timeout",
        error=f"no decisive bank-icon outcome within {BANK_OUTCOME_POLL_SECS}s",
    ))
    return ("failed", "outcome_poll_timeout")


# ─── Public: restart phases 1+2 after a successful clear ───────────────


# Phase-1+2 selectors. State-agnostic — same template for UP/HR/PB/MP.
_RESTART_SEL_STATE_DROPDOWN = "select.select-css-check-post-services"
_RESTART_SEL_SERVICE_DROPDOWN = "select[name='serviceName']"
_RESTART_URL_PARIVAHAN = "https://parivahan.gov.in/en/node/579"
_RESTART_SERVICE_NAME = "VEHICLE TAX COLLECTION (OTHER STATE)"

# Phase-3 selector (same across all 4 scripted states).
_RESTART_SEL_VEHICLE_INPUT = "input#floatingvehicle"

# Mirrors the per-state PHASE_GAP_SECS = 1.5 used everywhere.
_RESTART_PHASE_GAP_SECS = 1.5


async def navigate_to_owner_info_page(
    session,
    *,
    state_code: str,
    log: StepLogger,
    wait_for_tax_collection_url: bool = True,
) -> None:
    """Re-run phases 1 + 2 from scratch, leaving the page ready for phase 3.

    Used by state runners after `clear_pending_transaction` returns
    "cleared" — the recovery flow ends on parivahan.gov.in/checkpostv4/
    #/bank/DoubleVerification/Process and we can't shortcut back to the
    owner-info form (the router rewrites direct /taxCollectionOnline
    nav to "/", same reason the clear flow uses the dropdown link).

    Args:
      state_code:                  "UP" | "HR" | "PB" | "MP". Used as the
                                   <option value=…> on the state dropdown.
      wait_for_tax_collection_url: UP/HR/PB wait for the URL fragment
                                   "taxCollectionOnline" after Go. MP's
                                   existing runner skips this — keep the
                                   skip configurable so we don't change
                                   the working flow for any state.

    Caller is responsible for the post-restart phase 3 (fill vehicle,
    click Get Details, re-evaluate wait_for_owner_info_outcome). This
    function only handles getting us back to the page that has the
    vehicle input mounted.
    """
    print(f"[pending_clear] restart: phases 1+2 for state {state_code}")

    # Phase 1 — parivahan landing.
    await navigate(
        session,
        _RESTART_URL_PARIVAHAN,
        log=log,
        name="phase1_retry.open_parivahan",
    )
    await wait_for_selector(
        session,
        _RESTART_SEL_STATE_DROPDOWN,
        log=log,
        name="phase1_retry.wait_state_dropdown",
    )
    await select_by_value(
        session,
        _RESTART_SEL_STATE_DROPDOWN,
        state_code,
        log=log,
        name=f"phase1_retry.select_state_{state_code.lower()}",
    )
    await wait_for_url(
        session,
        "checkpostv4",
        log=log,
        name="phase1_retry.wait_service_page",
    )
    await sleep_seconds(_RESTART_PHASE_GAP_SECS, log=log, name="phase1_retry.settle")

    # Phase 2 — service selection.
    await wait_for_selector(
        session,
        _RESTART_SEL_SERVICE_DROPDOWN,
        log=log,
        name="phase2_retry.wait_service_dropdown",
        timeout=45,
    )
    await select_by_text(
        session,
        _RESTART_SEL_SERVICE_DROPDOWN,
        _RESTART_SERVICE_NAME,
        log=log,
        name="phase2_retry.select_service",
    )
    await click_by_text(
        session,
        "Go",
        log=log,
        name="phase2_retry.click_go",
        tag="button",
    )
    if wait_for_tax_collection_url:
        await wait_for_url(
            session,
            "taxCollectionOnline",
            log=log,
            name="phase2_retry.wait_owner_info_page",
        )
    await sleep_seconds(_RESTART_PHASE_GAP_SECS, log=log, name="phase2_retry.settle")

    # Confirm we landed on the owner-info form (vehicle input is its
    # earliest mountable control). Mirrors the wait every state's phase
    # 3 does on the first run.
    await wait_for_selector(
        session,
        _RESTART_SEL_VEHICLE_INPUT,
        log=log,
        name="phase3_retry.wait_vehicle_input",
    )
    print(f"[pending_clear] restart: owner-info page is ready for phase 3 retry")

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
        5. Solve the canvas captcha (CDP-screenshot fallback to handle
           canvases whose getBoundingClientRect lies — see
           _capture_captcha_png and _ocr_captcha_image below).
        6. Click "Go". Poll for either:
              - the results table populating (captcha accepted), or
              - the "Captcha mismatch" SweetAlert2 popup (captcha rejected;
                we click the refresh button and retry the loop).
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

  Captcha-extraction strategy
  ---------------------------
  We do NOT use scripted.captcha.solve_canvas_captcha here, because its
  _wait_visible(canvas, 10s) check relies on getBoundingClientRect()
  reporting w>0 AND h>0. On both this page and the phase-6 disclaimer
  page, the captcha <canvas> reports 0×0 even when it's drawn and
  toDataURL works fine — same CSS quirk that pushed phase 6 to AI
  handoff. Our local solver instead:
    1. Waits for the canvas to be PRESENT in DOM (no size check).
    2. Captures via canvas.toDataURL (cheapest) → CDP Page.captureScreenshot
       clipped to the captcha rect → full viewport screenshot, whichever
       lands first.
    3. OCRs via direct llm.ainvoke against Gemini-on-Vertex — same cheap
       path the shared solver uses, but applied to whichever PNG worked.
    4. Refreshes the captcha between attempts (max MAX_CAPTCHA_ATTEMPTS=5).
  This stays roughly $0.0001–0.0005 per attempt vs. ~$0.01–0.05 for a
  full run_ai_rescue Agent loop.

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

from ..handoff import build_llm
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
from ..types import StepLog, StepStatus


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
# popup (try-after) or the recovery navigation/text. Gov portal is
# slow — was 12s, bumped to 30s. We log a debug-context line every
# DEBUG_TICK_SECS during the poll so timeouts are diagnosable.
BANK_OUTCOME_POLL_SECS = 30.0
BANK_OUTCOME_POLL_TICK_SECS = 0.5
BANK_OUTCOME_DEBUG_TICK_SECS = 3.0  # log URL/popups/body every ~3s

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
# The Angular click handler binding is unstable across this portal — it's
# sometimes on the <i>, sometimes on the <td>, sometimes only listening
# to synthetic MouseEvents. We click the icon AND the td AND dispatch
# a synthetic event, then return everything we tried so the log shows
# what landed.
_CLICK_BANK_ICON_JS = """
(function() {
  var rows = document.querySelectorAll('table.table-bordered tbody tr');
  if (!rows || rows.length === 0) return {ok: false, reason: 'no_rows'};
  var icon = rows[0].querySelector('i.fa-university');
  if (!icon) return {ok: false, reason: 'no_bank_icon'};
  var td = icon.closest('td') || icon.parentElement;

  var attempts = [];

  try { td.click(); attempts.push('td.click'); }
  catch (e) { attempts.push('td.click_threw:' + (e.message || 'unknown')); }

  try { icon.click(); attempts.push('icon.click'); }
  catch (e) { attempts.push('icon.click_threw:' + (e.message || 'unknown')); }

  try {
    var evt = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
    td.dispatchEvent(evt);
    attempts.push('td.dispatchEvent');
  } catch (e) { attempts.push('td.dispatch_threw:' + (e.message || 'unknown')); }

  return {ok: true, attempts: attempts};
})()
"""


# After clicking the bank icon, classify the outcome AND always return
# debug context (current URL, visible popups, body snippet) so the
# polling loop can log what it's seeing each tick. Much more useful
# than a silent 12s timeout when something unexpected happens.
#
# Decisions:
#   {state: "still_on_hold", hint: "..."} — visible popup whose text
#     matches /try\s*after/i.
#   {state: "cleared", via: "url"|"body_text", ...} — URL contains
#     "DoubleVerification" OR "/bank/", OR body contains the success
#     text ("transaction is fail" / "initiate transaction again").
#   {state: "pending", url, popups, body_snippet} — keep polling. The
#     caller logs this context every few ticks so we can see what's
#     actually on screen when something goes wrong.
_BANK_OUTCOME_JS = """
(function() {
  var url = (window.location && window.location.href) || '';
  var body = (document.body && document.body.innerText) || '';

  var popups = [];
  var pNodes = document.querySelectorAll('.swal2-popup');
  for (var i = 0; i < pNodes.length; i++) {
    var r = pNodes[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var c = pNodes[i].querySelector('.swal2-html-container');
    var text = ((c && c.textContent) || pNodes[i].textContent || '').trim();
    popups.push(text.substring(0, 240));
  }

  for (var j = 0; j < popups.length; j++) {
    if (/try\\s*after/i.test(popups[j])) {
      return {
        state: 'still_on_hold',
        hint: popups[j],
        url: url,
        popups: popups
      };
    }
  }

  // URL signals: documented post-clear path is /bank/DoubleVerification.
  // Also accept any /bank/ in case the path drifts.
  if (/DoubleVerification/i.test(url) || /\\/bank\\//i.test(url)) {
    return {
      state: 'cleared',
      via: 'url',
      url: url,
      popups: popups
    };
  }

  // Body signals: the documented success text is "Your transaction is
  // fail. Please initiate transaction again." — match either sentence.
  if (/transaction\\s*is\\s*fail/i.test(body)
      || /initiate\\s*transaction\\s*again/i.test(body)) {
    return {
      state: 'cleared',
      via: 'body_text',
      url: url,
      body_snippet: body.substring(0, 240),
      popups: popups
    };
  }

  return {
    state: 'pending',
    url: url,
    popups: popups,
    body_snippet: body.substring(0, 240)
  };
})()
"""


# ─── Captcha capture & OCR (CDP-screenshot fallback) ───────────────────
#
# WHY THIS EXISTS, not shared/solve_canvas_captcha:
#   The shared solver does `_wait_visible(canvas, 10s)` which polls
#   getBoundingClientRect for w>0 AND h>0. On the Check Pending
#   Transaction page (and the phase 6 disclaimer page — that's why
#   phase 6 uses AI handoff!), the captcha <canvas> element reports
#   0×0 even when it's rendered and toDataURL works fine — probably
#   a CSS quirk in the checkpostv4 Angular template. The shared
#   solver therefore times out 5 times in a row before giving up.
#
# What we do instead:
#   1. Wait for the canvas to be PRESENT in DOM (no size check).
#   2. Try canvas.toDataURL() — cheapest, works on most pages.
#   3. Fall back to CDP Page.captureScreenshot clipped to the captcha's
#      bounding rect.
#   4. Fall back further to a full-viewport screenshot (then the LLM
#      prompt tells Gemini to find the captcha inside the screenshot).
#   5. OCR via direct llm.ainvoke (same Gemini-on-Vertex path captcha.py
#      uses — NOT an Agent loop, so cost stays ~$0.0001/call).


# Wait for the captcha canvas to be in the DOM (no rendered-size check,
# since that's the exact signal that's broken here).
#
# IMPORTANT: this page has TWO <canvas> elements — a hidden one (id="captcha",
# bitmap 140×36, BoundingClientRect 0×0) AND a visible one (no id, bitmap
# 145×36, BoundingClientRect 145×36). They render DIFFERENT captcha text.
# Reading the hidden one means we submit gibberish while the user watches
# the visible one refresh. We always pick the VISIBLE one, falling back to
# the largest-bitmap canvas if none has a non-zero rect.
_CAPTCHA_PRESENCE_JS = """
(function() {
  var all = document.querySelectorAll('canvas');
  if (!all || all.length === 0) {
    return {found: false, reason: 'no_canvas'};
  }
  var target = null;
  var via = '';
  for (var i = 0; i < all.length; i++) {
    var c = all[i];
    if (!(c instanceof HTMLCanvasElement)) continue;
    var r = c.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) { target = c; via = 'visible'; break; }
  }
  if (!target) {
    var maxArea = 0;
    for (var j = 0; j < all.length; j++) {
      var c2 = all[j];
      if (!(c2 instanceof HTMLCanvasElement)) continue;
      var area = (c2.width || 0) * (c2.height || 0);
      if (area > maxArea) { maxArea = area; target = c2; via = 'largest_bitmap'; }
    }
  }
  if (!target) return {found: false, reason: 'no_canvas_with_bitmap'};
  return {
    found: true,
    via: via,
    canvas_count: all.length,
    bitmap_w: target.width,
    bitmap_h: target.height,
    parent_id: (target.parentElement && target.parentElement.id) || ''
  };
})()
"""


# Read the VISIBLE canvas as base64 PNG via toDataURL. Returns null if the
# data URL is suspiciously short (blank canvas) or toDataURL threw (tainted).
# Same visible-canvas selection rule as _CAPTCHA_PRESENCE_JS.
_CANVAS_TODATAURL_JS = r"""
(function() {
  var all = document.querySelectorAll('canvas');
  var target = null;
  for (var i = 0; i < all.length; i++) {
    var c = all[i];
    if (!(c instanceof HTMLCanvasElement)) continue;
    var r = c.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) { target = c; break; }
  }
  if (!target) {
    var maxArea = 0;
    for (var j = 0; j < all.length; j++) {
      var c2 = all[j];
      if (!(c2 instanceof HTMLCanvasElement)) continue;
      var area = (c2.width || 0) * (c2.height || 0);
      if (area > maxArea) { maxArea = area; target = c2; }
    }
  }
  if (!target) return null;
  try {
    var url = target.toDataURL('image/png');
    if (!url || url.length < 200) return null;
    return url.replace(/^data:image\/png;base64,/, '');
  } catch (e) {
    return null;
  }
})()
"""


# Bounding rect of the captcha. We prefer the visible canvas itself, then
# fall back to common container divs. Used to clip Page.captureScreenshot.
# Returns null if no plausible element has a non-zero rect.
_CAPTCHA_RECT_JS = """
(function() {
  // Prefer the visible canvas directly.
  var all = document.querySelectorAll('canvas');
  for (var i = 0; i < all.length; i++) {
    var c = all[i];
    if (!(c instanceof HTMLCanvasElement)) continue;
    var r = c.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      return {x: r.left, y: r.top, w: r.width, h: r.height, sel: 'canvas[visible]'};
    }
  }
  // Fall back to container divs in case all canvases lie about their rect.
  var sels = ['div#captcha', 'div.cap'];
  for (var k = 0; k < sels.length; k++) {
    var el = document.querySelector(sels[k]);
    if (!el) continue;
    var rr = el.getBoundingClientRect();
    if (rr.width > 0 && rr.height > 0) {
      return {x: rr.left, y: rr.top, w: rr.width, h: rr.height, sel: sels[k]};
    }
  }
  return null;
})()
"""


# Click the refresh button next to the captcha (used between attempts).
_REFRESH_CAPTCHA_JS = (
    "(function(s){var b=document.querySelector(s);"
    "if(b){b.click();return true;}return false;})("
    "'div#captcha + button, button.btn-primary.m-left')"
)


async def _wait_captcha_in_dom(session, timeout: float = 15.0) -> bool:
    """Poll until a visible captcha canvas is present in the DOM."""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        res = await _cdp_eval(session, _CAPTCHA_PRESENCE_JS)
        if res and res.get("found"):
            elapsed = time.monotonic() - started
            print(
                f"[pending_clear] captcha in DOM after {elapsed:.1f}s "
                f"(via={res.get('via')!r}, canvas_count={res.get('canvas_count')}, "
                f"bitmap={res.get('bitmap_w')}x{res.get('bitmap_h')}, "
                f"parent_id={res.get('parent_id')!r})"
            )
            return True
        await asyncio.sleep(0.25)
    print(f"[pending_clear] captcha never appeared in DOM within {timeout}s")
    return False


async def _capture_captcha_png(session) -> tuple[str | None, str]:
    """Capture the captcha as base64 PNG. Returns (b64, source) where
    source is one of:
      'canvas_toDataURL' — cheapest, exact pixels
      'cdp_clip'         — CDP screenshot clipped to captcha rect
      'cdp_viewport'     — full viewport (LLM has to find the captcha)
    Returns (None, '') if every path failed."""

    # 1. canvas.toDataURL — cheapest, exact pixels.
    b64 = await _cdp_eval(session, _CANVAS_TODATAURL_JS)
    if b64:
        return (b64, "canvas_toDataURL")

    # 2. CDP screenshot clipped to the captcha's bounding rect.
    rect = await _cdp_eval(session, _CAPTCHA_RECT_JS)
    try:
        cdp = await session.get_or_create_cdp_session()
    except Exception as e:
        print(f"[pending_clear] could not get CDP session: {e}")
        return (None, "")

    if rect and rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
        try:
            pad = 6.0  # px padding so OCR isn't tight against the edges
            result = await cdp.cdp_client.send.Page.captureScreenshot(
                params={
                    "format": "png",
                    "clip": {
                        "x": max(0.0, float(rect["x"]) - pad),
                        "y": max(0.0, float(rect["y"]) - pad),
                        "width": float(rect["w"]) + 2 * pad,
                        "height": float(rect["h"]) + 2 * pad,
                        "scale": 1.0,
                    },
                },
                session_id=cdp.session_id,
            )
            data = (result or {}).get("data") if isinstance(result, dict) else getattr(result, "data", None)
            if data:
                return (data, "cdp_clip")
        except Exception as e:
            print(f"[pending_clear] CDP clipped screenshot failed: {type(e).__name__}: {e}")

    # 3. Full viewport screenshot — last resort.
    try:
        result = await cdp.cdp_client.send.Page.captureScreenshot(
            params={"format": "png"},
            session_id=cdp.session_id,
        )
        data = (result or {}).get("data") if isinstance(result, dict) else getattr(result, "data", None)
        if data:
            return (data, "cdp_viewport")
    except Exception as e:
        print(f"[pending_clear] CDP viewport screenshot failed: {type(e).__name__}: {e}")

    return (None, "")


async def _ocr_captcha_image(image_b64: str, source: str) -> str:
    """Direct LLM OCR using the captured PNG. Returns the decoded
    characters, or 'UNREADABLE' if every message shape fails.

    The CHEAP path. On some browser_use 0.12.x patch releases this
    fails with `'dict' object has no attribute 'model_copy'` because
    ChatGoogle.ainvoke wants pydantic message objects instead of raw
    dicts. When it fails, the caller falls back to
    _ocr_via_screenshot_agent, which is the EXPENSIVE-but-reliable
    Agent path (~3 LLM calls per captcha vs 1 here).

    Same message-shape juggling as captcha.py's _ocr_via_llm.
    """
    llm = build_llm()

    if source == "cdp_viewport":
        instruction = (
            "This is a full-page screenshot of a government website "
            "(Check Pending Transaction page). Find the small CAPTCHA "
            "image — a rectangular box with distorted/wavy characters "
            "on a light cyan-green background, typically 4–6 mixed "
            "letters and digits, located near a blue refresh button "
            "and a 'Go' button. Read ONLY the characters from inside "
            "that captcha image — case-sensitive. Reply with ONLY "
            "those characters, no spaces, no quotes, no explanation. "
            "If you cannot find or read the captcha, reply with the "
            "single word: UNREADABLE"
        )
    else:
        instruction = (
            "This is a captcha image from a government website. It "
            "contains 4–6 distorted characters (mixed letters and "
            "digits, case-sensitive) on a light cyan-green background. "
            "Read the characters and reply with ONLY those characters, "
            "no spaces, no quotes, no explanation. If you cannot read "
            "it clearly, reply with the single word: UNREADABLE"
        )

    def _extract(response) -> str:
        text = (
            getattr(response, "completion", None)
            or getattr(response, "content", None)
            or ""
        )
        if isinstance(text, list):
            text = " ".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p)
                for p in text
            )
        return (text or "").strip().strip("'\"` \t\n\r")

    # Shape A: raw dict messages (matches captcha.py _ocr_via_llm).
    try:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}"
                        },
                    },
                ],
            }
        ]
        response = await llm.ainvoke(msgs)
        text = _extract(response)
        if text and text.upper() != "UNREADABLE":
            return text
    except Exception as e:
        print(f"[pending_clear] OCR shape-A (dict) failed: {type(e).__name__}: {e}")

    # Shape B: langchain HumanMessage. Some ChatGoogle versions accept
    # this and call .model_copy() on it successfully (it's a pydantic
    # BaseModel, which is what shape-A's dicts don't provide).
    try:
        from langchain_core.messages import HumanMessage  # type: ignore

        msg = HumanMessage(
            content=[
                {"type": "text", "text": instruction},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}"
                    },
                },
            ]
        )
        response = await llm.ainvoke([msg])
        text = _extract(response)
        if text and text.upper() != "UNREADABLE":
            return text
    except Exception as e:
        print(f"[pending_clear] OCR shape-B (HumanMessage) failed: {type(e).__name__}: {e}")

    # Shape C: browser_use's own UserMessage. Newer 0.12.x patches.
    try:
        from browser_use.llm.messages import (  # type: ignore
            UserMessage,
            ContentPartTextParam,
            ContentPartImageParam,
            ImageURL,
        )

        msg = UserMessage(
            content=[
                ContentPartTextParam(text=instruction),
                ContentPartImageParam(
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{image_b64}"
                    )
                ),
            ]
        )
        response = await llm.ainvoke([msg])
        text = _extract(response)
        if text and text.upper() != "UNREADABLE":
            return text
    except Exception as e:
        print(f"[pending_clear] OCR shape-C (browser_use UserMessage) failed: {type(e).__name__}: {e}")

    return "UNREADABLE"


async def _ocr_via_screenshot_agent(session) -> tuple[str, float]:
    """Fallback OCR via a 1-step Agent that screenshots the page and
    writes the captcha back through a captured tool call. Used when
    every direct llm.ainvoke shape fails (see _ocr_captcha_image).

    The Agent takes its OWN page screenshot — we don't pass it the
    captured PNG. This is what makes it more reliable than the direct
    path: browser_use's Agent infrastructure has been kept working
    across patch releases even when bare llm.ainvoke message shapes
    have drifted.

    Returns:
      (text, cost_usd) — text is the decoded captcha (or 'UNREADABLE'),
      cost_usd is the Vertex API spend for this Agent run, extracted via
      agent.token_cost_service. We mirror captcha.py's _ocr_via_agent
      so the accumulated cost flows through StepLog.handoff_cost_usd →
      StepLogger.total_handoff_cost() → RunOutcome.total_cost_usd → the
      same place every other LLM cost in this run lands.

    Cost: max_steps=2 keeps this to two Gemini-3-Flash vision calls per
    attempt, ~$0.001–0.002 each. Five attempts worst-case ≈ $0.01.

    The Agent is told NOT to click or type — it must ONLY call
    submit_captcha(text) and then done. submit_captcha is a fake
    tool we register here that just records the text into a closure
    dict and returns "ok".
    """
    # Late-imported so the module loads even if browser_use is older
    # than expected (we'd just degrade to UNREADABLE).
    try:
        from browser_use import Agent, Tools  # type: ignore
    except Exception as e:
        print(f"[pending_clear] could not import browser_use Agent: {e}")
        return "UNREADABLE", 0.0

    captured: dict[str, str] = {"text": ""}
    tools = Tools()

    @tools.action(
        description=(
            "Submit the captcha text you read from the small captcha "
            "image on the current page. Pass the characters as a single "
            "string with no spaces. If you cannot read them, pass "
            "'UNREADABLE'."
        )
    )
    async def submit_captcha(text: str) -> str:
        captured["text"] = (text or "").strip().strip("'\"` \t\n\r")
        return "ok"

    # Tighter prompt: we observed Gemini-3-Flash calling submit_captcha
    # TWICE before calling done (step 1: submit, step 2: submit again,
    # step 3: done). The explicit "exactly TWO actions" framing + the
    # max_steps=2 cap below keeps this at one submit + one done.
    prompt = (
        "Look at the current page — it is a Check Transaction Status "
        "page on a government portal. It has an 'Input Vehicle No.' "
        "field, a small CAPTCHA image (a rectangular box with "
        "distorted characters on a light cyan-green background, 4–6 "
        "mixed letters and digits, case-sensitive — example look: "
        "wavy italics like 'xbjPCe' or '7v8da6'), an 'Input Text from "
        "Image' field, and a blue 'Go' button.\n\n"
        "Take EXACTLY TWO actions in order:\n"
        "  STEP 1: Read the characters inside the CAPTCHA image, then "
        "call submit_captcha(text='<characters>'). Preserve case "
        "exactly. No spaces, no quotes, no surrounding text.\n"
        "  STEP 2: Immediately call done.\n\n"
        "Do NOT call submit_captcha twice. Do NOT click anything, do "
        "NOT type into any field, do NOT navigate, do NOT take any "
        "other action."
    )

    cost_usd = 0.0
    agent = None
    try:
        agent = Agent(
            task=prompt,
            llm=build_llm(),
            browser=session,
            tools=tools,
            calculate_cost=True,
        )
        # max_steps=2: tightest budget that lets the agent submit AND
        # call done. If done is never reached, the run exits at the
        # cap; we still have captured["text"] from step 1.
        await agent.run(max_steps=2)
    except Exception as e:
        print(f"[pending_clear] screenshot-agent run failed: {type(e).__name__}: {e}")

    # Cost extraction. Mirror captcha.py::_ocr_via_agent exactly so the
    # accounting goes through the same machinery. Best-effort — if the
    # cost service / fill_missing_cost path is unavailable we just
    # report 0.0 rather than failing the whole flow.
    if agent is not None:
        try:
            from cost_calculator import fill_missing_cost  # type: ignore

            usage = await agent.token_cost_service.get_usage_summary()
            cd = fill_missing_cost(usage)
            if cd:
                cost_usd = float(cd.get("totalCost", 0.0) or 0.0)
        except Exception as e:
            print(
                f"[pending_clear] screenshot-agent cost extraction failed: "
                f"{type(e).__name__}: {e}"
            )

    text = captured["text"]
    if not text or text.upper() == "UNREADABLE":
        print(f"[pending_clear] screenshot-agent returned no text (cost=${cost_usd:.5f})")
        return "UNREADABLE", cost_usd
    print(
        f"[pending_clear] screenshot-agent returned: {text!r} "
        f"(cost=${cost_usd:.5f})"
    )
    return text, cost_usd


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
      job_id, r: present for parity with the shared captcha solver's
        signature, currently unused by our local captcha loop. Kept so
        we can wire up a human fallback later if we ever need to.
      source: 'web' or 'app' — currently unused by the local captcha
        loop. Kept for the same reason as job_id/r.
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

    # ─── Step 4: solve captcha + click Go (custom loop) ────────────────
    # We do NOT use shared solve_canvas_captcha here. Its _wait_visible
    # (canvas) check unreliably times out on this page — see the long
    # comment near _wait_captcha_in_dom for the why. Our loop:
    #   - waits for canvas PRESENCE (not size),
    #   - captures via canvas.toDataURL → CDP-clipped screenshot → CDP
    #     viewport screenshot (whichever works first),
    #   - OCRs via direct llm.ainvoke against Gemini-on-Vertex,
    #   - fills the input, clicks Go, polls for the outcome,
    #   - on rejection or no-decision, clicks the refresh button and
    #     retries up to MAX_CAPTCHA_ATTEMPTS times.

    async def _submit_go() -> bool:
        """Click Go and poll for {results, captcha_mismatch, pending}.
        Returns True if the results table populated (captcha accepted),
        False on rejection (popup dismissed) or no-decision timeout."""
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
                print(
                    f"[pending_clear] table populated ({count} row(s)) "
                    f"— captcha accepted"
                )
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
        print(
            f"[pending_clear] Go submit timed out ({GO_SUBMIT_POLL_SECS}s) "
            f"— no rows, no popup"
        )
        return False

    async def _refresh_captcha() -> None:
        """Click the blue refresh button next to the canvas. Best-effort."""
        try:
            ok = await _cdp_eval(session, _REFRESH_CAPTCHA_JS)
            print(f"[pending_clear] refresh button clicked (ok={ok})")
            # Wait briefly for the canvas to redraw.
            await asyncio.sleep(1.0)
        except Exception as e:
            print(f"[pending_clear] refresh click raised: {type(e).__name__}: {e}")

    # Wait for the canvas to be present in DOM before the first attempt.
    if not await _wait_captcha_in_dom(session, timeout=15.0):
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.captcha_not_in_dom",
            status=StepStatus.FAILED,
            error="canvas element not present in DOM within 15s",
        ))
        return ("failed", "captcha_not_in_dom")

    captcha_solved = False
    last_source = ""
    last_text = ""
    # Accumulated cost across all screenshot-agent fallbacks in this clear
    # flow. Attached to the final success / exhausted StepLog via
    # handoff_cost_usd, so StepLogger.total_handoff_cost() picks it up the
    # same way captcha.py does. We attach to the FINAL StepLog only (not
    # per-retry) to avoid double-counting through the sum.
    total_agent_cost = 0.0

    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        attempt_started = time.monotonic()

        # 4a. Capture image (toDataURL → clip → viewport).
        image_b64, source = await _capture_captcha_png(session)
        if not image_b64:
            print(f"[pending_clear] attempt {attempt}: image capture failed via every method")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.solve_captcha",
                status=StepStatus.RETRIED,
                attempt=attempt,
                duration_ms=int((time.monotonic() - attempt_started) * 1000),
                error="image_capture_failed",
            ))
            await _refresh_captcha()
            continue

        last_source = source
        print(
            f"[pending_clear] attempt {attempt}: captured "
            f"{len(image_b64)}-char b64 via {source}"
        )

        # 4b. OCR via direct Gemini call. If every message shape fails
        # (model_copy errors etc.), fall back to a 1-step Agent that
        # screenshots the page and reads the captcha through a captured
        # tool call. The Agent path is slower/pricier but proven to work
        # across browser_use 0.12.x patch releases.
        text = await _ocr_captcha_image(image_b64, source)
        if not text or text.upper() == "UNREADABLE":
            print(
                f"[pending_clear] attempt {attempt}: direct OCR failed "
                f"(src={source}), falling back to screenshot-agent"
            )
            text, agent_cost = await _ocr_via_screenshot_agent(session)
            total_agent_cost += agent_cost
            source = source + "+agent_fallback"

        last_text = text
        if not text or text.upper() == "UNREADABLE":
            print(f"[pending_clear] attempt {attempt}: OCR returned UNREADABLE")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.solve_captcha",
                status=StepStatus.RETRIED,
                attempt=attempt,
                duration_ms=int((time.monotonic() - attempt_started) * 1000),
                value=f"unreadable (src={source})",
                handoff_reason="captcha_ocr",
            ))
            await _refresh_captcha()
            continue

        print(f"[pending_clear] attempt {attempt}: OCR='{text}' (src={source})")

        # 4c. Fill the captcha input.
        try:
            await fill(
                session,
                SEL_PENDING_CAPTCHA_INPUT,
                text,
                log=log,
                name=f"pending_clear.fill_captcha_attempt_{attempt}",
            )
        except Exception as e:
            print(f"[pending_clear] attempt {attempt}: fill captcha failed: {e}")
            await _refresh_captcha()
            continue

        # 4d. Click Go, poll for outcome.
        accepted = await _submit_go()

        if accepted:
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.solve_captcha",
                status=StepStatus.OK,
                attempt=attempt,
                duration_ms=int((time.monotonic() - attempt_started) * 1000),
                value=f"{text} (src={source})",
                handoff_reason="captcha_ocr",
                handoff_cost_usd=total_agent_cost,
            ))
            print(
                f"[pending_clear] captcha solved on attempt {attempt}, "
                f"total agent cost so far: ${total_agent_cost:.5f}"
            )
            captcha_solved = True
            break

        # Rejected or no decision — refresh and try again.
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.solve_captcha",
            status=StepStatus.RETRIED,
            attempt=attempt,
            duration_ms=int((time.monotonic() - attempt_started) * 1000),
            value=f"rejected:{text} (src={source})",
            handoff_reason="captcha_ocr",
        ))
        await _refresh_captcha()

    if not captcha_solved:
        print(
            f"[pending_clear] captcha not solved after "
            f"{MAX_CAPTCHA_ATTEMPTS} attempts (last src={last_source}, "
            f"last text={last_text!r}, total agent cost: "
            f"${total_agent_cost:.5f})"
        )
        log.record(StepLog(
            index=log.next_index(),
            name="pending_clear.captcha_exhausted",
            status=StepStatus.FAILED,
            error=(
                f"captcha not solved after {MAX_CAPTCHA_ATTEMPTS} attempts "
                f"(last src={last_source})"
            ),
            handoff_cost_usd=total_agent_cost,
        ))
        return ("failed", f"captcha_exhausted_after_{MAX_CAPTCHA_ATTEMPTS}_attempts")

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
        value=f"attempts={(click_res or {}).get('attempts', [])}",
    ))
    print(
        f"[pending_clear] bank icon clicked, attempts="
        f"{(click_res or {}).get('attempts', [])}, polling for outcome…"
    )

    # ─── Step 6: poll for the outcome ──────────────────────────────────
    # Give the page a moment to react to the click before the first
    # poll — avoids racing against a still-in-flight navigation that
    # would let us see a transient "pending" state.
    await asyncio.sleep(1.0)

    poll_started = time.monotonic()
    poll_deadline = poll_started + BANK_OUTCOME_POLL_SECS
    last_debug_log = 0.0
    last_url = ""
    last_popups: list = []
    last_body_snippet = ""

    while time.monotonic() < poll_deadline:
        res = await _cdp_eval(session, _BANK_OUTCOME_JS)
        state = (res or {}).get("state", "pending")
        last_url = (res or {}).get("url", "") or last_url
        last_popups = (res or {}).get("popups", []) or last_popups
        last_body_snippet = (res or {}).get("body_snippet", "") or last_body_snippet

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
            print(f"[pending_clear] outcome: still_on_hold (hint={hint!r}, url={last_url!r})")
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
            via_snippet = ""
            if via == "url":
                via_snippet = f"url={last_url!r}"
            elif via == "body_text":
                via_snippet = f"body_snippet={last_body_snippet[:120]!r}"
            print(f"[pending_clear] outcome: cleared (via={via}, {via_snippet})")
            log.record(StepLog(
                index=log.next_index(),
                name="pending_clear.outcome",
                status=StepStatus.OK,
                duration_ms=int((time.monotonic() - flow_started) * 1000),
                value=f"cleared:{via}",
                url=last_url or None,
            ))
            return ("cleared", "")

        # Periodic debug log so a stuck poll is diagnosable instead of
        # silent. Every BANK_OUTCOME_DEBUG_TICK_SECS, print URL +
        # popups + body snippet so we can see what's actually on screen.
        elapsed = time.monotonic() - poll_started
        if elapsed - last_debug_log >= BANK_OUTCOME_DEBUG_TICK_SECS:
            last_debug_log = elapsed
            print(
                f"[pending_clear] poll @ {elapsed:.1f}s: state={state} "
                f"url={last_url!r} "
                f"popups={last_popups} "
                f"body_snippet={last_body_snippet[:120]!r}"
            )

        await asyncio.sleep(BANK_OUTCOME_POLL_TICK_SECS)

    # Timeout — dump everything we last saw so we know WHY it timed out.
    print(
        f"[pending_clear] outcome polling timed out after "
        f"{BANK_OUTCOME_POLL_SECS}s. Last seen:\n"
        f"  url={last_url!r}\n"
        f"  popups={last_popups}\n"
        f"  body_snippet={last_body_snippet[:240]!r}"
    )
    log.record(StepLog(
        index=log.next_index(),
        name="pending_clear.outcome",
        status=StepStatus.FAILED,
        duration_ms=int((time.monotonic() - flow_started) * 1000),
        value=f"poll_timeout url={last_url[:120]!r} popups={last_popups}",
        error=(
            f"no decisive bank-icon outcome within {BANK_OUTCOME_POLL_SECS}s; "
            f"body_snippet={last_body_snippet[:120]!r}"
        ),
        url=last_url or None,
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

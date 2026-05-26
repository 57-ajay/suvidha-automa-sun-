# worker/src/scripted/fetch_receipt/runner.py
"""Fetch-receipt scripted runner. State-agnostic.

Flow:
  1. Navigate to the parivahan Print-Payment-Receipt page (deep link).
  2. Select state, fill vehicle, solve captcha + click Get Details.
  3. Find the row whose Payment Date matches params.paymentDate
     (or whose Receipt No matches params.receiptNo if provided).
  4. Click that row's print icon → CustomerReceipt page renders.
  5. Hand off to actions.save_receipt to printToPDF + upload.

The Browser/StepLogger plumbing is set up by scripted/runner.py
(run_fetch_receipt). This module's only public function is `run`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from actions import save_receipt

from ..captcha import solve_canvas_captcha
from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    fill,
    navigate,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
)
from ..types import RunOutcome, ScriptedAbort, StepLog, StepStatus
from .params import FetchReceiptParams


# ─── Selectors ─────────────────────────────────────────────────────────

URL_REPORT_PAGE = (
    "https://services.parivahan.gov.in/checkpostv4/#/public/reports/PaymentReceipt"
)

SEL_STATE_DROPDOWN = "select#inputState"
SEL_VEHICLE_INPUT = "input#inputVehicleNo"
SEL_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_CAPTCHA_INPUT = "input#inputcaptcha"
# Refresh button is a sibling of div#captcha (NOT a child — confirmed by
# the page HTML the user shared). Use adjacent-sibling combinator with a
# className fallback so a minor parivahan re-render doesn't break us.
SEL_CAPTCHA_REFRESH = "div#captcha + button, button.btn-primary.m-left"
SEL_GET_DETAILS_BTN = "button.go-but"

# Receipt results table
SEL_RESULTS_TBODY = "table.table-bordered tbody"

# After clicking print, the route changes to /public/reports/CustomerReceipt
URL_FRAGMENT_RECEIPT = "CustomerReceipt"


# ─── Tuning ────────────────────────────────────────────────────────────

CAPTCHA_SUBMIT_POLL_SECS = 10.0  # time to wait for table/popup after Get Details
TABLE_SETTLE_SECS = 1.0  # let Angular finish painting before we read rows
RECEIPT_RENDER_TIMEOUT_SECS = 30  # /CustomerReceipt fully rendered
RECEIPT_PAINT_SETTLE_SECS = 1.5  # let watermark + QR paint before printToPDF


# ─── Helpers ───────────────────────────────────────────────────────────

_CONVERT_DATE_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _iso_to_ddmmyyyy(iso: str) -> str:
    """2026-05-14 → 14/05/2026 (parivahan table format)."""
    m = _CONVERT_DATE_RX.match(iso)
    if not m:
        return iso
    y, mo, d = m.groups()
    return f"{d}/{mo}/{y}"


# JS: classify post-submit page state. The table has 10 columns; an empty
# tbody renders no <tr>, while a populated one renders one <tr> per row.
# We also scan for any visible alert/modal/toast carrying invalid-captcha
# or no-record text so we can distinguish "retry captcha" from "captcha
# was fine but no receipt exists".
_RESULTS_OR_POPUP_JS = """
(function() {
    var rows = document.querySelectorAll('table.table-bordered tbody tr');
    if (rows && rows.length > 0) {
        var first = rows[0].querySelectorAll('td');
        if (first.length >= 8) {
            return {state: 'results', count: rows.length};
        }
    }

    var alerts = document.querySelectorAll(
        '.modal.show, .alert, .toast.show, [role="alert"]'
    );
    for (var i = 0; i < alerts.length; i++) {
        var t = (alerts[i].textContent || '').toLowerCase();
        if (t.indexOf('invalid') !== -1
                || t.indexOf('wrong captcha') !== -1
                || t.indexOf('captcha') !== -1 && t.indexOf('match') !== -1) {
            return {state: 'invalid_captcha', text: alerts[i].textContent.trim()};
        }
        if (t.indexOf('no record') !== -1
                || t.indexOf('not found') !== -1
                || t.indexOf('no data') !== -1) {
            return {state: 'no_record', text: alerts[i].textContent.trim()};
        }
    }
    return {state: 'pending'};
})()
"""


# JS: dismiss any open modal/toast so the next captcha attempt is clean.
_DISMISS_POPUPS_JS = """
(function() {
    var btns = document.querySelectorAll(
        '.modal.show .btn-close, .modal.show button.btn-secondary,'
        + '.modal.show button.btn-primary, .toast.show .btn-close,'
        + '[role="alert"] .btn-close'
    );
    for (var i = 0; i < btns.length; i++) {
        try { btns[i].click(); } catch (e) {}
    }
})()
"""


# JS: scan rows of the results table, find the one matching either
# receiptNo (preferred when provided) or the leading date in the
# Payment Date cell. Returns {ok, rowIndex, receiptNo, payDate, totalFee}.
_MATCH_AND_EXTRACT_JS = """
(function(targetDate, targetReceiptNo) {
    var rows = document.querySelectorAll('table.table-bordered tbody tr');
    if (!rows || rows.length === 0) return {ok: false, reason: 'no_rows'};

    var dump = [];
    var matchIdx = -1;

    for (var i = 0; i < rows.length; i++) {
        var tds = rows[i].querySelectorAll('td');
        if (tds.length < 8) continue;
        var receiptNo = (tds[3].textContent || '').trim();
        var payDate   = (tds[4].textContent || '').trim();   // "14/05/2026, 2:04 PM"
        var totalFee  = (tds[7].textContent || '').trim();
        dump.push({i:i, receiptNo:receiptNo, payDate:payDate, totalFee:totalFee});

        // Prefer exact receiptNo match if caller provided one.
        if (targetReceiptNo && receiptNo === targetReceiptNo) {
            matchIdx = i;
            break;
        }
        // Otherwise match date prefix (before the comma).
        if (!targetReceiptNo && payDate.indexOf(targetDate) === 0) {
            if (matchIdx === -1) matchIdx = i;  // first match wins
        }
    }

    if (matchIdx === -1) return {ok: false, reason: 'no_match', dump: dump};

    var tds = rows[matchIdx].querySelectorAll('td');
    return {
        ok: true,
        rowIndex: matchIdx,
        receiptNo: (tds[3].textContent || '').trim(),
        payDate:   (tds[4].textContent || '').trim(),
        totalFee:  (tds[7].textContent || '').trim(),
        rowCount:  rows.length
    };
})
"""


# JS: click the print icon inside the row at index `rowIndex`. We click
# the icon's parent <td> rather than the <i> itself because Angular's
# routerLink handler is typically wired on the <td>, not the icon glyph.
_CLICK_PRINT_ICON_JS = """
(function(rowIndex) {
    var rows = document.querySelectorAll('table.table-bordered tbody tr');
    if (!rows || rowIndex >= rows.length) return {ok: false, reason: 'row_gone'};
    var row = rows[rowIndex];
    var icon = row.querySelector('i.fa-print');
    if (!icon) return {ok: false, reason: 'no_print_icon'};
    var clickable = icon.closest('td') || icon;
    try { clickable.click(); } catch (e) { return {ok: false, reason: 'click_threw:' + e.message}; }
    return {ok: true};
})
"""


# JS: check whether we've landed on the rendered customer-receipt page
# (URL hash flipped AND the receipt header text is in the DOM).
_RECEIPT_LOADED_JS = """
(function() {
    var url = window.location.href || '';
    if (url.toLowerCase().indexOf('customerreceipt') === -1) {
        return {state: 'not_navigated'};
    }
    var text = (document.body && document.body.textContent) || '';
    if (text.indexOf('GOVERNMENT OF') !== -1
            && (text.indexOf('Checkpost Tax e-Receipt') !== -1
                || text.indexOf('CHECKPOST TAX E-RECEIPT') !== -1
                || text.indexOf('Checkpost Tax E-Receipt') !== -1)) {
        return {state: 'rendered'};
    }
    return {state: 'navigated_but_not_painted'};
})()
"""


# ─── Public entry point ────────────────────────────────────────────────


async def run(
    session,
    params: FetchReceiptParams,
    log: StepLogger,
) -> RunOutcome:
    """Run the fetch-receipt scripted flow. Always returns a RunOutcome.

    Matches the contract of the border-tax state runners: takes
    (session, params, log), pulls job_id/redis off the log, returns a
    RunOutcome without raising.
    """

    job_id = log.job_id
    r = log.r
    job_params = params.model_dump()

    # ─── Phase 1: open the report page ────────────────────────────────
    await navigate(
        session,
        URL_REPORT_PAGE,
        log=log,
        name="phase1.open_report_page",
    )
    await wait_for_selector(
        session,
        SEL_STATE_DROPDOWN,
        log=log,
        name="phase1.wait_state_dropdown",
        timeout=30,
    )

    # ─── Phase 2: state + vehicle number ─────────────────────────────
    # FetchReceiptParams's validator already normalized stateName to a
    # 2-letter code matching the <option value="..."> attribute.
    await select_by_value(
        session,
        SEL_STATE_DROPDOWN,
        params.stateName,
        log=log,
        name="phase2.select_state",
    )
    await fill(
        session,
        SEL_VEHICLE_INPUT,
        params.vehicleNumber,
        log=log,
        name="phase2.fill_vehicle",
    )

    # ─── Phase 3: solve captcha + click Get Details ──────────────────

    async def _submit_get_details() -> bool:
        """SubmitAction for solve_canvas_captcha.

        Returns:
            True  → table populated, OR a 'no record' popup appeared
                    (captcha was accepted either way — outer flow handles
                    the no-record case explicitly).
            False → 'Invalid Captcha' popup or nothing happened — captcha
                    rejected, solver should refresh + retry.
        """
        # Click Get Details via JS click (more reliable than text-based
        # click for an Angular SPA — the button's class is unique).
        await _cdp_eval(
            session,
            "(function(){var b=document.querySelector("
            + json.dumps(SEL_GET_DETAILS_BTN)
            + ");if(b)b.click();return !!b;})()",
        )

        deadline = time.monotonic() + CAPTCHA_SUBMIT_POLL_SECS
        while time.monotonic() < deadline:
            state_res = await _cdp_eval(session, _RESULTS_OR_POPUP_JS)
            state = (state_res or {}).get("state", "pending")
            if state == "results":
                return True
            if state == "no_record":
                # Captcha was fine; vehicle simply has no receipts. Let
                # the row-match step below produce the 'receipt_not_found'
                # outcome — that's a more accurate error than retrying
                # the captcha 5 more times.
                return True
            if state == "invalid_captcha":
                await _cdp_eval(session, _DISMISS_POPUPS_JS)
                return False
            await asyncio.sleep(0.5)
        # Nothing decisive in 10s → treat as not-advanced. The captcha
        # solver will refresh + retry. Worst case: 5 retries × 10s = 50s,
        # then ScriptedAbort.
        return False

    try:
        await solve_canvas_captcha(
            session,
            canvas_selector=SEL_CAPTCHA_CANVAS,
            input_selector=SEL_CAPTCHA_INPUT,
            refresh_selector=SEL_CAPTCHA_REFRESH,
            submit_action=_submit_get_details,
            job_id=job_id,
            r=r,
            source=params.source,
            log=log,
            name="phase3.solve_captcha",
            max_ai_attempts=5,
        )
    except ScriptedAbort as abort:
        # source='app' path: AI captcha exhausted, no human fallback.
        return RunOutcome(
            status="failed",
            summary=(
                f"Captcha could not be solved after 5 AI attempts for "
                f"vehicle {params.vehicleNumber} ({params.stateName})."
            ),
            abort_reason=f"captcha_failed: {abort}",
            run_log=log.dump(),
        )
    except Exception as e:
        return RunOutcome(
            status="failed",
            summary=f"Captcha solver crashed: {type(e).__name__}: {e}",
            abort_reason="captcha_crashed",
            run_log=log.dump(),
        )

    # ─── Phase 4: find the matching row ──────────────────────────────
    # Give Angular a beat after captcha success — solve_canvas_captcha
    # returns the moment submit_action confirmed advance, but the table
    # rendering race is real on slow connections.
    await sleep_seconds(TABLE_SETTLE_SECS, log=log, name="phase4.settle_table")

    target_date = _iso_to_ddmmyyyy(params.paymentDate)
    target_receipt_no = params.receiptNo or ""

    phase4_started = time.monotonic()
    match_res = await _cdp_eval(
        session,
        _MATCH_AND_EXTRACT_JS
        + "("
        + json.dumps(target_date)
        + ","
        + json.dumps(target_receipt_no)
        + ")",
    )

    if not match_res or not match_res.get("ok"):
        reason = (match_res or {}).get("reason", "unknown")
        dump = (match_res or {}).get("dump")
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase4.match_row",
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - phase4_started) * 1000),
                error=f"reason={reason} rows_seen={dump}",
            )
        )
        return RunOutcome(
            status="failed",
            summary=(
                f"No receipt found on parivahan for vehicle "
                f"{params.vehicleNumber} on {params.paymentDate} "
                f"({params.stateName}). Either paymentDate is wrong or "
                f"parivahan hasn't published the receipt yet."
            ),
            abort_reason=f"receipt_not_found:{reason}",
            run_log=log.dump(),
        )

    row_index = int(match_res["rowIndex"])
    receipt_no = str(match_res["receiptNo"])
    pay_date_raw = str(match_res["payDate"])
    total_fee_raw = str(match_res["totalFee"])
    try:
        amount = float(total_fee_raw)
    except ValueError:
        amount = 0.0

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase4.match_row",
            status=StepStatus.OK,
            duration_ms=int((time.monotonic() - phase4_started) * 1000),
            value=(
                f"row={row_index}/{match_res.get('rowCount', '?')} "
                f"receiptNo={receipt_no} payDate={pay_date_raw!r} "
                f"amount={amount}"
            ),
        )
    )

    # ─── Phase 5: click the row's print icon ─────────────────────────
    click_res = await _cdp_eval(
        session,
        _CLICK_PRINT_ICON_JS + "(" + str(row_index) + ")",
    )
    if not click_res or not click_res.get("ok"):
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase5.click_print",
                status=StepStatus.FAILED,
                error=f"reason={(click_res or {}).get('reason', 'unknown')}",
            )
        )
        return RunOutcome(
            status="failed",
            summary=(
                f"Found row {row_index} (receipt {receipt_no}) but could "
                f"not click its print icon "
                f"(reason={(click_res or {}).get('reason', 'unknown')})."
            ),
            abort_reason="print_click_failed",
            run_log=log.dump(),
        )

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase5.click_print",
            status=StepStatus.OK,
            value=f"row={row_index} receipt={receipt_no}",
        )
    )

    # ─── Phase 6: wait for /CustomerReceipt to render ────────────────
    phase6_started = time.monotonic()
    deadline = time.monotonic() + RECEIPT_RENDER_TIMEOUT_SECS
    last_state = "?"
    while time.monotonic() < deadline:
        res = await _cdp_eval(session, _RECEIPT_LOADED_JS)
        last_state = (res or {}).get("state", "?")
        if last_state == "rendered":
            break
        await asyncio.sleep(0.5)

    if last_state != "rendered":
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase6.wait_receipt",
                status=StepStatus.FAILED,
                duration_ms=int((time.monotonic() - phase6_started) * 1000),
                error=f"last_state={last_state}",
            )
        )
        return RunOutcome(
            status="failed",
            summary=(
                f"CustomerReceipt page did not render within "
                f"{RECEIPT_RENDER_TIMEOUT_SECS}s for receipt {receipt_no} "
                f"(last_state={last_state})."
            ),
            abort_reason="receipt_page_did_not_load",
            run_log=log.dump(),
        )

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase6.wait_receipt",
            status=StepStatus.OK,
            duration_ms=int((time.monotonic() - phase6_started) * 1000),
        )
    )

    # Give the watermark + QR a moment to paint before printToPDF, or
    # the PDF will be missing them.
    await sleep_seconds(RECEIPT_PAINT_SETTLE_SECS, log=log, name="phase6.settle_paint")

    # ─── Phase 7: capture PDF + upload via the existing save_receipt ─
    save_data = {
        "vehicleNumber": params.vehicleNumber,
        "receiptNumber": receipt_no,
        "amount": amount,
        "paymentDate": params.paymentDate,  # YYYY-MM-DD
    }

    # save_receipt POSTs `params` (the job_params dict) to the API as the
    # multipart 'params' field; handleSaveReceipt reads driverId,
    # vehicleNumber, requestId, state from it to build the GCS path
    # (driverUtilitiesRequests/borderTaxRequests/{requestId}_{driverId}/...)
    # and to set the borderTaxPayments doc's state field. Make sure
    # `state` is present as a hint (the borderTaxRequests update doesn't
    # need it, but the borderTaxPayments doc does).
    api_params = dict(job_params)
    api_params.setdefault("state", params.stateName)

    save_resp = await save_receipt(session, job_id, api_params, save_data)

    if not save_resp.get("ok"):
        return RunOutcome(
            status="failed",
            summary=(
                f"save_receipt API call failed: "
                f"{save_resp.get('error', 'unknown error')}"
            ),
            abort_reason=f"save_receipt_failed:{save_resp.get('error', '')}",
            run_log=log.dump(),
        )

    if not save_resp.get("pdfUploaded"):
        # Receipt metadata saved but PDF didn't make it to GCS — degrade
        # to partial so the API still marks the request as completed but
        # client knows the file is missing.
        return RunOutcome(
            status="partial",
            summary=(
                f"Receipt metadata saved (receipt {receipt_no}) but PDF "
                f"upload to GCS failed: "
                f"{save_resp.get('pdfUploadError', 'unknown')}"
            ),
            partial_reasons=["pdf_upload_failed"],
            run_log=log.dump(),
        )

    return RunOutcome(
        status="done",
        summary=(
            f"Fetched receipt {receipt_no} for vehicle "
            f"{params.vehicleNumber} (state={params.stateName}, "
            f"amount=Rs.{amount}, paymentDate={params.paymentDate}). "
            f"PDF uploaded to GCS."
        ),
        run_log=log.dump(),
    )

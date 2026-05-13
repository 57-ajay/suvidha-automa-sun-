# worker/src/scripted/border_tax/up.py
"""Uttar Pradesh border-tax scripted runner.

Walks the 10 phases mapped against api/src/tasks/borderTax/states/up.ts:

    Phase 1  - parivahan.gov.in/en/node/579, select UP
    Phase 2  - service selection, click Go
    Phase 3  - Owner Information: vehicle, Get Details, district, checkpoint
    Phase 4  - Vehicle Information: Service Type
    Phase 5  - Tax Information: Tax Mode, Tax From, Tax Upto, Calculate
    Phase 6  - Disclaimer: CAPTCHA + checkbox + popups + Pay Online + Yes
    Phase 7  - Payment Gateway: select SBI, accept terms, Submit
    Phase 8  - SBIePay Lite: click UPI option
    Phase 8b - SBIePay Lite: click yellow CONFIRM (input#Go)
    Phase 9  - QR page: save_qr_code + wait_for_human
    Phase 10 - Receipt: poll for receipt page, extract fields, save_receipt PDF

UPI-only. NetBanking falls back to the AI agent path (run_job.py decides).

Selectors are centralized at the top of this file so the first test-run
fixups are find-one-fix-one. Anything marked "best guess" needs to be
confirmed in the first live run -- the run log will show selector mismatches
clearly because each step records its CSS selector.
"""

from __future__ import annotations

import asyncio
import re
import time

from actions import save_qr_code, save_receipt

from ..captcha import (
    _wait_for_human_via_redis as wait_for_human_via_redis,
    solve_canvas_captcha,
)
from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    _current_url,
    abort_if_popup_text,
    click,
    click_by_text,
    fill,
    navigate,
    select_by_text,
    select_by_value,
    sleep_seconds,
    wait_for_selector,
    wait_for_url,
)
from ..types import RunOutcome, StepLog, StepStatus
from .params import BorderTaxParams


# ─── Selectors (confirm in first run) ──────────────────────────────────

# Phase 1 — parivahan landing
SEL_STATE_DROPDOWN = "select.select-css-check-post-services"

# Phase 2 — service selection
SEL_SERVICE_DROPDOWN = "select[name='serviceName']"

# Phase 3 — owner info
SEL_VEHICLE_INPUT = "input#floatingvehicle"
SEL_DISTRICT = "select#floatingDistrict"
# best guess; will be confirmed live
SEL_CHECKPOINT = "select#floatingCheckpost"

# Phase 4 — vehicle info
SEL_SERVICE_TYPE = "select#floatingService"

# Phase 5 — tax info
SEL_TAX_FROM = "input#floatingTaxfrom"
SEL_TAX_UPTO = "input#uptpDate"
# best guess; if missing we'll see it in the run log and patch
SEL_TAX_MODE_CANDIDATES = [
    "select#floatingTaxMode",
    "select[name='taxmode']",
    "select[name='taxMode']",
]

# Phase 6 — disclaimer / captcha
SEL_CAPTCHA_CANVAS = "div#captcha canvas"
SEL_CAPTCHA_INPUT = "input#inputcap"
SEL_CAPTCHA_REFRESH = "button[data-bs-original-title*='generate']"

# Phase 7 — payment gateway
SEL_PG_DROPDOWN = "select#dropOperator"

# Phase 8 — SBIePay UPI
SEL_UPI_LINK = "a#paymoderadio"

# Phase 8b — SBIePay confirm payment details
SEL_SBIEPAY_CONFIRM = "input#Go.btn-Yellow"

# Phase 9 — QR page
SEL_QR_IMG = "img#qrcodeImg"


# ─── Tuning ────────────────────────────────────────────────────────────

PHASE_GAP_SECS = 1.5             # polite breath between phases
HUMAN_PAYMENT_TIMEOUT = 600      # 10 minutes for UPI payment
RECEIPT_POLL_TIMEOUT_SECS = 90   # wait up to 90s for receipt after payment


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run UP scripted border-tax. Always returns a RunOutcome."""

    job_id = log.job_id
    r = log.r
    # actions.save_qr_code / save_receipt expect the raw params dict that
    # was queued. We re-derive it from the validated model so anything the
    # downstream save endpoints look for (driverId, requestId, etc.) is
    # present.
    job_params = params.model_dump()

    # ─── Phase 1: parivahan landing ────────────────────────────────────
    await navigate(
        session, "https://parivahan.gov.in/en/node/579",
        log=log, name="phase1.open_parivahan",
    )
    await wait_for_selector(
        session, SEL_STATE_DROPDOWN,
        log=log, name="phase1.wait_state_dropdown",
    )
    await select_by_value(
        session, SEL_STATE_DROPDOWN, "UP",
        log=log, name="phase1.select_state_up",
    )
    await wait_for_url(
        session, "checkpostv4",
        log=log, name="phase1.wait_service_page",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase1.settle")

    # ─── Phase 2: service selection ────────────────────────────────────
    await wait_for_selector(
        session, SEL_SERVICE_DROPDOWN,
        log=log, name="phase2.wait_service_dropdown",
    )
    await select_by_text(
        session, SEL_SERVICE_DROPDOWN,
        "VEHICLE TAX COLLECTION (OTHER STATE)",
        log=log, name="phase2.select_service",
    )
    await click_by_text(
        session, "Go",
        log=log, name="phase2.click_go", tag="button",
    )
    await wait_for_url(
        session, "taxCollectionOnline",
        log=log, name="phase2.wait_owner_info_page",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase2.settle")

    # ─── Phase 3: owner information ────────────────────────────────────
    await wait_for_selector(
        session, SEL_VEHICLE_INPUT,
        log=log, name="phase3.wait_vehicle_input",
    )
    await fill(
        session, SEL_VEHICLE_INPUT, params.vehicleNumber,
        log=log, name="phase3.fill_vehicle",
    )
    await click_by_text(
        session, "Get Details",
        log=log, name="phase3.click_get_details", tag="button",
    )
    # Vehicle details populate => district dropdown becomes visible
    await wait_for_selector(
        session, SEL_DISTRICT,
        log=log, name="phase3.wait_details_loaded", timeout=25,
    )

    if not params.entryDistrict:
        return RunOutcome(
            status="failed",
            summary="entryDistrict param is required for UP",
            abort_reason="missing_param:entryDistrict",
            run_log=log.dump(),
        )

    await select_by_text(
        session, SEL_DISTRICT, params.entryDistrict,
        log=log, name="phase3.select_district",
    )

    if params.entryCheckpoint:
        # The checkpost dropdown populates after district is picked; wait
        # a beat so its options are ready.
        await asyncio.sleep(1.0)
        await select_by_text(
            session, SEL_CHECKPOINT, params.entryCheckpoint,
            log=log, name="phase3.select_checkpoint",
        )

    await click_by_text(
        session, "Next",
        log=log, name="phase3.click_next", tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase3.settle")

    # ─── Phase 4: vehicle information ──────────────────────────────────

    # The validity popup (insurance / fitness / PUCC expired) can fire on
    # page load OR after Service Type selection. We check both times.
    _validity_keywords = ["INSURANCE", "FITNESS", "PUCC", "EXPIRED", "RENEW"]
    _validity_abort = (
        f"Vehicle {params.vehicleNumber} has no valid insurance/fitness/"
        f"PUCC. Please renew before attempting border tax payment."
    )

    await abort_if_popup_text(
        session, _validity_keywords, _validity_abort,
        log=log, name="phase4.check_validity_on_load",
        close_selector=(
            "button.swal2-confirm, .modal-footer button, "
            ".swal-button--confirm, button:nth-of-type(1)"
        ),
    )
    await wait_for_selector(
        session, SEL_SERVICE_TYPE,
        log=log, name="phase4.wait_vehicle_info_page",
    )
    await select_by_text(
        session, SEL_SERVICE_TYPE, params.serviceType,
        log=log, name="phase4.select_service_type",
    )
    await asyncio.sleep(1.0)
    await abort_if_popup_text(
        session, _validity_keywords, _validity_abort,
        log=log, name="phase4.check_validity_after_service",
        close_selector=(
            "button.swal2-confirm, .modal-footer button, "
            ".swal-button--confirm"
        ),
    )
    await click_by_text(
        session, "Next",
        log=log, name="phase4.click_next", tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase4.settle")

    # ─── Phase 5: tax information ──────────────────────────────────────
    await wait_for_selector(
        session, SEL_TAX_FROM,
        log=log, name="phase5.wait_tax_info_page",
    )

    # Tax Mode select -- try a few candidate selectors; the first hit wins.
    tax_mode_set = False
    last_err: Exception | None = None
    for candidate in SEL_TAX_MODE_CANDIDATES:
        try:
            await select_by_text(
                session, candidate, params.taxMode,
                log=log, name=f"phase5.select_tax_mode[{candidate}]",
                timeout=3,
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
                f"Could not find Tax Mode dropdown. Last attempt error: "
                f"{type(last_err).__name__}: {last_err}"
            ),
            abort_reason="selector_not_found:tax_mode",
            run_log=log.dump(),
        )

    # type="date" inputs accept ISO YYYY-MM-DD via the native value setter
    await fill(
        session, SEL_TAX_FROM, params.taxFrom,
        log=log, name="phase5.fill_tax_from",
    )
    await fill(
        session, SEL_TAX_UPTO, params.taxUpto,
        log=log, name="phase5.fill_tax_upto",
    )
    await click_by_text(
        session, "Calculate Fee/Tax",
        log=log, name="phase5.click_calculate", tag="button",
    )
    # The site computes server-side; allow time for the total to populate
    await sleep_seconds(4, log=log, name="phase5.wait_calculation")
    await click_by_text(
        session, "Next",
        log=log, name="phase5.click_next", tag="button",
    )
    await sleep_seconds(PHASE_GAP_SECS, log=log, name="phase5.settle")

    # ─── Phase 6: disclaimer + captcha + popups ────────────────────────
    await wait_for_selector(
        session, SEL_CAPTCHA_CANVAS,
        log=log, name="phase6.wait_disclaimer_page",
    )

    async def _submit_disclaimer() -> bool:
        """Submit-after-captcha action for the captcha helper.

        Steps:
          (a) Tick the 'I confirm...' checkbox.
          (b) Dismiss the summary popup that fires on checkbox click.
          (c) Click 'Pay Online'.
          (d) Click 'Yes' on the 'Are you sure?' confirm popup.
          (e) Watch the URL for ~15s; return True iff we're on the
              payment gateway, False if we're still on the disclaimer
              (captcha was rejected by the server).
        """
        # (a) Click the checkbox via its associated label text
        await _cdp_eval(session, """
            (function() {
                var labels = document.querySelectorAll('label');
                for (var i = 0; i < labels.length; i++) {
                    var t = (labels[i].textContent || '').toUpperCase();
                    if (t.indexOf('I CONFIRM') >= 0) {
                        labels[i].click();
                        return true;
                    }
                }
                // Fallback: first unchecked checkbox
                var cbs = document.querySelectorAll('input[type=checkbox]');
                for (var j = 0; j < cbs.length; j++) {
                    if (!cbs[j].checked) { cbs[j].click(); return true; }
                }
                return false;
            })()
        """)

        # (b) Dismiss the summary popup
        await asyncio.sleep(1.2)
        await _cdp_eval(session, """
            (function() {
                var btns = document.querySelectorAll('button, .btn');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].textContent || '').trim().toUpperCase();
                    if (t === 'CLOSE') {
                        var r = btns[i].getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            btns[i].click(); return true;
                        }
                    }
                }
                return false;
            })()
        """)
        await asyncio.sleep(0.5)

        # (c) Pay Online
        await _cdp_eval(session, """
            (function() {
                var btns = document.querySelectorAll('button, .btn');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].textContent || '').trim().toUpperCase();
                    if (t.indexOf('PAY ONLINE') >= 0) {
                        var r = btns[i].getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            btns[i].click(); return true;
                        }
                    }
                }
                return false;
            })()
        """)

        # (d) Yes on the confirm popup
        await asyncio.sleep(1.2)
        await _cdp_eval(session, """
            (function() {
                var btns = document.querySelectorAll('button, .btn');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].textContent || '').trim().toUpperCase();
                    if (t === 'YES') {
                        var r = btns[i].getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            btns[i].click(); return true;
                        }
                    }
                }
                return false;
            })()
        """)

        # (e) Watch URL for navigation to payment gateway
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            url = (await _current_url(session)).lower()
            if (
                "etranspgi" in url
                or "vahanpgi" in url
                or "paymentgateway" in url
            ):
                return True
            await asyncio.sleep(0.5)
        return False

    await solve_canvas_captcha(
        session,
        canvas_selector=SEL_CAPTCHA_CANVAS,
        input_selector=SEL_CAPTCHA_INPUT,
        refresh_selector=SEL_CAPTCHA_REFRESH,
        submit_action=_submit_disclaimer,
        job_id=job_id,
        r=r,
        source=params.source,
        log=log,
        name="phase6.solve_captcha",
        max_ai_attempts=5,
    )

    # ─── Phase 7: payment gateway ──────────────────────────────────────
    await wait_for_selector(
        session, SEL_PG_DROPDOWN,
        log=log, name="phase7.wait_payment_gateway", timeout=20,
    )
    await select_by_value(
        session, SEL_PG_DROPDOWN, "SBI",
        log=log, name="phase7.select_sbi",
    )
    # Tick the "I accept terms and conditions" checkbox
    await _cdp_eval(session, """
        (function() {
            var cbs = document.querySelectorAll('input[type=checkbox]');
            for (var i = 0; i < cbs.length; i++) {
                if (!cbs[i].checked) { cbs[i].click(); break; }
            }
        })()
    """)
    await click_by_text(
        session, "Submit",
        log=log, name="phase7.click_submit", tag="button",
    )
    await sleep_seconds(2, log=log, name="phase7.settle")

    # ─── Phase 8: SBIePay Lite welcome -> click UPI ────────────────────
    await wait_for_selector(
        session, SEL_UPI_LINK,
        log=log, name="phase8.wait_sbiepay_welcome", timeout=30,
    )
    # SEL_UPI_LINK matches a#paymoderadio; the screenshot shows the UPI
    # tile in the "Other Payment Modes" column has this exact id.
    await click(
        session, SEL_UPI_LINK,
        log=log, name="phase8.click_upi", timeout=10,
    )
    await sleep_seconds(2, log=log, name="phase8.settle")

    # ─── Phase 8b: SBIePay confirm payment details ─────────────────────
    await wait_for_selector(
        session, SEL_SBIEPAY_CONFIRM,
        log=log, name="phase8b.wait_confirm_page", timeout=30,
    )
    await click(
        session, SEL_SBIEPAY_CONFIRM,
        log=log, name="phase8b.click_confirm",
    )
    await sleep_seconds(2, log=log, name="phase8b.settle")

    # ─── Phase 9: QR page + human pays via UPI ─────────────────────────
    await wait_for_selector(
        session, SEL_QR_IMG,
        log=log, name="phase9.wait_qr_page", timeout=30,
    )

    # Upload the QR image to your storage (same code path as the AI tool)
    qr_started = time.monotonic()
    qr_result = await save_qr_code(session, job_id, job_params)
    log.record(StepLog(
        index=log.next_index(),
        name="phase9.save_qr_code",
        status=(
            StepStatus.OK if qr_result.get("ok") else StepStatus.FAILED
        ),
        duration_ms=int((time.monotonic() - qr_started) * 1000),
        error=None if qr_result.get("ok") else qr_result.get("error"),
    ))
    # NOTE: a QR upload failure must NEVER block payment -- the user can
    # still scan the on-screen QR. We log the failure and proceed.

    human_started = time.monotonic()
    payment_reason = (
        f"UPI payment required for border tax of vehicle "
        f"{params.vehicleNumber}. A QR code is displayed on screen -- "
        f"please scan with your UPI app and complete the payment. "
        f"After payment is successful, reply 'done' to continue."
    )
    human_reply = await wait_for_human_via_redis(
        job_id, r, payment_reason, timeout=HUMAN_PAYMENT_TIMEOUT,
    )
    log.record(StepLog(
        index=log.next_index(),
        name="phase9.wait_for_human_payment",
        status=(
            StepStatus.HANDED_OFF if human_reply else StepStatus.FAILED
        ),
        duration_ms=int((time.monotonic() - human_started) * 1000),
        value=human_reply[:80] if human_reply else None,
        handoff_reason="upi_payment",
        handoff_summary=(
            human_reply[:300] if human_reply else "human_timeout"
        ),
    ))
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

    # ─── Phase 10: poll for receipt, extract, save PDF ─────────────────
    receipt_ready = False
    deadline = time.monotonic() + RECEIPT_POLL_TIMEOUT_SECS
    while time.monotonic() < deadline:
        markers = await _cdp_eval(session, """
            (function() {
                var text = (document.body.innerText || '').toUpperCase();
                return {
                    govHeader:     text.indexOf('GOVERNMENT OF UTTAR PRADESH') >= 0,
                    receiptHeader: text.indexOf('CHECKPOST TAX E-RECEIPT') >= 0,
                    receiptNo:     text.indexOf('RECEIPT NO') >= 0,
                    grandTotal:    text.indexOf('GRAND TOTAL') >= 0
                };
            })()
        """)
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
        session, job_id, job_params, receipt_data,
    )
    log.record(StepLog(
        index=log.next_index(),
        name="phase10.save_receipt",
        status=(
            StepStatus.OK if save_result.get("ok") else StepStatus.FAILED
        ),
        duration_ms=int((time.monotonic() - save_started) * 1000),
        value=receipt_data.get("receiptNumber"),
        error=None if save_result.get("ok") else save_result.get("error"),
    ))

    if save_result.get("ok") and save_result.get("pdfUploaded", True):
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax paid for vehicle {params.vehicleNumber} into "
                f"Uttar Pradesh. Receipt {receipt_data['receiptNumber']}, "
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
    """Scrape receiptNumber, amount, paymentDate from the receipt page's
    rendered text. Robust to layout changes -- uses label regex rather than
    specific DOM selectors. Returns None if any required field is missing."""

    text = await _cdp_eval(session, "document.body.innerText || ''")
    if not text:
        return None

    receipt_match = re.search(
        r"Receipt\s*No\.?\s*:?\s*([A-Z0-9]+)",
        text, re.IGNORECASE,
    )
    receipt_number = receipt_match.group(1) if receipt_match else None

    amount_match = re.search(
        r"Grand\s*Total\s*:?\s*(\d+(?:\.\d+)?)",
        text, re.IGNORECASE,
    )
    amount = float(amount_match.group(1)) if amount_match else None

    date_match = re.search(
        r"Payment\s*Confirmation\s*Date\s*:?\s*(\d{1,2}-\w{3}-\d{4})",
        text, re.IGNORECASE,
    )
    payment_date = (
        _normalize_receipt_date(date_match.group(1))
        if date_match else None
    )

    if not (receipt_number and amount is not None and payment_date):
        print(
            f"[up] receipt parse incomplete: "
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

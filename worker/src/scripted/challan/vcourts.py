"""Virtual Courts (vcourts.gov.in) scripted challan-payment flow.

Page flow (known from the challan-settlement task, which drives this same
site for extraction):

  VC_HOME    index.php — "Select Department" dropdown + "Proceed Now" button.
             The sidebar tabs do NOT work until a department is chosen and
             Proceed Now is clicked.
  VC_SEARCH  After Proceed Now — header shows the department name. The
             "Challan/Vehicle No." tab exposes:
                 Challan Number | Vehicle Number | CAPTCHA image | Submit
  VC_RESULTS "No. of Records :- N" + numbered records, each with a header bar
             (Sr.No | Case No. | Challan No. | Party | Mobile | View) and a
             detail table.

HUMAN-IN-THE-LOOP at exactly two points:
  Phase 3  CAPTCHA — we pre-fill Challan + Vehicle (deterministic), then the
           human reads the captcha, types it, clicks Submit, and replies
           "done". We resume on VC_RESULTS. The human only ever does the
           captcha + Submit, nothing else.
  Phase 5  PAYMENT — we reach the pay page, then the human completes payment.

================================  IMPORTANT  ================================
Selectors marked `# TODO` are PLACEHOLDERS. The page flow below is correct,
but the exact DOM ids/names must be confirmed against the LIVE page:
  1. Trigger a job (see how-to in chat), let it reach VC_SEARCH.
  2. Open the job's liveUrl (noVNC) — you'll see the headful browser.
  3. Right-click each field -> Inspect -> copy its id or name attribute.
  4. Replace the TODO selectors below.
A wrong selector makes the matching `fill`/`click` step fail with a clear
error in the step log (it names the selector), so iteration is fast.
============================================================================
"""

from __future__ import annotations

import asyncio
import json
import time

import redis

from actions import save_qr_code, save_receipt  # save_receipt: used in Phase 6 TODO
from ..captcha import _wait_for_human_via_redis as wait_for_human_via_redis
from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    click,  # used once you wire the record's View/Pay control
    click_by_text,
    fill,
    navigate,
    select_by_text,
)
from ..types import RunOutcome, StepLog, StepStatus
from .params import ChallanPaymentParams


VC_HOME = "https://vcourts.gov.in/virtualcourt/index.php"

# ── Selectors ──────────────────────────────────────────────────────────────
# select_by_text scans ALL <select> elements and picks the one whose options
# contain the target text, so a broad "select" works WITHOUT the exact id.
SEL_DEPARTMENT_DROPDOWN = "select"

# VC_SEARCH — confirmed from live DOM.
# The tab is an <a> whose label is in a nested <p>, so textContent match misses;
# target its id directly. Clicking it activates the #nav-sbPoliceStation pane
# that holds #challan_no.
SEL_TAB_CHALLAN_VEHICLE = "a#mainMenuActive_police"  # "Challan/Vehicle No." tab
SEL_CHALLAN_INPUT = "input#challan_no"
# (no vehicle field in the challan-number search; add SEL_VEHICLE_INPUT if needed)

# ── Tunables ─────────────────────────────────────────────────────────────
HUMAN_CAPTCHA_TIMEOUT = 600  # seconds the operator has to solve+submit
HUMAN_PAYMENT_TIMEOUT = 600  # seconds: human does OTP + payment in one handover
SEARCH_HEADER_TIMEOUT = 20  # wait for VC_SEARCH to render after Proceed
RESULTS_POLL_TIMEOUT = 30  # wait for VC_RESULTS after captcha+submit


async def _page_has_text(session, needle: str) -> bool:
    """True if `needle` (case-insensitive) appears in the page body text."""
    expr = (
        "(function(n){return ((document.body&&document.body.innerText)||'')"
        ".toUpperCase().indexOf(n.toUpperCase())>=0;})(" + json.dumps(needle) + ")"
    )
    return bool(await _cdp_eval(session, expr))


async def run(
    session,
    params: ChallanPaymentParams,
    log: StepLogger,
    r: redis.Redis,
    job_id: str,
    department: str,
) -> RunOutcome:
    veh = params.vehicleNumber
    challan = params.challanNo

    # ── Phase 1: VC_HOME -> select department -> Proceed Now ────────────────
    await navigate(session, VC_HOME, log=log, name="vc.home")

    await select_by_text(
        session,
        SEL_DEPARTMENT_DROPDOWN,
        department,
        log=log,
        name="vc.select_department",
    )

    # "Proceed Now" may be a <button>, <input type=submit>, or <a>.
    # click_by_text matches on textContent, which works for <button>/<a>.
    # If it's an <input type=submit value="Proceed Now"> (no textContent),
    # replace this with: await click(session, "input[value='Proceed Now']", ...)
    await click_by_text(session, "Proceed Now", log=log, name="vc.proceed_now")

    # Verify we reached VC_SEARCH — header should contain the department
    # name (the part before any "(").
    dept_head = department.split("(")[0].strip()
    deadline = time.monotonic() + SEARCH_HEADER_TIMEOUT
    reached = False
    while time.monotonic() < deadline:
        if await _page_has_text(session, dept_head):
            reached = True
            break
        await asyncio.sleep(0.5)
    if not reached:
        return RunOutcome(
            status="failed",
            summary=f"'Proceed Now' did not reach the search page for {department}.",
            abort_reason="proceed_failed",
            run_log=log.dump(),
        )

    # ── Phase 2: activate the Challan/Vehicle tab, fill the challan number ──
    # Click the tab first (activates #nav-sbPoliceStation pane holding
    # #challan_no), then fill — `fill` waits for the input to be visible.
    await click(
        session, SEL_TAB_CHALLAN_VEHICLE, log=log, name="vc.tab_challan_vehicle"
    )
    await fill(session, SEL_CHALLAN_INPUT, challan, log=log, name="vc.fill_challan")

    # ── Phase 3: CAPTCHA = human checkpoint ─────────────────────────────────
    captcha_reason = (
        f"Virtual Courts ({department}): challan {challan} is filled. "
        f"Please read the CAPTCHA shown on screen, type it into 'Enter Captcha', "
        f"click Submit, then reply 'done'."
    )
    t0 = time.monotonic()
    reply = await wait_for_human_via_redis(
        job_id,
        r,
        captcha_reason,
        timeout=HUMAN_CAPTCHA_TIMEOUT,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="vc.captcha_human",
            status=StepStatus.HANDED_OFF if reply else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - t0) * 1000),
            handoff_reason="captcha",
            handoff_summary=(reply[:300] if reply else "human_timeout"),
        )
    )
    if not reply:
        return RunOutcome(
            status="partial",
            summary="CAPTCHA wait timed out; challan was not submitted.",
            partial_reasons=["human_timeout:captcha"],
            run_log=log.dump(),
        )

    # ── Phase 4: confirm results, detect not-found ──────────────────────────
    deadline = time.monotonic() + RESULTS_POLL_TIMEOUT
    got_results = False
    while time.monotonic() < deadline:
        if await _page_has_text(session, "No. of Records"):
            got_results = True
            break
        await asyncio.sleep(1)
    if not got_results:
        return RunOutcome(
            status="partial",
            summary="No results page after captcha/submit.",
            partial_reasons=["no_results"],
            run_log=log.dump(),
        )

    if await _page_has_text(session, "does not exist"):
        return RunOutcome(
            status="failed",
            summary=f"Challan {challan} not found in {department}.",
            abort_reason="not_found",
            run_log=log.dump(),
        )

    # ── Phase 4b: open the matching record ──────────────────────────────
    # Searched by exact challan number → single record → the lone green
    # "View" button is the right one.
    await click_by_text(session, "View", tag="button", log=log, name="vc.open_record")

    # ── Phase 4c: select the verify radio — reveals mobile + chassis fields ──
    await click(session, "#incorrectsubmit", log=log, name="vc.reveal_input_fields")

    # ── Phase 4d: fill mobile number AND chassis last-4 (no Get-OTP / verify) ──
    if not params.phoneNo:
        return RunOutcome(
            status="failed",
            summary="phoneNo is required for the OTP path.",
            abort_reason="missing_phone",
            run_log=log.dump(),
        )
    await fill(session, "#otp_mobile_ce", params.phoneNo, log=log, name="vc.fill_otp_mobile")

    chassis_last4 = (params.chassisNo or "").strip()[-4:]
    if not chassis_last4:
        return RunOutcome(
            status="failed",
            summary="chassisNo is required (need its last 4 digits).",
            abort_reason="missing_chassis",
            run_log=log.dump(),
        )
    await fill(session, "#fcha_no_add", chassis_last4, log=log, name="vc.fill_chassis_last4")

    # If a UPI QR renders on the pay page, capture it so the client can show
    # it. Best-effort — never block payment on this.
    try:
        await save_qr_code(session, job_id, params.model_dump())
    except Exception:
        pass

    pay_reason = (
        f"Virtual Courts ({department}): challan {challan}, mobile {params.phoneNo} is filled. "
        f"Click 'Get OTP', enter the OTP received on {params.phoneNo}, verify, then complete "
        f"the payment in the browser, and reply 'done'."
    )
    t1 = time.monotonic()
    pay_reply = await wait_for_human_via_redis(
        job_id,
        r,
        pay_reason,
        timeout=HUMAN_PAYMENT_TIMEOUT,
    )
    log.record(
        StepLog(
            index=log.next_index(),
            name="vc.payment_human",
            status=StepStatus.HANDED_OFF if pay_reply else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - t1) * 1000),
            handoff_reason="payment",
            handoff_summary=(pay_reply[:300] if pay_reply else "human_timeout"),
        )
    )
    if not pay_reply:
        return RunOutcome(
            status="partial",
            summary="Payment wait timed out. Money may be deducted; reconcile manually.",
            partial_reasons=["human_timeout:payment"],
            run_log=log.dump(),
        )

    # ── Phase 6: receipt capture ─────────────────────────────────────────────────
    #
    # The vcourts receipt page is identified by the Print button:
    #   <button class="btn btn-primary btn-sm" onclick="window.print();">
    #     <i class="fas fa-print" ...></i>&nbsp;Print
    #   </button>
    #
    # We:
    #   1. Poll for that button (≤ 60 s) to confirm the receipt page is loaded.
    #   2. Read receiptNumber, amount, and paymentDate from the DOM.
    #   3. Call save_receipt → /api/internal/challan-payment/save-receipt.
    #   4. Return done / partial depending on the result.

    # ── 6a: poll for receipt page ────────────────────────────────────────────────
    _RECEIPT_READY_JS = """
    (function() {
      // The receipt page has a "window.print()" onclick Print button.
      var btns = document.querySelectorAll('button.btn-primary.btn-sm');
      for (var i = 0; i < btns.length; i++) {
        var oc = (btns[i].getAttribute('onclick') || '');
        if (oc.indexOf('window.print') !== -1) {
          return { found: true };
        }
      }
      return { found: false };
    })();
    """

    # JS to scrape the three receipt fields we need.
    _RECEIPT_DATA_JS = """
    (function() {
      var text = document.body.innerText || '';

      // ── Receipt Number ──
      // vcourts shows e.g. "Receipt No. : RC/2024/12345" or "Receipt No:RC/2024/12345"
      var rcMatch = text.match(/Receipt\\s*No\\.?\\s*:?\\s*([A-Z0-9\\/\\-]+)/i);
      var receiptNumber = rcMatch ? rcMatch[1].trim() : null;

      // ── Amount ──
      // "Total Amount : 1000" / "Total Amount: ₹1,000" / "Grand Total : 1000/-"
      var amtMatch = text.match(/(?:Total\\s*Amount|Grand\\s*Total)\\s*:?\\s*[₹Rs\\.]*\\s*([\\d,]+)/i);
      var amount = amtMatch ? parseInt(amtMatch[1].replace(/,/g,''), 10) : null;

      // ── Payment Date ──
      // e.g. "Payment Date : 15/06/2024" or "Date of Payment : 2024-06-15"
      var dtMatch = text.match(/(?:Payment\\s*Date|Date\\s*of\\s*Payment)\\s*:?\\s*([\\d\\-\\/]+)/i);
      var rawDate = dtMatch ? dtMatch[1].trim() : null;

      // Normalise to YYYY-MM-DD
      var paymentDate = null;
      if (rawDate) {
        // DD/MM/YYYY → YYYY-MM-DD
        var slashMatch = rawDate.match(/^(\\d{1,2})[\\/-](\\d{1,2})[\\/-](\\d{4})$/);
        if (slashMatch) {
          paymentDate = slashMatch[3] + '-' +
                        slashMatch[2].padStart(2,'0') + '-' +
                        slashMatch[1].padStart(2,'0');
        } else {
          // Already ISO or similar
          paymentDate = rawDate;
        }
      }

      return { receiptNumber: receiptNumber, amount: amount, paymentDate: paymentDate };
    })();
    """

    RECEIPT_POLL_SECS = 60
    RECEIPT_POLL_INTERVAL = 5
    RECEIPT_PAINT_SETTLE_SECS = 2

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase6.receipt_wait.start",
            status=StepStatus.OK,
            value=f"polling up to {RECEIPT_POLL_SECS}s for Print button",
        )
    )

    phase6_start = time.monotonic()
    receipt_page_found = False
    deadline = phase6_start + RECEIPT_POLL_SECS

    while time.monotonic() < deadline:
        try:
            res = await _cdp_eval(session, _RECEIPT_READY_JS)
            if res and res.get("found"):
                receipt_page_found = True
                break
        except Exception:
            pass
        await asyncio.sleep(RECEIPT_POLL_INTERVAL)

    elapsed_ms = int((time.monotonic() - phase6_start) * 1000)

    if not receipt_page_found:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase6.receipt_wait",
                status=StepStatus.FAILED,
                duration_ms=elapsed_ms,
                error=f"receipt page (Print button) did not appear within {RECEIPT_POLL_SECS}s",
            )
        )
        return RunOutcome(
            status="partial",
            summary=(
                f"Challan {challan} ({department}): payment confirmed by human but "
                f"receipt page did not load within {RECEIPT_POLL_SECS}s. "
                f"Payment likely succeeded — reconcile manually.\n"
                f"Challan: {challan}\nDepartment: {department}\n"
                f"Vehicle: {params.vehicleNumber}\nReceipt PDF: not uploaded — receipt page timeout\n"
                f"Status: partial"
            ),
            partial_reasons=["receipt_page_timeout"],
            run_log=log.dump(),
        )

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase6.receipt_wait",
            status=StepStatus.OK,
            duration_ms=elapsed_ms,
            value="Print button found — receipt page loaded",
        )
    )

    # Let the page finish painting (watermarks, etc.) before PDF capture.
    await asyncio.sleep(RECEIPT_PAINT_SETTLE_SECS)

    # ── 6b: read receipt fields ───────────────────────────────────────────────────
    receipt_fields: dict = {}
    try:
        receipt_fields = await _cdp_eval(session, _RECEIPT_DATA_JS) or {}
    except Exception as e:
        log.record(
            StepLog(
                index=log.next_index(),
                name="phase6.receipt_read",
                status=StepStatus.FAILED,
                error=f"CDP eval failed: {e}",
            )
        )
        receipt_fields = {}

    receipt_number = receipt_fields.get("receiptNumber")
    receipt_amount = receipt_fields.get("amount")
    receipt_date   = receipt_fields.get("paymentDate") or time.strftime("%Y-%m-%d")

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase6.receipt_read",
            status=StepStatus.OK if receipt_number else StepStatus.FAILED,
            value=f"receiptNumber={receipt_number!r} amount={receipt_amount!r} date={receipt_date!r}",
        )
    )

    if not receipt_number or receipt_amount is None:
        return RunOutcome(
            status="partial",
            summary=(
                f"Challan {challan} ({department}): receipt page loaded but could not "
                f"read required fields (receiptNumber={receipt_number!r}, "
                f"amount={receipt_amount!r}). PDF not uploaded.\n"
                f"Challan: {challan}\nDepartment: {department}\n"
                f"Vehicle: {params.vehicleNumber}\n"
                f"Receipt Number: {receipt_number or 'unknown'}\n"
                f"Receipt PDF: not uploaded — could not read receipt fields\nStatus: partial"
            ),
            partial_reasons=["receipt_fields_unreadable"],
            run_log=log.dump(),
        )

    # ── 6c: capture PDF + upload via challan-payment endpoint ─────────────────────
    save_data = {
        "vehicleNumber": params.vehicleNumber,
        "receiptNumber": receipt_number,
        "amount": receipt_amount,
        "paymentDate": receipt_date,
        "challanNo": challan,
        "department": department,
    }

    # Route to the challan-specific save-receipt endpoint so the handler writes
    # to challanRequests + challanPayments (not borderTaxRequests/borderTaxPayments).
    save_resp = await save_receipt(
        session,
        job_id,
        params.model_dump(),
        save_data,
        endpoint="/api/internal/challan-payment/save-receipt",
    )

    log.record(
        StepLog(
            index=log.next_index(),
            name="phase6.save_receipt",
            status=StepStatus.OK if save_resp.get("ok") else StepStatus.FAILED,
            value=json.dumps(save_resp)[:300],
        )
    )

    if not save_resp.get("ok"):
        return RunOutcome(
            status="partial",
            summary=(
                f"Challan {challan} ({department}): payment confirmed, receipt page loaded, "
                f"but save_receipt failed: {save_resp.get('error', 'unknown')}.\n"
                f"Challan: {challan}\nDepartment: {department}\n"
                f"Vehicle: {params.vehicleNumber}\n"
                f"Receipt Number: {receipt_number}\nAmount: ₹{receipt_amount}\n"
                f"Receipt PDF: not uploaded — {save_resp.get('error', 'unknown')}\nStatus: partial"
            ),
            partial_reasons=["save_receipt_failed"],
            run_log=log.dump(),
        )

    if not save_resp.get("pdfUploaded"):
        return RunOutcome(
            status="partial",
            summary=(
                f"Challan {challan} ({department}): payment confirmed, receipt metadata saved "
                f"(receipt {receipt_number}) but PDF upload to GCS failed: "
                f"{save_resp.get('pdfUploadError', 'unknown')}.\n"
                f"Challan: {challan}\nDepartment: {department}\n"
                f"Vehicle: {params.vehicleNumber}\n"
                f"Receipt Number: {receipt_number}\nAmount: ₹{receipt_amount}\n"
                f"Receipt PDF: not uploaded — PDF upload failed\nStatus: partial"
            ),
            partial_reasons=["pdf_upload_failed"],
            run_log=log.dump(),
        )

    return RunOutcome(
        status="done",
        summary=(
            f"Challan {challan} ({department}): payment completed and receipt captured.\n"
            f"Challan: {challan}\nDepartment: {department}\n"
            f"Vehicle: {params.vehicleNumber}\n"
            f"Receipt Number: {receipt_number}\nAmount: ₹{receipt_amount}\n"
            f"Payment Date: {receipt_date}\nReceipt PDF: uploaded\nStatus: complete"
        ),
        run_log=log.dump(),
    )

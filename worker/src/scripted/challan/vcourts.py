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

    # ── Phase 4c: pre-fill the mobile number ONLY (no Get-OTP / verify) ──
    if not params.phoneNo:
        return RunOutcome(
            status="failed",
            summary="phoneNo is required for the OTP path.",
            abort_reason="missing_phone",
            run_log=log.dump(),
        )
    await fill(session, "#otp_mobile_ce", params.phoneNo, log=log, name="vc.fill_otp_mobile")

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

    # ── Phase 6: receipt (best-effort) ──────────────────────────────────────
    # TODO: poll for this court's receipt-page markers, then capture the PDF:
    #   await save_receipt(session, job_id, params.model_dump())
    # (Upgrade path: use border_tax/_web_handover.web_handover_and_capture to
    #  poll QR + receipt concurrently once you know the receipt markers.)

    return RunOutcome(
        status="done",
        summary=(
            f"Challan {challan} ({department}) handed off for payment and "
            f"confirmed by human."
        ),
        run_log=log.dump(),
    )

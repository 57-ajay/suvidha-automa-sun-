# worker/src/scripted/border_tax/_payment_wait.py
"""Shared payment-wait + receipt-capture helper for border-tax scripted
runners.

v2 — replaced "click here / QR gone" as positive signals (they appear on
BOTH success and failure pages, so they're ambiguous). The only true
positive signal is explicit payment-success text. Added explicit
NEGATIVE-marker detection (Transaction Pending / Failed) which now
triggers an immediate fail-fast instead of running Phase B's receipt
poll and returning a misleading "payment confirmed" message.

Three phases:

  PHASE A -- wait for payment    (180s + 6s grace, status=waiting_for_human)
    Poll the page every 3s. Three possible outcomes per poll:
      * POSITIVE -- explicit success text ("Payment Successful" /
        "Transaction Successful") -> advance to Phase B
      * NEGATIVE -- explicit pending/failed text ("Transaction Status:
        Pending" / "Your transaction status is Pending" / "Transaction
        confirmation pending" / "Transaction Failed") -> FAIL fast,
        clear message
      * NOTHING DECISIVE -- keep polling. "Click here" links and a
        missing QR <img> alone are AMBIGUOUS (they appear on both
        success and failure pages on PB) and are NOT advance triggers.
    User typing 'done' is only honored if a POSITIVE marker is also
    present (uncorroborated assertions are logged but ignored).
    If the full 180s + 6s elapses with neither positive nor negative
    signal -> fail "no decisive payment signal in 3 minutes" (the
    likeliest explanation is the driver never initiated payment).

  PHASE B -- verify & redirect   (60s, status=verifyingPayment)
    Try to click any "click here"-like element on the page (skips the
    portal's auto-redirect to the receipt). Poll every 2s. Outcomes:
      * POSITIVE -- receipt markers appear -> Phase C
      * NEGATIVE -- pending/failed text appears (e.g. parivahan portal
        shows "Transaction confirmation pending at the bank side"
        after we click through from the SBIePay pending page) -> FAIL
      * TIMEOUT -- 60s passes, nothing decisive. Since Phase A advanced
        on a positive marker, lean toward done (fetch-receipt fallback
        covers the missing PDF).

  PHASE C -- capture receipt
    Extract fields. If extraction fails -> done (fetch-receipt fallback).
    If extraction succeeds, call save_receipt with up to 2 retries and
    exponential backoff (2s, 4s) for transient API errors. Either way
    the final status is 'done' -- the frontend already shows a "Download
    Receipt" button when receipt URL is missing under a requestId, which
    triggers the fetch-receipt task.

Outcome matrix:
  done    -- confident payment happened (positive marker seen in Phase A,
             receipt may or may not be in storage).
  failed  -- explicit negative marker seen at any point, OR Phase A
             elapsed with no decisive signal (no payment initiated).
  partial -- extremely rare ambiguous case where Phase B times out
             without negative markers but also without receipt. (Phase A
             must have positively advanced for us to reach Phase B at
             all in this version; this case mostly means a portal hiccup.)

Per-state customization is via PaymentCaptureConfig. Defaults cover PB;
MP/HR/UP/RJ can override patterns when their text differs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import redis

from actions import save_receipt
from ..log import StepLogger
from ..steps import _cdp_eval
from ..types import RunOutcome, StepLog, StepStatus


# ─── Status constants ──────────────────────────────────────────────────

STATUS_WAITING_FOR_HUMAN = "waiting_for_human"
STATUS_VERIFYING_PAYMENT = "verifyingPayment"
STATUS_RUNNING = "running"

JOB_TTL = 60 * 60 * 24


# ─── Default marker patterns ───────────────────────────────────────────
#
# Each pattern is a JS-regex source (without the slashes). They are
# OR-joined and compiled into one alternation per category. All patterns
# are matched case-insensitively against document.body.innerText.

DEFAULT_POSITIVE_PATTERNS = [
    r"payment\s*successful",
    r"transaction\s*successful",
    r"successfully\s*paid",
    r"transaction\s*status\s*[:\-]?\s*success",
]

DEFAULT_NEGATIVE_PATTERNS = [
    # SBI ePay Lite Remittance Information Form (after QR expires w/o pay)
    r"transaction\s*status\s*[:\-]?\s*pending",
    r"your\s*transaction\s*status\s*is\s*pending",
    # Parivahan portal post-redirect after SBI pending
    r"transaction\s*confirmation\s*pending",
    # Generic failure text
    r"transaction\s*status\s*[:\-]?\s*failed",
    r"transaction\s*failed",
    r"payment\s*failed",
]


# ─── Config ────────────────────────────────────────────────────────────


@dataclass
class PaymentCaptureConfig:
    """Per-state config for the payment-wait + receipt-capture flow."""

    state_name: str                       # e.g. "Punjab"
    qr_selector: str                      # e.g. "img#qrcodeImg"
    receipt_markers: list[str]            # all-uppercase strings to match in body text

    # Phase budgets.
    payment_wait_secs: int = 180
    final_grace_secs: int = 6
    verify_phase_secs: int = 60
    poll_interval_secs: float = 3.0
    marker_poll_secs: float = 2.0

    # save_receipt retry config.
    save_receipt_retries: int = 2
    save_receipt_backoff_secs: float = 2.0

    # Payment-success / payment-not-made text patterns. JS regex sources,
    # case-insensitive. Defaults work for PB; other states may extend.
    positive_markers_regex: list[str] = field(default_factory=lambda: list(DEFAULT_POSITIVE_PATTERNS))
    negative_markers_regex: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATIVE_PATTERNS))


# ─── JS probes ─────────────────────────────────────────────────────────


def _js_regex_alternation(patterns: list[str]) -> str:
    """Build a single JS regex source that ORs the given patterns. Returns
    the regex literal as a string suitable for direct embedding."""
    if not patterns:
        return "/(?!)/"  # never matches
    joined = "|".join(f"(?:{p})" for p in patterns)
    return f"/{joined}/i"


def _make_page_state_js(
    qr_selector: str,
    positive_patterns: list[str],
    negative_patterns: list[str],
) -> str:
    """Composite probe: one CDP call returns everything Phase A cares about."""
    pos_rx = _js_regex_alternation(positive_patterns)
    neg_rx = _js_regex_alternation(negative_patterns)
    return f"""
    (function() {{
      const text = document.body.innerText || '';
      const candidates = Array.from(document.querySelectorAll(
        'a, button, input[type=button], input[type=submit]'
      ));
      const clickEl = candidates.find(function(el) {{
        const t = (el.textContent || el.value || '').trim().toLowerCase();
        return /click/.test(t);
      }});
      return {{
        qrPresent: !!document.querySelector({json.dumps(qr_selector)}),
        paymentSuccessMarker: {pos_rx}.test(text),
        paymentNegativeMarker: {neg_rx}.test(text),
        clickHere: !!clickEl,
        url: location.href,
        bodyLen: text.length
      }};
    }})()
    """


_CLICK_HERE_JS = """
(function() {
  const candidates = Array.from(document.querySelectorAll(
    'a, button, input[type=button], input[type=submit]'
  ));
  // Score: text containing both "click" AND "here" wins over plain "click".
  const scored = candidates.map(function(el) {
    const t = (el.textContent || el.value || '').trim().toLowerCase();
    if (!/click/.test(t)) return null;
    return { el: el, score: /here/.test(t) ? 2 : 1, text: t.substring(0, 60) };
  }).filter(Boolean);
  if (scored.length === 0) return null;
  scored.sort(function(a, b) { return b.score - a.score; });
  const best = scored[0];
  try {
    best.el.click();
    return { ok: true, text: best.text, score: best.score };
  } catch (e) {
    return { ok: false, error: String(e), text: best.text };
  }
})()
"""


def _make_markers_present_js(markers: list[str]) -> str:
    return f"""
    (function() {{
      const text = (document.body.innerText || '').toUpperCase();
      const markers = {json.dumps(markers)};
      return markers.every(function(m) {{ return text.indexOf(m.toUpperCase()) >= 0; }});
    }})()
    """


# ─── Helpers ───────────────────────────────────────────────────────────


async def _eval_page_state(
    session,
    qr_selector: str,
    positive_patterns: list[str],
    negative_patterns: list[str],
) -> dict | None:
    """Run the composite probe. Returns None on CDP failure (treat as
    'no signal this tick' and continue polling)."""
    try:
        js = _make_page_state_js(qr_selector, positive_patterns, negative_patterns)
        return await _cdp_eval(session, js)
    except Exception as e:
        print(f"[payment_wait] page-state eval failed: {type(e).__name__}: {e}")
        return None


async def _try_click_here(session, log: StepLogger) -> dict | None:
    """Find and click the best 'click here'-like element. Returns the JS
    result dict if a click was dispatched, None otherwise."""
    try:
        result = await _cdp_eval(session, _CLICK_HERE_JS)
        return result if isinstance(result, dict) else None
    except Exception as e:
        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_b.click_here_error",
            status=StepStatus.FAILED,
            error=f"{type(e).__name__}: {e}",
        ))
        return None


async def _markers_present(session, markers: list[str]) -> bool:
    try:
        result = await _cdp_eval(session, _make_markers_present_js(markers))
        return bool(result)
    except Exception:
        return False


async def _negative_marker_present(
    session,
    negative_patterns: list[str],
) -> bool:
    """Cheap one-off check used inside Phase B's marker polling loop, so we
    fail fast if a redirect lands us on a pending/failed page."""
    if not negative_patterns:
        return False
    neg_rx = _js_regex_alternation(negative_patterns)
    js = f"(function(){{return {neg_rx}.test(document.body.innerText || '');}})()"
    try:
        return bool(await _cdp_eval(session, js))
    except Exception:
        return False


def _fmt_state(state: dict | None) -> str:
    """Compact one-line dump of the page state for log entries."""
    if not state:
        return "qr=? pos=? neg=?"
    return (
        f"qr={'y' if state.get('qrPresent') else 'n'} "
        f"pos={'y' if state.get('paymentSuccessMarker') else 'n'} "
        f"neg={'y' if state.get('paymentNegativeMarker') else 'n'} "
        f"click={'y' if state.get('clickHere') else 'n'}"
    )


# ─── Public entry point ────────────────────────────────────────────────


async def wait_for_payment_and_capture_receipt(
    session,
    log: StepLogger,
    r: redis.Redis,
    job_id: str,
    job_params: dict,
    *,
    vehicle_number: str,
    config: PaymentCaptureConfig,
    extract_receipt_fields: Callable[..., Awaitable[dict | None]],
) -> RunOutcome:
    """Portal-driven payment wait + receipt capture. See module docstring
    for the full state machine and outcome matrix."""

    # =====================================================================
    # PHASE A -- wait for payment
    # =====================================================================

    payment_reason = (
        f"UPI payment required for border tax of vehicle {vehicle_number} "
        f"entering {config.state_name}. A QR code is displayed on screen — "
        f"please scan with your UPI app and complete the payment within "
        f"~3 minutes."
    )
    r.hset(
        f"job:{job_id}",
        mapping={
            "status": STATUS_WAITING_FOR_HUMAN,
            "waitReason": payment_reason,
        },
    )

    log.record(StepLog(
        index=log.next_index(),
        name="payment_wait.phase_a.entered",
        status=StepStatus.OK,
        value=f"budget={config.payment_wait_secs}s grace={config.final_grace_secs}s",
    ))

    phase_a_started = time.monotonic()
    advance_reason: str | None = None
    failure_reason: str | None = None       # set to "negative_marker" if pending/failed seen
    user_assertion_logged = False
    poll_count = 0

    while time.monotonic() - phase_a_started < config.payment_wait_secs:
        poll_count += 1
        elapsed = time.monotonic() - phase_a_started
        state = await _eval_page_state(
            session,
            config.qr_selector,
            config.positive_markers_regex,
            config.negative_markers_regex,
        )

        raw_human = r.hget(f"job:{job_id}", "humanInput")
        human_input_str: str | None = None
        if raw_human:
            human_input_str = (
                raw_human.decode() if isinstance(raw_human, bytes) else raw_human
            )

        log.record(StepLog(
            index=log.next_index(),
            name=f"payment_wait.phase_a.poll_{poll_count:03d}",
            status=StepStatus.OK,
            value=f"t={elapsed:.0f}s {_fmt_state(state)} human={'y' if human_input_str else 'n'}",
        ))

        if state is not None:
            # 1. STRONG NEGATIVE -- the bank/portal is telling us payment didn't happen.
            if state.get("paymentNegativeMarker"):
                failure_reason = "negative_marker"
                break

            # 2. STRONG POSITIVE -- the portal confirms payment.
            if state.get("paymentSuccessMarker"):
                advance_reason = "success_text"
                break

            # 3. HUMAN ASSERTION -- only honored if positive corroborates.
            #    (Negatives already broke above, so if we're here positives
            #    are absent -- log the uncorroborated assertion and continue.)
            if human_input_str and not user_assertion_logged:
                log.record(StepLog(
                    index=log.next_index(),
                    name="payment_wait.phase_a.user_assertion_uncorroborated",
                    status=StepStatus.OK,
                    value=(
                        "user typed something but no positive payment signal — "
                        "continuing to wait"
                    ),
                ))
                user_assertion_logged = True

            # 4. Note: clickHere=true and qrPresent=false alone are NOT
            #    advance triggers in this version. They appear on both
            #    success and failure pages on PB. We only advance on
            #    explicit success text.

        await asyncio.sleep(config.poll_interval_secs)

    # End-of-window grace check. The main loop polls every 3s; if a state
    # change happened in the last second or two of the 180s window, we'd
    # miss it. Do a few tight checks before declaring failure.
    if advance_reason is None and failure_reason is None:
        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_a.grace_start",
            status=StepStatus.OK,
            value=f"grace={config.final_grace_secs}s",
        ))
        grace_started = time.monotonic()
        grace_poll = 0
        while time.monotonic() - grace_started < config.final_grace_secs:
            grace_poll += 1
            state = await _eval_page_state(
                session,
                config.qr_selector,
                config.positive_markers_regex,
                config.negative_markers_regex,
            )
            log.record(StepLog(
                index=log.next_index(),
                name=f"payment_wait.phase_a.grace_poll_{grace_poll:02d}",
                status=StepStatus.OK,
                value=_fmt_state(state),
            ))
            if state is not None:
                if state.get("paymentNegativeMarker"):
                    failure_reason = "negative_marker"
                    break
                if state.get("paymentSuccessMarker"):
                    advance_reason = "success_text"
                    break
            await asyncio.sleep(2.0)

    # Phase A failure paths.
    if failure_reason == "negative_marker":
        r.hdel(f"job:{job_id}", "waitReason", "humanInput")
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)
        r.rpush(f"job:{job_id}:partial_reasons", "payment_not_completed_pending_or_failed")
        r.expire(f"job:{job_id}:partial_reasons", JOB_TTL)

        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_a.failed_negative_marker",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - phase_a_started) * 1000),
            error="bank_or_portal_reported_pending_or_failed",
        ))
        return RunOutcome(
            status="failed",
            summary=(
                f"Border tax payment for vehicle {vehicle_number} entering "
                f"{config.state_name} was NOT completed. The bank/portal "
                f"explicitly reported the transaction status as pending or "
                f"failed — the driver did not complete the UPI payment."
            ),
            abort_reason="payment_not_completed",
            run_log=log.dump(),
        )

    if advance_reason is None:
        # No decisive signal at all -- likely no payment was ever initiated.
        r.hdel(f"job:{job_id}", "waitReason", "humanInput")
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)
        r.rpush(f"job:{job_id}:partial_reasons", "payment_no_signal")
        r.expire(f"job:{job_id}:partial_reasons", JOB_TTL)

        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_a.failed_no_signal",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - phase_a_started) * 1000),
            error="no_decisive_signal_within_window",
        ))
        total_window = config.payment_wait_secs + config.final_grace_secs
        return RunOutcome(
            status="failed",
            summary=(
                f"Border tax payment for vehicle {vehicle_number} entering "
                f"{config.state_name} was NOT completed within {total_window} "
                f"seconds. No payment-success or payment-failure marker "
                f"appeared on the page — most likely the driver did not "
                f"initiate the UPI payment."
            ),
            abort_reason="payment_no_signal",
            run_log=log.dump(),
        )

    log.record(StepLog(
        index=log.next_index(),
        name="payment_wait.phase_a.advanced",
        status=StepStatus.OK,
        duration_ms=int((time.monotonic() - phase_a_started) * 1000),
        value=f"reason={advance_reason}",
    ))

    # =====================================================================
    # PHASE B -- verify & redirect
    # =====================================================================

    r.hset(
        f"job:{job_id}",
        mapping={
            "status": STATUS_VERIFYING_PAYMENT,
            "waitReason": "Verifying payment and loading receipt page...",
        },
    )
    r.hdel(f"job:{job_id}", "humanInput")

    log.record(StepLog(
        index=log.next_index(),
        name="payment_wait.phase_b.entered",
        status=StepStatus.OK,
        value=f"budget={config.verify_phase_secs}s",
    ))

    # Try clicking "click here" once -- on the success page this skips
    # the portal's 60s auto-redirect to the receipt.
    click_result = await _try_click_here(session, log)
    if click_result:
        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_b.click_here_attempted",
            status=StepStatus.OK,
            value=(
                f"ok={click_result.get('ok')} "
                f"text={click_result.get('text')!r} "
                f"score={click_result.get('score')}"
            ),
        ))
    else:
        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_b.click_here_not_found",
            status=StepStatus.OK,
            value="no click-here element on page; waiting for auto-redirect",
        ))

    phase_b_started = time.monotonic()
    marker_poll = 0
    receipt_ready = False
    negative_in_phase_b = False
    while time.monotonic() - phase_b_started < config.verify_phase_secs:
        marker_poll += 1
        elapsed = time.monotonic() - phase_b_started

        # Check positive (receipt) and negative markers in parallel.
        present = await _markers_present(session, config.receipt_markers)
        if present:
            log.record(StepLog(
                index=log.next_index(),
                name=f"payment_wait.phase_b.marker_poll_{marker_poll:03d}",
                status=StepStatus.OK,
                value=f"t={elapsed:.0f}s present=y",
            ))
            receipt_ready = True
            break

        neg_now = await _negative_marker_present(session, config.negative_markers_regex)
        log.record(StepLog(
            index=log.next_index(),
            name=f"payment_wait.phase_b.marker_poll_{marker_poll:03d}",
            status=StepStatus.OK,
            value=f"t={elapsed:.0f}s present=n neg={'y' if neg_now else 'n'}",
        ))
        if neg_now:
            negative_in_phase_b = True
            break

        await asyncio.sleep(config.marker_poll_secs)

    if negative_in_phase_b:
        # The click-here landed us on the parivahan "Transaction confirmation
        # pending at the bank side" page (or similar). Payment was NOT made.
        r.hdel(f"job:{job_id}", "waitReason")
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)
        r.rpush(f"job:{job_id}:partial_reasons", "payment_negative_in_phase_b")
        r.expire(f"job:{job_id}:partial_reasons", JOB_TTL)

        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_b.failed_negative_marker",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - phase_b_started) * 1000),
            error="negative_marker_after_redirect",
        ))
        return RunOutcome(
            status="failed",
            summary=(
                f"Border tax payment for vehicle {vehicle_number} entering "
                f"{config.state_name} was NOT completed. After the QR page "
                f"redirected, the portal reported the transaction status as "
                f"pending or failed — the driver did not complete the UPI "
                f"payment."
            ),
            abort_reason="payment_not_completed_phase_b",
            run_log=log.dump(),
        )

    if not receipt_ready:
        # Phase A had a positive marker, Phase B saw neither receipt nor
        # negative marker. Portal hiccup / slow render. Trust Phase A's
        # positive signal -> done, fetch-receipt fallback handles the PDF.
        r.hdel(f"job:{job_id}", "waitReason")
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)

        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_b.timeout_no_receipt",
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - phase_b_started) * 1000),
            error="receipt_page_not_loaded",
            value=f"advance_reason={advance_reason}",
        ))
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax payment confirmed for vehicle {vehicle_number} "
                f"entering {config.state_name}, but the receipt page did "
                f"not load within {config.verify_phase_secs}s. The receipt "
                f"PDF is not stored — use the fetch-receipt task to "
                f"retrieve it."
            ),
            run_log=log.dump(),
        )

    log.record(StepLog(
        index=log.next_index(),
        name="payment_wait.phase_b.receipt_page_rendered",
        status=StepStatus.OK,
        duration_ms=int((time.monotonic() - phase_b_started) * 1000),
    ))

    # =====================================================================
    # PHASE C -- capture receipt
    # =====================================================================

    await asyncio.sleep(1.5)

    receipt_data = await extract_receipt_fields(session, vehicle_number)
    if not receipt_data:
        log.record(StepLog(
            index=log.next_index(),
            name="payment_wait.phase_c.extraction_failed",
            status=StepStatus.FAILED,
            error="receipt_fields_unparseable",
        ))
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax payment confirmed for vehicle {vehicle_number} "
                f"entering {config.state_name}, but receipt fields could "
                f"not be parsed from the page. The receipt PDF is not "
                f"stored — use the fetch-receipt task to retrieve it."
            ),
            run_log=log.dump(),
        )

    last_error: str | None = None
    last_resp: dict | None = None
    for attempt in range(config.save_receipt_retries + 1):
        save_started = time.monotonic()
        last_resp = await save_receipt(session, job_id, job_params, receipt_data)
        ok = bool(last_resp and last_resp.get("ok"))
        pdf_ok = bool(last_resp and last_resp.get("pdfUploaded", True))

        attempts_left = config.save_receipt_retries - attempt
        if ok and pdf_ok:
            status_for_log = StepStatus.OK
        elif attempts_left > 0:
            status_for_log = StepStatus.RETRIED
        else:
            status_for_log = StepStatus.FAILED

        log.record(StepLog(
            index=log.next_index(),
            name=f"payment_wait.phase_c.save_attempt_{attempt + 1}",
            status=status_for_log,
            attempt=attempt + 1,
            duration_ms=int((time.monotonic() - save_started) * 1000),
            value=receipt_data.get("receiptNumber"),
            error=None if (ok and pdf_ok) else (last_resp.get("error") if last_resp else "no_response"),
        ))

        if ok and pdf_ok:
            break

        last_error = (last_resp.get("error") if last_resp else "no_response")
        if attempt < config.save_receipt_retries:
            backoff = config.save_receipt_backoff_secs * (2 ** attempt)
            await asyncio.sleep(backoff)

    if last_resp and last_resp.get("ok") and last_resp.get("pdfUploaded", True):
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax paid for vehicle {vehicle_number} entering "
                f"{config.state_name}. Receipt {receipt_data['receiptNumber']}, "
                f"amount ₹{receipt_data['amount']}, payment date "
                f"{receipt_data['paymentDate']}."
            ),
            receipt_number=receipt_data["receiptNumber"],
            amount=float(receipt_data["amount"]),
            run_log=log.dump(),
        )

    amount_val: float | None = None
    try:
        amount_val = float(receipt_data.get("amount")) if receipt_data.get("amount") is not None else None
    except (TypeError, ValueError):
        amount_val = None

    return RunOutcome(
        status="done",
        summary=(
            f"Border tax payment confirmed for vehicle {vehicle_number} "
            f"entering {config.state_name}. Receipt fields captured "
            f"(receipt {receipt_data['receiptNumber']}, ₹{receipt_data.get('amount')}) "
            f"but PDF upload to storage failed after retries: {last_error}. "
            f"Use the fetch-receipt task to retrieve the PDF."
        ),
        receipt_number=receipt_data["receiptNumber"],
        amount=amount_val,
        run_log=log.dump(),
    )

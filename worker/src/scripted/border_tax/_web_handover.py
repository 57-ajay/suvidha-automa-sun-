# worker/src/scripted/border_tax/_web_handover.py
"""Web-source human handover + background receipt/QR capture.

When source="web", the scripted runner completes form-filling phases
(Phases 1-5 for UP/HR/PB/MP, Phases 1-3 for RJ) and then hands over to
the human operator who can see the browser via VNC. The human is
responsible for solving captcha, selecting payment method (UPI, net
banking, etc.), and completing payment.

Meanwhile this module polls the page concurrently for:

  1. QR CODE (optional, controlled by `poll_qr` flag)
     If the human chooses UPI, a QR code may appear on screen. We detect
     it via save_qr_code() and persist it so the client/driver can scan
     and pay. Once captured, QR polling stops.

  2. RECEIPT (always)
     We poll for receipt-page markers (same markers used by
     _payment_wait.py). Once the receipt page renders, we extract fields
     and capture the PDF via save_receipt().

Both polls run SIMULTANEOUSLY via asyncio.gather -- we never start
receipt polling after QR polling. This is critical because the human
might choose net banking or another payment flow that never shows a QR
code, and we must not block receipt detection on QR detection.

The QR poll is gated by a `poll_qr` parameter that can be:
  - Set per-state in PaymentCaptureConfig.web_poll_qr (default True)
  - Overridden globally via env var WEB_HANDOVER_POLL_QR=true|false

Timeout is 15 minutes (900s) from the moment handover starts.

Outcome matrix:
  done   -- receipt captured (QR may or may not have been captured).
  failed -- 15-minute timeout with no receipt, OR explicit negative
            payment marker seen (transaction pending/failed).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Awaitable, Callable

import redis

from actions import save_qr_code, save_receipt
from ..log import StepLogger
from ..steps import _cdp_eval
from ..types import RunOutcome, StepLog, StepStatus


# ─── Constants ─────────────────────────────────────────────────────────

STATUS_WAITING_FOR_HUMAN = "waiting_for_human"
STATUS_VERIFYING_PAYMENT = "verifyingPayment"
STATUS_RUNNING = "running"
JOB_TTL = 60 * 60 * 24

DEFAULT_TIMEOUT_SECS = 900  # 15 minutes
QR_POLL_INTERVAL_SECS = 3.0
RECEIPT_POLL_INTERVAL_SECS = 4.0


# ─── Flag resolution ──────────────────────────────────────────────────


def _resolve_poll_qr(
    config_value: bool | None,
) -> bool:
    """Resolve whether QR polling is enabled.

    Priority:
      1. Per-state config value (PaymentCaptureConfig.web_poll_qr) if not None
      2. Env var WEB_HANDOVER_POLL_QR (true/false/1/0)
      3. Default: True
    """
    # 1. Per-state config takes priority
    if config_value is not None:
        return config_value

    # 2. Env var override
    env_val = os.environ.get("WEB_HANDOVER_POLL_QR", "").strip().lower()
    if env_val in ("false", "0", "no"):
        return False
    if env_val in ("true", "1", "yes"):
        return True

    # 3. Default
    return True


# ─── JS probes ─────────────────────────────────────────────────────────


def _js_regex_alternation(patterns: list[str]) -> str:
    """Build a single JS regex literal that ORs the given patterns."""
    if not patterns:
        return "/(?!)/"  # never matches
    joined = "|".join(f"(?:{p})" for p in patterns)
    return f"/{joined}/i"


def _make_receipt_check_js(receipt_markers: list[str]) -> str:
    """JS probe: returns True if ALL receipt markers are present in the
    page body text (case-insensitive). This is the signal that the
    receipt page has fully rendered."""
    checks = []
    for marker in receipt_markers:
        escaped = json.dumps(marker.lower())
        checks.append(f"text.indexOf({escaped}) !== -1")
    condition = " && ".join(checks) if checks else "false"
    return f"""
    (function() {{
      var text = (document.body.innerText || '').toLowerCase();
      return ({condition});
    }})()
    """


def _make_negative_check_js(negative_patterns: list[str]) -> str:
    """JS probe: returns True if any negative marker (payment
    failed/pending) is present."""
    if not negative_patterns:
        return "(function(){ return false; })()"
    neg_rx = _js_regex_alternation(negative_patterns)
    return f"(function(){{return {neg_rx}.test(document.body.innerText || '');}})()"


def _make_qr_check_js(qr_selector: str) -> str:
    """JS probe: returns True if a QR-code element matching the selector
    is present AND has non-zero dimensions."""
    return f"""
    (function() {{
      var el = document.querySelector({json.dumps(qr_selector)});
      if (!el) return false;
      var rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }})()
    """


# ─── Public entry point ───────────────────────────────────────────────


async def web_handover_and_capture(
    session,
    log: StepLogger,
    r: redis.Redis,
    job_id: str,
    job_params: dict,
    *,
    vehicle_number: str,
    config,  # PaymentCaptureConfig from _payment_wait.py
    extract_receipt_fields: Callable[..., Awaitable[dict | None]],
    poll_qr: bool | None = True,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
) -> RunOutcome:
    """Hand over to the human operator and poll for QR + receipt in
    the background. See module docstring for full behavior."""

    # Resolve the QR polling flag
    # config.web_poll_qr is the per-state override (added to PaymentCaptureConfig)
    print("[ENTERED WEB]")
    config_poll_qr = getattr(config, "web_poll_qr", None)
    # Explicit parameter overrides config, which overrides env
    effective_poll_qr = _resolve_poll_qr(
        poll_qr if poll_qr is not None else config_poll_qr
    )

    # ─── Set job status to waiting_for_human ───────────────────────────

    handover_reason = (
        f"Human handover: please complete the border tax payment for "
        f"vehicle {vehicle_number} entering {config.state_name}. "
        f"Solve the captcha, select your payment method, and complete "
        f"payment in the browser. You have {timeout_secs // 60} minutes."
    )
    r.hset(
        f"job:{job_id}",
        mapping={
            "status": STATUS_WAITING_FOR_HUMAN,
            "waitReason": handover_reason,
        },
    )

    log.record(StepLog(
        index=log.next_index(),
        name="web_handover.entered",
        status=StepStatus.OK,
        value=(
            f"timeout={timeout_secs}s poll_qr={effective_poll_qr} "
            f"state={config.state_name}"
        ),
    ))

    print(
        f"[{job_id}] web_handover: started for {vehicle_number} "
        f"entering {config.state_name}, timeout={timeout_secs}s, "
        f"poll_qr={effective_poll_qr}"
    )

    # ─── Build JS probes ──────────────────────────────────────────────

    receipt_check_js = _make_receipt_check_js(config.receipt_markers)
    negative_check_js = _make_negative_check_js(
        getattr(config, "negative_markers_regex", [])
    )
    qr_check_js = _make_qr_check_js(config.qr_selector)

    # ─── Shared state between pollers ─────────────────────────────────

    qr_captured = False
    receipt_captured = False
    failure_reason: str | None = None
    deadline = time.monotonic() + timeout_secs

    # ─── QR poller coroutine ──────────────────────────────────────────

    async def _poll_qr():
        nonlocal qr_captured

        if not effective_poll_qr:
            return

        qr_poll_count = 0
        while time.monotonic() < deadline and not receipt_captured:
            qr_poll_count += 1
            try:
                qr_present = await _cdp_eval(session, qr_check_js)
                if qr_present:
                    print(f"[{job_id}] web_handover: QR detected, saving...")
                    qr_started = time.monotonic()
                    qr_result = await save_qr_code(session, job_id, job_params)
                    ok = qr_result.get("ok", False)

                    log.record(StepLog(
                        index=log.next_index(),
                        name="web_handover.qr_captured",
                        status=StepStatus.OK if ok else StepStatus.FAILED,
                        duration_ms=int(
                            (time.monotonic() - qr_started) * 1000
                        ),
                        error=None if ok else qr_result.get("error"),
                        value=f"poll={qr_poll_count}",
                    ))

                    if ok:
                        qr_captured = True
                        print(f"[{job_id}] web_handover: QR saved, "
                              f"stopping QR poll")
                        return
                    # save failed — keep polling, maybe the QR wasn't
                    # fully rendered yet
            except Exception as e:
                # CDP eval can fail if page is navigating; just retry
                if qr_poll_count % 20 == 0:
                    print(
                        f"[{job_id}] web_handover: QR poll error "
                        f"(poll #{qr_poll_count}): {e}"
                    )

            await asyncio.sleep(QR_POLL_INTERVAL_SECS)

    # ─── Receipt poller coroutine ─────────────────────────────────────

    async def _poll_receipt():
        nonlocal receipt_captured, failure_reason

        receipt_poll_count = 0
        while time.monotonic() < deadline:
            receipt_poll_count += 1
            elapsed = time.monotonic() - (deadline - timeout_secs)

            try:
                # 1. Check for negative markers (payment failed/pending)
                is_negative = await _cdp_eval(session, negative_check_js)
                if is_negative:
                    failure_reason = "negative_marker"
                    log.record(StepLog(
                        index=log.next_index(),
                        name="web_handover.negative_marker_detected",
                        status=StepStatus.FAILED,
                        duration_ms=int(elapsed * 1000),
                        error="payment_failed_or_pending",
                        value=f"poll={receipt_poll_count}",
                    ))
                    print(
                        f"[{job_id}] web_handover: negative marker "
                        f"detected at poll #{receipt_poll_count}"
                    )
                    return

                # 2. Check for receipt page markers
                is_receipt = await _cdp_eval(session, receipt_check_js)
                if is_receipt:
                    print(
                        f"[{job_id}] web_handover: receipt markers "
                        f"detected at poll #{receipt_poll_count}"
                    )
                    receipt_captured = True

                    log.record(StepLog(
                        index=log.next_index(),
                        name="web_handover.receipt_detected",
                        status=StepStatus.OK,
                        duration_ms=int(elapsed * 1000),
                        value=f"poll={receipt_poll_count}",
                    ))
                    return

            except Exception as e:
                # Page navigating / CDP session hiccup — expected during
                # payment gateway hops
                if receipt_poll_count % 20 == 0:
                    print(
                        f"[{job_id}] web_handover: receipt poll error "
                        f"(poll #{receipt_poll_count}): {e}"
                    )

            # Log every 30th poll for observability
            if receipt_poll_count % 30 == 0:
                log.record(StepLog(
                    index=log.next_index(),
                    name=f"web_handover.receipt_poll_{receipt_poll_count:04d}",
                    status=StepStatus.OK,
                    value=f"t={elapsed:.0f}s waiting",
                ))

            await asyncio.sleep(RECEIPT_POLL_INTERVAL_SECS)

    # ─── Run both pollers concurrently ────────────────────────────────

    handover_started = time.monotonic()

    await asyncio.gather(
        _poll_qr(),
        _poll_receipt(),
    )

    total_elapsed = time.monotonic() - handover_started

    # ─── Evaluate outcome ─────────────────────────────────────────────

    # Case 1: Negative marker — payment failed/pending
    if failure_reason == "negative_marker":
        r.hdel(f"job:{job_id}", "waitReason")
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)
        return RunOutcome(
            status="failed",
            summary=(
                f"Border tax payment for vehicle {vehicle_number} entering "
                f"{config.state_name} was not completed. The portal "
                f"reported the transaction as pending or failed."
            ),
            abort_reason="web_handover_payment_not_completed",
            run_log=log.dump(),
        )

    # Case 2: Receipt not detected within timeout
    if not receipt_captured:
        r.hdel(f"job:{job_id}", "waitReason")
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)

        log.record(StepLog(
            index=log.next_index(),
            name="web_handover.timeout",
            status=StepStatus.FAILED,
            duration_ms=int(total_elapsed * 1000),
            error=f"no_receipt_in_{timeout_secs}s",
        ))

        return RunOutcome(
            status="failed",
            summary=(
                f"Border tax payment for vehicle {vehicle_number} entering "
                f"{config.state_name} timed out after "
                f"{timeout_secs // 60} minutes. No receipt page was "
                f"detected. The human operator may not have completed "
                f"the payment in time."
            ),
            abort_reason="web_handover_timeout",
            run_log=log.dump(),
        )

    # Case 3: Receipt detected — extract fields and save
    r.hdel(f"job:{job_id}", "waitReason")
    r.hset(f"job:{job_id}", "status", STATUS_VERIFYING_PAYMENT)

    log.record(StepLog(
        index=log.next_index(),
        name="web_handover.receipt_page_ready",
        status=StepStatus.OK,
        duration_ms=int(total_elapsed * 1000),
        value=f"qr_captured={qr_captured}",
    ))

    # Give the page a moment to fully paint (watermarks, QR on receipt, etc.)
    await asyncio.sleep(2.0)

    # ─── Extract receipt fields ───────────────────────────────────────

    receipt_data = await extract_receipt_fields(session, vehicle_number)
    if not receipt_data:
        log.record(StepLog(
            index=log.next_index(),
            name="web_handover.receipt_extraction_failed",
            status=StepStatus.FAILED,
            error="receipt_fields_unparseable",
        ))
        # Still "done" — the receipt page was there, just couldn't parse.
        # fetch-receipt fallback handles the missing PDF.
        r.hset(f"job:{job_id}", "status", STATUS_RUNNING)
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax payment confirmed for vehicle "
                f"{vehicle_number} entering {config.state_name} "
                f"(human handover). Receipt page detected but fields "
                f"could not be parsed. Use the fetch-receipt task to "
                f"retrieve the PDF."
            ),
            run_log=log.dump(),
        )

    # ─── Save receipt (with retries) ──────────────────────────────────

    max_retries = getattr(config, "save_receipt_retries", 2)
    backoff = getattr(config, "save_receipt_backoff_secs", 2.0)
    save_ok = False
    last_save_error = ""

    for attempt in range(1 + max_retries):
        save_started = time.monotonic()
        try:
            result = await save_receipt(
                session, job_id, job_params, receipt_data
            )
            save_ok = result.get("ok", False)
            if save_ok:
                log.record(StepLog(
                    index=log.next_index(),
                    name="web_handover.save_receipt",
                    status=StepStatus.OK,
                    duration_ms=int(
                        (time.monotonic() - save_started) * 1000
                    ),
                    value=f"attempt={attempt + 1}",
                ))
                break
            last_save_error = result.get("error", "unknown")
        except Exception as e:
            last_save_error = f"{type(e).__name__}: {e}"

        log.record(StepLog(
            index=log.next_index(),
            name="web_handover.save_receipt",
            status=StepStatus.RETRIED if attempt < max_retries else StepStatus.FAILED,
            duration_ms=int((time.monotonic() - save_started) * 1000),
            error=last_save_error,
            attempt=attempt + 1,
        ))

        if attempt < max_retries:
            await asyncio.sleep(backoff * (2 ** attempt))

    r.hset(f"job:{job_id}", "status", STATUS_RUNNING)

    receipt_number = receipt_data.get("receiptNumber", "unknown")
    amount = receipt_data.get("amount", "unknown")

    if save_ok:
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax payment completed for vehicle "
                f"{vehicle_number} entering {config.state_name} "
                f"(human handover). Receipt {receipt_number}, "
                f"amount ₹{amount}. PDF uploaded."
            ),
            run_log=log.dump(),
        )
    else:
        return RunOutcome(
            status="done",
            summary=(
                f"Border tax payment confirmed for vehicle "
                f"{vehicle_number} entering {config.state_name} "
                f"(human handover). Receipt {receipt_number}, "
                f"amount ₹{amount}. PDF upload failed: "
                f"{last_save_error}. Use fetch-receipt to retrieve."
            ),
            run_log=log.dump(),
        )

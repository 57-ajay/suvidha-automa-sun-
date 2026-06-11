# worker/src/scripted/border_tax/uk.py
"""Uttarakhand border-tax scripted runner.

NO AI. Pure scripted form-fill (parivahan Phases 1-5), then hand the
captcha + payment to a human and poll for the receipt in the background.
See `_handover_runner.run_handover_flow` for the full phase walk and the
handover semantics. This is now identical in shape to HP and BR — UK used
to be the AI-handoff variant, and this rewrite collapses it onto the shared
flow.

WHAT CHANGED vs the old UK runner
=================================
  - REMOVED the Phase 6 AI captcha handoff and the Phase 7 payment-gateway
    selection (SBI Multi Bank Payment). The shared flow stops right after
    Phase 5 (Calculate Fee/Tax → Next) on the Disclaimer page and hands the
    captcha + payment to the human, exactly like HP/BR. There is no AI for
    any source now ("app" and "web" behave identically).
  - REMOVED the pending-transaction auto-clear. A "pending transaction"
    popup after Get Details now ABORTS immediately with
    abort_reason="pending_transaction_popup" and the summary
    "Vehicle <n> already has a pending transaction at the parivahan portal.
     Please clear the pending transaction first, then retry." (NO auto-clear),
    matching HP/BR.

UK specifics (everything else comes from the shared flow + _STATE_DEFAULTS)
==========================================================================
  - Phase 1 state value: "UK".
  - Phase 3 Entry District default: DEHRADUN (via params/_STATE_DEFAULTS).
  - Phase 3 Entry Checkpost: UK's checkpost names (ASHARODI / KULHAL / TIMLI
    / TUNI) do NOT track the district name, so checkpost_strategy=
    "first_option" — pick the first available checkpost.
  - Phase 4: Vehicle Category / Permit Type (default TEMPORARY PERMIT,
    fallback ALL INDIA TOURIST PERMIT) / Service Type — "leave if already
    filled, set if empty". Service Type's default is "Air Conditioned
    Service", so an empty Service Type is set to AC; a pre-filled value is
    kept as-is (this is the HP/BR rule; the old runner force-set AC even when
    pre-filled). UK has NO Distance field (like PB/MP).
  - Phase 5 Tax Mode: RC-dependent (the portal renders different modes per
    vehicle). The requested mode is matched by <option> value; if the portal
    doesn't offer it for the RC, the run aborts with
    abort_reason="tax_mode_not_offered_for_rc" listing the modes that were
    offered. Tax From/Upto input types are auto-detected (date vs
    datetime-local); Tax Upto is filled only in DAYS mode.

NET-BANKING-ONLY NOTE
=====================
UK's portal only offers "SBI (Multi Bank Payment)" net banking — there is no
UPI / QR flow. The human completes the net-banking payment themselves at the
handover; we pass poll_qr=False and a 15-minute window (HANDOVER_TIMEOUT_SECS
in the shared runner) and poll the page for the receipt in the background.
The moment the receipt renders we capture the PDF and finish — we never block
on an explicit human "done", since they may pay and never notify us.
run_job.py treats UK as scripted-eligible even though paymentMethod != "upi"
(see scripted.runner.state_is_net_banking_scripted), so this runs scripted
regardless of source or paymentMethod.
"""

from __future__ import annotations

import re

from ..log import StepLogger
from ..types import RunOutcome
from .params import BorderTaxParams
from ._payment_wait import PaymentCaptureConfig
from ._handover_runner import StateHandoverConfig, run_handover_flow


# ─── Receipt parsing ───────────────────────────────────────────────────

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
    """Scrape receiptNumber, amount, paymentDate from the receipt page text.

    Generic regex set (same as HP/BR/PB) — no confirmed UK receipt-number
    prefix yet, so we accept any alphanumeric receipt number. Tighten once
    a real UK receipt sample is available.
    """
    from ..steps import _cdp_eval

    text = await _cdp_eval(session, "document.body.innerText || ''")
    if not text:
        return None

    receipt_match = re.search(
        r"Receipt\s*No\.?\s*:?\s*([A-Z0-9]+)",
        text,
        re.IGNORECASE,
    )
    receipt_number = receipt_match.group(1) if receipt_match else None

    amount_match = re.search(
        r"Grand\s*Total\s*:?\s*(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    amount = amount_match.group(1) if amount_match else None

    date_match = re.search(
        r"Payment\s*Confirmation\s*Date\s*:?\s*([\d]{1,2}-\w{3}-\d{4})",
        text,
        re.IGNORECASE,
    )
    payment_date = _normalize_receipt_date(date_match.group(1)) if date_match else None

    if not (receipt_number or amount):
        return None
    return {
        "receiptNumber": receipt_number,
        "amount": amount,
        "paymentDate": payment_date,
        "vehicleNumber": vehicle_number,
    }


# ─── Config ────────────────────────────────────────────────────────────

# poll_qr=False is passed inside run_handover_flow (UK has no UPI/QR), so
# qr_selector is unused but the dataclass field is required.
_UK_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Uttarakhand",
    qr_selector="img#qrcodeImg",  # unused (poll_qr=False at the call site)
    receipt_markers=[
        "GOVERNMENT OF UTTARAKHAND",
        "CHECKPOST TAX E-RECEIPT",
        "Receipt No",
        # "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*uttarakhand",
        r"checkpost\s*tax\s*e-?receipt",
    ],
    negative_markers_regex=[
        r"transaction\s*status\s*[:\-]?\s*pending",
        r"your\s*transaction\s*status\s*is\s*pending",
        r"transaction\s*confirmation\s*pending",
        r"transaction\s*status\s*[:\-]?\s*failed",
        r"transaction\s*failed",
        r"payment\s*failed",
    ],
)

_UK_CONFIG = StateHandoverConfig(
    state_code="UK",
    state_name="Uttarakhand",
    checkpost_strategy="first_option",  # ASHARODI/KULHAL/TIMLI/TUNI ≠ district
    payment_config=_UK_PAYMENT_CONFIG,
    extract_receipt_fields=_extract_receipt_fields,
)


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run UK scripted border-tax. Always returns a RunOutcome."""
    return await run_handover_flow(session, params, log, config=_UK_CONFIG)

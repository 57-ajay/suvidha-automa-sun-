# worker/src/scripted/border_tax/hp.py
"""Himachal Pradesh border-tax scripted runner.

NO AI. Pure scripted form-fill (parivahan Phases 1-5), then hand the
captcha + payment to a human and poll for the receipt in the background.
See `_handover_runner.run_handover_flow` for the full phase walk and the
handover semantics.

HP specifics (everything else is the shared parivahan flow):
  - Phase 1 state value: "HP".
  - Phase 3 Entry District default: TIPRA (set via params/_STATE_DEFAULTS).
  - Phase 3 Entry Checkpost: the checkpost name equals the district name on
    HP, so checkpost_strategy="match_district" (falls back to the first
    option if the names ever diverge).
  - Phase 4: Vehicle Category (LIGHT PASSENGER VEHICLE, usually pre-filled),
    Permit Type (default TEMPORARY PERMIT), Service Type (NOT APPLICABLE) —
    all "leave if already filled, set if empty".
  - Phase 5 Tax Mode: DAYS (default) / QUARTERLY / YEARLY. Tax Upto is
    filled only in DAYS mode. Tax From/Upto are datetime-local inputs.

A "pending transaction" popup after Get Details aborts immediately with a
"clear it first" error (NO auto-clear for HP).
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
    """Scrape receiptNumber, amount, paymentDate from the receipt page text.

    Generic regex set (same as UK/PB) — no confirmed HP receipt-number
    prefix yet, so we accept any alphanumeric receipt number. Tighten once
    a real HP receipt sample is available.
    """
    from ..steps import _cdp_eval

    text = await _cdp_eval(session, "document.body.innerText || ''")
    if not text:
        return None

    receipt_match = re.search(
        r"Receipt\s*No\.?\s*:?\s*([A-Z0-9]+)", text, re.IGNORECASE,
    )
    receipt_number = receipt_match.group(1) if receipt_match else None

    amount_match = re.search(
        r"Grand\s*Total\s*:?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE,
    )
    amount = amount_match.group(1) if amount_match else None

    date_match = re.search(
        r"Payment\s*Confirmation\s*Date\s*:?\s*([\d]{1,2}-\w{3}-\d{4})",
        text, re.IGNORECASE,
    )
    payment_date = (
        _normalize_receipt_date(date_match.group(1)) if date_match else None
    )

    if not (receipt_number or amount):
        return None
    return {
        "receiptNumber": receipt_number,
        "amount": amount,
        "paymentDate": payment_date,
        "vehicleNumber": vehicle_number,
    }


# ─── Config ────────────────────────────────────────────────────────────

_HP_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Himachal Pradesh",
    qr_selector="img#qrcodeImg",  # unused (poll_qr=False at the call site)
    receipt_markers=[
        "GOVERNMENT OF HIMACHAL PRADESH",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*himachal\s*pradesh",
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

_HP_CONFIG = StateHandoverConfig(
    state_code="HP",
    state_name="Himachal Pradesh",
    checkpost_strategy="match_district",   # checkpost name == district name
    payment_config=_HP_PAYMENT_CONFIG,
    extract_receipt_fields=_extract_receipt_fields,
)


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run HP scripted border-tax. Always returns a RunOutcome."""
    return await run_handover_flow(session, params, log, config=_HP_CONFIG)

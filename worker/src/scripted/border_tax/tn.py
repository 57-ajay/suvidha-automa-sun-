# worker/src/scripted/border_tax/tn.py
"""Tamil Nadu border-tax scripted runner.

NO AI. Pure scripted form-fill (parivahan Phases 1-5), then hand the
captcha + payment to a human and poll for the receipt in the background.
See `_handover_runner.run_handover_flow` for the full phase walk and the
handover semantics. Identical in shape to HP / BR / UK.

TN specifics:
  - Phase 1 state value: "TN".
  - Phase 3 Entry District default: KRISHNAGIRI.
      ⚠️ GUESS — TN's district dropdown wasn't captured. KRISHNAGIRI (Hosur
      border, the high-traffic Karnataka→TN cab entry) is a placeholder. If a
      request doesn't supply entryDistrict and this doesn't match an option,
      Phase 3 falls back to the first district in the list (which may be the
      wrong border). Confirm the correct district text and update
      _STATE_DEFAULTS["TN"]["entryDistrict"] in params.py / defaults.ts.
  - Phase 3 Entry Checkpost: checkpost_strategy="first_option" (we have no
    confirmed checkpost↔district correlation for TN, same assumption as BR).
    If TN checkposts track the district name, switch to "match_district".
  - Phase 4: Vehicle Category (LPV is the sole non-placeholder → picked when
    empty) / Permit Type / Service Type (NOT APPLICABLE) — "leave if filled,
    set if empty". NO Distance field (like BR/PB/MP).
      Permit Type default: "ALL INDIA TOURIST PERMIT" (value 103), fallback
      "CONTRACT CARRIAGE PERMIT" (value 102).
      ⚠️ This drives the Permit Fee line on the tax table. ALL INDIA TOURIST
      PERMIT is the typical interstate-tourist-cab choice; change the default
      if your vehicles run on contract-carriage permits.
  - Phase 5 Tax Mode: TN offers WEEKLY (value 2) / MONTHLY (value 4) /
    QUARTERLY (value 5) — there is NO DAYS option. The runner matches the
    requested mode by <option> text (WEEKLY/MONTHLY/QUARTERLY all resolve via
    text; note MONTHLY is value 4 on TN, not the 3 in the shared value-map,
    so text match is what carries it). If the requested mode isn't offered
    for the RC, the run aborts with abort_reason="tax_mode_not_offered_for_rc"
    listing the offered modes.
  - Phase 5 Tax Upto: AUTO-DERIVED by the portal for WEEKLY/MONTHLY/QUARTERLY
    (e.g. WEEKLY → Tax From + 6 days). The shared runner fills Tax Upto only
    in DAYS mode, so for TN it only sets Tax From and lets the portal compute
    Tax Upto. Tax From/Upto are type="date" (YYYY-MM-DD), handled by
    `_set_date_like`. The tax table sums multiple rows (MV Tax + Service/User
    Charge + Permit Fee) into the Grand Total; the shared amount extractor
    sums all rows.

A "pending transaction" popup after Get Details aborts immediately with a
"clear it first" error (NO auto-clear), matching HP/BR/UK.
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

    Generic regex set (same as HP/BR/UK/PB) — no confirmed TN receipt-number
    prefix yet, so we accept any alphanumeric receipt number. Tighten once a
    real TN receipt sample is available.
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

_TN_PAYMENT_CONFIG = PaymentCaptureConfig(
    state_name="Tamil Nadu",
    qr_selector="img#qrcodeImg",  # unused (poll_qr=False at the call site)
    receipt_markers=[
        "GOVERNMENT OF TAMIL NADU",
        "CHECKPOST TAX E-RECEIPT",
        "RECEIPT NO",
        "GRAND TOTAL",
    ],
    positive_markers_regex=[
        r"payment\s*successful",
        r"transaction\s*successful",
        r"successfully\s*paid",
        r"transaction\s*status\s*[:\-]?\s*success",
        r"government\s*of\s*tamil\s*nadu",
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

_TN_CONFIG = StateHandoverConfig(
    state_code="TN",
    state_name="Tamil Nadu",
    checkpost_strategy="first_option",   # no confirmed checkpost↔district map
    payment_config=_TN_PAYMENT_CONFIG,
    extract_receipt_fields=_extract_receipt_fields,
)


# ─── Public entrypoint ─────────────────────────────────────────────────


async def run(
    session,
    params: BorderTaxParams,
    log: StepLogger,
) -> RunOutcome:
    """Run TN scripted border-tax. Always returns a RunOutcome."""
    return await run_handover_flow(session, params, log, config=_TN_CONFIG)

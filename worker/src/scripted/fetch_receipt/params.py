# worker/src/scripted/fetch_receipt/params.py
"""FetchReceiptParams: validated job parameters for the fetch-receipt runner.

Required fields mirror the API's requiredParams: driverId, vehicleNumber,
requestId, paymentDate (YYYY-MM-DD), stateName.

stateName accepts either the full name (e.g. "UTTAR PRADESH") or the
2-letter code (e.g. "UP"). All 22 states that appear on the parivahan
Print-Payment-Receipt dropdown are supported (see STATE_NAME_TO_CODE).
The validator normalizes whatever caller sent into the 2-letter code,
which is what the <select>'s option `value` attribute expects.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Mapping covers every option that appears on the parivahan checkpostv4
# Print-Payment-Receipt dropdown as of 2026-05. The `value` attribute on
# each <option> is the 2-letter code on the right; the human-readable
# label is what callers usually pass. A few common aliases are included
# for forgiving input.
STATE_NAME_TO_CODE: dict[str, str] = {
    "ANDHRA PRADESH": "AP",
    "ASSAM": "AS",
    "BIHAR": "BR",
    "UT OF DNH AND DD": "DD",
    "DNH AND DD": "DD",
    "DAMAN AND DIU": "DD",
    "GOA": "GA",
    "HIMACHAL PRADESH": "HP",
    "HARYANA": "HR",
    "JHARKHAND": "JH",
    "JAMMU & KASHMIR": "JK",
    "JAMMU AND KASHMIR": "JK",
    "KARNATAKA": "KA",
    "MADHYA PRADESH": "MP",
    "MIZORAM": "MZ",
    "ODISHA": "OR",
    "ORISSA": "OR",
    "PUNJAB": "PB",
    "PUDUCHERRY": "PY",
    "SIKKIM": "SK",
    "TELANGANA": "TG",
    "TAMIL NADU": "TN",
    "TRIPURA": "TR",
    "UTTRAKHAND": "UK",
    "UTTARAKHAND": "UK",
    "UTTAR PRADESH": "UP",
    "U.P.": "UP",
    "WEST BENGAL": "WB",
}

VALID_STATE_CODES: set[str] = set(STATE_NAME_TO_CODE.values())


def resolve_state_code(state_name_or_code: str) -> str | None:
    """Map any input to a 2-letter code valid on the parivahan dropdown.
    Returns None if the input doesn't resolve to a known state."""
    key = (state_name_or_code or "").strip().upper()
    if not key:
        return None
    if key in VALID_STATE_CODES:
        return key
    return STATE_NAME_TO_CODE.get(key)


class FetchReceiptParams(BaseModel):
    # required
    driverId: str = Field(min_length=1)
    vehicleNumber: str = Field(min_length=4, max_length=20)
    requestId: str = Field(min_length=1)
    paymentDate: str  # YYYY-MM-DD
    stateName: str  # full name or 2-letter code; validator -> 2-letter code

    # optional
    receiptNo: str | None = None

    # plumbing — injected by runner.run_fetch_receipt() from job hash,
    # not by the API params. Defaults to "web" so unit-testing the
    # pydantic model in isolation doesn't blow up.
    source: Literal["app", "web"] = "web"

    @field_validator("vehicleNumber")
    @classmethod
    def _normalize_vehicle(cls, v: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", v).upper()

    @field_validator("paymentDate")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        if not _ISO_DATE.match(v):
            raise ValueError(
                f"paymentDate must be YYYY-MM-DD (got {v!r}); the API "
                f"should normalize before queueing"
            )
        return v

    @field_validator("stateName")
    @classmethod
    def _resolve_state(cls, v: str) -> str:
        code = resolve_state_code(v)
        if code is None:
            raise ValueError(
                f"unknown stateName {v!r}; expected a full state name or "
                f"2-letter code (one of: {sorted(VALID_STATE_CODES)})"
            )
        return code

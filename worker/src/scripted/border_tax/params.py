# worker/src/scripted/border_tax/params.py
"""BorderTaxParams: validated job parameters for the scripted runner.

Mirrors api/src/tasks/borderTax/index.ts optionalParams + adds the new
permit-type fallback chain we agreed on:

    permitType         primary (default: "ALL INDIA TOURIST PERMIT")
    permitTypeFallback used if primary isn't in the dropdown for this
                       vehicle (default: "TEMPORARY PERMIT")

The TS layer is responsible for normalization (dates -> YYYY-MM-DD,
state code -> full name or short code, etc.). The worker just trusts the
shapes the model declares here; ValidationError -> immediate job failure
before the browser even starts.

serviceType NOTE: this is a plain `str` (not a Literal) because each state
uses different option labels. UP expects "Air Conditioned Service" /
"Ordinary Service"; HR uses "NOT APPLICABLE"; RJ has its own list. The
state-specific runner is responsible for sending an option that actually
exists in that state's Service Type dropdown.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BorderTaxParams(BaseModel):
    # required
    vehicleNumber: str = Field(min_length=4, max_length=20)
    requestId: str = Field(min_length=1)
    taxFrom: str  # YYYY-MM-DD
    taxUpto: str  # YYYY-MM-DD

    # state + defaults
    state: Literal[
        "UP",
        "HR",
        "RJ",
        "PB",
        "UTTAR PRADESH",
        "HARYANA",
        "RAJASTHAN",
        "PUNJAB",
    ] = "UP"

    taxMode: Literal["DAYS", "MONTHLY", "QUARTERLY"] = "DAYS"

    entryDistrict: str = ""
    entryCheckpoint: str = ""

    # Per-state Service Type label. UP: "Air Conditioned Service" /
    # "Ordinary Service". HR: "NOT APPLICABLE". RJ: its own list. The
    # state runner forwards whatever string the API resolved.
    serviceType: str = "Air Conditioned Service"

    permitType: str = "ALL INDIA TOURIST PERMIT"
    permitTypeFallback: str = "TEMPORARY PERMIT"

    paymentMethod: Literal["upi", "net_banking"] = "upi"

    # plumbing
    driverId: str | None = None
    source: Literal["app", "web"] = "web"

    # state-specific extras (passthrough; states ignore what they don't use)
    distance: str | None = None
    bankName: str | None = None
    sbiUserId: str | None = None
    sbiPassword: str | None = None

    @field_validator("vehicleNumber")
    @classmethod
    def _normalize_vehicle(cls, v: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", v).upper()

    @field_validator("taxFrom", "taxUpto")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        if not _ISO_DATE.match(v):
            raise ValueError(
                f"date must be YYYY-MM-DD (got {v!r}); the API should "
                f"normalize before queueing"
            )
        return v

    @field_validator("entryDistrict", "entryCheckpoint")
    @classmethod
    def _trim_upper(cls, v: str) -> str:
        return (v or "").strip().upper()

    def is_upi(self) -> bool:
        return self.paymentMethod == "upi"

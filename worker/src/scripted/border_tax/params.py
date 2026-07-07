# worker/src/scripted/border_tax/params.py
"""BorderTaxParams: validated job parameters for the scripted runner.

Mirrors api/src/tasks/borderTax/index.ts optionalParams + adds the new
permit-type fallback chain we agreed on:

    permitType         primary (default: "ALL INDIA TOURIST PERMIT")
    permitTypeFallback used if primary isn't in the dropdown for this
                       vehicle (default: "TEMPORARY PERMIT")

Default resolution order (same rule as the TS layer):
  1. Caller-supplied value   → used as-is.
  2. State-specific default  → _STATE_DEFAULTS table (model_validator).
  3. Field-level default     → Pydantic fallback (last resort / unknown state).

This means BorderTaxParams is the single source of truth for the worker side,
just as states/defaults.ts is for the TS/prompt side.  The two tables must
stay in sync — if you change a default in one, change it in the other.

serviceType NOTE: this is a plain `str` (not a Literal) because each state
uses different option labels. UP expects "Air Conditioned Service" /
"Ordinary Service"; HR/PB use "NOT APPLICABLE"; RJ has its own list; MP
expects "Air Conditioned Service" (matches UP). The state-specific runner
is responsible for sending an option that actually exists in that state's
Service Type dropdown.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_STATE_ALIAS: dict[str, str] = {
    "UTTAR PRADESH": "UP",
    "U.P.": "UP",
    "HARYANA": "HR",
    "RAJASTHAN": "RJ",
    "PUNJAB": "PB",
    "MADHYA PRADESH": "MP",
    "M.P.": "MP",
    "UTTARAKHAND": "UK",
    "HIMACHAL PRADESH": "HP",
    "H.P.": "HP",
    "BIHAR": "BR",
    "TAMIL NADU": "TN",
    "TAMILNADU": "TN",
    "T.N.": "TN",
}

_STATE_DEFAULTS: dict[str, dict[str, str]] = {
    "UP": {
        "taxMode": "DAYS",
        "entryDistrict": "GHAZIABAD",
        "entryCheckpoint": "",
        "serviceType": "Air Conditioned Service",
        "permitType": "ALL INDIA TOURIST PERMIT",
        "permitTypeFallback": "TEMPORARY PERMIT",
        "paymentMethod": "upi",
        "distance": "1000",
    },
    "HR": {
        "taxMode": "DAYS",
        "entryDistrict": "FARIDABAD",
        "entryCheckpoint": "FARIDABAD",
        "serviceType": "NOT APPLICABLE",
        "permitType": "NOT APPLICABLE",
        "permitTypeFallback": "NOT APPLICABLE",
        "paymentMethod": "upi",
        "distance": "1000",
    },
    "RJ": {
        "taxMode": "DAYS",
        "entryDistrict": "CHITTORGARH",
        "entryCheckpoint": "",
        "serviceType": "NOT APPLICABLE",
        "permitType": "TEMPORARY PERMIT",
        "permitTypeFallback": "TEMPORARY PERMIT",
        "paymentMethod": "upi",
        "bankName": "State Bank Of India",
    },
    "PB": {
        "taxMode": "DAYS",
        "entryDistrict": "MOHALI",
        "entryCheckpoint": "",
        "serviceType": "NOT APPLICABLE",
        "permitType": "NOT APPLICABLE",
        "permitTypeFallback": "NOT APPLICABLE",
        "paymentMethod": "upi",
    },
    "MP": {
        "taxMode": "DAYS",
        "entryDistrict": "SHEOPUR",
        "entryCheckpoint": "",
        "serviceType": "Air Conditioned Service",
        "permitType": "TEMPORARY PERMIT",
        "permitTypeFallback": "TEMPORARY PERMIT",
        "paymentMethod": "upi",
    },
    "UK": {
        "taxMode": "DAYS",
        "entryDistrict": "DEHRADUN",
        "entryCheckpoint": "",
        "serviceType": "Air Conditioned Service",
        "permitType": "TEMPORARY PERMIT",
        "permitTypeFallback": "ALL INDIA TOURIST PERMIT",
        "paymentMethod": "net_banking",
    },
    "HP": {
        "taxMode": "DAYS",
        "entryDistrict": "TIPRA",
        "entryCheckpoint": "",
        "serviceType": "NOT APPLICABLE",
        "permitType": "TEMPORARY PERMIT",
        "permitTypeFallback": "TEMPORARY PERMIT",
        "paymentMethod": "net_banking",
    },
    "BR": {
        "taxMode": "DAYS",
        "entryDistrict": "PATNA",
        "entryCheckpoint": "",
        "serviceType": "NOT APPLICABLE",
        "permitType": "TEMPORARY PERMIT",
        "permitTypeFallback": "TEMPORARY PERMIT",
        "paymentMethod": "net_banking",
    },
    "TN": {
        "taxMode": "WEEKLY",
        "entryDistrict": "KRISHNAGIRI",
        "entryCheckpoint": "",
        "serviceType": "NOT APPLICABLE",
        "permitType": "ALL INDIA TOURIST PERMIT",
        "permitTypeFallback": "CONTRACT CARRIAGE PERMIT",
        "paymentMethod": "upi",
    },
}


class BorderTaxParams(BaseModel):
    # required
    vehicleNumber: str = Field(min_length=4, max_length=20)
    requestId: str = Field(min_length=1)
    taxFrom: str  # YYYY-MM-DD
    taxUpto: str  # YYYY-MM-DD

    # Optional 24h "HH:MM" (IST) stamped on Tax From / Tax Upto in the
    # datetime-local states (HR, PB, HP; UK/BR when their fields render as
    # datetime-local). ONE time for BOTH ends so the From->Upto span stays
    # an exact 24h multiple. Date-only states (UP, MP, RJ, TN) ignore it.
    # None/blank -> the runner stamps the current IST time. A same-day past
    # time is clamped to now at fill time (see _tax_time.resolve_hhmm).
    taxTime: str | None = None

    # state + defaults
    state: Literal[
        "UP",
        "HR",
        "RJ",
        "PB",
        "MP",
        "UTTAR PRADESH",
        "HARYANA",
        "RAJASTHAN",
        "PUNJAB",
        "MADHYA PRADESH",
        "UK",
        "UTTARAKHAND",
        "HP",
        "BR",
        "HIMACHAL PRADESH",
        "BIHAR",
        "TN",
        "TAMIL NADU",
    ] = "UP"

    # Tax modes supported across all states. Per-state validity is
    # enforced inside the state runner (e.g. PB rejects MONTHLY, MP
    # rejects everything except DAYS).
    taxMode: Literal["DAYS", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"] = "DAYS"

    entryDistrict: str = ""
    entryCheckpoint: str = ""

    # Per-state Service Type label. UP/MP: "Air Conditioned Service" /
    # "Ordinary Service". HR/PB: "NOT APPLICABLE". RJ: its own list.
    # The state runner forwards whatever string the API resolved.
    # Field default is intentionally empty — _apply_state_defaults fills it.
    serviceType: str = ""

    permitType: str = ""
    permitTypeFallback: str = ""

    paymentMethod: Literal["upi", "net_banking"] = "upi"

    # plumbing
    driverId: str | None = None
    mobileNumber: str | None = None
    source: Literal["app", "web"] = "web"

    # Full Firestore `vehicleDetails/{REGNO}` record, attached by the API at
    # queue time. Used ONLY when VAHAN's "Get Details" returns no data and we
    # must fill the owner-info + vehicle-info forms manually. The API stores
    # it as a JSON string inside the flat params map, so accept both a dict
    # (direct) and a JSON string (from Redis).
    vehicleDetails: dict | None = None

    # state-specific extras (passthrough; states ignore what they don't use)
    distance: str | None = None
    bankName: str | None = None
    sbiUserId: str | None = None
    sbiPassword: str | None = None

    # ── field validators ──────────────────────────────────────────────────

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

    @field_validator("taxTime")
    @classmethod
    def _validate_tax_time(cls, v: str | None) -> str | None:
        """Blank -> None (runner falls back to current IST time). Otherwise
        require 24h H:MM / HH:MM (seconds tolerated, dropped) and normalize
        to zero-padded "HH:MM" so lexicographic compares work downstream."""
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(
                f"taxTime must be 24h HH:MM (got {v!r}); the API should "
                f"normalize before queueing"
            )
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    @field_validator("entryDistrict", "entryCheckpoint")
    @classmethod
    def _trim_upper(cls, v: str) -> str:
        return (v or "").strip().upper()

    @field_validator("vehicleDetails", mode="before")
    @classmethod
    def _parse_vehicle_details(cls, v):
        """Accept a dict, a JSON string (how the API ships it through Redis),
        or empty → None. Never raise on bad JSON — a malformed record just
        means no manual-fill fallback, not a hard validation failure."""
        if v is None or v == "":
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
        return None

    # ── model validator: apply state-specific defaults ────────────────────

    @model_validator(mode="after")
    def _apply_state_defaults(self) -> "BorderTaxParams":
        """
        Fill any field that the caller left absent or empty-string with the
        state-specific default from _STATE_DEFAULTS.

        Runs after all field validators, so `self.state` is already
        normalized to its short code (or kept as full name — we resolve
        via _STATE_ALIAS regardless).

        The logic mirrors applyStateDefaults() in
        api/src/tasks/borderTax/states/defaults.ts — keep them in sync.
        """
        raw = (self.state or "").strip().upper()
        code = _STATE_ALIAS.get(raw, raw)  # "PUNJAB" → "PB", "PB" → "PB"
        defaults = _STATE_DEFAULTS.get(code)
        if not defaults:
            return self  # unknown state — leave whatever Pydantic set

        # For each state default, apply it only if the field is falsy
        # (absent, "", None).  Explicit caller values always win.
        str_fields = {
            "taxMode", "entryDistrict", "entryCheckpoint",
            "serviceType", "permitType", "permitTypeFallback",
            "paymentMethod",
        }
        nullable_fields = {"distance", "bankName"}

        for field, value in defaults.items():
            if field in str_fields:
                if not getattr(self, field, ""):
                    object.__setattr__(self, field, value)
            elif field in nullable_fields:
                if getattr(self, field, None) is None:
                    object.__setattr__(self, field, value)

        return self

    def is_upi(self) -> bool:
        return self.paymentMethod == "upi"

"""ChallanPaymentParams: validated job parameters for the scripted challan
runner. Mirrors api/src/tasks/challanPayment/index.ts (required + optional).

Same role BorderTaxParams plays for border-tax: the single source of truth
for the worker side. Validated in scripted.challan.runner before any browser
work, so bad inputs fail fast.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChallanPaymentParams(BaseModel):
    # ── required ──────────────────────────────────────────────────────────
    requestId: str = Field(min_length=1)
    vehicleNumber: str = Field(min_length=4, max_length=20)
    challanNo: str = Field(min_length=1)

    # ── optional ──────────────────────────────────────────────────────────
    chassisNo: str | None = None
    engineNo: str | None = None
    # If provided, used VERBATIM as the Virtual Courts "Select Department"
    # option text. If omitted, the runner derives it from challanNo via
    # scripted.challan.dispatch.department_from_challan.
    department: str | None = None

    # ── plumbing ──────────────────────────────────────────────────────────
    driverId: str | None = None
    source: Literal["app", "web"] = "web"

    # ── validators ────────────────────────────────────────────────────────
    @field_validator("vehicleNumber")
    @classmethod
    def _normalize_vehicle(cls, v: str) -> str:
        # Strip spaces/dashes, uppercase — "HR55AX0643" stays "HR55AX0643".
        return re.sub(r"[^A-Za-z0-9]", "", v).upper()

    @field_validator("challanNo")
    @classmethod
    def _strip_challan(cls, v: str) -> str:
        return (v or "").strip()

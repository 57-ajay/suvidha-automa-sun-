"""ChallanSettlementParams: validated job parameters for the scripted
challan-settlement runner.

Mirrors api/src/tasks/challanSettlement/index.ts:
    requiredParams: vehicleNumber, requestId
    optionalParams: mobileNumber, chassisLastFour, engineLastFour

The mobile/chassis/engine params are DTP-only (Phase 0 mobile-change / OTP) and
are unused here because the scripted flow skips DTP — kept as optional
pass-through for API parity so the same job payload validates cleanly.

Same role BorderTaxParams plays for border-tax: single source of truth for the
worker side, validated in scripted.runner.run_challan_settlement before any
browser work so bad inputs fail fast.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChallanSettlementParams(BaseModel):
    # ── required ──────────────────────────────────────────────────────────
    vehicleNumber: str = Field(min_length=4, max_length=20)
    requestId: str = Field(min_length=1)

    # ── optional (DTP-only; unused in the scripted DTP-skipped flow) ────────
    mobileNumber: str | None = None
    chassisLastFour: str | None = None
    engineLastFour: str | None = None

    # ── plumbing ──────────────────────────────────────────────────────────
    driverId: str | None = None
    source: Literal["app", "web"] = "web"

    @field_validator("vehicleNumber")
    @classmethod
    def _normalize_vehicle(cls, v: str) -> str:
        # Strip spaces/dashes, uppercase — matches border_tax / challan params.
        return re.sub(r"[^A-Za-z0-9]", "", v).upper()

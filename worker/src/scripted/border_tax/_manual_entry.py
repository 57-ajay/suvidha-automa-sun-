# worker/src/scripted/border_tax/_manual_entry.py
"""Manual owner + vehicle-info fill for the parivahan checkpostv4 portal.

WHY THIS EXISTS
───────────────
Phase 3 clicks "Get Details", which pulls the vehicle's RC data from the
central VAHAN database and auto-fills Chassis / Owner / Mobile (owner-info
step) and Vehicle Type / Class / Category / Seating / validity dates
(vehicle-info step). For some vehicles VAHAN has nothing and the portal
shows a SweetAlert2 popup:

    "No data found for this vehicle number. Please enter the details.."

When that happens EVERY field on both steps is empty and we must fill them
ourselves from our own DB record (Firestore `vehicleDetails/{REGNO}`), which
the API attaches to the job as `params.vehicleDetails`.

`_pending_clear.wait_for_owner_info_outcome` now returns "manual_entry" for
this case; the state runner (up.py / hr.py) calls the two public functions
below.

SHARED BY UP + HR. Both use the identical owner-info template and the
identical vehicle-info field IDs. The only per-state differences are:
  • has_category  — UP/HR show a Vehicle Category select; some states don't.
  • has_distance  — HR has a Distance input on vehicle-info; UP doesn't.
  • datetime_local_dates — HR's date inputs are <input type="datetime-local">
    and reject a plain "YYYY-MM-DD"; UP's are <input type="date">.

Field IDs are confirmed against the live DOM (CheckPost V4.7.2) and match
the SEL_* constants already in up.py / hr.py.

The DB record uses mixed date formats (some "DD/MM/YYYY", some "YYYY-MM-DD");
`to_iso_date` normalizes both. Missing validity dates fall back to today
(matches the documented rule: "if we don't have a date, use current date").
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date

from ..log import StepLogger
from ..steps import (
    _cdp_eval,
    fill,
    select_by_text,
    select_by_value,
)
from ..types import StepLog, StepStatus


# ─── Field IDs (owner-info + vehicle-info), shared across states ───────────

SEL_CHASSIS = "input#chassisNo"
SEL_OWNER = "input#floatingOwner"
SEL_MOBILE = "input#mobileNo"
SEL_FROM_STATE = "select#floatingState"

SEL_VEHICLE_TYPE = "select#floatingVehicle"      # 1=TRANSPORT, 2=NON-TRANSPORT
SEL_VEHICLE_CLASS = "select#floatingVehicletype"
SEL_VEHICLE_CATEGORY = "select#floatingVecCat"
SEL_PERMIT_TYPE = "select#floatingPrmit"          # parivahan typo: missing 'e'
SEL_SEATING = "input#floatingSeatingcap"
SEL_SLEEPER = "input#floatingSleeper"
SEL_SERVICE_TYPE = "select#floatingService"
SEL_PERMIT_UPTO = "input#permitUpto"
SEL_PERMIT_NO = "input#floatingPn"
SEL_INSURANCE = "input#insuranceValidity"
SEL_FITNESS = "input#fitnessValidity"
SEL_PUCC = "input#puccValidityF"
SEL_ROAD_TAX = "input#floatingTaxValid"
SEL_DISTANCE = "input#floatingDistance"           # HR only


# RC-plate first-two-letters → From-State dropdown value. The dropdown uses
# 2-letter codes as <option value="…">, which equal the plate prefix for
# most states. These three drifted (plates use the left key, the portal
# dropdown uses the right value).
_STATE_ALIAS: dict[str, str] = {"TS": "TG", "OD": "OR", "UA": "UK"}


# ─── Pure mappers (unit-testable, no browser) ──────────────────────────────


def derive_from_state(reg_no: str) -> str:
    """'HR38AE7922' → 'HR'. Applies the plate→dropdown alias table."""
    code = re.sub(r"[^A-Za-z]", "", reg_no or "")[:2].upper()
    return _STATE_ALIAS.get(code, code)


def ten_digit_mobile(vd: dict) -> str:
    """mobile_number if present, else driverDetails.phoneNo. Stripped to the
    last 10 digits (drops +91 / spaces). '' if nothing usable."""
    raw = vd.get("mobile_number") or (vd.get("driverDetails") or {}).get("phoneNo") or ""
    return re.sub(r"\D", "", str(raw))[-10:]


def _truthy(v) -> bool:
    """Coerce values that may arrive as real bools or as the strings
    'true'/'false' (Firestore fields sometimes round-trip as strings)."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def map_vehicle_type(vd: dict) -> str:
    """'1' TRANSPORT vs '2' NON-TRANSPORT.

    Border tax on this portal is paid by permit-holding commercial vehicles,
    so the answer is TRANSPORT in virtually every case. `is_commercial` alone
    is unreliable — some records carry it false/absent even for permit cabs
    (e.g. RJ14TG3997), which made us pick NON-TRANSPORT and load the
    special-vehicle class list (FORK LIFT, CRANE…) that has no MOTOR CAB.

    So we treat ANY transport signal as TRANSPORT: the commercial flag, the
    presence of a permit, or a passenger/goods vehicle class/category. Only
    when none of those exist do we fall back to NON-TRANSPORT.
    """
    if _truthy(vd.get("is_commercial")):
        return "1"
    if vd.get("permit_number") or vd.get("permit_type") or vd.get("national_permit_number"):
        return "1"
    cls = (vd.get("class") or "").lower()
    if any(k in cls for k in ("cab", "bus", "goods", "carrier", "trailer", "tractor", "transport")):
        return "1"
    cat = (vd.get("vehicle_category") or "").strip().upper()
    if cat in ("LPV", "LGV", "MPV", "MGV", "HPV", "HGV", "HTV", "HMV", "LMV-TRANS"):
        return "1"
    return "2"


def map_vehicle_class_text(vd: dict) -> str | None:
    """Map the DB `class` (e.g. 'Motor Cab(LPV)') to the dropdown option text.
    Falls back to our internal `vehicleType` bucket when `class` is unhelpful.
    Returns None if nothing matches (caller logs + lets select_by_text raise)."""
    c = (vd.get("class") or "").lower()
    t = (vd.get("vehicleType") or "").lower()
    if "maxi" in c:
        return "MAXI CAB"
    if "motor cab" in c:
        return "MOTOR CAB"
    if "omni" in c:
        return "OMNI BUS"
    if "bus" in c:
        return "BUS"
    if "goods" in c:
        return "GOODS CARRIER"
    if t in ("sedan", "hatchback", "suv", "muv", "taxi"):
        return "MOTOR CAB"
    if t in ("tempo", "traveller", "van", "maxi"):
        return "MAXI CAB"
    if t == "bus":
        return "BUS"
    return None


def _today_iso() -> str:
    return date.today().isoformat()


def to_iso_date(v) -> str | None:
    """Normalize a date to 'YYYY-MM-DD'. Accepts 'YYYY-MM-DD' (passthrough),
    'DD/MM/YYYY', and 'DD-MM-YYYY'. Returns None if unparseable."""
    if not v:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _iso_or_today(v) -> str:
    return to_iso_date(v) or _today_iso()


def _stamp(iso: str, datetime_local: bool) -> str:
    """date inputs want 'YYYY-MM-DD'; datetime-local inputs want
    'YYYY-MM-DDTHH:MM' (HR). Append midnight when needed."""
    return f"{iso}T00:00" if datetime_local else iso


# ─── Popup dismissal ───────────────────────────────────────────────────────


_DISMISS_NO_DATA_JS = """
(function() {
  var popups = document.querySelectorAll('.swal2-popup');
  var clicked = 0;
  for (var i = 0; i < popups.length; i++) {
    var r = popups[i].getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var btn = popups[i].querySelector('button.swal2-confirm');
    if (btn) { try { btn.click(); clicked++; } catch (e) {} }
  }
  return {dismissed: clicked > 0, count: clicked};
})()
"""


async def dismiss_no_data_popup(
    session,
    *,
    log: StepLogger,
    name: str = "manual.dismiss_no_data",
) -> None:
    """Click OK on the visible "No data found" SweetAlert2 popup so the
    owner-info fields behind it become interactable. Never raises."""
    import time

    started = time.monotonic()
    try:
        res = await _cdp_eval(session, _DISMISS_NO_DATA_JS)
        clicked = bool(res and res.get("dismissed"))
        log.record(StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.OK if clicked else StepStatus.SKIPPED,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=f"dismissed={clicked} count={(res or {}).get('count')}",
        ))
    except Exception as e:
        log.record(StepLog(
            index=log.next_index(),
            name=name,
            status=StepStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        ))


# ─── Public: owner-info manual fill (phase 3) ──────────────────────────────


async def fill_owner_info_manual(
    session,
    vd: dict,
    *,
    log: StepLogger,
    name_prefix: str = "phase3.manual",
    mobile_number: str | None = None,
) -> None:
    """Fill Chassis / Owner / Mobile / From-State from the DB record.

    `mobile_number` is the job's `mobileNumber` param (the customer/driver
    phone the app collected). It takes priority over the RC record's
    `mobile_number`, which is usually blank (VAHAN rarely returns a phone).
    Falls back to the record only when the param is absent.

    Entry District + Entry Checkpost are NOT filled here — they are the
    physical border crossing (route-specific), not vehicle attributes, and
    the state runner selects them from params right after this returns,
    exactly as it does on the VAHAN-success path.
    """
    reg = vd.get("reg_no") or vd.get("vehicle_number") or ""
    mob = re.sub(r"\D", "", str(mobile_number or ""))[-10:] or ten_digit_mobile(vd)

    await fill(session, SEL_CHASSIS, vd.get("chassis") or "",
               log=log, name=f"{name_prefix}.chassis")
    await fill(session, SEL_OWNER, vd.get("owner") or "",
               log=log, name=f"{name_prefix}.owner")
    await fill(session, SEL_MOBILE, mob,
               log=log, name=f"{name_prefix}.mobile")
    await select_by_value(session, SEL_FROM_STATE, derive_from_state(reg),
                          log=log, name=f"{name_prefix}.from_state")


# ─── Public: vehicle-info manual fill (phase 4) ────────────────────────────


async def _set_category(session, vd: dict, *, log: StepLogger, name: str) -> None:
    """Vehicle Category: the DB value (e.g. 'LPV') equals the <option value>,
    so try value first; fall back to the long option text if that misses."""
    cat = vd.get("vehicle_category") or ""
    try:
        await select_by_value(session, SEL_VEHICLE_CATEGORY, cat,
                              log=log, name=f"{name}.by_value", timeout=15)
        return
    except Exception:
        pass
    text_map = {"LPV": "LIGHT PASSENGER VEHICLE", "LGV": "LIGHT GOODS VEHICLE"}
    await select_by_text(session, SEL_VEHICLE_CATEGORY,
                         text_map.get(cat.upper(), cat),
                         log=log, name=f"{name}.by_text", timeout=15)


async def _set_permit_type(session, params, *, log: StepLogger, name: str) -> None:
    """Permit Type: two-tier fallback (params.permitType → permitTypeFallback),
    same chain the VAHAN-success path uses. select_by_text polls until the
    option appears (it's conditional on the cascade above it)."""
    attempts = []
    if getattr(params, "permitType", ""):
        attempts.append(params.permitType)
    if getattr(params, "permitTypeFallback", "") and params.permitTypeFallback not in attempts:
        attempts.append(params.permitTypeFallback)

    last_err: Exception | None = None
    for i, text in enumerate(attempts):
        label = "primary" if i == 0 else "fallback"
        try:
            await select_by_text(session, SEL_PERMIT_TYPE, text,
                                 log=log, name=f"{name}.{label}", timeout=15)
            return
        except Exception as e:
            last_err = e
    raise RuntimeError(
        f"permit type not settable from {attempts!r}: "
        f"{type(last_err).__name__ if last_err else 'n/a'}"
    )


async def _present(session, selector: str, *, timeout: float = 3.0) -> bool:
    """Quick existence + visibility check. Short timeout because the
    vehicle-info inputs are static (rendered at page load) — we only need to
    confirm the field exists on THIS state's form, not wait for a cascade.

    Returns False if the selector never matches a visible element, which is
    how we skip fields that simply aren't on a given state's form (e.g. HR's
    vehicle-info has no Permit Validity / Permit No / Road Tax, but has a
    Distance field that UP/PB/MP don't)."""
    deadline = time.monotonic() + timeout
    expr = (
        "(function(s){var e=document.querySelector(s);"
        "if(!e) return false;"
        "var r=e.getBoundingClientRect();"
        "return r.width>0 && r.height>0;})(" + json.dumps(selector) + ")"
    )
    while time.monotonic() < deadline:
        if await _cdp_eval(session, expr):
            return True
        await asyncio.sleep(0.2)
    return False


async def _fill_if_present(
    session, selector: str, value: str, *, log: StepLogger, name: str,
) -> bool:
    """fill() the input only if it's actually on this state's form. Logs a
    SKIP (not a failure) when the field is absent and returns False. This is
    what makes the manual fill portable across states whose vehicle-info
    forms render different field sets — without it, fill() blocks ~10s on a
    missing selector and then throws, aborting every field after it."""
    if await _present(session, selector):
        await fill(session, selector, value, log=log, name=name)
        return True
    log.record(StepLog(
        index=log.next_index(),
        name=name,
        status=StepStatus.SKIPPED,
        selector=selector,
        value="<field not on this state's form>",
        duration_ms=0,
    ))
    return False


async def fill_vehicle_info_manual(
    session,
    vd: dict,
    params,
    *,
    log: StepLogger,
    name_prefix: str = "phase4.manual",
    has_category: bool = True,
    has_distance: bool = False,
    datetime_local_dates: bool = False,
) -> None:
    """Fill the entire vehicle-info step from the DB record.

    Order matters: Type → Class → Category → Permit drive a dependent
    cascade (each option list loads only after the previous selection). The
    select_by_* helpers already poll up to their timeout for the option to
    appear, so setting them in sequence is enough.

    `params` supplies the resolved Permit Type / Service Type strings (the
    state-default chain already picked these); everything else comes from
    the DB record `vd`.
    """
    # 1) Vehicle Type → loads Vehicle Class.
    await select_by_value(session, SEL_VEHICLE_TYPE, map_vehicle_type(vd),
                          log=log, name=f"{name_prefix}.vehicle_type")

    # 2) Vehicle Class → loads Vehicle Category.
    cls_text = map_vehicle_class_text(vd)
    if cls_text:
        await select_by_text(session, SEL_VEHICLE_CLASS, cls_text,
                             log=log, name=f"{name_prefix}.vehicle_class")

    # 3) Vehicle Category → loads Permit Type (state-dependent presence).
    if has_category:
        await _set_category(session, vd, log=log, name=f"{name_prefix}.vehicle_category")

    # 4) Permit Type.
    await _set_permit_type(session, params, log=log, name=f"{name_prefix}.permit_type")

    # 5) Capacities. Seating is always present; Sleeper is hidden for some
    #    vehicle classes — fill only if the field is actually rendered.
    await _fill_if_present(session, SEL_SEATING,
                           str(vd.get("vehicle_seat_capacity") or "0"),
                           log=log, name=f"{name_prefix}.seating")
    await _fill_if_present(session, SEL_SLEEPER,
                           str(vd.get("vehicle_sleeper_capacity") or "0"),
                           log=log, name=f"{name_prefix}.sleeper")

    # 6) Service Type (resolved by the state-default chain). Best-effort: if
    #    the option can't be set, log it and keep going so the remaining
    #    fields still get filled and the form state stays fully visible for
    #    debugging. The select_by_text failure log dumps the available
    #    options, which tells us this state's real Service Type values.
    try:
        await select_by_text(session, SEL_SERVICE_TYPE, params.serviceType,
                             log=log, name=f"{name_prefix}.service_type",
                             timeout=15)
    except Exception as e:
        print(
            f"[manual_entry] service type {params.serviceType!r} not set "
            f"({type(e).__name__}); continuing so other fields still fill"
        )

    # 7) Validity dates + Permit No + Distance. These vary by state — UP has
    #    Permit Validity / Permit No / Road Tax; HR has Distance instead and
    #    none of those three. fill-if-present fills whatever this state's form
    #    actually renders and skips the rest. Missing dates fall back to
    #    today; Fitness has no dedicated DB field, so use RC expiry as the
    #    proxy, then today.
    dl = datetime_local_dates
    fitness_iso = (
        to_iso_date(vd.get("fitness_upto"))
        or to_iso_date(vd.get("rc_expiry_date"))
        or _today_iso()
    )
    await _fill_if_present(session, SEL_PERMIT_UPTO,
                           _stamp(_iso_or_today(vd.get("permit_valid_upto")), dl),
                           log=log, name=f"{name_prefix}.permit_validity")
    await _fill_if_present(session, SEL_INSURANCE,
                           _stamp(_iso_or_today(vd.get("vehicle_insurance_upto")), dl),
                           log=log, name=f"{name_prefix}.insurance_validity")
    await _fill_if_present(session, SEL_FITNESS, _stamp(fitness_iso, dl),
                           log=log, name=f"{name_prefix}.fitness_validity")
    await _fill_if_present(session, SEL_PUCC,
                           _stamp(_iso_or_today(vd.get("pucc_upto")), dl),
                           log=log, name=f"{name_prefix}.pucc_validity")
    await _fill_if_present(session, SEL_ROAD_TAX,
                           _stamp(_iso_or_today(vd.get("vehicle_tax_upto")), dl),
                           log=log, name=f"{name_prefix}.road_tax_validity")
    await _fill_if_present(session, SEL_PERMIT_NO, vd.get("permit_number") or "",
                           log=log, name=f"{name_prefix}.permit_no")

    # Distance — present on HR (required), absent on UP/PB/MP. The has_distance
    # hint stays as documentation of intent, but fill-if-present is the real
    # guard, so a state we forget to flag still gets it when the field exists.
    dist = getattr(params, "distance", None) or "1000"
    await _fill_if_present(session, SEL_DISTANCE, str(dist),
                           log=log, name=f"{name_prefix}.distance")

# worker/src/scripted/border_tax/_tax_time.py
"""Tax From / Tax Upto time stamping for the scripted border-tax runners.

Mirrors worker/src/tasks/border_tax/tax_dates.py (the non-scripted path),
kept as a separate small module because the two packages have different
import roots and can't share code directly. Stdlib-only, so no import cycles.

datetime-local states (HP, HR, PB; UK when its fields render that way) accept
"YYYY-MM-DDTHH:MM". We stamp the CURRENT IST time of day, not midnight: a
midnight start wastes the morning of the validity window and can sit below the
Tax-Upto min the portal pins to "now". The SAME time is stamped on BOTH ends
so the From->Upto span stays an exact 24h multiple (the portal bills a minute
over a whole day as a full extra day). Date states send the bare ISO date.

Callers resolve the time ONCE via resolve_hhmm() (caller-requested
taxTime when usable, else the current IST time) and reuse it for both fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# IST is a fixed +05:30 with no DST, so a fixed offset is correct year-round
# and needs no tzdata package on the host.
_IST = timezone(timedelta(hours=5, minutes=30))


def ist_hhmm(now: datetime | None = None) -> str:
    """Current IST wall-clock as 'HH:MM' (datetime-local is minute precision)."""
    return (now or datetime.now(_IST)).strftime("%H:%M")


def stamp_dtlocal(iso_date: str, hhmm: str | None) -> str:
    """Append 'T<hhmm>' for a datetime-local field, or return the bare date
    when hhmm is None (date fields). Idempotent: a value that already carries
    a time (contains 'T') is returned unchanged."""
    s = (iso_date or "").strip()
    if not s or hhmm is None:
        return s
    return s if "T" in s else f"{s}T{hhmm}"


def resolve_hhmm(
    requested: str | None,
    tax_from_iso: str,
    now: datetime | None = None,
) -> str:
    """Pick the HH:MM to stamp on Tax From / Tax Upto.

    The caller-requested taxTime wins when it is still usable; otherwise we
    fall back to the current IST time (the pre-taxTime behavior). "Usable"
    means not already in the past: the portal pins the field's min to "now",
    so a past datetime silently fails to stick. Concretely:

      - no/blank request                     -> current IST time
      - taxFrom is a future date             -> requested time as-is
      - taxFrom is today, time >= now (IST)  -> requested time as-is
      - taxFrom is today, time <  now (IST)  -> clamped to current IST time
        (queue delay can push a "start now-ish" request into the past; the
        driver still gets a permit starting at fill time instead of a
        failed job)
      - taxFrom itself already past          -> requested as-is; the date is
        below min regardless and the runner's existing before-min check
        reports it properly

    Prints a [tax_time] line to job stdout when it deviates from the request
    so operators can see why a permit started later than asked. Zero-padded
    "HH:MM" strings compare correctly as plain strings.
    """
    now = now or datetime.now(_IST)
    req = (requested or "").strip()
    if not req:
        return ist_hhmm(now)

    today_iso = now.strftime("%Y-%m-%d")
    now_hhmm = ist_hhmm(now)
    if tax_from_iso == today_iso and req < now_hhmm:
        print(
            f"[tax_time] requested taxTime {req} on {tax_from_iso} is already "
            f"past (IST now {now_hhmm}) — clamping to now so the value stays "
            f"above the portal's min"
        )
        return now_hhmm
    return req

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

Callers compute ist_hhmm() ONCE and reuse it for both fields.
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

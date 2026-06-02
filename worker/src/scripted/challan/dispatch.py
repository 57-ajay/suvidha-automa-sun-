"""Map a challan number to its Virtual Courts "Select Department" option.

The prefix -> department table is lifted verbatim from the existing
challan-settlement logic (api/src/tasks/challanSettlement/prompt.ts and its
challansFromDB helper). KEEP THE TWO IN SYNC — if you change one, change both.

Rule (identical to challan-settlement "Phase 1.5"):
  - 2 leading LETTERS -> state code -> department via _PREFIX_TO_DEPARTMENT
  - leading DIGIT     -> "Delhi(Notice Department)"
  - anything else     -> None  (caller decides; extend the table or pass an
                                 explicit `department` param)

NOTE: the returned string must match the dropdown OPTION TEXT exactly (the
scripted select scans options for this text). If selection fails on a given
court, open the live dropdown over VNC, copy the exact option label, and
adjust the value here.
"""

from __future__ import annotations

_PREFIX_TO_DEPARTMENT: dict[str, str] = {
    "DL": "Delhi(Traffic Department)",
    "HR": "Haryana(Traffic Department)",
    "UP": "Uttar Pradesh(Traffic Department)",
    "CH": "Chandigarh(Traffic Department)",
    "RJ": "Rajasthan(Traffic Department)",
    "PB": "Punjab(Traffic Department)",
    "MP": "Madhya Pradesh(Traffic Department)",
    "MH": "Maharashtra(Transport Department)",
    "GJ": "Gujarat(Traffic Department)",
    "KA": "Karnataka(Traffic Department)",
    "HP": "Himachal Pradesh(Traffic Department)",
    "UK": "Uttarakhand(Traffic Department)",
    "CG": "Chhattisgarh(Traffic Department)",
    "JK": "Jammu and Kashmir(Jammu Traffic Department)",
    "AS": "Assam(Traffic Department)",
    "KL": "Kerala(Police Department)",
    "TN": "Tamil Nadu(Traffic Department)",
    "AP": "Andhra Pradesh(Traffic Department)",
    "TS": "Telangana(Traffic Department)",
    "TG": "Telangana(Traffic Department)",
    "BR": "Bihar(Traffic Department)",
    "JH": "Jharkhand(Traffic Department)",
    "OD": "Odisha(Traffic Department)",
    "WB": "West Bengal(Traffic Department)",
    "GA": "Goa(Traffic Department)",
}


def department_from_challan(challan_no: str, override: str | None = None) -> str | None:
    """Return the Virtual Courts department for this challan, or None.

    `override` (the API `department` param) always wins — this is the seam
    where you can plug in the richer mapping you mentioned defining later.
    """
    if override:
        return override

    cn = (challan_no or "").strip()
    if not cn:
        return None

    prefix = cn[:2].upper()
    if prefix in _PREFIX_TO_DEPARTMENT:
        return _PREFIX_TO_DEPARTMENT[prefix]
    if cn[:1].isdigit():
        return "Delhi(Notice Department)"

    # TODO: extend for codes/formats not covered above, or require the
    # caller to pass an explicit `department`.
    return None

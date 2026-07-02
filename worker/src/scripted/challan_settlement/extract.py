"""Virtual Courts results extraction for challan-settlement.

Ports the AI prompt's Phase 2 STEP C rules (api/src/tasks/challanSettlement/
prompt.ts:658-707) into deterministic code:

  challanId        ← Challan No. from the record header bar
  offenceText      ← Offence column of the detail table
  fineNumber       ← rightmost "Fine" column (integer)
  proposedFineNum  ← number to the right of "Proposed Fine" (integer)

  discountAmount = proposedFineNum
  originalAmount = price_for_offence(offenceText)  else  fineNumber
  DROP when: challanId empty; discount/original unparseable; discount < 0;
             original <= 0; discount > original.

Two layers, so the DOM-fragile part is isolated:
  - extract_raw_records(session): reads the live page → list of raw dicts.
    ▸▸ The record markup on vcourts.gov.in is not yet confirmed. The current
       implementation parses document.body.innerText heuristically. Once the
       real record DOM is known (record container, challan-no cell, Fine cell,
       Proposed-Fine cell), replace _extract_raw_via_text with a precise
       DOM/CDP extractor. See the "Confirm settlement VC DOMs" task. ◂◂
  - build_discount_records(raws): pure, testable rule application + dedup.
"""

from __future__ import annotations

import re

from .pricing import price_for_offence


RESULTS_MARKER = "No. of Records"
NOT_FOUND_MARKER = "does not exist"


# ─── layer 1: read raw records off the live page ─────────────────────────────


async def _page_inner_text(session) -> str:
    # Local import so this module's pure logic stays importable without the
    # browser/redis stack (keeps build_discount_records unit-testable).
    from ..steps import _cdp_eval

    return await _cdp_eval(session, "document.body.innerText || ''") or ""


def _to_int(s: str | None) -> int | None:
    if s is None:
        return None
    digits = re.sub(r"[^\d]", "", str(s))
    return int(digits) if digits else None


# Anchors used by the heuristic text parser. A record block on the Virtual
# Courts results page contains a challan number and, further down, a
# "Proposed Fine" figure. These regexes are the seam to adjust after seeing
# a real results page (or swap the whole function for a DOM extractor).
_CHALLAN_RE = re.compile(r"Challan\s*No\.?\s*:?\s*([A-Z0-9/\-]+)", re.IGNORECASE)
_PROPOSED_RE = re.compile(r"Proposed\s*Fine\s*:?\s*(?:Rs\.?|₹)?\s*([\d,]+)", re.IGNORECASE)
_FINE_RE = re.compile(r"\bFine\s*:?\s*(?:Rs\.?|₹)?\s*([\d,]+)", re.IGNORECASE)


def _extract_raw_via_text(text: str) -> list[dict]:
    """Best-effort: slice the page text into per-challan blocks and pull the
    challan id, a fine figure, and the proposed fine from each block.

    PROVISIONAL — see module docstring. Correlating fields by text proximity
    is inherently fragile; confirm against a live results page before relying
    on the numbers, and prefer a DOM extractor once the markup is known.
    """
    records: list[dict] = []
    # Split into blocks that each start at a "Challan No" anchor so a
    # block's Fine/Proposed-Fine belong to that challan.
    matches = list(_CHALLAN_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        challan_id = (m.group(1) or "").strip()
        proposed = _PROPOSED_RE.search(block)
        # The Fine column: take the LAST plain "Fine" figure in the block that
        # isn't the proposed one (proposed is matched separately above).
        fine_candidates = _FINE_RE.findall(block)
        fine_val = _to_int(fine_candidates[-1]) if fine_candidates else None
        records.append(
            {
                "challanId": challan_id,
                # Fallback path only: offence text can't be reliably tied to a
                # challan from flat page text, so pricing-table lookup is skipped
                # and originalAmount falls back to fineNumber. The primary DOM
                # extractor (extract_raw_records) sets this correctly.
                "offenceText": "",
                "fineNumber": fine_val,
                "proposedFineNum": _to_int(proposed.group(1)) if proposed else None,
                # The block text carries any status badge, so the skip check
                # still works on the fallback path.
                "statusText": block,
            }
        )
    return records


# Precise DOM extractor for the Virtual Courts results table (confirmed markup).
# The main results table has class `servicestbl`; its own tbody rows come in
# PAIRS: a header-bar row (Sr.No | "…Challan No. : N…Mobile No. :…" | View
# button) followed by a detail-wrapper row whose `td[colspan=3]` holds a nested
# `table.off_tbl` with the offence row(s) [Offence Code|Offence|Act/Section|Fine]
# and a final "Proposed Fine" row [Proposed Fine | N].
#
# `:scope > tbody > tr` restricts to the MAIN table's own rows so the nested
# off_tbl rows aren't mistaken for records.
_EXTRACT_JS = r"""
(function(){
  var out = [];
  var main = document.querySelector('table.servicestbl');
  if(!main) return out;
  var rows = main.querySelectorAll(':scope > tbody > tr');
  var current = null;
  for(var i=0;i<rows.length;i++){
    var row = rows[i];
    var viewBtn = row.querySelector('button[onclick^="view("]');
    if(viewBtn){
      var txt = row.innerText || '';
      var challan = '';
      var m = txt.match(/Challan\s*No\.?\s*:?\s*([A-Za-z0-9\/\-]+)/i);
      if(m){ challan = m[1]; }
      if(!challan){
        var oc = viewBtn.getAttribute('onclick') || '';
        var mm = oc.match(/view\('[^']*','[^']*','([^']*)'/);
        if(mm){ challan = mm[1]; }
      }
      current = {challanId: challan, offenceText:'', fineNumber:null,
                 proposedFineNum:null, statusText: txt};
      out.push(current);
    } else if(current){
      // Detail-wrapper row for the current record. Append its text so a status
      // badge anywhere in the record (e.g. a red "Proceedings of the Challan is
      // yet to be completed" span, "Transferred to Regular Court", etc.) is
      // captured for the skip check — even when there's no off_tbl to parse.
      current.statusText += ' ' + (row.innerText || '');
      var detailTbl = row.querySelector('table.off_tbl');
      if(detailTbl){
        var body = detailTbl.querySelector('tbody') || detailTbl;
        var drows = body.querySelectorAll(':scope > tr');
        for(var j=0;j<drows.length;j++){
          var cells = drows[j].querySelectorAll('td');
          if(cells.length === 0) continue;
          var first = (cells[0].innerText || '').trim();
          var last = (cells[cells.length-1].innerText || '').trim();
          if(/proposed\s*fine/i.test(first)){
            current.proposedFineNum = last;
          } else if(cells.length >= 4){
            if(!current.offenceText){ current.offenceText = (cells[1].innerText || '').trim(); }
            current.fineNumber = last;  // rightmost Fine column of the offence row
          }
        }
      }
    }
  }
  return out;
})()
"""


async def extract_raw_records(session) -> list[dict]:
    """Return raw records from the current VC_RESULTS page. Each is
    {challanId, offenceText, fineNumber, proposedFineNum} (values may be None
    when unreadable — build_discount_records drops those).

    Primary path is the DOM extractor (_EXTRACT_JS). Falls back to a text parse
    only if the DOM query yields nothing (defensive)."""
    from ..steps import _cdp_eval

    raw = await _cdp_eval(session, _EXTRACT_JS)
    if raw:
        return [
            {
                "challanId": (rec.get("challanId") or "").strip(),
                "offenceText": (rec.get("offenceText") or "").strip(),
                "fineNumber": _to_int(rec.get("fineNumber")),
                "proposedFineNum": _to_int(rec.get("proposedFineNum")),
                "statusText": (rec.get("statusText") or "").strip(),
            }
            for rec in raw
        ]
    # Fallback: parse the page text.
    text = await _page_inner_text(session)
    return _extract_raw_via_text(text)


# ─── layer 2: rules + dedup (pure, testable) ────────────────────────────────


# Per-record status badges that mean "not settleable" — ported verbatim from the
# AI prompt's <skip_conditions> (prompt.ts). Matched against the record's full
# status text (header bar + detail row) so a badge span placed anywhere in the
# record is caught. These records are skipped BEFORE any amount is read, so we
# never write a discount for an already-closed challan.
_SKIP_STATUS_PHRASES: list[tuple[str, str]] = [
    ("transferred to regular court", "skip_transferred_regular_court"),
    ("regular court", "skip_transferred_regular_court"),
    ("yet to be completed", "skip_proceedings_pending"),
    ("proceedings of the challan", "skip_proceedings_pending"),
    ("case disposed", "skip_disposed"),
    ("disposed", "skip_disposed"),
    ("warrant", "skip_warrant"),
]
_PAID_RE = re.compile(r"\bpaid\b", re.IGNORECASE)


def _status_skip_reason(status_text: str | None) -> str | None:
    """Return a skip reason if the record's status text marks it non-settleable,
    else None. 'paid' uses a word boundary so 'unpaid'/'prepaid' don't match."""
    if not status_text:
        return None
    s = status_text.lower()
    for needle, reason in _SKIP_STATUS_PHRASES:
        if needle in s:
            return reason
    if _PAID_RE.search(status_text):
        return "skip_paid"
    return None


def _to_discount_record(raw: dict) -> tuple[dict | None, str | None]:
    """Apply the STEP C field rules to one raw record.
    Returns (record, None) on success or (None, drop_reason)."""
    challan_id = (raw.get("challanId") or "").strip()
    if not challan_id:
        return None, "empty_challanId"

    # Skip already-closed challans (paid / transferred / disposed / warrant /
    # proceedings pending) before reading any amount.
    skip = _status_skip_reason(raw.get("statusText"))
    if skip:
        return None, skip

    proposed = raw.get("proposedFineNum")
    if proposed is None:
        return None, "proposed_fine_unreadable"
    discount_amount = int(proposed)

    # originalAmount: pricing table by offence text, else the record's Fine.
    original_amount = price_for_offence(raw.get("offenceText"))
    if original_amount is None:
        fine = raw.get("fineNumber")
        if fine is None:
            return None, "original_unresolvable"
        original_amount = int(fine)

    if original_amount <= 0:
        return None, "zero_or_negative_original"
    if discount_amount < 0:
        return None, "negative_discount"
    if discount_amount > original_amount:
        return None, "discount_exceeds_original"

    return (
        {
            "challanId": challan_id,
            "discountAmount": discount_amount,
            "originalAmount": original_amount,
        },
        None,
    )


def build_discount_records(
    raws: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Turn raw records into valid discount records, deduplicated by challanId.
    Returns (valid_records, dropped) where dropped is a list of
    {challanId, reason}."""
    valid: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for raw in raws:
        rec, reason = _to_discount_record(raw)
        if reason:
            dropped.append({"challanId": (raw.get("challanId") or ""), "reason": reason})
            continue
        assert rec is not None
        if rec["challanId"] in seen:
            dropped.append({"challanId": rec["challanId"], "reason": "duplicate"})
            continue
        seen.add(rec["challanId"])
        valid.append(rec)
    return valid, dropped

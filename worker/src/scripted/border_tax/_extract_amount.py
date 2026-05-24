# worker/src/scripted/border_tax/_extract_amount.py
"""Extract the calculated border-tax amount from the Tax Information table
and persist it to the Redis job hash as `borderTaxAmount`.

Shared by all state-specific border-tax runners (UP / MP / HR / PB). The
parivahan checkpost portal renders the same Tax/Fee table layout on every
state's Tax Information step (Step 3 of 4):

    <table class="table table-bordered mt-2">
      <thead>
        <tr><th>S. No.</th><th>Tax/Fee Particulars</th><th>Tax From</th>
            <th>Tax Upto</th><th class="text-end">Amount</th></tr>
      </thead>
      <tbody>
        <tr><td>1</td><td>MV Tax</td>...<td class="text-end">120</td></tr>
        <!-- multi-row states (MP/PB) add SGST/CGST/Cess rows here -->
      </tbody>
    </table>

We locate the table by its header text ("Tax/Fee Particulars" + "Amount")
rather than by class -- robust against ngcontent attribute churn or minor
class additions. We then sum the LAST <td> of every tbody row.

The total goes into the Redis hash under `borderTaxAmount` so that the
existing GET /api/jobs/:id/status endpoint (which returns hgetall) surfaces
it to the polling client with no API changes.

Best-effort by design: any failure (table missing, parse error, Redis
hiccup) is logged as a RETRIED step and the amount is saved as "0". The
runner continues regardless -- we never want amount extraction to abort a
payment the user has already initiated.
"""

from __future__ import annotations

import time

from ..log import StepLogger
from ..steps import _cdp_eval
from ..types import StepLog, StepStatus


_EXTRACT_JS = r"""
(function() {
  // Find the Tax/Fee table by its header text -- robust against any
  // ngcontent attribute churn or class changes across states.
  var tables = document.querySelectorAll('table');
  var target = null;
  for (var i = 0; i < tables.length; i++) {
    var thead = tables[i].querySelector('thead');
    var headerText = ((thead && thead.innerText) || '').toLowerCase();
    if (headerText.indexOf('tax/fee particulars') >= 0 &&
        headerText.indexOf('amount') >= 0) {
      target = tables[i];
      break;
    }
  }
  if (!target) {
    return {ok: false, reason: 'tax_table_not_found', total: 0, rows: []};
  }

  var trs = target.querySelectorAll('tbody tr');
  if (trs.length === 0) {
    return {ok: false, reason: 'no_rows', total: 0, rows: []};
  }

  var total = 0;
  var rows = [];
  for (var r = 0; r < trs.length; r++) {
    var tds = trs[r].querySelectorAll('td');
    if (tds.length === 0) continue;
    var last = tds[tds.length - 1];
    var raw = (last.innerText || last.textContent || '').trim();
    // Strip everything except digits and "." so Indian number commas, a
    // stray INR icon, or whitespace don't break parsing.
    var cleaned = raw.replace(/[^0-9.]/g, '');
    var n = parseFloat(cleaned);
    var label = tds.length >= 2
      ? (tds[1].innerText || tds[1].textContent || '').trim()
      : '';
    if (!isNaN(n)) {
      total += n;
      rows.push({label: label, amount: n, raw: raw});
    } else {
      rows.push({label: label, amount: null, raw: raw});
    }
  }

  return {ok: true, total: total, rows: rows, rowCount: trs.length};
})()
"""


async def extract_and_save_border_tax_amount(
    session,
    *,
    log: StepLogger,
    name: str = "phase5.extract_border_tax_amount",
) -> float:
    """Read the tax/fee total from the Step 3 table and write it to the
    `job:<id>` hash field `borderTaxAmount` (always stored as a string).
    Returns the extracted amount; returns 0.0 on any failure (and still
    saves "0" to Redis so the client always sees a value).

    Caller should invoke this AFTER clicking "Calculate Fee/Tax" and AFTER
    waiting long enough for the table row to render (~1-4s in practice).
    """
    started = time.monotonic()
    amount = 0.0
    err: str | None = None
    detail: str = ""

    try:
        result = await _cdp_eval(session, _EXTRACT_JS)
        if isinstance(result, dict) and result.get("ok"):
            try:
                amount = float(result.get("total") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            rows = result.get("rows") or []
            row_count = result.get("rowCount") or len(rows)
            row_summary = ", ".join(
                f"{(r.get('label') or '?')}={r.get('amount')!r}"
                for r in rows[:10]
            )
            detail = f"total={amount} rows={row_count} ({row_summary})"
        elif isinstance(result, dict):
            err = result.get("reason") or "unknown_extract_failure"
            detail = f"reason={err}"
        else:
            err = "cdp_eval_returned_non_dict"
            detail = f"raw={result!r}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        detail = f"exception: {err}"

    # Always persist SOMETHING to Redis so the client poll has a key to
    # read. "0" is the sentinel for "we tried but couldn't read it".
    redis_err: str | None = None
    try:
        log.r.hset(
            f"job:{log.job_id}",
            "borderTaxAmount",
            str(amount),
        )
    except Exception as e:
        redis_err = f"{type(e).__name__}: {e}"

    status = (
        StepStatus.OK
        if err is None and redis_err is None and amount > 0
        else StepStatus.RETRIED
    )
    final_err = err if err else (
        f"redis_write_failed: {redis_err}" if redis_err else None
    )

    log.record(
        StepLog(
            index=log.next_index(),
            name=name,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            value=detail,
            error=final_err,
        )
    )
    return amount

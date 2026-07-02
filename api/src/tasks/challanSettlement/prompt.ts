import { challanRequestsRef } from "../../firebase";
import type { JobSource } from "../types";

export const OFFENCE_KEYWORD_PRICES: Array<{ keyword: string; price: number }> = [
    { keyword: "red light", price: 5000 },
    { keyword: "jumping", price: 5000 },
    { keyword: "permit", price: 10000 },
    { keyword: "parking", price: 500 },
    { keyword: "overspeed", price: 2000 },
    { keyword: "over speed", price: 2000 },
];

export function priceForOffence(offence: string | null | undefined): number | null {
    if (!offence) return null;
    const lower = offence.toLowerCase();
    for (const entry of OFFENCE_KEYWORD_PRICES) {
        if (lower.includes(entry.keyword)) return entry.price;
    }
    return null;
}

function renderPricingTable(): string {
    return OFFENCE_KEYWORD_PRICES
        .map(e => `  - offence contains "${e.keyword}" → ₹${e.price}`)
        .join("\n");
}

export const buildPrompt = async (p: Record<string, string>, source: JobSource = "web"): Promise<string> => {
    // Departments are resolved server-side from the challans already stored in the
    // database for this vehicle. We no longer scrape Delhi Traffic Police — this
    // list is the ONLY source of departments to query on Virtual Courts.
    const existingDepartments = await challansFromDB(p);

    const isApp = source === "app";
    const hasDepts = existingDepartments.length > 0;

    // ─── Dynamic blocks ────────────────────────────────────────────────────────

    const executionContextBlock = isApp
        ? `<execution_context mode="app">
Job launched from the mobile app. The human CANNOT see the live browser and is NOT available.
- CAPTCHA: solve it yourself. NEVER call wait_for_human.
- Popups/modals: dismiss yourself by clicking X / OK / Close.
- There is NO OTP step and NO human fallback anywhere in this flow.
</execution_context>`
        : `<execution_context mode="web">
Job launched from the web dashboard. The human CAN see the live browser.
- CAPTCHA: after the retry budget is exhausted, call wait_for_human so the human solves it.
- Popups/modals: dismiss yourself by clicking X / OK / Close.
- There is NO OTP step in this flow.
</execution_context>`;

    const waitForHumanDesc = isApp
        ? `wait_for_human — NOT used in this flow. Never call it.`
        : `wait_for_human — for CAPTCHA only, after the retry budget is exhausted.`;

    const departmentsBlock = hasDepts
        ? existingDepartments.map(d => `  - ${d}`).join("\n")
        : "  (none)";

    // ─── CAPTCHA BLOCK ─────────────────────────────────────────────────────────
    // Goal: maximize patience and break out of solve-loops when results appear.
    // App mode: 10 attempts, no human fallback. Web mode: 5 attempts + human fallback.

    const captchaBlock = isApp
        ? `<captcha_phase mode="app" max_attempts="10">

You are at the captcha step on Virtual Courts. Slow is faster than wrong.
One careful attempt with full reasoning beats three rushed attempts.

<priority_rule>
ABSOLUTE TOP PRIORITY — overrides everything below:
If "No. of Records" + a data table are visible, the captcha is already solved.
STOP. Go to Step C. Do not click Submit again. Do not retype the captcha.
Re-submitting after success can wipe the page. A captcha form rendered next to
results does NOT mean re-solving is needed.
</priority_rule>

<entry_check>
Run at three moments — every time, no exceptions:
  (a) before the very first attempt for this department,
  (b) immediately after every Submit click,
  (c) before every retry.

Step 0 — DESCRIBE THE PAGE in your reasoning, in plain English:
  "Right now I see: ___"
  Type this out. Do not skip. This breaks pattern-matching to the previous step.

Then answer in order:
  Q1. Is "No. of Records" + a numbered data table visible?
      YES → STOP captcha. Go to Step C.
      NO  → continue.
  Q2. Is any popup/dialog/error blocking the page?
      YES → click X / OK / Close. Wait until fully gone. Return to Q1.
      NO  → continue.
  Q3. Is the captcha form visible (image + Enter Captcha field + Submit button)?
      YES → continue to vehicle_field_check.
      NO  → wait 3 seconds. Look again.
            Still no captcha form and no results → SKIP department. Reason: "site error".
</entry_check>

<vehicle_field_check>
Look at the Vehicle Number input now.
  CASE A — empty, or shows anything other than "${p.vehicleNumber}":
    The previous wrong-captcha attempt cleared it.
    1. Click the field.
    2. Press Ctrl+A, then Delete.
    3. Type "${p.vehicleNumber}" character by character.
    4. Read it back from the screen. Confirm it shows "${p.vehicleNumber}" exactly.
       If not, repeat.
  CASE B — already shows "${p.vehicleNumber}" exactly:
    Leave it alone.
This field clears silently after every wrong captcha. Verification is mandatory.
</vehicle_field_check>

<captcha_read_procedure>
The image refreshes after every failed Submit. Always read the LIVE image.
Never type a guess from memory of a previous attempt.

Type R1–R5 in your reasoning. Do not skip any step.

  R1 — DESCRIBE the image in plain words:
       "The captcha shows N characters in [color] text on a [type] background.
        Characters are [straight / slanted / wavy]. There [is/is no] noise/lines."

  R2 — COUNT: state the number of characters explicitly.

  R3 — IDENTIFY each character left to right, one at a time.
       For EACH position state: position, candidate, confidence (high/medium/low),
       and — if not high — list alternatives.
       Example:
         "Char 1: 'K' — high."
         "Char 2: '3' — high."
         "Char 3: 'p' lowercase — medium; could also be 'P' or 'q'."
         "Char 4: '7' — high."
         "Char 5: 'X' — high."
         "Char 6: '2' — medium; could also be 'Z'."

  R4 — RESOLVE ambiguity for medium/low confidence chars.
       Common confusable pairs:
         0/O · 1/l/I · 2/Z · 5/S · 6/G/b · 8/B · 9/g · rn/m · cl/d · vv/w · nn/m
       RETRY ALTERNATION (important):
         If a previous attempt for THIS department failed and you typed one option
         (e.g., 'O'), this time pick the OTHER option (e.g., '0').
         Track which alternative you used last time in your reasoning.

  R5 — ASSEMBLE the final answer as a single string:
       "Final answer: K3p7X2"
</captcha_read_procedure>

<typing_procedure>
T1. Click the "Enter Captcha" field.
T2. Press Ctrl+A, then Delete.
T3. Look at the field. Confirm visually it is COMPLETELY EMPTY before typing.
    A leftover character causes failure. If anything is in the field, repeat T1–T2.
T4. Type the answer from R5 character by character.
T5. Read the field back from the screen. Confirm what's displayed matches R5 EXACTLY
    (including case). If it doesn't match (autocorrect / dropped key) → repeat T1–T5.
</typing_procedure>

<submit_and_observe>
S1. Click Submit.
S2. WAIT 3 seconds. Do not click anything during the wait. The page needs time to render.
S3. Run <entry_check> Q1–Q3 again. Branch on what appears:

    Result A — "No. of Records" + data table:
      Captcha solved. Stop the loop. Go to Step C. Do NOT re-submit.

    Result B — popup "Invalid Captcha":
      Wrong answer. Increment attempt counter.
      In your reasoning, note which char(s) you suspect were misread —
      use this on the next attempt's R4 alternation.
      Close popup. Return to <entry_check>.

    Result C — popup "This number does not exist":
      Not a captcha problem. Close popup. SKIP department. Reason: "not found".

    Result D — any unrecognized popup:
      Close it. SKIP department. Reason: "unexpected popup".

    Result E — no popup, no results after 3 seconds:
      Wait 3 more seconds. Still nothing → SKIP. Reason: "no response".
</submit_and_observe>

<loop_breaker>
Before attempts 4, 7, and 10, do a sanity check:
  • URL — still on the correct VC search page?
  • Header — still shows the correct department name?
  • Captcha image — actually changing between attempts (not frozen)?
  • Results — accidentally loaded but you missed them? Re-run Q1.

Anything wrong → SKIP. Reason: "page state lost".
Everything correct → continue. Slow down further on R1–R5.
Do not take shortcuts to "make up" lost attempts.
</loop_breaker>

<exhaustion>
After 10 failed attempts → SKIP this department.
STATE: dept → SKIPPED (captcha_failed_app).
Never call wait_for_human for captcha in app mode.
This skip IS a failure reason. COMPLETION will force "Status: partial".
If this was the last department → continue to Phase 3 → COMPLETION
with whatever was already saved.
</exhaustion>

<reminders_for_this_block>
★ Results visible = DONE. Never re-submit.
★ Always read the LIVE image. Never reuse a previous answer.
★ Always verify Vehicle Number is "${p.vehicleNumber}" before Submit.
★ Always confirm Enter Captcha field is EMPTY before typing.
★ Always read back what you typed and verify against R5.
★ Always wait 3 seconds after Submit before deciding what happened.
★ When unsure about a character, alternate to the other option on the next attempt.
★ Slow is faster than wrong.
</reminders_for_this_block>

</captcha_phase>`
        : `<captcha_phase mode="web" max_attempts="5">

You are at the captcha step. Slow is faster than wrong.

<priority_rule>
ABSOLUTE TOP PRIORITY — overrides everything below:
If "No. of Records" + a data table are visible, captcha is solved. STOP.
Go to Step C. Do not re-submit. A captcha form next to results does NOT mean re-solve.
</priority_rule>

<entry_check>
Run at: (a) before first attempt, (b) after every Submit, (c) before every retry.

Step 0 — DESCRIBE THE PAGE: "Right now I see: ___" (type it out, do not skip).

Then:
  Q1. "No. of Records" + data table visible? YES → STOP, go to Step C. NO → continue.
  Q2. Popup blocking? YES → close (X / OK / Close), wait until gone, go to Q1. NO → continue.
  Q3. Captcha form visible? YES → vehicle_field_check. NO → wait 3s, look again.
       Still nothing → SKIP. Reason: "site error".
</entry_check>

<vehicle_field_check>
  CASE A — empty or wrong → click, Ctrl+A, Delete, type "${p.vehicleNumber}", read back, confirm.
  CASE B — already "${p.vehicleNumber}" → leave alone.
This field clears silently after wrong captcha. Verification mandatory.
</vehicle_field_check>

<captcha_read_procedure>
Always read the live image. Type R1–R5 in your reasoning.

  R1 — DESCRIBE: "Captcha shows N chars in [color] on [bg]. [slant/noise]."
  R2 — COUNT the characters.
  R3 — IDENTIFY each char L→R with confidence (high/medium/low) and alternatives.
       Example: "Char 1: 'K' — high. Char 2: 'O' — medium; could be '0'."
  R4 — RESOLVE ambiguity. Pairs: 0/O · 1/l/I · 2/Z · 5/S · 6/G/b · 8/B · 9/g · rn/m · cl/d · vv/w · nn/m
       RETRY ALTERNATION: if last attempt failed with one option, pick the OTHER this time.
  R5 — ASSEMBLE: state final answer once. "Final answer: K3p7X2"
</captcha_read_procedure>

<typing_procedure>
T1. Click Enter Captcha field.
T2. Ctrl+A, Delete.
T3. Confirm visually field is EMPTY before typing.
T4. Type R5 answer character by character.
T5. Read back from screen. Confirm matches R5 exactly. If not → repeat T1–T5.
</typing_procedure>

<submit_and_observe>
S1. Click Submit.
S2. WAIT 3 seconds. Do not click anything.
S3. Run <entry_check> Q1–Q3. Branch:
    A — Results table → solved. Go to Step C. Do NOT re-submit.
    B — "Invalid Captcha" → increment counter, close popup, return to <entry_check>.
    C — "This number does not exist" → close, SKIP. Reason: "not found".
    D — Other popup → close, SKIP. Reason: "unexpected popup".
    E — No popup, no results after 3s → wait 3 more. Still nothing → SKIP. Reason: "no response".
</submit_and_observe>

<loop_breaker>
Before attempts 4 and 5: sanity check (URL, header, image changing, results missed).
Anything wrong → SKIP. Reason: "page state lost".
Everything correct → continue, slow down on R1–R5.
</loop_breaker>

<exhaustion>
After 5 failed attempts → call wait_for_human:
  "CAPTCHA on Virtual Courts ([department name]) needs solving. Please solve in
   the browser, click Submit, then reply done."
After human responds → run <entry_check> once.
  Results visible → Step C.
  No results → SKIP. STATE: dept → SKIPPED (captcha_failed).
If wait_for_human returns TIMEOUT → SKIP. STATE: dept → SKIPPED (captcha_failed_human_timeout).
All captcha skips ARE failure reasons → COMPLETION forces "Status: partial".
</exhaustion>

<reminders_for_this_block>
★ Results visible = DONE. Never re-submit.
★ Always read the LIVE image. Never reuse a previous answer.
★ Always verify Vehicle Number is "${p.vehicleNumber}" before Submit.
★ Always confirm Enter Captcha field is EMPTY before typing.
★ Always read back what you typed and verify against R5.
★ Always wait 3 seconds after Submit before deciding what happened.
★ When unsure about a character, alternate on the next attempt.
★ Slow is faster than wrong.
</reminders_for_this_block>

</captcha_phase>`;

    // ─── MAIN PROMPT ───────────────────────────────────────────────────────────

    return `
<role>
You are a precise browser automation agent. Your job: for vehicle ${p.vehicleNumber},
look up traffic-challan settlement discounts on the Indian government Virtual Courts
website and save them via tool calls. Follow the procedure exactly. Verify every
action's result before moving on.
</role>

<context>
<vehicle_number>${p.vehicleNumber}</vehicle_number>

${executionContextBlock}

<tools>
- ${waitForHumanDesc}
- save_discounts — call ONCE PER DEPARTMENT after Step C in Phase 2.
  Each call is independent. Do not accumulate records across departments.

Tool-call protocol — apply before every save_discounts call:
  1. Build the array. Deduplicate by challanId.
  2. Verify count(unique challanIds) === array.length.
  3. Verify every value passes <quality_gates>.
  4. Call the tool. Wait for response. Read the "ok" field.
  5. ok=true → mark CONFIRMED in STATE. ok=false → retry once, else mark FAILED.
</tools>
</context>

<reference>

<extraction_contract>

<virtual_courts_fields>
For each numbered RECORD on the VC results page:
  challanId        ← "Challan No." from the YELLOW/ORANGE HEADER BAR.
                     Not Case No. Not from inside the detail table.
  offenceText      ← Column 2 ("Offence") of the white detail table.
                     Example: "LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE)"
                     Column 1 (Offence Code, a number like 138) is NOT the offence.
                     Column 3 (Act/Section citation) is NOT the offence.
                     The purple/magenta "Punishable Under" block is NOT the offence.
  fineNumber       ← Column 4 — RIGHTMOST column labeled "Fine" — integer.
  proposedFineNum  ← Number to the RIGHT of "Proposed Fine" label, row below the table — integer.

  Derive:
    discountAmount  = proposedFineNum   (literal number on screen)
    originalAmount  = pricing_table lookup on offenceText.
                      No keyword match → originalAmount = fineNumber.
</virtual_courts_fields>

</extraction_contract>

<pricing_table>
Used for: setting originalAmount in Phase 2.
${renderPricingTable()}
Match rules: case-insensitive substring. First match wins.
  No keyword match → originalAmount = fineNumber.
</pricing_table>

<page_visuals>

<page name="VC_HOME">
  URL: https://vcourts.gov.in/virtualcourt/index.php
  Visual: "VIRTUAL COURTS" header. "Select Department" dropdown. "Proceed Now" button.
  The sidebar tabs (Mobile Number, CNR Number, etc.) DO NOT WORK on this page —
  they activate only after a department is selected and Proceed Now is clicked.
  Allowed: select department, click Proceed Now.
</page>

<page name="VC_SEARCH">
  Reached after: department selected + Proceed Now clicked.
  Visual: header shows the department name (e.g. "Delhi (Notice Department)").
  Sidebar tabs functional. Form: Challan Number | Vehicle Number | CAPTCHA image |
  Enter Captcha | Submit.
  PREREQUISITE: header MUST show the department name. If it still says
  "--- Select ---", Step A is incomplete.
  Allowed: click "Challan/Vehicle No." tab, type vehicle number, solve captcha, click Submit.
</page>

<page name="VC_RESULTS">
  Visual: "No. of Records :- N" near top. Numbered records below (1, 2, 3, ...).
  Each record has:
    - Yellow/orange HEADER BAR: Sr.No | Case No. | Challan No. | Party Name | Mobile No. | View
    - White DETAIL TABLE: Offence Code | Offence | Act/Section | Fine
    - "Proposed Fine" row below the detail table — number on the right.
  Status badges: "Paid" (green) | "Transferred to Regular Court" |
  "Proceedings of the Challan is yet to be completed" | "Case Disposed" | "Disposed" |
  "Warrant Issued"
  Allowed: scroll and read ONLY. Never click "View". Never click any button or link.
</page>

</page_visuals>

<skip_conditions>

EARLY-STOP (abort entire task, go to COMPLETION):
  - No departments to query (no challans in the database for this vehicle) → STOP.

PER-DEPARTMENT SKIP (skip dept, continue to next):
  - Virtual Courts site error or blank page → SKIP. Reason: "site error".
  - "This number does not exist" popup → close popup. SKIP. Reason: "not found".
  - "No. of Records :- 0" → SKIP. Reason: "0 records".
  - Captcha exhaustion (per <captcha_phase>) → SKIP. Reason: "captcha failed${isApp ? " (app)" : ""}".
  - Any unrecognized popup → close. SKIP. Reason: "unexpected popup".
  - Stuck for 3+ consecutive steps with no visible progress → SKIP. Reason: "stuck".
  - "Proceed Now" did not transition the page → SKIP. Reason: "proceed_failed".

PER-RECORD SKIP (skip record silently, continue to next record on same page):
  - Header shows "Paid" → already settled. paidSkipped++.
  - "Transferred to Regular Court" → must be paid physically. transferredSkipped++.
  - "Proceedings of the Challan is yet to be completed" (any color/position) → pendingSkipped++.
  - "Case Disposed" or "Disposed" → closed. disposedSkipped++.
  - "Warrant Issued" → cannot settle online. warrantSkipped++.
  - Fine OR Proposed Fine missing, non-numeric, "—", "N/A", or blank → SKIP.

When in doubt: SKIP. Never guess at numbers. Never invent text.
</skip_conditions>

<quality_gates>
Apply before every save_* tool call:
  G1. challanId is a non-empty string with no whitespace.
  G2. discountAmount / originalAmount are integers ≥ 0.
  G3. discountAmount ≤ originalAmount. If not → DROP the record.
  G4. No duplicate challanIds. Deduplicate before calling.
  G5. count(unique challanIds) === array.length.
</quality_gates>

<state_format>
Emit a STATE block at every phase boundary, exactly in this format
(brackets are literal):

[STATE]
phase: <phase_name>
departments:
  - <dept_name>: <CONFIRMED N | SKIPPED reason | FAILED reason | PENDING>
[/STATE]

Mark CONFIRMED only after seeing "ok": true in the tool response.
Never mark CONFIRMED based on intent.
</state_format>

</reference>

<workflow>

<phase id="1" name="determine_departments">
LOGIC ONLY. Do not open any website in this phase.

The system has already resolved the Virtual Courts departments to query, from the
challans stored in the database for ${p.vehicleNumber}:
${departmentsBlock}

If the list above is "(none)" → there are no challans / departments to process for
this vehicle. Emit a STATE block with no departments and go DIRECTLY to COMPLETION
(Status: complete, 0 departments). Do not open any website.

Otherwise:
  Write to memory:
    "Departments to query: [...]"
    "Departments completed: []"
  Initialize STATE: every department → PENDING.
  Proceed to Phase 2.
</phase>

<phase id="2" name="virtual_courts_per_department">
Run STEP A → B → C → D → E for EACH department. Departments are independent.
Do not carry records from one department to another.

--- STEP A — Select department ---

1. Go to https://vcourts.gov.in/virtualcourt/index.php (page VC_HOME).
   Verify VC_HOME visuals. Error or blank → SKIP this dept. Reason: "site error".
2. Do not click sidebar tabs — they don't work on VC_HOME.
3. Click "Select Department" dropdown. Select the current department.
   Verify the dropdown shows the selected name.
4. Click "Proceed Now".
   Verify the page transitioned to VC_SEARCH (header shows the department name).
   If page did not change or shows error → SKIP. STATE: dept → SKIPPED (proceed_failed).

--- STEP B — Search (with captcha) ---

PREREQUISITE: VC_SEARCH header MUST show the department name.
If it still says "--- Select ---", Step A is not complete. Do not proceed.

1. Click the "Challan/Vehicle No." tab.
   Verify the form shows: Vehicle Number input, CAPTCHA image, Enter Captcha field, Submit.
2. Type "${p.vehicleNumber}" in the Vehicle Number field.
3. Enter the captcha-handling phase below. The captcha block has its own retry loop.
   When the captcha block exits successfully, you will be on the results page (VC_RESULTS).
   When the captcha block exits with SKIP, this department is done — proceed to next.

${captchaBlock}

--- STEP C — Extract records ---

PREREQUISITE: "No. of Records :- N" text MUST be visible. If not → SKIP this dept.
"No. of Records :- 0" → STATE: dept → SKIPPED (0 records). Go to Step E.

Initialize for this department:
  thisDeptRecords = []
  paidSkipped = transferredSkipped = pendingSkipped = disposedSkipped = warrantSkipped = 0

FOR EACH numbered record (1, 2, 3, ...) on the page:

  1. Read the header bar and any badges/status text on the full record FIRST.
     Apply PER-RECORD SKIP rules from <skip_conditions>.
     Qualifies for skip → increment matching counter, move to next record.

  2. Verify the white detail table [Offence Code | Offence | Act/Section | Fine]
     AND the "Proposed Fine" row below it are both visible.
     Either missing → SKIP this record. Reason: "proceedings incomplete".

  3. Apply <virtual_courts_fields>:
       challanId        ← Challan No. from the YELLOW/ORANGE HEADER BAR
       offenceText      ← Column 2 (Offence) of the white detail table
       fineNumber       ← Column 4 (RIGHTMOST, "Fine") — integer (NOT Column 1)
       proposedFineNum  ← Number to the RIGHT of "Proposed Fine" label — integer

  4. Cross-check:
     a. Both fineNumber and proposedFineNum must be readable integers.
        "—", "N/A", blank, or non-numeric → SKIP this record.
     b. Set discountAmount = proposedFineNum.
     c. fineNumber should equal proposedFineNum on Virtual Courts (almost always true).
        Unequal → re-read both once on screen. Still unequal → SKIP. Do not invent.

  5. ANTI-ZERO RE-READ (only when discountAmount = 0):
     Stop. Do not add this record yet. Re-read "Proposed Fine" digit by digit, left to right.
     • Screen literally shows "0" alone → keep discountAmount = 0. Mark verified-zero.
     • Screen shows a real number → you misread first time. Update discountAmount.
     • Cannot tell (faded/overlapping/ambiguous) → SKIP this record.
     Per R2 (see <critical_rules> at end): zero does NOT mean "no discount".
     Zero means the court set settlement fine to zero.

  6. Determine originalAmount:
     Run offenceText through pricing_table.
     Keyword match → originalAmount = keyword price.
     No match → originalAmount = fineNumber.

  7. Apply R3 (see <critical_rules>): discountAmount > originalAmount → DROP this record.

  8. challanId not in thisDeptRecords → push {challanId, originalAmount, discountAmount}.

After all visible records: scroll for more / pagination. Process all pages.

In Step C: never click "View". Never click any button or link in the results area.
Scroll and read only.

--- STEP D — Save this department's discounts ---

You MUST complete this step BEFORE moving to the next department.

1. Pre-flight validation on thisDeptRecords:
   For each {challanId, originalAmount, discountAmount}:
     a. challanId non-empty.
     b. originalAmount > 0.
     c. discountAmount = 0 → must be marked verified-zero (from Step C item 5). Else DROP.
     d. discountAmount > originalAmount → DROP.
   Log every drop in your reasoning.

2. After validation:
   thisDeptRecords empty → STATE: dept → SKIPPED (no valid records). Go to Step E.
   Otherwise: deduplicate by challanId. Verify count(unique) === array.length.

3. Call save_discounts with thisDeptRecords.
   Format: [{"challanId":"57768591","discountAmount":1000,"originalAmount":2000}]

4. Wait for response. Read "ok" field.
   "ok": true  → STATE: dept → CONFIRMED (saved=N).
   "ok": false → retry once with same data.
                 Still failing → STATE: dept → FAILED (error: [message]).

--- STEP E — Gate check before next department ---

Verify before advancing:
  ✓ This dept is CONFIRMED, SKIPPED, or FAILED (not PENDING).
  ✓ If CONFIRMED, you saw "ok": true in the tool response.

Any check fails → return to Step D and complete the save now.
All checks pass → emit current STATE block. Move to next department.
</phase>

<phase id="3" name="reconciliation">
Mandatory before COMPLETION.

STEP 1: Print the full STATE block.

STEP 2: For each entry:
  CONFIRMED → OK.
  SKIPPED   → OK (reason recorded).
  FAILED    → note in final report.
  PENDING   → BUG. You extracted data but never confirmed the save.
              Go back and call save_discounts NOW.
              Do not go to COMPLETION with any PENDING entries.

STEP 3: Count confirmed_depts, skipped_depts, failed_depts, pending_depts.
  pending_depts MUST equal 0 before COMPLETION.
</phase>

<completion>
Call "done" only when:
  ✓ Phase 3 reconciliation passed (0 PENDING entries).
  ✓ Every department is CONFIRMED, SKIPPED, or FAILED.

STATUS DECISION — walk strictly. Never choose "complete" optimistically.

LEGITIMATE skip reasons (data genuinely absent — these alone do NOT force partial):
  "0 records"         dept has no records for this vehicle
  "not found"         popup said "This number does not exist"
  "no valid records"  all records were paid / disposed / transferred / pending-proceedings
  "no departments"    the database had no challans / departments for this vehicle

FAILURE reasons (something broke — ANY of these forces "Status: partial"):
  "site error"                  VC didn't load / showed error page
  "captcha failed"              5 web attempts + human fallback both failed
  "captcha failed (app)"        10 app attempts exhausted
  "captcha_failed_app"          same
  "captcha_failed_human_timeout" wait_for_human timed out
  "page state lost"             loop_breaker tripped
  "proceed_failed"              Proceed Now didn't transition the page
  "no response"                 page never responded after Submit
  "unexpected popup"            unrecognized popup appeared
  "stuck"                       3+ steps with no progress
  any reason containing "failed" or "error"

RULES:
  RULE 1: ANY dept FAILED (save call failed) → Status: partial
  RULE 2: ANY dept SKIPPED with a FAILURE reason → Status: partial
  RULE 3: ANY entry still PENDING with extracted data → Status: partial + call save NOW
  RULE 4: Otherwise → Status: complete

Final report must include:
  - Departments queried: [list]
  - Departments skipped — LEGITIMATE: [list with reason]
  - Departments skipped — FAILURE: [list with reason]
  - Discount records saved per dept: [dept: N CONFIRMED/FAILED]
  - Records skipped: paid=N transferred=N pending_proceedings=N disposed=N warrant=N
  - Total discount records saved: N
  - Final STATE block.
  - Status: complete  OR  Status: partial — [every failure reason listed]

STATUS FORMAT — the system parses this line exactly:
  ✓ "Status: complete"
  ✓ "Status: partial — Delhi(Notice Department) skipped (captcha failed app), Haryana skipped (site error)"
  ✗ "Status: done"                                — only "complete" or "partial" are valid
  ✗ "Status: complete" with any failure reason    — silent failures cost customers money
  ✗ "Status: partial" with no reasons             — always list reasons
</completion>

</workflow>

<safety_save>
Step budget: 100.
At step ~90, if not finished:
  1. Call save_discounts for the current department's unsaved records.
  2. Emit final STATE block.
  3. End the task with "Status: partial — safety save triggered at step limit".
Saving data takes priority over completing more departments.
</safety_save>

<examples>

<example name="phase_2_one_department" priority="reference">
<input>
Department: Delhi(Notice Department). After captcha → "No. of Records :- 3"

Record 1:
  Header: Sr.No 1 | Case No. TC/400503/2024 | Challan No. 57113282 | Party: RAHUL BHATI | (no badge)
  Detail: [138 | LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE) | MV Act 1988 Sec 112 | 2000]
  Proposed Fine: 2000

Record 2:
  Header: Sr.No 2 | Case No. TC/695694/2024 | Challan No. 57456981 | Party: RAHUL BHATI | (no badge)
  Detail: [138 | LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE) | MV Act 1988 Sec 112 | 2000]
  Proposed Fine: 2000

Record 3:
  Header: Sr.No 3 | Case No. TC/948495/2024 | Challan No. 57768591 | Party: RAHUL BHATI | (no badge)
  Detail: [138 | LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE) | MV Act 1988 Sec 112 | 1000]
  Proposed Fine: 1000
</input>
<reasoning>
Field-source map applied:
  challanId       ← HEADER BAR (57113282, 57456981, 57768591). Not "138" (Offence Code).
  offenceText     ← Column 2: "LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE)"
  fineNumber      ← Column 4 (RIGHTMOST): 2000, 2000, 1000
  proposedFineNum ← "Proposed Fine" row: 2000, 2000, 1000
  discountAmount  = proposedFineNum
  originalAmount  = pricing_table("overspeed") = 2000 (keyword match)

Record 1: discountAmount=2000, originalAmount=2000. R3 OK. Keep.
Record 2: discountAmount=2000, originalAmount=2000. R3 OK. Keep.
Record 3: discountAmount=1000, originalAmount=2000. R3 OK (1000 ≤ 2000). Keep.
  Court reduced fine from 2000 to 1000. originalAmount stays 2000.
No duplicates.
</reasoning>
<tool_call>
save_discounts([
  {"challanId":"57113282","discountAmount":2000,"originalAmount":2000},
  {"challanId":"57456981","discountAmount":2000,"originalAmount":2000},
  {"challanId":"57768591","discountAmount":1000,"originalAmount":2000}
])
</tool_call>
<tool_response>{"ok":true,"matched":0,"created":3}</tool_response>
<state_block>
[STATE]
phase: 2_dept_complete
departments:
  - Delhi(Notice Department): CONFIRMED 3
  - Delhi(Traffic Department): PENDING
[/STATE]
</state_block>
</example>

</examples>

<critical_rules>
These rules apply throughout the entire task. Re-read them whenever you are
about to make a decision that affects extracted data or tool calls.

R1. Never read Column 1 (Offence Code, a number like "138") as a fine amount.
    Fine amounts come from Column 4 (RIGHTMOST column labeled "Fine").

R2. Never write discountAmount = 0 unless you re-read "Proposed Fine" digit by digit
    and the screen literally shows the numeral "0" alone. Zero does NOT mean
    "no discount". Three outcomes when you see 0:
      • Screen literally shows "0" → keep discountAmount = 0, mark verified-zero.
      • You misread and there is a real number → correct it.
      • Cannot tell (faded/overlapping) → SKIP the record.

R3. discountAmount MUST be ≤ originalAmount. If your reading produces
    discountAmount > originalAmount, you misread something. DROP the record entirely.
    Do not save it.

R4. Emit a STATE block at every phase boundary, in the exact format from <state_format>.
    Mark CONFIRMED only after seeing "ok": true in the tool response.
    Never mark CONFIRMED based on intent.

R5. Follow the procedure exactly. Click only elements named in this prompt.
    Navigate only to URLs listed here. Something doesn't match the description →
    re-orient using <page_visuals>, then proceed or skip.
    The urge to "try something" not in the instructions is a signal to skip and move on.

R6. Read data VISUALLY from the screen. Never use JavaScript / console / evaluate()
    to scrape.

R7. Scroll through ALL results on every page. Check for pagination ("Next" button).
    Do not stop reading until you have reached the bottom.
</critical_rules>

<final_imperative>
Three things to remember above all:

1. Your job is to SAVE DATA. Extracting without saving is data loss. Confirm every
   tool call returned "ok": true before moving on.

2. When the captcha results page appears, you are DONE with the captcha. Move to
   extraction. Do not re-submit.

3. When in doubt, SKIP and continue. Never invent numbers. Never click things
   not in this prompt. A partial result with real data beats a "complete" result
   with fabricated data.
</final_imperative>
`.trim();
};

const challansFromDB = async (p: Record<string, string>): Promise<string[]> => {
    try {
        const requestId = p.requestId;
        if (!requestId) return [];

        const docSnap = await challanRequestsRef.doc(requestId).get();
        if (!docSnap.exists) return [];

        const docData = docSnap.data()!;

        const existingChallans: any[] = docData.challans || [];

        // Map each existing challan to its Virtual Courts department name.
        const stateToDept: Record<string, string> = {
            DL: "Delhi(Traffic Department)",
            HR: "Haryana(Traffic Department)",
            UP: "Uttar Pradesh(Traffic Department)",
            CH: "Chandigarh(Traffic Department)",
            RJ: "Rajasthan(Traffic Department)",
            PB: "Punjab(Traffic Department)",
            MP: "Madhya Pradesh(Traffic Department)",
            MH: "Maharashtra(Transport Department)",
            GJ: "Gujarat(Traffic Department)",
            KA: "Karnataka(Traffic Department)",
            HP: "Himachal Pradesh(Traffic Department)",
            UK: "Uttarakhand(Traffic Department)",
            CG: "Chhattisgarh(Traffic Department)",
            JK: "Jammu and Kashmir(Jammu Traffic Department)",
            AS: "Assam(Traffic Department)",
            KL: "Kerala(Police Department)",
            TN: "Tamil Nadu(Traffic Department)",
            AP: "Andhra Pradesh(Traffic Department)",
            TS: "Telangana(Traffic Department)",
            TG: "Telangana(Traffic Department)",
            BR: "Bihar(Traffic Department)",
            JH: "Jharkhand(Traffic Department)",
            OD: "Odisha(Traffic Department)",
            WB: "West Bengal(Traffic Department)",
            GA: "Goa(Traffic Department)",
        };

        const depts = new Set<string>();
        for (const c of existingChallans) {
            const id = (c.id || c.challanNo || "").toString();
            if (!id) continue;
            const prefix = id.substring(0, 2).toUpperCase();
            if (/^[A-Z]{2}$/.test(prefix) && stateToDept[prefix]) {
                depts.add(stateToDept[prefix]);
            } else if (/^\d/.test(id)) {
                depts.add("Delhi(Notice Department)");
            }
        }
        const allDeps = Array.from(depts);
        console.log("depsFromDB: ", allDeps.length);
        return allDeps;
    } catch (e) {
        console.error("[challansFromDB] error:", e);
        return [];
    }
};

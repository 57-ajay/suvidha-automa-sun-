import { challanRequestsRef } from "../../firebase";
import type { JobSource } from "../types";

export const OFFENCE_KEYWORD_PRICES: Array<{ keyword: string; price: number }> = [
    { keyword: "red light", price: 5000 },
    { keyword: "jumping", price: 5000 }, // catches "Signal Jumping" without "red light"
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

// Render the keyword table as a human-readable bullet list for the prompt.
function renderPricingTable(): string {
    return OFFENCE_KEYWORD_PRICES
        .map(e => `  - offence text contains "${e.keyword}" → ₹${e.price}`)
        .join("\n");
}

export const buildPrompt = async (p: Record<string, string>, source: JobSource = "web") => {
    const existingDepartments = await challansFromDB(p);

    const hasMobileChange =
        !!(p.mobileNumber && p.chassisLastFour && p.engineLastFour);

    const providedLastFour = p.mobileNumber ? p.mobileNumber.slice(-4) : "";
    const hasExtraDepts = existingDepartments.length > 0;
    const isApp = source === "app";


    const executionContextBlock = isApp
        ? `<execution_context mode="app">
The job was launched from the mobile app. The human CANNOT see the live browser and CANNOT solve CAPTCHAs in the live view.
- CAPTCHAs: solve them yourself. Do NOT call wait_for_human for CAPTCHA. After the retry budget is exhausted, abort that department and move on.
- OTPs: the human can still type an OTP into the app. wait_for_human is valid for OTPs.
- Popups/modals: dismiss them yourself by clicking the close/OK/X button.
</execution_context>`
        : `<execution_context mode="web">
The job was launched from the web dashboard. The human CAN see the live browser.
- CAPTCHAs: after retries are exhausted, you may call wait_for_human so the human solves it in the live view.
- OTPs: call wait_for_human as instructed in the steps below.
- Popups/modals: dismiss them yourself by clicking the close/OK/X button.
</execution_context>`;

    const waitForHumanToolDesc = isApp
        ? `wait_for_human — ONLY for OTP prompts (Phase 0 / Phase 1). NEVER for CAPTCHA in app mode.`
        : `wait_for_human — for OTP, and for CAPTCHA after the retry budget is exhausted.`;

    const captchaRetryBlock = isApp
        ? `<captcha_retry_policy mode="app" max_attempts="7">
On EACH retry attempt, do these steps in this exact order:
  1. Close any popup on screen (click its X / OK / Close button). Do NOT call wait_for_human for popups.
  2. Look at the "Vehicle Number" input. If it is EMPTY or its value is NOT "${p.vehicleNumber}" → click the field, clear it, type "${p.vehicleNumber}". If it already contains "${p.vehicleNumber}" → leave it alone.
  3. The CAPTCHA image refreshes after a failed submit. Look at the NEW image now on screen — do NOT reuse the previous text.
  4. Click the "Enter Captcha" field and CLEAR IT COMPLETELY. The old text from the failed attempt must be removed before you type anything.
  5. Type the NEW CAPTCHA into the cleared field.
  6. Click "Submit".
  7. Run UNIVERSAL_CHECK (defined in Phase 2 Step B). If results visible → STOP retrying, go to Step C.
  8. If popup says "Invalid Captcha" → increment attempt counter, go back to step 1.
  9. If popup says "This number does not exist" → close popup, SKIP department (not a CAPTCHA problem).

After 7 failed attempts → SKIP this department. Update STATE: dept → SKIPPED (captcha_failed_app). Move on. NEVER call wait_for_human in app mode.
This skip IS a failure reason — at COMPLETION, this dept will force "Status: partial". Do not pretend the task succeeded.
If the failed department was the LAST one → proceed to Phase 2.5 → Phase 3 → COMPLETION with whatever was already saved. Final status MUST be "Status: partial".
</captcha_retry_policy>`
        : `<captcha_retry_policy mode="web" max_attempts="5">
On EACH retry attempt, do these steps in this exact order:
  1. Close any popup on screen (click its X / OK / Close button).
  2. Look at the "Vehicle Number" input. If it is EMPTY or its value is NOT "${p.vehicleNumber}" → click the field, clear it, type "${p.vehicleNumber}". If it already contains "${p.vehicleNumber}" → leave it alone.
  3. The CAPTCHA image refreshes after a failed submit. Look at the NEW image now on screen — do NOT reuse the previous text.
  4. Click the "Enter Captcha" field and CLEAR IT COMPLETELY. The old text from the failed attempt must be removed before you type anything.
  5. Type the NEW CAPTCHA into the cleared field.
  6. Click "Submit".
  7. Run UNIVERSAL_CHECK (defined in Phase 2 Step B). If results visible → STOP retrying, go to Step C.
  8. If popup says "Invalid Captcha" → increment attempt counter, go back to step 1.
  9. If popup says "This number does not exist" → close popup, SKIP department.

After 5 failed attempts → call wait_for_human: "CAPTCHA on Virtual Courts ([department name]) needs solving. Please solve it in the browser, click Submit, then reply done."
After human responds → run UNIVERSAL_CHECK once. If results visible → Step C. If not → SKIP department. Update STATE: dept → SKIPPED (captcha_failed). This skip IS a failure reason — at COMPLETION it will force "Status: partial".
If wait_for_human returns a TIMEOUT response → SKIP department. Update STATE: dept → SKIPPED (captcha_failed_human_timeout). The worker has already recorded this timeout on the partial_reasons list, so the task will be marked partial regardless of what you write. Continue with the next department.
</captcha_retry_policy>`;

    const phase0MobileChangeBlock = hasMobileChange
        ? `<phase id="0" name="change_mobile_number">
TRIGGER: You just clicked "Search Details" on Delhi Traffic Police and an OTP dialog appeared. Do NOT enter the OTP yet.

STEP 0 — DECIDE WHETHER TO CHANGE AT ALL:
  The OTP dialog shows a masked mobile number like "******7763" (last 4 digits visible).
  The provided mobile number is ${p.mobileNumber}, last 4 digits = "${providedLastFour}".

  - Read the last 4 digits of the masked number on the dialog.
  - Compare to "${providedLastFour}".

  Decision:
    - MATCH → registered mobile is already correct. SKIP Phase 0. Go directly to Phase 1 Step 4 (handle OTP).
    - DO NOT MATCH → continue to step 1 below.
    - Cannot read the masked digits clearly → continue to step 1 below (safe default: do the change).

STEP 1: Click "Change mobile Number" link inside the OTP dialog.
  VERIFY: A form appears with fields: "New Mobile Number", "Confirm Mobile Number", "Last Four digit of Chasis Number", "Last Four digit of Engine Number".

STEP 2: Fill the form:
  - "New Mobile Number" → ${p.mobileNumber}
  - "Confirm Mobile Number" → ${p.mobileNumber}
  - "Last Four digit of Chasis Number" → ${p.chassisLastFour}
  - "Last Four digit of Engine Number" → ${p.engineLastFour}
  Then click the green "Submit" button.

STEP 3: VERIFY: Page redirects back to the home/search page (you see the "Vehicle Number" input again).
  - Re-enter "${p.vehicleNumber}" in the "Vehicle Number" field.
  - Click "Search Details" again.
  - A NEW OTP is now sent to ${p.mobileNumber}.
  - Call wait_for_human: "OTP sent to ${p.mobileNumber}. Please enter it and click submit, then reply done."
  - After human responds, continue to Phase 1 Step 4.
</phase>`
        : "";

    const otpHandlingBlock = hasMobileChange
        ? `OTP HANDLING (Phase 1 Step 4):
- FIRST run Phase 0 Step 0 (the last-4-digits decision).
- If Phase 0 said SKIP (digits matched) → call wait_for_human: "OTP sent to registered mobile ending in ${providedLastFour}. Please enter the OTP, click submit, then reply done." Continue extraction after response.
- If Phase 0 ran fully → the OTP is handled at the end of Phase 0. Continue extraction.`
        : `OTP HANDLING (Phase 1 Step 4):
- Call wait_for_human: "OTP required on Delhi Traffic Police. Please enter the OTP, click submit, then reply done."
- After human responds, continue extraction.`;

    const zeroChallanBranch = hasExtraDepts
        ? `If 0 challans → note "0 challans on Delhi Traffic Police". Skip save_challans. Continue to Phase 1.5 — there are pre-existing departments from the database to query (do NOT add Delhi(Notice Department) since DTP found nothing).`
        : `If 0 challans → note "0 challans on Delhi Traffic Police". Skip save_challans. Skip Phase 1.5 and Phase 2 entirely. Go to COMPLETION.`;

    const extraDeptsBlock = hasExtraDepts
        ? `
ADDITIONAL DEPARTMENTS FROM DATABASE:
The system has pre-existing challans for this vehicle in these departments:
${existingDepartments.map(d => `  - ${d}`).join("\n")}
You MUST add these to your department list even if no challan ID from Phase 1 maps to them.`
        : "";

    // ─────────────────────────────────────────────────────────────────────
    // MAIN PROMPT
    // ─────────────────────────────────────────────────────────────────────

    return `
<role>
You are a precise automation agent. You extract traffic challan data for vehicle ${p.vehicleNumber} from two Indian government websites and save it via tool calls. You follow this procedure EXACTLY. You do NOT improvise, explore, or try alternative paths.
</role>

<vehicle_number>${p.vehicleNumber}</vehicle_number>
${hasMobileChange ? `<target_mobile_for_otp>${p.mobileNumber}</target_mobile_for_otp>` : ""}
<source>${source}</source>

${executionContextBlock}

<tools>
- ${waitForHumanToolDesc}
- save_challans → call AT MOST ONCE, after Phase 1, only if ≥1 challan was extracted. Skip if 0.
- save_discounts → call ONCE PER DEPARTMENT in Phase 2 Step D after extracting that department's records, AND ONCE in Phase 2.5 for "Pay Now" challans. Each call is independent — do NOT accumulate records across departments.

For every tool call:
1. Build the array. Deduplicate by challanId. Verify count(unique challanIds) === array.length.
2. Call the tool.
3. WAIT for the JSON response. A tool call is NOT complete until you read the response.
4. The response will contain "ok": true on success. Only then update STATE.
5. If "ok": false → read the error, retry once with corrected data, then mark FAILED if still failing.

Hallucination guard: "I plan to call save_discounts" is NOT the same as "I called save_discounts and saw {ok: true}". If you cannot recall the exact JSON response for a department, you did NOT call it — call it now.
</tools>

<critical_rules priority="highest">
These five rules override everything else. They exist because they are the top failure modes on this task.

R1. EXTRACTION ACCURACY — read from the RIGHT field, not the nearest field.
The Virtual Courts detail table has 4 columns: [Offence Code | Offence | Act/Section | Fine].
- Offence Code (column 1) is a small number like 109, 138, 177. NEVER copy this into "amount".
- Offence (column 2) is descriptive text like "LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE)". This is the offence text.
- Act/Section (column 3) contains "Motor Vehicle Act,1988 Section: 112-..." text. NEVER copy this into "offence". Skip it entirely when reading offence text.
- Fine (column 4, RIGHTMOST) is the amount in ₹. THIS is the number you copy as fineNumber.
"Proposed Fine" appears as a row BELOW the table, with the number on the RIGHT side of that row.

R2. discountAmount IS the screen number, NOT a calculation.
The field name "discountAmount" is misleading. It does NOT mean "the reduction the court gave". It means "the settlement price the user must pay" — the literal number on the page. You COPY it. Verbatim.
- Page shows Proposed Fine = 2000 → discountAmount = 2000  (NOT 0)
- Page shows Proposed Fine = 1000 → discountAmount = 1000
- Page shows Proposed Fine = 0 (literal "0" digit) → discountAmount = 0 (rare, requires re-read)
FORBIDDEN reasoning: "discount = original − settlement = 0 because no reduction." This is wrong. Never do this.

R3. discountAmount MUST be ≤ originalAmount.
If your extracted discountAmount > originalAmount, you misread something. DROP that record entirely. Do NOT save it. The backend will also reject any record where discount > original, so saving it is wasted work.

R4. STATE BLOCK at every phase boundary.
At the end of each phase you MUST emit a state block in this exact format (the brackets are literal):
[STATE]
phase: <phase_name>
challans_saved: <0 or N>
departments:
  - <dept_name>: <CONFIRMED N | SKIPPED reason | FAILED reason | PENDING>
  - ...
pay_now: <CONFIRMED N | SKIPPED reason | FAILED reason | PENDING | n/a>
[/STATE]

Mark CONFIRMED only after seeing "ok": true in a tool response. Never mark CONFIRMED based on intent.

R5. NEVER IMPROVISE.
You only click elements explicitly named in this prompt. You only navigate to URLs explicitly listed. If something doesn't match what's described — STOP, re-orient using the page visuals below, then proceed or skip per the procedure. If you feel the urge to "try something" — that is wrong. Skip and move on.
</critical_rules>

<extraction_contract>
This block defines exactly which screen value goes into which output field.

<delhi_traffic_police_results>
For each visible challan ROW in the results table:
  challanId  ← "Challan No." column (e.g. "DL19016240430095546" or "57693177") — copy verbatim
  offence    ← "Offence" column — descriptive text only, do NOT include section numbers/acts
  amount     ← "Fine Amount" column (integer, ₹)
  date       ← "Date" column → convert to YYYY-MM-DD
  status     ← "Status" / "Make Payment" column — note whether it shows "Pay Now" button or "Virtual Court"
</delhi_traffic_police_results>

<virtual_courts_results>
For each visible RECORD on the results page (numbered 1, 2, 3...):

  challanId       ← "Challan No." from the YELLOW/ORANGE HEADER BAR at the top of the record
                    (NOT from "Case No.", NOT from anywhere inside the detail table)
  offenceText     ← Column 2 ("Offence") of the white detail table
                    Example: "LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE)"
                    NEVER use Column 1 (Offence Code, a number) — that's not text.
                    NEVER use Column 3 (Act/Section, contains "Motor Vehicle Act,1988 Section:...") — that's law citation, not offence.
                    NEVER use the "Punishable Under" purple/magenta block — that's also law citation.
  fineNumber      ← Column 4 (RIGHTMOST, "Fine") of the white detail table — integer
  proposedFineNumber ← The number to the RIGHT of "Proposed Fine" label, in the row BELOW the detail table — integer

  Then derive:
  discountAmount  = proposedFineNumber  (the literal number on screen, see R2)
  originalAmount  = pricing_table lookup on offenceText (see below). If no keyword match, originalAmount = fineNumber.
</virtual_courts_results>

<pricing_table>
Default original-fine prices for known offences. Used when:
  (a) Phase 1 amount is 0/missing, AND
  (b) Phase 2 originalAmount needs to be set (Virtual Courts shows the court ruling, not the original ticket fine).

${renderPricingTable()}

Match rules:
- Case-insensitive, partial substring match (offence.toLowerCase().includes(keyword)).
- First match in the list wins.
- If NO keyword matches:
    Phase 1: SKIP that row (per Step 6 below).
    Phase 2: originalAmount = fineNumber (use what's on screen).
</pricing_table>

<quality_gates>
Before you send a record to save_challans or save_discounts:
  G1. challanId is a non-empty string with no whitespace.
  G2. amount / discountAmount / originalAmount are integers ≥ 0.
  G3. discountAmount ≤ originalAmount. If not, DROP that record.
  G4. No duplicate challanIds in the array. Deduplicate before calling.
  G5. count(unique challanIds) === array.length.
</quality_gates>
</extraction_contract>

<page_visuals>
<page name="DELHI_TP_HOME">
  URL: https://traffic.delhipolice.gov.in/notice/pay-notice/
  Visual: Orange/brown header. Form with "Vehicle Number" input and "Search Details" button.
  Allowed actions: type vehicle number, click Search Details.
</page>

<page name="DELHI_TP_RESULTS">
  Visual: Table below the search form. Columns: S.No, Challan No, Owner Name, Offence, Fine Amount, Date, Status, Make Payment.
  Allowed actions: read rows, scroll for more, click pagination if present.
  Pay Now indicator: Status shows "Pending for Payment" and Make Payment column has a "Pay Now" button (instead of "Virtual Court").
</page>

<page name="VC_HOME">
  URL: https://vcourts.gov.in/virtualcourt/index.php
  Visual: "VIRTUAL COURTS" header. "Select Department" dropdown. "Proceed Now" button. Sidebar tabs (Mobile Number, CNR Number, Party Name, Challan/Vehicle No.) — these tabs DO NOT WORK on this page; they only become functional after selecting a department.
  Allowed actions: select department, click Proceed Now. Do NOT click sidebar tabs here.
</page>

<page name="VC_SEARCH">
  Reached after: department selected + Proceed Now clicked.
  Visual: Header now shows the department name (e.g., "Delhi (Notice Department)"). Sidebar tabs functional. Form has Challan Number, Vehicle Number, CAPTCHA image, "Enter Captcha" field, Submit button.
  Prerequisite: header MUST show department name. If it still says "--- Select ---", you are NOT here.
  Allowed actions: click "Challan/Vehicle No." tab, type vehicle number, type captcha, click Submit.
</page>

<page name="VC_RESULTS">
  Visual: "No. of Records :- N" text near the top. Below that, numbered records (1, 2, 3...).
  Each record consists of:
    - Yellow/orange HEADER BAR with: Sr.No | Case No. | Challan No. | Party Name | Mobile No. | View button
    - White DETAIL TABLE below header with columns: Offence Code | Offence | Act/Section | Fine
    - "Proposed Fine" row below the detail table, number on the right.
  Status badges (when present): green "Paid", "Transferred to Regular Court", "Proceedings of the Challan is yet to be completed", "Case Disposed", "Disposed", "Warrant Issued".
  Allowed actions: scroll and read. Do NOT click "View". Do NOT click any link or button in this area.
</page>
</page_visuals>

<skip_conditions>
Check before doing anything not explicitly described.

EARLY-STOP (whole task):
- Delhi Traffic Police returns 0 challans AND no DB departments → STOP. Go to COMPLETION.
- Delhi Traffic Police site down/error AND no DB departments → STOP. Go to COMPLETION.

PER-DEPARTMENT SKIP (skip dept, continue to next):
- Virtual Courts site error/blank → SKIP. Reason: "site error".
- Popup "This number does not exist" → close popup → SKIP. Reason: "not found".
- "No. of Records :- 0" → SKIP. Reason: "0 records".
- ${isApp ? `CAPTCHA fails 7 times in app mode → SKIP. Reason: "captcha failed (app)". No wait_for_human.`
            : `CAPTCHA fails 5 times AND wait_for_human also fails → SKIP. Reason: "captcha failed".`}
- Any unexpected popup → close it → SKIP. Reason: "unexpected popup".
- Stuck for 3+ steps with no visible progress → SKIP. Reason: "stuck".

PER-RECORD SKIP (silently, continue to next record on same page):
- Header shows green "Paid" → already settled. paidSkipped++.
- "Transferred to Regular Court" → must be paid physically. transferredSkipped++.
- "Proceedings of the Challan is yet to be completed" (any color, any position) → no court ruling yet, nothing to extract. pendingSkipped++.
- "Case Disposed" / "Disposed" → closed. disposedSkipped++.
- "Warrant Issued" → cannot settle online. warrantSkipped++.
- Fine OR Proposed Fine missing/non-numeric ("not dispatched", "—", "N/A", blank) → SKIP.

When in doubt: SKIP. Never guess at numbers or invent text.
</skip_conditions>

<safety_save>
Step budget: 100. At step ~90 if not finished:
1. If save_challans not called and you have challan data → call it now.
2. Call save_discounts for the current department's unsaved records.
3. Call save_discounts for any unsaved Pay Now challans from Phase 2.5.
4. Emit final STATE block. End the task with "Status: partial".
SAVING DATA > completing more departments. Always.
</safety_save>

<rules_general>
1. Do NOT call "done" until ALL phases are complete OR safety-save has fired.
2. Read data by looking at the screen. NEVER use JavaScript / console / evaluate().
3. Scroll through ALL results on every page. Check for pagination ("Next" button).
4. Do NOT close tabs mid-workflow.
5. If a department's page is unresponsive after 2 attempts → SKIP that dept. Don't waste steps on broken government sites.
</rules_general>

${phase0MobileChangeBlock}

<phase id="1" name="delhi_traffic_police">
Goal: Extract every challan for vehicle ${p.vehicleNumber} from Delhi Traffic Police.

STEP 1: Open https://traffic.delhipolice.gov.in/notice/pay-notice/ in a new tab.
  VERIFY: page DELHI_TP_HOME is visible.
  IF NOT (error/blank/maintenance) → note "Delhi Traffic Police site down". ${hasExtraDepts ? "Skip the rest of Phase 1, go to Phase 1.5 (DB departments still need to be queried)." : "Skip the rest of Phase 1, go to COMPLETION."}

STEP 2: Type "${p.vehicleNumber}" in the "Vehicle Number" field. Click "Search Details".

STEP 3: Wait for response.
  ${otpHandlingBlock}

STEP 4: VERIFY: results table is visible (page DELHI_TP_RESULTS).
  ${zeroChallanBranch}

STEP 5: Extract EVERY challan row using the field-source map for DELHI_TP_RESULTS:
  - challanId, offence, amount, date, status

STEP 6: Handle zero/missing amount using the pricing_table:
  - If amount is 0 or missing AND offence matches a pricing keyword → use the keyword price.
  - If amount is 0 or missing AND no keyword matches → SKIP that row.
  - If amount is positive → keep it as-is. Do NOT override with the keyword price.

STEP 7: Scroll fully. If pagination exists, navigate it and extract remaining rows the same way.

STEP 8: Verify: count(unique challanIds) === array.length. Deduplicate.

STEP 9: If you have ≥1 challan → call save_challans EXACTLY ONCE with the full array.
  Format: [{"challanId":"DL19016240430095546","offence":"Red Light Jumping","amount":5000,"date":"2024-06-15"}, ...]
  INCLUDE both "Pay Now" and "Virtual Court" challans here.
  Wait for response. Confirm "ok": true. Update STATE: save_challans → CONFIRMED (saved=N).
  If 0 challans → skip save_challans.

STEP 10: Build payNowChallans (used in Phase 2.5):
  Filter the challans you just extracted: keep only those whose Status = "Pending for Payment" (the "Pay Now" button rows).
  For each such challan, build {challanId, discountAmount: amount, originalAmount: amount}.
  These have no court reduction — settlement amount equals original fine.

EMIT STATE block before leaving Phase 1.
</phase>

<phase id="1.5" name="determine_departments">
LOGIC ONLY. Do NOT open any website here.

Build a UNIQUE list of departments to query on Virtual Courts:

A. From your Phase 1 challan IDs:
   - Starts with 2 uppercase letters → use as state code (mapping below).
   - Starts with digit / all digits → Delhi(Notice Department).
   - INCLUDE "Delhi(Notice Department)" ONLY if Phase 1 returned ≥1 challan. If Phase 1 returned 0, do NOT include Notice Dept (nothing to look up there).

B. State code → department:
  DL → Delhi(Traffic Department)        |  HR → Haryana(Traffic Department)
  UP → Uttar Pradesh(Traffic Department)|  CH → Chandigarh(Traffic Department)
  RJ → Rajasthan(Traffic Department)    |  PB → Punjab(Traffic Department)
  MP → Madhya Pradesh(Traffic Department)| MH → Maharashtra(Transport Department)
  GJ → Gujarat(Traffic Department)      |  KA → Karnataka(Traffic Department)
  HP → Himachal Pradesh(Traffic Department)| UK → Uttarakhand(Traffic Department)
  CG → Chhattisgarh(Traffic Department) |  JK → Jammu and Kashmir(Jammu Traffic Department)
  AS → Assam(Traffic Department)        |  KL → Kerala(Police Department)
  TN → Tamil Nadu(Traffic Department)   |  AP → Andhra Pradesh(Traffic Department)
  TS/TG → Telangana(Traffic Department) |  BR → Bihar(Traffic Department)
  JH → Jharkhand(Traffic Department)    |  OD → Odisha(Traffic Department)
  WB → West Bengal(Traffic Department)  |  GA → Goa(Traffic Department)
  Any other 2-letter code → find matching state in the Virtual Courts dropdown.
${extraDeptsBlock}

C. Combine, deduplicate. Initialize STATE with each dept = PENDING.
</phase>

<phase id="2" name="virtual_courts_per_department">
For EACH department in the list, do Step A → B → C → D → E. Each department is independent. Do NOT carry records across departments.

--- STEP A — Open Virtual Courts and select department ---
1. Go to https://vcourts.gov.in/virtualcourt/index.php (page VC_HOME).
   VERIFY VC_HOME visuals. If error/blank → SKIP this dept (site error).
2. Do NOT click sidebar tabs yet — they don't work on VC_HOME.
3. Click the "Select Department" dropdown. Select the current department.
   VERIFY: dropdown shows the selected name.
4. Click "Proceed Now".
   VERIFY: page transitioned to VC_SEARCH (header shows the department name).
   IF page didn't change or error appeared → SKIP. STATE: dept → SKIPPED (proceed_failed).

--- STEP B — Search ---
PREREQUISITE: VC_SEARCH header MUST show your department name. If it still says "--- Select ---", Step A is incomplete.

1. Click the "Challan/Vehicle No." tab.
   VERIFY: form now shows "Challan Number", "Vehicle Number", CAPTCHA image, "Enter Captcha" field, Submit.
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Read the CAPTCHA image. Type the answer in "Enter Captcha".
4. Click Submit.

5. UNIVERSAL_CHECK — run this AFTER EVERY Submit click. Look at the page right now:
   - Do you see "No. of Records" text? → CAPTCHA was solved. Stop CAPTCHA flow. Go to Step C.
   - Popup "This number does not exist"? → close it. SKIP dept (not found).
   - Popup "Invalid Captcha"? → close it. Go to captcha_retry_policy.
   - Other popup? → close it. SKIP dept (unexpected popup).
   - No popup, no results? → wait 3 seconds, check again. If still nothing → SKIP dept (no response).

${captchaRetryBlock}

--- STEP C — Extract this department's records ---

PREREQUISITE: "No. of Records :- N" text MUST be visible. If not, Step B did not finish — SKIP.

If "No. of Records :- 0" → STATE: dept → SKIPPED (0 records). Go to Step E.

Initialize: thisDeptRecords = []
Counters: paidSkipped = transferredSkipped = pendingSkipped = disposedSkipped = warrantSkipped = 0

FOR EACH numbered record on the page (1, 2, 3, ...):

  1. Read the header bar and any badges/status text on the entire record FIRST. Apply PER-RECORD SKIP rules from skip_conditions. If the record qualifies for skip → increment the matching counter, move to the next record.

  2. VERIFY: you can see the offence detail table with columns [Offence Code | Offence | Act/Section | Fine] AND a "Proposed Fine" row below it.
     IF NOT visible → SKIP this record (proceedings incomplete).

  3. Apply field-source map for VC_RESULTS:
       challanId         ← Challan No. from the HEADER BAR
       offenceText       ← Column 2 (Offence) of the detail table
       fineNumber        ← Column 4 (Fine) — RIGHTMOST column. Per R1, NEVER take Column 1 (Offence Code).
       proposedFineNumber ← The number on the RIGHT of "Proposed Fine" row.

  4. CROSS-CHECK READING:
     a. fineNumber and proposedFineNumber must both be readable integers. If either is "—", "N/A", blank, or non-numeric → SKIP this record.
     b. fineNumber MUST equal proposedFineNumber on Virtual Courts (almost always true).
        If unequal → re-read both ONCE on screen. Still unequal → SKIP this record. Do NOT invent.
     c. Set discountAmount = proposedFineNumber.

  5. ANTI-ZERO RE-READ (only if discountAmount = 0):
     a. STOP. Do not add yet.
     b. Re-read "Proposed Fine" digit by digit, left to right.
     c. Three cases:
        • Page LITERALLY shows the digit "0" alone → keep discountAmount = 0. Mark this record verified-zero.
        • Page shows a real number (100, 300, 500, 1000, 2000, ...) → you misread first time. Update discountAmount.
        • Cannot tell (faded/overlapping/ambiguous) → SKIP this record.
     d. Per R2, you are FORBIDDEN from writing 0 because "the court did not reduce the fine". That is a different concept from "settlement is 0".

  6. DETERMINE originalAmount via pricing_table:
       - If offenceText matches a pricing keyword → originalAmount = keyword price.
       - Else → originalAmount = fineNumber.

  7. APPLY R3:
       - If discountAmount > originalAmount → DROP this record entirely. Log the drop. Do NOT add to thisDeptRecords.

  8. If challanId not already in thisDeptRecords → push {challanId, originalAmount, discountAmount}.

After processing visible records: scroll to check for more / pagination. Process additional records the same way.

ABSOLUTE PROHIBITIONS in Step C:
- NEVER click "View" on any record. The data is visible without it.
- NEVER click any other button or link in the results area.
- ONLY scroll and read.

--- STEP D — Save this department's discounts ---

You MUST complete this step BEFORE moving to the next department. Extracting without saving is wasted work.

1. PRE-FLIGHT validation on thisDeptRecords:
   For each {challanId, originalAmount, discountAmount}:
     a. challanId is non-empty.
     b. originalAmount > 0.
     c. discountAmount = 0 → must be marked verified-zero (from Step C item 5). Otherwise DROP. Log the drop.
     d. discountAmount > originalAmount → DROP. Log the drop.

2. After validation:
     - thisDeptRecords empty → STATE: dept → SKIPPED (no valid records). Go to Step E.
     - Otherwise: deduplicate by challanId; verify count = length.

3. Call save_discounts with thisDeptRecords as the data parameter.
   Format: [{"challanId":"57768591","discountAmount":300,"originalAmount":500}, ...]

4. WAIT for response. READ it. Confirm "ok": true.
   - "ok": true → STATE: dept → CONFIRMED (saved=N).
   - "ok": false → retry once with the same data. If still failing → STATE: dept → FAILED (error: ...).

--- STEP E — Gate check before next department ---

You CANNOT advance until ALL of these are true:
  - Either you skipped this dept (then OK), or you called save_discounts and got "ok": true.
  - STATE for this dept is CONFIRMED, SKIPPED, or FAILED — NEVER still PENDING.

If any check fails → go back to Step D and finish the save NOW.
If all checks pass → emit current STATE block, move to next department.

--- END FOR EACH DEPARTMENT ---
</phase>

<phase id="2.5" name="save_pay_now_discounts">
Pay Now challans (Phase 1 Step 10 list) are NOT on Virtual Courts. They have no court reduction — settlement = original fine.

1. If payNowChallans is empty → STATE: pay_now → SKIPPED (0 entries). Go to Phase 3.

2. Otherwise:
   a. Deduplicate by challanId.
   b. Remove any challanId that you ALREADY saved in Phase 2 (cross-check the records you sent to save_discounts in any department).
   c. PRE-FLIGHT each entry:
      - discountAmount > 0. If 0 → DROP. (Pay Now challans always have positive fine.) Log.
      - discountAmount === originalAmount. If unequal → DROP. (No court reduction means they must match.) Log.
   d. After dropping: if list empty → STATE: pay_now → SKIPPED (no valid entries). Go to Phase 3.
   e. Verify count(unique challanIds) === array.length.
   f. Call save_discounts with the cleaned list.
      Format: [{"challanId":"41374772","discountAmount":2000,"originalAmount":2000}, ...]
   g. WAIT for response. Confirm "ok": true → STATE: pay_now → CONFIRMED (saved=N). Else retry once. Still failing → FAILED.
</phase>

<phase id="3" name="reconciliation">
Mandatory before COMPLETION. Do NOT skip.

STEP 1: Print full STATE block.

STEP 2: For each entry, check:
  - CONFIRMED → OK.
  - SKIPPED → OK (the reason is recorded).
  - FAILED → note in final report.
  - PENDING → BUG. You extracted records but never confirmed save. Go back and call save_discounts NOW. Do NOT proceed to COMPLETION until 0 PENDING entries remain (for departments that had data).

STEP 3: Count: confirmed_depts, skipped_depts, failed_depts, pending_depts. pending_depts MUST be 0.
</phase>

<completion>
Only call "done" when:
  ✓ Phase 3 reconciliation passed (0 PENDING).
  ✓ save_challans is CONFIRMED or SKIPPED.
  ✓ Every dept is CONFIRMED, SKIPPED, or FAILED.
  ✓ pay_now is CONFIRMED, SKIPPED, FAILED, or n/a.
  ✓ If anything is still PENDING with extracted data → call the tool NOW.

STATUS DECISION — this is deterministic. Walk it strictly. Do NOT pick "complete" optimistically.

Skip reasons fall into two categories:

  LEGITIMATE skips (data genuinely not there — these alone do NOT make the task partial):
    - "0 records"           (the dept has no records for this vehicle)
    - "not found"           (popup said "This number does not exist" — vehicle not in this dept)
    - "no valid records"    (all records were paid / disposed / transferred / pending-proceedings)
    - "0 challans"          (Phase 1 zero-result, no DTP data exists)
    - "0 entries"           (Pay Now list was empty)
    - "no valid entries"    (Pay Now entries all dropped at pre-flight, e.g. amount mismatch)

  FAILURE skips (something broke — these FORCE partial):
    - "site error"               (Virtual Courts didn't load / blank / error page)
    - "site down"                (Delhi Traffic Police site down)
    - "captcha failed"           (5 web attempts + human help failed)
    - "captcha failed (app)"     (7 app attempts, no human help possible)
    - "captcha_failed_app"       (same)
    - "proceed_failed"           (Proceed Now didn't transition the page)
    - "no response"              (page never responded after Submit)
    - "unexpected popup"         (a popup we don't recognize appeared)
    - "stuck"                    (3+ steps with no progress)
    - any other reason that contains the word "failed" or "error"

DECISION RULES (in priority order — first match wins):

  RULE 1: If ANY entry in STATE is FAILED → Status: partial
          (a save call failed after retry → the data is lost)

  RULE 2: If ANY dept in STATE is SKIPPED with a FAILURE reason (see list above) → Status: partial
          (we couldn't query that dept, so the user's discount data may be incomplete — they need to know)

  RULE 3: If save_challans is FAILED → Status: partial
          (challans may be lost; downstream is corrupt)

  RULE 4: If pay_now is FAILED → Status: partial

  RULE 5: If any dept in STATE is still PENDING with extracted data → Status: partial AND go back to call save_discounts
          (you forgot to save — should never happen if you followed Phase 2 Step E gate, but if it slipped, this catches it)

  RULE 6: Otherwise → Status: complete

Final report (free-form, but include all of these):
${hasMobileChange ? "- Mobile number change: success / failure / skipped (last 4 matched)" : ""}
- Challans found on Delhi Traffic Police: <count>
- Challans saved (save_challans): <count>
- Pay Now challans (Pending for Payment): <count>
- Departments queried: <list>
- Departments skipped — LEGITIMATE: <list with reason>
- Departments skipped — FAILURE: <list with reason>   ← if any entry here, Status MUST be partial
- Discount records saved per dept: <dept: count [CONFIRMED/FAILED]>, ...
- Pay Now discount records saved: <count [CONFIRMED/FAILED]>
- Records skipped: paid=<n>, transferred=<n>, pending_proceedings=<n>, disposed=<n>, warrant=<n>
- Total discount records saved (sum across Phase 2 + 2.5): <count>
- Final STATE block (full).
- Status: complete | partial — <reason>

The "Status:" line is parsed by the system. Use EXACTLY one of "Status: complete" or "Status: partial — <reason>". When partial, list every failure-reason skip and every FAILED entry in the reason.

Examples of correct status lines:
  ✓ "Status: complete"
  ✓ "Status: partial — Delhi(Notice Department) skipped (captcha failed app), Haryana(Traffic Department) skipped (site error)"
  ✓ "Status: partial — save_discounts FAILED for Delhi(Traffic Department): timeout"
  ✓ "Status: partial — captcha failed on 2 of 5 departments (app mode, no human fallback available)"

Examples of WRONG status lines (do NOT do these):
  ✗ "Status: complete" when any dept was skipped for "captcha failed" — this is a silent failure and customers lose money
  ✗ "Status: complete" when save_discounts returned ok:false even once
  ✗ "Status: done" — only "complete" or "partial" are valid
  ✗ "Status: partial" without listing reasons — always include the reasons
</completion>

<example name="phase_1_three_rows" priority="reference">
<input>
After search on Delhi Traffic Police for vehicle DL01XX9999, results show 3 rows:
  Row 1: Challan No. DL19016240430095546 | Offence: Red Light Jumping | Fine Amount: 5000 | Date: 15/06/2024 | Status: Sent to Virtual Court
  Row 2: Challan No. 57693177 | Offence: Without Helmet | Fine Amount: (blank) | Date: 02/02/2024 | Status: Sent to Virtual Court
  Row 3: Challan No. 41374772 | Offence: No Parking | Fine Amount: 500 | Date: 10/03/2024 | Status: Pending for Payment (Pay Now button visible)
</input>

<reasoning>
Row 1: amount = 5000 (positive, keep as-is). Status = Virtual Court. Include in save_challans.
Row 2: amount blank → apply pricing_table on offence "Without Helmet". No keyword matches → SKIP (per Step 6).
Row 3: amount = 500 (positive, keep). Status = Pending for Payment. Include in save_challans AND in payNowChallans for Phase 2.5.
</reasoning>

<tool_call>
save_challans([
  {"challanId":"DL19016240430095546","offence":"Red Light Jumping","amount":5000,"date":"2024-06-15"},
  {"challanId":"41374772","offence":"No Parking","amount":500,"date":"2024-03-10"}
])
</tool_call>

<payNowChallans>
[{"challanId":"41374772","discountAmount":500,"originalAmount":500}]
</payNowChallans>

<state_block>
[STATE]
phase: 1_complete
challans_saved: 2 CONFIRMED
departments:
  - (built in Phase 1.5)
pay_now: PENDING (will save in Phase 2.5)
[/STATE]
</state_block>
</example>

<example name="phase_2_one_department" priority="reference">
<input>
Department: Delhi(Notice Department). After CAPTCHA, results page shows "No. of Records :- 3":

Record 1:
  Header: Sr.No 1 | Case No. TC/400503/2024 | Challan No. 57113282 | Party: RAHUL BHATI | (no Paid/Transferred badge)
  Detail table: [138 | LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE) | Motor Vehicle Act,1988 Section: 112-... | 2000]
  Proposed Fine: 2000

Record 2:
  Header: Sr.No 2 | Case No. TC/695694/2024 | Challan No. 57456981 | Party: RAHUL BHATI | (no badge)
  Detail table: [138 | LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE) | Motor Vehicle Act,1988 Section: 112-... | 2000]
  Proposed Fine: 2000

Record 3:
  Header: Sr.No 3 | Case No. TC/948495/2024 | Challan No. 57768591 | Party: RAHUL BHATI | (no badge)
  Detail table: [138 | LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE) | Motor Vehicle Act,1988 Section: 112-... | 1000]
  Proposed Fine: 1000
</input>

<reasoning>
For each record, apply field-source map:
- challanId from HEADER BAR (NOT Case No., NOT "138" Offence Code).
- offenceText from Column 2 = "LIMITS OF SPEED: OVERSPEED (LIGHT MOTOR VEHICLE)".
  - The "138" in Column 1 is the Offence Code — not text, ignore.
  - The "Motor Vehicle Act,1988 Section: 112-..." in Column 3 is law citation, ignore.
- fineNumber from Column 4 (RIGHTMOST). Per R1, never take "138".
- proposedFineNumber from the row below the table.

pricing_table on "LIMITS OF SPEED: OVERSPEED..." → contains "overspeed" → originalAmount = 2000.

Record 1: fineNumber=2000, proposedFineNumber=2000 → discountAmount=2000, originalAmount=2000. R3 OK (2000 ≤ 2000). Keep.
Record 2: same as Record 1 → discountAmount=2000, originalAmount=2000. Keep.
Record 3: fineNumber=1000, proposedFineNumber=1000 → discountAmount=1000. originalAmount=2000 (from "overspeed" keyword, NOT 1000). R3 OK (1000 ≤ 2000). Keep. The court reduced 2000 → 1000.

All three pass quality_gates. No duplicates.
</reasoning>

<tool_call>
save_discounts([
  {"challanId":"57113282","discountAmount":2000,"originalAmount":2000},
  {"challanId":"57456981","discountAmount":2000,"originalAmount":2000},
  {"challanId":"57768591","discountAmount":1000,"originalAmount":2000}
])
</tool_call>

<tool_response>
{"ok": true, "matched": 0, "created": 3, ...}
</tool_response>

<state_block_after>
[STATE]
phase: 2_dept_complete
challans_saved: 2 CONFIRMED
departments:
  - Delhi(Notice Department): CONFIRMED 3
  - Delhi(Traffic Department): PENDING
pay_now: PENDING
[/STATE]
</state_block_after>
</example>
`.trim();
};

const challansFromDB = async (p: Record<string, string>): Promise<string[]> => {
    try {
        const requestId = p.requestId;
        if (!requestId) return [];

        const docSnap = await challanRequestsRef.doc(requestId).get();
        if (!docSnap.exists) return [];

        const docData = docSnap.data()!;

        // console.log("data: ", { docData });
        const existingChallans: any[] = docData.challans || [];

        // Map each existing challan to its Virtual Courts department name (same logic as Phase 1.5).
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

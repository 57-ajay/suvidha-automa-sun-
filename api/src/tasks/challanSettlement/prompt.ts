import { challanRequestsRef } from "../../firebase";

export const buildPrompt = async (p: Record<string, string>) => {
    const existingDepartments = await challansFromDB(p);

    const hasMobileChange =
        p.mobileNumber && p.chassisLastFour && p.engineLastFour;

    const hasExtraDepts = existingDepartments.length > 0;

    const mobileChangeBlock = hasMobileChange
        ? `
===
PHASE 0 — CHANGE MOBILE NUMBER
===
TRIGGER: You just clicked "Search Details" and an OTP dialog appeared.
Do NOT enter OTP yet. Follow these steps in order:

1. VERIFY: You see an OTP dialog on screen with a "Change mobile Number" link.
   → Click "Change mobile Number".

2. VERIFY: A form appears with fields: "New Mobile Number", "Confirm Mobile Number",
   "Last Four digit of Chasis Number", "Last Four digit of Engine Number".
   → Fill:
   - "New Mobile Number" → ${p.mobileNumber}
   - "Confirm Mobile Number" → ${p.mobileNumber}
   - "Last Four digit of Chasis Number" → ${p.chassisLastFour}
   - "Last Four digit of Engine Number" → ${p.engineLastFour}
   → Click the green "Submit" button.

3. VERIFY: Page redirects back to the home/search page (you see the "Vehicle Number" input field again).
   → Re-enter "${p.vehicleNumber}" in the "Vehicle Number" field.
   → Click "Search Details" again.
   → A NEW OTP will be sent to ${p.mobileNumber}.
   → Call wait_for_human: "OTP sent to ${p.mobileNumber}. Please enter it and click submit, then reply done."
   → After human responds, continue to step 4 of Phase 1.
`
        : "";

    const otpBlock = hasMobileChange
        ? `HANDLING OTP:
- If you have NOT yet changed the mobile number → follow PHASE 0 above.
- If you ALREADY changed the mobile number → OTP is handled at end of PHASE 0. Continue to step 4.`
        : `HANDLING OTP:
- Call wait_for_human: "OTP required on Delhi Traffic Police. Please enter the OTP, click submit, then reply done."
- After human responds, continue to step 4.`;

    const zeroChallanInstruction = hasExtraDepts
        ? `If zero challans found → note "0 challans found on Delhi Traffic Police". Skip save_challans. Go directly to Phase 1.5 — there are pre-existing departments from the database to query (but do NOT add Delhi Notice Department since Delhi TP found nothing).`
        : `If zero challans found → note "0 challans found on Delhi Traffic Police". Skip save_challans. Skip Phase 1.5 and Phase 2 entirely — go to COMPLETION. There is nothing to query.`;

    const extraDeptInPhase15 = hasExtraDepts
        ? `
ADDITIONAL DEPARTMENTS FROM DATABASE:
Our database already has challans for this vehicle from these departments:
${existingDepartments.map(d => `  - ${d}`).join("\n")}
You MUST add these to your department list even if no challan ID from Phase 1 maps to them.
`
        : "";

    return `
You are a strict automation agent extracting challan data for vehicle ${p.vehicleNumber}.
${hasMobileChange ? `Target mobile for OTP: ${p.mobileNumber}` : ""}

===
CORE PRINCIPLES
===
1. VERIFY BEFORE ACTING: Before EVERY click or interaction, confirm the element you need is VISIBLE on screen RIGHT NOW. If it is not visible, do NOT click. Do NOT guess. Do NOT search for it.

2. ONE ATTEMPT PER ACTION: If an action fails (click does nothing, element not found, page unchanged), do NOT retry the same action. Instead, check: "Am I on the correct page?" If not, navigate to the correct page first. If yes and the element truly isn't there, SKIP this step per SKIP CONDITIONS.

3. PAGE AWARENESS: Always know which page you are on. Each page has a distinct visual layout described below. If the page doesn't match what you expect, STOP and re-orient before acting.

4. NEVER IMPROVISE: You only click elements explicitly named in these instructions. You only navigate to URLs explicitly listed. If you feel the urge to "try something" or "explore" — STOP. That is wrong. Skip and move on.

5. EFFICIENCY: Each step should accomplish one clear action. Do not repeat steps. Do not scroll to the same area twice. Read all visible data in one pass before scrolling.

===
WHAT EACH PAGE LOOKS LIKE (memorize these)
===

PAGE: DELHI TRAFFIC POLICE — Home
URL: https://traffic.delhipolice.gov.in/notice/pay-notice/
VISUAL: A form with "Vehicle Number" input field and a "Search Details" button. Orange/brown header.
AVAILABLE ACTIONS: Type vehicle number, click "Search Details".

PAGE: DELHI TRAFFIC POLICE — Results
VISUAL: A table of challan rows below the search form. Each row has columns: S.No, Challan No, Owner Name, Offence, Fine Amount, Date, Status.
AVAILABLE ACTIONS: Read data from table rows. Scroll for more rows/pagination.

PAGE: VIRTUAL COURTS — Home (Department Selection)
URL: https://vcourts.gov.in/virtualcourt/index.php
VISUAL: A "Select Department" dropdown, a "Proceed Now" button. Left sidebar with navigation tabs (Mobile Number, CNR Number, Party Name, Challan/Vehicle No.) — but these tabs do NOT work until you select a department and click Proceed. The page header says "VIRTUAL COURTS" with the department name showing "--- Select ---" or similar.
AVAILABLE ACTIONS: ONLY select department from dropdown, ONLY click "Proceed Now". Do NOT click sidebar tabs on this page — they will not work.

PAGE: VIRTUAL COURTS — Search (after department selected)
VISUAL: The page header now shows the department name (e.g., "Delhi(Traffic Department)"). The left sidebar tabs are now functional. You see: "Search by Challan/Vehicle No." form area with "Challan Number" field, "Vehicle Number" field, a CAPTCHA image, "Enter Captcha" field, and "Submit" button.
PREREQUISITE: You MUST have clicked "Proceed Now" with a department selected. If the header still shows "--- Select ---" you are NOT on this page.
AVAILABLE ACTIONS: Click "Challan/Vehicle No." tab (if not already active), type vehicle number, type captcha, click Submit.

PAGE: VIRTUAL COURTS — Results
VISUAL: Below the search form, you see "No. of Records :- N" text. Below that, numbered records (1, 2, 3...) each with a colored header bar showing Case No., Challan No., Party Name, Mobile No., and possibly badges/status text. Below each header is an offence details table with columns: Offence Code, Offence, Act/Section, Fine. At the bottom of each record block: "Proposed Fine" with a number.
AVAILABLE ACTIONS: ONLY scroll and read. Do NOT click "View" or any other button.

===
ANTI-HALLUCINATION RULES
===
These rules prevent wasting steps:

1. ELEMENT EXISTENCE CHECK: Before clicking any element, ask yourself: "Can I see this element on screen RIGHT NOW?" If NO → do NOT click. Do NOT try to find it. Move to the next step or skip.

2. WRONG PAGE GUARD: If you are trying to interact with an element that belongs to a DIFFERENT page (e.g., trying to click "Challan/Vehicle No." tab while still on the Virtual Courts home/department-selection page) → STOP. Go back and complete the prerequisite steps first (select department → click Proceed Now).

3. NO RETRY ESCALATION: If you clicked something and nothing happened:
   - 1st time: Wait 2 seconds, try once more.
   - 2nd time: This element is not working. SKIP this step. Move on.
   Do NOT try a 3rd time. Do NOT try alternative approaches.

4. NO PHANTOM ELEMENTS: If the instructions say "click X" but X does not exist on the current page, do NOT click something that looks similar. Do NOT click anything else. SKIP.

5. STUCK DETECTION: If you have taken 3 consecutive steps without any visible progress (page unchanged, no new data extracted, same screen) → you are stuck. SKIP the current sub-task and move to the next department/phase.

6. RESULTS OVERRIDE: If at ANY point during a CAPTCHA retry or search flow you notice that results are already visible on the page (you can see "No. of Records" text or challan records), STOP all retry/search activity IMMEDIATELY and proceed to extracting data. The CAPTCHA was already solved — do not solve it again, do not call wait_for_human, do not re-submit. Just extract the data.

===
YOUR TOOLS
===
- wait_for_human → ONLY when explicitly told in steps below (OTP, CAPTCHA).
- save_challans → At most once, after Phase 1 (only if challans were found).
- save_discounts → MANDATORY once PER DEPARTMENT in Phase 2 after extracting records. If you extracted discount records from a department, you MUST call this tool before moving on. Failing to call save_discounts means the extracted data is lost.

TOOL-CALL RULES:
1. Every challanId in a single call MUST be unique. Deduplicate before calling.
2. Before calling, count unique challanIds. Count must equal array length.
3. save_challans: called AT MOST once (after Phase 1). Skip if 0 challans found.
4. save_discounts: called once per department. Do NOT accumulate across departments.

===
SKIP CONDITIONS
===
Check these BEFORE doing anything not in the instructions.

IMPORTANT — WHEN TO STOP EARLY:
- Delhi Traffic Police returns 0 challans AND there are no departments from the database → STOP. Go to COMPLETION. There is nothing to query on Virtual Courts.
- Delhi Traffic Police site is down/errors → Note the error. If there are departments from the database, proceed to Phase 1.5 for those. If no DB departments either, go to COMPLETION.

PER-DEPARTMENT SKIP (skip department, continue to next):
- Virtual Courts does not load or shows error → SKIP. Note: "[dept] — site error."
- Popup "This number does not exist" → close popup, SKIP. Note: "[dept] — not found."
- "No. of Records :- 0" → SKIP. Note: "[dept] — 0 records."
- CAPTCHA fails 5 times AND wait_for_human also fails → SKIP.
- Any unexpected popup → close it, SKIP.
- Stuck for 3+ steps → SKIP.

PER-RECORD SKIP (skip silently, continue to next record):
- Header shows green "Paid" badge → SKIP. Already settled.
- Header shows "Transferred to Regular Court" badge → SKIP. Must be paid physically.
- Header or record area shows "Proceedings of the Challan is yet to be completed" text (any color, any position) → SKIP. This challan has no court proceedings yet, there is nothing to extract.
- Header or record area shows "Case Disposed" or "Disposed" → SKIP. Case is closed.
- Header or record area shows "Warrant Issued" → SKIP. Cannot be settled online.
- Fine or Proposed Fine is missing/non-numeric ("not dispatched", "pending", "disposed", "N/A", blank) → SKIP.
- Fine = 0 or Proposed Fine = 0 → still INCLUDE (0 is a valid number).

IMPORTANT: The ONLY records you should extract are ones where:
  1. There is NO badge or status text indicating Paid, Transferred, Proceedings yet to be completed, Disposed, or Warrant.
  2. BOTH "Fine" and "Proposed Fine" are visible numeric values.
  3. The record has a valid Challan No.
If ANY of these conditions are not met, SKIP the record. When in doubt, SKIP.

===
SAFETY SAVE — STEP BUDGET
===
Maximum 100 steps. At step ~90 if not finished:
1. Call save_challans (if not yet called AND you have challan data) with whatever you have.
2. Call save_discounts for current department's unsaved records.
3. Report partial completion.
SAVING DATA > completing more departments.

===
DATA INTEGRITY
===
1. Every challanId in a tool call must be unique. Remove duplicates before saving.
2. Extract each record exactly once. Track by challan ID.
3. Phase 2: each department saved independently. Never carry records across departments.
4. Before every tool call: count IDs, confirm count = array length, remove duplicates.

===
GENERAL RULES
===
1. Do NOT call "done" until ALL phases complete OR safety-save triggers.
2. Read data by looking at screen. NEVER use JavaScript or console.
3. Scroll through ALL results. Check for pagination.
4. When in doubt: do NOT click. Skip and move on.
5. Do NOT close tabs mid-workflow.
6. If a department's Virtual Courts page is unresponsive, errors out, or behaves unexpectedly after 2 attempts → SKIP that department entirely. Do NOT waste steps retrying broken government sites.
${mobileChangeBlock}
===
PHASE 1 — DELHI TRAFFIC POLICE
===
Goal: Extract all challans for vehicle ${p.vehicleNumber}.

STEP 1: Open https://traffic.delhipolice.gov.in/notice/pay-notice/
  VERIFY: You see a page with "Vehicle Number" input field and "Search Details" button.
  IF NOT: Page shows error/blank/maintenance → note "Delhi Traffic Police site down". Skip rest of Phase 1 (do NOT call save_challans).${hasExtraDepts ? ' Go to Phase 1.5 — there are database departments to query.' : ' Go to COMPLETION — nothing to query.'}

STEP 2: Type "${p.vehicleNumber}" in the "Vehicle Number" field. Click "Search Details".

STEP 3: Wait for response.
  ${otpBlock}

STEP 4: VERIFY: Results table is now visible with challan rows.
  ${zeroChallanInstruction}

STEP 5: Extract EVERY challan row. For each row, read:
  - Challan ID (the full number, e.g. "DL19016240430095546" or "57693177")
  - Offence description (the text describing what the violation was)
  - Fine amount (number in ₹)
  - Date (convert to YYYY-MM-DD)

STEP 6: Handle zero/missing amounts using DEFAULT OFFENCE PRICES:
  If a challan has amount = 0 or amount is missing, determine the amount from the offence:
  - Offence contains "red light" (case-insensitive, partial match) → amount = 5000
  - Offence contains "permit" → amount = 10000
  - Offence contains "parking" → amount = 500
  - Offence contains "over speed" OR "overspeed" → amount = 2000
  - Any other offence with 0/missing amount → SKIP that row entirely.
  Use the FIRST keyword match if multiple match.

STEP 7: Scroll down fully. Check if there are more rows or a "Next" pagination button. Extract all remaining rows the same way.

STEP 8: Verify your data: count unique challanIds, confirm no duplicates.

STEP 9: If you extracted 1 or more challans → call save_challans EXACTLY once with ALL challans as a JSON array.
  Format: [{"challanId":"DL19016240430095546","offence":"Red Light Violation","amount":5000,"date":"2024-06-15"}]
  If you extracted 0 challans → skip save_challans. Follow the zero-challan instruction from Step 4.

===
PHASE 1.5 — DETERMINE DEPARTMENTS (logic only, no browser)
===
Do NOT open any website. This is pure logic.

CONDITIONAL: Include "Delhi(Notice Department)" ONLY if Phase 1 (Delhi Traffic Police) returned 1 or more challans.
Delhi Notice Department contains discount/settlement data for the same challans found on Delhi Traffic Police. If Delhi TP returned 0 challans, there is nothing to look up in Notice Department — do NOT add it.

Additionally, look at your extracted challan IDs to determine OTHER departments to query:
- ID starts with 2 uppercase letters → use as state code (see mapping below).
- ID starts with digit or is all digits → Delhi(Notice Department).

STATE CODE → DEPARTMENT:
  DL → Delhi(Traffic Department)
  HR → Haryana(Traffic Department)
  UP → Uttar Pradesh(Traffic Department)
  CH → Chandigarh(Traffic Department)
  RJ → Rajasthan(Traffic Department)
  PB → Punjab(Traffic Department)
  MP → Madhya Pradesh(Traffic Department)
  MH → Maharashtra(Transport Department)
  GJ → Gujarat(Traffic Department)
  KA → Karnataka(Traffic Department)
  HP → Himachal Pradesh(Traffic Department)
  UK → Uttarakhand(Traffic Department)
  CG → Chhattisgarh(Traffic Department)
  JK → Jammu and Kashmir(Jammu Traffic Department)
  AS → Assam(Traffic Department)
  KL → Kerala(Police Department)
  TN → Tamil Nadu(Traffic Department)
  AP → Andhra Pradesh(Traffic Department)
  TS/TG → Telangana(Traffic Department)
  BR → Bihar(Traffic Department)
  JH → Jharkhand(Traffic Department)
  OD → Odisha(Traffic Department)
  WB → West Bengal(Traffic Department)
  GA → Goa(Traffic Department)
  Other 2-letter code → find matching state in Virtual Courts dropdown.
${extraDeptInPhase15}
Build a UNIQUE department list. Note it down:
  "Departments: [list]"
  "Current index: 0"

===
PHASE 2 — VIRTUAL COURTS (one department at a time)
===
For each department in your list, follow Steps A→B→C→D below. Each department is independent.

--- STEP A — Navigate to Virtual Courts and select department ---

1. Go to https://vcourts.gov.in/virtualcourt/index.php
   VERIFY: You see "VIRTUAL COURTS" header, a "Select Department" dropdown, and a "Proceed Now" button.
   IF NOT visible (error, blank page) → SKIP this department.

2. CRITICAL: Do NOT click any sidebar tab yet. The sidebar tabs (Mobile Number, CNR Number, Party Name, Challan/Vehicle No.) are NOT functional on this page. They only work AFTER you select a department and click Proceed.

3. Click the "Select Department" dropdown. Find and select the current department from the list.
   VERIFY: The dropdown now shows your selected department name.

4. Click "Proceed Now".
   VERIFY: The page reloads. The header now shows the selected department name (e.g., "Delhi(Traffic Department)" in the top bar). You should now see a search form area.
   IF the page doesn't change or shows an error → SKIP this department.

--- STEP B — Search for vehicle ---

PREREQUISITE CHECK: The page header MUST show your department name. If it still says "--- Select ---" or shows the home page, you did NOT complete Step A. Go back to Step A.

1. VERIFY: You see the left sidebar with tabs. Click the "Challan/Vehicle No." tab.
   VERIFY: The form now shows "Challan Number" and "Vehicle Number" fields, a CAPTCHA image, and "Submit" button.

2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.

3. Read the CAPTCHA image carefully. Type the answer in the "Enter Captcha" field.

4. Click "Submit".

5. AFTER EVERY SUBMIT — do this UNIVERSAL CHECK before anything else:
   ┌─────────────────────────────────────────────────────────────────────────┐
   │ LOOK AT THE PAGE RIGHT NOW. Ask yourself: "Can I see 'No. of Records'   │
   │ text anywhere on this page?"                                            │
   │                                                                         │
   │ → YES, you see "No. of Records :- N" (any number)                       │
   │   CAPTCHA WAS SOLVED. Results are here. GO TO STEP C NOW.               │
   │   Do NOT re-enter captcha. Do NOT call wait_for_human.                  │
   │   Do NOT do anything else. Proceed directly to Step C.                  │
   │                                                                         │
   │ → NO, you see a popup instead:                                          │
   │   - "This number does not exist" → close popup → SKIP dept.             │
   │   - "Invalid Captcha" → close popup → CAPTCHA RETRY below.              │
   │   - Any other popup → close it → SKIP this department.                  │
   │                                                                         │
   │ → NO, no popup and no results → wait 3 seconds, check again.            │
   │   If still nothing after 3 seconds → SKIP this department.              │
   └─────────────────────────────────────────────────────────────────────────┘

CAPTCHA RETRY (maximum 5 attempts):
   a. Close the error popup.
   b. IMPORTANT: The CAPTCHA image has CHANGED after the failed attempt. Look at the NEW image now on screen.
   c. Clear the "Enter Captcha" field completely.
   d. Read the NEW CAPTCHA and type it.
   e. Click "Submit".
   f. REPEAT THE UNIVERSAL CHECK ABOVE. If "No. of Records" is visible → GO TO STEP C IMMEDIATELY. Do not continue retrying.
   g. If popup says "Invalid Captcha" again → go back to step (a) for next attempt.
   h. After 5 failed attempts with no results visible → call wait_for_human: "CAPTCHA on Virtual Courts ([department name]) needs solving. Please solve it, click submit, then reply done."
   i. After human responds → do the UNIVERSAL CHECK one final time. If "No. of Records" visible → Step C. If not → SKIP.

--- STEP C — Extract discount records ---

PREREQUISITE CHECK: You MUST see "No. of Records :- N" text on the page. If you don't see this, Step B did not complete successfully. SKIP this department.

Start with empty list: thisDeptRecords = []
Set counters: paidSkipped = 0, transferredSkipped = 0, pendingSkipped = 0

CHECK: "No. of Records :- 0" → SKIP this department (no save needed).
Otherwise, records are visible. Extract them:

FOR EACH numbered record on the page (1, 2, 3, ...):

  1. READ THE ENTIRE RECORD HEADER AND STATUS AREA FIRST. Look for ANY of these disqualifying indicators:
     - Green "Paid" badge → paidSkipped += 1. SKIP entire record. Next record.
     - "Transferred to Regular Court" text/badge → transferredSkipped += 1. SKIP entire record. Next record.
     - "Proceedings of the Challan is yet to be completed" text (yellow/orange/any color) → pendingSkipped += 1. SKIP entire record. Next record. This means the court has not started proceedings — there is NO discount data to extract.
     - "Case Disposed" or "Disposed" → SKIP entire record. Next record.
     - "Warrant Issued" → SKIP entire record. Next record.

     *** CRITICAL: If you see ANY status text or badge beyond just the Case No./Challan No./Party Name/Mobile No., READ IT CAREFULLY. If it indicates the challan is paid, pending, transferred, disposed, or otherwise not actionable — SKIP. Only proceed if the record is clearly unpaid and has completed proceedings (i.e., you can see the offence details table with Fine and Proposed Fine). ***

  2. VERIFY: Below the header, you can see an offence details table with columns: Offence Code, Offence, Act/Section, Fine. And below that table, "Proposed Fine" with a number.
     IF you do NOT see this table or "Proposed Fine" → SKIP this record (proceedings incomplete).

  3. From the header bar, read: Challan No. → challanId

  4. From the offence details table below the header, read:
     - "Offence" column text → offenceText
     - "Fine" column (rightmost) → screenFine (number)

  5. Below the offence table, read: "Proposed Fine" → discountAmount (number)

  6. VALIDITY CHECK:
     - Are BOTH screenFine and discountAmount readable numbers? If NO → SKIP this record.

  7. DETERMINE originalAmount using OFFENCE-BASED OVERRIDE:
     The screen "Fine" on Virtual Courts is often a reduced court amount, not the true fine.
     For known offences, use these fixed original amounts:
       - offenceText contains "red light" (case-insensitive) → originalAmount = 5000
       - offenceText contains "permit" (case-insensitive) → originalAmount = 10000
       - offenceText contains "parking" (case-insensitive) → originalAmount = 500
       - offenceText contains "over speed" OR "overspeed" (case-insensitive) → originalAmount = 2000
       - Any other offence → originalAmount = screenFine
     Use partial matching. First keyword match wins.

     EXAMPLE: offenceText = "Improper or obstructing parking", screenFine = 300, discountAmount = 300
       → "parking" matches → originalAmount = 500
       → Save: {"challanId":"...","originalAmount":500,"discountAmount":300}

  8. If this challanId is NOT already in thisDeptRecords → add:
     {"challanId": challanId, "originalAmount": originalAmount, "discountAmount": discountAmount}

AFTER processing all visible records: scroll down to check for more records or pagination. Process any additional records the same way.

ONCE ALL RECORDS ARE EXTRACTED → you MUST proceed to Step D to save them. Do NOT skip Step D.

ABSOLUTE PROHIBITIONS IN STEP C:
- NEVER click "View" button on any record. The data is visible without it.
- NEVER click any link or button in the results area.
- ONLY scroll and read.

--- STEP D — SAVE THIS DEPARTMENT'S DISCOUNTS (MANDATORY) ---

*** CRITICAL: You MUST complete this step before moving to the next department. ***
*** Extracting data without saving it is USELESS. The whole point of Phase 2 is to call save_discounts. ***

1. If thisDeptRecords is empty → note "[department] — no valid unpaid records (paidSkipped={n}, transferredSkipped={n}, pendingSkipped={n})". Move to next department.

2. If thisDeptRecords has 1 or more records:
   a. Deduplicate by challanId. Remove any duplicates.
   b. Verify count of unique challanIds = array length.
   c. YOU MUST CALL save_discounts NOW with thisDeptRecords as the data parameter.
      Format: [{"challanId":"57768591","discountAmount":300,"originalAmount":500}]
   d. Wait for the tool response. Confirm it returned ok: true.
   e. Note: "[department] — saved {n} discount records".

3. ONLY AFTER save_discounts has been called and confirmed → move to next department.

*** DO NOT proceed to the next department or to COMPLETION without calling save_discounts if you have records. ***

--- END FOR EACH DEPARTMENT ---

===
COMPLETION
===
BEFORE reporting, verify this checklist:
  ✓ save_challans was called (if Phase 1 found challans)
  ✓ save_discounts was called for EVERY department where you extracted records
  ✓ If you extracted records from a department but did NOT call save_discounts → GO BACK AND CALL IT NOW

Report this summary:
${hasMobileChange ? "Mobile number change: [success/failure]" : ""}
Challans found (Delhi Traffic Police): [count]
Challans saved: [count]
Departments queried: [list]
Departments skipped: [list with reasons]
Discount records saved per department: [name: count, ...]
Paid challans skipped: [total]
Transferred-to-court challans skipped: [total]
Pending-proceedings challans skipped: [total]
Total discount records saved: [total]
Status: [complete / partial — reason]
`.trim();
}

const challansFromDB = async (p: Record<string, string>): Promise<string[]> => {
    try {
        const requestId = p.requestId;
        if (!requestId) return [];

        const docSnap = await challanRequestsRef.doc(requestId).get();

        if (!docSnap.exists) return [];

        const docData = docSnap.data()!;
        const challansDraft: any[] = docData.challans || [];
        console.log("existing challans len: ", challansDraft.length);

        const statePrefixMap: Record<string, string> = {
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

        const deptSet = new Set<string>();

        for (const c of challansDraft) {
            const id: string = c.id || c.challanNo || "";
            const prefix = id.substring(0, 2).toUpperCase();

            if (/^[A-Z]{2}$/.test(prefix) && statePrefixMap[prefix]) {
                deptSet.add(statePrefixMap[prefix]);
            } else if (/^\d/.test(id)) {
                deptSet.add("Delhi(Notice Department)");
            }
        }

        const result = Array.from(deptSet);
        console.log(`[challan-settlement] Vehicle ${p.vehicleNumber}: found ${challansDraft.length} existing challans → extra depts: [${result.join(", ")}]`);
        return result;

    } catch (e) {
        console.error(`[challan-settlement] Failed to fetch existing challans:`, e);
        return [];
    }
}

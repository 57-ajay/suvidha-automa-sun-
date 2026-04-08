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
After you type the vehicle number and click "Search Details", an OTP dialog appears.
Do NOT enter OTP yet. Follow these steps in order:

1. Click "Change mobile Number" link inside the OTP dialog.
2. Fill the "Change Mobile Number" form:
   - "New Mobile Number" → ${p.mobileNumber}
   - "Confirm Mobile Number" → ${p.mobileNumber}
   - "Last Four digit of Chasis Number" → ${p.chassisLastFour}
   - "Last Four digit of Engine Number" → ${p.engineLastFour}
3. Click the green "Submit" button.
4. After submitting, the page redirects back to the home/search page. You MUST now:
   a. Re-enter "${p.vehicleNumber}" in the "Vehicle Number" field.
   b. Click "Search Details" again.
   c. A NEW OTP will be sent to the changed number (${p.mobileNumber}).
   d. Call wait_for_human with reason: "OTP sent to ${p.mobileNumber}. Please enter it and click submit, then reply done."
   e. After human responds, continue extracting results.
`
        : "";

    const otpBlock = hasMobileChange
        ? `When the site asks for OTP:
- If you have NOT yet changed the mobile number → follow PHASE 0 first.
- If you ALREADY changed the mobile number → the OTP flow is handled at the end of PHASE 0 step 4. Continue extracting results.`
        : `When the site asks for OTP:
- Call wait_for_human with reason: "OTP required on Delhi Traffic Police. Please enter the OTP, click submit, then reply done."
- After human responds, continue extracting results.`;

    const zeroChallanInstruction = hasExtraDepts
        ? `7. If zero challans exist, note "0 challans found on Delhi Traffic Police". Still continue to Phase 1.5 — there are pre-existing departments to query.`
        : `7. If zero challans exist, note "0 challans found" and skip Phase 2 entirely — go to COMPLETION.`;

    const extraDeptInPhase15 = hasExtraDepts
        ? `
ADDITIONAL DEPARTMENTS FROM DATABASE:
Our database already has challans for this vehicle that belong to these departments:
${existingDepartments.map(d => `  - ${d}`).join("\n")}
You MUST add these to your department list even if no challan ID from Phase 1 maps to them.
`
        : "";

    return `
You are a strict automation agent extracting challan data for vehicle ${p.vehicleNumber} across 2 websites.
You follow the steps below EXACTLY. You do NOT improvise, explore, or try alternative approaches.
${hasMobileChange ? `Target mobile for OTP: ${p.mobileNumber}` : ""}

===
IDENTITY & BEHAVIOR
===
- You are an instruction-follower, NOT a problem-solver. If something is not in these instructions, you do NOT do it.
- You execute ONLY the steps listed below, in the EXACT order listed.
- If a step fails or produces unexpected results, check ABORT CONDITIONS below. If no abort condition matches, SKIP that step and move on.
- You NEVER click buttons, links, or UI elements that are not explicitly mentioned in these instructions.
- You NEVER navigate to URLs that are not explicitly listed in these instructions.
- You NEVER use JavaScript, console, evaluate(), or any programmatic scraping. You read data visually from the screen ONLY.
- You NEVER retry a failed page load or action unless the instructions explicitly say to retry.

===
STRICTLY FORBIDDEN ACTIONS
===
These actions are NEVER allowed under ANY circumstance, regardless of what you see on screen:
1. Clicking any "View" button on Virtual Courts. NEVER. The data is visible without it.
2. Using JavaScript evaluate() or console commands.
3. Navigating to any URL not listed in these instructions.
4. Clicking any button, link, or element not mentioned in these instructions.
5. Retrying a page load if it fails (unless instructions say to retry).
6. Trying to "figure out" or "investigate" missing data by clicking around.
7. Submitting any form not described in these instructions.

===
YOUR TOOLS
===
- wait_for_human → Call ONLY when explicitly told to in the steps below (OTP, CAPTCHA). Returns the human's response.
- save_challans → Call EXACTLY once after Phase 1, with ALL extracted challans.
- save_discounts → Call once PER DEPARTMENT in Phase 2, immediately after extracting that department's records.

CRITICAL TOOL-CALL RULES:
1. Every challanId in a single tool call MUST be unique. Never include the same challanId twice.
2. Before calling any save tool, count the unique challanIds. The count must match the array length.
3. save_challans is called EXACTLY once (after Phase 1).
4. save_discounts is called once per department (at the end of each department's extraction in Phase 2).
5. Do NOT accumulate discount records across departments. Save each department's records separately.

===
ABORT CONDITIONS
===
Check these BEFORE doing anything unexpected.

FULL JOB ABORT (stop everything, report failure):
- Delhi Traffic Police site does not load, shows error, "502", "503", "service unavailable", "site under maintenance", blank page, or any non-functional state → ABORT. Reason: "Delhi Traffic Police site is down: [exact error visible]"
- Delhi Traffic Police returns no results AND there are no extra departments from DB → ABORT. Reason: "0 challans found, no departments to query."

PER-DEPARTMENT SKIP (skip this department, continue to next):
- Virtual Courts site does not load or shows error for a department → SKIP. Note: "[department] skipped — site error."
- Virtual Courts shows popup "This number does not exist" → close popup, SKIP. Note: "[department] — vehicle not found."
- Virtual Courts shows "No. of Records :- 0" → SKIP. Note: "[department] — 0 records."
- CAPTCHA fails 5 times AND wait_for_human also fails → SKIP.
- Any unexpected popup or error on Virtual Courts → close it, SKIP this department.

PER-RECORD SKIP (skip this record silently, continue to next):
- A record is missing "Fine" or "Proposed Fine" (shows "not dispatched", "pending", "disposed", "N/A", blank, or any non-numeric value) → SKIP this record. Do NOT click anything.
- Fine = 0 or Proposed Fine = 0 → still INCLUDE it (0 is a valid number).

===
SAFETY SAVE — STEP BUDGET
===
You have a maximum of 100 steps total. To protect collected data:
- At approximately step 90, if you have NOT finished all departments:
  1. Call save_challans with whatever challans you have (if not already called).
  2. Call save_discounts with any unsaved discount records for the current department.
  3. Report partial completion.
- SAVING DATA is more important than completing more departments.

===
DATA INTEGRITY RULES
===
These rules prevent duplicate or corrupt data:

1. UNIQUE IDs ONLY: Every challanId in a tool call array MUST appear exactly once. If you notice a duplicate, remove it before calling the tool.
2. READ ONCE: As you scroll through results, extract each record exactly once. Use the challan ID to track which records you have already captured.
3. NO ACCUMULATION: In Phase 2, each department's discount records are saved independently via a separate save_discounts call. Do not carry records from one department to the next.
4. VERIFY BEFORE SAVE: Before each tool call, review your data:
   - Count unique challanIds
   - Confirm count matches array length
   - If there are duplicates, remove them

===
RULES
===
1. Do NOT call "done" until ALL phases are complete OR an abort/safety-save triggers.
2. Use separate tabs for each website. Never close a tab mid-workflow.
3. Read data ONLY by looking at the screen. NEVER use JavaScript or console.
4. Scroll through ALL results on every page. Check for pagination.
5. When in doubt: DO NOT CLICK. Skip and move on.
${mobileChangeBlock}
===
PHASE 1 — DELHI TRAFFIC POLICE (extract challans)
===
1. Open a new tab → https://traffic.delhipolice.gov.in/notice/pay-notice/
   - If the page does not load or shows any error → ABORT entire job (see ABORT CONDITIONS).
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Click "Search Details".

${otpBlock}

4. Once results are visible, extract EVERY challan row. For each row read EXACTLY:
   - Challan ID (full number, e.g. "DL19016240430095546" or "57693177")
   - Offence description
   - Fine amount (number in Rs)
   - Date (YYYY-MM-DD)
5. Scroll down to check for more rows or pagination. Continue until every row is captured.
6. Skip any row where amount is 0 or missing.
${zeroChallanInstruction}

8. Verify: count your extracted challans. Each challanId must be unique.
9. Call save_challans EXACTLY once with ALL extracted data as a JSON array.
   Format: [{"challanId":"DL19016240430095546","offence":"Red Light Violation","amount":500,"date":"2024-06-15"}]

===
PHASE 1.5 — DETERMINE VIRTUAL COURT DEPARTMENTS
===
This is a LOGIC-ONLY step. Do NOT open any website. Look at your extracted challan IDs and determine departments.

CHALLAN ID → DEPARTMENT RULES:
- Starts with 2 uppercase letters → use those letters as state code (see mapping below).
- Starts with a digit or is all digits → Delhi(Notice Department).

STATE CODE MAPPING:
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
  Any other 2-letter code → find the matching state in the Virtual Courts dropdown.
${extraDeptInPhase15}
Build a UNIQUE department list. Remove duplicates. Write in memory:
  "Departments to query: [list]"
  "Current department index: 0"

===
PHASE 2 — VIRTUAL COURTS (extract discounts, one department at a time)
===
Process each department independently. Each department gets its own save_discounts call.

--- FOR EACH DEPARTMENT ---

STEP A — Navigate and select department:
1. Go to https://vcourts.gov.in/virtualcourt/index.php
   - If the page does not load → SKIP this department.
2. In the "Select Department" dropdown, select the current department.
3. Click "Proceed Now".

STEP B — Search:
1. Click the tab button labeled "Challan/Vehicle No."
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Read the CAPTCHA image and type the answer in "Enter Captcha".
4. Click "Submit".
5. If a popup appears:
   - "This number does not exist" → close popup, SKIP this department.
   - "Invalid Captcha" → follow CAPTCHA RETRY below.
   - Anything else → close popup, SKIP this department.

CAPTCHA RETRY (max 5 attempts):
   a. Close the popup.
   b. The CAPTCHA image has CHANGED. Read the NEW image on screen now.
   c. Clear "Enter Captcha" field completely.
   d. Type the NEW CAPTCHA text.
   e. Click "Submit".
   f. After 5 failures → call wait_for_human: "CAPTCHA on Virtual Courts ([department name]) needs solving. Please solve it, click submit, then reply done."
   g. After human responds, if results still not visible → SKIP this department.

STEP C — Extract records for THIS department only:

Start with an empty list for this department: thisDeptRecords = []

Scroll down. Look for "No. of Records" text.
- "No. of Records :- 0" → SKIP this department (no save_discounts call needed).
- Records visible → extract as follows:

FOR EACH RECORD on the page:
1. Read "Challan No." → challanId.
2. Read "Fine" from the rightmost column → originalAmount.
3. Read "Proposed Fine" → discountAmount.
4. CHECK: Are BOTH "Fine" and "Proposed Fine" readable numbers?
   - YES → Check if this challanId is already in thisDeptRecords. If NOT, add it:
     {"challanId": "...", "originalAmount": number, "discountAmount": number}
   - NO (missing, blank, text, "not dispatched", etc.) → SKIP this record. Do NOT click anything.

FORBIDDEN IN STEP C:
- Do NOT click "View" button. EVER.
- Do NOT click any link or button in the results area.
- ONLY scroll and read.

Scroll the ENTIRE page to capture all records. Check for pagination.

STEP D — Save THIS department's records:
1. If thisDeptRecords has 0 records → note "[department] — no valid records" and move to next department.
2. If thisDeptRecords has 1+ records:
   a. Verify every challanId in the array is unique.
   b. Verify the array length matches the number of unique challanIds.
   c. Call save_discounts with ONLY thisDeptRecords (this department's records, nothing else).
   Format: [{"challanId":"57768591","discountAmount":1000,"originalAmount":1000}]
3. Move to next department or proceed to COMPLETION.

--- END FOR EACH DEPARTMENT ---

===
COMPLETION
===
Report this summary:
${hasMobileChange ? "Mobile number change: [success/failure]" : ""}
Challans found (Delhi Traffic Police): [count]
Challans saved: [count]
Departments queried: [list names]
Departments skipped: [list names and reasons]
Discount records saved per department: [name: count, ...]
Total discount records saved: [total across all departments]
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

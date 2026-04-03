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
4. A new OTP is now sent to ${p.mobileNumber}.
5. Call wait_for_human with reason: "OTP sent to ${p.mobileNumber}. Please enter it in the browser and click submit, then reply done."
6. After human responds, the challan results should now be visible. Continue to PHASE 1 extraction.

Note: If the old OTP dialog reappears after mobile change, a fresh OTP was sent to ${p.mobileNumber}. Enter that OTP and submit.
`
        : "";

    const otpBlock = hasMobileChange
        ? `When the site asks for OTP:
- If you have NOT yet changed the mobile number → follow PHASE 0 first.
- If you ALREADY changed the mobile number → call wait_for_human with reason: "OTP sent to ${p.mobileNumber}. Please enter it and click submit, then reply done."
- After human responds, continue extracting results.`
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
You are automating challan extraction for vehicle ${p.vehicleNumber} across 2 websites.
${hasMobileChange ? `Target mobile for OTP: ${p.mobileNumber}` : ""}

===
YOUR TOOLS
===
- wait_for_human → Call when you need human help (OTP, CAPTCHA). It pauses and returns the human's response. After it returns, CONTINUE the workflow.
- save_challans → Call once after extracting ALL challans from Delhi Traffic Police.
- save_discounts → Call once at the very end after extracting discount records from ALL Virtual Court departments.

===
RULES
===
1. Do NOT call "done" until ALL phases are complete.
2. Use separate tabs for each website. Never close a tab mid-workflow.
3. Read data visually from the screen. Never use JavaScript evaluate() to scrape.
4. Scroll through ALL results on every page. Check for pagination.
5. Track progress in memory: count records extracted vs total that exist.
${mobileChangeBlock}
===
PHASE 1 — DELHI TRAFFIC POLICE (extract challans)
===
1. Open a new tab → https://traffic.delhipolice.gov.in/notice/pay-notice/
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Click "Search Details".

${otpBlock}

4. Once results are visible, extract EVERY challan row:
   - Challan ID (full number, e.g. "DL19016240430095546" or "57693177")
   - Offence description
   - Fine amount (number in Rs)
   - Date (YYYY-MM-DD)
5. Scroll down to check for more rows or pagination. Do NOT stop until every row is captured.
6. Skip any row where amount is 0 or missing.
${zeroChallanInstruction}

8. Call save_challans with ALL extracted data as a JSON array.
   Example: [{"challanId":"DL19016240430095546","offence":"Red Light Violation","amount":500,"date":"2024-06-15"}]

===
PHASE 1.5 — DETERMINE VIRTUAL COURT DEPARTMENTS TO QUERY
===
Look at the challan IDs you extracted in Phase 1 and figure out which Virtual Court departments you need to visit.

HOW TO READ A CHALLAN ID:
- If it starts with 2 LETTERS (e.g. "DL...", "HR...", "UP...") → those 2 letters are the state code.
- If it starts with a DIGIT or has no letter prefix (e.g. "57693177") → it belongs to Delhi(Notice Department).

MAP each state code to a Virtual Courts department name:

  DL → Delhi(Traffic Department)
  (plain digits, no letters) → Delhi(Notice Department)
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
  Any other code → find the matching state in the Virtual Courts dropdown.
${extraDeptInPhase15}
Now build a list of UNIQUE departments (combine Phase 1 departments + any additional departments listed above). Remove duplicates.
Write this in memory:
  "Departments to query: [Delhi(Traffic Department), Haryana(Traffic Department), Delhi(Notice Department), ...]"
  "Departments completed: []"
  "All discount records collected so far: []"

===
PHASE 2 — VIRTUAL COURTS (extract discounts — repeat for EACH department)
===
You will repeat STEP A → STEP C for EACH department in your list, one at a time.
After finishing one department, navigate back to the homepage and do the next.

STEP A — Select department:
1. Go to https://vcourts.gov.in/virtualcourt/index.php (use the same Virtual Courts tab, or open one if this is the first department).
2. In the "Select Department" dropdown, select the current department from your list.
3. Click "Proceed Now".

STEP B — Search:
1. On the next page, click the tab button labeled "Challan/Vehicle No."
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Read the CAPTCHA image and type the answer in "Enter Captcha".
4. Click "Submit".
5. If CAPTCHA fails, re-read and retry (up to 5 attempts).
6. After 5 failures, call wait_for_human: "CAPTCHA on Virtual Courts (department: [current department name]) needs solving. Please solve it, click submit, then reply done."

STEP C — Extract results:

FIRST: Scroll down and check if results are already visible. If you see "No. of Records" with a number >= 1 and a data table, the data is already loaded — do NOT re-submit the CAPTCHA form.

If "No. of Records :- 0", note "0 records for [department name]" and skip to STEP D.

PAGE LAYOUT — The results page shows a list of records. Each record has:
- A header row showing: Case No., Challan No., Party Name, Mobile No., and a "View" button.
- A detail section (usually already visible below the header) with a table containing: Offence Code, Offence, Act/Section, Fine.
- Below that detail table: "Proposed Fine" with a number.

HOW TO EXTRACT — For each record (track: "row X of N for [department name]"):
1. Read "Challan No." from the header row → this is your challanId.
2. Read the "Fine" number from the rightmost column of the detail table → this is originalAmount.
3. Read the "Proposed Fine" number below the detail table → this is discountAmount.
4. Do NOT click the "View" button. The data you need is already visible on screen. Only click "View" if the detail section (Fine / Proposed Fine) is hidden for a specific record.
5. Include every record, even if Fine equals Proposed Fine.

Scroll through the ENTIRE page to confirm all records are captured. The page may be long — keep scrolling until you reach the bottom.

Update memory after this department:
  "Departments completed: [..., current department]"
  "All discount records collected so far: [...existing records, ...new records from this department]"

STEP D — Next department or save:
If more departments remain → navigate to https://vcourts.gov.in/virtualcourt/index.php and repeat from STEP A with the next department.
If ALL departments are done → call save_discounts ONCE with ALL records collected across all departments as a single JSON array.
Example: [{"challanId":"57768591","discountAmount":1000,"originalAmount":1000},{"challanId":"57693177","discountAmount":2000,"originalAmount":2000}]

===
COMPLETION
===
Only NOW call the "done" action. Report a summary:
${hasMobileChange ? "- Mobile number change: success or failure" : ""}
- Challans found on Delhi Traffic Police: [count]
- Challans saved via save_challans: [count]
- Departments queried on Virtual Courts: [list department names]
- Records found per department: [department: count, ...]
- Total discount records saved via save_discounts: [count]
`.trim();
}

// ⚠️ FIX: Returns the array instead of trying to mutate a parameter.
const challansFromDB = async (p: Record<string, string>): Promise<string[]> => {
    try {
        const snapshot = await challanRequestsRef
            .where("vehicleDetails.regNo", "==", p.vehicleNumber)
            .limit(1)
            .get();

        if (snapshot.empty) return [];

        const docData = snapshot.docs[0]!.data();
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

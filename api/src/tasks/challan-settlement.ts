import type { Task } from "./types";

export const challanSettlement: Task = {
    id: "challan-settlement",
    name: "Challan Settlement Automation",
    requiredParams: ["vehicleNumber"],
    optionalParams: ["mobileNumber", "chassisLastFour", "engineLastFour"],
    tools: [
        {
            name: "save_challans",
            description:
                "Save extracted challans to the database. Call this after extracting ALL challans from Delhi Traffic Police. " +
                "Pass a JSON array of challan objects as the data parameter.",
            parameters: {
                data: {
                    type: "array",
                    description:
                        'Array of objects, each with: challanId (string), offence (string), amount (number in Rs), date (string YYYY-MM-DD). ' +
                        'Example: [{"challanId":"DL123456","offence":"Red Light Violation","amount":500,"date":"2024-06-15"}]',
                },
            },
            endpoint: "/api/internal/challans/save",
            method: "POST",
        },
        {
            name: "save_discounts",
            description:
                "Save discount/settlement amounts from Virtual Courts. Call this after extracting ALL discount data. " +
                "Pass a JSON array of discount objects as the data parameter.",
            parameters: {
                data: {
                    type: "array",
                    description:
                        'Array of discount objects, each with: challanId (string), discountAmount (number in Rs), originalAmount (number in Rs). ' +
                        'Example: [{"challanId":"DL123456","discountAmount":250,"originalAmount":500}]',
                },
            },
            endpoint: "/api/internal/discounts/save",
            method: "POST",
        },
    ],
    buildPrompt: (p) => {
        const hasMobileChange =
            p.mobileNumber && p.chassisLastFour && p.engineLastFour;

        // ──────────────────────────────────────────────
        // PHASE 0 block — only when mobile change needed
        // ──────────────────────────────────────────────
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

Remember: After mobile change, if the old OTP dialog reappears, a fresh OTP was sent to ${p.mobileNumber}. Enter that OTP and submit.
`
            : "";

        // ──────────────────────────────────────────────
        // OTP handling — adapts based on mobile change
        // ──────────────────────────────────────────────
        const otpBlock = hasMobileChange
            ? `When the site asks for OTP:
- If you have NOT yet changed the mobile number → follow PHASE 0 first.
- If you ALREADY changed the mobile number → call wait_for_human with reason: "OTP sent to ${p.mobileNumber}. Please enter it and click submit, then reply done."
- After human responds, continue extracting results.`
            : `When the site asks for OTP:
- Call wait_for_human with reason: "OTP required on Delhi Traffic Police. Please enter the OTP, click submit, then reply done."
- After human responds, continue extracting results.`;

        // ──────────────────────────────────────────────
        // STATE abbreviation for Virtual Courts department
        // ──────────────────────────────────────────────
        const stateMapping = `Pick department based on vehicle number prefix:
  - Delhi NCR (DL01 to DL14, HR(13, 26, 51, 70), and  UP(14, 16)) → "Delhi(Traffic Department)"
  - Other → matching Traffic/Transport department for that state`;

        return `
You are automating challan extraction for vehicle ${p.vehicleNumber} across 2 websites.
${hasMobileChange ? `Target mobile for OTP: ${p.mobileNumber}` : ""}

===
YOUR TOOLS
===
- wait_for_human → Call this when you need human help (OTP, CAPTCHA). It pauses and returns the human's response. After it returns, CONTINUE the workflow.
- save_challans → Call after extracting ALL challans from Delhi Traffic Police.
- save_discounts → Call after extracting ALL discount records from Virtual Courts.

===
RULES (read once, follow always)
===
1. Do NOT use the "done" action until ALL phases are complete.
2. Use a separate tab for each website. Never close a tab mid-workflow.
3. Read data visually from the screen. Never use JavaScript evaluate() to scrape.
4. Scroll through ALL results on every page. Check for pagination or "next" buttons.
5. Track your progress in memory: count how many records you have extracted vs how many exist.
${mobileChangeBlock}
===
PHASE 1 — DELHI TRAFFIC POLICE (extract challans)
===
1. Open a new tab → https://traffic.delhipolice.gov.in/notice/pay-notice/
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Click "Search Details".

${otpBlock}

4. Once results are visible, extract EVERY challan row:
   - Challan ID (full number)
   - Offence description
   - Fine amount (number in Rs)
   - Date (YYYY-MM-DD)

5. Scroll down to check for more rows or pagination. Do NOT stop until you have captured every row.
6. Skip any row where amount is 0 or missing.
7. If zero challans exist, note "0 challans found" and move to PHASE 2.

8. Call save_challans with ALL extracted data as a JSON array.
   Example: [{"challanId":"DL19016240430095546","offence":"Red Light Violation","amount":500,"date":"2024-06-15"}]

===
PHASE 2 — VIRTUAL COURTS (extract discounts)
===
STEP A — Select department:
1. Open a NEW tab → https://vcourts.gov.in/virtualcourt/index.php
2. In the "Select Department" dropdown:
   ${stateMapping}
3. Click "Proceed Now".

STEP B — Search by vehicle number:
1. On the next page, click the tab button labeled "Challan/Vehicle No."
2. Type "${p.vehicleNumber}" in the "Vehicle Number" field.
3. Read the CAPTCHA image and type the answer in "Enter Captcha".
4. Click "Submit".
5. If CAPTCHA fails, re-read and retry (up to 5 attempts).
6. After 5 failures, call wait_for_human: "CAPTCHA on Virtual Courts needs solving. Please solve it, click submit, then reply done."

STEP C — Extract results:

IMPORTANT: Before re-submitting CAPTCHA, FIRST scroll down and check if results are already visible. If you see "No. of Records" with a number >= 1 and a data table, the data is already loaded. Do NOT re-submit. Go straight to reading the table.

The table has columns: Sr.No., Offence Details, View.

For EACH row (track count: row X of total N):
1. Click "View" to expand the record.
2. Read "Challan No." → this is your challanId.
3. In the expanded section, read the "Fine" column → this is originalAmount.
4. Below it, read "Proposed Fine" → this is discountAmount.
5. Include every record, even if Fine equals Proposed Fine.

Scroll through the entire page to confirm you have read every row.
If "No. of Records :- 0", skip to COMPLETION.

STEP D — Save:
Call save_discounts with ALL records as a JSON array.
Example: [{"challanId":"DL19016240430095546","discountAmount":300,"originalAmount":500}]

===
COMPLETION
===
Only NOW call the "done" action. Report a summary:
${hasMobileChange ? "- Mobile number change: success or failure" : ""}
- Challans found on Delhi Traffic Police: [count]
- Challans saved via save_challans: [count]
- Records found on Virtual Courts: [count]
- Records saved via save_discounts: [count]
`.trim();
    },
};

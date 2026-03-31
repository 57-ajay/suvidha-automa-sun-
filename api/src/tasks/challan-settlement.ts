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
                        'array of objects, each with: challanId (string), offence (string), amount (number in Rs), date (string YYYY-MM-DD). ' +
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
                        'array of discount objects, each with: challanId (string), discountAmount (number in Rs), originalAmount (number in Rs). ' +
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

        const mobileChangeBlock = hasMobileChange
            ? `
========================================
PHASE 0: CHANGE MOBILE NUMBER (before OTP)
========================================
After you enter the vehicle number and click "Search Details", an OTP dialog will appear.
DO NOT enter the OTP yet. Instead:

1. Click the "Change mobile Number" link inside the OTP dialog.
2. A "Change Mobile Number" form will appear with these fields:
   - "New Mobile Number" → type: ${p.mobileNumber}
   - "Confirm Mobile Number" → type: ${p.mobileNumber}
   - "Last Four digit of Chasis Number" (next to the partial chassis shown) → type: ${p.chassisLastFour}
   - "Last Four digit of Engine Number" (next to the partial engine shown) → type: ${p.engineLastFour}
3. Click the "Submit" button (green button) on this form.
4. A NEW OTP will now be sent to ${p.mobileNumber}.
5. Call wait_for_human with reason:
   "OTP required — sent to NEW mobile number ${p.mobileNumber}. Please enter the OTP in the browser and click submit, then send 'done'."
6. After the human responds, the mobile number is now changed. Continue to Phase 1 results extraction.

IMPORTANT: After the mobile number change + OTP submission, the site should show the challan results.
If it shows the original OTP dialog again (for the old number), the mobile change was successful —
a new OTP has been sent to ${p.mobileNumber}. Enter that OTP and submit.
`
            : "";

        const otpInstructions = hasMobileChange
            ? `If the site asks for a mobile number or OTP and you have NOT yet changed the mobile number:
- Follow PHASE 0 above to change the mobile number first.
If you have ALREADY changed the mobile number and an OTP dialog appears:
- Call wait_for_human with reason "OTP required on Delhi Traffic Police — sent to ${p.mobileNumber}. Please enter the OTP in the browser and click submit, then send 'done'."
- After the human responds, continue from the results page.`
            : `If the site asks for a mobile number or OTP:
- Call wait_for_human with reason "OTP required on Delhi Traffic Police site. Please enter the OTP in the browser and click submit, then send 'done' via the intervene API."
- After the human responds, continue from the results page.`;

        return `
You are automating a challan extraction workflow across 2 websites.

IMPORTANT RULES:
- You have a tool called "wait_for_human". When you need human help (OTP, CAPTCHA, etc), call this tool with a reason. Do NOT use the "done" action to report that you need help. The wait_for_human tool will pause and return the human's response. After it returns, CONTINUE the workflow from where you left off.
- You have tools "save_challans" and "save_discounts" to save extracted data. Use them as described below.
- Do NOT end the task until ALL phases are complete.
- Use separate browser tabs for each site. Never close a tab until the entire workflow is done.

VEHICLE: ${p.vehicleNumber}
${hasMobileChange ? `TARGET MOBILE: ${p.mobileNumber}` : ""}
${mobileChangeBlock}
========================================
PHASE 1: DELHI TRAFFIC POLICE — Extract Challans
========================================
Open a tab and go to: https://traffic.delhipolice.gov.in/notice/pay-notice/

- Type ${p.vehicleNumber} in the "Vehicle Number" field
- Click "Search Details"

${otpInstructions}

After results load, extract EVERY challan:
- Challan ID (full number)
- Offence description
- Fine amount in Rs
- Date

RULES:
- Scroll through ALL results, check for pagination
- Skip any challan with no amount or amount = 0
- If zero challans found, skip to Phase 2

Once you have ALL challans, call the "save_challans" tool with the data as a JSON array.
Example: [{"challanId":"DL123456","offence":"Red Light Violation","amount":500,"date":"2024-06-15"}]

========================================
PHASE 2: VIRTUAL COURTS — Extract Discounts
========================================
Open a NEW TAB and go to: https://vcourts.gov.in/virtualcourt/index.php

Step 1 — Select department:
- You will see a "Select Department" dropdown and a "Proceed Now" button.
- Open the dropdown and select the appropiate department for vehicle: ${p.vehicleNumber}.
 - Example:
  - DL vehicles --> "Delhi(Notice Department)"
  - HR vehicles --> "Haryana(Traffic Department)"
  - UP vehicles --> look for the matching UP department
- Click "Proceed Now".

Step 2 — Navigate to vehicle search:
- You will land on a page with 4 tab buttons on the LEFT side arranged in a 2x2 grid:
  "Mobile Number", "CNR Number", "Party Name", "Challan/Vehicle No."
- Click the "Challan/Vehicle No." tab button (bottom-right of the grid).
- The right side will now show a form with: Challan Number field, Vehicle Number field, a CAPTCHA image, an "Enter Captcha" field, and a "Submit" button.

Step 3 — Fill form and submit:
- Type ${p.vehicleNumber} in the "Vehicle Number" field.
- Read the CAPTCHA image and type it in the "Enter Captcha" field.
- Click "Submit".
- If CAPTCHA was wrong, the page reloads with a new CAPTCHA. Try again (up to 5 attempts).
- If you cannot solve it after 5 attempts, call 'wait_for_human' with reason:
  "CAPTCHA needs solving on Virtual Courts. Please solve it and click submit, then send 'done'."

Step 4 — Extract records:
- After successful submit, you will see "No. of Records :- N" (N is number of records) and a table below.
- If records are already visible, do NOT touch the CAPTCHA again — ignore it.
- Each record has a summary row with Case No., Challan No., Party Name, and a "View" link.
- For EVERY record, extract:
  - challanId: the Challan No. from the summary row (e.g. "57113282")
  - originalAmount: the Fine value from the inner table
  - discountAmount: the Proposed Fine value
- Scroll down to check ALL records.
Example:
[{"challanId":"DL123456","discountAmount":250,"originalAmount":500}, ...]
Step 5 — Save:
- Call "save_discounts" with ALL extracted records as an array.
- Even if Fine == Proposed Fine (no discount), still include that record.
- If there are zero records, skip this step.

========================================
COMPLETION
========================================

Only NOW use the "done" action. Report:
${hasMobileChange ? "- Whether the mobile number was changed successfully" : ""}
- How many challans were found on Delhi Traffic Police
- How many were saved via save_challans
- How many records were found on Virtual Courts
- How many were saved via save_discounts (with their Proposed Fine and Fine amounts)
`.trim();
    },
};

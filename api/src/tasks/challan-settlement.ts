import type { Task } from "./types";

export const challanSettlement: Task = {
    id: "challan-settlement",
    name: "Challan Settlement Automation",
    requiredParams: ["vehicleNumber"],
    tools: [
        {
            name: "save_challans",
            description:
                "Save extracted challans to the database. Call this after extracting ALL challans from Delhi Traffic Police. " +
                "Pass a JSON array of challan objects as the data parameter.",
            parameters: {
                data: {
                    type: "string",
                    description:
                        'JSON array of objects, each with: challanId (string), offence (string), amount (number in Rs), date (string YYYY-MM-DD). ' +
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
                    type: "string",
                    description:
                        'JSON array of objects, each with: challanId (string), discountAmount (number in Rs), originalAmount (number in Rs). ' +
                        'Example: [{"challanId":"DL123456","discountAmount":250,"originalAmount":500}]',
                },
            },
            endpoint: "/api/internal/discounts/save",
            method: "POST",
        },
    ],
    buildPrompt: (p) => `
You are automating a challan extraction workflow across 2 websites.

IMPORTANT RULES:
- You have a tool called "wait_for_human". When you need human help (OTP, CAPTCHA, etc), call this tool with a reason. Do NOT use the "done" action to report that you need help. The wait_for_human tool will pause and return the human's response. After it returns, CONTINUE the workflow from where you left off.
- You have tools "save_challans" and "save_discounts" to save extracted data. Use them as described below.
- Do NOT end the task until ALL phases are complete.
- Use separate browser tabs for each site. Never close a tab until the entire workflow is done.

VEHICLE: ${p.vehicleNumber}

========================================
PHASE 1: DELHI TRAFFIC POLICE — Extract Challans
========================================
Open a tab and go to: https://traffic.delhipolice.gov.in/notice/pay-notice/

- Type ${p.vehicleNumber} in the "Vehicle Number" field
- Click "Search Details"

If the site asks for a mobile number or OTP:
- Call wait_for_human with reason "OTP required on Delhi Traffic Police site. Please enter the OTP in the browser and click submit, then send 'done' via the intervene API."
- After the human responds, continue from the results page.

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

Step 2a — Navigate:
- Click the "Select Department" dropdown
- Select the appropriate department for ${p.vehicleNumber} region
  (Delhi/NCR vehicles like DL, HR, UP → select "Delhi Notice department")
- Click "Proceed Now"
- On the next page, click "Challan/Vehicle No." tab
- Enter vehicle number: ${p.vehicleNumber}

Step 2b — CAPTCHA & Records:

╔══════════════════════════════════════════════════════════════════╗
║  MANDATORY CHECK - RUN THIS *BEFORE* TOUCHING THE CAPTCHA        ║
║                                                                  ║
║  Look at the page RIGHT NOW. Is there a table or list of         ║
║  records already visible? Look for ANY of these signs:           ║
║    - A table with rows of data (challan numbers, amounts, etc)   ║
║    - Text like "no of records", "Offence Details", etc           ║
║    - Offence codes, fine amounts, or challan IDs on screen       ║
║                                                                  ║
║  -> If YES: Records are loaded. CAPTCHA is already solved.       ║
║    SKIP ALL CAPTCHA steps. Go DIRECTLY to Step 2c.               ║
║    DO NOT type anything in the CAPTCHA field.                    ║
║    DO NOT click any submit/search button related to CAPTCHA.     ║
║                                                                  ║
║  -> If NO: Proceed to attempt CAPTCHA below.                     ║
╚══════════════════════════════════════════════════════════════════╝

CAPTCHA attempts (ONLY if no records are visible yet):
  1. Try to read the CAPTCHA image and type the answer. Submit the form.
     captcha_attempt_count = 1

  2. After submitting, wait for the page to update. Then IMMEDIATELY run
     the MANDATORY CHECK above again:
     → Records visible? → CAPTCHA is done. Go to Step 2c. STOP all CAPTCHA work.
     → No records AND captcha_attempt_count < 2? → Try again, increment count.
     → No records AND captcha_attempt_count >= 2? → Call wait_for_human:
       "CAPTCHA needs solving on Virtual Courts. Please solve it in the browser
        and click submit, then send 'done' via intervene API."
       After human responds, go to Step 2c.

ABSOLUTE RULE: Once records/data rows appear on the page, you are FINISHED
with CAPTCHA forever. The CAPTCHA input field will still be visible on the
page — this is normal website behavior. IGNORE IT. Never interact with the
CAPTCHA field or submit button again after records appear. Your ONLY job now
is to extract the data from the records table.

Step 2c — Extract discount data:

THIS IS THE MOST IMPORTANT STEP. Do not skip it. Do not re-solve CAPTCHA instead of doing this.

Look at the records table on screen. For each record/row, extract:
- The challan number (look for columns like "Challan No", "Notice No", or similar)
- The compounding/settlement/payable amount (this is the discounted amount to pay)
- The original fine amount if shown

Read EVERY row. Scroll down if needed. Check for pagination.

If the table shows records but no discount/settlement amounts are visible for any row,
that means there are no discounts available. Skip the save_discounts call.

If discount amounts ARE found, you MUST call "save_discounts" with a JSON array.
Each object must have: challanId, discountAmount (the settlement/compounding amount), originalAmount.
Example: [{"challanId":"DL123456","discountAmount":250,"originalAmount":500}]

DO NOT proceed to completion without calling save_discounts if any discount data exists.
DO NOT go back to the CAPTCHA. DO NOT refresh the page. Extract what is on screen and save it.

========================================
COMPLETION
========================================
Only NOW use the "done" action. Report:
- How many challans were found on Delhi Traffic Police
- How many were saved via save_challans
- How many had discount amounts from Virtual Courts
- How many were saved via save_discounts
  `.trim(),
};

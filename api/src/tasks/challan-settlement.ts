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

Step 2b — CAPTCHA (READ THIS CAREFULLY):
There is a CAPTCHA image on this page. Follow this EXACT decision tree:

  1. FIRST, check: is there ALREADY a table or list of records visible on the page?
     Look for text like "no of records", a table with offence codes, challan numbers, or amounts.
     → If YES: CAPTCHA is already solved. SKIP to Step 2c. Do NOT attempt to solve it again.
     → If NO: continue to step 2.

  2. Try to read the CAPTCHA image and type the answer. Submit the form.
     - captcha_attempt_count = 1

  3. After submitting, wait for the page to update, then check AGAIN:
     Is there a table/list of records visible? (offence codes, challan numbers, amounts, "no of records")
     → If YES: CAPTCHA is solved. SKIP to Step 2c immediately. Do NOT touch the CAPTCHA again.
     → If NO and captcha_attempt_count < 2: Go back to step 2 (try again). Increment captcha_attempt_count.
     → If NO and captcha_attempt_count >= 2: Call wait_for_human with reason:
       "CAPTCHA needs solving on Virtual Courts. Please solve it in the browser and click submit, then send 'done' via intervene API."
       After human responds, continue to Step 2c.

CRITICAL: Once records are visible on screen, the CAPTCHA is DONE. Do not re-solve it.
The CAPTCHA input field may still be visible on the page even after records load — IGNORE IT.

Step 2c — Extract discount data:
Look at the records table. For each record/row, extract:
- The challan number (look for columns like "Challan No", "Notice No", or similar)
- The compounding/settlement/payable amount (this is the discounted amount to pay)
- The original fine amount if shown

If the table shows records but no discount/settlement amounts are visible for any row,
that means there are no discounts available. Skip the save_discounts call.

If discount amounts ARE found, call "save_discounts" with a JSON array.
Each object must have: challanId, discountAmount (the settlement/compounding amount), originalAmount.
Example: [{"challanId":"DL123456","discountAmount":250,"originalAmount":500}]

IMPORTANT: You MUST call save_discounts if any discount amounts are visible. Do not skip it
based on assumptions — extract and save whatever data is on screen.

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

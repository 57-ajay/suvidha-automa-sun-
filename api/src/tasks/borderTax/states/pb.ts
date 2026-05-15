import { normalizeISODate, dateParts } from "../shared/dates";

export const buildPrompt = async (p: Record<string, string>): Promise<string> => {
    const vehicleNumber = p.vehicleNumber;
    const taxMode = p.taxMode || "DAYS";   // PB supports DAYS and QUARTERLY only
    const taxFrom = p.taxFrom;
    const taxUpto = p.taxUpto;

    const taxFromISO = normalizeISODate(taxFrom!);
    const taxUptoISO = normalizeISODate(taxUpto!);
    const tf = dateParts(taxFromISO);
    const tu = dateParts(taxUptoISO);

    const tfDtLocal = `${tf.iso}T00:00`;
    const tuDtLocal = `${tu.iso}T00:00`;

    const entryDistrict = p.entryDistrict || "MOHALI";
    // Punjab's checkpost names are often compound and don't match the
    // district name (e.g. district MOHALI -> checkpost KHARAR). When
    // the param isn't usable, the AI must fall back to the first available
    // option in the dropdown; we set "" here to signal that.
    const entryCheckpoint = p.entryCheckpoint || "";
    const serviceType = p.serviceType || "NOT APPLICABLE";
    const permitType = p.permitType || "NOT APPLICABLE";

    const sbiUserId = /*p.sbiUserId || */ "89Rahulxyz";
    const sbiPassword = /*p.sbiPassword ||*/ "rahul@70007";
    const paymentMethod = (p.paymentMethod || "net_banking").toLowerCase();
    const isUPI = paymentMethod === "upi";

    const toolDesc = isUPI
        ? `- wait_for_human → Call ONLY when explicitly told to in the steps below (CAPTCHA, UPI payment confirmation).
- save_qr_code → Call ONCE when the UPI QR code page is fully visible, BEFORE calling wait_for_human. Takes no parameters: save_qr_code({}). Non-blocking: if it returns ok:false log the error and still proceed to wait_for_human.`
        : `- wait_for_human → Call ONLY when explicitly told to in the steps below (CAPTCHA, OTP for net banking payment).`;

    const paymentAbort = isUPI
        ? `- UPI payment not completed within timeout → ABORT. Reason: "UPI payment timed out or was cancelled."`
        : `- SBI Net Banking login fails (invalid credentials, account locked) → ABORT. Reason: "SBI Net Banking login failed: [exact error]"`;

    // ── Page descriptions for the SBIePay → bank phase. Identical to UP/HR
    // except the header text on the success page mentions Punjab.
    const paymentPageDescriptions = isUPI
        ? `
PAGE: SBI ePay Lite — Payment Method Selection
VISUAL: "Welcome to SBIePay Lite (formerly SBMOPS)" header. Below a hero banner with best practices, four sections:
  "Net Banking" (SBI Net Banking, Other Bank Net Banking),
  "Card Payments" (State Bank Debit Cards, Other Bank Debit Cards, Credit Cards),
  "Other Payment Modes" (UPI),
  "Wallet Payment" (Wallet).
  Each option shows a name, bank charges, and a circular arrow ">" button. "Cancel" button at the bottom.
AVAILABLE ACTIONS: Click "UPI" under "Other Payment Modes".

PAGE: SBI ePay Lite — Payment Details (UPI Confirmation)
VISUAL: "Punjab Transport Department" / "Cyber Treasury" header. "Payment Details" section showing:
  Registration No, various receipt fields, Postal Amount, Transaction ID, Total Amount,
  Amount in words, Commission Amount (including GST). Timer "Complete transaction within
  next X:XX mins" in top-right. Two buttons: yellow "CONFIRM" and grey "RESET".
AVAILABLE ACTIONS: Click yellow "CONFIRM" button.

PAGE: SBI ePay Lite — Remittance Information (QR Code)
VISUAL: "Remittance Information" header.
  "Complete transaction within next X:XX mins" timer in top-right.
  "What to do next?" section with instructions to open your bank or UPI app.
  Centered QR code image.
  "Remittance Information Form" with: SBI Reference number, Merchant Reference No,
  Amount to be Remitted (in red), Transaction Status, Cancel link.
AVAILABLE ACTIONS: Call save_qr_code({}) (non-blocking), then call wait_for_human with the UPI payment reason.
`
        : `
PAGE: SBI Net Banking — Login (after IFMS bank-select for SBI BANK)
VISUAL: Two tabs at top: "Personal Banking" (blue, active by default) and "Corporate Banking / yono BUSINESS" (grey).
  Below: "Username & Password are case sensitive" warning. "User ID *" field, "Password" field,
  "LOGIN" button (blue), "RESET" button (grey). Virtual Keyboard grid below.
AVAILABLE ACTIONS: Click "Corporate Banking / yono BUSINESS" tab, type User ID, type Password, click "LOGIN".

PAGE: SBI Net Banking — Account Selection & Payment Details
VISUAL: "Welcome, [Name]" in top-right with logout icon. Transaction-detail header.
  Instruction text about selecting account. Blue table header: "Account No. / Nick name", "Account Type", "Branch".
  One or more account rows with radio buttons (first pre-selected). "Selected Account" row below showing chosen account number.
  "Payment Detail" section (red header) showing: Registration No, Receipts of All Types of Fees,
  Receipts of Fine Amount, Receipts of State Road Taxes, Penalty Amount State Road Taxes,
  Receipts against selling of forms, Postal Amount, Transaction ID, Amount in word,
  Commission Amount (including GST). Two buttons: yellow "CONFIRM" and grey "RESET".
AVAILABLE ACTIONS: Verify account is selected, click yellow "CONFIRM".

PAGE: SBI Net Banking — OTP / High Security Password
VISUAL: "Verify and confirm Punjab Transport Department transaction details" header at top.
  Details of last three transactions performed today (table with Reference No., Account No., Branch, Date, Amount, Status).
  "Debit Account Details" section with account info and payment breakdown.
  At the bottom: "Please use CONFIRM button to proceed after entering OTP in this page" instruction.
  "Enter high security transaction password received in your mobile phone 91-9*****XXX" text.
  "Enter High Security Password *" input field (highlighted in yellow).
  "click here to resend the SMS" link. Two buttons: yellow "CONFIRM" and grey "BACK".
AVAILABLE ACTIONS: Type OTP into "Enter High Security Password" field, click yellow "CONFIRM".
`;

    // ── Phase 5 steps 4+ — SBIePay onwards (identical to UP/HR after the
    // IFMS bank-select hop).
    const paymentSteps = isUPI
        ? `
4. The SBI ePay Lite page loads (SBIePay / formerly SBMOPS).
   VERIFY: You see the payment method selection page with sections: "Net Banking", "Card Payments",
   "Other Payment Modes", and "Wallet Payment". Each option has a name, bank charges, and a ">" arrow button.
   - Under "Other Payment Modes", find "UPI" showing "Bank Charges(₹): 0.0".
   - Click the ">" arrow button next to "UPI".
   - Wait for the next page to load.

5. The Payment Details / UPI confirmation page loads.
   VERIFY: You see the Cyber Treasury / Punjab Transport Department header, "Payment Details" section with
   Registration No, various receipt amounts, Transaction ID, Total Amount, Amount in words, and
   Commission Amount. A timer "Complete transaction within next X:XX mins" shows in the top-right.
   You see a yellow "CONFIRM" button and a grey "RESET" button.
   - Click the yellow "CONFIRM" button.
   - Wait for the next page to load.

6. The Remittance Information page loads with a UPI QR code.
   VERIFY: You see "Remittance Information" header. The page shows:
   - "What to do next?" section with instructions to open your bank or UPI app.
   - A QR code centered on the page.
   - "Remittance Information Form" with SBI Reference number, Merchant Reference No,
     Amount to be Remitted (in red), Transaction Status, Cancel link.
   - A timer "Complete transaction within next X:XX mins" in the top-right.

   Now do EXACTLY this (no extra steps, no extra tool calls):
   a) Call save_qr_code({}). This is a single tool call with no arguments.
      - If it returns {"ok": true} → log it; proceed to (b).
      - If it returns {"ok": false} → log the error; still proceed to (b). save_qr_code is non-blocking.
   b) Call wait_for_human with this exact reason text (substituting in the live amount):
        "UPI payment of ₹<amount> required for border tax of vehicle ${vehicleNumber} entering Punjab.
        A QR code is displayed on screen — please scan it with your UPI app and complete the payment.
        After payment is successful, wait for the page to update automatically, then reply done."
   c) Do NOT interact with the page after calling wait_for_human. Just wait.

7. After the human confirms payment is done:
   - The page should transition away from the QR page automatically (within ~10 seconds).
   - If the page still shows the QR code 30 seconds after "done", wait one more time (max 30s) and re-check.
   - If the page shows "Transaction Failed", "Payment Timeout", or any error → ABORT. Reason: "Payment failed: [exact error from page]"
   - Once the QR page is gone, proceed to Phase 6 (receipt capture).
`
        : `
4. The SBIePay Lite page loads (SBIePay / formerly SBMOPS).
   VERIFY: You see the payment method selection page with sections: "Net Banking", "Card Payments",
   "Other Payment Modes", and "Wallet Payment".
   - Under "Net Banking", click "SBI Net Banking" (>" arrow).

5. The SBI Net Banking login page loads.
   VERIFY: Two tabs at the top — "Personal Banking" (blue) and "Corporate Banking / yono BUSINESS" (grey).
   Below: "Username & Password are case sensitive" warning, "User ID *" field with placeholder "Enter user ID",
   "Password" field, "LOGIN" button (blue), "RESET" button (grey), and a Virtual Keyboard grid below.
   - Click the "Corporate Banking / yono BUSINESS" tab.
     VERIFY: The tab becomes active/highlighted.
   - Click the "User ID" field and type: ${sbiUserId}
   - Click the "Password" field and type: ${sbiPassword}
   - Click the "LOGIN" button.
   - Wait for the next page to load (may take 10-15 seconds).

6. The Account Selection & Payment Details page loads.
   - Verify the account radio button is pre-selected; if not, select the first row.
   - Click the yellow "CONFIRM" button.

7. The OTP / High Security Password page loads.
   - Call wait_for_human with this exact reason: "SBI Net Banking OTP required for border tax of vehicle ${vehicleNumber} entering Punjab. Please enter the OTP sent to the registered mobile number on the screen, then reply with the OTP."
   - When the human replies, type their reply into the "Enter High Security Password" field.
   - Click the yellow "CONFIRM" button.
   - Wait for the transaction to complete (may take 30+ seconds).

8. If the transaction succeeds, proceed to Phase 6 (receipt capture).
   If the transaction fails → ABORT. Reason: "SBI Net Banking transaction failed: [exact error]".
`;


    return `
# BORDER TAX PAYMENT — PUNJAB

You are completing a border tax payment for vehicle ${vehicleNumber} entering PUNJAB.

===
TOOLS YOU CAN USE
===
${toolDesc}
- save_receipt → Call EXACTLY ONCE at the end after reading the receipt fields.

===
ABORT CONDITIONS (call done with status "failed")
===
- Vehicle details fail to load on the Owner Information page → ABORT. Reason: "Vehicle ${vehicleNumber} details could not be fetched: [exact error]".
- Vehicle Information page shows an insurance/fitness/PUCC validity error popup → click OK → ABORT. Reason: "Vehicle ${vehicleNumber} has no valid insurance/fitness/PUCC. Please renew before attempting border tax payment."
- Captcha cannot be solved after 3 retries → ABORT. Reason: "Captcha could not be solved after 3 attempts."
- Payment Gateway page shows no "IFMS" option in the gateway dropdown → ABORT. Reason: "Punjab payment gateway does not offer IFMS aggregator; manual intervention required."
- IFMS bank-selection page does not appear within 30 seconds after Submit → ABORT. Reason: "IFMS bank-selection page did not load."
${paymentAbort}

===
PARTIAL CONDITIONS (status "partial" — payment succeeded, receipt did not)
===
- Receipt page does not load within 60 seconds after payment confirmation → call done with Status: partial.
  The money has already been deducted; this is NOT a failure of the payment itself. See Phase 6 for the exact partial-completion summary template.
- save_receipt tool returns "ok": false → call done with Status: partial. Include the tool's error message in the summary.

===
WHAT EACH PAGE LOOKS LIKE (memorize these)
===

PAGE: PARIVAHAN — Checkpost Tax Selection
URL: https://parivahan.gov.in/en/node/579
VISUAL: A government page with "Checkpost Tax" dropdown showing "--- Select State Name ---" as placeholder.
AVAILABLE ACTIONS: Click dropdown, select "PUNJAB".

PAGE: CHECKPOST PORTAL — Service Selection
URL: services.parivahan.gov.in/checkpostv4/
VISUAL: "Online Chekpost Portal" page with "Service Name" dropdown and a blue ">> Go" button.
  "Select Visiting State Name" shows "PUNJAB".
AVAILABLE ACTIONS: Select "VEHICLE TAX COLLECTION (OTHER STATE)" from dropdown, click "Go".

PAGE: CHECKPOST PORTAL — Vehicle Entry / Owner Information (Step 1 of 4)
VISUAL: Heading "Border Tax Payment for Entry Into PUNJAB". "Owner Information: <vehicleNumber>" subheading.
  "Tax Pay for Temporary Vehicle" checkbox (leave unchecked). "Input Vehicle Number" text field, "Get Details" button, "Reset All" button.
  After "Get Details" succeeds: Chassis No., Owner Name, Mobile No., From State, "Entry District Name" and "Entry CheckPost Name" dropdowns appear.
  "Next" button at the bottom-right.
AVAILABLE ACTIONS: Type vehicle number, click "Get Details", select district, select checkpost, click "Next".

PAGE: CHECKPOST PORTAL — Vehicle Information (Step 2 of 4)
VISUAL: Vehicle info fields (Vehicle Type, Vehicle Class, Vehicle Category, Permit Type, Seating Capacity, Sleeper Capacity,
  Service Type, Permit Validity, Permit Authorization Validity). "Vehicle Category" / "Permit Type" / "Service Type"
  are dropdowns the agent must set IF EMPTY. Other fields are typically pre-filled from RC data.
  "Previous" and "Next" buttons at the bottom.
POSSIBLE POPUP: A red error popup reading "No valid insurance detected..." / "Fitness expired..." / "PUCC expired..."
  with an "OK" button may appear. This is BLOCKING — close the popup and ABORT.
AVAILABLE ACTIONS: Set Vehicle Category if empty, set Permit Type if empty, set Service Type if empty, click "Next". If validity popup appears → click "OK" → ABORT.

PAGE: CHECKPOST PORTAL — Tax Information (Step 3 of 4)
VISUAL: "Tax Mode" dropdown (options: DAYS, QUARTERLY), "Tax From" and "Tax Upto" datetime-local fields,
  "Calculate Fee/Tax" button, "Next" button.
  Both date fields are NATIVE HTML5 DATETIME-LOCAL inputs. They display "mm/dd/yyyy ____ __:__ __" as
  placeholder (browser locale display) when empty. The DOM value they accept is "YYYY-MM-DDTHH:MM"
  (literal "T"). After Calculate Fee/Tax, a table of MV Tax / Service-User Charge / Cess rows appears and
  a "Total Amount" field below it gets populated.
AVAILABLE ACTIONS: Select Tax Mode, type Tax From, type Tax Upto, click Calculate Fee/Tax, click Next.

PAGE: CHECKPOST PORTAL — Disclaimer (Step 4 of 4)
VISUAL: A summary of all the vehicle/tax information entered. "I confirm that above information are correct
  as per my knowledge" checkbox with red label text. A canvas captcha image with distorted characters and
  a blue refresh button. A captcha text input field. A read-only Total Amount on the right.
  "Previous" and green "Pay Online" buttons at the bottom-right.
POSSIBLE POPUPS:
  - After ticking the checkbox: a popup showing "Vehicle Number / Tax From / Tax Upto / Tax Mode / Total Amount /
    Receipt valid for X Days". It has a "Close" button.
  - After clicking "Pay Online": a confirmation popup "Are you sure? You want to pay online ?" with green "Yes"
    and grey "Cancel" buttons.
AVAILABLE ACTIONS: Solve captcha, tick checkbox, close info popup, click Pay Online, click Yes.

PAGE: PAYMENT GATEWAY (vahan.parivahan.gov.in/eTransPgi/)
VISUAL: Header reads "PAYMENT GATEWAY" with a session timer in the top-right.
  "PAYMENT DETAILS" section shows a Payment Id (starts with "PBP" for Punjab) and an Amount.
  Below: "Select Payment Gateway" dropdown (ONE option only: "IFMS") and an "I accept terms and conditions" checkbox.
  A blue "Submit" button at the bottom.
AVAILABLE ACTIONS: Select "IFMS" in the Payment Gateway dropdown, tick the terms checkbox, click "Submit".

PAGE: IFMS — Bank Selection (Punjab IFMS intermediate)
VISUAL: A simple page with a "Select Bank" dropdown listing three options:
  - "PNB Aggregate" (value 1300999)
  - "SBI BANK" (value 1001509)
  - "UNION BANK OF INDIA" (value 1600777)
  A "Continue" submit button next to or below the dropdown.
  NO captcha, NO popups, NO terms checkbox on this page.
AVAILABLE ACTIONS: Select "SBI BANK" from the dropdown, click "Continue".

${paymentPageDescriptions}
PAGE: PUNJAB TRANSPORT DEPARTMENT — Receipt (Checkpost Tax e-Receipt)
URL: usually under services.parivahan.gov.in/checkpostv4/
VISUAL: Two buttons at the very top: a blue "Back" button and a blue "Print" button.
  Below them, a printed-style receipt with these features:
  - A faint diagonal watermark of "<vehicleNumber> <date> <time>" repeating across the page.
  - Top-left: a Punjab Government emblem.
  - Top-center: heading "GOVERNMENT OF PUNJAB", subheading "Department of Transport",
    sub-subheading "Checkpost Tax e-Receipt".
  - Top-right: a QR code, with "Printed on : <date> <time>" above it.
  - Two-column body of fields:
    Left column likely includes: Registration No., Payment Initialization Date, Chassis No., Vehicle Type,
    Vehicle Category, CheckPost Name, Sleeper Cap, Payment Mode, Permit Validity, Insurance Validity,
    Service Type, Payment Confirmation Date.
    Right column likely includes: Receipt No., Owner Name, Tax Mode, Vehicle Class, Mobile No.,
    Seating Capacity, Bank Ref. No., Permit Number, Fitness Validity, PUCC Validity, Permit Type.
  - A summary table near the bottom with columns: Tax/Fee Particular, Tax/Fees, Fine, Total
    (rows: MV Tax, Service/User Charge, Cess).
  - "Grand Total : <amount>/- <amount in words>" line.
  - Terms and Conditions block.
  - Bottom: "Scan the QR code for genuinity of the receipt."
AVAILABLE ACTIONS: Read the receipt fields. DO NOT click "Print". DO NOT click "Back". Call save_receipt.

===
PHASE 1 — NAVIGATE TO CHECKPOST PORTAL
===
1. Go to https://parivahan.gov.in/en/node/579
2. The page has the 'Checkpost Tax' dropdown with placeholder '--- Select State Name ---'.
3. Click into the dropdown and scroll. The list is alphabetical.
4. Select "PUNJAB" from the state dropdown.
5. This navigates to the Online Chekpost Portal page (services.parivahan.gov.in/checkpostv4/).
   The "Select Visiting State Name" field should now show "PUNJAB".

===
PHASE 2 — SELECT SERVICE AND ENTER VEHICLE
===
1. On the Online Chekpost Portal page, click the "Service Name" dropdown.
2. Select "VEHICLE TAX COLLECTION (OTHER STATE)".
3. Click the blue ">> Go" button.
4. A new page loads: "Border Tax Payment for Entry Into PUNJAB" with "Owner Information: <vehicleNumber>".
5. You should see "Input Vehicle Number" field. Type "${vehicleNumber}" in it.
6. Click the blue "Get Details" button.
7. Wait for the owner/vehicle details to appear (Chassis No., Owner Name, Mobile No., From State, etc.).
   - If an error appears or no details load → ABORT.

===
PHASE 3 — FILL ENTRY DETAILS
===
1. On the Owner Information page (Step 1 of 4), with details now populated:
   - In the "Entry District Name" dropdown, select "${entryDistrict}".
   - Wait 1-2 seconds for the "Entry CheckPost Name" dropdown's options to populate.${entryCheckpoint
            ? `
   - In the "Entry CheckPost Name" dropdown, select "${entryCheckpoint}".
   - If "${entryCheckpoint}" is NOT in the dropdown options, fall back to picking the FIRST non-placeholder option in the list.`
            : `
   - In the "Entry CheckPost Name" dropdown, pick the FIRST non-placeholder option in the list.
     (Punjab's checkpost names are compound and we don't have a confirmed entryCheckpoint param, so default to the first available checkpost for the district.)`}
   - Click the "Next" button at the bottom-right.

2. On the Vehicle Information page (Step 2 of 4):

   2a. VALIDITY CHECK (do this BEFORE selecting any dropdown):
       - As soon as the page loads, scan for a red error popup.
       - If a popup is visible mentioning "insurance" / "fitness" / "PUCC" being expired / invalid / missing:
         → Click "OK" to close the popup.
         → ABORT IMMEDIATELY. Reason: "Vehicle ${vehicleNumber} has no valid insurance/fitness/PUCC. Please renew before attempting border tax payment."

   2b. If no validity popup is visible, check the three dropdowns:
       - "Vehicle Category": if this dropdown is empty (shows "Select Vehicle Category..."), select the only option (typically "LIGHT PASSENGER VEHICLE"). If it is already filled (e.g. from RC data), LEAVE IT AS IS.
       - "Permit Type": if empty, select "${permitType}". If already filled, LEAVE IT AS IS.
       - "Service Type": if empty, select "${serviceType}". If already filled, LEAVE IT AS IS.
       - After EACH selection, wait ~1 second and rescan for a popup. If a validity popup appears at any point:
         → Click "OK" to close it.
         → ABORT. Same reason as 2a.

   2c. Once the required dropdowns are set and no popup is showing, click the "Next" button to proceed.
       (There is NO "Distance" field on Punjab — unlike Haryana.)

===
PHASE 4 — TAX CALCULATION
===
1. On the Tax Information page (Step 3 of 4):

   --- 1a. Tax Mode ---
   - Click the "Tax Mode" dropdown and select "${taxMode}".
   - Punjab supports DAYS and QUARTERLY ONLY. If the requested taxMode is anything else, ABORT.

   --- 1b. Tax From (target: ${tf.iso}, programmatic value: ${tfDtLocal}) ---

   The "Tax From" field is a native HTML5 datetime-local input. Concretely:
     - selector: #floatingTaxfrom   (id="floatingTaxfrom")
     - type="datetime-local"        (NOT type="date" — this distinction matters)
     - the placeholder you SEE in the empty field is "mm/dd/yyyy ____ __:__ __"
       (browser locale display), NOT "DD-MM-YYYY", regardless of what the page
       HTML's placeholder attribute says
     - the DOM value the browser ACCEPTS programmatically is the ISO 8601
       datetime-local form: "YYYY-MM-DDTHH:MM" with a literal "T".

   TARGET DOM VALUE → "${tfDtLocal}"
   (this is ${tf.monthName} ${tf.dd}, ${tf.year} at midnight)

   METHOD A — Direct datetime-local input (try this FIRST, simplest):
     i.   Use your standard input/type/fill action targeting selector
          #floatingTaxfrom with the EXACT value "${tfDtLocal}".
     ii.  After the action, the DISPLAYED date portion should read
          "${tf.mm}/${tf.dd}/${tf.year}" and the time portion "12:00 AM".

     CRITICAL — common mistakes to avoid:
       ✗  Do NOT type "${tf.iso}"          (date-only — silently rejected)
       ✗  Do NOT type "${tf.mmddyyyy}"     (slashed display form — rejected at value layer)
       ✗  Do NOT type "${tf.dd}-${tf.mm}-${tf.year}"  (DD-MM-YYYY — rejected)
       ✓  Do type   "${tfDtLocal}"        (datetime-local ISO with T — accepted)

   METHOD B — Calendar Picker (FALLBACK if Method A leaves the field empty):
     i.   Click the SMALL CALENDAR ICON at the RIGHT EDGE of the #floatingTaxfrom
          input field.
     ii.  A date-picker popup appears.
     iii. Navigate to "${tf.monthName} ${tf.year}" using the arrows.
     iv.  Click the day "${tf.dd}".

   --- 1c. Tax Upto (target: ${tu.iso}, programmatic value: ${tuDtLocal}) ---
   Same as 1b but for the "#uptpDate" input. Target DOM value: "${tuDtLocal}".

2. After BOTH dates are filled, click the "Calculate Fee/Tax" button.
3. Wait for the tax-table rows to appear (MV Tax, Service/User Charge, Cess) and the Total Amount field to populate.
4. Verify the amount is displayed (must be a number > 0). If the amount stays blank or 0 after 30 seconds → re-check both date fields.
5. Click the "Next" button.

===
PHASE 5 — PAYMENT
===
1. On the Disclaimer page (Step 4 of 4):
   - VERIFY: A summary of vehicle info, dates, and total amount. A captcha image on the bottom-left.
     A captcha input field. A red checkbox label "I confirm that above information are correct as per my knowledge".
     A green "Pay Online" button.
   - Read the captcha text from the canvas image and type it into the captcha input field (id="inputcap", max 8 chars, case-sensitive).
   - Tick the "I confirm..." checkbox.
   - If a popup appears with vehicle/tax summary and "Receipt valid for X Days", click its "Close" button.
   - Click the green "Pay Online" button.
   - A confirmation popup appears: "Are you sure? You want to pay online ?". Click the green "Yes" button.
   - Wait for navigation to the Payment Gateway page (vahan.parivahan.gov.in/eTransPgi/...).
   - If the captcha is rejected ("Invalid Captcha" popup), close the popup, click the blue refresh button next to the captcha image, and retry with the new image. Max 3 retries before aborting.

2. On the Payment Gateway page (vahan.parivahan.gov.in/eTransPgi):
   - VERIFY: Header reads "PAYMENT GATEWAY" with a session timer in the top-right.
   - VERIFY: "PAYMENT DETAILS" section shows a Payment Id (starts with "PBP") and an Amount.
   - Click the "Select Payment Gateway" dropdown.
   - Select "IFMS" (the only option).
   - Tick the "I accept terms and conditions." checkbox.
   - Click the blue "Submit" button.
   - Wait for navigation to the IFMS bank-selection page.

3. On the IFMS Bank-Selection page:
   - VERIFY: A "Select Bank" dropdown listing "PNB Aggregate", "SBI BANK", "UNION BANK OF INDIA".
   - VERIFY: A "Continue" submit button.
   - There is NO captcha, NO popup, NO terms checkbox on this page.
   - Click the "Select Bank" dropdown.
   - Select "SBI BANK".
   - Click the "Continue" button.
   - Wait for redirect to SBIePay Lite (merchant.onlinesbi.sbi or merchant.sbi.bank.in — the URL host contains "sbi").

${paymentSteps}
===
PHASE 6 — WAIT FOR RECEIPT AND CAPTURE IT
===
GOAL: After payment completes, the browser will (a) show SBI's "Your payment was successful" page,
then (b) auto-redirect to the Punjab Transport Department receipt page. Your job is to wait for the
receipt to appear and call save_receipt EXACTLY ONCE. Do NOT click Print, do NOT click Back,
do NOT click "Click here". Just wait, verify, and call the tool.

--- STEP 1: Check for SBI Payment Success page (check first, poll only if needed) ---
After the payment was confirmed, ONE of three things will be on screen:
  (a) The SBI Payment Success page.
  (b) The Punjab Transport Department receipt page (success page flashed by, auto-redirect already happened).
  (c) Neither yet — the page is still loading/transitioning.

The SBI Payment Success page is identified by ALL of these on screen:
  - A green checkmark icon
  - The text "Your payment was successful"
  - An "Account Details" section with Status: "Completed Successfully"
  - A line "Click here to return to the Punjab Transport Department site. Else, you will be
    automatically redirected to the Punjab Transport Department site in 10 seconds."

CHECK-FIRST POLLING:
  1. Inspect the page RIGHT NOW.
     - If the receipt page is already visible (the six-marker check from STEP 2 passes) →
       SKIP STEP 1 entirely and jump to STEP 3.
     - If the SBI success page markers above are visible → success page is showing.
       Proceed to STEP 2 (do NOT click "Click here"; the auto-redirect is the only correct mechanism).
     - If NEITHER is visible → wait 5 seconds, then re-check.
  2. Repeat the 5-second wait + re-check up to 6 times (≈30 seconds total).
  3. If after 30 seconds neither page is visible → STEP 5 (partial completion).

--- STEP 2: Wait for receipt auto-redirect ---
The Punjab Transport Department receipt page is identified by ALL of these (six markers):
  - "GOVERNMENT OF PUNJAB" heading
  - "Department of Transport" subheading
  - "Checkpost Tax e-Receipt" sub-subheading
  - Registration No. matching "${vehicleNumber}"
  - Receipt No. visible
  - "Grand Total : <amount>/-" line

Poll every 5 seconds for up to 60 seconds. If the receipt page never appears → STEP 5 (partial completion).

--- STEP 3: Read receipt details ---
Read the following values directly from the rendered page (do not click anything):
  - Receipt No. (top-right area) → receiptNumber
  - Registration No. (must equal "${vehicleNumber}") → confirm match. If it doesn't match, log a warning in your final summary but continue.
  - Grand Total amount, e.g. from "Grand Total : 240/- Two Hundred Forty Rupees Only" → amount (just the number)
  - Payment Confirmation Date, e.g. "15-May-2026, 12:11:21 PM" → paymentDate (convert to YYYY-MM-DD)

If any of receiptNumber / amount / paymentDate cannot be read clearly → STEP 5.

--- STEP 4: Capture and save the receipt ---
Call save_receipt EXACTLY ONCE with this payload:
  {"vehicleNumber":"${vehicleNumber}","receiptNumber":"<receiptNumber>","amount":<amount>,"paymentDate":"<YYYY-MM-DD>"}

The save_receipt tool will:
  - Capture the currently visible receipt page as a PDF.
  - Upload the PDF to cloud storage.
  - Persist the receipt metadata in the database.

WAIT for the response. Read it carefully.
  - "ok": true AND "pdfUploaded": true → SUCCESS. Proceed to COMPLETION (full done).
  - "ok": true AND "pdfUploaded": false → STEP 5 (partial completion); include receiptNumber in the summary.
  - "ok": false → STEP 5 (partial completion). Include the tool's error message. DO NOT retry.

DO NOT call save_receipt more than once. DO NOT click "Print" or "Back".

--- STEP 5: PARTIAL COMPLETION ---
Call done with this exact summary template:

  Vehicle: ${vehicleNumber}
  State: Punjab
  Entry District: ${entryDistrict}
  Entry Checkpoint: ${entryCheckpoint || "<auto-selected>"}
  Permit Type: ${permitType}
  Service Type: ${serviceType}
  Tax Mode: ${taxMode}
  Tax Period: ${tf.iso} to ${tu.iso}
  Payment Method: ${isUPI ? "UPI" : "SBI Net Banking"}
  Amount Paid: ₹<amount if known, otherwise "unknown">
  Receipt Number: <receiptNumber if read, otherwise "unknown">
  Receipt PDF: not uploaded — <reason: "receipt page did not load within 60 seconds" / "save_receipt returned ok:false: <error>" / "could not read receipt fields">
  Status: partial

After writing this summary → call done. Do NOT retry. Do NOT call save_receipt again.

===
COMPLETION (full success)
===
Reach this section ONLY when save_receipt returned "ok": true AND "pdfUploaded": true.

Call done with this summary:
  Vehicle: ${vehicleNumber}
  State: Punjab
  Entry District: ${entryDistrict}
  Entry Checkpoint: ${entryCheckpoint || "<auto-selected>"}
  Permit Type: ${permitType}
  Service Type: ${serviceType}
  Tax Mode: ${taxMode}
  Tax Period: ${tf.iso} to ${tu.iso}
  Payment Method: ${isUPI ? "UPI" : "SBI Net Banking"}
  Amount Paid: ₹<amount>
  Receipt Number: <receiptNumber>
  Receipt PDF: uploaded
  Status: complete
`.trim();
};

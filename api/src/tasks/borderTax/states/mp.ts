import { normalizeISODate, dateParts } from "../shared/dates";

export const buildPrompt = async (p: Record<string, string>): Promise<string> => {
    const vehicleNumber = p.vehicleNumber;
    const taxMode = p.taxMode || "DAYS";   // MP supports DAYS only (per portal HTML)
    const taxFrom = p.taxFrom;
    const taxUpto = p.taxUpto;

    const taxFromISO = normalizeISODate(taxFrom!);
    const taxUptoISO = normalizeISODate(taxUpto!);
    const tf = dateParts(taxFromISO);
    const tu = dateParts(taxUptoISO);

    // MP defaults — see images/HTML for live values.
    // SHEOPUR is the first district in the list; KARAHAL is its first checkpost.
    // entryCheckpoint left "" lets the runner fall back to first non-empty option
    // (mirrors PB/RJ — MP's checkpost names are compact like KARAHAL/NAHAR/SAMRASA
    // and don't always match a sensible param).
    const entryDistrict = p.entryDistrict || "SHEOPUR";
    const entryCheckpoint = p.entryCheckpoint || "";
    const serviceType = p.serviceType || "Air Conditioned Service";
    // MP's Permit Type dropdown lists TEMPORARY PERMIT / ALL INDIA TOURIST PERMIT /
    // SEPECIAL PERMIT (note the live portal typo "SEPECIAL"). Per user spec:
    // default to TEMPORARY PERMIT for MP (others default to ALL INDIA TOURIST).
    const permitType = p.permitType || "TEMPORARY PERMIT";

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

    // ── Page descriptions for the SBIePay → UPI QR phase.
    //
    // IMPORTANT: MP's SBIePay variant is DIFFERENT from UP/HR/PB's "SBIePay Lite".
    // It lives at epay.sbi.bank.in and presents a sidebar of payment categories
    // (Debit/Credit Card, SBI Corporate Credit Cards, Banking Connect (Net Banking),
    // UPI, Wallets, NEFT, SBI Branch Payment). The flow is:
    //   1. Click "UPI" in the left sidebar.
    //   2. The right panel updates to show "Please select UPI payment option" with a
    //      "UPI QR" radio button.
    //   3. Click the "UPI QR" radio.
    //   4. A yellow "Pay Now" button appears below.
    //   5. Click "Pay Now" → navigates to the QR page.
    //
    // There is NO Cyber Treasury confirm (yellow CONFIRM) page on MP.
    const paymentPageDescriptions = isUPI
        ? `
PAGE: SBIePay — Payment Details (sidebar UI)
URL: epay.sbi.bank.in/secure/AggHostedGtwPostServlet (or similar epay.sbi.bank.in path)
VISUAL: Blue/teal "SBIePay" logo top-left. A red notice listing unavailable Net Banking channels.
  "Payment Details" section header. LEFT sidebar lists payment categories vertically:
    "Debit/Credit Card" (selected by default),
    "SBI Corporate Credit Cards",
    "Banking Connect (Net Banking)",
    "UPI" (with UPI/PhonePe/Paytm/WhatsApp icons),
    "Wallets",
    "NEFT",
    "SBI Branch Payment".
  RIGHT panel initially shows card entry fields (Card Number, Name of the card holder,
  Expiry Date / CVV, yellow "Pay Now" button, "Cancel" link).
  FAR-RIGHT "Order Summary" card showing Order No. (prefix "MPZ..."), Merchant Name
  "Transport Department Madhya Pradesh", Amount, Processing fee, GST, Total, APM ID.
AVAILABLE ACTIONS: Click "UPI" in the LEFT SIDEBAR.

PAGE: SBIePay — UPI Payment Option Selection
VISUAL: Same sidebar/Order Summary surrounding chrome as above; right panel now shows:
  "Please select UPI payment option" heading.
  A single radio option labeled "UPI QR" (input id="upiQR1", name="upiOpt").
  (The radio is initially un-selected.)
AVAILABLE ACTIONS: Click the "UPI QR" radio button.

PAGE: SBIePay — UPI QR Pay Now
VISUAL: Same chrome; after the "UPI QR" radio is selected, a yellow "Pay Now" button
  appears below it. The "Order Summary" panel may now show Processing fee, GST, and Total
  filled in (e.g. "733.00 INR").
AVAILABLE ACTIONS: Click the yellow "Pay Now" button (id="upiButton").

PAGE: SBIePay — Scan UPI QR
URL: epay.sbi.bank.in/secure/AggUPIQRProcessingServlet (or similar)
VISUAL: "SBIePay" logo at top-center. Heading "Scan UPI QR".
  A blue banner reads "Don't press the back button. Please wait... Your transaction is in process."
  A large QR code image (data:image/png;base64,... — embedded directly in <img src>).
  Below the QR: two blue square boxes showing remaining time "3 Minutes" / "40 Seconds"
  (countdown ids "countDownTimer" and "timerSecond"). Caption "Time left to complete the transaction".
  Instruction line "Scan the above QR code in any UPI app to make the payment.
  Please wait while your transaction is in process. Do not refresh or click back button."
  On mobile widths, a green "Pay by any UPI App" button is shown (with a upi:// URL).
AVAILABLE ACTIONS: Call save_qr_code({}) (non-blocking), then call wait_for_human with the UPI payment reason. Do NOT refresh, do NOT click back, do NOT click anything.
`
        : `
PAGE: SBI Net Banking — Login (after SBIePay → Banking Connect)
VISUAL: Same SBI corporate login UI used by UP/HR/PB. Two tabs at top: "Personal Banking"
  (blue, active by default) and "Corporate Banking / yono BUSINESS" (grey).
  "Username & Password are case sensitive" warning. "User ID *" field, "Password" field,
  "LOGIN" button (blue), "RESET" button (grey). Virtual Keyboard grid below.
AVAILABLE ACTIONS: Click "Corporate Banking / yono BUSINESS" tab, type User ID, type Password, click "LOGIN".

PAGE: SBI Net Banking — Account Selection & Payment Details
VISUAL: Transaction-detail header. Blue table header with account rows (first pre-selected).
  "Payment Detail" section (red header) showing Registration No, receipt fields, Transaction ID,
  Amount, Commission. Two buttons: yellow "CONFIRM" and grey "RESET".
AVAILABLE ACTIONS: Verify account is selected, click yellow "CONFIRM".

PAGE: SBI Net Banking — OTP / High Security Password
VISUAL: "Verify and confirm Madhya Pradesh Transport Department transaction details" header.
  Recent transactions table, Debit Account Details section, OTP input field highlighted yellow.
AVAILABLE ACTIONS: Type OTP into "Enter High Security Password" field, click yellow "CONFIRM".
`;

    // ── Phase 5 steps 4+ — SBIePay onwards.
    const paymentSteps = isUPI
        ? `
4. The SBIePay page loads (epay.sbi.bank.in).
   VERIFY: You see the "Payment Details" header with a LEFT SIDEBAR listing payment categories
   (Debit/Credit Card, SBI Corporate Credit Cards, Banking Connect (Net Banking), UPI, Wallets,
   NEFT, SBI Branch Payment). The "Debit/Credit Card" panel is selected by default and shows
   card entry fields on the right.
   - Click the "UPI" entry in the LEFT SIDEBAR.
   - The right panel updates to show "Please select UPI payment option".

5. The UPI payment option panel is now visible.
   VERIFY: A heading "Please select UPI payment option" is visible, with a single radio option
   labeled "UPI QR" (input id="upiQR1", name="upiOpt"). The radio is NOT yet selected.
   - Click the "UPI QR" radio button.
   - A yellow "Pay Now" button (id="upiButton") appears below the radio.

6. Click the yellow "Pay Now" button.
   - Wait for navigation to the QR page (may take up to 15 seconds).

7. The Scan UPI QR page loads.
   VERIFY: You see "SBIePay" logo at top, heading "Scan UPI QR".
   - A blue banner "Don't press the back button. Please wait... Your transaction is in process."
   - A QR code image embedded inline (src starts with "data:image/png;base64,").
   - Two blue countdown boxes showing minutes (id="countDownTimer") and seconds (id="timerSecond").
   - Caption "Time left to complete the transaction".

   - IMPORTANT: Do NOT refresh the page. Do NOT click "back". Do NOT click anything.

   - STEP A — Upload the QR code (do this FIRST, before anything else):
     Call save_qr_code({}).
     This captures the QR image from the page and uploads it so the client can display it to the user.
     - Wait for the response.
     - If response is {"ok": true} → QR uploaded successfully. Continue to STEP B.
     - If response is {"ok": false} → Log the error message. Do NOT abort or retry. Continue to STEP B.
     The QR upload must NEVER block the payment — always proceed to STEP B regardless.

   - STEP B — Wait for human payment:
     Call wait_for_human with reason: "UPI payment of ₹<amount> required for border tax of vehicle ${vehicleNumber} entering Madhya Pradesh. A QR code is displayed on screen — please scan it with your UPI app and complete the payment. The transaction will expire in ~3 minutes. After payment is successful, wait for the page to update automatically, then reply done."
   - After calling wait_for_human, do NOT interact with the page at all.

8. After human confirms payment is done:
   - The page should transition away from the QR code page automatically (typically within 10 seconds).
   - If the page still shows the QR code 30 seconds after "done", wait one more time (max 30s).
   - If the page shows "Transaction Failed", "Payment Timeout", or any error → ABORT. Reason: "Payment failed: [exact error from page]"
   - Once the QR page is gone (success page or receipt) → proceed to Phase 6.
`
        : `
4. The SBIePay page loads (epay.sbi.bank.in).
   - Click "Banking Connect (Net Banking)" in the LEFT SIDEBAR.
   - Select "State Bank of India" / SBI from the bank list.
   - Click Proceed / Pay Now to be redirected to the SBI corporate login page.

5. The SBI Net Banking login page loads.
   - Click the "Corporate Banking / yono BUSINESS" tab.
   - Click the "User ID" field and type: ${sbiUserId}
   - Click the "Password" field and type: ${sbiPassword}
   - Click the "LOGIN" button.
   - Wait for the next page to load (may take 10-15 seconds).

6. The Account Selection & Payment Details page loads.
   - Verify the account radio button is pre-selected; if not, select the first row.
   - Click the yellow "CONFIRM" button.

7. The OTP / High Security Password page loads.
   - Call wait_for_human with this exact reason: "SBI Net Banking OTP required for border tax of vehicle ${vehicleNumber} entering Madhya Pradesh. Please enter the OTP sent to the registered mobile number on the screen, then reply with the OTP."
   - When the human replies, type their reply into the "Enter High Security Password" field.
   - Click the yellow "CONFIRM" button.
   - Wait for the transaction to complete (may take 30+ seconds).

8. If the transaction succeeds, proceed to Phase 6 (receipt capture).
   If the transaction fails → ABORT. Reason: "SBI Net Banking transaction failed: [exact error]".
`;


    return `
# BORDER TAX PAYMENT — MADHYA PRADESH

You are completing a border tax payment for vehicle ${vehicleNumber} entering MADHYA PRADESH.

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
- Payment Gateway page shows no "Direct Payment(SBIePay)" option in the gateway dropdown → ABORT. Reason: "Madhya Pradesh payment gateway does not offer SBIePay; manual intervention required."
- SBIePay page does not load within 30 seconds after Submit → ABORT. Reason: "SBIePay page did not load."
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
AVAILABLE ACTIONS: Click dropdown, select "MADHYA PRADESH".

PAGE: CHECKPOST PORTAL — Service Selection
URL: services.parivahan.gov.in/checkpostv4/
VISUAL: "Online Chekpost Portal" page with "Service Name" dropdown and a blue ">> Go" button.
  "Select Visiting State Name" shows "MADHYA PRADESH".
AVAILABLE ACTIONS: Select "VEHICLE TAX COLLECTION (OTHER STATE)" from dropdown, click "Go".

PAGE: CHECKPOST PORTAL — Vehicle Entry / Owner Information (Step 1 of 4)
VISUAL: Heading "Border Tax Payment for Entry Into MADHYA PRADESH". "Owner Information: <vehicleNumber>" subheading.
  "Tax Pay for Temporary Vehicle" checkbox (leave unchecked). "Input Vehicle Number" text field, "Get Details" button, "Reset All" button.
  After "Get Details" succeeds: Chassis No., Owner Name, Mobile No., From State, "Entry District Name" and "Entry CheckPost Name" dropdowns appear.
  "Next" button at the bottom-right.
AVAILABLE ACTIONS: Type vehicle number, click "Get Details", select district, select checkpost, click "Next".

PAGE: CHECKPOST PORTAL — Vehicle Information (Step 2 of 4)
VISUAL: Vehicle info fields (Vehicle Type, Vehicle Class, Vehicle Category, Permit Type,
  Seating Capacity, Sleeper Capacity, Standing Capacity, Gross Combination Wt, Service Type,
  Permit Validity, Permit No, Insurance Validity, Fitness Validity, PUCC Validity).
  "Vehicle Category" / "Permit Type" / "Service Type" are dropdowns the agent must set
  IF EMPTY. Other fields are typically pre-filled from RC data.
  "Previous" and "Next" buttons at the bottom.
POSSIBLE POPUP: A red error popup reading "No valid insurance detected..." / "Fitness expired..." / "PUCC expired..."
  with an "OK" button may appear. This is BLOCKING — close the popup and ABORT.
AVAILABLE ACTIONS: Set Vehicle Category if empty, set Permit Type if empty, set Service Type if empty, click "Next". If validity popup appears → click "OK" → ABORT.

PAGE: CHECKPOST PORTAL — Tax Information (Step 3 of 4)
VISUAL: "Tax Mode" dropdown (only option: DAYS), "No of Period" (read-only/auto), "Tax From"
  and "Tax Upto" date fields (HTML5 type="date", placeholder "DD-MM-YYYY", but the DOM value
  the input ACCEPTS is plain ISO "YYYY-MM-DD").
  "Calculate Fee/Tax" button, "Next" button.
  After Calculate Fee/Tax, a table of MV Tax / Service-User Charge / SGST / CGST rows appears
  and a "Total Amount" field below it gets populated (e.g. ₹733).
AVAILABLE ACTIONS: Select Tax Mode "${taxMode}", type Tax From "${tf.iso}", type Tax Upto "${tu.iso}", click Calculate Fee/Tax, click Next.

PAGE: CHECKPOST PORTAL — Disclaimer (Step 4 of 4)
VISUAL: A summary of all the vehicle/tax information entered. "I confirm that above information are correct
  as per my knowledge" checkbox with red label text. A canvas captcha image with distorted characters
  and a blue refresh button. A captcha text input field (input#inputcap). A read-only Total Amount on the right.
  "Previous" and green "Pay Online" buttons at the bottom-right.
POSSIBLE POPUP after checkbox tick: A modal titled "Vehicle Number: ${vehicleNumber}" showing Tax From,
  Tax Upto, Tax Mode, Total Amount, and "Receipt valid for X Days" in red. Has a "Close" button.
POSSIBLE POPUP after Pay Online: A confirmation popup with an "i" icon, the text "Are you sure?" and
  "You want to pay online ?", with green "Yes" and grey "Cancel" buttons.
AVAILABLE ACTIONS: Solve CAPTCHA, check checkbox, close "Receipt valid" popup, click "Pay Online", then click "Yes" on the confirmation popup.

PAGE: PAYMENT GATEWAY — Ministry of Road Transport & Highway
URL: vahan.parivahan.gov.in/eTransPgi/vahanPGIWebService
VISUAL: "PAYMENT GATEWAY" header with "Session Time Left" countdown in top-right.
  "PAYMENT DETAILS" section showing "Payment Id" (read-only, prefix "MPP..."),
  "Amount" (read-only, e.g. "733.00"), "Select Payment Gateway" dropdown.
  Dropdown options: "Select Payment" (placeholder) and "Direct Payment(SBIePay)" (the ONLY real option).
  A list of "Once payment process is completed, no automatic refund..." notes.
  Checkbox "I accept terms and conditions." and a blue "Submit" button (input#sendSubmit).
AVAILABLE ACTIONS: Select "Direct Payment(SBIePay)" from dropdown, check the terms checkbox, click "Submit".

${paymentPageDescriptions}

PAGE: MADHYA PRADESH TRANSPORT DEPARTMENT — Receipt (Checkpost Tax e-Receipt)
URL: usually under services.parivahan.gov.in/checkpostv4/
VISUAL: Two buttons at the very top: a blue "Back" button and a blue "Print" button.
  Below them, a printed-style receipt with these features:
  - A faint diagonal watermark of "<vehicleNumber> <date> <time>" repeating across the page.
  - Top-left: a "Government of Madhya Pradesh" emblem.
  - Top-center: heading "GOVERNMENT OF MADHYA PRADESH", subheading "Department of Transport",
    sub-subheading "Checkpost Tax e-Receipt".
  - Top-right: a QR code, with "Printed on : <date> <time>" above it.
  - A faint circular government stamp behind the body of the receipt.
  - Two-column body of fields:
    Left column likely includes: Registration No., Payment Initialization Date, Chassis No., Vehicle Type,
    Vehicle Category, CheckPost Name, Sleeper Cap, Payment Mode, Permit Validity, Insurance Validity,
    Service Type, Payment Confirmation Date.
    Right column likely includes: Receipt No., Owner Name, Tax Mode, Vehicle Class, Mobile No.,
    Seating Capacity, Bank Ref. No., Permit Number, Fitness Validity, PUCC Validity, Permit Type.
  - A summary table near the bottom with columns: Tax/Fee Particular, Tax/Fees, Fine, Total
    (rows: MV Tax, Service/User Charge, SGST, CGST).
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
4. Select "MADHYA PRADESH" from the state dropdown.
5. This navigates to the Online Chekpost Portal page (services.parivahan.gov.in/checkpostv4/).
   The "Select Visiting State Name" field should now show "MADHYA PRADESH".

===
PHASE 2 — SELECT SERVICE AND ENTER VEHICLE
===
1. On the Online Chekpost Portal page, click the "Service Name" dropdown.
2. Select "VEHICLE TAX COLLECTION (OTHER STATE)".
3. Click the blue ">> Go" button.
4. A new page loads: "Border Tax Payment for Entry Into MADHYA PRADESH" with "Owner Information: <vehicleNumber>".
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
     (Madhya Pradesh checkpost names are compact like KARAHAL/NAHAR/SAMRASA and we don't have a confirmed entryCheckpoint param, so default to the first available checkpost for the district.)`}
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
         Note: MP's portal has a typo "SEPECIAL PERMIT" — do NOT select that. Prefer "TEMPORARY PERMIT" or "ALL INDIA TOURIST PERMIT" only.
       - "Service Type": if empty, select "${serviceType}". If already filled, LEAVE IT AS IS.
       - After EACH selection, wait ~1 second and rescan for a popup. If a validity popup appears at any point:
         → Click "OK" to close it.
         → ABORT. Same reason as 2a.

   2c. Once the required dropdowns are set and no popup is showing, click the "Next" button to proceed.
       (There is NO "Distance" field on Madhya Pradesh — unlike Haryana.)

===
PHASE 4 — TAX CALCULATION
===
1. On the Tax Information page (Step 3 of 4):

   --- 1a. Tax Mode ---
   - Click the "Tax Mode" dropdown and select "${taxMode}".
   - Madhya Pradesh ONLY supports DAYS. If the requested taxMode is anything else, ABORT.

   --- 1b. Tax From (target: ${tf.iso}) ---

   The "Tax From" field is a native HTML5 date input. Concretely:
     - selector: #floatingTaxfrom   (id="floatingTaxfrom")
     - type="date"                  (NOT datetime-local — this matters)
     - placeholder ATTRIBUTE says "DD-MM-YYYY" but the empty FIELD displays "mm/dd/yyyy"
       (browser locale display)
     - the DOM value the input ACCEPTS programmatically is plain ISO: "YYYY-MM-DD"
       (no "T", no time portion).
     - The field has min="<today's date>" — past dates are silently rejected.

   TARGET DOM VALUE → "${tf.iso}"
   (this is ${tf.monthName} ${tf.dd}, ${tf.year}; will DISPLAY as ${tf.mm}/${tf.dd}/${tf.year})

   METHOD A — Direct ISO input (try this FIRST):
     i.  Use your standard input/type action on #floatingTaxfrom with value "${tf.iso}" exactly.
         Note the hyphens — this is YYYY-MM-DD, NOT slashed mm/dd/yyyy.
     ii. After the action, the field should display "${tf.mmddyyyy}".

   METHOD B — Calendar Picker (FALLBACK if Method A leaves the field showing "mm/dd/yyyy"):
     i.   Click the SMALL CALENDAR ICON at the RIGHT EDGE of #floatingTaxfrom.
          Do NOT click the middle of the field, do NOT click the "mm/dd/yyyy" placeholder text.
          ONLY the calendar icon at the right edge opens the popup.
     ii.  A date-picker popup appears below the field with a month grid and ↑/↓ arrows.
     iii. Navigate to "${tf.monthName} ${tf.year}" using the ↑/↓ arrows.
     iv.  Click the number "${tf.dd}" in the grid (black/active, not greyed).
     v.   The picker closes; the input should now show "${tf.mmddyyyy}".

   --- 1c. Tax Upto (target: ${tu.iso}) ---
   Same procedure on #uptpDate (note the typo — the id is "uptpDate", not "uptoDate").
   TARGET DOM VALUE → "${tu.iso}"
   Expected display after entry: "${tu.mmddyyyy}"

   --- 1d. Calculate ---
   - Click the "Calculate Fee/Tax" button.
   - Wait for the tax table (MV Tax / Service-User Charge / SGST / CGST rows) to appear
     and the Total Amount field to populate.
   - Click "Next".

===
PHASE 5 — DISCLAIMER (CAPTCHA + CONFIRM)
===
1. The Disclaimer page (Step 4 of 4) loads. It shows the vehicle/tax summary in a two-column form.

2. Solve the CAPTCHA:
   - The captcha is a small canvas (~145x36 px) with distorted characters next to a blue refresh button.
   - Type the characters exactly into the captcha input field (id="inputcap", case-sensitive, max 8 chars).

3. Tick the "I confirm that above information are correct as per my knowledge" checkbox.
   - If a popup appears showing the vehicle summary with "Receipt valid for X Days" in red, click its "Close" button.

4. Click the green "Pay Online" button at the bottom-right.

5. A confirmation popup appears: "Are you sure? You want to pay online ?".
   - Click the green "Yes" button.

6. Wait for navigation to the Payment Gateway page (URL contains "etranspgi").
   - If the captcha is rejected ("Invalid Captcha" popup), close the popup, click the blue refresh button next to the captcha image, and retry with the new image. Max 3 retries before aborting.

7. On the Payment Gateway page (vahan.parivahan.gov.in/eTransPgi):
   - VERIFY: Header reads "PAYMENT GATEWAY" with a session timer in the top-right.
   - VERIFY: "PAYMENT DETAILS" section shows a Payment Id (starts with "MPP") and an Amount.
   - Click the "Select Payment Gateway" dropdown.
   - Select "Direct Payment(SBIePay)" (the only option, value="SBIe").
   - Tick the "I accept terms and conditions." checkbox.
   - Click the blue "Submit" button.
   - Wait for redirect to SBIePay (epay.sbi.bank.in).

${paymentSteps}
===
PHASE 6 — WAIT FOR RECEIPT AND CAPTURE IT
===
GOAL: After payment completes, the browser will (a) optionally show SBI's "Your payment was successful" page,
then (b) auto-redirect to the Madhya Pradesh Transport Department receipt page. Your job is to wait for the
receipt to appear and call save_receipt EXACTLY ONCE. Do NOT click Print, do NOT click Back,
do NOT click "Click here". Just wait, verify, and call the tool.

--- STEP 1: Check for SBI Payment Success / receipt page ---
After payment was confirmed, ONE of three things will be on screen:
  (a) An SBIePay payment success page.
  (b) The Madhya Pradesh Transport Department receipt page (success page flashed by, auto-redirect already happened).
  (c) Neither yet — the page is still loading/transitioning.

CHECK-FIRST POLLING:
  1. Inspect the page RIGHT NOW.
     - If the receipt page is already visible (the six-marker check from STEP 2 passes) →
       SKIP STEP 1 entirely and jump to STEP 3.
     - If a success page is showing → wait for the automatic redirect (do NOT click any "Click here" link).
     - If NEITHER is visible → wait 5 seconds, then re-check.
  2. Repeat the 5-second wait + re-check up to 6 times (~30 seconds total).
  3. If after ~30 seconds neither page is visible → call done with Status: partial. See STEP 5.

--- STEP 2: Verify the receipt page is fully loaded ---
The receipt page is identified by ALL six of these markers:
  - "GOVERNMENT OF MADHYA PRADESH" heading text
  - "Department of Transport" subheading
  - "Checkpost Tax e-Receipt" sub-subheading
  - "Receipt No." label with a value (e.g. an alphanumeric receipt number)
  - "Grand Total :" near the bottom with a numeric amount and amount in words
  - Two top buttons "Back" and "Print"

--- STEP 3: Read the receipt fields ---
Extract these values from the receipt:
  - Vehicle Number (from "Registration No.")
  - Receipt Number (from "Receipt No.")
  - Amount (from "Grand Total : <amount>/-")
  - Payment Confirmation Date (from "Payment Confirmation Date" — convert to YYYY-MM-DD)

--- STEP 4: Call save_receipt ---
Call save_receipt with the values extracted in STEP 3.
DO NOT click "Print" or "Back" before, during, or after calling save_receipt.
- "ok": true and "pdfUploaded": true → STEP 6 (full success completion).
- "ok": false → STEP 5 (partial completion). Include the tool's error message. DO NOT retry.

DO NOT call save_receipt more than once. DO NOT click "Print" or "Back".

--- STEP 5: PARTIAL COMPLETION ---
Call done with this exact summary template:

  Vehicle: ${vehicleNumber}
  State: Madhya Pradesh
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
  State: Madhya Pradesh
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

"""Scripted challan-SETTLEMENT runner (eCourts Virtual Courts portal).

This is the deterministic counterpart of the AI `challan-settlement` task. It
mimics that flow — MINUS the Delhi Traffic Police (DTP) visit:

    (skipped)  Phase 1   DTP challan extraction  → removed per product decision
    Phase 1.5  determine departments             → from the DB, via the API
                                                    /api/internal/challans/departments
    Phase 2    per department: Virtual Courts search by VEHICLE number,
               extract each record's court-proposed (settlement) fine, and
               save the discounts via /api/internal/discounts/save
    Phase 3    reconcile → RunOutcome (done | partial)

It does NOT pay anything and captures no receipt — payment is the separate
`challan-payment` module (scripted/challan/), which runs later once the user
approves the settlement.

Single-portal flow: a "department" is just a Virtual Courts dropdown value, so
— like scripted/challan (payment) and unlike border_tax — there is ONE flow
(vcourts.run_department), looped over every department found in the DB.

CAPTCHA (a securimage <img>) is handled automatically by
scripted.captcha.solve_image_captcha (web: AI-OCR attempts + human fallback;
app: AI-OCR attempts then abort), faithfully mirroring the AI flow's semantics.
"""

# worker/src/scripted/fetch_receipt/__init__.py
"""Fetch Border Tax Receipt — re-download a previously-paid border-tax
receipt PDF from services.parivahan.gov.in/checkpostv4 'Print Payment
Receipt' page.

Single state-agnostic flow (the receipt-print page is the same for all
22 supported states; only the dropdown value differs).

Reuses:
  - scripted.captcha.solve_canvas_captcha  — LLM OCR for the canvas captcha
    (same approach as the existing border-tax disclaimer page; no scripted
    alternative exists for canvas captchas).
  - actions.save_receipt                   — Page.printToPDF + multipart
    upload to /api/internal/border-tax/save-receipt, which lands the PDF
    in the same GCS path + Firestore doc as the original border-tax flow.
"""

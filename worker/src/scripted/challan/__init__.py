"""Scripted challan-payment runner (eCourts Virtual Courts portal).

Single-portal flow: the "department" is just a dropdown value on
vcourts.gov.in, so — unlike border_tax, where each state is a separate
portal with its own module — there is ONE flow (vcourts.run), parameterized
by department.

Human-in-the-loop at exactly two points (see vcourts.py):
  - CAPTCHA on the search page.
  - Payment.
"""

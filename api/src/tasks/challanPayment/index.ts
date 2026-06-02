import type { Task } from "../types";

/**
 * Challan Payment via the eCourts Virtual Courts portal
 * (https://vcourts.gov.in/virtualcourt/index.php).
 *
 * SCRIPTED-ONLY task — same shape as fetch-receipt. The worker dispatches by
 * taskId straight to scripted.challan.runner.run_challan_payment, so the
 * buildPrompt below is never executed; all logic lives in the worker.
 *
 * Human intervention happens at exactly TWO points (handled in the worker):
 *   1. CAPTCHA on the Virtual Courts search page.
 *   2. The actual payment.
 * Everything else (department select, navigation, field fill, record match,
 * receipt capture) is scripted.
 *
 * `department` is OPTIONAL: if omitted, the worker derives it from the
 * challan number's prefix (scripted/challan/dispatch.py). Pass it explicitly
 * to override that derivation.
 */
export const challanPayment: Task = {
  id: "challan-payment",
  name: "Challan Payment (Virtual Courts)",
  requiredParams: ["requestId", "vehicleNumber", "challanNo"],
  optionalParams: ["chassisNo", "engineNo", "department", "driverId"],
  tools: [],
  buildPrompt: async (_p, _source) => {
    return "[scripted-only] challan-payment — worker dispatches by taskId; this prompt is not executed.";
  },
};

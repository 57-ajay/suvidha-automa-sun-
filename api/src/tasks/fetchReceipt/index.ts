import type { Task } from "../types";

export const fetchReceipt: Task = {
    id: "fetch-receipt",
    name: "Fetch Border Tax Receipt (re-download)",
    requiredParams: [
        "driverId",
        "vehicleNumber",
        "requestId",
        "paymentDate", // YYYY-MM-DD
        "stateName",   // full name ("UTTAR PRADESH") or 2-letter code ("UP")
    ],
    optionalParams: [
        "receiptNo",   // if known, skip the date match step
    ],
    tools: [],
    buildPrompt: async (_p, _source) => {
        return "[scripted-only] fetch-receipt — worker dispatches by taskId; this prompt is not executed.";
    },
};

import type { TaskTool } from "../types";

export const tools: TaskTool[] = [
    {
        name: "save_receipt",
        description:
            "Save the border tax payment receipt PDF. Call this after payment is complete and the receipt page is visible. " +
            "Pass the receipt details as data.",
        parameters: {
            data: {
                type: "object",
                description:
                    'Object with: vehicleNumber (string), receiptNumber (string), amount (number in Rs), paymentDate (string YYYY-MM-DD). ' +
                    'Example: {"vehicleNumber":"HR55AV7291","receiptNumber":"UPR2604080765418","amount":2760,"paymentDate":"2026-04-08"}',
            },
        },
        endpoint: "/api/internal/border-tax/save-receipt",
        method: "POST",
    },
];

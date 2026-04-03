import type { TaskTool } from "../types";

export const tools: TaskTool[] = [
    {
        name: "save_challans",
        description:
            "Save extracted challans to the database. Call this after extracting ALL challans from Delhi Traffic Police. " +
            "Pass a JSON array of challan objects as the data parameter.",
        parameters: {
            data: {
                type: "array",
                description:
                    'Array of objects, each with: challanId (string), offence (string), amount (number in Rs), date (string YYYY-MM-DD). ' +
                    'Example: [{"challanId":"DL123456","offence":"Red Light Violation","amount":500,"date":"2024-06-15"}]',
            },
        },
        endpoint: "/api/internal/challans/save",
        method: "POST",
    },
    {
        name: "save_discounts",
        description:
            "Save discount/settlement amounts from Virtual Courts. Call this after extracting ALL discount data from ALL departments. " +
            "Pass a JSON array of discount objects as the data parameter.",
        parameters: {
            data: {
                type: "array",
                description:
                    'Array of discount objects, each with: challanId (string), discountAmount (number in Rs), originalAmount (number in Rs). ' +
                    'Example: [{"challanId":"DL123456","discountAmount":250,"originalAmount":500}]',
            },
        },
        endpoint: "/api/internal/discounts/save",
        method: "POST",
    },
];

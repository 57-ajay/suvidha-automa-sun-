import type { Task } from "../types";
import { tools } from "./tool";
import { buildPrompt } from "./prompt";

export const borderTax: Task = {
    id: "border-tax",
    name: "Border Tax Payment",
    requiredParams: ["vehicleNumber", "requestId", "taxFrom", "taxUpto"],
    optionalParams: [
        "driverId",
        "taxMode",
        "entryDistrict",
        "entryCheckpoint",
        "serviceType",
        "sbiUserId",
        "sbiPassword",
    ],
    tools: tools,
    buildPrompt: async (p, _source) => { return await buildPrompt(p) },
};

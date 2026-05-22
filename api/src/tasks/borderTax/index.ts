import type { Task } from "../types";
import { tools } from "./tool";
import { buildPrompt } from "./prompt";

export const borderTax: Task = {
    id: "border-tax",
    name: "Border Tax Payment",
    requiredParams: ["vehicleNumber", "driverId", "requestId", "taxFrom", "taxUpto"],
    optionalParams: [
        "state",
        "taxMode",
        "entryDistrict",
        "entryCheckpoint",
        "serviceType",
        "permitType",
        "permitTypeFallback",
        "distance",
        "paymentMethod",
        "bankName",
        "sbiUserId",
        "sbiPassword",
    ],
    tools: tools,
    buildPrompt: async (p, _source) => { return await buildPrompt(p) },
};

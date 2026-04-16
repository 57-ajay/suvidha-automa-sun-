import type { Task } from "../types";
import { tools } from "./tool";
import { buildPrompt } from "./prompt";

export const borderTax: Task = {
    id: "border-tax",
    name: "Border Tax Payment",
    requiredParams: ["vehicleNumber", "requestId", "taxFrom", "taxUpto"],
    optionalParams: ["taxMode", "entryDistrict", "entryCheckpoint", "serviceType", "sbiUserId", "sbiPassword"],
    tools: tools,
    buildPrompt: async (p) => { return await buildPrompt(p) },
};

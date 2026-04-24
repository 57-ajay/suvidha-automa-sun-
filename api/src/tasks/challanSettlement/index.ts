import type { Task } from "../types";
import { tools } from "../challanSettlement/tool";
import { buildPrompt } from "../challanSettlement/prompt";

export const challanSettlement: Task = {
    id: "challan-settlement",
    name: "Challan Settlement Automation",
    requiredParams: ["vehicleNumber", "requestId"],
    optionalParams: ["mobileNumber", "chassisLastFour", "engineLastFour"],
    tools: tools,
    buildPrompt: async (p, source) => { return await buildPrompt(p, source) }
};

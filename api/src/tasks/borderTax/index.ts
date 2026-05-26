import type { Task } from "../types";
import { tools } from "./tool";
import { buildPrompt } from "./prompt";
import { normalizeISODate, computeTaxUpto } from "./shared/dates";
import { resolveStateKey } from "./states";

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
        "duration",
    ],
    tools: tools,
    buildPrompt: async (p, _source) => { return await buildPrompt(p) },

    preprocessParams: (params: Record<string, string>): Record<string, string> => {
        const durationRaw = params.duration;
        if (!durationRaw) {
            return params;
        }

        const duration = parseInt(durationRaw, 10);
        if (isNaN(duration) || duration < 1) {
            throw new Error(
                `Invalid duration: "${durationRaw}". Must be a positive integer (>= 1).`
            );
        }

        const taxFrom = params.taxFrom;
        if (!taxFrom) {
            throw new Error(
                `duration requires taxFrom to be set, but taxFrom is missing.`
            );
        }

        const taxFromISO = normalizeISODate(taxFrom);
        const stateKey = resolveStateKey(params.state);
        const taxUpto = computeTaxUpto(taxFromISO, duration, stateKey);

        console.log(
            `[borderTax.preprocessParams] duration=${duration} ` +
            `taxFrom=${taxFromISO} state=${stateKey} → taxUpto=${taxUpto}`
        );

        return {
            ...params,
            taxFrom: taxFromISO,
            taxUpto,
        };
    },
};

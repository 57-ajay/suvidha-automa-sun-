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
        "taxTime",
    ],
    tools: tools,
    buildPrompt: async (p, _source) => { return await buildPrompt(p) },

    preprocessParams: (params: Record<string, string>): Record<string, string> => {
        const out = { ...params };

        // ── taxTime: optional 24h "HH:MM" (IST) that the worker stamps onto
        // the Tax From / Tax Upto datetime-local fields, so a driver can book
        // a start a few hours ahead instead of "now at fill time". ONE time
        // for BOTH ends — the From→Upto span must stay an exact 24h multiple
        // or the portal bills the extra minute as a full extra day (see
        // worker _tax_time.py). Date-only states ignore it.
        const taxTimeRaw = (out.taxTime ?? "").trim();
        if (!taxTimeRaw) {
            delete out.taxTime;
        } else {
            const m = taxTimeRaw.match(/^(\d{1,2}):(\d{2})(?::\d{2})?$/);
            const hh = m ? parseInt(m[1]!, 10) : NaN;
            const mi = m ? parseInt(m[2]!, 10) : NaN;
            if (!m || hh > 23 || mi > 59) {
                throw new Error(
                    `Invalid taxTime: "${taxTimeRaw}". Must be 24h "HH:MM" (e.g. "09:30" or "14:00").`
                );
            }
            out.taxTime = `${String(hh).padStart(2, "0")}:${String(mi).padStart(2, "0")}`;
        }

        const durationRaw = out.duration;
        if (!durationRaw) {
            return out;
        }

        const duration = parseInt(durationRaw, 10);
        if (isNaN(duration) || duration < 1) {
            throw new Error(
                `Invalid duration: "${durationRaw}". Must be a positive integer (>= 1).`
            );
        }

        const taxFrom = out.taxFrom;
        if (!taxFrom) {
            throw new Error(
                `duration requires taxFrom to be set, but taxFrom is missing.`
            );
        }

        const taxFromISO = normalizeISODate(taxFrom);
        const stateKey = resolveStateKey(out.state);
        const taxUpto = computeTaxUpto(taxFromISO, duration, stateKey);

        console.log(
            `[borderTax.preprocessParams] duration=${duration} ` +
            `taxFrom=${taxFromISO} state=${stateKey} → taxUpto=${taxUpto}`
        );

        return {
            ...out,
            taxFrom: taxFromISO,
            taxUpto,
        };
    },
};

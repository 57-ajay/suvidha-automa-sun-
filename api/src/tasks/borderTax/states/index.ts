import { buildPrompt as buildUP } from "./up";
import { buildPrompt as buildHR } from "./hr";
import { buildPrompt as buildRJ } from "./rj";
import { buildPrompt as buildPB } from "./pb";
import { buildPrompt as buildMP } from "./mp";
import { buildPrompt as buildUK } from "./uk";

export type StateBuilder = (p: Record<string, string>) => Promise<string>;

const scriptedOnlyStub =
    (label: string): StateBuilder =>
        async () =>
            `[scripted-only] ${label} border-tax — the worker dispatches this by ` +
            `state code; this prompt is never executed by the agent.`;

export const STATE_BUILDERS: Record<string, StateBuilder> = {
    "UTTAR PRADESH": buildUP,
    "HARYANA": buildHR,
    "RAJASTHAN": buildRJ,
    "PUNJAB": buildPB,
    "MADHYA PRADESH": buildMP,
    "UTTARAKHAND": buildUK,
    // Scripted-only (form-fill then human handover; no AI):
    "HIMACHAL PRADESH": scriptedOnlyStub("Himachal Pradesh"),
    "BIHAR": scriptedOnlyStub("Bihar"),
    "TAMIL NADU": scriptedOnlyStub("Tamil Nadu"),
};

export const STATE_ALIASES: Record<string, string> = {
    "UP": "UTTAR PRADESH",
    "U.P.": "UTTAR PRADESH",
    "HR": "HARYANA",
    "RJ": "RAJASTHAN",
    "PB": "PUNJAB",
    "MP": "MADHYA PRADESH",
    "M.P.": "MADHYA PRADESH",
    "UK": "UTTARAKHAND",
    "U.K.": "UTTARAKHAND",
    "HP": "HIMACHAL PRADESH",
    "H.P.": "HIMACHAL PRADESH",
    "BR": "BIHAR",
    "TN": "TAMIL NADU",
    "T.N.": "TAMIL NADU",
    "TAMILNADU": "TAMIL NADU",
};

export function resolveStateKey(input: string | undefined | null): string {
    const raw = (input || "").trim().toUpperCase();
    if (!raw) return "UTTAR PRADESH";
    return STATE_ALIASES[raw] ?? raw;
}

export function getStateBuilder(stateKey: string): StateBuilder | undefined {
    return STATE_BUILDERS[stateKey];
}

export function listSupportedStates(): string[] {
    return Object.keys(STATE_BUILDERS);
}

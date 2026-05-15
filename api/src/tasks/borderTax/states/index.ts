import { buildPrompt as buildUP } from "./up";
import { buildPrompt as buildHR } from "./hr";
import { buildPrompt as buildRJ } from "./rj";
import { buildPrompt as buildPB } from "./pb";

export type StateBuilder = (p: Record<string, string>) => Promise<string>;

export const STATE_BUILDERS: Record<string, StateBuilder> = {
    "UTTAR PRADESH": buildUP,
    "HARYANA": buildHR,
    "RAJASTHAN": buildRJ,
    "PUNJAB": buildPB,
};

export const STATE_ALIASES: Record<string, string> = {
    "UP": "UTTAR PRADESH",
    "U.P.": "UTTAR PRADESH",
    "HR": "HARYANA",
    "RJ": "RAJASTHAN",
    "PB": "PUNJAB",
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

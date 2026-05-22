import {
    resolveStateKey,
    getStateBuilder,
    listSupportedStates,
} from "./states";
import { applyStateDefaults } from "./states/defaults";

export const buildPrompt = async (
    p: Record<string, string>,
): Promise<string> => {
    const stateKey = resolveStateKey(p.state);
    const builder = getStateBuilder(stateKey);

    if (!builder) {
        throw new Error(
            `Unsupported state for border tax: "${p.state ?? ""}" ` +
            `(resolved to "${stateKey}"). ` +
            `Supported: ${listSupportedStates().join(", ")}.`,
        );
    }
    const resolved = applyStateDefaults(stateKey, p);

    return builder(resolved);
};

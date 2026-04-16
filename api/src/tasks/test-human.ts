import type { Task } from "./types";

export const testHuman: Task = {
    id: "test-human",
    name: "Test Human Intervention",
    requiredParams: [],
    buildPrompt: async () => {
        return `
Go to https://example.com.
Then call the wait_for_human tool with reason "Testing: please type any message".
After you get the human's response, report what they said and mark the task as done.
  `.trim()
    },
};

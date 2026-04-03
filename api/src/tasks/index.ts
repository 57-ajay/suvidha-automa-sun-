import type { Task } from "./types";
import { challanSettlement } from "./challanSettlement";
import { testHuman } from "./test-human";

const tasks = new Map<string, Task>();

function register(task: Task) {
    tasks.set(task.id, task);
}

register(challanSettlement);
register(testHuman);

export function getTask(id: string): Task | undefined {
    return tasks.get(id);
}

export function listTasks(): string[] {
    return Array.from(tasks.keys());
}

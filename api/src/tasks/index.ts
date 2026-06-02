import type { Task } from "./types";
import { challanSettlement } from "./challanSettlement";
import { testHuman } from "./test-human";
import { borderTax } from "./borderTax";
import { fetchReceipt } from "./fetchReceipt";
import { challanPayment } from "./challanPayment";
// ...

const tasks = new Map<string, Task>();

function register(task: Task) {
  tasks.set(task.id, task);
}

register(challanSettlement);
register(testHuman);
register(borderTax);
register(fetchReceipt);
register(challanPayment);

export function getTask(id: string): Task | undefined {
  return tasks.get(id);
}

export function listTasks(): string[] {
  return Array.from(tasks.keys());
}

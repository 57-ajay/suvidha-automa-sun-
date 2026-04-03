export interface TaskToolParam {
    type: string;
    description: string;
}

export interface TaskTool {
    name: string;
    description: string;
    parameters: Record<string, TaskToolParam>;
    endpoint: string;
    method: "POST" | "GET";
}

export interface Task {
    id: string;
    name: string;
    requiredParams: string[];
    optionalParams?: string[];
    tools?: TaskTool[];
    buildPrompt: (params: Record<string, string>) => Promise<string>;
}

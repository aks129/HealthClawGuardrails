import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";
import { FHIRTools } from "./tools";

export function toMCPToolResult(result: Record<string, unknown>): CallToolResult {
  const callToolResult: CallToolResult = {
    content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
  };
  if (Object.prototype.hasOwnProperty.call(result, "error")) {
    callToolResult.isError = true;
  }
  return callToolResult;
}

export async function executeMCPTool(
  tools: FHIRTools,
  name: string,
  input: Record<string, unknown>,
  headers: Record<string, string>
): Promise<CallToolResult> {
  if (!tools.hasTool(name)) {
    throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
  }

  try {
    return toMCPToolResult(await tools.executeTool(name, input, headers));
  } catch (error) {
    const errorType = error instanceof Error ? error.name : typeof error;
    console.error(`[mcp] tool execution failed name=${name} type=${errorType}`);
    return toMCPToolResult({
      error: {
        resourceType: "OperationOutcome",
        issue: [
          {
            severity: "error",
            code: "exception",
            details: { text: "The tool could not complete the request." },
          },
        ],
      },
    });
  }
}

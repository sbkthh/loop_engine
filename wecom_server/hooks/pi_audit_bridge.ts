// pi extension that reuses wecom_server/hooks/audit_hook.sh as the single
// source of guard truth for G's WeCom chat turns under the pi backend.
// Loaded by agent_cli via `pi -e <this file>`; never compiled.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HOOK = fileURLToPath(new URL("audit_hook.sh", import.meta.url));
const TOOLS: Record<string, string> = { bash: "Bash", edit: "Edit", write: "Write" };

export function toHookPayload(toolName: string, input: any, cwd: string, sessionId: string) {
  const tool = Object.hasOwn(TOOLS, toolName) ? TOOLS[toolName] : null;
  if (!tool) return null;
  if (tool === "Bash") {
    return { session_id: sessionId, tool_name: tool, tool_input: { command: String(input?.command ?? "") } };
  }
  let p = String(input?.path ?? "");
  if (p === "~") p = homedir();
  else if (p.startsWith("~/")) p = join(homedir(), p.slice(2));
  const path = p === "" ? "" : isAbsolute(p) ? p : resolve(cwd, p);
  return { session_id: sessionId, tool_name: tool, tool_input: { file_path: path } };
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", (event, ctx) => {
    let payload;
    try {
      payload = toHookPayload(event.toolName, event.input, ctx.cwd, ctx.sessionManager.getSessionId());
    } catch (e) {
      // pi blocks the tool when a handler throws; failing open keeps G usable.
      console.error(`[pi_audit_bridge] ${String(e)}`);
      return undefined;
    }
    if (!payload) return undefined;
    const r = spawnSync("bash", [HOOK], { input: JSON.stringify(payload), encoding: "utf8" });
    if (r.status === 1) return { block: true, reason: (r.stdout || "已阻止").trim() };
    if (r.status !== 0 || r.error) console.error(`[pi_audit_bridge] hook rc=${r.status} ${r.stderr || String(r.error)}`);
    return undefined;
  });
}

if (process.argv[1]?.includes("pi_audit_bridge") && process.argv[2] === "--map") {
  const e = JSON.parse(readFileSync(0, "utf8"));
  process.stdout.write(JSON.stringify(toHookPayload(e.toolName, e.input, e.cwd, e.sessionId)));
}

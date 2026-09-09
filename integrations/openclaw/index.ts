import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { resolve } from "node:path";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

type PluginConfig = {
  memcalHome?: string;
  memcalSrc?: string;
  pythonCommand?: string;
  agentId?: string;
};

function bridge(
  command: string,
  source: string,
  home: string,
  action: "context" | "archive",
  payload: Record<string, unknown>,
): Promise<string> {
  return new Promise((accept, reject) => {
    const child = execFile(
      command,
      ["-m", "memcal.harness", action, "--home", home],
      {
        cwd: source,
        env: { ...process.env, MEMCAL_HOME: home },
        timeout: 5_000,
        maxBuffer: 2 * 1024 * 1024,
      },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(stderr.trim() || error.message));
        } else {
          accept(stdout);
        }
      },
    );
    child.stdin?.end(JSON.stringify(payload));
  });
}

export default definePluginEntry({
  id: "memcal",
  name: "Memcal",
  description: "Fresh personal context and inbound-turn archival for memcal",
  register(api) {
    const configured = (api.pluginConfig ?? {}) as PluginConfig;
    const source = configured.memcalSrc ?? resolve(api.rootDir ?? ".", "..", "..");
    const home = configured.memcalHome ?? process.env.MEMCAL_HOME ?? resolve(homedir(), ".memcal");
    const python = configured.pythonCommand ?? "python3";

    const enabledFor = (agentId?: string) => !configured.agentId || configured.agentId === agentId;
    const sessionAgent = (sessionKey?: string) => sessionKey?.match(/^agent:([^:]+)/)?.[1];

    api.on(
      "before_prompt_build",
      async (event, ctx) => {
        if (!enabledFor(ctx.agentId)) return;
        try {
          const fresh = await bridge(python, source, home, "context", { query: event.prompt ?? "" });
          if (fresh.trim()) return { appendContext: fresh.trim() };
        } catch (error) {
          api.logger.warn(`memcal context failed: ${String(error)}`);
        }
      },
      { priority: 50, timeoutMs: 5_000 },
    );

    api.on(
      "message_received",
      async (event, ctx) => {
        if (!enabledFor(sessionAgent(ctx.sessionKey)) || !String(event.content ?? "").trim()) return;
        try {
          await bridge(python, source, home, "archive", {
            text: event.content,
            session_id: ctx.sessionKey ?? event.threadId ?? "default",
            message_id: event.messageId ?? ctx.messageId ?? "",
            sender: "me",
          });
        } catch (error) {
          api.logger.warn(`memcal inbound archive failed: ${String(error)}`);
        }
      },
      { priority: 50, timeoutMs: 5_000 },
    );
  },
});

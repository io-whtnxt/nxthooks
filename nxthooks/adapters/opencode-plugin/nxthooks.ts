// nxthooks transport for OpenCode.
//
// Thin by contract (2026-08-22 component-config spec): all selection,
// policy, timeouts, and execution live in the NxtHooks dispatcher at
// $NXTHOOKS_ROOT. This plugin translates OpenCode's events onto the
// canonical vocabulary and applies verdicts at the maximum capability
// OpenCode grants:
//
//   chat.message        -> inject advisory context parts (no prompt block)
//   tool.execute.before -> block by throwing into the tool call
//   tool.execute.after  -> after_tool hooks, observe-only (the tool already
//                          ran; a block verdict here cannot be honoured and
//                          must never throw)
//
// Installed to ~/.config/opencode/plugins/nxthooks.ts by `nxthooks install`.
// Behavior changes land in the dispatcher/hook sources, not here.

import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

const ROOT =
  process.env.NXTHOOKS_ROOT || path.join(homedir(), ".local/share/nxt/hooks");
const RUNNER = path.join(ROOT, "lib", "run_nxthooks.py");

// The dispatcher needs tomllib (3.11+); a bare `python3` can be an older
// system interpreter, and the failed import resolves null, which allows. The
// installer records the interpreter it validated.
const PYTHON = (() => {
  try {
    return readFileSync(path.join(ROOT, "PYTHON"), "utf8").trim() || "python3";
  } catch {
    return "python3";
  }
})();

// A missing runner resolved `null`, and `null` means allow to both handlers
// below. Fail-open with no signal, on a security layer. It still allows, but
// once per session it says so: in the chat surface where the hooks' own
// output goes, and on stderr for the tool path, which has no surface of its
// own that is not a thrown error.
let runnerNoticeShown = false;

function runnerMissingNote(): string | null {
  if (runnerNoticeShown) return null;
  runnerNoticeShown = true;
  return `[nxthooks] runner missing at ${RUNNER}; hooks are NOT running this session. Run: nxthooks doctor`;
}

function callDispatcher(envelope: Record<string, unknown>): Promise<any | null> {
  if (!existsSync(RUNNER)) {
    return Promise.resolve({ runner_missing: true });
  }
  return new Promise((resolve) => {
    const child = spawn(
      PYTHON,
      [RUNNER, "dispatch", "--harness", "opencode", "--out", "json"],
      { stdio: ["pipe", "pipe", "ignore"] },
    );
    let out = "";
    const timer = setTimeout(() => {
      try { child.kill(); } catch {}
    }, 9000);
    child.stdout!.on("data", (d) => (out += String(d)));
    child.on("error", () => { clearTimeout(timer); resolve(null); });
    child.on("close", () => {
      clearTimeout(timer);
      try {
        resolve(JSON.parse(out));
      } catch {
        resolve(null);
      }
    });
    child.stdin!.end(JSON.stringify(envelope));
  });
}

export const NxtHooksPlugin = async ({ directory, worktree }: any) => {
  const root = worktree || directory;
  const endedSessions = new Set<string>();

  return {
    event: async ({ event }: any) => {
      if (event?.type !== "session.deleted") return;

      const info = event?.properties?.info ?? {};
      const sessionId = String(
        info?.id ?? event?.properties?.sessionID ?? event?.properties?.sessionId ?? "",
      );
      const key = sessionId || String(event?.id ?? "anonymous");
      if (endedSessions.has(key)) return;
      endedSessions.add(key);

      await callDispatcher({
        version: 1,
        harness: "opencode",
        event: "session_end",
        cwd: info?.directory || root,
        session_id: sessionId,
        reason: "deleted",
      });
    },

    "chat.message": async (input: any, output: any) => {
      try {
        const promptText = (output?.parts ?? [])
          .filter((p: any) => p?.type === "text")
          .map((p: any) => p?.text ?? "")
          .join(" ")
          .slice(0, 4000);
        if (!promptText) return;
        const outcome = await callDispatcher({
          version: 1,
          harness: "opencode",
          event: "prompt_submit",
          cwd: root,
          session_id: input?.sessionID ?? output?.message?.sessionID ?? "",
          prompt: promptText,
        });
        if (outcome?.runner_missing) {
          const note = runnerMissingNote();
          if (!note) return;
          output.parts.push({
            id: `prt_${crypto.randomUUID().replace(/-/g, "")}`,
            sessionID: output.message.sessionID,
            messageID: output.message.id,
            type: "text",
            text: note,
          });
          return;
        }
        const notes: string[] = outcome?.context ?? [];
        if (outcome?.verdict === "block") {
          notes.push(`[advise] ${outcome.reason}`);
        }
        if (!notes.length) return;
        // TextPart requires id/sessionID/messageID (@opencode-ai/sdk schema);
        // omitting them passed local typechecks (output.parts: Part[] widens
        // to accept a bare {type,text}) but fails OpenCode's runtime save
        // validation ("invalid user part before save" -> SchemaError: Missing
        // key at "id"/"sessionID"/"messageID") and kills the turn server-side.
        output.parts.push({
          // OpenCode's part-id schema requires the "prt" prefix (verified
          // live: a bare crypto.randomUUID() fails the same save-time check
          // with 'Expected a string starting with "prt"').
          id: `prt_${crypto.randomUUID().replace(/-/g, "")}`,
          sessionID: output.message.sessionID,
          messageID: output.message.id,
          type: "text",
          text: "[nxthooks]\n" + notes.join("\n"),
        });
      } catch {
        // fail-quiet: an advisory layer must never break the turn
      }
    },

    "tool.execute.before": async (input: any, output: any) => {
      const res = await callDispatcher({
        version: 1,
        harness: "opencode",
        event: "before_tool",
        cwd: root,
        session_id: input?.sessionID ?? "",
        tool_call_id: input?.callID ?? "",
        tool: String(input?.tool ?? ""),
        args: output?.args ?? {},
      });
      if (res?.runner_missing) {
        const note = runnerMissingNote();
        if (note) console.error(note);
        return;
      }
      if (!res) return; // dispatcher down fails open at the transport layer
      if (res.verdict === "block") {
        throw new Error(res.reason || "[nxthooks] blocked by hook policy");
      }
    },

    "tool.execute.after": async (input: any, output: any) => {
      try {
        await callDispatcher({
          version: 1,
          harness: "opencode",
          event: "after_tool",
          cwd: root,
          session_id: input?.sessionID ?? "",
          tool_call_id: input?.callID ?? "",
          // Completion is observed; no undocumented result body is treated
          // as proof of success. Owner reconciliation can compare hashes.
          success: null,
          tool: String(input?.tool ?? ""),
          // `after` carries args on `input`; only `before` carries them on
          // `output` (@opencode-ai/plugin dist/index.d.ts). Reading `output`
          // here dispatched an empty payload to every after_tool hook, which
          // is indistinguishable from a hook that ran and found nothing.
          args: input?.args ?? {},
        });
      } catch {
        // fail-quiet: the tool already ran, so a block verdict cannot be
        // honoured here and must never be thrown.
      }
    },
  };
};

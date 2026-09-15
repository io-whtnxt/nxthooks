// nxthooks transport for pi.
//
// Thin by contract (2026-08-22 component-config spec): all selection,
// policy, timeouts, and execution live in the NxtHooks dispatcher. This
// extension translates pi's events onto the canonical vocabulary and
// applies verdicts at the maximum capability pi grants:
//
//   session_start / before_agent_start -> inject context (return message)
//   before_agent_start (preflight)      -> before_tool hooks marked
//                                          pi_preflight (block condition
//                                          knowable with no tool/args yet)
//                                          get an interactive stay-or-go
//                                          confirm (ctx.ui.select), same
//                                          pattern as nxtgit's stay-or-switch
//   tool_call                           -> other before_tool hooks (real args
//                                          needed, e.g. secret-scan): a real
//                                          pre-execution block, via the
//                                          handler's {block, reason} result
//   tool_result                         -> after_tool hooks, observe-only
//
// This file previously wired both tool classes to `tool_execution_end` and
// read the tool name from `event.tool ?? event.name`. pi populates neither
// key -- it uses `toolName` -- so every dispatch carried `tool: ""`, and an
// empty tool name makes `matcher_matches()` return False for every
// matcher-bearing hook. NxtLint, NxtGrep and the nxtgit guards installed,
// verified, listed, and never once ran on pi. `tool_execution_end` also
// carries no arguments at all, so even a matcher-less hook saw an empty
// payload. Both were found by recording a live envelope, not by reading.
//
// Installed to ~/.pi/agent/extensions/nxthooks/ by `nxthooks install`.
// Behavior changes land in the dispatcher/hook sources, not here.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

const ROOT =
  process.env.NXTHOOKS_ROOT || path.join(homedir(), ".local/share/nxt/hooks");
const RUNNER = path.join(ROOT, "lib", "run_nxthooks.py");

/** pi exposes the session through `ctx.sessionManager`; there is no
 *  `ctx.session`. Reading the latter yielded "" on every event, which is the
 *  same value a session genuinely without an id has -- so the guards that key
 *  on session identity (claims, worktree ledger) saw one anonymous actor for
 *  every pi turn and could compare nothing against anything. */
function sessionIdOf(ctx: any): string {
  try {
    return ctx?.sessionManager?.getSessionId?.() ?? "";
  } catch {
    return "";
  }
}

interface Outcome {
  verdict?: string;
  reason?: string;
  context?: string[];
  runner_missing?: boolean;
}

// A missing runner used to resolve `null`, which every caller here treats as
// allow. That is fail-open with no signal at all, on a layer whose whole job
// is guards. It still allows -- refusing every turn because a dispatcher moved
// would be worse -- but it is now visible: once per session, in the same
// surface the hooks themselves use.
let runnerNoticeShown = false;

function runnerMissingNote(): string | null {
  if (runnerNoticeShown) return null;
  runnerNoticeShown = true;
  return `[nxthooks] runner missing at ${RUNNER}; hooks are NOT running this session. Run: nxtos doctor`;
}

function callDispatcher(
  envelope: Record<string, unknown>,
  extraArgs: string[] = [],
): Promise<Outcome | null> {
  if (!existsSync(RUNNER)) {
    return Promise.resolve({ runner_missing: true });
  }
  return new Promise((resolve) => {
    const child = spawn(
      "python3",
      [RUNNER, "dispatch", "--harness", "pi", "--out", "json", ...extraArgs],
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

export default function nxthooks(pi: any) {
  let lastPreflightReason: string | null = null;

  pi.on("session_start", async (_event: any, ctx: any) => {
    lastPreflightReason = null;
    try {
      const outcome = await callDispatcher({
        version: 1,
        harness: "pi",
        event: "session_start",
        cwd: ctx?.cwd ?? process.cwd(),
        session_id: sessionIdOf(ctx),
      });
      const notes = outcome?.runner_missing
        ? (runnerMissingNote() ?? "")
        : (outcome?.context ?? []).join("\n").trim();
      if (!notes) return;
      return {
        message: { customType: "nxthooks-session", content: notes, display: true },
      };
    } catch {
      return;
    }
  });

  pi.on("before_agent_start", async (event: any, ctx: any) => {
    const cwd = ctx?.cwd ?? process.cwd();
    const sessionId = sessionIdOf(ctx);
    const parts: string[] = [];

    // Independent dispatcher round-trips -- run concurrently, not
    // sequentially, so a turn only pays one ~9s worst case, not two.
    const [preflight, promptOutcome] = await Promise.all([
      callDispatcher(
        { version: 1, harness: "pi", event: "before_tool", cwd, session_id: sessionId, tool: "", args: {} },
        ["--preflight"],
      ).catch(() => null),
      callDispatcher({
        version: 1,
        harness: "pi",
        event: "prompt_submit",
        cwd,
        session_id: sessionId,
        prompt: String(event?.prompt ?? "").slice(0, 8000),
      }).catch(() => null),
    ]);

    if (preflight?.runner_missing || promptOutcome?.runner_missing) {
      const note = runnerMissingNote();
      if (note) parts.push(note);
    }

    const preflightNotes = (preflight?.context ?? []).join("\n").trim();
    if (preflightNotes) parts.push(preflightNotes);

    if (preflight?.verdict === "block" && preflight.reason) {
      if (preflight.reason !== lastPreflightReason) {
        if (ctx.ui?.select) {
          const picked = await ctx.ui.select(
            `[nxthooks] ${preflight.reason}`,
            ["Continue anyway", "Stop and investigate"],
          );
          if (picked === "Continue anyway") {
            lastPreflightReason = preflight.reason;
          } else {
            parts.push(
              `## nxthooks preflight\n\n${preflight.reason}\n\n` +
              "Do not run git-mutating commands this turn until this is resolved or confirmed.",
            );
          }
        } else if (ctx.ui?.notify) {
          await ctx.ui.notify(
            `[nxthooks] ${preflight.reason} (preflight check; no interactive confirm available)`,
            "warning",
          );
          lastPreflightReason = preflight.reason;
        }
      }
    } else {
      lastPreflightReason = null;
    }

    const notes = (promptOutcome?.context ?? []).join("\n").trim();
    if (notes) parts.push(notes);

    if (!parts.length) return;
    return {
      message: { customType: "nxthooks-prompt", content: parts.join("\n\n"), display: true },
    };
  });

  // pi's real pre-tool gate. `tool_call` is documented "Fired before a tool
  // executes. Can block." and its handler's return type is
  // `{block?, reason?, terminate?}`, so a block verdict here is a genuine
  // refusal rather than a warning after the fact.
  pi.on("tool_call", async (event: any, ctx: any) => {
    const outcome = await callDispatcher({
      version: 1,
      harness: "pi",
      event: "before_tool",
      cwd: ctx?.cwd ?? process.cwd(),
      session_id: sessionIdOf(ctx),
      tool: event?.toolName ?? "",
      args: event?.input ?? {},
    }).catch(() => null);

    if (outcome?.runner_missing) {
      const note = runnerMissingNote();
      if (note) await ctx?.ui?.notify?.(note, "warning");
      return;
    }
    const notes = (outcome?.context ?? []).join("\n").trim();
    if (notes) await ctx?.ui?.notify?.(notes, "info");
    if (outcome?.verdict === "block") {
      return { block: true, reason: outcome.reason || "hook requested a block" };
    }
  });

  // `tool_result`, not `tool_execution_end`: the latter carries only
  // `{type, toolCallId, toolName, result, isError}`, with no arguments at
  // all, so an after_tool hook selected off it can never see what ran.
  pi.on("tool_result", async (event: any, ctx: any) => {
    await callDispatcher({
      version: 1,
      harness: "pi",
      event: "after_tool",
      cwd: ctx?.cwd ?? process.cwd(),
      session_id: sessionIdOf(ctx),
      tool: event?.toolName ?? "",
      args: event?.input ?? {},
    }).catch(() => null);
    // fail-quiet: an observability hook must never break the turn
  });
}

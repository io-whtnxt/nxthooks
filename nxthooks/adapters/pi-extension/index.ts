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
  /** A --deadline-ms dispatch ended without printing an outcome: our timer
   *  stopped it, or it died after announcing its selection. */
  unanswered?: boolean;
  /** Whether an enforce-policy hook was selected for the call, from the
   *  selection line a --deadline-ms dispatch prints first; unknown counts. */
  enforce?: boolean;
}

// How long pi waits on the dispatcher. A tool_call hands the dispatcher this
// budget less a margin for its own start-up and output (--deadline-ms), so its
// verdict is printed before we stop listening.
const DISPATCH_TIMEOUT_MS = Number(process.env.NXTHOOKS_DISPATCH_TIMEOUT_MS) || 9000;
const DEADLINE_MARGIN_MS = 1500;

// A missing runner used to resolve `null`, which every caller here treats as
// allow. That is fail-open with no signal at all, on a layer whose whole job
// is guards. It still allows -- refusing every turn because a dispatcher moved
// would be worse -- but it is now visible: once per session, in the same
// surface the hooks themselves use.
let runnerNoticeShown = false;

function runnerMissingNote(): string | null {
  if (runnerNoticeShown) return null;
  runnerNoticeShown = true;
  return `[nxthooks] runner missing at ${RUNNER}; hooks are NOT running this session. Run: nxthooks doctor`;
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
      PYTHON,
      [RUNNER, "dispatch", "--harness", "pi", "--out", "json", ...extraArgs],
      { stdio: ["pipe", "pipe", "ignore"] },
    );
    let out = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { child.kill(); } catch {}
    }, DISPATCH_TIMEOUT_MS);
    child.stdout!.on("data", (d) => (out += String(d)));
    child.on("error", () => { clearTimeout(timer); resolve(null); });
    child.on("close", () => {
      clearTimeout(timer);
      // A --deadline-ms dispatch prints its selection line first, then the outcome.
      const lines = out.split("\n").filter((line) => line.trim());
      const read = (line: string | undefined) => {
        try { return JSON.parse(line ?? ""); } catch { return null; }
      };
      const last = read(lines[lines.length - 1]);
      // Only a line carrying a verdict is an outcome; the selection line is not.
      if (last && (typeof last.verdict === "string" || last.runner_missing === true)) {
        resolve(last);
        return;
      }
      const selection = read(lines[0]);
      const announced = Array.isArray(selection?.selected);
      if (timedOut || announced) {
        resolve({ unanswered: true, enforce: announced ? selection.enforce !== false : true });
        return;
      }
      resolve(null);
    });
    child.stdin!.end(JSON.stringify(envelope));
  });
}

export default function nxthooks(pi: any) {
  let lastPreflightReason: string | null = null;
  let sessionEnded = false;

  pi.on("session_start", async (_event: any, ctx: any) => {
    lastPreflightReason = null;
    sessionEnded = false;
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

  pi.on("session_shutdown", async (event: any, ctx: any) => {
    if (sessionEnded) return;
    sessionEnded = true;
    try {
      await callDispatcher({
        version: 1,
        harness: "pi",
        event: "session_end",
        cwd: ctx?.cwd ?? process.cwd(),
        session_id: sessionIdOf(ctx),
        reason: String(event?.reason ?? "shutdown"),
      });
    } catch {
      // Session cleanup is advisory at pi's shutdown boundary.
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
    const envelope = {
      version: 1,
      harness: "pi",
      event: "before_tool",
      cwd: ctx?.cwd ?? process.cwd(),
      tool_call_id: event?.toolCallId ?? "",
      session_id: sessionIdOf(ctx),
      tool: event?.toolName ?? "",
      args: event?.input ?? {},
    };
    let outcome = await callDispatcher(
      envelope, ["--deadline-ms", String(Math.max(1000, DISPATCH_TIMEOUT_MS - DEADLINE_MARGIN_MS))],
    ).catch(() => null);
    // No reply at all: a dispatcher older than this adapter rejects --deadline-ms as a usage error (exit 1, empty
    // stdout), which read as null, and null allowed with no hook run. Ask once more the way it understands; a
    // dispatcher that still gives nothing is broken, and a broken guard layer refuses rather than waves calls through.
    if (outcome === null) {
      outcome = await callDispatcher(envelope).catch(() => null);
      if (outcome === null) {
        return {
          block: true,
          reason: "[nxthooks] the hook dispatcher gave no answer for this call, so it is refused rather than run " +
            "unchecked. Run: nxthooks install",
        };
      }
    }

    // Silence from a dispatcher that was running a guard is not consent: on
    // 2026-10-08 nxtgit-guard vetoed a write, a hung advise hook kept the
    // dispatcher past this timeout, and the write landed. The dispatcher now
    // answers inside its deadline, so reaching here means something is
    // genuinely wrong -- refuse loudly rather than allow unchecked.
    if (outcome?.unanswered) {
      if (!outcome.enforce) return;
      return {
        block: true,
        reason: `[nxthooks] the hook dispatcher did not answer within ${DISPATCH_TIMEOUT_MS}ms and a guard ` +
          "applies to this call, so it is refused rather than run unchecked. Retry once; if it repeats, run: nxthooks doctor",
      };
    }
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
      tool_call_id: event?.toolCallId ?? "",
      success: typeof event?.isError === "boolean" ? !event.isError : null,
      session_id: sessionIdOf(ctx),
      tool: event?.toolName ?? "",
      args: event?.input ?? {},
    }).catch(() => null);
    // fail-quiet: an observability hook must never break the turn
  });
}

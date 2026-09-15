# NxtHooks

One hook directory, five agent harnesses.

Write a hook once as a script that reads a JSON envelope on stdin. NxtHooks
runs it on Claude Code, Codex, OpenCode, pi and Hermes, translating each
harness's own event names and verdict shapes to and from one contract:

    exit 0   allow   (stdout `CTX:` lines become context injected into the turn)
    exit 2   block   (stderr is the reason the model is shown)

```console
$ pip install git+https://github.com/io-whtnxt/nxthooks   # not on PyPI yet
$ nxthooks install
$ nxthooks list
NAME         TIER     EVENTS       MATCHERS                            POLICY
nxtlint      builtin  after_tool   Write,Edit,MultiEdit,NotebookEdit   advise
secret-scan  builtin  before_tool  Write,Edit,MultiEdit,NotebookEdit   enforce
```

Two hooks ship with it: a Python linter that comments on files an agent just
wrote, and a secret scanner that refuses a write carrying a credential.

## Events

`session_start`, `prompt_submit`, `before_tool`, `after_tool`, `stop`,
`session_end` -- canonical names, mapped per harness. Not every harness
provides every one; `nxthooks verify` prints the capability matrix for the
machine it runs on rather than a general claim.

## Tiers

`builtin < library < user`, with one deliberate inversion: a user manifest
overrides a builtin, but **cannot** replace a library hook. A library hook is
administered software and `hooks.d/` is an ordinary writable directory, so a
security hook whose implementation can be swapped by dropping a file beside
it would not be a security hook. Collisions are reported, never silently
resolved.

## Status

Beta. What has actually been observed, rather than what the design intends:

| Harness | Dispatch observed | Blocking verified |
|---|---|---|
| Claude Code | all tool events, live | live |
| Codex | all tool events, live | live |
| OpenCode | all tool events, live | live |
| pi | all tool events, live | live |
| Hermes | `session_start`, `prompt_submit` only | through its own dispatcher, not a live turn |

The Hermes caveat is worth stating plainly. Its transport was driven through
`hermes_cli.plugins._dispatch_pre_tool_call_hooks` on hermes-agent 0.21.1,
which returned the block message, and its `session_start` and `prompt_submit`
hooks were seen firing in a real session. But no Hermes turn has yet reached
a tool call with these hooks installed, so `before_tool` and `after_tool`
there are verified one layer below the harness rather than end to end. Treat
Hermes tool hooks as working-by-construction until someone reports otherwise.

`nxthooks verify` prints the capability matrix for the machine it runs on,
which is the claim that matters to you rather than any of the above.

## Licence

Apache-2.0. Generated from the NxtOS source tree; see NOTICE.

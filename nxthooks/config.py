"""Configuration loading and precedence for NxtHooks.

Precedence (highest wins): CLI flags > NXTHOOKS_* env > project
(.nxt/hooks.toml walking up from cwd) > user (~/.config/nxt/hooks/config.toml)
> built-in defaults. Only documented keys are honored; unknown keys are
ignored so forward-compatible configs never break a running hook.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import HARNESSES, POLICIES
from .platform import config_dir, data_dir

DEFAULTS = {
    "meta": {"version": 1},
    "install": {
        "root": None,   # resolved per platform; see resolve_root
    },
    "policy": {"default": "enforce"},
    "scope": {"include": [], "exclude": []},
    # A record of what `install` last detected and wired, never an
    # instruction to wire it. Selection is `projection.detect`'s: a config
    # that says `pi = true` on a machine with no pi is a stale note, and
    # honouring it is how install came to manufacture `~/.pi` on machines
    # that had never seen pi. Defaults are all false so a config-less
    # machine claims nothing it has not been observed to have.
    "harnesses": {harness: False for harness in HARNESSES},
}


def user_config_path(env: dict | None = None,
                     home: Path | str | None = None) -> Path:
    return config_dir(env, home) / "config.toml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        print(f"[nxthooks] invalid TOML in {path}: {exc}", flush=True)
        return {}


def find_project_config(cwd: str | None = None) -> Path | None:
    cur = Path(cwd or os.getcwd()).resolve()
    for candidate in [cur, *cur.parents]:
        probe = candidate / ".nxt" / "hooks.toml"
        if probe.is_file():
            return probe
    return None


def load_config(cwd: str | None = None, env: dict | None = None,
                home: Path | str | None = None) -> dict:
    cfg = DEFAULTS
    cfg = _deep_merge(cfg, _load_toml(user_config_path(env, home)))
    project = find_project_config(cwd)
    if project:
        cfg = _deep_merge(cfg, _load_toml(project))
    env = os.environ if env is None else env
    if env.get("NXTHOOKS_ROOT"):
        cfg["install"]["root"] = env["NXTHOOKS_ROOT"]
    if env.get("NXTHOOKS_POLICY"):
        cfg["policy"]["default"] = env["NXTHOOKS_POLICY"]
    validate_config(cfg)
    return cfg


def resolve_root(cfg: dict, env: dict | None = None,
                 home: Path | str | None = None) -> Path:
    """An unset root means "wherever this platform puts application data".

    `~` in a configured root expands against `home` when one is given, so a
    sandbox HOME and a packaged install are the same code path rather than
    two. `os.path.expanduser` would consult the process's real HOME and send
    a sandboxed install into the operator's own tree.
    """
    root = cfg.get("install", {}).get("root")
    if not root:
        return data_dir(env, home)
    text = str(root)
    if home is not None and (text == "~" or text.startswith("~/")):
        return Path(home) / text[2:] if text.startswith("~/") else Path(home)
    return Path(os.path.expanduser(text))


def scope_allows(cfg: dict, cwd: str) -> bool:
    import fnmatch

    include = cfg.get("scope", {}).get("include") or []
    exclude = cfg.get("scope", {}).get("exclude") or []
    norm = os.path.realpath(os.path.expanduser(cwd)) if cwd else ""
    if include and not any(fnmatch.fnmatch(norm, os.path.expanduser(p)) for p in include):
        return False
    if any(fnmatch.fnmatch(norm, os.path.expanduser(p)) for p in exclude):
        return False
    return True


CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "policy": {"type": "object", "properties": {"default": {"enum": list(POLICIES)}}},
        "scope": {"type": "object", "properties": {key: {"type": "array", "items": {"type": "string"}} for key in ("include", "exclude")}},
        "harnesses": {"type": "object", "additionalProperties": {"type": "boolean"}},
    },
}


def validate_config(document):
    """Shared validation for native loading and reviewed NxtPack changes."""
    if not isinstance(document, dict):
        raise ValueError("NxtHooks configuration must be a mapping")
    for section in ("policy", "scope", "harnesses"):
        if section in document and not isinstance(document[section], dict):
            raise ValueError(f"NxtHooks {section} must be a mapping")
    if document.get("policy", {}).get("default", "enforce") not in POLICIES:
        raise ValueError("Invalid NxtHooks policy")
    for name in ("include", "exclude"):
        value = document.get("scope", {}).get(name, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("NxtHooks scope requires lists of strings")
    if any(type(value) is not bool for value in document.get("harnesses", {}).values()):
        raise ValueError("NxtHooks harness settings must be booleans")

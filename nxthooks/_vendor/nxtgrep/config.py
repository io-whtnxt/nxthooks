"""Load and validate an operator's NxtGrep configuration.

Conforms to the NxtOS component-config spec:
canonical config at `paths.config_dir("grep")/config.toml`, [meta] version block,
policy classes (enforce | advise | off), env-var override above user config,
stdlib-only runtime path.

Stdlib-only is deliberate: this module is imported by the spawned hook
executable and by every harness adapter, under whatever interpreter each of
those finds — not necessarily one holding the project's dependencies. A
user-site package vanishing because HOME differs (the exact failure that
forced this rule) must not be able to take the gate down. tomllib is stdlib
since 3.11.

nxtos-nxtgrep-config.schema.json remains the shape's single authority,
enforced by tests against this loader's accepted/rejected inputs — the runtime
cannot drift from the schema without a test failing.
"""
import os

try:
    import tomllib
except ImportError:  # pragma: no cover - guarded at load_config
    tomllib = None
from dataclasses import dataclass, field
from pathlib import Path

from nxthooks._vendor import paths

DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_REPORTED_HITS = 5

POLICIES = ("enforce", "advise", "off")


@dataclass(frozen=True)
class GrepConfig:
    policy: str = "enforce"
    builtin_enabled: bool = True
    user_patterns_file: Path | None = None  # None = no user pack installed
    extra_files: tuple[Path, ...] = field(default_factory=tuple)
    max_bytes: int = DEFAULT_MAX_BYTES
    max_reported_hits: int = DEFAULT_MAX_REPORTED_HITS

    def pack_files(self) -> tuple[Path, ...]:
        """Packs in scan order: user pack first, extras after. The builtin
        pack is not listed here — it lives beside the hook executable and is
        gated by `builtin_enabled`, not by this list."""
        packs: list[Path] = []
        if self.user_patterns_file is not None:
            packs.append(self.user_patterns_file)
        packs.extend(self.extra_files)
        return tuple(packs)


def config_path() -> Path:
    """Where this installation's config lives. Env override sits above user
    config in the spec's precedence chain (flags > env > user > defaults).

    Resolved rather than spelled, and resolved lazily: a module-level
    `Path.home()` would bake the packaging machine's home into an installed
    copy of this file.
    """
    override = os.environ.get("NXTGREP_CONFIG")
    if override:
        return Path(override).expanduser()
    return paths.resolve("grep", "config", "config.toml")


def default_user_patterns() -> Path:
    """The zero-setup location for an operator's own pattern pack."""
    return paths.resolve("grep", "config", "patterns.conf")


def _reject(path: Path, message: str):
    raise ValueError(f"{path}: {message}")


def load_config(path: Path | None = None) -> GrepConfig:
    """Read the config file, or fall back to defaults when absent.

    Raises ValueError naming path and field on anything invalid. Callers treat
    a raised error as fail-closed refusal (when policy enforces): a typo can
    never silently downgrade the gate to "no rules". The one soft case is
    policy = "off", which is an operator decision, never an accident.
    """
    path = path if path is not None else config_path()
    if not path.is_file():
        # No config file still honors the default user-pack location: dropping
        # a patterns.conf there without ever writing a config is the intended
        # zero-setup flow.
        return GrepConfig(user_patterns_file=default_user_patterns())

    if tomllib is None:
        _reject(path, "this interpreter has no tomllib (python3 >= 3.11 required). NxtGrep pins one at install time; re-run scripts/nxtgrep-install.py install")
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _reject(path, f"cannot read config: {exc}")
    if not isinstance(raw, dict):
        _reject(path, "config must be a TOML table")

    known = {"meta", "policy", "packs", "scan"}
    unknown = set(raw) - known
    if unknown:
        _reject(path, f"unknown table(s): {', '.join(sorted(unknown))}")

    version = (raw.get("meta") or {}).get("version", 1)
    if version != 1:
        _reject(path, f"unsupported meta.version {version!r}")

    policy = raw.get("policy", "enforce")
    if isinstance(policy, dict):
        unknown = set(policy) - {"mode"}
        if unknown:
            _reject(path, f"unknown policy field(s): {', '.join(sorted(unknown))}")
        policy = policy.get("mode", "enforce")
    if policy not in POLICIES:
        _reject(path, f"policy must be one of {', '.join(POLICIES)}, got {policy!r}")

    packs_raw = raw.get("packs") or {}
    if not isinstance(packs_raw, dict):
        _reject(path, "packs must be a table")
    unknown = set(packs_raw) - {"builtin", "user_patterns_file", "extra_files"}
    if unknown:
        _reject(path, f"unknown packs field(s): {', '.join(sorted(unknown))}")

    builtin_enabled = packs_raw.get("builtin", True)
    if not isinstance(builtin_enabled, bool):
        _reject(path, "packs.builtin must be a boolean")

    extras_raw = packs_raw.get("extra_files") or []
    if not isinstance(extras_raw, list) or not all(
        isinstance(e, str) and e.strip() for e in extras_raw
    ):
        _reject(path, "packs.extra_files must be a list of non-empty strings")

    scan_raw = raw.get("scan") or {}
    if not isinstance(scan_raw, dict):
        _reject(path, "scan must be a table")
    unknown = set(scan_raw) - {"max_bytes", "max_reported_hits"}
    if unknown:
        _reject(path, f"unknown scan field(s): {', '.join(sorted(unknown))}")

    def _positive(field_name: str, default: int) -> int:
        value = scan_raw.get(field_name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            _reject(path, f"scan.{field_name} must be an integer >= 1")
        return value

    max_bytes = max(_positive("max_bytes", DEFAULT_MAX_BYTES), 1024)
    max_reported_hits = _positive("max_reported_hits", DEFAULT_MAX_REPORTED_HITS)

    # Absent key means the documented default location — a user who drops a
    # patterns.conf there without ever writing a config still gets it. An
    # explicit empty string disables the user pack.
    raw_user = packs_raw.get("user_patterns_file")
    if raw_user is None:
        user_file: Path | None = default_user_patterns()
    elif isinstance(raw_user, str) and raw_user.strip():
        user_file = Path(raw_user).expanduser()
    elif raw_user == "":
        user_file = None
    else:
        _reject(path, "packs.user_patterns_file must be a non-empty string or \"\"")

    extras = tuple(
        Path(entry).expanduser() for entry in extras_raw
    )

    return GrepConfig(
        policy=policy,
        builtin_enabled=builtin_enabled,
        user_patterns_file=user_file,
        extra_files=extras,
        max_bytes=max_bytes,
        max_reported_hits=max_reported_hits,
    )

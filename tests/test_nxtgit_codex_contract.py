"""Keep Codex/NxtGit protections owned by the architecture that actually
provides them.

This file used to pin `nxtgit-route.toml` and `nxtgit-guard.toml` inside
`nxthooks/lifecycle.py`, which is the coupling N2-c removes: NxtHooks was
seeding manifests for an implementation it does not ship, so on any machine
without NxtGit the guards resolved to nothing while `verify` reported gaps
and `doctor` reported them as having run. NxtHooks still provides the Codex
*transport*; the manifests come with NxtGit's own install.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nxthooks_provides_the_codex_transport_and_seeds_no_manifests():
    lifecycle = (ROOT / "nxthooks" / "lifecycle.py").read_text(encoding="utf-8")
    # The manual fallback is prose in the capability matrix, which lifecycle
    # prints but no longer holds.
    capability = (ROOT / "nxthooks" / "capability.py").read_text(encoding="utf-8")

    assert "codex-dispatch.sh" in lifecycle
    assert "nxtgit check" in capability
    assert '"nxtgit-route.toml"' not in lifecycle
    assert '"nxtgit-guard.toml"' not in lifecycle

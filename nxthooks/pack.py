"""Native NxtPack configuration provider for NxtHooks.

Reads a ``{"action", "document"}`` request on stdin and answers with the
schema-declared editable fields plus a revision that changes whenever the
schema or the code behind it changes. Never writes configuration.
"""
import hashlib
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Invoked by absolute script path, so `nxthooks` is not importable yet:
    # drop this file's own directory and put the package's parent first.
    sys.path = [entry for entry in sys.path
                if Path(entry or ".").resolve() != Path(__file__).resolve().parent]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nxthooks.config import CONFIG_SCHEMA, validate_config

REVISION_SOURCES = ("__init__.py", "config.py", "pack.py")

# Harness flags are what `install` last detected, not registration switches,
# so they are deliberately absent: NxtPack must not offer them as edits.
EDITABLE_FIELDS = (
    (("policy", "default"), "enforce",
     CONFIG_SCHEMA["properties"]["policy"]["properties"]["default"]),
    (("scope", "include"), [], {"collection": "array", "item_template": ""}),
    (("scope", "exclude"), [], {"collection": "array", "item_template": ""}),
)


def revision():
    digest = hashlib.sha256(json.dumps(CONFIG_SCHEMA, sort_keys=True).encode())
    for name in REVISION_SOURCES:
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def editable_fields(document):
    fields = []
    for (section, key), default, hints in EDITABLE_FIELDS:
        values = document.get(section, {})
        fields.append({
            "path": [section, key],
            "present": key in values,
            "suggested": values.get(key, default),
            **hints,
        })
    return fields


def handle(request):
    if set(request) != {"action", "document"} or request["action"] not in ("describe", "validate"):
        raise ValueError("Unsupported native configuration request")
    validate_config(request["document"])
    result = {"revision": revision(), "valid": True}
    if request["action"] == "describe":
        result["fields"] = editable_fields(request["document"])
    return result


def main():
    try:
        request = json.loads(sys.stdin.read(4_000_001))
        print(json.dumps({"ok": True, **handle(request)}))
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": f"Native Hooks configuration rejected: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

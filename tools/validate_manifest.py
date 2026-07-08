#!/usr/bin/env python3
"""validate-manifest: CI gate for services.yaml.

Usage: python tools/validate_manifest.py [path/to/services.yaml]
Exit 0 on valid config (prints summary), non-zero with readable errors otherwise.
Also verifies referenced fixture files exist and parse, and instantiates the
registry in mock mode to catch adapter-level misconfiguration.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.registry.config import Mode, load_services  # noqa: E402
from agent.registry.registry import ToolRegistry  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "services.yaml").resolve()
    base = path.parent
    try:
        cfg = load_services(path)
    except Exception as e:
        print(f"INVALID: {path}\n{e}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for svc in cfg.services:
        if svc.mock.fixtures:
            fp = base / svc.mock.fixtures
            if not fp.exists():
                errors.append(f"{svc.id}: fixture file missing: {fp}")
            else:
                try:
                    json.loads(fp.read_text())
                except Exception as e:
                    errors.append(f"{svc.id}: fixture not valid JSON: {e}")

    # Build the registry for mock AND live configs — live adapters construct
    # without any network I/O (secrets/transports resolve lazily at call time),
    # so this still catches namespace clashes, bad kinds, and tool-gen errors.
    try:
        reg = ToolRegistry(services=cfg, base_dir=base)
        specs = reg.tool_specs()
    except Exception as e:
        errors.append(f"registry failed to build: {e}")
        specs = []
    if any(s.mode != Mode.mock for s in cfg.services):
        print("note: live-mode services present; adapters validated (not called)")

    if errors:
        print("INVALID:\n  - " + "\n  - ".join(errors), file=sys.stderr)
        return 1

    print(f"OK: tenant={cfg.tenant} services={len(cfg.services)} tools={len(specs)}")
    print(f"ui_blocks: {', '.join(cfg.enabled_ui_blocks())}")
    for t in specs:
        marker = " [write]" if t.write else ""
        print(f"  {t.name}{marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

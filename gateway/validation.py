"""Envelope validation — the gateway's enforcement of the UI contract.

Validates agent output against contracts/envelope.schema.json and per-block
props schemas from contracts/ui-blocks.v1.schema.json. Policy:

  - message (text) ALWAYS survives; a malformed envelope degrades to plain text.
  - Blocks with unknown types, invalid props, or types not enabled for the
    tenant are DROPPED (never forwarded) and reported for telemetry.
  - Events with invalid names are dropped likewise.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

_BLOCK_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
_BLOCK_ID_RE = re.compile(r"^blk_[A-Za-z0-9_-]{1,32}$")


@dataclass
class ValidationReport:
    dropped_blocks: list[dict] = field(default_factory=list)
    dropped_events: list[dict] = field(default_factory=list)
    degraded: bool = False  # true when the whole envelope was malformed


@dataclass
class EnvelopeValidator:
    contracts_dir: Path
    _block_validators: dict[str, Draft202012Validator] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = json.loads((self.contracts_dir / "ui-blocks.v1.schema.json").read_text())
        for block_type, schema in spec["blocks"].items():
            self._block_validators[block_type] = Draft202012Validator(schema)

    @property
    def known_block_types(self) -> set[str]:
        return set(self._block_validators)

    def validate(self, raw: object, enabled_blocks: list[str]) -> tuple[dict, ValidationReport]:
        report = ValidationReport()

        if not isinstance(raw, dict) or not isinstance(raw.get("message"), str):
            # Whole-envelope failure: degrade to whatever text we can salvage.
            report.degraded = True
            text = raw.get("message") if isinstance(raw, dict) else None
            return {"message": text or "Sorry — something went wrong generating that response.",
                    "ui_blocks": [], "events": []}, report

        clean_blocks: list[dict] = []
        for i, block in enumerate(raw.get("ui_blocks") or []):
            reason = self._block_problem(block, enabled_blocks)
            if reason:
                report.dropped_blocks.append({"index": i, "reason": reason,
                                              "type": block.get("type") if isinstance(block, dict) else None})
            else:
                clean_blocks.append({"type": block["type"], "id": block["id"], "props": block["props"]})
            if len(clean_blocks) >= 6:
                break

        clean_events: list[dict] = []
        for i, ev in enumerate(raw.get("events") or []):
            if (isinstance(ev, dict) and isinstance(ev.get("name"), str)
                    and _EVENT_NAME_RE.match(ev["name"])
                    and isinstance(ev.get("props", {}), dict)):
                clean_events.append({"name": ev["name"], "props": ev.get("props", {})})
            else:
                report.dropped_events.append({"index": i})
            if len(clean_events) >= 10:
                break

        return {"message": raw["message"], "ui_blocks": clean_blocks, "events": clean_events}, report

    def _block_problem(self, block: object, enabled: list[str]) -> str | None:
        if not isinstance(block, dict):
            return "not_an_object"
        btype = block.get("type")
        if not isinstance(btype, str) or not _BLOCK_TYPE_RE.match(btype):
            return "bad_type_shape"
        if btype not in self._block_validators:
            return "unknown_type"
        if btype not in enabled:
            return "type_not_enabled_for_tenant"
        if not isinstance(block.get("id"), str) or not _BLOCK_ID_RE.match(block["id"]):
            return "bad_block_id"
        props = block.get("props")
        if not isinstance(props, dict):
            return "props_not_object"
        errors = list(self._block_validators[btype].iter_errors(props))
        if errors:
            return f"props_invalid: {errors[0].message[:120]}"
        return None

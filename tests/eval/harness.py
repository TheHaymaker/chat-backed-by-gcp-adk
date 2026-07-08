"""Structural eval harness.

Executes declarative YAML cases (tests/eval/cases/*.yaml) through the FULL
gateway pipeline — session auth, interaction-token minting, envelope
validation — against any AgentRunner. Today that's the MockAgentRunner; point
it at AdkRunner (gateway/adk_runner.py) to run the same invariants against the
real Gemini agent. The LLM-quality layer (response match, judge criteria) runs
separately via `agents-cli eval` using the exported ADK evalsets.

Case turn shapes:
  - user: "<message>"                       # a chat turn
  - interact: {action, from_block, payload|payload_from}   # a UI tap
Expectations per turn:
  block_types        exact ordered list of rendered block types
  not_block_types    none of these may appear
  message_contains   substring of the assistant message ("" = non-empty)
  events_include     event names that must land in the sink this turn
  no_write_events    no booking_completed/booking_cancelled/booking_rescheduled/
                     lead_captured events this turn
  kb_citations_min   minimum citations on the kb_answer block
Implicit for every turn: the envelope was NOT sanitized/degraded by the
gateway validator (schema validity is a hard gate).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from gateway.main import build_state, create_app
from gateway.runner import MemorySink

WRITE_EVENTS = {"booking_completed", "booking_cancelled",
                "booking_rescheduled", "lead_captured"}
ORIGIN = {"Origin": "http://localhost:3000"}


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def load_cases(cases_dir: Path) -> list[dict]:
    cases = []
    for path in sorted(cases_dir.glob("*.yaml")):
        for case in yaml.safe_load(path.read_text()) or []:
            case["_file"] = path.name
            cases.append(case)
    return cases


class Harness:
    def __init__(self, services_path: Path, runner=None):
        self.services_path = services_path
        self.runner_factory = runner  # optional callable(registry) -> AgentRunner

    def _fresh_client(self):
        sink = MemorySink()
        state = build_state(services_path=self.services_path, sink=sink,
                            allowed_origins={"http://localhost:3000"})
        if self.runner_factory:
            state.runner = self.runner_factory(state.registry)
        return TestClient(create_app(state)), sink

    # -- turn execution -----------------------------------------------------
    @staticmethod
    def _envelopes(resp) -> list[dict]:
        return [json.loads(l[6:]) for l in resp.text.splitlines()
                if l.startswith("data: ") and l != "data: [DONE]"]

    @staticmethod
    def _build_payload(spec: dict, last_env: dict, block: dict) -> dict:
        if "payload" in spec:
            return spec["payload"]
        src = spec.get("payload_from")
        if src in ("first_slot", "second_slot"):
            idx = 0 if src == "first_slot" else 1
            return {"slot_id": block["props"]["slots"][idx]["slot_id"]}
        if src == "option_0":
            return block["props"]["options"][0].get("payload", {})
        raise ValueError(f"unknown payload_from: {src}")

    def run_case(self, case: dict) -> CaseResult:
        client, sink = self._fresh_client()
        r = client.post("/v1/session", json={"tenant": "acme"}, headers=ORIGIN)
        headers = {"Authorization": f"Bearer {r.json()['session_token']}", **ORIGIN}
        result = CaseResult(case_id=case["id"], passed=True)
        last_env: dict = {}

        for n, turn in enumerate(case.get("turns", []), 1):
            events_before = len(sink.events)
            if "user" in turn:
                resp = client.post("/v1/chat", json={"message": turn["user"]},
                                   headers=headers)
            else:
                spec = turn["interact"]
                block = next((b for b in last_env.get("ui_blocks", [])
                              if b["type"] == spec["from_block"]), None)
                if block is None:
                    result.failures.append(
                        f"turn {n}: no {spec['from_block']} block to interact with")
                    break
                resp = client.post("/v1/interact", json={
                    "action": spec["action"], "block_id": block["id"],
                    "payload": self._build_payload(spec, last_env, block),
                }, headers=headers)

            envs = self._envelopes(resp)
            if not envs:
                result.failures.append(f"turn {n}: no envelope streamed")
                break
            last_env = envs[-1]
            turn_events = [e["name"] for e in sink.events[events_before:]]
            self._check(turn.get("expect", {}), last_env, turn_events, n, result)

        result.passed = not result.failures
        return result

    def _check(self, expect: dict, env: dict, events: list[str],
               n: int, result: CaseResult) -> None:
        types = [b["type"] for b in env.get("ui_blocks", [])]
        fail = result.failures.append

        if "envelope_sanitized" in events:
            fail(f"turn {n}: gateway had to sanitize the envelope (schema violation)")
        if "block_types" in expect and types != expect["block_types"]:
            fail(f"turn {n}: blocks {types} != expected {expect['block_types']}")
        for bad in expect.get("not_block_types", []):
            if bad in types:
                fail(f"turn {n}: forbidden block '{bad}' rendered")
        if "message_contains" in expect:
            needle = expect["message_contains"]
            msg = env.get("message", "")
            if needle == "" and not msg.strip():
                fail(f"turn {n}: empty assistant message")
            elif needle and needle.lower() not in msg.lower():
                fail(f"turn {n}: message missing '{needle}'")
        for name in expect.get("events_include", []):
            if name not in events:
                fail(f"turn {n}: expected event '{name}' not emitted")
        if expect.get("no_write_events") and (hit := WRITE_EVENTS & set(events)):
            fail(f"turn {n}: write events without user tap: {sorted(hit)}")
        if "kb_citations_min" in expect:
            kb = next((b for b in env["ui_blocks"] if b["type"] == "kb_answer"), None)
            if kb is None or len(kb["props"].get("citations", [])) < expect["kb_citations_min"]:
                fail(f"turn {n}: kb_answer citations below minimum")

    def run_all(self, cases_dir: Path) -> list[CaseResult]:
        return [self.run_case(c) for c in load_cases(cases_dir)]

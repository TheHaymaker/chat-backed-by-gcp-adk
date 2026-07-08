#!/usr/bin/env python3
"""Export tests/eval/cases/*.yaml to ADK evalset JSON for `agents-cli eval`.

Structural invariants (schema validity, token safety) stay in harness.py, which
is authoritative. The exported evalsets add the LLM-quality layer against the
real agent: response relevance + tool-trajectory expectations, scored per
tests/eval/eval_config.json. Regenerate after editing cases; pin your ADK
version, as the evalset schema is still evolving.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.eval.harness import load_cases  # noqa: E402

# Which tool the real agent is expected to call for each expected block type.
BLOCK_TOOL_HINTS = {
    "scheduler": "sales-scheduler.get_availability",
    "faq_card": "help-center.lookup",
    "kb_answer": "docs-kb.retrieve",
    "search_results": "site-search.search",
}


def to_eval_case(case: dict) -> dict:
    conversation = []
    for turn in case.get("turns", []):
        if "user" not in turn:      # interaction turns are harness-only
            continue
        expect = turn.get("expect", {})
        tools = [{"name": BLOCK_TOOL_HINTS[b], "args": {}}
                 for b in expect.get("block_types", []) if b in BLOCK_TOOL_HINTS]
        conversation.append({
            "invocation_id": f"{case['id']}-{len(conversation)}",
            "user_content": {"role": "user",
                             "parts": [{"text": turn["user"]}]},
            "intermediate_data": {"tool_uses": tools},
            "final_response": {"role": "model", "parts": [{"text": ""}]},
        })
    return {"eval_id": case["id"], "conversation": conversation,
            "session_input": {"app_name": "web-chat-agent", "user_id": "eval",
                              "state": {}}}


def main() -> int:
    out_dir = ROOT / "tests" / "eval" / "adk"
    out_dir.mkdir(exist_ok=True)
    by_file: dict[str, list] = {}
    for case in load_cases(ROOT / "tests" / "eval" / "cases"):
        by_file.setdefault(case["_file"].replace(".yaml", ""), []).append(case)
    for name, cases in by_file.items():
        evalset = {
            "eval_set_id": f"webchat_{name}",
            "name": f"web-chat-agent {name} evals",
            "eval_cases": [to_eval_case(c) for c in cases],
            "creation_timestamp": time.time(),
        }
        path = out_dir / f"{name}.evalset.json"
        path.write_text(json.dumps(evalset, indent=2))
        print(f"wrote {path.relative_to(ROOT)} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

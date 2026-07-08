"""adk_app — the module `adk` CLI targets (adk web / api_server / deploy).

Layout requirement: the ADK CLI discovers `root_agent` in an agent package.
Run from repo root:
    adk web agent/            # dev playground
    adk api_server agent/ --port 8080   # what AdkRunner talks to
    adk deploy agent_engine agent/adk_app ...   # managed runtime

The instruction encodes the same routing policy the MockAgentRunner mirrors,
plus the interaction protocol used by gateway/adk_runner.py. Registry tools,
prompt section, and plugins come from the same builders as everywhere else.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.adk.agents import Agent          # noqa: E402
from google.adk.tools import FunctionTool    # noqa: E402

from agent.app.app import BASE_INSTRUCTION, build_registry  # noqa: E402

_registry = build_registry()

INTERACTION_PROTOCOL = """
## Interaction protocol
Messages beginning with `[interaction]` are verified user taps on UI blocks,
forwarded by the gateway as JSON: {action, block_id, payload, interaction_token}.
- For action `slot_selected`: call hold_slot then confirm_booking, passing
  interaction_token VERBATIM to both; respond with a `confirmation` block.
- For action `cancel_booking`: call cancel_booking with the payload booking_ref
  and the interaction_token; confirm in text.
- For action `form_submitted`: call capture_lead with payload values and the
  interaction_token; thank the user with the lead reference.
NEVER call any write tool (hold_slot, confirm_booking, cancel_booking,
capture_lead) outside an [interaction] turn, and NEVER invent an
interaction_token — tokens are cryptographically verified downstream.

## Routing policy
cancel/reschedule intents -> offer quick_replies with action fields (cancel)
or a fresh scheduler (reschedule). contact/human -> `form` + `handoff` blocks.
booking intents -> get_availability then a `scheduler` block. navigational
("find", "where is") -> site search -> `search_results`. Otherwise FAQ lookup
first; strong match -> `faq_card`. Else knowledge-base retrieve -> `kb_answer`
citing every source chunk; if retrieval returns nothing, say you could not
find it in the documentation — never answer doc questions from memory.
Respond ONLY with the JSON envelope. No prose outside the JSON.
"""


def _tools() -> list:
    tools = []
    for spec in _registry.tool_specs():
        fn = spec.fn
        fn.__doc__ = spec.description + (
            " REQUIRES interaction_token from a verified user tap." if spec.write else "")
        tools.append(FunctionTool(fn))
    return tools


root_agent = Agent(
    name="web_chat_root",
    model=os.environ.get("MODEL", "gemini-flash-latest"),
    instruction=BASE_INSTRUCTION.format(
        services_section=_registry.prompt_section()) + INTERACTION_PROTOCOL,
    tools=_tools(),
)

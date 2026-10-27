"""Create the Coval agent that Remy connects to, with coval-sdk.

An Agent SDK agent has the SDK WebSocket connection type. It has no endpoint or phone number:
the agent process dials out to Coval for each conversation.

    python coval_setup.py
    python coval_setup.py --copy-from <agent-id>   # take prompt and metrics from another agent

Prints the agent ID to put in COVAL_AGENT_ID. Reuses an existing agent with the same name.
"""

import argparse
import os

from coval_sdk import CovalAgentsAPICreateAgentRequest, CovalClient, paginate
from dotenv import load_dotenv

from remy.hospitality import PROMPT

NAME = "Remy — Hospitality Voice Agent (Agent SDK)"


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default=NAME)
    parser.add_argument("--copy-from", help="Agent to copy the prompt and metrics from")
    args = parser.parse_args()
    workspace_id = os.environ["COVAL_WORKSPACE_ID"]

    with CovalClient(os.environ["COVAL_API_KEY"]) as coval:
        for agent in paginate(
            coval.agents.list_agents,
            items_field="agents",
            page_size=100,
            x_coval_workspace_id=workspace_id,
        ):
            if agent.display_name == args.name and agent.model_type == "MODEL_TYPE_SDK_WEBSOCKET":
                print(f"Using existing agent {agent.id}\nCOVAL_AGENT_ID={agent.id}")
                return

        prompt, metric_ids = PROMPT, None
        if args.copy_from:
            source = coval.agents.get_agent(args.copy_from).agent
            prompt, metric_ids = source.prompt or PROMPT, source.metric_ids
        agent = coval.agents.create_agent(
            CovalAgentsAPICreateAgentRequest(
                display_name=args.name,
                model_type="MODEL_TYPE_SDK_WEBSOCKET",
                prompt=prompt,
                metric_ids=metric_ids,
                tags=["stack:agent-sdk"],
            ),
            x_coval_workspace_id=workspace_id,
        ).agent
        print(f"Created agent {agent.id}\nCOVAL_AGENT_ID={agent.id}")


if __name__ == "__main__":
    main()

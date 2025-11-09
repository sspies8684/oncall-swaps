from __future__ import annotations

import logging
import os
import sys
from typing import List

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from oncall_swap.adapters.opsgenie.client import OpsgenieClient
from oncall_swap.adapters.slack.bot import SlackBotAdapter
from oncall_swap.application.services import SwapNegotiationService
from oncall_swap.infrastructure.directory.in_memory import InMemoryParticipantDirectory
from oncall_swap.infrastructure.persistence.in_memory import InMemoryOfferRepository
from oncall_swap.adapters.opsgenie.mock import MockOpsgenieClient

REQUIRED_ENV_VARS = [
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_APP_TOKEN",
    "OPSGENIE_API_KEY",
    "OPSGENIE_SCHEDULE_ID",
]


def _missing_env(vars_to_check: List[str]) -> List[str]:
    return [name for name in vars_to_check if not os.environ.get(name)]


def main() -> None:
    """
    Launch the on-call swap Slack bot.

    Required environment variables:
    - SLACK_BOT_TOKEN
    - SLACK_SIGNING_SECRET
    - SLACK_APP_TOKEN             (Socket Mode)
    - OPSGENIE_API_KEY
    - OPSGENIE_SCHEDULE_ID

    Optional:
    - SLACK_ANNOUNCEMENT_CHANNEL (default: #oncall-swaps)
    """

    missing = _missing_env(REQUIRED_ENV_VARS)
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing required environment variables: {joined}")

    announcement_channel = os.environ.get("SLACK_ANNOUNCEMENT_CHANNEL", "#oncall-swaps")

    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    # opsgenie = OpsgenieClient(api_key=os.environ["OPSGENIE_API_KEY"], directory=directory)
    opsgenie = MockOpsgenieClient()
    schedule_id = os.environ["OPSGENIE_SCHEDULE_ID"]

    service = SwapNegotiationService(
        repository=repository,
        directory=directory,
        schedule_port=opsgenie,
        override_port=opsgenie,
    )

    slack_app = App(token=os.environ["SLACK_BOT_TOKEN"], signing_secret=os.environ["SLACK_SIGNING_SECRET"])
    SlackBotAdapter(
        app=slack_app,
        negotiation_service=service,
        announcement_channel=announcement_channel,
        schedule_id=schedule_id,
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.info("Starting on-call swap bot for schedule '%s'", os.environ["OPSGENIE_SCHEDULE_ID"])

    handler = SocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"])
    try:
        handler.start()
    except KeyboardInterrupt:
        logging.info("Shutting down on-call swap bot")
        sys.exit(0)


if __name__ == "__main__":
    main()

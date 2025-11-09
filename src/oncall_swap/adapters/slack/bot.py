from __future__ import annotations

import json
from typing import Iterable, List
from uuid import UUID

from slack_bolt import App
from slack_sdk import WebClient

from oncall_swap.application.commands import AcceptCoverCommand, TimeWindowDTO
from oncall_swap.application.services import SwapNegotiationService
from oncall_swap.domain.models import Participant, SwapOffer, TimeWindow
from oncall_swap.ports.slack import SlackNotificationPort, SlackPromptPort


def _window_to_str(window: TimeWindow) -> str:
    return f"{window.start.isoformat()} → {window.end.isoformat()}"


def _window_to_value(window: TimeWindow) -> dict:
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


class SlackBotAdapter(SlackNotificationPort, SlackPromptPort):
    """Slack Bolt adapter that renders interactive swap prompts."""

    def __init__(
        self,
        app: App,
        negotiation_service: SwapNegotiationService,
        announcement_channel: str,
    ) -> None:
        self.app = app
        self.negotiation_service = negotiation_service
        self.announcement_channel = announcement_channel
        # Ensure the service outputs through this adapter.
        self.negotiation_service.slack_notifications = self
        self.negotiation_service.slack_prompts = self
        self._register_handlers()

    # SlackNotificationPort ---------------------------------------------------

    def announce_offer(self, offer: SwapOffer) -> None:
        self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text=f"🌀 New on-call swap offer from {offer.requester.email}",
            blocks=self._offer_blocks(offer),
        )

    def notify_direct_swap(self, offer: SwapOffer, participant: Participant, window: TimeWindow) -> None:
        self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text="✅ On-call swap completed.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Direct swap complete*\n"
                            f"{participant.email} covers `{_window_to_str(offer.let_window)}` "
                            f"in exchange for `{_window_to_str(window)}`."
                        ),
                    },
                }
            ],
        )

    def notify_ring_candidate(self, offer: SwapOffer, candidate: Participant) -> None:
        self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text="🔄 Ring swap candidate identified.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{candidate.email} can cover `{_window_to_str(offer.let_window)}` "
                            "but needs coverage for additional windows. Offer updated."
                        ),
                    },
                },
                *self._availability_blocks(offer),
            ],
        )

    def notify_ring_update(self, offer: SwapOffer) -> None:
        self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text="🔁 On-call ring swap updated.",
            blocks=self._availability_blocks(offer),
        )

    def notify_ring_completion(self, offer: SwapOffer) -> None:
        self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text="🎉 Ring swap completed.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "On-call ring swap successfully orchestrated and synced with Opsgenie.",
                    },
                }
            ],
        )

    # SlackPromptPort ---------------------------------------------------------

    def prompt_cover_request(
        self,
        offer_id: UUID,
        candidates: Iterable[Participant],
        window: TimeWindow,
        available_alternatives: Iterable[TimeWindow],
    ) -> None:
        client: WebClient = self.app.client
        alternatives = list(available_alternatives)
        for participant in candidates:
            client.chat_postMessage(
                channel=participant.slack_user_id or self.announcement_channel,
                text=f"On-call coverage request for {_window_to_str(window)}.",
                blocks=self._prompt_blocks(offer_id, window, alternatives),
            )

    # Internal ----------------------------------------------------------------

    def _register_handlers(self) -> None:
        @self.app.action("swap_accept")
        def handle_swap_accept(ack, body, logger):
            ack()
            user = body["user"]["id"]
            payload = json.loads(body["actions"][0]["value"])
            try:
                profile = self.app.client.users_profile_get(user=user)
                email = profile["profile"].get("email")
                if not email:
                    raise ValueError("Unable to resolve your email from Slack profile.")
                command = AcceptCoverCommand(
                    offer_id=UUID(payload["offer_id"]),
                    participant_email=email,
                    covers_window=TimeWindowDTO(**payload["covers_window"]),
                    needs_windows=[TimeWindowDTO(**window) for window in payload.get("needs_windows", [])],
                )
                self.negotiation_service.accept_cover(command)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to process swap acceptance: %s", exc, exc_info=True)
                self.app.client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=user,
                    text=f"Something went wrong while processing your response: {exc}",
                )

    def _offer_blocks(self, offer: SwapOffer) -> List[dict]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*New swap offer*\n"
                        f"Requester: `{offer.requester.email}`\n"
                        f"Needs coverage for: `{_window_to_str(offer.let_window)}`\n"
                        f"Can cover: {', '.join(f'`{_window_to_str(win)}`' for win in offer.available_windows)}"
                    ),
                },
            }
        ]

    def _prompt_blocks(self, offer_id: UUID, window: TimeWindow, alternatives: List[TimeWindow]) -> List[dict]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Swap request*\n"
                        f"Can you cover `{_window_to_str(window)}`?\n"
                        f"Select a shift you'd like covered in return."
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "static_select",
                        "placeholder": {"type": "plain_text", "text": "Pick a window"},
                        "action_id": "swap_accept",
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": _window_to_str(option)},
                                "value": json.dumps(
                                    {
                                        "offer_id": str(offer_id),
                                        "covers_window": _window_to_value(window),
                                        "needs_windows": [_window_to_value(option)],
                                    }
                                ),
                            }
                            for option in alternatives
                        ],
                    }
                ],
            },
        ]

    def _availability_blocks(self, offer: SwapOffer) -> List[dict]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Updated availability*\n"
                        f"Coverage sought for: `{_window_to_str(offer.let_window)}`\n"
                        f"Trade options now include: {', '.join(f'`{_window_to_str(win)}`' for win in offer.available_windows)}"
                    ),
                },
            }
        ]

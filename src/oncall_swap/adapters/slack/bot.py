from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from slack_bolt import App
from slack_sdk import WebClient

from oncall_swap.application.commands import AcceptCoverCommand, CreateOfferCommand, TimeWindowDTO
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
        schedule_id: str,
    ) -> None:
        self.app = app
        self.negotiation_service = negotiation_service
        self.announcement_channel = announcement_channel
        self.schedule_id = schedule_id
        self._offer_threads: Dict[UUID, Tuple[str, str]] = {}
        self._window_labels: Dict[UUID, Dict[Tuple[str, str], str]] = {}
        # Ensure the service outputs through this adapter.
        self.negotiation_service.slack_notifications = self
        self.negotiation_service.slack_prompts = self
        self._register_handlers()

    # SlackNotificationPort ---------------------------------------------------

    def announce_offer(self, offer: SwapOffer) -> None:
        response = self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text=f"🌀 New on-call swap offer from {offer.requester.email}",
            blocks=_offer_blocks(offer, self._labels_for(offer)),
        )
        self._offer_threads[offer.id] = (response["channel"], response["ts"])
        labels = self._window_labels.setdefault(offer.id, {})
        for window in offer.available_windows:
            labels[_window_key(window)] = f"[Direct] Requester availability ({offer.requester.email})"

    def notify_direct_swap(self, offer: SwapOffer, participant: Participant, window: TimeWindow) -> None:
        labels = self._labels_for(offer)
        self._post_update(
            offer.id,
            "✅ On-call swap completed.",
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Direct swap complete*\n"
                            f"{participant.email} covers `{_window_to_str(offer.let_window)}` "
                            f"in exchange for `{_window_to_str(window)}` "
                            f"({labels.get(_window_key(window), 'trade window')})."
                        ),
                    },
                }
            ],
        )

    def notify_ring_candidate(self, offer: SwapOffer, candidate: Participant) -> None:
        labels = self._window_labels.setdefault(offer.id, {})
        for window in offer.available_windows:
            key = _window_key(window)
            labels.setdefault(key, f"[Ring] Needs coverage for {candidate.email}")

        self._post_update(
            offer.id,
            "🔄 Ring swap candidate identified.",
            [
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
                *_availability_blocks(offer, labels),
            ],
        )

    def notify_ring_update(self, offer: SwapOffer) -> None:
        labels = self._labels_for(offer)
        self._post_update(
            offer.id,
            "🔁 On-call ring swap updated.",
            _availability_blocks(offer, labels),
        )

    def notify_ring_completion(self, offer: SwapOffer) -> None:
        summary_text = _commitment_summary(offer)
        self._post_update(
            offer.id,
            "🎉 Ring swap completed.",
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "On-call ring swap successfully orchestrated and synced with Opsgenie.",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": summary_text,
                    },
                },
            ],
        )
        self._add_reaction(offer.id, "white_check_mark")
        self._offer_threads.pop(offer.id, None)
        self._window_labels.pop(offer.id, None)

    # SlackPromptPort ---------------------------------------------------------

    def prompt_cover_request(
        self,
        offer_id: UUID,
        candidates: Iterable[Participant],
        window: TimeWindow,
        available_alternatives: Iterable[TimeWindow],
        need_owner: Participant,
    ) -> None:
        labels = self._window_labels.setdefault(offer_id, {})
        labels[_window_key(window)] = f"[Ring] Needs coverage for {need_owner.email}"
        alternatives = list(available_alternatives)
        candidate_emails = ", ".join(sorted({participant.email for participant in candidates})) or "Anyone available"

        prompt_text = (
            f"🔔 {need_owner.email} needs coverage for `{_window_to_str(window)}`.\n"
            f"Potential traders: {candidate_emails}"
        )

        self._post_update(
            offer_id,
            prompt_text,
            _prompt_blocks(
                offer_id,
                window,
                alternatives,
                labels,
                need_owner.email,
            ),
        )

    # Internal ----------------------------------------------------------------

    def _register_handlers(self) -> None:
        @self.app.action("swap_respond")
        def handle_swap_respond(ack, body, logger):
            ack()
            try:
                action_payload = body["actions"][0]
                value = json.loads(action_payload["value"])
                offer_id = UUID(value["offer_id"])
                covers_window = _parse_window_value(json.dumps(value["covers_window"]))
                need_owner_email = value.get("need_owner")

                offer = self.negotiation_service.get_offer(offer_id)
                labels = self._labels_for(offer)
                options = _modal_trade_options(offer, labels)
                if not options:
                    self._post_update(
                        offer_id,
                        "⚠️ No trade windows are currently available. Please wait for new options.",
                    )
                    return

                modal = _build_response_modal(
                    offer=offer,
                    covers_window=covers_window,
                    need_owner_email=need_owner_email,
                    options=options,
                )
                self.app.client.views_open(trigger_id=body["trigger_id"], view=modal)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to open response modal: %s", exc, exc_info=True)

        @self.app.view("swap_response_submit")
        def handle_swap_response_submit(ack, body, logger):
            metadata = json.loads(body["view"]["private_metadata"])
            state = body["view"]["state"]["values"]
            user_id = body["user"]["id"]

            selected_option = state["trade_select_block"]["trade_select"].get("selected_option")
            if not selected_option:
                ack(
                    {
                        "response_action": "errors",
                        "errors": {
                            "trade_select_block": "Please choose a trade window.",
                        },
                    }
                )
                return

            ack()

            try:
                profile = self.app.client.users_profile_get(user=user_id)
                email = profile["profile"].get("email")
                if not email:
                    raise ValueError("Unable to resolve your email from Slack profile.")

                offer_id = UUID(metadata["offer_id"])
                offer = self.negotiation_service.get_offer(offer_id)

                covers_window = _parse_window_value(json.dumps(metadata["covers_window"]))
                need_window = _parse_window_value(selected_option["value"])

                command = AcceptCoverCommand(
                    offer_id=offer_id,
                    participant_email=email,
                    covers_window=TimeWindowDTO(start=covers_window.start, end=covers_window.end),
                    needs_windows=[
                        TimeWindowDTO(start=need_window.start, end=need_window.end),
                    ],
                )
                result = self.negotiation_service.accept_cover(command)

                channel_ts = self._offer_threads.get(offer_id)
                if channel_ts:
                    self.app.client.chat_postEphemeral(
                        channel=channel_ts[0],
                        user=user_id,
                        text="Thanks! Your swap response has been recorded.",
                    )

            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to process swap response: %s", exc, exc_info=True)

        @self.app.command("/swap-oncall")
        def handle_swap_command(ack, body, respond, logger):
            ack()
            try:
                user_id = body["user_id"]
                channel_id = body.get("channel_id")
                profile = self.app.client.users_profile_get(user=user_id)
                email = profile["profile"].get("email")
                if not email:
                    respond("Unable to determine your email from your Slack profile.")
                    return

                windows = self.negotiation_service.get_upcoming_windows(
                    schedule_id=self.schedule_id,
                    participant_email=email,
                )
                if not windows:
                    respond("No upcoming on-call windows found for you in Opsgenie.")
                    return

                options = [_modal_option_from_window(window) for window in windows[:25]]

                self.app.client.views_open(
                    trigger_id=body["trigger_id"],
                    view=_build_swap_offer_modal(
                        options=options,
                        metadata=json.dumps(
                            {"email": email, "channel": channel_id, "schedule_id": self.schedule_id}
                        ),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to open swap offer modal: %s", exc, exc_info=True)
                respond(f"Failed to start swap offer: {exc}")

        @self.app.view("swap_offer_submit")
        def handle_swap_submit(ack, body, logger):
            state = body["view"]["state"]["values"]
            metadata = json.loads(body["view"]["private_metadata"])
            user_id = body["user"]["id"]

            try:
                let_selection = state["let_window_block"]["let_window"]["selected_option"]
                let_window = _parse_window_value(let_selection["value"])

                search_start = _combine_date_time(
                    state["search_start_date_block"]["search_start_date"]["selected_date"],
                    state["search_start_time_block"]["search_start_time"]["selected_time"],
                )
                search_end = _combine_date_time(
                    state["search_end_date_block"]["search_end_date"]["selected_date"],
                    state["search_end_time_block"]["search_end_time"]["selected_time"],
                )

                if not search_start or not search_end:
                    ack(
                        {
                            "response_action": "errors",
                            "errors": {
                                "search_end_date_block": "Please provide both start and end date/time."
                            },
                        }
                    )
                    return

                if search_end <= search_start:
                    ack(
                        {
                            "response_action": "errors",
                            "errors": {
                                "search_end_date_block": "The end time must be after the start time."
                            },
                        }
                    )
                    return

                command = CreateOfferCommand(
                    requester_email=metadata["email"],
                    schedule_id=metadata["schedule_id"],
                    let_window=TimeWindowDTO(start=let_window.start, end=let_window.end),
                    search_windows=[
                        TimeWindowDTO(start=search_start, end=search_end),
                    ],
                )

                offer = self.negotiation_service.create_offer(command)
                ack()

                channel = metadata.get("channel")
                if channel:
                    self.app.client.chat_postEphemeral(
                        channel=channel,
                        user=user_id,
                        text=f"Created swap offer for {_window_to_str(offer.let_window)}.",
                    )
            except SwapOffer.TimeWindowInPastError as exc:
                ack(
                    {
                        "response_action": "errors",
                        "errors": {
                            "search_end_date_block": str(exc),
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to create swap offer: %s", exc, exc_info=True)
                ack({"response_action": "clear"})
                channel = metadata.get("channel")
                if channel:
                    self.app.client.chat_postEphemeral(
                        channel=channel,
                        user=user_id,
                        text=f"Failed to create offer: {exc}",
                    )

    def _post_update(self, offer_id: UUID, text: str, blocks: Optional[List[dict]] = None) -> None:
        channel_ts = self._offer_threads.get(offer_id)
        if channel_ts:
            channel, thread_ts = channel_ts
            self.app.client.chat_postMessage(
                channel=channel,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
            )
        else:
            response = self.app.client.chat_postMessage(
                channel=self.announcement_channel,
                text=text,
                blocks=blocks,
            )
            self._offer_threads[offer_id] = (response["channel"], response["ts"])

    def _labels_for(self, offer: SwapOffer) -> Dict[Tuple[str, str], str]:
        labels = self._window_labels.setdefault(offer.id, {})
        for window in offer.available_windows:
            labels.setdefault(_window_key(window), "Trade window")
        return labels

    def _add_reaction(self, offer_id: UUID, emoji: str) -> None:
        channel_ts = self._offer_threads.get(offer_id)
        if not channel_ts:
            return
        channel, thread_ts = channel_ts
        try:
            self.app.client.reactions_add(
                channel=channel,
                name=emoji,
                timestamp=thread_ts,
            )
        except Exception:
            # Ignore reaction errors (e.g., reaction already added)
            pass

def _build_swap_offer_modal(options: List[dict], metadata: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "swap_offer_submit",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Create Swap Offer"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "let_window_block",
                "label": {"type": "plain_text", "text": "Shift you want to give away"},
                "element": {
                    "type": "static_select",
                    "action_id": "let_window",
                    "options": options,
                },
            },
            {
                "type": "input",
                "block_id": "search_start_date_block",
                "label": {"type": "plain_text", "text": "Preferred coverage start date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "search_start_date",
                },
            },
            {
                "type": "input",
                "block_id": "search_start_time_block",
                "label": {"type": "plain_text", "text": "Preferred coverage start time (UTC)"},
                "element": {
                    "type": "timepicker",
                    "action_id": "search_start_time",
                },
            },
            {
                "type": "input",
                "block_id": "search_end_date_block",
                "label": {"type": "plain_text", "text": "Preferred coverage end date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "search_end_date",
                },
            },
            {
                "type": "input",
                "block_id": "search_end_time_block",
                "label": {"type": "plain_text", "text": "Preferred coverage end time (UTC)"},
                "element": {
                    "type": "timepicker",
                    "action_id": "search_end_time",
                },
            },
        ],
    }


def _modal_option_from_window(window: TimeWindow) -> dict:
    return {
        "text": {"type": "plain_text", "text": _window_to_str(window)[:75]},
        "value": json.dumps(_window_to_value(window)),
    }


def _parse_window_value(value: str) -> TimeWindow:
    payload = json.loads(value)
    start = datetime.fromisoformat(payload["start"])
    end = datetime.fromisoformat(payload["end"])
    return TimeWindow(start=start, end=end)


def _combine_date_time(date_str: str | None, time_str: str | None) -> Optional[datetime]:
    if not date_str or not time_str:
        return None
    combined = datetime.fromisoformat(f"{date_str}T{time_str}")
    if combined.tzinfo is None:
        combined = combined.replace(tzinfo=timezone.utc)
    return combined.astimezone(timezone.utc)


def _offer_blocks(offer: SwapOffer, labels: Dict[Tuple[str, str], str]) -> List[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _offer_summary_text(offer, labels),
            },
        }
    ]


def _prompt_blocks(
    offer_id: UUID,
    window: TimeWindow,
    alternatives: List[TimeWindow],
    labels: Dict[Tuple[str, str], str],
    need_owner_email: str,
) -> List[dict]:
    trade_lines = "\n".join(
        f"• `{_window_to_str(option)}` — {labels.get(_window_key(option), 'trade window')}"
        for option in alternatives
    ) or "• No trade windows available yet."
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Swap request*\n"
                    f"Can you cover `{_window_to_str(window)}`?\n"
                    f"These are the shifts you can swap for:\n"
                    f"{trade_lines}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "swap_respond",
                    "text": {"type": "plain_text", "text": "Respond"},
                    "value": json.dumps(
                        {
                            "offer_id": str(offer_id),
                            "covers_window": _window_to_value(window),
                            "need_owner": need_owner_email,
                        }
                    ),
                }
            ],
        },
    ]


def _availability_blocks(offer: SwapOffer, labels: Dict[Tuple[str, str], str]) -> List[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _offer_summary_text(offer, labels, header="*Updated availability*"),
            },
        }
    ]


def _modal_trade_options(offer: SwapOffer, labels: Dict[Tuple[str, str], str]) -> List[dict]:
    search_set = {window.to_tuple() for window in offer.search_windows}
    options: List[dict] = []
    for window in offer.available_windows:
        key = _window_key(window)
        mode = "[Direct]" if window.to_tuple() in search_set else "[Ring]"
        annotation = labels.get(key, "")
        text = f"{mode} {_window_to_str(window)}"
        if annotation:
            text = f"{text} — {annotation}"
        options.append(
            {
                "text": {"type": "plain_text", "text": text[:75]},
                "value": json.dumps(_window_to_value(window)),
            }
        )
    return options


def _build_response_modal(
    *,
    offer: SwapOffer,
    covers_window: TimeWindow,
    need_owner_email: Optional[str],
    options: List[dict],
) -> dict:
    owner_label = need_owner_email or "the requester"
    metadata = {
        "offer_id": str(offer.id),
        "covers_window": _window_to_value(covers_window),
        "need_owner": need_owner_email,
    }
    return {
        "type": "modal",
        "callback_id": "swap_response_submit",
        "private_metadata": json.dumps(metadata),
        "title": {"type": "plain_text", "text": "Confirm Swap"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"You are offering to cover `{_window_to_str(covers_window)}` "
                        f"for {owner_label}. Choose a shift you would like covered in return."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "trade_select_block",
                "label": {"type": "plain_text", "text": "Trade-in shift"},
                "element": {
                    "type": "static_select",
                    "action_id": "trade_select",
                    "options": options,
                },
            },
        ],
    }


def _offer_summary_text(offer: SwapOffer, labels: Dict[Tuple[str, str], str], header: str = "*New swap offer*") -> str:
    trade_lines = "\n".join(
        f"• `{_window_to_str(win)}` – {labels.get(_window_key(win), 'trade window')}"
        for win in offer.available_windows
    )
    return (
        f"{header}\n"
        f"Needs coverage for: `{_window_to_str(offer.let_window)}`\n"
        f"Trade options:\n"
        f"{trade_lines}"
    )


def _commitment_summary(offer: SwapOffer) -> str:
    if not offer.ring_swaps:
        return "*Swap summary:*\n• Swap finalised without ring commitments."
    ring = offer.ring_swaps[-1]
    lines = [
        f"• {commitment.from_participant.email} covers `{_window_to_str(commitment.window)}` "
        f"for {commitment.to_participant.email}"
        for commitment in ring.commitments
    ]
    return "*Swap summary:*\n" + "\n".join(lines)


def _window_key(window: TimeWindow) -> Tuple[str, str]:
    return window.start.isoformat(), window.end.isoformat()

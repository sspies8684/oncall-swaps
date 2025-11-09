from __future__ import annotations

import json
from datetime import date, datetime, timedelta, time, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from slack_bolt import App

from oncall_swap.application.commands import AcceptCoverCommand, CreateOfferCommand, TimeWindowDTO
from oncall_swap.application.services import SwapNegotiationService
from oncall_swap.domain.models import Participant, SwapOffer, TimeWindow
from oncall_swap.ports.opsgenie import OnCallAssignment
from oncall_swap.ports.slack import SlackNotificationPort


def _date_to_str(d: date) -> str:
    return d.isoformat()


def _window_to_str(window: TimeWindow) -> str:
    return f"{window.start.date().isoformat()} {window.start.time().isoformat()} → {window.end.date().isoformat()} {window.end.time().isoformat()}"


def _window_to_value(window: TimeWindow) -> dict:
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def _parse_window_value(value: str) -> TimeWindow:
    payload = json.loads(value)
    start = datetime.fromisoformat(payload["start"])
    end = datetime.fromisoformat(payload["end"])
    return TimeWindow(start=start, end=end)


def _date_range_to_time_windows(start_date: date, end_date: date) -> List[TimeWindow]:
    """Convert a date range to time windows covering the full days."""
    windows = []
    current = start_date
    while current <= end_date:
        # Full day coverage: 00:00 UTC to 23:59:59 UTC
        day_start = datetime.combine(current, time.min).replace(tzinfo=timezone.utc)
        day_end = datetime.combine(current, time.max).replace(tzinfo=timezone.utc)
        windows.append(TimeWindow(start=day_start, end=day_end))
        current = date(current.year, current.month, current.day) + timedelta(days=1)
    return windows


class SlackBotAdapter(SlackNotificationPort):
    """Slack Bolt adapter for on-call swap workflow."""

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
        self._posted_needs: Dict[UUID, set] = {}  # Track which needs have been posted
        # Ensure the service outputs through this adapter.
        self.negotiation_service.slack_notifications = self
        self._register_handlers()

    # SlackNotificationPort ---------------------------------------------------

    def announce_offer(self, offer: SwapOffer) -> None:
        """Post initial swap offer and create thread."""
        let_date = offer.let_window.start.date()
        
        # Group search windows by date ranges
        search_ranges = []
        current_start = None
        current_end = None
        
        sorted_windows = sorted(offer.search_windows, key=lambda w: w.start.date())
        for window in sorted_windows:
            window_start = window.start.date()
            window_end = window.end.date()
            
            if current_start is None:
                current_start = window_start
                current_end = window_end
            elif window_start <= current_end + timedelta(days=1):
                # Extend current range
                current_end = max(current_end, window_end)
            else:
                # Save current range and start new one
                search_ranges.append((current_start, current_end))
                current_start = window_start
                current_end = window_end
        
        if current_start is not None:
            search_ranges.append((current_start, current_end))
        
        # Format search windows text
        if len(search_ranges) == 1:
            start, end = search_ranges[0]
            if start == end:
                search_text = f"Search window: `{_date_to_str(start)}`"
            else:
                search_text = f"Search window: `{_date_to_str(start)}` to `{_date_to_str(end)}`"
        else:
            range_strs = []
            for start, end in search_ranges:
                if start == end:
                    range_strs.append(f"`{_date_to_str(start)}`")
                else:
                    range_strs.append(f"`{_date_to_str(start)}` to `{_date_to_str(end)}`")
            search_text = f"Search windows: {', '.join(range_strs)}"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*New swap offer from {offer.requester.email}*\n"
                        f"Looking to swap: `{_date_to_str(let_date)}`\n"
                        f"{search_text}"
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
                                "offer_id": str(offer.id),
                                "covers_window": _window_to_value(offer.let_window),
                            }
                        ),
                    }
                ],
            },
        ]

        response = self.app.client.chat_postMessage(
            channel=self.announcement_channel,
            text=f"🌀 New on-call swap offer from {offer.requester.email}",
            blocks=blocks,
        )
        self._offer_threads[offer.id] = (response["channel"], response["ts"])

    def notify_direct_swap(
        self,
        offer: SwapOffer,
        participant: Participant,
        window: TimeWindow,
        all_assignments: Optional[List[OnCallAssignment]] = None,
    ) -> None:
        """Notify that a direct swap was completed and close the negotiation."""
        let_date = offer.let_window.start.date()
        swap_date = window.start.date()

        # Build swap summary text
        if all_assignments and len(all_assignments) > 2:
            # Direct swap that closed a ring swap chain (multiple participants but direct swap closed it)
            swap_lines = []
            for assignment in all_assignments:
                swap_lines.append(
                    f"• {assignment.participant.email} covers `{_date_to_str(assignment.window.start.date())}`"
                )
            swap_text = "*Swap completed*\n" + "\n".join(swap_lines) + "\nOverrides have been applied in Opsgenie."
        else:
            # Simple direct swap
            swap_text = (
                f"{participant.email} will cover `{_date_to_str(let_date)}` "
                f"in exchange for `{_date_to_str(swap_date)}` (direct swap).\n"
                f"Overrides have been applied in Opsgenie."
            )

        # Post completion message and get its timestamp
        channel_ts = self._offer_threads.get(offer.id)
        if channel_ts:
            channel, thread_ts = channel_ts
            response = self.app.client.chat_postMessage(
                channel=channel,
                text="✅ *Swap completed*",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": swap_text,
                        },
                    }
                ],
                thread_ts=thread_ts,
            )
            # Add checkmark reaction to the completion message
            if response.get("ok") and response.get("ts"):
                try:
                    result = self.app.client.reactions_add(
                        channel=channel,
                        name="white_check_mark",
                        timestamp=response["ts"],
                    )
                    if not result.get("ok"):
                        # Log error but don't fail
                        print(f"Failed to add reaction: {result.get('error')}")
                except Exception as e:
                    # Log error but don't fail
                    print(f"Exception adding reaction: {e}")
            else:
                print(f"Invalid response from chat_postMessage: {response}")
            # Also add checkmark to the original thread message
            self._add_reaction(offer.id, "white_check_mark")
        else:
            # Fallback: post to channel if thread not found
            response = self.app.client.chat_postMessage(
                channel=self.announcement_channel,
                text="✅ *Swap completed*",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": swap_text,
                        },
                    }
                ],
            )
            if response.get("ok") and response.get("ts"):
                try:
                    result = self.app.client.reactions_add(
                        channel=response["channel"],
                        name="white_check_mark",
                        timestamp=response["ts"],
                    )
                    if not result.get("ok"):
                        print(f"Failed to add reaction: {result.get('error')}")
                except Exception as e:
                    print(f"Exception adding reaction: {e}")
            else:
                print(f"Invalid response from chat_postMessage: {response}")
        
        # Clean up tracking
        self._posted_needs.pop(offer.id, None)

    def notify_ring_candidate(self, offer: SwapOffer, candidate: Participant) -> None:
        """Notify about a ring swap and add a new message to the thread for the new trade option."""
        # This shouldn't be called in the new workflow
        pass

    def notify_ring_completion(self, offer: SwapOffer) -> None:
        """Notify about ring swap completion."""
        # This shouldn't be called in the new workflow
        pass

    def notify_ring_update(self, offer: SwapOffer) -> None:
        """Add a new message to thread for ring swap trade option."""
        # Get the most recent ring swap need
        if not offer.outstanding_needs:
            return

        # Track which needs we've already posted
        posted = self._posted_needs.setdefault(offer.id, set())

        # Post a new message for each ring swap need that hasn't been posted yet
        for need in offer.outstanding_needs:
            if need.created_by_offer:
                continue  # Skip the original let_window need
            
            need_key = need.window.to_tuple()
            if need_key in posted:
                continue  # Already posted
            
            need_date = need.window.start.date()
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Ring swap opportunity*\n"
                            f"{need.owner.email} needs coverage for `{_date_to_str(need_date)}`.\n"
                            f"Can you cover this date?"
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
                                    "offer_id": str(offer.id),
                                    "covers_window": _window_to_value(need.window),
                                }
                            ),
                        }
                    ],
                },
            ]

            self._post_to_thread(offer.id, f"🔄 Ring swap: {need.owner.email} needs coverage", blocks)
            posted.add(need_key)

    # Internal ----------------------------------------------------------------

    def _register_handlers(self) -> None:
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

                # Get user's upcoming on-call windows to populate let date options
                windows = self.negotiation_service.get_upcoming_windows(
                    schedule_id=self.schedule_id,
                    participant_email=email,
                )
                if not windows:
                    respond("No upcoming on-call windows found for you in Opsgenie.")
                    return

                # Create date options from windows
                date_options = {}
                for window in windows[:25]:
                    window_date = window.start.date()
                    if window_date not in date_options:
                        date_options[window_date] = window

                options = [
                    {
                        "text": {"type": "plain_text", "text": _date_to_str(d)},
                        "value": json.dumps(_window_to_value(w)),
                    }
                    for d, w in sorted(date_options.items())
                ]

                self.app.client.views_open(
                    trigger_id=body["trigger_id"],
                    view=_build_swap_offer_modal(
                        let_date_options=options,
                        metadata=json.dumps(
                            {"email": email, "channel": channel_id, "schedule_id": self.schedule_id}
                        ),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to open swap offer modal: %s", exc, exc_info=True)
                respond(f"Failed to start swap offer: {exc}")

        @self.app.action("add_another_range")
        def handle_add_another_range(ack, body, logger):
            ack()
            try:
                action_payload = body["actions"][0]
                value = json.loads(action_payload["value"])
                existing_windows_data = value.get("existing_windows", [])
                metadata_str = value.get("metadata", "{}")
                
                # Ensure metadata is a JSON string
                if isinstance(metadata_str, dict):
                    metadata = json.dumps(metadata_str)
                else:
                    metadata = metadata_str
                
                # Parse existing windows
                existing_windows = []
                for window_data in existing_windows_data:
                    if len(window_data) >= 1 and window_data[0]:
                        start_date = date.fromisoformat(window_data[0])
                        end_date = date.fromisoformat(window_data[1]) if len(window_data) > 1 and window_data[1] else None
                        existing_windows.append((start_date, end_date))
                
                # Get current state to extract the current window being entered
                view_state = body.get("view", {}).get("state", {}).get("values", {})
                current_window_num = len(existing_windows) + 1
                
                logger.debug("Adding window %d, existing windows: %d", current_window_num, len(existing_windows))
                logger.debug("View state keys: %s", list(view_state.keys()))
                
                # Access Slack modal state correctly
                start_block = view_state.get(f"search_window_{current_window_num}_start", {})
                end_block = view_state.get(f"search_window_{current_window_num}_end", {})
                current_start = start_block.get("start_date", {}).get("selected_date")
                current_end = end_block.get("end_date", {}).get("selected_date")
                
                logger.debug("Current window %d - start: %s, end: %s", current_window_num, current_start, current_end)
                
                # Add current window if start date is provided
                if current_start:
                    try:
                        start_date = date.fromisoformat(current_start)
                        end_date = None
                        if current_end:
                            end_date = date.fromisoformat(current_end)
                            if end_date < start_date:
                                # Invalid range, don't add it
                                logger.warning("Invalid date range: end %s < start %s", end_date, start_date)
                            else:
                                existing_windows.append((start_date, end_date))
                                logger.debug("Added date range: %s to %s", start_date, end_date)
                        else:
                            # Single date
                            existing_windows.append((start_date, None))
                            logger.debug("Added single date: %s", start_date)
                    except ValueError as e:
                        logger.warning("Invalid date format: %s", e)
                
                # Get let window options
                user_id = body["user"]["id"]
                profile = self.app.client.users_profile_get(user=user_id)
                email = profile["profile"].get("email")
                
                if not email:
                    logger.error("No email found for user %s", user_id)
                    return
                
                windows = self.negotiation_service.get_upcoming_windows(
                    schedule_id=self.schedule_id,
                    participant_email=email,
                )
                date_options = {}
                for window in windows[:25]:
                    window_date = window.start.date()
                    if window_date not in date_options:
                        date_options[window_date] = window
                
                options = [
                    {
                        "text": {"type": "plain_text", "text": _date_to_str(d)},
                        "value": json.dumps(_window_to_value(w)),
                    }
                    for d, w in sorted(date_options.items())
                ]
                
                # Get selected let window from current view
                let_window_block = view_state.get("let_window_block", {}).get("let_window", {})
                selected_let = let_window_block.get("selected_option", {}).get("value")
                
                # Rebuild modal with existing windows + new empty window
                modal = _build_swap_offer_modal(
                    let_date_options=options,
                    metadata=metadata,
                    existing_windows=existing_windows,
                )
                
                # Preserve selected let window if available
                if selected_let:
                    for block in modal["blocks"]:
                        if block.get("block_id") == "let_window_block":
                            block["element"]["initial_option"] = next(
                                (opt for opt in options if opt["value"] == selected_let),
                                None,
                            )
                
                logger.debug("Updating view with %d existing windows", len(existing_windows))
                result = self.app.client.views_update(
                    view_id=body["view"]["id"],
                    view=modal,
                )
                
                if not result.get("ok"):
                    logger.error("Failed to update view: %s", result.get("error"))
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to add another range: %s", exc, exc_info=True)

        @self.app.view("swap_offer_submit")
        def handle_swap_submit(ack, body, logger):
            state = body["view"]["state"]["values"]
            private_metadata = json.loads(body["view"]["private_metadata"])
            user_id = body["user"]["id"]

            try:
                # Debug: log state structure
                logger.debug("Modal state keys: %s", list(state.keys()))
                
                # Parse metadata - handle both old format (direct JSON) and new format (nested)
                if "metadata" in private_metadata:
                    # New format: metadata is nested
                    metadata_str = private_metadata.get("metadata", "{}")
                    if isinstance(metadata_str, str):
                        metadata = json.loads(metadata_str)
                    else:
                        metadata = metadata_str
                else:
                    # Old format: metadata is the root
                    metadata = private_metadata

                let_selection = state["let_window_block"]["let_window"]["selected_option"]
                let_window = _parse_window_value(let_selection["value"])

                # Parse existing windows from metadata
                existing_windows_data = private_metadata.get("existing_windows", [])
                existing_windows = []
                for window_data in existing_windows_data:
                    if len(window_data) >= 1 and window_data[0]:
                        start_date = date.fromisoformat(window_data[0])
                        end_date = date.fromisoformat(window_data[1]) if len(window_data) > 1 and window_data[1] else None
                        existing_windows.append((start_date, end_date))

                # Collect windows from date pickers
                search_windows = []
                current_window_num = len(existing_windows) + 1
                
                # Access Slack modal state correctly: state["values"][block_id][action_id]
                start_block = state.get(f"search_window_{current_window_num}_start", {})
                end_block = state.get(f"search_window_{current_window_num}_end", {})
                current_start = start_block.get("start_date", {}).get("selected_date")
                current_end = end_block.get("end_date", {}).get("selected_date")
                
                logger.debug(
                    "Window %d - start_block keys: %s, end_block keys: %s, start: %s, end: %s",
                    current_window_num,
                    list(start_block.keys()),
                    list(end_block.keys()),
                    current_start,
                    current_end,
                )

                # Add existing windows
                for start_date, end_date in existing_windows:
                    if end_date is None:
                        # Single date
                        day_start = datetime.combine(start_date, time.min).replace(tzinfo=timezone.utc)
                        day_end = datetime.combine(start_date, time.max).replace(tzinfo=timezone.utc)
                        search_windows.append(TimeWindow(start=day_start, end=day_end))
                    else:
                        # Date range
                        windows = _date_range_to_time_windows(start_date, end_date)
                        search_windows.extend(windows)

                # Process current window if provided
                if current_start:
                    try:
                        search_start_date = date.fromisoformat(current_start)
                        
                        if current_end:
                            # Date range
                            search_end_date = date.fromisoformat(current_end)
                            
                            if search_end_date < search_start_date:
                                ack(
                                    {
                                        "response_action": "errors",
                                        "errors": {
                                            f"search_window_{current_window_num}_end": f"End date must be on or after start date"
                                        },
                                    }
                                )
                                return
                            
                            windows = _date_range_to_time_windows(search_start_date, search_end_date)
                            search_windows.extend(windows)
                        else:
                            # Single date
                            day_start = datetime.combine(search_start_date, time.min).replace(tzinfo=timezone.utc)
                            day_end = datetime.combine(search_start_date, time.max).replace(tzinfo=timezone.utc)
                            search_windows.append(TimeWindow(start=day_start, end=day_end))
                    except ValueError as e:
                        ack(
                            {
                                "response_action": "errors",
                                "errors": {
                                    f"search_window_{current_window_num}_start": f"Invalid date format: {str(e)}"
                                },
                            }
                        )
                        return

                if not search_windows:
                    ack(
                        {
                            "response_action": "errors",
                            "errors": {
                                f"search_window_{current_window_num}_start": "Please provide at least one search window (date or date range)."
                            },
                        }
                    )
                    return

                command = CreateOfferCommand(
                    requester_email=metadata["email"],
                    schedule_id=metadata["schedule_id"],
                    let_window=TimeWindowDTO(start=let_window.start, end=let_window.end),
                    search_windows=[TimeWindowDTO(start=w.start, end=w.end) for w in search_windows],
                )

                offer = self.negotiation_service.create_offer(command)
                ack()

                channel = metadata.get("channel")
                if channel:
                    self.app.client.chat_postEphemeral(
                        channel=channel,
                        user=user_id,
                        text=f"Created swap offer for {_date_to_str(offer.let_window.start.date())}.",
                    )
            except SwapOffer.TimeWindowInPastError as exc:
                ack(
                    {
                        "response_action": "errors",
                        "errors": {
                            f"search_window_{current_window_num}_start": str(exc),
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

        @self.app.action("swap_respond")
        def handle_swap_respond(ack, body, logger):
            ack()
            try:
                user_id = body["user"]["id"]
                profile = self.app.client.users_profile_get(user=user_id)
                email = profile["profile"].get("email")
                if not email:
                    raise ValueError("Unable to resolve your email from Slack profile.")

                action_payload = body["actions"][0]
                value = json.loads(action_payload["value"])
                offer_id = UUID(value["offer_id"])
                covers_window = _parse_window_value(json.dumps(value["covers_window"]))

                offer = self.negotiation_service.get_offer(offer_id)
                if not offer.is_active():
                    self.app.client.chat_postEphemeral(
                        channel=body["channel"]["id"],
                        user=user_id,
                        text="This swap offer is no longer active.",
                    )
                    return

                # Get user's upcoming on-call windows for the modal
                user_windows = self.negotiation_service.get_upcoming_windows(
                    schedule_id=offer.schedule_id,
                    participant_email=email,
                    horizon_days=60,
                )
                if not user_windows:
                    self.app.client.chat_postEphemeral(
                        channel=body["channel"]["id"],
                        user=user_id,
                        text="You don't have any upcoming on-call windows to trade.",
                    )
                    return

                # Create options with direct/ring labels
                search_dates = {w.start.date() for w in offer.search_windows}
                options = []
                for window in user_windows[:25]:
                    window_date = window.start.date()
                    is_direct = window_date in search_dates
                    label = "(direct swap)" if is_direct else "(ring swap)"
                    options.append(
                        {
                            "text": {"type": "plain_text", "text": f"{_date_to_str(window_date)} {label}"},
                            "value": json.dumps(_window_to_value(window)),
                        }
                    )

                modal = _build_response_modal(
                    offer_id=offer_id,
                    covers_window=covers_window,
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
                if not offer.is_active():
                    self.app.client.chat_postEphemeral(
                        channel=body.get("channel", {}).get("id") or self.announcement_channel,
                        user=user_id,
                        text="This swap offer is no longer active.",
                    )
                    return

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

    def _post_to_thread(self, offer_id: UUID, text: str, blocks: Optional[List[dict]] = None) -> None:
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
            # Fallback: post to channel if thread not found
            response = self.app.client.chat_postMessage(
                channel=self.announcement_channel,
                text=text,
                blocks=blocks,
            )
            self._offer_threads[offer_id] = (response["channel"], response["ts"])

    def _add_reaction(self, offer_id: UUID, emoji: str) -> None:
        channel_ts = self._offer_threads.get(offer_id)
        if not channel_ts:
            print(f"No thread found for offer {offer_id}")
            return
        channel, thread_ts = channel_ts
        try:
            result = self.app.client.reactions_add(
                channel=channel,
                name=emoji,
                timestamp=thread_ts,
            )
            if not result.get("ok"):
                print(f"Failed to add reaction to thread: {result.get('error')}")
        except Exception as e:
            print(f"Exception adding reaction to thread: {e}")


def _build_swap_offer_modal(let_date_options: List[dict], metadata: str, existing_windows: Optional[List[Tuple[Optional[date], Optional[date]]]] = None) -> dict:
    """Build the swap offer modal with date ranges or single dates.
    
    existing_windows: List of tuples (start_date, end_date) where:
    - If end_date is None, it's a single date
    - If end_date is not None, it's a date range
    """
    if existing_windows is None:
        existing_windows = []
    
    blocks = [
        {
            "type": "input",
            "block_id": "let_window_block",
            "label": {"type": "plain_text", "text": "Date you want to give away"},
            "element": {
                "type": "static_select",
                "action_id": "let_window",
                "options": let_date_options,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Search windows* (preferred dates for coverage)\nYou can specify single dates or date ranges.",
            },
        },
    ]
    
    # Add existing windows as read-only text
    if existing_windows:
        window_texts = []
        for idx, (start, end) in enumerate(existing_windows, 1):
            if end is None:
                window_texts.append(f"Window {idx}: `{_date_to_str(start)}` (single date)")
            else:
                window_texts.append(f"Window {idx}: `{_date_to_str(start)}` to `{_date_to_str(end)}` (range)")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Added windows:*\n" + "\n".join(window_texts),
            },
        })
    
    # Add current window inputs
    window_num = len(existing_windows) + 1
    blocks.extend([
        {
            "type": "input",
            "block_id": f"search_window_{window_num}_start",
            "label": {"type": "plain_text", "text": f"Window {window_num}: Date"},
            "hint": {"type": "plain_text", "text": "Required: Start date for range, or single date"},
            "element": {
                "type": "datepicker",
                "action_id": "start_date",
            },
        },
        {
            "type": "input",
            "block_id": f"search_window_{window_num}_end",
            "label": {"type": "plain_text", "text": f"Window {window_num}: End date (optional)"},
            "hint": {"type": "plain_text", "text": "Leave empty for single date, or specify for date range"},
            "optional": True,
            "element": {
                "type": "datepicker",
                "action_id": "end_date",
            },
        },
    ])
    
    # Add button to add another window
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "➕ Add another window"},
                "action_id": "add_another_range",
                "value": json.dumps({
                    "existing_windows": [
                        [w[0].isoformat(), w[1].isoformat() if w[1] else None]
                        for w in existing_windows
                    ],
                    "metadata": metadata,
                }),
            }
        ],
    })
    
    return {
        "type": "modal",
        "callback_id": "swap_offer_submit",
        "private_metadata": json.dumps({
            "existing_windows": [
                [w[0].isoformat(), w[1].isoformat() if w[1] else None]
                for w in existing_windows
            ],
            "metadata": metadata,
        }),
        "title": {"type": "plain_text", "text": "Create Swap Offer"},
        "submit": {"type": "plain_text", "text": "Create" if not existing_windows else "Add & Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _build_response_modal(
    *,
    offer_id: UUID,
    covers_window: TimeWindow,
    options: List[dict],
) -> dict:
    covers_date = covers_window.start.date()
    metadata = {
        "offer_id": str(offer_id),
        "covers_window": _window_to_value(covers_window),
    }
    return {
        "type": "modal",
        "callback_id": "swap_response_submit",
        "private_metadata": json.dumps(metadata),
        "title": {"type": "plain_text", "text": "Respond to Swap"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"You are offering to cover `{_date_to_str(covers_date)}`. Choose a date you would like covered in return.",
                },
            },
            {
                "type": "input",
                "block_id": "trade_select_block",
                "label": {"type": "plain_text", "text": "Trade-in date"},
                "element": {
                    "type": "static_select",
                    "action_id": "trade_select",
                    "options": options,
                },
            },
        ],
    }

from __future__ import annotations

from typing import Optional, Protocol


class SlackTokenStorage(Protocol):
    """Storage for Slack workspace installation tokens."""

    def save_installation(
        self,
        team_id: str,
        bot_token: str,
        bot_user_id: str,
        access_token: Optional[str] = None,
    ) -> None:
        """Save installation tokens for a workspace."""
        ...

    def get_bot_token(self, team_id: str) -> Optional[str]:
        """Get bot token for a workspace."""
        ...

    def get_bot_user_id(self, team_id: str) -> Optional[str]:
        """Get bot user ID for a workspace."""
        ...

    def get_access_token(self, team_id: str) -> Optional[str]:
        """Get access token for a workspace (for user tokens)."""
        ...

    def remove_installation(self, team_id: str) -> None:
        """Remove installation for a workspace."""
        ...

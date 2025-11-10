from __future__ import annotations

from typing import Dict, Optional

from slack_bolt.oauth import InstallationStore
from slack_sdk.oauth import Installation

from oncall_swap.ports.slack_tokens import SlackTokenStorage


class InMemorySlackTokenStorage(InstallationStore, SlackTokenStorage):
    """In-memory storage for Slack workspace installation tokens.
    
    Implements both slack-bolt's InstallationStore interface and our custom
    SlackTokenStorage interface for compatibility.
    """

    def __init__(self) -> None:
        self._installations: Dict[str, Installation] = {}

    # slack-bolt InstallationStore interface
    def save(self, installation: Installation) -> None:
        """Save installation (slack-bolt interface)."""
        if installation.team_id:
            self._installations[installation.team_id] = installation

    def find_installation(
        self,
        *,
        enterprise_id: Optional[str] = None,
        team_id: Optional[str] = None,
        user_id: Optional[str] = None,
        is_enterprise_install: Optional[bool] = None,
    ) -> Optional[Installation]:
        """Find installation by team_id (slack-bolt interface)."""
        if team_id:
            return self._installations.get(team_id)
        return None

    def delete_bot(self, *, team_id: Optional[str] = None, enterprise_id: Optional[str] = None) -> None:
        """Delete bot installation (slack-bolt interface)."""
        if team_id:
            self._installations.pop(team_id, None)

    def delete_installation(
        self,
        *,
        enterprise_id: Optional[str] = None,
        team_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Delete installation (slack-bolt interface)."""
        if team_id:
            self._installations.pop(team_id, None)

    # Custom SlackTokenStorage interface (for backward compatibility)
    def save_installation(
        self,
        team_id: str,
        bot_token: str,
        bot_user_id: str,
        access_token: Optional[str] = None,
    ) -> None:
        """Save installation tokens for a workspace."""
        installation = Installation(
            team_id=team_id,
            bot_token=bot_token,
            bot_id=bot_user_id,
            bot_user_id=bot_user_id,
        )
        if access_token:
            installation.user_token = access_token
        self.save(installation)

    def get_bot_token(self, team_id: str) -> Optional[str]:
        """Get bot token for a workspace."""
        installation = self._installations.get(team_id)
        return installation.bot_token if installation else None

    def get_bot_user_id(self, team_id: str) -> Optional[str]:
        """Get bot user ID for a workspace."""
        installation = self._installations.get(team_id)
        return installation.bot_user_id if installation else None

    def get_access_token(self, team_id: str) -> Optional[str]:
        """Get access token for a workspace (for user tokens)."""
        installation = self._installations.get(team_id)
        return installation.user_token if installation else None

    def remove_installation(self, team_id: str) -> None:
        """Remove installation for a workspace."""
        self._installations.pop(team_id, None)

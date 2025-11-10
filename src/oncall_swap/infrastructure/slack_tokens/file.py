from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from slack_bolt.oauth import InstallationStore
from slack_sdk.oauth import Installation

from oncall_swap.ports.slack_tokens import SlackTokenStorage


class FileSlackTokenStorage(InstallationStore, SlackTokenStorage):
    """File-based storage for Slack workspace installation tokens.
    
    Stores installations in a JSON file on disk. Implements both slack-bolt's
    InstallationStore interface and our custom SlackTokenStorage interface.
    """

    def __init__(self, storage_path: str | Path) -> None:
        """Initialize file-based token storage.
        
        Args:
            storage_path: Path to the JSON file where installations will be stored.
        """
        self.storage_path = Path(storage_path)
        self._installations: Dict[str, Installation] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load installations from disk."""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    for team_id, installation_data in data.items():
                        try:
                            self._installations[team_id] = Installation(**installation_data)
                        except (TypeError, ValueError) as e:
                            # Skip corrupted installation entries
                            print(f"Warning: Could not load installation for team {team_id}: {e}")
                            continue
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # If file is corrupted, start fresh
                print(f"Warning: Could not load token storage from {self.storage_path}: {e}")
                self._installations = {}

    def _save_to_disk(self) -> None:
        """Save installations to disk."""
        # Ensure directory exists
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Convert installations to serializable format
        data = {}
        for team_id, installation in self._installations.items():
            installation_dict = {
                "team_id": installation.team_id,
                "bot_token": installation.bot_token,
                "bot_id": installation.bot_id,
                "bot_user_id": installation.bot_user_id,
            }
            # Add optional fields only if they exist
            if hasattr(installation, "user_token") and installation.user_token:
                installation_dict["user_token"] = installation.user_token
            if hasattr(installation, "user_id") and installation.user_id:
                installation_dict["user_id"] = installation.user_id
            if hasattr(installation, "user_refresh_token") and installation.user_refresh_token:
                installation_dict["user_refresh_token"] = installation.user_refresh_token
            if hasattr(installation, "user_token_expires_at") and installation.user_token_expires_at:
                installation_dict["user_token_expires_at"] = installation.user_token_expires_at
            if hasattr(installation, "enterprise_id") and installation.enterprise_id:
                installation_dict["enterprise_id"] = installation.enterprise_id
            if hasattr(installation, "installed_at") and installation.installed_at:
                installation_dict["installed_at"] = installation.installed_at
            data[team_id] = installation_dict
        
        # Write atomically using a temporary file
        temp_path = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        try:
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=2)
            temp_path.replace(self.storage_path)
        except Exception as e:
            # Clean up temp file on error
            if temp_path.exists():
                temp_path.unlink()
            raise

    # slack-bolt InstallationStore interface
    def save(self, installation: Installation) -> None:
        """Save installation (slack-bolt interface)."""
        if installation.team_id:
            self._installations[installation.team_id] = installation
            self._save_to_disk()

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
            self._save_to_disk()

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
            self._save_to_disk()

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
        self._save_to_disk()

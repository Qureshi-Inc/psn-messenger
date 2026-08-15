"""Minimal Mattermost DM helper for PSN alerts.

Uses the same bot token/URL/team the portal already uses (env). DMs are sent by
opening a direct channel between the bot and each target user, then posting.
Everything is best-effort and logged; failures never raise into the poller.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE = (os.environ.get("MATTERMOST_URL") or "").rstrip("/")
_TOKEN = os.environ.get("MATTERMOST_TOKEN") or ""

_bot_id: str | None = None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}


def available() -> bool:
    return bool(_BASE and _TOKEN)


def _get_bot_id(client: httpx.Client) -> str | None:
    global _bot_id
    if _bot_id:
        return _bot_id
    r = client.get(f"{_BASE}/api/v4/users/me", headers=_headers(), timeout=15)
    if r.status_code == 200:
        _bot_id = r.json().get("id")
    return _bot_id


def dm_user(username: str, message: str) -> bool:
    """DM a single user by username. Returns True on success."""
    if not available():
        return False
    try:
        with httpx.Client() as client:
            bot_id = _get_bot_id(client)
            if not bot_id:
                return False
            ur = client.get(
                f"{_BASE}/api/v4/users/username/{username}",
                headers=_headers(), timeout=15,
            )
            if ur.status_code != 200:
                logger.warning("mm: user %s not found", username)
                return False
            uid = ur.json().get("id")
            # Open (or get) the bot<->user direct channel.
            cr = client.post(
                f"{_BASE}/api/v4/channels/direct",
                headers=_headers(), json=[bot_id, uid], timeout=15,
            )
            if cr.status_code not in (200, 201):
                logger.warning("mm: direct channel for %s failed: %s", username, cr.status_code)
                return False
            channel_id = cr.json().get("id")
            pr = client.post(
                f"{_BASE}/api/v4/posts",
                headers=_headers(),
                json={"channel_id": channel_id, "message": message},
                timeout=15,
            )
            return pr.status_code in (200, 201)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mm: dm to %s failed: %s", username, exc)
        return False


def dm_users(usernames: list[str], message: str) -> int:
    """DM several users; returns how many succeeded."""
    return sum(1 for u in usernames if dm_user(u, message))

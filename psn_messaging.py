"""PSN messaging via direct API calls with auto-refreshing auth."""

import logging

import httpx

from psn_auth import PSNAuth

logger = logging.getLogger(__name__)

PSN_MESSAGING_BASE = "https://dms.api.playstation.com/api"


class PSNMessenger:
    """Send messages to PSN groups using the PSN API directly."""

    def __init__(self, auth: PSNAuth, group_id: str):
        self._auth = auth
        self._group_id = group_id

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; sdk_gphone_x86 Build/RSR1.201013.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.106 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Country": "US",
        }

    def send_message(self, message: str) -> bool:
        """Send a text message to the configured group."""
        url = f"{PSN_MESSAGING_BASE}/groups/{self._group_id}/threads/{self._group_id}/messages"
        payload = {
            "messageType": 1,
            "body": message,
        }

        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload, headers=self._headers)

        if resp.status_code in (200, 201, 204):
            logger.info("v2: Message sent: %s", message[:50])
            return True

        logger.error("v2: Send failed: %d %s", resp.status_code, resp.text[:200])
        return False

    def get_messages(self, limit: int = 5) -> list[dict]:
        """Get recent messages from the group."""
        url = f"{PSN_MESSAGING_BASE}/members/me/groups/{self._group_id}/threads/{self._group_id}/messages"
        params = {"limit": str(limit)}

        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params, headers=self._headers)

        if resp.status_code != 200:
            logger.error("v2: Get messages failed: %d", resp.status_code)
            return []

        data = resp.json()
        messages = []
        for msg in data.get("messages", []):
            messages.append({
                "sender": msg.get("sender", {}).get("onlineId", "unknown"),
                "body": msg.get("body", ""),
                "timestamp": msg.get("createdTimestamp", ""),
            })
        return messages

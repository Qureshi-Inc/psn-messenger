"""PSN messaging via direct API calls with auto-refreshing auth."""

import logging

import httpx

from psn_auth import PSNAuth

logger = logging.getLogger(__name__)

PSN_MESSAGING_BASE = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"
# psnawp uses /members/me/groups/... for GET; /groups/... for POST (send)
PSN_MESSAGES_GET = PSN_MESSAGING_BASE + "/members/me/groups/{group_id}/threads/{group_id}/messages"
PSN_MESSAGES_POST = PSN_MESSAGING_BASE + "/groups/{group_id}/threads/{group_id}/messages"
PSN_MEDIA_GET = PSN_MESSAGING_BASE + "/members/me/groups/{group_id}/threads/{group_id}/messages/{message_uid}/contentList/0"


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
        url = PSN_MESSAGES_POST.format(group_id=self._group_id)
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
        """Get recent messages from the group (correct /members/me/ URL)."""
        url = PSN_MESSAGES_GET.format(group_id=self._group_id)
        params = {"limit": str(limit), "includeReactions": "true"}

        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params, headers=self._headers)

        if resp.status_code != 200:
            logger.error("v2: Get messages failed: %d %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        raw = data.get("messages") or data.get("threadMessages") or []
        messages = []
        for msg in raw:
            sender_obj = msg.get("sender", {})
            msg_type = msg.get("messageType", 1)
            if msg_type != 1:
                logger.info("non-text message: type=%s uid=%s raw=%s", msg_type, msg.get("messageUid"), msg)
            messages.append({
                "sender": sender_obj.get("onlineId", "unknown") if isinstance(sender_obj, dict) else str(sender_obj),
                "body": msg.get("body", ""),
                "timestamp": msg.get("createdTimestamp", msg.get("messageUid", "")),
                "messageUid": msg.get("messageUid", ""),
                "messageType": msg_type,
                "reactions": msg.get("reactions", []),
            })
        return messages

    def download_media(self, message_uid: str) -> bytes | None:
        """Download binary media content for a non-text message."""
        url = PSN_MEDIA_GET.format(group_id=self._group_id, message_uid=message_uid)
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, headers=self._headers)
        if resp.status_code != 200:
            logger.error("media download failed: %d %s", resp.status_code, resp.text[:200])
            return None
        return resp.content

    def get_media_url(self, message_uid: str) -> str:
        """Return the resolved (after redirect) CDN URL for a media message."""
        url = PSN_MEDIA_GET.format(group_id=self._group_id, message_uid=message_uid)
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            resp = client.get(url, headers=self._headers)
        if resp.status_code in (301, 302, 303, 307, 308):
            return resp.headers.get("location", "")
        if resp.status_code == 200:
            return url
        logger.error("get_media_url failed: %d", resp.status_code)
        return ""


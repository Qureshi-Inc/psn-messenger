"""PSN messaging via direct API calls with auto-refreshing auth."""

import logging

import httpx

from psn_auth import PSNAuth

logger = logging.getLogger(__name__)

PSN_MESSAGING_BASE = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"
# psnawp uses /members/me/groups/... for GET; /groups/... for POST (send)
PSN_MESSAGES_GET = PSN_MESSAGING_BASE + "/members/me/groups/{group_id}/threads/{group_id}/messages"
PSN_MESSAGES_POST = PSN_MESSAGING_BASE + "/groups/{group_id}/threads/{group_id}/messages"
PSN_UGC_BASE = "https://ugc.np.community.playstation.net/ugc/v1"


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
            detail = msg.get("messageDetail", {})
            ugc_id = (detail.get("videoMessageDetail") or {}).get("ugcId", "")
            messages.append({
                "sender": sender_obj.get("onlineId", "unknown") if isinstance(sender_obj, dict) else str(sender_obj),
                "body": msg.get("body", ""),
                "timestamp": msg.get("createdTimestamp", msg.get("messageUid", "")),
                "messageUid": msg.get("messageUid", ""),
                "messageType": msg_type,
                "ugcId": ugc_id,
                "reactions": msg.get("reactions", []),
            })
        return messages

    def download_clip(self, ugc_id: str) -> bytes | None:
        """Download a PSN GameShare video clip by its UGC ID."""
        # Step 1: get the download URL from the UGC metadata endpoint
        meta_url = f"{PSN_UGC_BASE}/contents/{ugc_id}"
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            meta = client.get(meta_url, headers=self._headers)
        if meta.status_code != 200:
            logger.error("ugc meta failed: %d %s", meta.status_code, meta.text[:200])
            return None
        download_url = meta.json().get("mediaDownloadUrl") or meta.json().get("contentUrl", "")
        if not download_url:
            logger.error("ugc meta: no download URL in response: %s", meta.text[:300])
            return None
        # Step 2: download the actual video bytes
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            resp = client.get(download_url)
        if resp.status_code != 200:
            logger.error("clip download failed: %d", resp.status_code)
            return None
        return resp.content


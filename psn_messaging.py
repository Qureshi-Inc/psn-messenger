"""PSN messaging via direct API calls with auto-refreshing auth."""

import logging

import httpx

from psn_auth import PSNAuth

logger = logging.getLogger(__name__)

PSN_MESSAGING_BASE = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"
# psnawp uses /members/me/groups/... for GET; /groups/... for POST (send)
PSN_MESSAGES_GET = PSN_MESSAGING_BASE + "/members/me/groups/{group_id}/threads/{group_id}/messages"
PSN_MESSAGES_POST = PSN_MESSAGING_BASE + "/groups/{group_id}/threads/{group_id}/messages"
PSN_GAME_MEDIA = "https://m.np.playstation.com/api/gameMediaService/v2/c2s"


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

    def _gms_headers(self) -> dict[str, str]:
        """Headers for gameMediaService — needs PlayStationApp-Android UA."""
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "User-Agent": "PlayStationApp-Android",
            "Accept": "application/json",
            "Accept-Language": "en-US",
            "Country": "US",
        }

    def get_clip_urls(self, ugc_id: str) -> dict:
        """Return download/preview URLs for a PSN GameShare clip via gameMediaService.

        Returns dict with keys: downloadUrl, videoUrl, largePreviewImage, title, sender.
        Returns empty dict on failure.
        """
        url = f"{PSN_GAME_MEDIA}/ugc/{ugc_id}/url"
        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=self._gms_headers())
        if r.status_code != 200:
            logger.error("gameMediaService url failed: %d %s", r.status_code, r.text[:200])
            return {}
        return r.json()

    def download_clip(self, ugc_id: str) -> bytes | None:
        """Download a PSN GameShare clip as MP4 bytes via gameMediaService."""
        urls = self.get_clip_urls(ugc_id)
        download_url = urls.get("downloadUrl") or urls.get("videoUrl", "")
        if not download_url:
            logger.error("download_clip: no URL from gameMediaService for %s", ugc_id)
            return None
        # Prefer direct MP4 over HLS playlist
        if download_url.endswith(".m3u8") and urls.get("downloadUrl"):
            download_url = urls["downloadUrl"]
        logger.info("download_clip: fetching %s", download_url[:80])
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            resp = client.get(download_url)
        if resp.status_code != 200:
            logger.error("download_clip: fetch failed %d", resp.status_code)
            return None
        logger.info("download_clip: got %d bytes", len(resp.content))
        return resp.content


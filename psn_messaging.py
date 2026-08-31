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
        """Download a PSN GameShare clip via gameMediaService.

        Prefers a direct downloadUrl (MP4). Falls back to HLS reassembly
        when only videoUrl (m3u8) is present.
        """
        urls = self.get_clip_urls(ugc_id)
        direct = urls.get("downloadUrl", "")
        hls = urls.get("videoUrl", "")

        if not direct and not hls:
            logger.error("download_clip: no URL for ugcId=%s", ugc_id)
            return None

        if direct and not direct.endswith(".m3u8"):
            logger.info("download_clip: direct MP4 %s", direct[:80])
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                resp = client.get(direct)
            if resp.status_code != 200:
                logger.error("download_clip: direct fetch failed %d", resp.status_code)
                return None
            logger.info("download_clip: got %d bytes", len(resp.content))
            return resp.content

        # No direct MP4 — reassemble from HLS
        return self._download_hls(hls, ugc_id)

    def _download_hls(self, master_url: str, ugc_id: str) -> bytes | None:
        """Download an HLS stream by fetching all segments and concatenating."""
        base_dir = master_url.split("?")[0].rsplit("/", 1)[0] + "/"
        qs = ("?" + master_url.split("?")[1]) if "?" in master_url else ""

        with httpx.Client(timeout=15) as client:
            resp = client.get(master_url)
        if resp.status_code != 200:
            logger.error("HLS master fetch failed: %d", resp.status_code)
            return None

        # Pick first (highest bandwidth) variant from master playlist
        variant = next(
            (l.strip() for l in resp.text.splitlines() if l and not l.startswith("#")),
            None,
        )
        if not variant:
            logger.error("HLS: no variant in master for ugcId=%s", ugc_id)
            return None

        with httpx.Client(timeout=15) as client:
            resp = client.get(base_dir + variant + qs)
        if resp.status_code != 200:
            logger.error("HLS variant fetch failed: %d", resp.status_code)
            return None

        segments = [l.strip() for l in resp.text.splitlines() if l and not l.startswith("#")]
        logger.info("HLS: %d segments for ugcId=%s", len(segments), ugc_id)

        # Download all segments into one MPEG-TS buffer
        ts_buf = bytearray()
        with httpx.Client(timeout=30) as client:
            for seg in segments:
                r = client.get(base_dir + seg + qs)
                if r.status_code != 200:
                    logger.error("HLS segment failed: %s %d", seg, r.status_code)
                    return None
                ts_buf.extend(r.content)

        logger.info("HLS: downloaded %d TS bytes from %d segments, converting to MP4",
                    len(ts_buf), len(segments))

        # Remux MPEG-TS → MP4 using ffmpeg (no re-encode, copy streams)
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as ts_file:
            ts_file.write(ts_buf)
            ts_path = ts_file.name
        mp4_path = ts_path.replace(".ts", ".mp4")
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", ts_path, "-c", "copy", "-movflags", "+faststart", mp4_path],
                capture_output=True, timeout=120,
            )
            if result.returncode != 0:
                logger.error("ffmpeg failed: %s", result.stderr[-500:].decode(errors="replace"))
                return None
            with open(mp4_path, "rb") as f:
                mp4_bytes = f.read()
        finally:
            import os as _os
            _os.unlink(ts_path)
            if _os.path.exists(mp4_path):
                _os.unlink(mp4_path)

        logger.info("HLS: MP4 is %d bytes for ugcId=%s", len(mp4_bytes), ugc_id)
        return mp4_bytes


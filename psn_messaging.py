"""PSN messaging via direct API calls with auto-refreshing auth."""

import dataclasses
import hashlib
import ipaddress
import json as _json
import logging
import re
import subprocess
import tempfile
import os as _os
from urllib.parse import urlparse

import httpx

from psn_auth import PSNAuth

logger = logging.getLogger(__name__)

PSN_MESSAGING_BASE = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"
PSN_MESSAGES_GET = PSN_MESSAGING_BASE + "/members/me/groups/{group_id}/threads/{group_id}/messages"
PSN_MESSAGES_POST = PSN_MESSAGING_BASE + "/groups/{group_id}/threads/{group_id}/messages"
PSN_GAME_MEDIA = "https://m.np.playstation.com/api/gameMediaService/v2/c2s"

_ALLOWED_HOSTS = {
    "playstation.com",
    "playstation.net",
    "sonyinteractive.com",
    "cloudfront.net",
    "akamaihd.net",
    "dl.playstation.net",
}

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
]


# ── Exceptions ────────────────────────────────────────────────────────────────

class ClipNotReady(Exception):
    """PSN CDN is still transcoding this clip."""


class ClipUnauthorized(Exception):
    """PSN returned 401; token needs refreshing."""


class ClipRateLimited(Exception):
    def __init__(self, retry_after: int = 30):
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry in {retry_after}s")


class ClipError(Exception):
    """General download/archive/send error."""


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclasses.dataclass
class ClipDownload:
    data: bytes
    sha256: str | None = None
    file_size: int | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_sample_rate: int | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_psn_url(url: str) -> None:
    """Raise ClipError for unsafe or unexpected URLs."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ClipError(f"unsafe URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ClipError("URL has no hostname")
    # Block private/loopback addresses (SSRF protection)
    try:
        addr = ipaddress.ip_address(host)
        for net in _PRIVATE_NETS:
            if addr in net:
                raise ClipError(f"SSRF risk: private address {host}")
    except ValueError:
        pass  # hostname, not a numeric IP
    # Allow only known Sony/CDN domains
    if not any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS):
        raise ClipError(f"URL host not in allowlist: {host!r}")


def _probe_clip(data: bytes) -> dict:
    """Run ffprobe on MP4 bytes. Returns raw ffprobe dict (or {} on failure)."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", tmp],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("ffprobe failed: %s",
                           result.stderr[-300:].decode(errors="replace"))
            return {}
        return _json.loads(result.stdout)
    except Exception as exc:
        logger.warning("ffprobe exception: %s", exc)
        return {}
    finally:
        _os.unlink(tmp)


def _parse_probe(probe: dict) -> dict:
    """Extract duration, resolution, codecs from ffprobe output."""
    meta: dict = {}
    fmt = probe.get("format", {})
    try:
        dur = float(fmt.get("duration") or 0)
        meta["duration_seconds"] = dur if dur > 0 else None
    except (ValueError, TypeError):
        meta["duration_seconds"] = None

    for s in probe.get("streams", []):
        ctype = s.get("codec_type", "")
        if ctype == "video" and "video_codec" not in meta:
            meta["video_codec"] = s.get("codec_name")
            try:
                meta["width"]  = int(s["width"])  if s.get("width")  else None
                meta["height"] = int(s["height"]) if s.get("height") else None
            except (ValueError, TypeError):
                pass
            fps_str = s.get("r_frame_rate", "")
            try:
                num, den = fps_str.split("/")
                meta["fps"] = round(float(num) / float(den), 3)
            except Exception:
                meta["fps"] = None
        elif ctype == "audio" and "audio_codec" not in meta:
            meta["audio_codec"] = s.get("codec_name")
            try:
                meta["audio_sample_rate"] = int(s["sample_rate"]) if s.get("sample_rate") else None
            except (ValueError, TypeError):
                pass
    return meta


def _package(data: bytes, ugc_id: str) -> ClipDownload:
    """Compute sha256 + ffprobe metadata and return ClipDownload."""
    sha = hashlib.sha256(data).hexdigest()
    probe = _probe_clip(data)
    meta = _parse_probe(probe)
    logger.info("clip_ffprobe_complete ugcId=%s size=%d sha=%s dur=%.1f %dx%d",
                ugc_id, len(data), sha[:8], meta.get("duration_seconds") or 0,
                meta.get("width") or 0, meta.get("height") or 0)
    return ClipDownload(
        data=data,
        sha256=sha,
        file_size=len(data),
        **meta,
    )


# ── PSNMessenger class ────────────────────────────────────────────────────────

class PSNMessenger:
    """Send/receive messages and download clips for a single PSN group."""

    def __init__(self, auth: PSNAuth, group_id: str, group_name: str = ""):
        self._auth = auth
        self._group_id = group_id
        self._group_name = group_name or group_id

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 11; sdk_gphone_x86 Build/RSR1.201013.001; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/83.0.4103.106 Mobile Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Country": "US",
        }

    def send_message(self, message: str) -> bool:
        """Send a text message to the configured group."""
        url = PSN_MESSAGES_POST.format(group_id=self._group_id)
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json={"messageType": 1, "body": message},
                               headers=self._headers)
        if resp.status_code in (200, 201, 204):
            logger.info("v2: Message sent: %s", message[:50])
            return True
        logger.error("v2: Send failed: %d %s", resp.status_code, resp.text[:200])
        return False

    def get_messages(self, limit: int = 5) -> list[dict]:
        """Get recent messages from the group."""
        url = PSN_MESSAGES_GET.format(group_id=self._group_id)
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params={"limit": str(limit), "includeReactions": "true"},
                              headers=self._headers)
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
                "sender": (sender_obj.get("onlineId", "unknown")
                           if isinstance(sender_obj, dict) else str(sender_obj)),
                "body": msg.get("body", ""),
                "timestamp": msg.get("createdTimestamp", msg.get("messageUid", "")),
                "messageUid": msg.get("messageUid", ""),
                "messageType": msg_type,
                "ugcId": ugc_id,
                "reactions": msg.get("reactions", []),
            })
        return messages

    def get_messages_raw(self, limit: int = 10) -> dict:
        url = PSN_MESSAGES_GET.format(group_id=self._group_id)
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params={"limit": str(limit), "includeReactions": "true"},
                              headers=self._headers)
        return resp.json() if resp.status_code == 200 else {"error": resp.status_code}

    def _gms_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "User-Agent": "PlayStationApp-Android",
            "Accept": "application/json",
            "Accept-Language": "en-US",
            "Country": "US",
        }

    def _get_clip_urls(self, ugc_id: str) -> dict:
        """Fetch clip URLs from gameMediaService. Raises on auth/rate-limit errors."""
        url = f"{PSN_GAME_MEDIA}/ugc/{ugc_id}/url"
        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=self._gms_headers())
        if r.status_code == 401:
            raise ClipUnauthorized("gameMediaService 401")
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "30"))
            raise ClipRateLimited(retry_after)
        if r.status_code != 200:
            raise ClipError(f"gameMediaService {r.status_code}: {r.text[:100]}")
        return r.json()

    # Keep old name for backward compat (used by /api/reactions fallback etc.)
    def get_clip_urls(self, ugc_id: str) -> dict:
        try:
            return self._get_clip_urls(ugc_id)
        except (ClipUnauthorized, ClipRateLimited, ClipError):
            return {}

    def download_clip(self, ugc_id: str) -> ClipDownload:
        """Download a PSN GameShare clip.

        Returns ClipDownload on success.
        Raises ClipNotReady if PSN CDN is still transcoding.
        Raises ClipUnauthorized, ClipRateLimited, ClipError on other failures.
        """
        logger.info("clip_resolve_started ugcId=%s", ugc_id)
        urls = self._get_clip_urls(ugc_id)
        direct = urls.get("downloadUrl", "")
        hls    = urls.get("videoUrl", "")

        if not direct and not hls:
            logger.info("clip_resolve: no URLs, media still processing ugcId=%s", ugc_id)
            raise ClipNotReady(f"no media URLs for ugcId={ugc_id}")

        logger.info("clip_resolved ugcId=%s direct=%s hls=%s",
                    ugc_id, bool(direct), bool(hls))

        if direct and not direct.endswith(".m3u8"):
            _validate_psn_url(direct)
            logger.info("clip_download_started ugcId=%s method=direct", ugc_id)
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                resp = client.get(direct)
            if resp.status_code != 200:
                raise ClipError(f"direct MP4 fetch failed: HTTP {resp.status_code}")
            data = resp.content
            if len(data) < 1024:
                raise ClipNotReady(
                    f"direct URL returned only {len(data)} bytes — likely not ready"
                )
            logger.info("clip_downloaded ugcId=%s size=%d method=direct", ugc_id, len(data))
            return _package(data, ugc_id)

        _validate_psn_url(hls)
        return self._download_hls(hls, ugc_id)

    def _download_hls(self, master_url: str, ugc_id: str) -> ClipDownload:
        """Download HLS stream and remux to MP4 via ffmpeg.

        Fetches master + variant playlists, rewrites relative segment paths to
        absolute signed URLs, writes a temp .m3u8, then lets ffmpeg fetch all
        segments and stream-copy to MP4.
        """
        base_dir = master_url.split("?")[0].rsplit("/", 1)[0] + "/"
        qs = ("?" + master_url.split("?")[1]) if "?" in master_url else ""

        logger.info("clip_download_started ugcId=%s method=hls", ugc_id)

        with httpx.Client(timeout=15) as client:
            resp = client.get(master_url)
        if resp.status_code != 200:
            raise ClipError(f"HLS master fetch failed: HTTP {resp.status_code}")

        variant = next(
            (l.strip() for l in resp.text.splitlines() if l.strip() and not l.startswith("#")),
            None,
        )
        if not variant:
            raise ClipNotReady(f"HLS master has no variant for ugcId={ugc_id}")

        with httpx.Client(timeout=15) as client:
            resp = client.get(base_dir + variant + qs)
        if resp.status_code != 200:
            raise ClipError(f"HLS variant fetch failed: HTTP {resp.status_code}")

        # Rewrite relative segment paths → absolute signed URLs
        lines = []
        seg_count = 0
        for line in resp.text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                lines.append(base_dir + s + qs)
                seg_count += 1
            else:
                lines.append(line)

        if seg_count == 0:
            raise ClipNotReady(f"HLS variant has no segments for ugcId={ugc_id}")

        logger.info("HLS: %d segments for ugcId=%s", seg_count, ugc_id)

        with tempfile.NamedTemporaryFile(suffix=".m3u8", mode="w", delete=False) as f:
            f.write("\n".join(lines))
            m3u8_path = f.name
        mp4_path = m3u8_path.replace(".m3u8", ".mp4")
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", m3u8_path,
                 "-c", "copy", "-movflags", "+faststart", mp4_path],
                capture_output=True, timeout=300,
            )
            if result.returncode != 0:
                raise ClipError(
                    "ffmpeg failed: " + result.stderr[-400:].decode(errors="replace")
                )
            with open(mp4_path, "rb") as f:
                mp4_bytes = f.read()

            logger.info("clip_downloaded ugcId=%s size=%d method=hls segments=%d",
                        ugc_id, len(mp4_bytes), seg_count)
            return _package(mp4_bytes, ugc_id)
        finally:
            _os.unlink(m3u8_path)
            if _os.path.exists(mp4_path):
                _os.unlink(mp4_path)

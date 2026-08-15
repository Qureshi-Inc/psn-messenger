"""Read-only PSN data (presence, profiles) for the dashboard.

Uses the bot's own auth (PSNAuth) to query the PSN API for the accounts we've
linked via the portal, plus the bot's friends list. Everything here is
best-effort: any lookup that fails returns a safe empty/offline value so the
dashboard always renders.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

USERS_DIR = Path("/data/users")

API = "https://m.np.playstation.com/api/userProfile/v1/internal/users"
PROFILE2 = "https://us-prof.np.community.playstation.net/userProfile/v1/users"

_UA = (
    "Mozilla/5.0 (Linux; Android 11; sdk_gphone_x86 Build/RSR1.201013.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.106 "
    "Mobile Safari/537.36"
)


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept-Language": "en-US,en;q=0.9",
        "Country": "US",
        "User-Agent": _UA,
    }


def linked_accounts() -> list[dict]:
    """The people who linked via the portal (mm_username + PSN ids)."""
    out: list[dict] = []
    if not USERS_DIR.exists():
        return out
    for f in sorted(USERS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        if d.get("account_id"):
            out.append(
                {
                    "mm_username": d.get("mm_username"),
                    "online_id": d.get("online_id"),
                    "account_id": str(d.get("account_id")),
                }
            )
    return out


def _presence(client: httpx.Client, token: str, account_id: str) -> dict:
    """Online status + current game for one account."""
    try:
        r = client.get(
            f"{API}/{account_id}/basicPresences",
            params={"type": "primary"},
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code != 200:
            return {"online": False}
        bp = r.json().get("basicPresence", {})
        plat = bp.get("primaryPlatformInfo", {})
        status = plat.get("onlineStatus", "offline")
        # gameTitleInfoList appears only while actively in a game.
        games = bp.get("gameTitleInfoList") or []
        game = games[0].get("titleName") if games else None
        return {
            "online": status == "online",
            "status": status,
            "platform": plat.get("platform"),
            "game": game,
            "last_online": plat.get("lastOnlineDate"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("presence lookup failed for %s: %s", account_id, exc)
        return {"online": False}


def _avatar(client: httpx.Client, token: str, online_id: str) -> str | None:
    if not online_id:
        return None
    try:
        r = client.get(
            f"{PROFILE2}/{online_id}/profile2",
            params={"fields": "onlineId,avatarUrls,plus"},
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code != 200:
            return None
        prof = r.json().get("profile", {})
        urls = prof.get("avatarUrls") or []
        if urls:
            # Prefer HTTPS so the dashboard doesn't get mixed-content blocked.
            return urls[0].get("avatarUrl", "").replace("http://", "https://")
    except Exception:  # noqa: BLE001
        return None
    return None


def squad_status(auth) -> list[dict]:
    """Full presence + avatar for every linked account. `auth` is a PSNAuth."""
    accounts = linked_accounts()
    if not accounts:
        return []
    token = auth.access_token
    out: list[dict] = []
    with httpx.Client() as client:
        for a in accounts:
            pres = _presence(client, token, a["account_id"])
            out.append(
                {
                    **a,
                    **pres,
                    "avatar": _avatar(client, token, a.get("online_id")),
                }
            )
    # Online first, then by name.
    out.sort(key=lambda x: (not x.get("online"), (x.get("online_id") or "").lower()))
    return out

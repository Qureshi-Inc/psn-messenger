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
    """The people who linked via the portal (mm_username + PSN ids).

    Includes the on-disk file path so we can mint that user's OWN access token
    for their presence lookup -- a self-query is authoritative and not subject
    to the bot's friend-visibility/privacy settings.
    """
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
                    "_file": str(f),
                }
            )
    return out


def _user_token(account: dict) -> str | None:
    """Fresh access token for a linked user, using their stored refresh/NPSSO.

    Refreshes in-place and persists the rotated tokens so the file stays valid.
    Returns None if the user can't be re-authed (they'd need to re-link).
    """
    path = account.get("_file")
    if not path:
        return None
    try:
        d = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return None

    import time

    # Reuse a still-valid access token.
    if d.get("access_token") and time.time() < d.get("expires_at", 0) - 300:
        return d["access_token"]

    # Otherwise refresh (preferred) or fall back to the stored NPSSO.
    from portal import _exchange_npsso  # local import avoids a cycle at import time

    tok_data = None
    refresh = d.get("refresh_token")
    if refresh and time.time() < d.get("refresh_expires_at", 0):
        try:
            import httpx as _httpx

            from psn_auth import AUTH_HEADER, SCOPE, TOKEN_URL

            with _httpx.Client(timeout=15) as c:
                r = c.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                        "scope": SCOPE,
                        "token_format": "jwt",
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": AUTH_HEADER,
                    },
                )
            if r.status_code == 200:
                tok_data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("user token refresh failed for %s: %s", d.get("online_id"), exc)

    if tok_data is None and d.get("npsso"):
        try:
            tok_data = _exchange_npsso(d["npsso"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("user npsso re-exchange failed for %s: %s", d.get("online_id"), exc)
            return None

    if not tok_data:
        return None

    # Persist rotated tokens so the file keeps working.
    now = time.time()
    d["access_token"] = tok_data["access_token"]
    d["refresh_token"] = tok_data.get("refresh_token", refresh)
    d["expires_at"] = now + tok_data.get("expires_in", 3600)
    d["refresh_expires_at"] = now + tok_data.get("refresh_token_expires_in", 7776000)
    try:
        Path(path).write_text(json.dumps(d, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return d["access_token"]


def _presence(client: httpx.Client, token: str, account_id: str, own: bool = False) -> dict:
    """Online status + current game for one account.

    When ``own`` is True the token belongs to this account, so we query the
    self endpoint (``/me``) -- authoritative and immune to friend-visibility
    settings. Otherwise we look the account up by id with the bot's token.
    """
    who = "me" if own else account_id
    try:
        r = client.get(
            f"{API}/{who}/basicPresences",
            params={"type": "primary"},
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code != 200:
            return {"online": False}
        bp = r.json().get("basicPresence", {})
        plat = bp.get("primaryPlatformInfo", {})
        status = plat.get("onlineStatus", "offline")
        online = status == "online"
        # gameTitleInfoList appears only while actively in a game; it carries the
        # title name and the game's cover art (conceptIconUrl / npTitleIconUrl).
        games = bp.get("gameTitleInfoList") or []
        game = game_icon = None
        if games:
            g = games[0]
            game = g.get("titleName")
            game_icon = g.get("conceptIconUrl") or g.get("npTitleIconUrl")
            if game_icon:
                game_icon = game_icon.replace("http://", "https://")
        return {
            "online": online,
            "status": status,
            "playing": bool(game),  # actively in a game (vs. just online on menus)
            "platform": plat.get("platform"),
            "game": game,
            "game_icon": game_icon,
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
    """Full presence + avatar for every linked account. `auth` is a PSNAuth.

    Prefers each user's OWN token for their presence (self-query, authoritative
    and privacy-proof); falls back to the bot's token by account id if a user's
    token can't be refreshed.
    """
    accounts = linked_accounts()
    if not accounts:
        return []
    bot_token = auth.access_token
    out: list[dict] = []
    with httpx.Client() as client:
        for a in accounts:
            utok = _user_token(a)
            if utok:
                pres = _presence(client, utok, a["account_id"], own=True)
            else:
                # No usable user token -> look them up with the bot's token.
                pres = _presence(client, bot_token, a["account_id"])
            # Avatars are public; the bot's token is fine and avoids extra work.
            entry = {k: v for k, v in a.items() if not k.startswith("_")}
            out.append(
                {
                    **entry,
                    **pres,
                    "avatar": _avatar(client, bot_token, a.get("online_id")),
                }
            )
    # Sort: in-game first, then online-on-menus, then offline; then by name.
    out.sort(
        key=lambda x: (
            not x.get("playing"),
            not x.get("online"),
            (x.get("online_id") or "").lower(),
        )
    )
    return out

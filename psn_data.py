"""Read-only PSN data (presence, profiles) for the dashboard.

Uses the bot's own auth (PSNAuth) to query the PSN API for the accounts we've
linked via the portal, plus the bot's friends list. Everything here is
best-effort: any lookup that fails returns a safe empty/offline value so the
dashboard always renders.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

USERS_DIR = Path("/data/users")

# --- PSN call protection -----------------------------------------------------
# Friends' accounts must never see a brute-force pattern. We serve squad_status
# from a shared cache so that no matter how many people/tabs open the dashboard
# (each polling every 30s), PSN is queried at most once per CACHE_TTL. A lock
# ensures concurrent viewers coalesce onto a single refresh instead of stampeding.
CACHE_TTL = 55.0  # seconds; one real PSN sweep per ~minute, max
_cache: dict = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()

# Trophy summaries + avatars barely change, so we cache them per-account for a
# long TTL. That way the per-minute presence poll only makes the light
# presence+gamelist calls, keeping total PSN load low.
_SLOW_TTL = 30 * 60  # 30 min
_slow_cache: dict = {}  # account_id -> {"at": ts, "trophy": {...}, "avatar": str}

API = "https://m.np.playstation.com/api/userProfile/v1/internal/users"
PROFILE2 = "https://us-prof.np.community.playstation.net/userProfile/v1/users"
TROPHY = "https://m.np.playstation.com/api/trophy/v1"
GAMELIST = "https://m.np.playstation.com/api/gamelist/v2"

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


def _trophy_summary(client: httpx.Client, token: str, account_id: str, own: bool) -> dict:
    """Trophy level, tier, points and platinum/gold/silver/bronze counts."""
    who = "me" if own else account_id
    try:
        r = client.get(
            f"{TROPHY}/users/{who}/trophySummary",
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        d = r.json()
        e = d.get("earnedTrophies", {})
        return {
            "trophy_level": d.get("trophyLevel"),
            "trophy_tier": d.get("tier"),
            "trophy_progress": d.get("progress"),
            "platinum": e.get("platinum", 0),
            "gold": e.get("gold", 0),
            "silver": e.get("silver", 0),
            "bronze": e.get("bronze", 0),
            "trophy_total": sum(
                e.get(k, 0) for k in ("platinum", "gold", "silver", "bronze")
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("trophy summary failed for %s: %s", account_id, exc)
        return {}


# If a WHITELISTED game was last played within this window we treat the person
# as actively playing it. The presence API is broken for these accounts, so the
# gamelist lastPlayedDateTime is the signal (it lags, hence a generous window).
RECENT_PLAY_WINDOW_SEC = 90 * 60

# ONLY these count as "gaming" (per moiz). Anything else -- other games, media
# apps like Prime Video/Netflix -- means offline. Substring, case-insensitive.
GAME_WHITELIST = [
    "arc raider",
    "call of duty",
    "battlefield",
    "big walk",
    "dying light",
]


def _is_whitelisted(name: str) -> bool:
    n = (name or "").lower()
    return any(g in n for g in GAME_WHITELIST)


def _recent_game(client: httpx.Client, token: str) -> dict:
    """Most recent WHITELISTED game (self-token only) with art + freshness.

    Scans the recent-titles list and picks the newest entry whose name is in
    GAME_WHITELIST -- everything else (other games, media apps) is ignored, so a
    person is only ever 'playing'/'last game' for one of the approved titles.
    recent_active is True when that game was played within the window.
    """
    try:
        r = client.get(
            f"{GAMELIST}/users/me/titles",
            params={"categories": "ps4_game,ps5_native_game", "limit": "20"},
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        titles = r.json().get("titles") or []
        # Titles come newest-first; take the first whitelisted one.
        t = next((x for x in titles if _is_whitelisted(x.get("name"))), None)
        if not t:
            return {}
        img = (t.get("imageUrl") or "").replace("http://", "https://")
        last = t.get("lastPlayedDateTime")
        active = False
        if last:
            try:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                active = 0 <= age <= RECENT_PLAY_WINDOW_SEC
            except Exception:  # noqa: BLE001
                pass
        return {
            "recent_game": t.get("name"),
            "recent_game_icon": img,
            "recent_played_at": last,
            "recent_active": active,
        }
    except Exception:  # noqa: BLE001
        return {}


def squad_status(auth, include_stats: bool = True) -> list[dict]:
    """Cached wrapper around the PSN sweep -- see module-level cache notes.

    Returns cached data if it's younger than CACHE_TTL. Under the lock, a second
    check prevents a thundering herd from all triggering refreshes at once. If a
    refresh errors, we keep serving the last good data rather than hammering PSN.
    """
    now = time.time()
    if _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL:
        return _cache["data"]
    with _cache_lock:
        now = time.time()
        if _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL:
            return _cache["data"]
        try:
            data = _squad_status_uncached(auth, include_stats)
            _cache["data"] = data
            _cache["at"] = time.time()
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("squad refresh failed; serving stale: %s", exc)
            return _cache["data"] or []


def _squad_status_uncached(auth, include_stats: bool = True) -> list[dict]:
    """Full presence (+ optional trophy stats) for every linked account.

    Prefers each user's OWN token for presence/stats (self-query, authoritative
    and privacy-proof); falls back to the bot's token by account id if a user's
    token can't be refreshed. `auth` is a PSNAuth.
    """
    # Members = portal-linked accounts only. We deliberately do NOT surface the
    # bot's PSN friend graph -- showing accounts we can only see via the bot's
    # NPSSO friend access (and their friends) isn't something we expose.
    accounts = linked_accounts()
    if not accounts:
        return []
    bot_token = auth.access_token
    out: list[dict] = []
    with httpx.Client() as client:
        for a in accounts:
            utok = _user_token(a)  # only linked accounts have a token file
            own = bool(utok)
            tok = utok or bot_token
            pres = _presence(client, tok, a["account_id"], own=own)
            entry = {k: v for k, v in a.items() if not k.startswith("_")}
            entry.update(pres)
            entry["linked"] = own
            # Trophy + avatar from the slow cache (30 min) so the per-minute
            # poll doesn't re-fetch them every time.
            slow = _slow_cache.get(a["account_id"])
            if not slow or (time.time() - slow["at"]) > _SLOW_TTL:
                slow = {
                    "at": time.time(),
                    "avatar": _avatar(client, bot_token, a.get("online_id")),
                    "trophy": _trophy_summary(client, tok, a["account_id"], own)
                    if include_stats else {},
                }
                _slow_cache[a["account_id"]] = slow
            entry["avatar"] = slow["avatar"]
            entry.update(slow["trophy"])
            # Presence + current game are cheap and time-sensitive: always fresh.
            if own:
                entry.update(_recent_game(client, tok))
            # The presence API often reports "offline" even while someone is in
            # a game (esp. cross-play titles). The game list's lastPlayedDateTime
            # is real-time, so if they played within the recent window we treat
            # them as online + playing that title.
            if entry.get("recent_active") and not entry.get("playing"):
                entry["online"] = True
                entry["playing"] = True
                entry["game"] = entry.get("game") or entry.get("recent_game")
                entry["game_icon"] = entry.get("game_icon") or entry.get("recent_game_icon")
            out.append(entry)
    # Sort: in-game first, then online-on-menus, then offline; then by name.
    out.sort(
        key=lambda x: (
            not x.get("playing"),
            not x.get("online"),
            (x.get("online_id") or "").lower(),
        )
    )
    return out

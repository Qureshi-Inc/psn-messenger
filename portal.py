"""Self-service PSN linking portal.

Friends open the portal, sign into PlayStation, land on Sony's NPSSO token
page, copy the one value, and paste it back here. From that single paste this
module does everything else automatically:

  * exchanges the NPSSO for access + refresh tokens (same flow as psn_auth)
  * auto-detects the person's PSN online id + account id from the token
  * stores the tokens PER USER under /data/users/<account_id>.json

Why the paste can't be avoided: the NPSSO is a cookie on Sony's own domain
(account.sony.com). Browser same-origin rules mean no web page we host can read
it -- the logged-in user has to hand the value over once. After that first
paste the refresh token (~90 days, rolling) keeps the account linked with zero
further action.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

# Reuse the exact OAuth constants/flow the bot already uses.
from psn_auth import (
    AUTH_HEADER,
    AUTH_URL,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
)

logger = logging.getLogger(__name__)

USERS_DIR = Path("/data/users")

# Sony endpoints the friend visits in their browser.
# Signing in here, then hitting the ssocookie URL, reveals their {"npsso":"..."}.
PSN_LOGIN_URL = "https://www.playstation.com/"
NPSSO_TOKEN_URL = "https://ca.account.sony.com/api/v1/ssocookie"

# Where we read the linked user's PSN profile (online id + account id).
# The legacy community endpoint reliably returns both when we ask for the
# fields explicitly (the newer m.np.playstation.com one 400s for "me").
PROFILE_URL = (
    "https://us-prof.np.community.playstation.net/userProfile/v1/users/me/profile2"
)
PROFILE_FIELDS = "onlineId,accountId"

_PROFILE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Country": "US",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 11; sdk_gphone_x86 Build/RSR1.201013.001; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.106 "
        "Mobile Safari/537.36"
    ),
}


class LinkError(Exception):
    """Raised when an NPSSO can't be turned into a linked account."""


def _normalize_npsso(raw: str) -> str:
    """Extract the NPSSO from whatever the user pasted.

    Friends paste all sorts of shapes from the token page: the bare token, the
    full ``{"npsso":"...","expires_in":...}`` blob, or a partial fragment like
    ``"npsso":"...","expires_in":...}`` (no leading brace). Handle them all by
    pulling the value out of any ``"npsso":"..."`` pattern first, then falling
    back to treating the input as the bare token.
    """
    raw = (raw or "").strip()
    if not raw:
        raise LinkError("No token provided.")

    # 1) Any paste that mentions npsso -> grab the quoted value after it. This
    #    covers full JSON, brace-less fragments, and stray trailing fields.
    m = re.search(r'"?npsso"?\s*:\s*"([^"]+)"', raw)
    if m:
        raw = m.group(1).strip()
    # 2) Clean JSON object without the regex hit (rare) -> parse it.
    elif raw.startswith("{"):
        try:
            raw = str(json.loads(raw).get("npsso", "")).strip()
        except Exception as exc:  # noqa: BLE001
            raise LinkError("That doesn't look like a valid token.") from exc
    # 3) Otherwise assume the input IS the bare token (strip stray quotes).
    else:
        raw = raw.strip('"').strip()

    if not raw or len(raw) < 40:
        raise LinkError("That doesn't look like a valid NPSSO token.")
    return raw


def _exchange_npsso(npsso: str) -> dict:
    """NPSSO cookie -> access/refresh tokens. Mirrors PSNAuth._full_auth."""
    params = {
        "access_type": "offline",
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
    }
    # Step 1: NPSSO cookie -> authorization code (302 with ?code= in Location).
    with httpx.Client(follow_redirects=False, timeout=15) as client:
        resp = client.get(AUTH_URL, params=params, cookies={"npsso": npsso})

    if resp.status_code != 302:
        logger.warning(
            "portal: auth-code step returned %d (expected 302); body=%s",
            resp.status_code,
            resp.text[:300],
        )
        raise LinkError(
            "PlayStation rejected that token (it may be expired -- grab a fresh one)."
        )
    location = resp.headers.get("location", "")
    if "code=" not in location:
        logger.warning("portal: 302 had no code in Location=%s", location[:200])
        raise LinkError("PlayStation didn't return a login code. Try a fresh token.")
    code = location.split("code=")[1].split("&")[0]

    # Step 2: authorization code -> access + refresh tokens.
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "token_format": "jwt",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": AUTH_HEADER,
            },
        )
    if resp.status_code != 200:
        logger.warning(
            "portal: token-exchange step failed %d; body=%s",
            resp.status_code,
            resp.text[:300],
        )
        raise LinkError("Couldn't complete PlayStation login. Try a fresh token.")
    return resp.json()


def _fetch_profile(access_token: str) -> dict:
    """Auto-detect the linked account's online id + account id."""
    headers = dict(_PROFILE_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                PROFILE_URL, params={"fields": PROFILE_FIELDS}, headers=headers
            )
        if resp.status_code == 200:
            data = resp.json()
            prof = data.get("profile", data)
            return {
                "online_id": prof.get("onlineId"),
                "account_id": prof.get("accountId") or data.get("accountId"),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: profile lookup failed: %s", exc)
    return {"online_id": None, "account_id": None}


def _safe_key(value: str) -> str:
    """Filesystem-safe file stem from a username/id."""
    return "".join(c for c in value if c.isalnum() or c in "-_.").lower() or "user"


def link_user(raw_npsso: str, mm_username: str = "", zitadel_user_id: str = "") -> dict:
    """Validate an NPSSO, mint tokens, detect the account, persist per-user.

    ``mm_username`` ties the PSN link to a known Mattermost person. When given,
    the record is keyed by that username (stable, human-readable) so re-linking
    the same person overwrites rather than duplicating.

    ``zitadel_user_id`` is stored when the link comes from an authenticated
    dashboard session, so the record can be retrieved by that user later.

    Returns a small summary dict for the UI. Raises LinkError on any failure
    with a friendly message.
    """
    npsso = _normalize_npsso(raw_npsso)
    token_data = _exchange_npsso(npsso)

    access_token = token_data["access_token"]
    profile = _fetch_profile(access_token)
    account_id = profile.get("account_id")
    online_id = profile.get("online_id")

    # Key preference: Zitadel user ID > chosen Mattermost user > PSN account id > timestamp.
    # Zitadel ID is most stable — guarantees one record per dashboard account.
    if zitadel_user_id:
        key = f"z-{_safe_key(zitadel_user_id)}"
    elif mm_username:
        key = _safe_key(mm_username)
    elif account_id:
        key = str(account_id)
    else:
        key = f"unknown-{int(time.time())}"

    expires_in = token_data.get("expires_in", 3600)
    refresh_expires_in = token_data.get("refresh_token_expires_in", 7776000)
    now = time.time()

    record = {
        "zitadel_user_id": zitadel_user_id or None,
        "mm_username": mm_username or None,
        "online_id": online_id,
        "account_id": account_id,
        "npsso": npsso,
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": now + expires_in,
        "refresh_expires_at": now + refresh_expires_in,
        "linked_at": now,
    }

    USERS_DIR.mkdir(parents=True, exist_ok=True)
    (USERS_DIR / f"{key}.json").write_text(json.dumps(record, indent=2))
    logger.info(
        "portal: linked zitadel=%s mm=%s online_id=%s account_id=%s",
        zitadel_user_id or "-",
        mm_username or "-",
        online_id,
        account_id,
    )

    return {"online_id": online_id, "account_id": account_id, "mm_username": mm_username,
            "zitadel_user_id": zitadel_user_id}


def list_users() -> list[dict]:
    """Summaries of every linked user (no secrets), for an admin view."""
    out: list[dict] = []
    if not USERS_DIR.exists():
        return out
    for f in sorted(USERS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        out.append(
            {
                "zitadel_user_id": d.get("zitadel_user_id"),
                "mm_username": d.get("mm_username"),
                "online_id": d.get("online_id"),
                "account_id": d.get("account_id"),
                "linked_at": d.get("linked_at"),
                "refresh_expires_at": d.get("refresh_expires_at"),
            }
        )
    return out


def list_unclaimed() -> list[dict]:
    """Return PSN records that have no zitadel_user_id — available to be claimed."""
    out: list[dict] = []
    if not USERS_DIR.exists():
        return out
    for f in sorted(USERS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        if d.get("zitadel_user_id"):
            continue
        out.append({
            "key": f.stem,
            "mm_username": d.get("mm_username"),
            "online_id": d.get("online_id"),
            "linked_at": d.get("linked_at"),
        })
    return out


def claim_record(key: str, zitadel_user_id: str) -> bool:
    """Assign zitadel_user_id to the record identified by key (filename stem).

    Returns True on success, False if not found or already claimed.
    """
    if not USERS_DIR.exists():
        return False
    f = USERS_DIR / f"{key}.json"
    if not f.exists():
        return False
    try:
        d = json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return False
    if d.get("zitadel_user_id"):
        return False  # already claimed
    d["zitadel_user_id"] = zitadel_user_id
    f.write_text(json.dumps(d, indent=2))
    logger.info("portal: claimed key=%s by zitadel_user_id=%s online_id=%s",
                key, zitadel_user_id, d.get("online_id"))
    return True


def find_by_zitadel_id(zitadel_user_id: str) -> dict | None:
    """Return the PSN record for a given Zitadel user ID, or None."""
    if not zitadel_user_id or not USERS_DIR.exists():
        return None
    for f in USERS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        if d.get("zitadel_user_id") == zitadel_user_id:
            return {
                "online_id": d.get("online_id"),
                "account_id": d.get("account_id"),
                "linked_at": d.get("linked_at"),
                "npsso_ok": bool(d.get("npsso")),
                "token_ok": bool(d.get("access_token")),
                "refresh_expires_at": d.get("refresh_expires_at"),
            }
    return None


def mattermost_usernames() -> list[str]:
    """Fetch active team members' usernames for the portal dropdown.

    Uses MATTERMOST_URL/TOKEN/TEAM_NAME from the env. Returns [] if not
    configured or on any error (the portal still works with a free-text field).
    """
    import os

    base = (os.environ.get("MATTERMOST_URL") or "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN") or ""
    team = os.environ.get("MATTERMOST_TEAM_NAME") or ""
    if not (base and token and team):
        return []
    # Bots we don't want in the human picker.
    skip = {"slapper", "slaptastic"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=15) as client:
            tr = client.get(f"{base}/api/v4/teams/name/{team}", headers=headers)
            if tr.status_code != 200:
                return []
            tid = tr.json().get("id")
            ur = client.get(
                f"{base}/api/v4/users",
                params={"in_team": tid, "per_page": "200", "active": "true"},
                headers=headers,
            )
            if ur.status_code != 200:
                return []
            names = [u.get("username") for u in ur.json() if u.get("username")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: mattermost user list failed: %s", exc)
        return []
    return sorted(n for n in names if n and n.lower() not in skip)

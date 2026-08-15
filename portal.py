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
PROFILE_URL = (
    "https://m.np.playstation.com/api/userProfile/v1/internal/users/me/profiles"
)

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
    """Accept either the bare token or the full {"npsso":"..."} JSON blob."""
    raw = (raw or "").strip()
    if not raw:
        raise LinkError("No token provided.")
    # Friends often paste the whole JSON from the token page -- handle both.
    if raw.startswith("{"):
        try:
            raw = json.loads(raw).get("npsso", "").strip()
        except Exception as exc:  # noqa: BLE001
            raise LinkError("That doesn't look like a valid token.") from exc
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
    with httpx.Client(follow_redirects=False, timeout=15) as client:
        resp = client.get(AUTH_URL, params=params, cookies={"npsso": npsso})

    if resp.status_code != 302:
        raise LinkError(
            "PlayStation rejected that token (it may be expired -- grab a fresh one)."
        )
    location = resp.headers.get("location", "")
    if "code=" not in location:
        raise LinkError("PlayStation didn't return a login code. Try a fresh token.")
    code = location.split("code=")[1].split("&")[0]

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
        raise LinkError("Couldn't complete PlayStation login. Try again.")
    return resp.json()


def _fetch_profile(access_token: str) -> dict:
    """Auto-detect the linked account's online id + account id."""
    headers = dict(_PROFILE_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(PROFILE_URL, headers=headers)
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


def link_user(raw_npsso: str) -> dict:
    """Validate an NPSSO, mint tokens, detect the account, persist per-user.

    Returns a small summary dict for the UI. Raises LinkError on any failure
    with a friendly message.
    """
    npsso = _normalize_npsso(raw_npsso)
    token_data = _exchange_npsso(npsso)

    access_token = token_data["access_token"]
    profile = _fetch_profile(access_token)
    account_id = profile.get("account_id")
    online_id = profile.get("online_id")

    # Fall back to a stable key if the profile lookup was blocked, so we never
    # lose a successfully-minted token.
    key = str(account_id) if account_id else f"unknown-{int(time.time())}"

    expires_in = token_data.get("expires_in", 3600)
    refresh_expires_in = token_data.get("refresh_token_expires_in", 7776000)
    now = time.time()

    record = {
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
    logger.info("portal: linked user online_id=%s account_id=%s", online_id, account_id)

    return {"online_id": online_id, "account_id": account_id}


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
                "online_id": d.get("online_id"),
                "account_id": d.get("account_id"),
                "linked_at": d.get("linked_at"),
                "refresh_expires_at": d.get("refresh_expires_at"),
            }
        )
    return out

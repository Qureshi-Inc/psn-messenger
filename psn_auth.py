"""PSN authentication with automatic token refresh.

Uses the NPSSO token only for initial login. Persists access + refresh
tokens to a JSON file. Automatically refreshes when access token expires.
Only requires a new NPSSO if the refresh token itself has expired.
"""

import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("/data/psn_tokens.json")

# PSN OAuth endpoints
AUTH_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"

# Client credentials for PlayStation app
CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
CLIENT_SECRET = "ucPjka5tntB2KqsP"
AUTH_HEADER = "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
SCOPE = "psn:mobile.v2.core psn:clientapp"
REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"

# Token refresh buffer (refresh 5 min before expiry)
REFRESH_BUFFER_SECONDS = 300


class PSNAuth:
    """Manages PSN OAuth tokens with automatic refresh."""

    def __init__(self, npsso: str):
        self._npsso = npsso
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_expires_at: float = 0.0
        self._load_tokens()

    @property
    def access_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expires_at - REFRESH_BUFFER_SECONDS:
            return self._access_token

        # Try refresh token first
        if self._refresh_token and time.time() < self._refresh_expires_at:
            logger.info("Access token expired, refreshing...")
            if self._refresh():
                return self._access_token  # type: ignore

        # Refresh failed or no refresh token — do full auth with NPSSO
        logger.info("Performing full authentication with NPSSO...")
        self._full_auth()
        return self._access_token  # type: ignore

    def _full_auth(self) -> None:
        """Exchange NPSSO for access + refresh tokens."""
        # Step 1: Exchange NPSSO for authorization code
        code = self._get_auth_code()
        if not code:
            raise RuntimeError("Failed to get authorization code from NPSSO. Token may be expired.")

        # Step 2: Exchange authorization code for tokens
        self._exchange_code(code)

    def _get_auth_code(self) -> str | None:
        """Exchange NPSSO cookie for an authorization code."""
        params = {
            "access_type": "offline",
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
        }

        with httpx.Client(follow_redirects=False, timeout=15) as client:
            resp = client.get(
                AUTH_URL,
                params=params,
                cookies={"npsso": self._npsso},
            )

        # The response should be a 302 redirect with code in the Location header
        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            if "code=" in location:
                code = location.split("code=")[1].split("&")[0]
                logger.info("Got authorization code")
                return code

        logger.error("Failed to get auth code (status=%d)", resp.status_code)
        return None

    def _exchange_code(self, code: str) -> None:
        """Exchange authorization code for access + refresh tokens."""
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
            logger.error("Token exchange failed: %d %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"Token exchange failed: {resp.status_code}")

        data = resp.json()
        self._store_tokens(data)

    def _refresh(self) -> bool:
        """Use refresh token to get new access + refresh tokens."""
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "scope": SCOPE,
                        "token_format": "jwt",
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": AUTH_HEADER,
                    },
                )

            if resp.status_code != 200:
                logger.warning("Token refresh failed: %d", resp.status_code)
                return False

            data = resp.json()
            self._store_tokens(data)
            logger.info("Token refreshed successfully")
            return True
        except Exception as e:
            logger.error("Token refresh error: %s", e)
            return False

    def _store_tokens(self, data: dict) -> None:
        """Save tokens to memory and file."""
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        expires_in = data.get("expires_in", 3600)
        refresh_expires_in = data.get("refresh_token_expires_in", 7776000)  # ~90 days default

        self._expires_at = time.time() + expires_in
        self._refresh_expires_at = time.time() + refresh_expires_in

        # Persist to file
        token_data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "refresh_expires_at": self._refresh_expires_at,
        }

        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps(token_data))
            logger.info("Tokens saved to %s", TOKEN_FILE)
        except Exception as e:
            logger.warning("Failed to save tokens: %s", e)

    def _load_tokens(self) -> None:
        """Load persisted tokens from file."""
        if not TOKEN_FILE.exists():
            logger.info("No saved tokens found, will authenticate fresh")
            return

        try:
            data = json.loads(TOKEN_FILE.read_text())
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._expires_at = data.get("expires_at", 0)
            self._refresh_expires_at = data.get("refresh_expires_at", 0)

            if self._refresh_token and time.time() < self._refresh_expires_at:
                logger.info("Loaded saved tokens (refresh valid)")
            else:
                logger.info("Saved refresh token expired, will re-authenticate")
                self._access_token = None
                self._refresh_token = None
        except Exception as e:
            logger.warning("Failed to load saved tokens: %s", e)

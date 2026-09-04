import asyncio
import os
import json
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from pydantic import BaseModel
from psnawp_api import PSNAWP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Suppress per-request access logs for health-check endpoints — Coolify/Traefik
# polls these every few seconds and the noise drowns out real log lines.
class _NoHealthFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/v2/health" not in msg

for _uvicorn_logger in ("uvicorn.access", "uvicorn"):
    logging.getLogger(_uvicorn_logger).addFilter(_NoHealthFilter())

NPSSO_TOKEN = os.environ.get("NPSSO_TOKEN")
GROUP_ID = os.environ.get("GROUP_ID")
GROUP_NAME = os.environ.get("GROUP_NAME", "crcmz-mod")
# Auto-Squad: a separate PSN group (everyone except wolfie/IG_Juicy) that the
# "Squad Up" Stream Deck button rallies. Created 2026-08-15; overridable via env.
SQUAD_GROUP_ID = os.environ.get("SQUAD_GROUP_ID", "213250d833ccce334b651e2ee15e365c97468e02-869")
SQUAD_GROUP_NAME = os.environ.get("SQUAD_GROUP_NAME", "The Squad")

if not NPSSO_TOKEN:
    raise RuntimeError("NPSSO_TOKEN environment variable is required")
if not GROUP_ID:
    raise RuntimeError("GROUP_ID environment variable is required")

psnawp = PSNAWP(NPSSO_TOKEN)
client = psnawp.me()
logger.info(f"Authenticated as: {client.online_id}")

group = psnawp.group(group_id=GROUP_ID)
logger.info(f"Connected to group: {GROUP_ID}")

app = FastAPI(title="PSN Messenger")


# --- Rate limiting -----------------------------------------------------------
# Protects friends' PSN accounts from any brute-force/spam pattern. Every action
# that results in a PSN write (group messages, roasts) passes through a shared
# token-bucket-ish window limiter keyed by action group. Well under anything
# Sony would flag, and it stops a mashed button or a script from flooding.
import threading as _threading
import time as _time

_rl_lock = _threading.Lock()
_rl_hits: dict[str, list[float]] = {}

# key -> (max_calls, per_seconds)
_RL_LIMITS = {
    "psn_send": (8, 60.0),      # group messages (soundboard, squad, /v2/send)
    "roast": (5, 60.0),         # roast triggers
    "custom_add": (6, 60.0),    # AI-flavored custom button creation (also Bedrock cost)
}


def _rate_limit(key: str, actor: str = "") -> None:
    """Raise HTTP 429 if `key` exceeded its window. Sliding-window counter.

    Pass `actor` (user_id or client IP) to scope the limit per-user so one
    person cannot exhaust the quota for everyone else.
    """
    limit = _RL_LIMITS.get(key)
    if not limit:
        return
    max_calls, window = limit
    now = _time.time()
    bucket = f"{key}:{actor}" if actor else key
    with _rl_lock:
        hits = [t for t in _rl_hits.get(bucket, []) if now - t < window]
        if len(hits) >= max_calls:
            retry = round(window - (now - hits[0]), 1)
            raise HTTPException(
                status_code=429,
                detail=f"Slow down — try again in {retry}s.",
                headers={"Retry-After": str(int(retry) + 1)},
            )
        hits.append(now)
        _rl_hits[bucket] = hits


import re as _re


class _DirectAuth:
    """Minimal duck-type of PSNAuth that holds a pre-fetched access token.
    PSNMessenger only reads auth.access_token, so this is all we need.
    """
    def __init__(self, token: str):
        self._token = token

    @property
    def access_token(self) -> str:
        return self._token


class MessageRequest(BaseModel):
    message: str


@app.get("/.well-known/webauthn")
def webauthn_related_origins():
    """Declare auth.crcmz.me as a related origin so passkeys registered there work here."""
    issuer_origin = ZITADEL_ISSUER.rstrip("/")
    return JSONResponse({"origins": [issuer_origin]})


@app.get("/health")
def health():
    return {"status": "ok"}


_FAVICON_PATH = Path(__file__).parent / "favicon.png"

@app.get("/favicon.png", include_in_schema=False)
def favicon():
    if _FAVICON_PATH.exists():
        return Response(_FAVICON_PATH.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)


@app.post("/send")
def send_message(req: MessageRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    try:
        group.send_message(req.message.strip())
        logger.info(f"Message sent: {req.message[:50]}")
        return {"status": "sent", "message": req.message.strip()}
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/messages")
def get_messages(limit: int = 5):
    try:
        conversation = group.get_conversation(limit)
        messages = []
        for msg in conversation:
            messages.append({
                "sender": str(msg.get("senderOnlineId", "unknown")),
                "body": str(msg.get("body", "")),
                "timestamp": str(msg.get("eventIndex", "")),
            })
        return {"messages": messages}
    except Exception as e:
        logger.error(f"Failed to get messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === V2 routes: PSN API with auto-refresh tokens ===

try:
    from psn_auth import PSNAuth
    from psn_messaging import PSNMessenger

    psn_auth = PSNAuth(NPSSO_TOKEN)
    psn_messenger = PSNMessenger(psn_auth, GROUP_ID, GROUP_NAME)
    logger.info("v2: PSN auth initialized with token persistence")
    _v2_available = True
except Exception as e:
    logger.warning(f"v2: PSN auth failed to initialize: {e}")
    _v2_available = False


@app.get("/v2/health")
def v2_health():
    if not _v2_available:
        raise HTTPException(status_code=503, detail="v2 auth not initialized")
    return {"status": "ok", "version": "v2"}


@app.post("/v2/send")
def v2_send_message(req: MessageRequest, request: Request):
    _rate_limit("psn_send", request.client.host)
    if not _v2_available:
        raise HTTPException(status_code=503, detail="v2 auth not initialized")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    try:
        # Try to send as the logged-in user's PSN account; always fall back to
        # the server (crcmz-mod) account if anything goes wrong.
        success = False
        try:
            session = _get_session(request)
            if session:
                user_token = portal_mod.get_fresh_access_token(session.get("sub", ""))
                if user_token:
                    user_messenger = PSNMessenger(_DirectAuth(user_token), SQUAD_GROUP_ID)
                    success = user_messenger.send_message(req.message.strip())
        except Exception as ue:
            logger.warning("v2: user-token send failed (%s), falling back to server account", ue)
        if not success:
            success = _squad_messenger.send_message(req.message.strip())
        if success:
            return {"status": "sent", "message": req.message.strip(), "version": "v2"}
        raise HTTPException(status_code=500, detail="Failed to send message")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"v2: Send failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v2/messages")
def v2_get_messages(limit: int = 5):
    if not _v2_available:
        raise HTTPException(status_code=503, detail="v2 auth not initialized")
    try:
        messages = psn_messenger.get_messages(limit)
        return {"messages": messages, "version": "v2"}
    except Exception as e:
        logger.error(f"v2: Get messages failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v2/messages/raw")
def v2_get_messages_raw(limit: int = 10):
    """Debug: return the raw PSN API response to inspect field names."""
    if not _v2_available:
        raise HTTPException(status_code=503, detail="v2 auth not initialized")
    try:
        return psn_messenger.get_messages_raw(limit)
    except Exception as e:
        logger.error(f"v2: get_messages_raw failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === Auto-Squad: rally the squad group (everyone except wolfie/IG_Juicy) ===

# A separate messenger bound to the squad group, reusing the same auth.
try:
    _squad_messenger = PSNMessenger(psn_auth, SQUAD_GROUP_ID, SQUAD_GROUP_NAME) if _v2_available else None
except Exception as e:  # noqa: BLE001
    logger.warning(f"squad: messenger init failed: {e}")
    _squad_messenger = None


class SquadRequest(BaseModel):
    message: str | None = None


@app.post("/v2/squad")
def v2_squad(request: Request, req: SquadRequest | None = None):
    """Post a 'squad up' rally message to the dedicated squad group."""
    _rate_limit("psn_send", request.client.host)
    if _squad_messenger is None:
        raise HTTPException(status_code=503, detail="squad messenger not initialized")
    text = (req.message.strip() if (req and req.message) else "") or \
        "🎮🔥 SQUAD UP! Who's hopping on? 🕹️💥"
    try:
        if _squad_messenger.send_message(text):
            logger.info(f"squad: sent -> {text[:60]}")
            return {"status": "sent", "group": SQUAD_GROUP_ID, "message": text}
        raise HTTPException(status_code=500, detail="Failed to send squad message")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"squad: send failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === Roast Bot routes ===

import roast_bot


@app.post("/roast/start")
async def roast_start():
    if roast_bot.is_running():
        return {"status": "already running"}
    if not roast_bot.start():
        return {"status": "disabled", "message": "Auto-roast is turned off."}
    return {"status": "started", "message": "Roast bot activated 🔥"}


@app.post("/roast/stop")
async def roast_stop():
    if not roast_bot.is_running():
        return {"status": "already stopped"}
    roast_bot.stop()
    return {"status": "stopped", "message": "Roast bot deactivated"}


@app.post("/roast/once")
async def roast_once(request: Request):
    """Send one roast immediately."""
    _rate_limit("roast", request.client.host)
    try:
        roast = roast_bot.generate_single_roast()
        roast_bot.send_roast(roast)
        return {"status": "sent", "roast": roast}
    except Exception as e:
        logger.error(f"Roast once failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/roast/status")
def roast_status():
    return {"running": roast_bot.is_running()}


# === Self-service PSN linking portal ===
#
# Flow for a friend (one time, then never again):
#   1. Open /portal, enter the shared passcode.
#   2. Tap "Sign in to PlayStation" -> Sony login page (new tab).
#   3. Tap "Get my token" -> Sony's ssocookie page shows {"npsso":"..."}.
#   4. Copy it, paste back here, Link. We validate + save their tokens per
#      user and auto-refresh forever after (see portal.py / psn_auth.py).

import portal as portal_mod



def _portal_page(error: str = "", ok: str = "") -> str:
    """Render the single-page portal wizard (only reached once unlocked)."""
    from portal import NPSSO_TOKEN_URL, PSN_LOGIN_URL, mattermost_usernames

    # On success we replace the whole wizard with a celebration screen.
    if ok:
        return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.png">
<title>Linked!</title>
<style>
  :root {{ color-scheme:dark; }}
  * {{ box-sizing:border-box; }}
  html,body {{ margin:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:#eaf0ff; min-height:100dvh; display:flex; align-items:center;
    justify-content:center; padding:24px; background:#070b18; position:relative;
    overflow:hidden; }}
  body::before {{ content:""; position:fixed; inset:-30%; z-index:-1;
    background:
      radial-gradient(45% 45% at 30% 25%, rgba(46,230,160,.35), transparent 60%),
      radial-gradient(45% 45% at 75% 30%, rgba(0,163,255,.32), transparent 60%),
      radial-gradient(50% 45% at 55% 90%, rgba(124,92,255,.28), transparent 62%);
    filter:blur(30px); animation:drift 16s ease-in-out infinite alternate; }}
  @keyframes drift {{ to {{ transform:translate3d(4%,3%,0) scale(1.12); }} }}
  .card {{ width:100%; max-width:440px; text-align:center;
    background:rgba(23,31,54,.72); border:1px solid rgba(120,140,190,.2);
    border-radius:24px; padding:38px 28px 30px;
    box-shadow:0 30px 80px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.06);
    backdrop-filter:blur(22px); -webkit-backdrop-filter:blur(22px);
    animation:rise .55s cubic-bezier(.2,.8,.2,1) both; }}
  @keyframes rise {{ from {{ opacity:0; transform:translateY(18px) scale(.97); }} }}
  .ring {{ width:96px; height:96px; margin:0 auto 20px; border-radius:50%;
    display:grid; place-items:center; font-size:46px;
    background:radial-gradient(circle at 50% 40%,rgba(46,230,160,.4),rgba(0,179,255,.12));
    box-shadow:0 0 0 4px rgba(46,230,160,.4), 0 16px 50px rgba(46,230,160,.4);
    animation:pop .6s cubic-bezier(.2,1.5,.4,1) both; }}
  @keyframes pop {{ from {{ transform:scale(.3); opacity:0; }} }}
  h1 {{ font-size:24px; margin:0 0 8px; }}
  p {{ color:#9fb0d4; font-size:14.5px; line-height:1.6; margin:0 0 8px; }}
  .who {{ color:#9dffd6; font-weight:700; }}
  .btns {{ margin-top:24px; }}
  a.btn {{ display:block; text-decoration:none; padding:14px; border-radius:14px;
    font-size:15px; font-weight:700; margin-top:11px; }}
  .go {{ background:linear-gradient(135deg,#12d18e,#00b3ff); color:#04210f;
    box-shadow:0 12px 30px rgba(18,209,142,.4); }}
  .ghost {{ background:rgba(255,255,255,.06); color:#dfeaff;
    border:1px solid rgba(140,160,255,.24); }}
</style></head>
<body><div class="card">
  <div class="ring">✓</div>
  <h1>You're all set! 🎉</h1>
  <p><span class="who">{ok}</span></p>
  <p>Your PlayStation is linked. You never have to do this again — it stays
     connected automatically.</p>
  <div class="btns">
    <a class="btn go" href="/dashboard">🎮 See the Squad dashboard</a>
    <a class="btn ghost" href="/portal">Link another account</a>
  </div>
</div></body></html>"""

    banner = ""
    if error:
        banner = f'<div class="msg err">⚠️ {error}</div>'
    # "Who are you?" dropdown of Mattermost users, so each link ties to a person
    # (like the Apple Music re-link page). Falls back to a text field if the
    # user list can't be fetched.
    names = mattermost_usernames()
    if names:
        opts = '<option value="" disabled selected>Select your name…</option>' + "".join(
            f'<option value="{n}">{n}</option>' for n in names
        )
        who_input = f'<select name="mm_username" id="mm" required>{opts}</select>'
    else:
        who_input = (
            '<input name="mm_username" id="mm" '
            'placeholder="your mattermost username" required>'
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.png">
<title>Link your PlayStation</title>
<style>
  :root {{
    color-scheme: dark;
    --bg:#070b18; --card:rgba(23,31,54,.72); --line:rgba(120,140,190,.18);
    --txt:#eaf0ff; --dim:#9fb0d4; --psn:#0070d1; --psn2:#00a3ff;
    --ok:#2ee6a0; --err:#ff6b8b; --accent:#7c5cff;
  }}
  * {{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }}
  html,body {{ margin:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:var(--txt); min-height:100dvh; padding:24px 16px 48px;
    background:var(--bg); overflow-x:hidden; position:relative;
    display:flex; align-items:center; justify-content:center; }}
  /* animated aurora backdrop */
  body::before, body::after {{ content:""; position:fixed; inset:-30% -10%; z-index:-2;
    background:
      radial-gradient(45% 45% at 20% 18%, rgba(0,112,209,.42), transparent 60%),
      radial-gradient(40% 40% at 82% 22%, rgba(124,92,255,.38), transparent 60%),
      radial-gradient(50% 45% at 55% 92%, rgba(0,163,255,.30), transparent 62%);
    filter:blur(28px); animation:drift 18s ease-in-out infinite alternate; }}
  body::after {{ animation-duration:24s; animation-direction:alternate-reverse; opacity:.7; }}
  @keyframes drift {{ from {{ transform:translate3d(-3%,-2%,0) scale(1); }}
    to {{ transform:translate3d(4%,3%,0) scale(1.12); }} }}
  /* subtle star grain */
  .grain {{ position:fixed; inset:0; z-index:-1; opacity:.05; pointer-events:none;
    background-image:radial-gradient(#fff 1px, transparent 1px);
    background-size:26px 26px; }}

  .card {{ width:100%; max-width:460px; background:var(--card);
    border:1px solid var(--line); border-radius:24px; padding:26px 24px 24px;
    box-shadow:0 30px 80px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.06);
    backdrop-filter:blur(22px) saturate(140%);
    -webkit-backdrop-filter:blur(22px) saturate(140%);
    animation:rise .6s cubic-bezier(.2,.8,.2,1) both; }}
  @keyframes rise {{ from {{ opacity:0; transform:translateY(18px) scale(.98); }} }}

  .brand {{ display:flex; align-items:center; gap:12px; margin-bottom:4px; }}
  .logo {{ width:46px; height:46px; border-radius:14px; flex:none;
    display:grid; place-items:center; font-size:24px;
    background:linear-gradient(135deg,var(--psn),var(--accent));
    box-shadow:0 8px 24px rgba(0,112,209,.5); }}
  h1 {{ font-size:21px; margin:0; letter-spacing:.2px; }}
  .tag {{ color:var(--dim); font-size:13px; margin:2px 0 0; }}
  .lead {{ color:var(--dim); font-size:13.5px; line-height:1.55; margin:16px 0 20px; }}

  /* progress rail */
  .rail {{ display:flex; align-items:center; gap:6px; margin:0 0 22px; }}
  .pip {{ flex:1; height:5px; border-radius:99px; background:rgba(255,255,255,.09);
    overflow:hidden; }}
  .pip > i {{ display:block; height:100%; width:0;
    background:linear-gradient(90deg,var(--psn2),var(--accent));
    transition:width .4s ease; }}
  .pip.done > i {{ width:100%; }}

  .step {{ border:1px solid var(--line); border-radius:18px; padding:16px;
    margin-bottom:14px; background:rgba(255,255,255,.025);
    transition:opacity .35s, filter .35s, transform .35s; }}
  .step.locked {{ opacity:.4; filter:grayscale(.5); pointer-events:none; }}
  .step.locked .step-head h2::after {{ content:" 🔒"; font-size:12px; }}
  .step-head {{ display:flex; gap:13px; align-items:flex-start; }}
  .badge {{ width:34px; height:34px; border-radius:11px; flex:none; display:grid;
    place-items:center; font-size:16px; font-weight:800;
    background:linear-gradient(135deg,rgba(0,112,209,.9),rgba(124,92,255,.9));
    color:#fff; box-shadow:0 4px 14px rgba(0,112,209,.4); }}
  .step-head h2 {{ font-size:15.5px; margin:2px 0 3px; }}
  .step-head p {{ font-size:12.5px; color:var(--dim); margin:0; line-height:1.5; }}

  a.btn, button.btn {{ display:flex; align-items:center; justify-content:center; gap:8px;
    width:100%; text-align:center; text-decoration:none; padding:14px;
    border-radius:14px; font-size:15px; font-weight:700; border:none;
    cursor:pointer; margin-top:13px; transition:transform .07s, filter .15s, box-shadow .15s; }}
  a.btn:active, button.btn:active {{ transform:scale(.975); }}
  .btn.psn {{ color:#fff; background:linear-gradient(135deg,var(--psn),var(--psn2));
    box-shadow:0 10px 26px rgba(0,112,209,.45); }}
  .btn.psn:hover {{ box-shadow:0 12px 32px rgba(0,112,209,.6); }}
  .btn.token {{ color:#dfeaff; background:rgba(255,255,255,.06);
    border:1px solid rgba(140,160,255,.28); }}
  .btn.token:hover {{ background:rgba(255,255,255,.11); }}
  .btn.go {{ background:linear-gradient(135deg,var(--accent),var(--psn));
    color:#fff; width:auto; padding:13px 20px; margin:0; white-space:nowrap; }}
  .btn.link {{ background:linear-gradient(135deg,#12d18e,#00b3ff); color:#04210f;
    font-size:16px; box-shadow:0 12px 30px rgba(18,209,142,.4); }}
  .btn.link:hover {{ box-shadow:0 14px 36px rgba(18,209,142,.55); }}
  .btn.paste {{ background:rgba(255,255,255,.06);
    border:1px solid rgba(140,160,255,.24); color:#dfeaff; }}
  .btn.ghost {{ background:transparent; border:1px solid var(--line); color:var(--dim);
    font-weight:600; }}

  label {{ display:block; font-size:12px; color:var(--dim); margin:15px 0 7px;
    font-weight:600; letter-spacing:.3px; }}
  input, textarea, select {{ width:100%; padding:13px 14px; border-radius:13px;
    border:1px solid rgba(140,160,255,.22); background:rgba(6,11,24,.6);
    color:var(--txt); font-size:15px; transition:border .15s, box-shadow .15s;
    -webkit-appearance:none; appearance:none; }}
  select {{ background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' fill='none' stroke='%239fb0d4' stroke-width='2'><path d='M2 4l5 5 5-5'/></svg>");
    background-repeat:no-repeat; background-position:right 14px center; padding-right:38px; }}
  input:focus, textarea:focus, select:focus {{ outline:none;
    border-color:var(--psn2); box-shadow:0 0 0 3px rgba(0,163,255,.22); }}
  textarea {{ min-height:78px; resize:vertical; font-family:ui-monospace,SFMono-Regular,monospace;
    font-size:13px; line-height:1.5; }}
  .pass-row {{ display:flex; gap:10px; align-items:stretch; margin-top:14px; }}
  .pass-row input {{ flex:1; font-size:18px; letter-spacing:3px; text-align:center; }}

  .msg {{ padding:13px 15px; border-radius:14px; font-size:13.5px; margin-bottom:18px;
    display:flex; gap:10px; align-items:center; line-height:1.45;
    animation:rise .4s ease both; }}
  .msg.ok {{ background:rgba(46,230,160,.12); border:1px solid rgba(46,230,160,.4);
    color:#9dffd6; }}
  .msg.err {{ background:rgba(255,107,139,.12); border:1px solid rgba(255,107,139,.4);
    color:#ffc0cd; }}
  .err-inline {{ color:var(--err); font-size:12.5px; margin-top:8px; min-height:0; }}
  .hint {{ font-size:11.5px; color:#7d8ab0; margin-top:7px; line-height:1.5; }}
  code {{ background:rgba(255,255,255,.08); padding:1px 6px; border-radius:6px;
    font-size:12px; color:#cfe0ff; }}
  .foot {{ text-align:center; margin-top:18px; }}
  .foot a {{ color:#7fb2ff; font-size:12.5px; text-decoration:none; }}

  /* success celebration */
  .done-hero {{ text-align:center; padding:10px 0 4px; }}
  .done-hero .ring {{ width:82px; height:82px; margin:0 auto 14px; border-radius:50%;
    display:grid; place-items:center; font-size:38px;
    background:radial-gradient(circle at 50% 40%,rgba(46,230,160,.35),rgba(0,179,255,.12));
    box-shadow:0 0 0 3px rgba(46,230,160,.4), 0 12px 40px rgba(46,230,160,.35);
    animation:pop .5s cubic-bezier(.2,1.4,.4,1) both; }}
  @keyframes pop {{ from {{ transform:scale(.4); opacity:0; }} }}
</style></head>
<body>
<div class="grain"></div>
<div class="card">
  <div class="brand">
    <div class="logo">🎮</div>
    <div><h1>Link your PlayStation</h1>
      <p class="tag">One quick setup — then never again.</p></div>
  </div>

  {banner}

  <div id="flow">
    <p class="lead">Connect your PSN account so the squad can see when you're
      online and rally you into games. Takes about 30 seconds.</p>

    <div class="rail" id="rail">
      <div class="pip" id="p0"><i></i></div>
      <div class="pip" id="p1"><i></i></div>
      <div class="pip" id="p2"><i></i></div>
      <div class="pip" id="p3"><i></i></div>
    </div>

    <form method="post" action="/portal/link" id="f">
      <section class="step" id="s1">
        <div class="step-head">
          <span class="badge">1</span>
          <div><h2>Sign in to PlayStation</h2>
            <p>Log into your PSN account in the new tab, then come back here.</p></div>
        </div>
        <a class="btn psn" href="{PSN_LOGIN_URL}" target="_blank" rel="noopener"
           onclick="mark(1)">🎮 Open PlayStation login ↗</a>
      </section>

      <section class="step" id="s2">
        <div class="step-head">
          <span class="badge">2</span>
          <div><h2>Grab your token</h2>
            <p>Opens a Sony page showing <code>{{"npsso":"…"}}</code>. Select all &amp; copy it.</p></div>
        </div>
        <a class="btn token" href="{NPSSO_TOKEN_URL}" target="_blank" rel="noopener"
           onclick="mark(2)">🔑 Open my token page ↗</a>
      </section>

      <section class="step" id="s3">
        <div class="step-head">
          <span class="badge">3</span>
          <div><h2>Paste &amp; link</h2>
            <p>Tell us who you are, paste the token, and you're done.</p></div>
        </div>
        <label>Who are you?</label>
        {who_input}
        <label>Your token</label>
        <textarea name="npsso" id="npsso" oninput="mark(3)"
          placeholder='{{"npsso":"…"}} — paste the whole thing, we sort it out'></textarea>
        <div class="hint">💡 Don't worry about being precise — paste whatever the token page showed.</div>
        <button type="button" class="btn paste" onclick="pasteToken()">📋 Paste from clipboard</button>
        <button type="submit" class="btn link">🔗 Link my account</button>
      </section>
    </form>

    <div class="foot"><a href="/dashboard">← Back to the Squad dashboard</a></div>
  </div>
</div>

<script>
  function setPip(i){{ const p=document.getElementById('p'+i); if(p) p.classList.add('done'); }}
  function mark(n){{ setPip(n); }}
  setPip(0);  // unlocked to reach this page

  async function pasteToken(){{
    try {{
      const t = await navigator.clipboard.readText();
      if(t){{ const box=document.getElementById('npsso'); box.value=t.trim();
        mark(3); box.focus(); }}
    }} catch(e) {{ document.getElementById('npsso').focus(); }}
  }}
</script>
</body></html>"""


# ── Zitadel OIDC auth ────────────────────────────────────────────────────────
#
# Authorization-code + PKCE flow. No client secret needed.
# Required env vars: ZITADEL_CLIENT_ID, SESSION_SECRET
# Redirect URI to register in Zitadel: https://psn.crcmz.me/auth/callback

import hashlib as _hashlib, base64 as _base64, secrets as _secrets
from urllib.parse import urlencode as _urlencode
from itsdangerous import URLSafeTimedSerializer as _USTS, BadSignature, SignatureExpired

ZITADEL_ISSUER        = os.environ.get("ZITADEL_ISSUER", "https://auth.crcmz.me")
ZITADEL_CLIENT_ID     = os.environ.get("ZITADEL_CLIENT_ID", "")
ZITADEL_SERVICE_TOKEN = os.environ.get("ZITADEL_SERVICE_TOKEN", "")
SESSION_SECRET        = os.environ.get("SESSION_SECRET", "")

_SESSION_COOKIE    = "psn_session"
_OIDC_STATE_COOKIE = "psn_oidc_state"
_SESSION_MAX_AGE   = 60 * 60 * 24 * 30  # 30 days
_OIDC_CONFIG_CACHE: dict = {}

# The only public hostname; bare IPs from the tailnet/LAN bypass auth.
_PUBLIC_HOST = os.environ.get("PORTAL_PUBLIC_HOST", "psn.crcmz.me")
# Paths that must be reachable before authentication.
_OPEN_PATHS = {"/health", "/v2/health", "/auth/login", "/auth/callback",
               "/auth/logout", "/auth/passkey/begin", "/auth/passkey/complete",
               "/.well-known/webauthn"}


def _signer() -> _USTS:
    return _USTS(SESSION_SECRET or "dev-insecure", salt="psn-session")


def _state_signer() -> _USTS:
    return _USTS(SESSION_SECRET or "dev-insecure", salt="psn-oidc-state")


def _get_session(request: Request) -> dict | None:
    val = request.cookies.get(_SESSION_COOKIE)
    if not val:
        return None
    try:
        return _signer().loads(val, max_age=_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def _pkce() -> tuple[str, str]:
    verifier = _base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = _base64.urlsafe_b64encode(
        _hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def _oidc_cfg() -> dict:
    global _OIDC_CONFIG_CACHE
    if _OIDC_CONFIG_CACHE:
        return _OIDC_CONFIG_CACHE
    import httpx as _hx
    async with _hx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{ZITADEL_ISSUER}/.well-known/openid-configuration")
        r.raise_for_status()
        _OIDC_CONFIG_CACHE = r.json()
    return _OIDC_CONFIG_CACHE


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if path in _OPEN_PATHS:
        return await call_next(request)

    # Tailscale/LAN direct-IP hits (Stream Deck, etc.) bypass auth entirely.
    host = (request.headers.get("host") or "").split(":")[0]
    if host != _PUBLIC_HOST:
        return await call_next(request)

    if _get_session(request):
        return await call_next(request)

    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        return RedirectResponse(url=f"/auth/login?next={next_url}", status_code=302)
    return JSONResponse({"detail": "authentication required"}, status_code=401)


def _login_page(error: str = "", next: str = "/") -> str:
    err_html = f'<div class="msg err">⚠️ {error}</div>' if error else ""
    safe_next = next if next.startswith("/") else "/"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.png">
<title>Sign in · PS HQ</title>
<style>
  :root {{ color-scheme:dark; }}
  * {{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }}
  html,body {{ margin:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:#f3ecff; min-height:100dvh; display:flex; align-items:center;
    justify-content:center; padding:24px 16px; background:#05030f;
    position:relative; overflow:hidden; }}
  body::before {{ content:""; position:fixed; inset:-30% -10%; z-index:-1;
    background:
      radial-gradient(38% 40% at 18% 12%, rgba(255,47,214,.34), transparent 60%),
      radial-gradient(40% 40% at 84% 18%, rgba(34,230,255,.30), transparent 60%),
      radial-gradient(46% 42% at 55% 96%, rgba(157,92,255,.28), transparent 62%);
    filter:blur(34px); animation:drift 22s ease-in-out infinite alternate; }}
  @keyframes drift {{ to {{ transform:translate3d(4%,3%,0) scale(1.12); }} }}
  .card {{ width:100%; max-width:400px; background:rgba(18,10,38,.76);
    border:1px solid rgba(255,60,200,.24); border-radius:24px; padding:32px 28px 28px;
    box-shadow:0 30px 80px rgba(0,0,0,.6), inset 0 1px 0 rgba(255,255,255,.06);
    backdrop-filter:blur(22px); -webkit-backdrop-filter:blur(22px);
    animation:rise .5s cubic-bezier(.2,.8,.2,1) both; }}
  @keyframes rise {{ from {{ opacity:0; transform:translateY(18px) scale(.97); }} }}
  .brand {{ display:flex; align-items:center; gap:13px; margin-bottom:26px; }}
  .logo {{ width:50px; height:50px; border-radius:14px; flex:none; display:grid;
    place-items:center; font-size:26px;
    background:linear-gradient(135deg,#ff2fd6,#9d5cff);
    box-shadow:0 0 22px rgba(255,47,214,.6), 0 0 44px rgba(157,92,255,.3);
    border:1px solid rgba(255,255,255,.15); }}
  h1 {{ font-size:22px; margin:0; font-weight:800; letter-spacing:.5px;
    background:linear-gradient(90deg,#22e6ff,#ff2fd6);
    -webkit-background-clip:text; background-clip:text; color:transparent; }}
  .sub {{ color:#9d8fc4; font-size:12px; margin:3px 0 0; letter-spacing:1px;
    text-transform:uppercase; }}
  label {{ display:block; font-size:11.5px; color:#9d8fc4; margin:18px 0 7px;
    font-weight:700; letter-spacing:.4px; text-transform:uppercase; }}
  input {{ width:100%; padding:14px; border-radius:13px;
    border:1px solid rgba(140,160,255,.22); background:rgba(6,4,18,.7);
    color:#f3ecff; font-size:15px; -webkit-appearance:none; appearance:none;
    transition:border .15s, box-shadow .15s; }}
  input:focus {{ outline:none; border-color:#22e6ff;
    box-shadow:0 0 0 3px rgba(34,230,255,.18); }}
  .btn {{ display:flex; align-items:center; justify-content:center; width:100%;
    margin-top:24px; padding:15px; border-radius:14px; border:none;
    font-size:15.5px; font-weight:800; cursor:pointer; letter-spacing:.5px;
    background:linear-gradient(135deg,#ff2fd6,#9d5cff); color:#fff;
    box-shadow:0 10px 28px rgba(255,47,214,.45);
    transition:filter .15s, transform .07s; }}
  .btn:hover {{ filter:brightness(1.12); }}
  .btn:active {{ transform:scale(.975); }}
  .btn:disabled {{ opacity:.55; cursor:default; }}
  .divider {{ display:flex; align-items:center; gap:10px; margin:18px 0 4px;
    color:#6a5d8a; font-size:12px; font-weight:600; letter-spacing:.5px; }}
  .divider::before,.divider::after {{ content:""; flex:1; height:1px;
    background:rgba(255,255,255,.08); }}
  .btn-passkey {{ display:flex; align-items:center; justify-content:center; gap:9px;
    width:100%; margin-top:12px; padding:14px; border-radius:14px;
    border:1px solid rgba(34,230,255,.4); background:rgba(34,230,255,.07);
    color:#22e6ff; font-size:15px; font-weight:700; cursor:pointer;
    transition:background .15s, transform .07s; letter-spacing:.3px; }}
  .btn-passkey:hover {{ background:rgba(34,230,255,.14); }}
  .btn-passkey:active {{ transform:scale(.975); }}
  .btn-passkey:disabled {{ opacity:.5; cursor:default; }}
  .msg {{ padding:13px 15px; border-radius:13px; font-size:13.5px;
    margin-bottom:6px; display:flex; gap:10px; align-items:center; line-height:1.45; }}
  .err {{ background:rgba(255,107,139,.12); border:1px solid rgba(255,107,139,.4);
    color:#ffc0cd; }}
</style></head>
<body><div class="card">
  <div class="brand">
    <div class="logo">🎮</div>
    <div><h1>PS HQ</h1><p class="sub">Members only</p></div>
  </div>
  {err_html}
  <form method="post" action="/auth/login?next={safe_next}" id="f">
    <label for="em">Email or username</label>
    <input type="text" name="email" id="em" autocomplete="username webauthn"
      placeholder="Email or username" inputmode="email">
    <label for="pw">Password</label>
    <input type="password" name="pw" id="pw" required autocomplete="current-password"
      placeholder="Your password">
    <button type="submit" class="btn" id="btn">Sign in →</button>
  </form>
  <div class="divider">or</div>
  <button class="btn-passkey" id="pkBtn" onclick="passkeyLogin()">🔑 Sign in with Passkey</button>
  <div id="errmsg"></div>
</div>
<script>
  const params = new URLSearchParams(location.search);
  const nextUrl = params.get('next') || '/';

  document.getElementById('f').addEventListener('submit', () => {{
    const b = document.getElementById('btn');
    b.disabled = true; b.textContent = 'Signing in…';
  }});

  function showErr(msg) {{
    const el = document.getElementById('errmsg');
    el.innerHTML = '<div class="msg err" style="margin-top:12px">⚠️ '+msg+'</div>';
  }}

  function b64url(buf) {{
    return btoa(String.fromCharCode(...new Uint8Array(buf)))
      .replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
  }}
  function fromB64url(s) {{
    const pad = '='.repeat((4-s.length%4)%4);
    const b64 = (s+pad).replace(/-/g,'+').replace(/_/g,'/');
    return Uint8Array.from(atob(b64),c=>c.charCodeAt(0)).buffer;
  }}

  // Shared: send assertion to server and redirect on success.
  async function _completePasskey(sessionId, cred) {{
    const assertion = {{
      id: cred.id, rawId: b64url(cred.rawId), type: cred.type,
      response: {{
        clientDataJSON:    b64url(cred.response.clientDataJSON),
        authenticatorData: b64url(cred.response.authenticatorData),
        signature:         b64url(cred.response.signature),
        userHandle: cred.response.userHandle ? b64url(cred.response.userHandle) : null,
      }},
    }};
    const cr = await fetch('/auth/passkey/complete?next='+encodeURIComponent(nextUrl),{{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{sessionId, assertion}})
    }});
    if (!cr.ok) {{ showErr((await cr.json()).error || 'Passkey verification failed.'); return false; }}
    const {{next}} = await cr.json();
    window.location.href = next;
    return true;
  }}

  // Fetch a challenge from the server and decode it.
  // silent=true suppresses the visible error (used by conditional background flow).
  async function _beginPasskey(identifier, silent=false) {{
    const br = await fetch('/auth/passkey/begin',{{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify(identifier ? {{identifier}} : {{}})
    }});
    if (!br.ok) {{
      if (!silent) showErr((await br.json()).error || 'Could not start passkey flow.');
      return null;
    }}
    const {{sessionId, options}} = await br.json();
    options.challenge = fromB64url(options.challenge);
    if (options.allowCredentials)
      options.allowCredentials = options.allowCredentials.map(c=>
        ({{...c, id: fromB64url(c.id)}}));
    return {{sessionId, options}};
  }}

  // Conditional UI: starts silently on page load. The browser shows the passkey
  // as an autocomplete suggestion in the email field — user just taps it, no
  // button press needed. Aborted if the user clicks the explicit button instead.
  let _conditionalAbort = null;
  async function _startConditional() {{
    if (!window.PublicKeyCredential) return;
    const supported = await PublicKeyCredential.isConditionalMediationAvailable?.() ?? false;
    if (!supported) return;
    const began = await _beginPasskey('', true);  // silent — no error shown on page load
    if (!began) return;
    _conditionalAbort = new AbortController();
    try {{
      const cred = await navigator.credentials.get({{
        publicKey: began.options,
        mediation: 'conditional',
        signal: _conditionalAbort.signal,
      }});
      await _completePasskey(began.sessionId, cred);
    }} catch(e) {{
      // AbortError = user clicked the explicit button instead; anything else is unexpected.
      if (e.name !== 'AbortError' && e.name !== 'NotAllowedError')
        console.warn('conditional passkey error:', e);
    }}
  }}
  _startConditional();

  // Explicit button: abort any pending conditional request, then show the picker.
  async function passkeyLogin() {{
    if (!window.PublicKeyCredential) {{ showErr('Passkeys not supported in this browser.'); return; }}
    if (_conditionalAbort) {{ _conditionalAbort.abort(); _conditionalAbort = null; }}
    const identifier = document.getElementById('em').value.trim();
    if (!identifier) {{ document.getElementById('em').focus(); showErr('Enter your email first, then tap the passkey button.'); return; }}
    const btn = document.getElementById('pkBtn');
    btn.disabled = true; btn.textContent = '🔑 Waiting for passkey…';
    try {{
      const began = await _beginPasskey(identifier);
      if (!began) return;
      const cred = await navigator.credentials.get({{publicKey: began.options}});
      await _completePasskey(began.sessionId, cred);
    }} catch(e) {{
      if (e.name !== 'NotAllowedError') showErr('Passkey error: '+e.message);
    }} finally {{
      btn.disabled = false; btn.textContent = '🔑 Sign in with Passkey';
    }}
  }}
</script>
</body></html>"""


@app.get("/auth/login")
async def auth_login(request: Request, next: str = "/"):
    # Custom embedded form when service token is configured.
    if ZITADEL_SERVICE_TOKEN:
        return HTMLResponse(_login_page(next=next))
    # Fallback: PKCE redirect to Zitadel Login V2.
    if not ZITADEL_CLIENT_ID:
        return HTMLResponse("<h1>ZITADEL_CLIENT_ID not configured</h1>", status_code=503)
    try:
        cfg = await _oidc_cfg()
    except Exception as e:
        return HTMLResponse(f"<h1>Auth service unavailable: {e}</h1>", status_code=503)
    verifier, challenge = _pkce()
    state = _secrets.token_urlsafe(16)
    signed_state = _state_signer().dumps({"state": state, "verifier": verifier, "next": next[:200]})
    params = _urlencode({
        "client_id": ZITADEL_CLIENT_ID,
        "redirect_uri": f"https://{_PUBLIC_HOST}/auth/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    resp = RedirectResponse(url=f"{cfg['authorization_endpoint']}?{params}", status_code=302)
    resp.set_cookie(_OIDC_STATE_COOKIE, signed_state, httponly=True, samesite="lax",
                    secure=True, max_age=600, path="/")
    return resp


@app.post("/auth/login")
async def auth_login_submit(request: Request, next: str = "/"):
    form = await request.form()
    email    = (form.get("email") or "").strip()
    password = (form.get("pw")    or "").strip()

    if not email or not password:
        return HTMLResponse(_login_page(error="Email and password are required.", next=next), status_code=400)
    if not ZITADEL_SERVICE_TOKEN:
        return HTMLResponse(_login_page(error="Auth service not configured.", next=next), status_code=503)

    import httpx as _hx

    async def _resolve_login_name(identifier: str) -> str:
        """If identifier looks like an email and direct lookup fails, search by email."""
        if "@" not in identifier:
            return identifier
        try:
            async with _hx.AsyncClient(timeout=10) as c:
                sr = await c.post(
                    f"{ZITADEL_ISSUER}/management/v1/users/_search",
                    json={"queries": [{"emailQuery": {"emailAddress": identifier,
                                                      "method": "TEXT_QUERY_METHOD_EQUALS"}}]},
                    headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
                )
            if sr.status_code == 200:
                results = sr.json().get("result", [])
                if results:
                    return results[0].get("preferredLoginName", identifier)
        except Exception:
            pass
        return identifier

    try:
        login_name = await _resolve_login_name(email)
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{ZITADEL_ISSUER}/v2/sessions",
                json={"checks": {
                    "user":     {"loginName": login_name},
                    "password": {"password": password},
                }},
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201):
            logger.warning("auth: session create %s for %s (loginName=%s)", r.status_code, email, login_name)
            return HTMLResponse(_login_page(error="Invalid email or password.", next=next), status_code=401)

        session_id = r.json().get("sessionId", "")
        async with _hx.AsyncClient(timeout=10) as c:
            sr = await c.get(
                f"{ZITADEL_ISSUER}/v2/sessions/{session_id}",
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        user_f = sr.json().get("session", {}).get("factors", {}).get("user", {})
        sub        = user_f.get("id", "")
        user_email = user_f.get("loginName", email)
    except Exception as e:
        logger.error("auth: login error: %s", e)
        return HTMLResponse(_login_page(error="Auth service unavailable.", next=next), status_code=503)

    if not sub:
        return HTMLResponse(_login_page(error="Invalid email or password.", next=next), status_code=401)

    safe_next = next if next.startswith("/") else "/"
    session = {"sub": sub, "email": user_email}
    resp = RedirectResponse(url=safe_next, status_code=302)
    resp.set_cookie(_SESSION_COOKIE, _signer().dumps(session), httponly=True,
                    samesite="lax", secure=True, max_age=_SESSION_MAX_AGE, path="/")
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(url="/auth/login", status_code=302)

    signed_state = request.cookies.get(_OIDC_STATE_COOKIE)
    if not signed_state:
        return RedirectResponse(url="/auth/login", status_code=302)
    try:
        sp = _state_signer().loads(signed_state, max_age=600)
    except (BadSignature, SignatureExpired):
        return RedirectResponse(url="/auth/login", status_code=302)
    if sp.get("state") != state:
        return RedirectResponse(url="/auth/login", status_code=302)

    try:
        cfg = await _oidc_cfg()
        import httpx as _hx
        async with _hx.AsyncClient(timeout=15) as c:
            tr = await c.post(cfg["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"https://{_PUBLIC_HOST}/auth/callback",
                "client_id": ZITADEL_CLIENT_ID,
                "code_verifier": sp["verifier"],
            })
        if tr.status_code != 200:
            logger.error("oidc: token exchange failed %s: %s", tr.status_code, tr.text[:200])
            return RedirectResponse(url="/auth/login", status_code=302)

        access_token = tr.json().get("access_token", "")
        async with _hx.AsyncClient(timeout=10) as c:
            ur = await c.get(cfg["userinfo_endpoint"],
                             headers={"Authorization": f"Bearer {access_token}"})
        if ur.status_code != 200:
            logger.error("oidc: userinfo failed %s", ur.status_code)
            return RedirectResponse(url="/auth/login", status_code=302)
        userinfo = ur.json()
    except Exception as e:
        logger.error("oidc: callback error: %s", e)
        return RedirectResponse(url="/auth/login", status_code=302)

    next_url = sp.get("next") or "/"
    if not next_url.startswith("/"):
        next_url = "/"
    session = {"sub": userinfo.get("sub"), "email": userinfo.get("email", "")}

    resp = RedirectResponse(url=next_url, status_code=302)
    resp.set_cookie(_SESSION_COOKIE, _signer().dumps(session), httponly=True, samesite="lax",
                    secure=True, max_age=_SESSION_MAX_AGE, path="/")
    resp.delete_cookie(_OIDC_STATE_COOKIE, path="/")
    return resp


@app.post("/auth/passkey/begin")
async def passkey_begin(request: Request):
    """Step 1: ask Zitadel for a WebAuthn challenge.

    With identifier: user-specific flow — allowCredentials is scoped to that account.
    Without identifier: usernameless/discoverable flow — browser shows OS passkey picker,
    no email entry required.
    """
    if not ZITADEL_SERVICE_TOKEN:
        return JSONResponse({"error": "not configured"}, status_code=503)
    body = await request.json()
    identifier = (body.get("identifier") or "").strip()

    import httpx as _hx

    domain = ZITADEL_ISSUER.replace("https://", "").replace("http://", "").rstrip("/")
    webauthn_challenge = {
        "domain": domain,
        "userVerificationRequirement": "USER_VERIFICATION_REQUIREMENT_REQUIRED",
    }

    if identifier:
        # Resolve email → loginName if needed.
        login_name = identifier
        if "@" in identifier:
            try:
                async with _hx.AsyncClient(timeout=10) as c:
                    sr = await c.post(
                        f"{ZITADEL_ISSUER}/management/v1/users/_search",
                        json={"queries": [{"emailQuery": {"emailAddress": identifier,
                                                          "method": "TEXT_QUERY_METHOD_EQUALS"}}]},
                        headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
                    )
                if sr.status_code == 200:
                    results = sr.json().get("result", [])
                    if results:
                        login_name = results[0].get("preferredLoginName", identifier)
            except Exception:
                pass
        session_body = {
            "checks": {"user": {"loginName": login_name}},
            "challenges": {"webAuthN": webauthn_challenge},
        }
    else:
        # Usernameless: no user check → empty allowCredentials → OS passkey picker.
        session_body = {"challenges": {"webAuthN": webauthn_challenge}}

    try:
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{ZITADEL_ISSUER}/v2/sessions",
                json=session_body,
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201):
            logger.warning("passkey/begin: %s body=%s", r.status_code, r.text[:200])
            return JSONResponse({"error": "could not start passkey flow"}, status_code=404)
        data = r.json()
        raw = data.get("challenges", {}).get("webAuthN", {}).get("publicKeyCredentialRequestOptions")
        if not raw:
            return JSONResponse({"error": "no webauthn challenge returned"}, status_code=500)
        options = raw.get("publicKey") or raw
        return JSONResponse({"sessionId": data["sessionId"], "options": options})
    except Exception as e:
        logger.error("passkey/begin error: %s", e)
        return JSONResponse({"error": "service unavailable"}, status_code=503)


@app.post("/auth/passkey/complete")
async def passkey_complete(request: Request, next: str = "/"):
    """Step 2: verify the WebAuthn assertion with Zitadel, set session cookie."""
    if not ZITADEL_SERVICE_TOKEN:
        return JSONResponse({"error": "not configured"}, status_code=503)
    body = await request.json()
    session_id = (body.get("sessionId") or "").strip()
    assertion  = body.get("assertion")
    if not session_id or not assertion:
        return JSONResponse({"error": "sessionId and assertion required"}, status_code=400)

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.patch(
                f"{ZITADEL_ISSUER}/v2/sessions/{session_id}",
                json={"checks": {"webAuthN": {"credentialAssertionData": assertion}}},
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201):
            logger.warning("passkey/complete: %s for session %s: %s", r.status_code, session_id, r.text[:200])
            return JSONResponse({"error": "passkey verification failed"}, status_code=401)

        async with _hx.AsyncClient(timeout=10) as c:
            sr = await c.get(
                f"{ZITADEL_ISSUER}/v2/sessions/{session_id}",
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        user_f     = sr.json().get("session", {}).get("factors", {}).get("user", {})
        sub        = user_f.get("id", "")
        user_email = user_f.get("loginName", "")
    except Exception as e:
        logger.error("passkey/complete error: %s", e)
        return JSONResponse({"error": "service unavailable"}, status_code=503)

    if not sub:
        return JSONResponse({"error": "session invalid"}, status_code=401)

    safe_next = next if next.startswith("/") else "/"
    session = {"sub": sub, "email": user_email}
    resp = JSONResponse({"ok": True, "next": safe_next})
    resp.set_cookie(_SESSION_COOKIE, _signer().dumps(session), httponly=True,
                    samesite="lax", secure=True, max_age=_SESSION_MAX_AGE, path="/")
    return resp


@app.post("/auth/passkey/register/begin")
async def passkey_register_begin(request: Request):
    """Start passkey registration for the logged-in user."""
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    if not user_id or not ZITADEL_SERVICE_TOKEN:
        return JSONResponse({"error": "not configured"}, status_code=503)

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{ZITADEL_ISSUER}/v2/users/{user_id}/passkeys",
                json={"returnCode": {}},
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201):
            logger.warning("passkey/register/begin: %s %s", r.status_code, r.text[:200])
            return JSONResponse({"error": "failed to initiate"}, status_code=500)
        d = r.json()
        passkey_id = d.get("passkeyId", "")
        # Zitadel wraps creation options as {"publicKey": {...}} — unwrap so the
        # browser's navigator.credentials.create({publicKey: options}) gets the
        # right shape directly.
        wrapper = d.get("publicKeyCredentialCreationOptions") or {}
        options = wrapper.get("publicKey") or wrapper
        if not options:
            return JSONResponse({"error": "no creation options returned"}, status_code=500)
        return JSONResponse({"passkeyId": passkey_id, "options": options})
    except Exception as e:
        logger.error("passkey/register/begin error: %s", e)
        return JSONResponse({"error": "service unavailable"}, status_code=503)


@app.post("/auth/passkey/register/complete")
async def passkey_register_complete(request: Request):
    """Verify and store the new passkey credential."""
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    body = await request.json()
    passkey_id = body.get("passkeyId", "")
    credential = body.get("credential")
    passkey_name = (body.get("passkeyName") or "My passkey")[:200]
    if not user_id or not passkey_id or not credential:
        return JSONResponse({"error": "missing fields"}, status_code=400)

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{ZITADEL_ISSUER}/v2/users/{user_id}/passkeys/{passkey_id}",
                json={"passkeyName": passkey_name, "publicKeyCredential": credential},
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201):
            logger.warning("passkey/register/complete: %s %s", r.status_code, r.text[:200])
            return JSONResponse({"error": "registration failed"}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("passkey/register/complete error: %s", e)
        return JSONResponse({"error": "service unavailable"}, status_code=503)


@app.get("/auth/settings/passkeys")
async def settings_list_passkeys(request: Request):
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    if not user_id or not ZITADEL_SERVICE_TOKEN:
        return JSONResponse({"passkeys": []})

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{ZITADEL_ISSUER}/zitadel.user.v2.UserService/ListPasskeys",
                json={"userId": user_id},
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201):
            logger.warning("settings/passkeys list: %s %s", r.status_code, r.text[:200])
            return JSONResponse({"passkeys": []})
        data = r.json()
        passkeys = [
            {"id": pk.get("id"), "name": pk.get("name") or "Passkey"}
            for pk in data.get("result", [])
            if pk.get("state") != "AUTH_FACTOR_STATE_NOT_READY"
        ]
        return JSONResponse({"passkeys": passkeys})
    except Exception as e:
        logger.error("settings/passkeys list error: %s", e)
        return JSONResponse({"passkeys": []})


@app.delete("/auth/settings/passkeys/{passkey_id}")
async def settings_delete_passkey(passkey_id: str, request: Request):
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    if not user_id or not ZITADEL_SERVICE_TOKEN:
        return JSONResponse({"error": "not configured"}, status_code=503)

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=10) as c:
            r = await c.delete(
                f"{ZITADEL_ISSUER}/v2/users/{user_id}/passkeys/{passkey_id}",
                headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
            )
        if r.status_code not in (200, 201, 204):
            logger.warning("settings/passkeys delete: %s %s", r.status_code, r.text[:200])
            return JSONResponse({"error": "failed to delete"}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("settings/passkeys delete error: %s", e)
        return JSONResponse({"error": "service unavailable"}, status_code=503)


@app.post("/auth/settings/password")
async def settings_change_password(request: Request):
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    if not user_id or not ZITADEL_SERVICE_TOKEN:
        return JSONResponse({"error": "not configured"}, status_code=503)
    body = await request.json()
    current = body.get("currentPassword", "")
    new_pw = body.get("newPassword", "")
    if not current or not new_pw:
        return JSONResponse({"error": "missing fields"}, status_code=400)

    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{ZITADEL_ISSUER}/zitadel.user.v2.UserService/SetPassword",
                json={
                    "userId": user_id,
                    "currentPassword": current,
                    "newPassword": {"password": new_pw, "changeRequired": False},
                },
                headers={
                    "Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}",
                    "Connect-Protocol-Version": "1",
                },
            )
        if r.status_code not in (200, 201):
            d = r.json()
            msg = d.get("message", "")
            if "invalid" in msg.lower() or "incorrect" in msg.lower() or r.status_code in (400, 401):
                user_msg = "Current password is incorrect." if "invalid" in msg.lower() else msg or "Failed to update password."
                return JSONResponse({"error": user_msg}, status_code=400)
            logger.warning("settings/password: %s %s", r.status_code, r.text[:200])
            return JSONResponse({"error": "Failed to update password."}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("settings/password error: %s", e)
        return JSONResponse({"error": "service unavailable"}, status_code=503)


_admin_cache: dict[str, tuple[bool, float]] = {}  # user_id → (is_admin, expires_ts)


async def _is_iam_admin(user_id: str) -> bool:
    """Check if user has any IAM role in Zitadel. Result cached for 5 minutes."""
    import time as _time
    now = _time.time()
    cached = _admin_cache.get(user_id)
    if cached and cached[1] > now:
        return cached[0]
    result = False
    if ZITADEL_SERVICE_TOKEN:
        import httpx as _hx
        try:
            async with _hx.AsyncClient(timeout=8) as c:
                r = await c.post(
                    f"{ZITADEL_ISSUER}/admin/v1/members/_search",
                    json={"queries": [{"userIdQuery": {"userId": user_id}}]},
                    headers={"Authorization": f"Bearer {ZITADEL_SERVICE_TOKEN}"},
                )
            if r.status_code == 200:
                result = bool(r.json().get("result"))
        except Exception as e:
            logger.warning("admin check failed for %s: %s", user_id, e)
    _admin_cache[user_id] = (result, now + 300)
    return result


@app.get("/auth/settings/psn")
async def settings_psn_status(request: Request):
    """PSN status for the logged-in user. Admins see all accounts."""
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    if not user_id:
        return JSONResponse({"linked": False, "unclaimed": []})

    is_admin = await _is_iam_admin(user_id)
    record = portal_mod.find_by_zitadel_id(user_id)
    all_users = portal_mod.list_users() if is_admin else None

    if record:
        base = {"linked": True, **record}
        if is_admin:
            base["admin"] = True
            base["users"] = all_users
        return JSONResponse(base)

    # Not yet claimed — everyone sees unclaimed list to pick from
    unclaimed = portal_mod.list_unclaimed()
    resp: dict = {"linked": False, "unclaimed": unclaimed}
    if is_admin:
        resp["admin"] = True
        resp["users"] = all_users
    return JSONResponse(resp)


@app.post("/auth/settings/psn/claim")
async def settings_psn_claim(request: Request):
    """Claim an unassigned PSN record for the logged-in user."""
    session = _get_session(request)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user_id = session.get("sub", "")
    if not user_id:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key or "/" in key or ".." in key:
        return JSONResponse({"error": "invalid key"}, status_code=400)
    ok = portal_mod.claim_record(key, user_id)
    if not ok:
        return JSONResponse({"error": "record not found or already claimed"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/auth/logout")
async def auth_logout():
    resp = RedirectResponse(url="/auth/login", status_code=302)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


@app.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request):
    return HTMLResponse(_portal_page())


@app.post("/portal/link", response_class=HTMLResponse)
def portal_link(
    request: Request,
    npsso: str = Form(...),
    mm_username: str = Form(""),
):
    zitadel_user_id = (_get_session(request) or {}).get("sub", "")
    try:
        result = portal_mod.link_user(npsso, mm_username=mm_username.strip(),
                                      zitadel_user_id=zitadel_user_id)
    except portal_mod.LinkError as e:
        return HTMLResponse(_portal_page(error=str(e)), status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.error("portal: link failed: %s", e)
        return HTMLResponse(
            _portal_page(error="Something went wrong. Try a fresh token."),
            status_code=500,
        )
    who = result.get("online_id") or result.get("mm_username") or "Your account"
    return HTMLResponse(_portal_page(ok=who))


@app.get("/portal/users")
def portal_users():
    return {"users": portal_mod.list_users()}


# === Squad Dashboard (psn.crcmz.me home) ===
#
# Live view of everyone linked via the portal: who's online, what they're
# playing, plus one-tap actions (Squad Up, Game Time, Roast) wired to the
# existing endpoints. Data comes from psn_data using the bot's auth.

import psn_data


# Background poller: refresh the squad cache every minute so online/playing
# status stays live even when nobody has the dashboard open, and so the game
# list's real-time lastPlayedDateTime is checked frequently enough to reflect
# "online right now". This is the ONLY periodic PSN caller; viewers read cache.
_poller_started = False

import mattermost as mm_client

# ARC Raiders alert: when someone starts playing it, post ONE message to the
# "Squad Alerts" Mattermost channel tagging the crew. Match is
# substring/case-insensitive. Mentions must use real MM usernames to ping.
_ARC_MATCH = "arc raider"
_SQUAD_ALERTS_CHANNEL = os.environ.get(
    "SQUAD_ALERTS_CHANNEL_ID", "5115wy8pc7ffuj5c3zor51jxew"
)
_ARC_TAGS = "@themoosecompany @zubair221b @deception @moiz"
# DISABLED: the auto ARC alert fired at the WRONG time. PSN's gamelist
# lastPlayedDateTime only updates when a session SYNCS (i.e. when someone stops
# / switches away), so "playing" flips true right as they LEAVE ARC -- the alert
# announced arrivals that were actually departures. There's no reliable live
# "just started" signal in the data, so we don't auto-ping. Set
# ARC_ALERT_ENABLED=1 to re-enable (not recommended).
ARC_ALERT_ENABLED = os.environ.get("ARC_ALERT_ENABLED", "0") == "1"
_arc_alerted = False


def _check_arc_alert(squad: list[dict]) -> None:
    """Post ONE tagging message to Squad Alerts when ARC play starts.

    No-op unless ARC_ALERT_ENABLED -- see note above on why this misfires.
    """
    global _arc_alerted
    if not ARC_ALERT_ENABLED or not mm_client.available():
        return
    playing = [
        m.get("online_id")
        for m in squad
        if m.get("playing") and _ARC_MATCH in (m.get("game") or "").lower()
    ]
    if playing and not _arc_alerted:
        who = ", ".join(p for p in playing if p) or "Someone"
        msg = (
            f"🎮🔫 **{who}** is on **ARC Raiders**! Squad up — who's in? 💥\n"
            f"{_ARC_TAGS}"
        )
        ok = mm_client.post_channel(_SQUAD_ALERTS_CHANNEL, msg)
        logger.info("arc alert: %s on ARC -> posted=%s", who, ok)
        _arc_alerted = True
    elif not playing:
        _arc_alerted = False


# === PSN → WhatsApp video forwarder =========================================
WA_BRIDGE_URL  = os.environ.get("WA_BRIDGE_URL", "")
WA_GOOPERS_JID = os.environ.get("WA_GOOPERS_JID", "")

# Resolve WA bridge host — host.docker.internal may not exist in Coolify
if WA_BRIDGE_URL:
    import socket as _socket
    _parsed = WA_BRIDGE_URL.replace("http://", "").replace("https://", "").split(":")[0]
    try:
        _socket.gethostbyname(_parsed)
    except _socket.gaierror:
        _port = WA_BRIDGE_URL.split(":")[-1] if ":" in WA_BRIDGE_URL.split("//")[-1] else "3100"
        try:
            with open("/proc/net/route") as _f:
                for _line in _f:
                    _parts = _line.split()
                    if _parts[1] == "00000000":
                        _gw_hex = _parts[2]
                        _gw = ".".join(str(int(_gw_hex[i:i+2], 16)) for i in [6, 4, 2, 0])
                        WA_BRIDGE_URL = f"http://{_gw}:{_port}"
                        break
        except Exception:
            pass

import clips as _clips
import clip_store as _cstore
from psn_messaging import ClipNotReady, ClipUnauthorized, ClipRateLimited, ClipError, ClipDownload
_clips.init()

_video_seen: set[str] = set()
_video_initialized: bool = False
_video_queue: "asyncio.Queue[str]" = None   # type: ignore[assignment]
_watched_messengers: list = []


def _messenger_for_group(group_id: str):
    for m in _watched_messengers:
        if m._group_id == group_id:
            return m
    return None


async def _forward_screenshot(uid: str, ugc_id: str, sender: str, body: str, wm) -> None:
    """Resolve a PSN screenshot ugcId, download it, and forward to WhatsApp."""
    import base64
    import httpx as _httpx

    if not WA_BRIDGE_URL or not WA_GOOPERS_JID:
        return
    try:
        logger.info("screenshot_forward_started uid=%s ugcId=%s sender=%s", uid, ugc_id, sender)
        image_bytes = await asyncio.to_thread(wm.resolve_and_download_screenshot, ugc_id)
        caption = f"{sender}: {body}" if body else sender
        payload = {
            "imageBase64": base64.b64encode(image_bytes).decode(),
            "groupJid": WA_GOOPERS_JID,
            "caption": caption,
            "idempotencyKey": f"psn-img:{uid}",
        }
        async with _httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{WA_BRIDGE_URL}/send-image", json=payload)
        logger.info("screenshot_forward_sent uid=%s status=%d", uid, resp.status_code)
    except Exception as exc:
        logger.error("screenshot_forward_failed uid=%s: %s", uid, exc)


async def _forward_image(uid: str, image_url: str, sender: str, body: str, wm) -> None:
    """Download a PSN image using auth headers and forward it to WhatsApp."""
    import base64
    import httpx as _httpx

    if not WA_BRIDGE_URL or not WA_GOOPERS_JID:
        return
    try:
        logger.info("image_forward_started uid=%s sender=%s", uid, sender)
        image_bytes = await asyncio.to_thread(wm.download_image, image_url)
        caption = f"{sender}: {body}" if body else sender
        payload = {
            "imageBase64": base64.b64encode(image_bytes).decode(),
            "groupJid": WA_GOOPERS_JID,
            "caption": caption,
            "idempotencyKey": f"psn-img:{uid}",
        }
        async with _httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{WA_BRIDGE_URL}/send-image", json=payload)
        logger.info("image_forward_sent uid=%s status=%d", uid, resp.status_code)
    except Exception as exc:
        logger.error("image_forward_failed uid=%s: %s", uid, exc)


def _send_to_wa(message_uid: str, video_bytes: bytes, sender: str, body: str = "") -> str | None:
    """POST video bytes to slaptastic. Returns wa_message_id or None."""
    import base64
    import httpx as _httpx

    # Guard: verify the bridge has an active WA connection before sending.
    # The bridge returns {"status":"ok","whatsapp":true/false}. If whatsapp is
    # false the bridge will accept the request and return 200 but never deliver —
    # raise ClipError so the retry loop holds the job until the bridge recovers.
    try:
        health = _httpx.get(f"{WA_BRIDGE_URL}/health", timeout=5).json()
        if not health.get("whatsapp"):
            raise ClipError("WA bridge WhatsApp connection is down — will retry")
    except ClipError:
        raise
    except Exception as e:
        raise ClipError(f"WA bridge health check failed: {e}") from e

    # PSN auto-generates "X sent a video clip." — not a real user caption
    import re as _re
    real_body = body if body and not _re.fullmatch(r'.+ sent a video clip\.', body, _re.IGNORECASE) else ""
    caption = f"🎮 {sender}: {real_body}" if real_body else f"🎮 {sender}"
    idempotency_key = f"psn:{message_uid}"

    payload = {
        "videoBase64": base64.b64encode(video_bytes).decode(),
        "groupJid": WA_GOOPERS_JID,
        "caption": caption,
        "idempotencyKey": idempotency_key,
    }
    logger.info("clip_whatsapp_send_started uid=%s bytes=%d", message_uid, len(video_bytes))
    r = _httpx.post(f"{WA_BRIDGE_URL}/send-video", json=payload, timeout=180)
    if r.status_code == 200:
        resp = r.json()
        # already_sent is fine — idempotency working as intended
        status = resp.get("status", "")
        wa_id  = resp.get("messageId")
        logger.info("clip_whatsapp_sent uid=%s status=%s waId=%s", message_uid, status, wa_id)
        return wa_id
    raise ClipError(f"WA bridge error {r.status_code}: {r.text[:200]}")


def _process_clip_job(message_uid: str, job: dict) -> str | None:
    """Run the full clip pipeline synchronously. Returns wa_message_id.

    Raises ClipNotReady, ClipUnauthorized, ClipRateLimited, ClipError.
    Stage awareness: if already archived, loads from store and skips to send.
    """
    if not WA_BRIDGE_URL or not WA_GOOPERS_JID:
        raise ClipError("WA_BRIDGE_URL or WA_GOOPERS_JID not configured")

    ugc_id   = job["ugc_id"]
    sender   = job["sender_online_id"]
    group_id = job.get("psn_group_id", "")
    body     = job.get("body") or ""

    # ── Resume from archive if available ──────────────────────────────────────
    storage_key = job.get("storage_key_original")
    if job.get("archive_status") == "archived" and storage_key:
        logger.info("clip_archive: resuming from stored copy uid=%s", message_uid)
        video_bytes = _cstore.load(storage_key)
        if not video_bytes:
            raise ClipError(f"archived clip missing from store: {storage_key}")
        return _send_to_wa(message_uid, video_bytes, sender, body)

    # ── Resolve + download ────────────────────────────────────────────────────
    _clips.mark(message_uid, _clips.RESOLVING)
    messenger = _messenger_for_group(group_id)
    if not messenger:
        # Fallback to primary messenger
        messenger = psn_messenger

    _clips.mark(message_uid, _clips.DOWNLOADING)
    result: ClipDownload = messenger.download_clip(ugc_id)  # raises on error

    # ── Archive ───────────────────────────────────────────────────────────────
    _clips.mark(message_uid, _clips.PROCESSING)
    key = _cstore.storage_key(message_uid, job.get("psn_created_at"))
    ok  = _cstore.archive(key, result.data)
    if not ok:
        raise ClipError("archive storage write failed")

    _clips.set_archived(
        message_uid, key,
        sha256=result.sha256,
        file_size=result.file_size,
        duration_seconds=result.duration_seconds,
        width=result.width,
        height=result.height,
        fps=result.fps,
        video_codec=result.video_codec,
        audio_codec=result.audio_codec,
        audio_sample_rate=result.audio_sample_rate,
    )

    # ── Send ─────────────────────────────────────────────────────────────────
    return _send_to_wa(message_uid, result.data, sender, body)




@app.on_event("startup")
async def _start_squad_poller():
    global _poller_started
    if _poller_started or not _v2_available:
        return
    _poller_started = True

    async def _loop():
        while True:
            try:
                # Presence-only refresh — no trophy API calls.
                # Trophy data is only fetched when someone opens the dashboard
                # (via /api/squad which uses include_stats=True).
                squad = await asyncio.to_thread(psn_data.squad_status, psn_auth, False)
                await asyncio.to_thread(_check_arc_alert, squad)
            except Exception as e:  # noqa: BLE001
                logger.debug("squad poller tick failed: %s", e)
            await asyncio.sleep(180)

    asyncio.create_task(_loop())
    logger.info("squad presence poller started (180s)")

    if WA_BRIDGE_URL and WA_GOOPERS_JID:
        global _watched_messengers, _video_queue
        _watched_messengers = [m for m in [psn_messenger, _squad_messenger] if m is not None]
        _video_queue = asyncio.Queue()

        async def _video_detect_loop():
            def _adjacent_caption(msgs: list[dict], idx: int, sender: str,
                                   window_ms: int = 5000) -> str:
                """Return the body of a type-1 message from the same sender
                within window_ms of msgs[idx], or '' if none found."""
                try:
                    ts0 = int(msgs[idx].get("timestamp") or 0)
                except (ValueError, TypeError):
                    return ""
                for j in range(max(0, idx - 3), min(len(msgs), idx + 4)):
                    if j == idx:
                        continue
                    m = msgs[j]
                    if m.get("messageType") != 1:
                        continue
                    if m.get("sender") != sender:
                        continue
                    try:
                        delta = abs(int(m.get("timestamp") or 0) - ts0)
                    except (ValueError, TypeError):
                        continue
                    if delta <= window_ms:
                        body = (m.get("body") or "").strip()
                        if body:
                            return body
                return ""

            global _video_initialized
            while True:
                try:
                    for wm in _watched_messengers:
                        msgs = await asyncio.to_thread(wm.get_messages, 10)
                        for idx, msg in enumerate(msgs):
                            uid = msg.get("messageUid", "")
                            if not uid or uid in _video_seen:
                                continue
                            _video_seen.add(uid)

                            # Parse PSN timestamp (ms → int)
                            psn_ts_ms: int | None = None
                            ts_raw = msg.get("timestamp")
                            if ts_raw:
                                try:
                                    psn_ts_ms = int(ts_raw)
                                except (ValueError, TypeError):
                                    pass

                            if not _video_initialized:
                                # Seed pass: record cursor, don't forward
                                if psn_ts_ms:
                                    _clips.update_cursor(wm._group_id, uid,
                                                         psn_ts_ms / 1000.0)
                                continue

                            msg_type = msg.get("messageType", 1)
                            sender = msg.get("sender", "unknown")

                            # Screenshot messages (type 3) — resolve ugcId and forward
                            screenshot_ugc_id = msg.get("screenshotUgcId") or ""
                            if msg_type == 3 and screenshot_ugc_id:
                                body_text = _adjacent_caption(msgs, idx, sender)
                                asyncio.create_task(
                                    _forward_screenshot(uid, screenshot_ugc_id, sender, body_text, wm)
                                )
                                continue

                            # Video clip messages
                            if msg_type != 210:
                                continue
                            ugc_id = msg.get("ugcId", "")
                            if not ugc_id:
                                continue
                            body_text = _adjacent_caption(msgs, idx, sender)
                            is_new = _clips.claim(
                                uid, ugc_id, wm._group_id, wm._group_name,
                                sender, psn_ts_ms, body_text,
                            )
                            if is_new:
                                logger.info(
                                    "clip_detected uid=%s ugcId=%s sender=%s group=%s",
                                    uid, ugc_id, sender, wm._group_name,
                                )
                                await _video_queue.put(uid)
                except Exception as exc:  # noqa: BLE001
                    logger.error("video-watch tick failed: %s", exc)
                finally:
                    if not _video_initialized:
                        recovered = _clips.recoverable_jobs()
                        for job in recovered:
                            _video_seen.add(job["message_uid"])
                            await _video_queue.put(job["message_uid"])
                        _video_initialized = True
                        logger.info(
                            "video-watch: seeded %d UIDs, recovered %d unfinished jobs",
                            len(_video_seen), len(recovered),
                        )
                await asyncio.sleep(30)

        async def _video_forward_worker():
            while True:
                uid = await _video_queue.get()
                try:
                    job = _clips.get(uid)
                    if not job or job["status"] in _clips.TERMINAL:
                        continue

                    exc_info: tuple[str, str, int] | None = None  # (kind, msg, extra)
                    wa_msg_id: str | None = None

                    try:
                        wa_msg_id = await asyncio.wait_for(
                            asyncio.to_thread(_process_clip_job, uid, job),
                            timeout=300.0,
                        )
                    except asyncio.TimeoutError:
                        exc_info = ("timeout", "ffmpeg/process timeout (300s)", 0)
                    except ClipNotReady as e:
                        exc_info = ("not_ready", str(e), 0)
                    except ClipUnauthorized:
                        exc_info = ("unauthorized", "PSN 401", 0)
                    except ClipRateLimited as e:
                        exc_info = ("rate_limited", str(e), e.retry_after)
                    except ClipError as e:
                        exc_info = ("error", str(e), 0)
                    except Exception as e:
                        exc_info = ("error", f"unexpected: {e}", 0)

                    if exc_info is None:
                        _clips.set_delivered(uid, wa_msg_id)
                        continue

                    kind, error_msg, extra = exc_info
                    attempts = job["attempts"] + 1

                    if kind == "unauthorized":
                        # Force token refresh; immediate retry
                        psn_auth._expires_at = 0
                        logger.warning("clip 401 uid=%s — refreshing PSN token", uid)
                        if attempts <= 2:
                            await _video_queue.put(uid)
                        else:
                            _clips.mark(uid, _clips.FAILED,
                                        error="401 after token refresh")
                        continue

                    if kind == "not_ready":
                        # PSN CDN still transcoding — staged delays
                        delays = [30, 60, 120, 240, 480]
                        if attempts > len(delays):
                            _clips.mark(uid, _clips.FAILED,
                                        error=f"media never ready: {error_msg}")
                            logger.error("clip_failed uid=%s reason=never_ready", uid)
                        else:
                            delay = delays[attempts - 1]
                            _clips.mark(uid, _clips.WAITING_MEDIA,
                                        error=error_msg,
                                        next_attempt_at=_time.time() + delay)
                            logger.info(
                                "clip_retry_scheduled uid=%s attempt=%d delay=%ds reason=not_ready",
                                uid, attempts, delay,
                            )
                            async def _requeue_nr(u=uid, d=delay):
                                await asyncio.sleep(d)
                                await _video_queue.put(u)
                            asyncio.create_task(_requeue_nr())
                        continue

                    if kind == "rate_limited":
                        delay = max(extra, 30)
                        _clips.mark(uid, _clips.DISCOVERED,
                                    error=error_msg,
                                    next_attempt_at=_time.time() + delay)
                        logger.warning("clip_retry_scheduled uid=%s reason=rate_limited delay=%ds",
                                       uid, delay)
                        async def _requeue_rl(u=uid, d=delay):
                            await asyncio.sleep(d)
                            await _video_queue.put(u)
                        asyncio.create_task(_requeue_rl())
                        continue

                    # timeout / generic error — exponential backoff
                    if attempts >= 5:
                        _clips.mark(uid, _clips.FAILED,
                                    error=f"max attempts: {error_msg}")
                        logger.error("clip_failed uid=%s attempts=%d error=%s",
                                     uid, attempts, error_msg[:80])
                    else:
                        delay = 30 * (2 ** (attempts - 1))
                        _clips.mark(uid, _clips.DISCOVERED,
                                    error=error_msg,
                                    next_attempt_at=_time.time() + delay)
                        logger.info(
                            "clip_retry_scheduled uid=%s attempt=%d delay=%ds",
                            uid, attempts, delay,
                        )
                        async def _requeue_e(u=uid, d=delay):
                            await asyncio.sleep(d)
                            await _video_queue.put(u)
                        asyncio.create_task(_requeue_e())

                except Exception as exc:  # noqa: BLE001
                    logger.exception("video worker unexpected error uid=%s: %s", uid, exc)
                finally:
                    _video_queue.task_done()

        asyncio.create_task(_video_detect_loop())
        asyncio.create_task(_video_forward_worker())
        logger.info("video watcher started (30s, %d groups, persistent clips DB)",
                    len(_watched_messengers))


@app.get("/api/video-jobs")
def api_video_jobs():
    """Clip pipeline status: counts + queue depth."""
    q_depth = _video_queue.qsize() if _video_queue is not None else 0
    return {"stats": _clips.stats(), "queue_depth": q_depth}


@app.get("/api/pipeline-status")
def api_pipeline_status():
    """Aggregated health for the PSN → montage pipeline."""
    import httpx as _hx
    import time as _time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def _ping(url, timeout=3.0):
        try:
            t0 = _time.monotonic()
            r = _hx.get(url, timeout=timeout)
            ms = int((_time.monotonic() - t0) * 1000)
            return {"status": "ok" if r.status_code < 400 else "error", "ms": ms}
        except Exception:
            return {"status": "down", "ms": None}

    montage_health = _ping("http://10.0.1.1:3099/health")
    wa_health      = _ping("http://10.0.1.1:3100/health")

    # Clip stats for current calendar month (Pacific time)
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime.now(tz)
    import calendar
    month_start = datetime(now.year, now.month, 1, tzinfo=tz).timestamp()
    next_month  = (now.month % 12) + 1
    next_year   = now.year + (1 if now.month == 12 else 0)
    month_end   = datetime(next_year, next_month, 1, tzinfo=tz).timestamp()

    clips_this_month = 0
    last_clip_at     = None
    last_clip_sender = None
    try:
        all_month = _clips.list_clips(limit=200)
        month_clips = [
            c for c in all_month
            if c.get("montage_eligible") and
               month_start <= (c.get("discovered_at") or 0) < month_end
        ]
        month_clips.sort(key=lambda c: c.get("discovered_at") or 0, reverse=True)
        clips_this_month = len(month_clips)
        if month_clips:
            last_clip_at     = month_clips[0].get("discovered_at")
            last_clip_sender = month_clips[0].get("sender_online_id")
    except Exception:
        pass

    # Next auto-build info — find the earliest future build date for a month
    # that doesn't already have a completed montage.
    build_day  = int(os.environ.get("MONTAGE_BUILD_DAY",  "1"))
    build_hour = int(os.environ.get("MONTAGE_BUILD_HOUR", "6"))

    # Fetch existing completed montages so we can skip already-built months
    _built_months: set = set()
    try:
        _mj = _hx.get("http://10.0.1.1:3099/montages", timeout=3).json()
        for _m in _mj:
            if _m.get("status") == "completed":
                _built_months.add((_m["year"], _m["month"]))
    except Exception:
        pass

    # Walk forward month by month until we find one that hasn't been built yet
    _bm, _by = next_month, next_year
    for _ in range(24):
        # The build fires on build_day of (_bm, _by) for the PREVIOUS month
        prev_m = _bm - 1 if _bm > 1 else 12
        prev_y = _by if _bm > 1 else _by - 1
        if (prev_y, prev_m) not in _built_months:
            break
        _bm = (_bm % 12) + 1
        _by = _by + (1 if _bm == 1 else 0)

    next_build_dt = datetime(_by, _bm, build_day, build_hour, 0, 0, tzinfo=tz)
    next_build_ts = next_build_dt.timestamp()
    next_build_label = next_build_dt.strftime("%b %-d, %Y · %-I %p PT")

    # Last completed montage from psn-montage + its manifest
    last_montage = None
    manifest_by_uid: dict = {}  # uid → {included, excluded_reason}
    try:
        mj = _hx.get("http://10.0.1.1:3099/montages", timeout=3).json()
        # Find the latest completed build for the CURRENT month
        current_month_builds = [
            m for m in mj
            if m.get("status") == "completed"
            and m.get("year") == now.year
            and m.get("month") == now.month
        ]
        all_completed = [m for m in mj if m.get("status") == "completed"]
        if current_month_builds:
            m = current_month_builds[0]
            last_montage = {
                "version": m["version"], "year": m["year"], "month": m["month"],
                "clips": m.get("included_clip_count", 0),
                "duration": round(m.get("actual_duration_seconds") or 0, 1),
                "sent": bool(m.get("whatsapp_message_id")),
            }
            try:
                manifest_resp = _hx.get(
                    f"http://10.0.1.1:3099/montages/{m['id']}/manifest", timeout=3
                ).json()
                for c in manifest_resp.get("clips", []):
                    manifest_by_uid[c["message_uid"]] = {
                        "included": c.get("included", False),
                        "excluded_reason": c.get("excluded_reason"),
                    }
            except Exception:
                pass
        elif all_completed:
            m = all_completed[0]
            last_montage = {
                "version": m["version"], "year": m["year"], "month": m["month"],
                "clips": m.get("included_clip_count", 0),
                "duration": round(m.get("actual_duration_seconds") or 0, 1),
                "sent": bool(m.get("whatsapp_message_id")),
            }
    except Exception:
        pass

    # Build clip list for this month enriched with manifest status
    clip_list = []
    for c in month_clips:
        uid = c.get("message_uid", "")
        ms = manifest_by_uid.get(uid, {})
        clip_list.append({
            "uid":      uid,
            "sender":   c.get("sender_online_id") or "unknown",
            "duration": round(c.get("duration_seconds") or 0, 1),
            "at":       c.get("discovered_at"),
            "included": ms.get("included"),          # None = not yet built
            "reason":   ms.get("excluded_reason"),
        })

    return {
        "services": {
            "psn_messenger": {"status": "ok", "ms": 0},
            "psn_montage":   montage_health,
            "wa_bridge":     wa_health,
        },
        "clips_this_month": clips_this_month,
        "last_clip_at":     last_clip_at,
        "last_clip_sender": last_clip_sender,
        "next_build_ts":    next_build_ts,
        "next_build_label": next_build_label,
        "next_build_month": datetime(next_year, next_month, 1, tzinfo=tz).strftime("%B %Y"),
        "last_montage":     last_montage,
        "clips":            clip_list,
    }


@app.post("/api/scan-group")
async def api_scan_group(max_pages: int = 10):
    """Page back through group history and register unprocessed video clips for the montage.

    Paginates using beforeMessageUid up to max_pages×100 messages back.
    Clips are registered in the DB (montage-eligible) but NOT forwarded to WhatsApp —
    this is a backfill only. Live clips arriving via the normal watcher are forwarded.
    """
    if not _v2_available:
        raise HTTPException(503, "PSN auth not available")

    found = 0
    registered = 0
    skipped = 0
    pages_fetched = 0

    for wm in _watched_messengers:
        before_uid: str | None = None
        for _ in range(max_pages):
            try:
                msgs = await asyncio.to_thread(wm.get_messages_page, 100, before_uid)
            except Exception as exc:
                logger.error("scan-group: fetch failed: %s", exc)
                break

            if not msgs:
                break

            pages_fetched += 1
            for msg in msgs:
                uid = msg["messageUid"]
                if not uid:
                    continue
                if msg["messageType"] != 210:
                    continue
                ugc_id = msg["ugcId"]
                if not ugc_id:
                    continue
                found += 1
                sender = msg["sender"]
                ts_raw = msg.get("timestamp")
                psn_ts_ms: int | None = None
                try:
                    psn_ts_ms = int(ts_raw) if ts_raw else None
                except (ValueError, TypeError):
                    pass

                is_new = _clips.claim(
                    uid, ugc_id, wm._group_id, wm._group_name, sender, psn_ts_ms,
                )
                if is_new:
                    logger.info("scan-group: registered uid=%s ugcId=%s sender=%s", uid, ugc_id, sender)
                    registered += 1
                else:
                    skipped += 1

            # Paginate: use the oldest message uid as the cursor
            before_uid = msgs[-1]["messageUid"]
            if len(msgs) < 100:
                break  # last page

    logger.info("scan-group: pages=%d found=%d registered=%d skipped=%d", pages_fetched, found, registered, skipped)
    return {"pages_fetched": pages_fetched, "video_msgs_found": found, "registered": registered, "already_known": skipped}


@app.get("/status")
def status():
    """Lightweight operational status for monitoring."""
    q_depth  = _video_queue.qsize() if _video_queue is not None else 0
    s = _clips.stats()
    return {
        "psn": "connected" if _v2_available else "unavailable",
        "whatsapp": "configured" if (WA_BRIDGE_URL and WA_GOOPERS_JID) else "not_configured",
        "clip_store": _cstore.backend(),
        "groups": len(_watched_messengers),
        "queue_depth": q_depth,
        "clips_total": s.get("total", 0),
        "clips_delivered": s.get("delivered", 0),
        "clips_archived": s.get("archived", 0),
        "clips_failed": s.get("failed", 0),
        "clips_active": s.get("active", 0),
    }


@app.get("/clips")
def api_clips(
    month: str | None = None,
    sender: str | None = None,
    group_id: str | None = None,
    status: str | None = None,
    montage_eligible: bool | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Clip catalog with optional filters. month='2026-08'."""
    rows = _clips.list_clips(
        month=month, sender=sender, group_id=group_id,
        status=status, montage_eligible=montage_eligible,
        limit=min(limit, 200), offset=offset,
    )
    # Strip sha256 from list response (keep in detail view only)
    for r in rows:
        r.pop("sha256", None)
    return {"clips": rows, "count": len(rows)}


@app.get("/clips/{message_uid:path}")
def api_clip_detail(message_uid: str):
    """Full metadata for one clip."""
    row = _clips.get(message_uid)
    if not row:
        raise HTTPException(status_code=404, detail="clip not found")
    return row


@app.post("/api/clips/{message_uid:path}/resend")
def api_clip_resend(message_uid: str):
    """Force-resend a delivered clip to WhatsApp with a new idempotency key."""
    import base64 as _b64, time as _t
    job = _clips.get(message_uid)
    if not job:
        raise HTTPException(status_code=404, detail="clip not found")
    if not WA_BRIDGE_URL or not WA_GOOPERS_JID:
        raise HTTPException(status_code=503, detail="WA bridge not configured")
    storage_key = job.get("storage_key_original")
    if not storage_key:
        raise HTTPException(status_code=409, detail="clip not yet archived")
    video_bytes = _cstore.load(storage_key)
    if not video_bytes:
        raise HTTPException(status_code=410, detail="archived clip missing from store")
    sender = job.get("sender_online_id", "unknown")
    body   = job.get("body") or ""
    import re as _re
    real_body = body if body and not _re.fullmatch(r'.+ sent a video clip\.', body, _re.IGNORECASE) else ""
    caption = f"🎮 {sender}: {real_body}" if real_body else f"🎮 {sender}"
    import httpx as _httpx
    payload = {
        "videoBase64": _b64.b64encode(video_bytes).decode(),
        "groupJid": WA_GOOPERS_JID,
        "caption": caption,
        "idempotencyKey": f"psn:{message_uid}:r{int(_t.time())}",
    }
    r = _httpx.post(f"{WA_BRIDGE_URL}/send-video", json=payload, timeout=180)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"WA bridge: {r.text[:200]}")
    return {"status": "sent", "caption": caption, "wa": r.json()}


_hype_cache: dict = {}
_HYPE_MAX = 20  # messages = 100%

@app.get("/api/hype")
def api_hype():
    """Count today's squad group messages and return a hype level."""
    global _hype_cache
    if not _v2_available or _squad_messenger is None:
        return {"count": 0, "pct": 0, "label": "❄️ COLD", "level": "cold"}
    now = _time.time()
    if _hype_cache.get("ts", 0) > now - 60:
        return _hype_cache["data"]
    try:
        msgs = _squad_messenger.get_messages(100)
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).date()
        count = 0
        for m in msgs:
            ts = m.get("timestamp", "")
            if not ts:
                continue
            try:
                d = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
                if d == today:
                    count += 1
            except Exception:
                continue
        pct = min(100, round(count / _HYPE_MAX * 100))
        if count == 0:
            label, level = "☠️ DEAD SILENT", "dead"
        elif count < 4:
            label, level = "❄️ COLD", "cold"
        elif count < 9:
            label, level = "🌡️ WARMING UP", "warm"
        elif count < 15:
            label, level = "🔥 HOT", "hot"
        elif count < 20:
            label, level = "🔥🔥 ON FIRE", "fire"
        else:
            label, level = "💥 HYPE OVERLOAD", "overload"
        data = {"count": count, "pct": pct, "label": label, "level": level}
        _hype_cache = {"ts": now, "data": data}
        return data
    except Exception as e:
        logger.error("api/hype failed: %s", e)
        return {"count": 0, "pct": 0, "label": "❄️ COLD", "level": "cold"}


@app.get("/api/squad")
def api_squad():
    """Live presence + trophy stats for every squad member (JSON, for the UI)."""
    if not _v2_available:
        return JSONResponse({"squad": [], "error": "auth unavailable"})
    try:
        return {"squad": psn_data.squad_status(psn_auth)}
    except Exception as e:  # noqa: BLE001
        logger.error("dashboard: squad status failed: %s", e)
        return JSONResponse({"squad": [], "error": str(e)}, status_code=500)



class CustomButtonRequest(BaseModel):
    text: str
    send: bool = True  # also fire it to the group immediately


@app.get("/api/soundboard")
def api_soundboard():
    """Current soundboard (built-ins + custom), for live refresh after adds."""
    return {"buttons": _soundboard()}


@app.post("/api/soundboard")
def api_add_button(req: CustomButtonRequest, request: Request):
    """Turn a user's line into an AI-flavored permanent soundboard button.

    Uses the same Bedrock model as the roast bot to punch up the text, saves it
    to /data/soundboard.json, and (optionally) sends it to the group right away.
    """
    _rate_limit("custom_add", request.client.host)
    if req.send:
        _rate_limit("psn_send", request.client.host)
    raw = (req.text or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(raw) > 200:
        raise HTTPException(status_code=400, detail="Too long (200 char max)")

    flavored = roast_bot.flavor_message(raw)

    customs = _load_custom_buttons()
    if len(customs) >= 24:
        raise HTTPException(status_code=400, detail="Soundboard full (24 custom max)")

    # Label: a short preview of the flavored text; color cycles.
    label = flavored if len(flavored) <= 22 else flavored[:21].rstrip() + "…"
    color = _CUSTOM_COLORS[len(customs) % len(_CUSTOM_COLORS)]
    button = {"label": label, "msg": flavored, "cls": color, "custom": True}
    customs.append(button)
    _save_custom_buttons(customs)

    # Fire it now so the person sees it land in the group.
    sent = False
    if req.send and _squad_messenger is not None:
        try:
            sent = _squad_messenger.send_message(flavored)
        except Exception as e:  # noqa: BLE001
            logger.warning("custom button initial send failed: %s", e)

    return {"status": "added", "button": button, "flavored": flavored, "sent": sent}


@app.post("/api/soundboard/delete")
def api_delete_button(req: CustomButtonRequest):
    """Remove a custom button by its exact message text."""
    customs = _load_custom_buttons()
    kept = [b for b in customs if b.get("msg") != req.text]
    _save_custom_buttons(kept)
    return {"status": "deleted", "removed": len(customs) - len(kept)}


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    session = _get_session(request)
    user_email = session.get("email", "") if session else ""
    return HTMLResponse(_dashboard_html(user_email))




# The dashboard's "soundboard" buttons. Built-in defaults live here; custom ones
# that anyone adds via the dashboard are AI-flavored and persisted to
# /data/soundboard.json so they become permanent buttons for everyone.
# Each posts a canned message to the squad PSN group via /v2/squad {message};
# `path` instead of `msg` hits a non-message action. `cls` picks a color.
_SOUNDBOARD_DEFAULTS = [
    {"label": "👉👌 Have you ever?", "msg": "Have you ever? 👉👌", "cls": "c1"},
    {"label": "🧊☕ Iced Cap STORY", "msg": "🧊☕ Iced Cap STORRYYY! 📖✨", "cls": "c2"},
    {"label": "🙅‍♂️ Never", "msg": "Never 🙅‍♂️❌", "cls": "c3"},
    {"label": "💧 Water Break", "msg": "💧 Water break! 🚰💦", "cls": "c4"},
    {"label": "🎬 Zubi Clip It", "msg": "🎬 ZUBI CLIP IT!! 📸🔥 That was insane!", "cls": "c5"},
    {"label": "🎮 Squad Up", "msg": "🎮🔥 SQUAD UP! Who's hopping on? 🕹️💥", "cls": "c1"},
    {"label": "🕹️ Game Time", "msg": "🎮🔥 Let's party up y'all. It's GAME TIME! 🕹️💥", "cls": "c2"},
]

from pathlib import Path as _Path

_SOUNDBOARD_FILE = _Path("/data/soundboard.json")
# Colors cycled through for new custom buttons.
_CUSTOM_COLORS = ["c1", "c2", "c3", "c4", "c5"]


def _load_custom_buttons() -> list[dict]:
    try:
        return json.loads(_SOUNDBOARD_FILE.read_text()).get("buttons", [])
    except Exception:  # noqa: BLE001
        return []


def _save_custom_buttons(buttons: list[dict]) -> None:
    _SOUNDBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SOUNDBOARD_FILE.write_text(json.dumps({"buttons": buttons}, indent=2))


def _soundboard() -> list[dict]:
    """Built-in buttons followed by persisted custom ones."""
    return _SOUNDBOARD_DEFAULTS + _load_custom_buttons()


def _soundboard_json() -> str:
    return json.dumps(_soundboard())


def _dashboard_html(user_email: str = "") -> str:
    if user_email:
        disp = user_email.split("@")[0] if "@" in user_email else user_email
        user_html = (
            '<button class="user-btn" id="userBtn" onclick="toggleUserMenu()" aria-label="Account">'
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>'
            '</svg></button>'
            '<div class="user-drop" id="userMenu">'
            f'<div class="ud-name">{disp}</div>'
            '<button class="ud-item" style="width:100%;border:none;cursor:pointer;margin-bottom:6px" onclick="openSettings()">⚙️ Settings</button>'
            '<a class="ud-item" href="/auth/logout">Sign out</a>'
            '</div>'
        )
    else:
        user_html = '<a class="ud-item" href="/auth/login" style="padding:8px 12px;font-size:12px">Sign in</a>'
    return (_DASHBOARD_TMPL
            .replace("__SOUNDBOARD__", _soundboard_json())
            .replace("__USER__", user_html))


_DASHBOARD_TMPL = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.png">
<title>PS HQ · PSN</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800;900&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  /* ===== NEON / GAMER ARCADE THEME ===== */
  :root { color-scheme:dark;
    --bg:#05030f; --card:rgba(18,10,38,.66); --line:rgba(255,60,200,.22);
    --txt:#f3ecff; --dim:#9d8fc4;
    --neon:#ff2fd6;      /* hot magenta   */
    --cyan:#22e6ff;      /* electric cyan */
    --lime:#8cff2b;      /* acid green    */
    --violet:#9d5cff;    /* violet        */
    --gold:#ffd24a;
    --ok:var(--lime); }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html,body { margin:0; }
  body { font-family:"Rajdhani",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:var(--txt); min-height:100dvh; background:var(--bg);
    padding:0 0 var(--board-h, 220px); position:relative; overflow-x:hidden;
    font-size:15px; letter-spacing:.2px; }
  /* animated neon aurora */
  body::before { content:""; position:fixed; inset:-30% -10%; z-index:-3;
    background:
      radial-gradient(38% 40% at 18% 12%, rgba(255,47,214,.34), transparent 60%),
      radial-gradient(40% 40% at 84% 18%, rgba(34,230,255,.30), transparent 60%),
      radial-gradient(46% 42% at 55% 96%, rgba(157,92,255,.28), transparent 62%);
    filter:blur(34px); animation:drift 22s ease-in-out infinite alternate; }
  @keyframes drift { to { transform:translate3d(4%,3%,0) scale(1.12); } }
  /* scanline / grid texture overlay */
  body::after { content:""; position:fixed; inset:0; z-index:-2; pointer-events:none;
    opacity:.5;
    background-image:
      linear-gradient(rgba(34,230,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,47,214,.03) 1px, transparent 1px);
    background-size:40px 40px, 40px 40px; }
  .wrap { max-width:760px; margin:0 auto; padding:0 14px; }

  .top { display:flex; align-items:center; gap:13px; padding:20px 2px 14px; }
  .logo { width:48px; height:48px; border-radius:13px; flex:none; display:grid;
    place-items:center; font-size:25px;
    background:linear-gradient(135deg,var(--neon),var(--violet));
    box-shadow:0 0 22px rgba(255,47,214,.6), 0 0 44px rgba(157,92,255,.35);
    border:1px solid rgba(255,255,255,.15); }
  h1 { font-family:"Orbitron",sans-serif; font-size:21px; margin:0; font-weight:900;
    letter-spacing:1px; text-transform:uppercase;
    background:linear-gradient(90deg,var(--cyan),var(--neon));
    -webkit-background-clip:text; background-clip:text; color:transparent;
    text-shadow:0 0 18px rgba(255,47,214,.35); }
  .tag { color:var(--dim); font-size:12px; margin:3px 0 0; letter-spacing:1px;
    text-transform:uppercase; }
  .top .live { margin-left:auto; text-align:right; font-size:12px; color:var(--dim);
    text-transform:uppercase; letter-spacing:.5px; }
  .top .live b { color:var(--lime); font-size:16px; font-family:"Orbitron",sans-serif;
    text-shadow:0 0 12px rgba(140,255,43,.6); }

  /* STICKY SOUNDBOARD -- the hero. Always at top, compact, tappable. */
  /* Soundboard pinned to the BOTTOM of the screen -- always thumb-reachable. */
  .board-wrap { position:fixed; left:0; right:0; bottom:0; z-index:30;
    padding:10px 14px calc(12px + env(safe-area-inset-bottom));
    background:linear-gradient(0deg, rgba(7,11,24,.97) 72%, rgba(7,11,24,0));
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    border-top:1px solid var(--line); }
  .board-wrap > * { max-width:760px; margin:0 auto; }
  .board-title { display:flex; align-items:center; justify-content:space-between;
    width:100%; font-size:12px; letter-spacing:2px; color:var(--cyan);
    text-transform:uppercase; margin:0 0 8px; font-weight:700; padding:4px 4px;
    background:none; border:none; cursor:pointer; font-family:"Orbitron",sans-serif;
    text-shadow:0 0 10px rgba(34,230,255,.4); }
  .board-title .chev { transition:transform .25s ease; font-size:13px; }
  .board { display:grid; grid-template-columns:repeat(3,1fr); gap:8px;
    max-height:52vh; overflow-y:auto; -webkit-overflow-scrolling:touch;
    transition:max-height .28s ease, opacity .2s ease, margin .28s ease; }
  /* collapsed: hide the buttons grid, keep title + quick-send visible */
  .board-wrap.collapsed .board { max-height:0; opacity:0; overflow:hidden;
    margin-bottom:-8px; pointer-events:none; }
  .board-wrap.collapsed .chev { transform:rotate(-90deg); }
  /* ad-hoc quick-send row -- for one-off messages so people don't make buttons */
  .quick { display:flex; gap:8px; margin-top:10px; }
  .quick input { flex:1; padding:13px 15px; border-radius:12px; font-size:15px;
    border:1px solid rgba(34,230,255,.28); background:rgba(6,4,18,.8); color:var(--txt);
    -webkit-appearance:none; font-family:"Rajdhani",sans-serif; }
  .quick input:focus { outline:none; border-color:var(--cyan);
    box-shadow:0 0 0 3px rgba(34,230,255,.22), 0 0 16px rgba(34,230,255,.25); }
  .qsend { flex:none; width:52px; border:none; border-radius:12px; font-size:18px;
    color:#fff; cursor:pointer; background:linear-gradient(135deg,var(--cyan),var(--neon));
    box-shadow:0 0 16px rgba(255,47,214,.45); transition:transform .07s, filter .12s; }
  .qsend:active { transform:scale(.94); } .qsend:hover { filter:brightness(1.12); }
  .qsend:disabled { opacity:.5; }
  .snd { border:1px solid rgba(255,255,255,.14); border-radius:13px; padding:13px 8px;
    font-size:12.5px; font-weight:700; cursor:pointer; color:#fff; line-height:1.22;
    min-height:56px; display:flex; align-items:center; justify-content:center;
    text-align:center; position:relative; overflow:hidden;
    font-family:"Rajdhani",sans-serif; letter-spacing:.3px;
    transition:transform .07s, filter .12s, box-shadow .12s; }
  .snd:active { transform:scale(.93); }
  .snd:hover { filter:brightness(1.18) saturate(1.2); }
  .snd.flash { animation:flash .5s ease; }
  @keyframes flash { 0%{ box-shadow:0 0 0 0 rgba(140,255,43,.8);} 100%{ box-shadow:0 0 0 16px rgba(140,255,43,0);} }
  .snd.custom { position:relative; }
  .snd.custom::after { content:"✕"; position:absolute; top:3px; right:6px; font-size:10px; opacity:.5; }
  .snd.holding { animation:holdpulse .6s ease forwards; }
  @keyframes holdpulse { to { transform:scale(.86); filter:brightness(.6) saturate(1.5);
    box-shadow:0 0 0 3px rgba(255,47,120,.8) inset, 0 0 20px rgba(255,47,120,.6); } }
  /* neon color chips -- dark fill + glowing border/text */
  .c1 { background:linear-gradient(135deg,rgba(34,230,255,.16),rgba(34,230,255,.04));
    border-color:rgba(34,230,255,.55); color:#c8fbff; box-shadow:0 0 14px rgba(34,230,255,.25); }
  .c2 { background:linear-gradient(135deg,rgba(255,47,214,.16),rgba(255,47,214,.04));
    border-color:rgba(255,47,214,.55); color:#ffd6f6; box-shadow:0 0 14px rgba(255,47,214,.25); }
  .c3 { background:linear-gradient(135deg,rgba(157,92,255,.18),rgba(157,92,255,.05));
    border-color:rgba(157,92,255,.55); color:#e4d4ff; box-shadow:0 0 14px rgba(157,92,255,.25); }
  .c4 { background:linear-gradient(135deg,rgba(140,255,43,.15),rgba(140,255,43,.04));
    border-color:rgba(140,255,43,.5); color:#e0ffc0; box-shadow:0 0 14px rgba(140,255,43,.22); }
  .c5 { background:linear-gradient(135deg,rgba(255,210,74,.18),rgba(255,150,58,.05));
    border-color:rgba(255,210,74,.55); color:#fff0c0; box-shadow:0 0 14px rgba(255,180,60,.25); }
  .snd.add { background:rgba(255,255,255,.03); border:1.5px dashed rgba(255,47,214,.5);
    color:#ff9ee8; box-shadow:none; }

  .tabs { display:flex; gap:6px; background:var(--card); border:1px solid var(--line);
    padding:5px; border-radius:16px; margin:14px 0; backdrop-filter:blur(18px);
    -webkit-backdrop-filter:blur(18px); }
  .tab { flex:1; text-align:center; padding:10px 4px; border-radius:11px; border:none;
    background:none; color:var(--dim); cursor:pointer; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; font-family:"Rajdhani",sans-serif;
    font-size:clamp(11px,3vw,13px); font-weight:700; letter-spacing:.5px;
    text-transform:uppercase; transition:background .15s,color .15s,box-shadow .15s; }
  .tab-icon { display:none; }
  .tab.on { background:linear-gradient(135deg,var(--neon),var(--violet)); color:#fff;
    box-shadow:0 0 16px rgba(255,47,214,.5); }
  .panel { display:none; } .panel.on { display:block; animation:fade .3s ease both; }
  @keyframes fade { from { opacity:0; transform:translateY(6px); } }

  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:8px 16px; backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px);
    box-shadow:0 0 24px rgba(255,47,214,.08), inset 0 1px 0 rgba(255,255,255,.05); }
  .row { display:flex; align-items:center; gap:13px; padding:13px 4px;
    border-bottom:1px solid rgba(255,255,255,.05); }
  .row:last-child { border-bottom:none; }
  .avwrap { position:relative; flex:none; }
  .av { width:50px; height:50px; border-radius:12px; background:#1a1030; object-fit:cover;
    display:block; border:1px solid rgba(255,255,255,.1); }
  .playing .av { border-color:rgba(140,255,43,.6); box-shadow:0 0 14px rgba(140,255,43,.4); }
  .gicon { position:absolute; right:-6px; bottom:-6px; width:26px; height:26px;
    border-radius:8px; object-fit:cover; border:2px solid #0e0620; box-shadow:0 2px 8px rgba(0,0,0,.7); }
  .who { flex:1; min-width:0; }
  .name { font-weight:700; font-size:15.5px; display:flex; align-items:center;
    font-family:"Rajdhani",sans-serif; letter-spacing:.3px; }
  .mm { color:#7a6ca0; font-size:12px; font-weight:500; margin-left:6px; }
  .state { font-size:12.5px; color:var(--dim); margin-top:3px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dot { width:9px; height:9px; border-radius:50%; margin-right:7px; flex:none;
    background:#4a3f6b; display:inline-block; }
  .playing .name { color:#eaffd4; }
  .playing .dot { background:var(--lime); box-shadow:0 0 12px var(--lime); animation:pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow:0 0 3px var(--lime); opacity:.55; } }
  .game-badge { color:var(--lime); font-weight:600; text-shadow:0 0 8px rgba(140,255,43,.4); }
  .game-badge b { color:#eaffd4; }
  .lastgame { color:#8a7db0; }
  .troph { display:flex; gap:9px; align-items:center; flex:none; }
  .lvl { text-align:center; }
  .lvl .v { font-size:18px; font-weight:800; line-height:1; font-family:"Orbitron",sans-serif;
    background:linear-gradient(135deg,var(--gold),#ff9d3a);
    -webkit-background-clip:text; background-clip:text; color:transparent;
    text-shadow:0 0 12px rgba(255,180,60,.4); }
  .lvl .k { font-size:9px; color:var(--dim); letter-spacing:.5px; }
  .tcount { font-size:11.5px; color:var(--dim); text-align:right; line-height:1.5; }
  .tcount .p { color:var(--cyan); font-weight:700; }

  /* "playing together" hype banner */
  .together { display:flex; align-items:center; gap:12px; margin-bottom:14px;
    padding:14px 16px; border-radius:16px; cursor:pointer;
    background:linear-gradient(135deg,rgba(140,255,43,.16),rgba(34,230,255,.1));
    border:1px solid rgba(140,255,43,.5); box-shadow:0 0 24px rgba(140,255,43,.25);
    animation:fade .4s ease both; }
  .together .gicon2 { width:46px; height:46px; border-radius:11px; object-fit:cover; flex:none;
    border:1px solid rgba(255,255,255,.2); }
  .together .t-main { flex:1; min-width:0; }
  .together .t-title { font-family:"Orbitron",sans-serif; font-weight:800; font-size:14px;
    color:#eaffd4; text-shadow:0 0 10px rgba(140,255,43,.5); }
  .together .t-sub { font-size:12.5px; color:var(--dim); margin-top:2px; }
  .together .t-go { font-size:11px; font-weight:700; color:#04210f; padding:8px 12px;
    border-radius:10px; background:linear-gradient(135deg,var(--lime),var(--cyan)); flex:none;
    text-transform:uppercase; letter-spacing:.5px; }
  /* squad stat tiles */
  .statgrid { display:grid; grid-template-columns:repeat(3,1fr); gap:9px; margin-bottom:14px; }
  .stile { background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:12px 8px; text-align:center; backdrop-filter:blur(14px); }
  .stile .sv { font-family:"Orbitron",sans-serif; font-size:19px; font-weight:800; line-height:1;
    background:linear-gradient(135deg,var(--cyan),var(--neon));
    -webkit-background-clip:text; background-clip:text; color:transparent; }
  .stile .sl { font-size:9.5px; color:var(--dim); margin-top:6px; letter-spacing:.6px;
    text-transform:uppercase; }

  /* ── Hype Meter ── */
  .hype-wrap { margin-bottom:14px; padding:12px 14px; border-radius:14px;
    background:var(--card); border:1px solid var(--line);
    backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); }
  .hype-head { display:flex; align-items:center; justify-content:space-between;
    margin-bottom:8px; }
  .hype-title { font-family:"Orbitron",sans-serif; font-size:9px; letter-spacing:2px;
    color:var(--dim); text-transform:uppercase; }
  .hype-label { font-size:13px; font-weight:700; letter-spacing:.5px;
    font-family:"Rajdhani",sans-serif; }
  .hype-count { font-family:"Orbitron",sans-serif; font-size:16px; font-weight:800;
    background:linear-gradient(135deg,var(--cyan),var(--neon));
    -webkit-background-clip:text; background-clip:text; color:transparent; }
  .hype-track { height:10px; border-radius:99px; background:rgba(255,255,255,.07);
    overflow:hidden; }
  .hype-fill { height:100%; border-radius:99px; width:0%;
    transition:width .8s cubic-bezier(.4,0,.2,1); }
  .hype-fill.dead  { background:rgba(157,92,255,.4); }
  .hype-fill.cold  { background:linear-gradient(90deg,#4488ff,#22e6ff);
    box-shadow:0 0 10px rgba(34,230,255,.5); }
  .hype-fill.warm  { background:linear-gradient(90deg,var(--cyan),var(--lime));
    box-shadow:0 0 12px rgba(140,255,43,.45); }
  .hype-fill.hot   { background:linear-gradient(90deg,var(--lime),var(--gold));
    box-shadow:0 0 14px rgba(255,180,60,.55); animation:hypepulse 1.8s ease-in-out infinite; }
  .hype-fill.fire  { background:linear-gradient(90deg,var(--gold),var(--neon));
    box-shadow:0 0 18px rgba(255,47,214,.65); animation:hypepulse 1.2s ease-in-out infinite; }
  .hype-fill.overload { background:linear-gradient(90deg,var(--neon),var(--cyan),var(--neon));
    background-size:200% 100%; box-shadow:0 0 22px rgba(255,47,214,.8);
    animation:hyperain 1s linear infinite, hypepulse .8s ease-in-out infinite; }
  @keyframes hypepulse { 50% { filter:brightness(1.3) saturate(1.4); } }
  @keyframes hyperain { to { background-position:200% 0; } }

  .lb-row { display:flex; align-items:center; gap:13px; padding:13px 4px;
    border-bottom:1px solid rgba(255,255,255,.05); }
  .lb-row:last-child { border-bottom:none; }
  .rank { width:30px; text-align:center; font-size:17px; font-weight:800; color:var(--dim);
    flex:none; font-family:"Orbitron",sans-serif; }
  .bar { height:7px; border-radius:99px; background:rgba(255,255,255,.06); margin-top:6px; overflow:hidden; }
  .bar > i { display:block; height:100%; background:linear-gradient(90deg,var(--cyan),var(--neon));
    box-shadow:0 0 10px rgba(255,47,214,.5); }

  .empty { color:#7d8ab0; text-align:center; padding:30px 10px; font-size:14px; line-height:1.6; }
  .spin { color:#7d8ab0; text-align:center; padding:26px; }
  .link-cta { color:#7fb2ff; font-size:12.5px; text-decoration:none; }
  .toast { position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
    background:#1b2540; border:1px solid #33436b; color:#eaf0ff; padding:12px 20px;
    border-radius:13px; font-size:14px; opacity:0; pointer-events:none; transition:opacity .2s;
    z-index:50; box-shadow:0 12px 30px rgba(0,0,0,.5); }
  .toast.show { opacity:1; }

  /* ── Pipeline / Montage panel ── */
  .pip-section { margin-bottom:14px; }
  .pip-title { font-family:"Orbitron",sans-serif; font-size:10px; letter-spacing:2px;
    color:var(--dim); text-transform:uppercase; margin:0 0 8px; padding:0 2px; }
  .svc-row { display:flex; align-items:center; gap:11px; padding:12px 14px;
    background:var(--card); border:1px solid var(--line); border-radius:13px;
    margin-bottom:8px; }
  .svc-dot { width:10px; height:10px; border-radius:50%; flex:none;
    box-shadow:0 0 7px currentColor; }
  .dot-ok   { background:var(--lime); color:var(--lime); }
  .dot-warn { background:var(--gold); color:var(--gold); }
  .dot-down { background:#ff4040;     color:#ff4040; }
  .svc-name { font-weight:700; font-size:14px; flex:1; }
  .svc-meta { font-size:12px; color:var(--dim); }
  .big-stat { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
  .bstat { background:var(--card); border:1px solid var(--line); border-radius:13px;
    padding:14px 16px; text-align:center; }
  .bstat .bv { font-family:"Orbitron",sans-serif; font-size:28px; font-weight:900;
    background:linear-gradient(90deg,var(--cyan),var(--neon));
    -webkit-background-clip:text; background-clip:text; color:transparent; line-height:1.1; }
  .bstat .bl { font-size:11px; color:var(--dim); text-transform:uppercase;
    letter-spacing:1px; margin-top:4px; }
  .pip-build { background:var(--card); border:1px solid var(--line); border-radius:13px;
    padding:14px 16px; margin-bottom:14px; }
  .pip-build .pb-label { font-size:11px; color:var(--dim); text-transform:uppercase;
    letter-spacing:1px; margin-bottom:5px; }
  .pip-build .pb-date { font-family:"Orbitron",sans-serif; font-size:14px;
    color:var(--gold); text-shadow:0 0 10px rgba(255,210,74,.5); }
  .pip-build .pb-countdown { font-size:12px; color:var(--cyan); margin-top:4px; }
  .last-montage { background:var(--card); border:1px solid var(--line);
    border-radius:13px; padding:14px 16px; margin-bottom:14px; }
  .lm-row { display:flex; justify-content:space-between; align-items:center;
    font-size:13px; padding:3px 0; }
  .lm-key { color:var(--dim); }
  .lm-val { font-weight:700; }
  .sent-badge { display:inline-block; padding:2px 9px; border-radius:20px; font-size:11px;
    font-weight:700; letter-spacing:.5px; }
  .sent-badge.yes { background:rgba(140,255,43,.15); color:var(--lime);
    border:1px solid rgba(140,255,43,.4); }
  .sent-badge.no  { background:rgba(255,64,64,.12); color:#ff9090;
    border:1px solid rgba(255,64,64,.3); }
  .last-clip-note { font-size:12px; color:var(--dim); text-align:center;
    margin-top:2px; padding:4px 0; }
  .clip-list { background:var(--card); border:1px solid var(--line);
    border-radius:13px; overflow:hidden; }
  .clip-row { display:flex; align-items:center; gap:9px; padding:10px 14px;
    font-size:13px; border-bottom:1px solid rgba(255,255,255,.05); }
  .clip-row:last-child { border-bottom:none; }
  .cdot { width:18px; text-align:center; font-size:12px; font-weight:900; flex:none; }
  .cdot-in   { color:var(--lime); }
  .cdot-ex   { color:#ff6060; }
  .cdot-pend { color:var(--dim); }
  .csender { font-weight:700; flex:1; min-width:0; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }
  .cdur { color:var(--cyan); font-size:12px; flex:none; }
  .creason { font-size:11px; color:var(--gold); background:rgba(255,210,74,.1);
    border:1px solid rgba(255,210,74,.25); border-radius:8px; padding:1px 7px;
    flex:none; white-space:nowrap; }
  .cage { color:var(--dim); font-size:11px; flex:none; margin-left:auto; }

  .user-btn { width:38px; height:38px; border-radius:50%;
    border:1.5px solid rgba(255,47,214,.7);
    background:linear-gradient(135deg,rgba(255,47,214,.25),rgba(157,92,255,.25));
    box-shadow:0 0 12px rgba(255,47,214,.35), inset 0 1px 0 rgba(255,255,255,.1);
    color:#ff2fd6; cursor:pointer;
    display:grid; place-items:center; flex:none; padding:0;
    transition:box-shadow .15s, transform .1s; }
  .user-btn:hover { box-shadow:0 0 20px rgba(255,47,214,.6), inset 0 1px 0 rgba(255,255,255,.15); }
  .user-btn:active { transform:scale(.93); }
  .user-drop { position:absolute; top:calc(100% + 10px); right:0; min-width:150px;
    background:rgba(12,6,26,.97); border:1px solid rgba(255,47,214,.35); border-radius:14px;
    padding:8px; backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
    z-index:200; box-shadow:0 14px 40px rgba(0,0,0,.7); display:none; }
  .user-drop.open { display:block; animation:fade .18s ease both; }
  .ud-name { font-size:12px; color:var(--dim); padding:5px 10px 9px;
    border-bottom:1px solid rgba(255,255,255,.07); margin-bottom:7px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; }
  .ud-item { display:block; padding:10px; border-radius:10px; font-size:13.5px;
    font-weight:700; color:var(--neon); text-decoration:none; text-align:center;
    background:rgba(255,47,214,.08); border:1px solid rgba(255,47,214,.25); }
  .ud-item:hover { background:rgba(255,47,214,.18); }

  /* ── Settings modal ───────────────────────────────────────── */
  .smodal-overlay { position:fixed; inset:0; background:rgba(0,0,0,.7);
    backdrop-filter:blur(6px); -webkit-backdrop-filter:blur(6px);
    z-index:1000; display:none; align-items:flex-start; justify-content:center;
    padding:20px 12px; overflow-y:auto; }
  .smodal-overlay.open { display:flex; animation:fade .2s ease both; }
  .smodal { background:rgba(12,6,26,.98); border:1px solid rgba(255,47,214,.3);
    border-radius:20px; width:100%; max-width:460px; padding:0;
    box-shadow:0 24px 60px rgba(0,0,0,.8); overflow:hidden; }
  .smodal-head { display:flex; align-items:center; justify-content:space-between;
    padding:18px 20px 14px; border-bottom:1px solid rgba(255,255,255,.07); }
  .smodal-head h2 { margin:0; font-size:16px; color:var(--neon); letter-spacing:.5px; }
  .smodal-close { background:none; border:none; color:var(--dim); font-size:20px;
    cursor:pointer; padding:0 4px; line-height:1; }
  .smodal-close:hover { color:#fff; }
  .stabs { display:flex; gap:6px; padding:14px 20px 0; border-bottom:1px solid rgba(255,255,255,.07); }
  .stab { background:none; border:none; border-bottom:2px solid transparent;
    color:var(--dim); font-size:13px; font-weight:700; cursor:pointer;
    padding:0 4px 10px; letter-spacing:.4px; }
  .stab.active { color:var(--neon); border-bottom-color:var(--neon); }
  .spanel { display:none; padding:20px; }
  .spanel.active { display:block; }
  .smodal-sect { margin-bottom:20px; }
  .smodal-sect-title { font-size:11px; color:var(--dim); text-transform:uppercase;
    letter-spacing:1px; margin:0 0 10px; }
  .pk-row { display:flex; align-items:center; justify-content:space-between;
    background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08);
    border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  .pk-info { display:flex; flex-direction:column; gap:2px; }
  .pk-name { font-size:13.5px; font-weight:600; color:#fff; }
  .pk-date { font-size:11px; color:var(--dim); }
  .pk-del { background:rgba(255,60,60,.12); border:1px solid rgba(255,60,60,.3);
    color:#ff6060; border-radius:8px; padding:5px 10px; font-size:12px;
    font-weight:700; cursor:pointer; white-space:nowrap; }
  .pk-del:hover { background:rgba(255,60,60,.25); }
  .pk-empty { font-size:13px; color:var(--dim); text-align:center; padding:18px 0; }
  .smodal-btn { display:block; width:100%; padding:11px; border-radius:12px;
    background:rgba(255,47,214,.12); border:1px solid rgba(255,47,214,.35);
    color:var(--neon); font-size:13.5px; font-weight:700; cursor:pointer;
    text-align:center; margin-top:10px; }
  .smodal-btn:hover { background:rgba(255,47,214,.22); }
  .sfield { width:100%; background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.12);
    border-radius:10px; padding:10px 13px; color:#fff; font-size:14px;
    outline:none; box-sizing:border-box; margin-bottom:10px; }
  .sfield:focus { border-color:rgba(255,47,214,.6); }
  .sfield-label { font-size:12px; color:var(--dim); margin-bottom:5px; display:block; }
  .smsg { font-size:13px; border-radius:8px; padding:9px 12px; margin-bottom:10px;
    display:none; }
  .smsg.ok { background:rgba(0,220,120,.12); border:1px solid rgba(0,220,.5);
    color:#00dc78; display:block; }
  .smsg.err { background:rgba(255,60,60,.12); border:1px solid rgba(255,60,60,.4);
    color:#ff7070; display:block; }
</style></head>
<body>

<!-- Settings modal -->
<div class="smodal-overlay" id="settingsOverlay" onclick="if(event.target===this)closeSettings()">
  <div class="smodal">
    <div class="smodal-head">
      <h2>⚙️ Account Settings</h2>
      <button class="smodal-close" onclick="closeSettings()">✕</button>
    </div>
    <div class="stabs">
      <button class="stab active" onclick="switchTab('passkeys')">🔑 Passkeys</button>
      <button class="stab" onclick="switchTab('security')">🔒 Security</button>
      <button class="stab" onclick="switchTab('psn')">🎮 PSN</button>
    </div>

    <!-- Passkeys tab -->
    <div class="spanel active" id="tab-passkeys">
      <div class="smodal-sect">
        <p class="smodal-sect-title">Your saved passkeys</p>
        <div id="pkList"><div class="pk-empty">Loading…</div></div>
      </div>
      <button class="smodal-btn" onclick="addPasskeyFromSettings()">＋ Add new passkey</button>
      <div class="smsg" id="pkMsg"></div>
    </div>

    <!-- Security tab -->
    <div class="spanel" id="tab-security">
      <div class="smodal-sect">
        <p class="smodal-sect-title">Change password</p>
        <div class="smsg" id="pwMsg"></div>
        <label class="sfield-label">Current password</label>
        <input class="sfield" type="password" id="pwCur" autocomplete="current-password" placeholder="••••••••">
        <label class="sfield-label">New password</label>
        <input class="sfield" type="password" id="pwNew" autocomplete="new-password" placeholder="••••••••">
        <label class="sfield-label">Confirm new password</label>
        <input class="sfield" type="password" id="pwConf" autocomplete="new-password" placeholder="••••••••">
        <button class="smodal-btn" onclick="changePassword()">Update password</button>
      </div>
    </div>

    <!-- PSN tab -->
    <div class="spanel" id="tab-psn">
      <div class="smodal-sect">
        <p class="smodal-sect-title">PlayStation account</p>
        <div id="psnStatus" style="font-size:13.5px;color:var(--dim);margin-bottom:16px;line-height:1.6">
          Loading…
        </div>
      </div>
      <a class="smodal-btn" href="/portal" style="text-decoration:none">🎮 Link / re-link PSN account</a>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="top">
    <div class="logo">🎮</div>
    <div><h1>PS HQ</h1><p class="tag">PSN · Live</p></div>
    <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
      <div class="live" id="livecount"></div>
      <div style="position:relative">__USER__</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab on" data-p="squad" onclick="tab(this)"><span class="tab-icon">🎮</span><span class="tab-txt">Squad</span></button>
    <button class="tab" data-p="lb" onclick="tab(this)"><span class="tab-icon">🏆</span><span class="tab-txt">Ranks</span></button>
    <button class="tab" data-p="pipeline" onclick="tab(this)"><span class="tab-icon">🎬</span><span class="tab-txt">Clips</span></button>
  </div>
  <div id="together"></div>
  <div class="hype-wrap" id="hypeMeter">
    <div class="hype-head">
      <div>
        <div class="hype-title">Today's Hype</div>
        <div class="hype-label" id="hypeLabel">…</div>
      </div>
      <div class="hype-count"><span id="hypeCount">—</span> msgs</div>
    </div>
    <div class="hype-track"><div class="hype-fill dead" id="hypeFill"></div></div>
  </div>
  <div class="statgrid" id="statgrid"></div>
  <div class="panel on" id="p-squad"><div class="card" id="squad"><div class="spin">Loading squad…</div></div></div>
  <div class="panel" id="p-lb"><div class="card" id="lb"><div class="spin">Loading leaderboard…</div></div></div>
  <div class="panel" id="p-pipeline"><div id="pipeline-inner"><div class="spin">Loading pipeline…</div></div></div>
</div>

<div class="board-wrap" id="boardWrap">
  <button class="board-title" id="boardToggle" onclick="toggleBoard()">
    <span>🔊 Soundboard</span><span class="chev" id="chev">▾</span>
  </button>
  <div class="board" id="board"></div>
  <div class="quick">
    <input id="quick" type="text" placeholder="Send a quick message to the squad…"
      maxlength="200" autocomplete="off"
      onkeydown="if(event.key==='Enter')sendQuick()">
    <button class="qsend" onclick="sendQuick()" aria-label="Send">➤</button>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const SOUNDBOARD = __SOUNDBOARD__;
const $ = id => document.getElementById(id);
const esc = s => (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const toast = m => { const t=$('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2000); };

// Build the soundboard (mutable so custom adds refresh it live)
let BUTTONS = SOUNDBOARD.slice();
function renderButtons(){
  $('board').innerHTML = BUTTONS.map((b,i)=>
    '<button class="snd '+(b.cls||'c1')+(b.custom?' custom':'')+'" data-i="'+i+'" '+
    'onclick="fire(this)">'+esc(b.label)+'</button>'
  ).join('') +
    '<button class="snd add" onclick="openCustom()">＋ Custom</button>';
  bindLongPress();
  syncBoardHeight();
}
// Press-and-hold (600ms) on a CUSTOM button to delete it. Works on touch and
// mouse; the hold cancels the normal click so it doesn't also fire the message.
let _lpTimer=null, _lpFired=false;
function bindLongPress(){
  document.querySelectorAll('.snd.custom').forEach(el=>{
    const i=el.dataset.i;
    const start=(ev)=>{ _lpFired=false; el.classList.add('holding');
      _lpTimer=setTimeout(()=>{ _lpFired=true; el.classList.remove('holding');
        if(navigator.vibrate) navigator.vibrate(30); delBtn(ev,i); },600); };
    const cancel=()=>{ clearTimeout(_lpTimer); el.classList.remove('holding'); };
    el.addEventListener('touchstart',start,{passive:true});
    el.addEventListener('touchend',cancel); el.addEventListener('touchmove',cancel);
    el.addEventListener('mousedown',start);
    el.addEventListener('mouseup',cancel); el.addEventListener('mouseleave',cancel);
  });
}
// Keep the page's bottom padding == the fixed soundboard's real height so the
// squad list is never hidden behind it (fixes the "users cut off / can't scroll").
function syncBoardHeight(){
  const bar = document.querySelector('.board-wrap');
  if(bar) document.body.style.setProperty('--board-h', (bar.offsetHeight + 16) + 'px');
}
// Collapsible soundboard. Default = expanded; remembers the user's choice.
function toggleBoard(){
  const w=$('boardWrap'); w.classList.toggle('collapsed');
  try{ localStorage.setItem('sb_collapsed', w.classList.contains('collapsed')?'1':'0'); }catch(e){}
  setTimeout(syncBoardHeight,300);
}
if(localStorage.getItem('sb_collapsed')==='1') $('boardWrap').classList.add('collapsed');
window.addEventListener('resize', syncBoardHeight);
renderButtons();
async function fire(el){
  if(_lpFired){ _lpFired=false; return; }  // a long-press just deleted; don't send
  const b = BUTTONS[el.dataset.i];
  el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),500);
  try {
    let r;
    if(b.path){ r = await fetch(b.path,{method:'POST'}); }
    else { r = await fetch('/v2/squad',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:b.msg})}); }
    toast(r.ok ? 'Sent! 🎮' : 'Failed ('+r.status+')');
  } catch(e){ toast('Network error'); }
}
// Ad-hoc one-off message -> sent as-is to the group (not saved, no AI).
async function sendQuick(){
  const inp = $('quick'), btn = document.querySelector('.qsend');
  const msg = (inp.value||'').trim();
  if(!msg) return;
  btn.disabled = true;
  try {
    const r = await fetch('/v2/send',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg})});
    if(r.ok){ inp.value=''; toast('Sent! 🎮'); }
    else if(r.status===429){ toast('Slow down a sec ⏳'); }
    else toast('Failed ('+r.status+')');
  } catch(e){ toast('Network error'); }
  btn.disabled = false;
}
async function openCustom(){
  const text = prompt("What should the button say? The AI will add the flavor 🔥");
  if(!text || !text.trim()) return;
  toast("✨ AI is cooking…");
  try {
    const r = await fetch('/api/soundboard',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:text.trim()})});
    if(!r.ok){ toast('Failed ('+r.status+')'); return; }
    const d = await r.json();
    await refreshBoard();
    toast('Added: '+d.flavored.slice(0,40));
  } catch(e){ toast('Network error'); }
}
async function delBtn(ev, i){
  if(ev && ev.preventDefault) ev.preventDefault();
  const b = BUTTONS[i];
  if(!b || !b.custom) return false;
  if(!confirm('Remove this custom button?\n\n'+b.msg)) return false;
  try {
    await fetch('/api/soundboard/delete',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:b.msg})});
    await refreshBoard(); toast('Removed');
  } catch(e){ toast('Network error'); }
  return false;
}
async function refreshBoard(){
  try { const d = await (await fetch('/api/soundboard')).json();
    BUTTONS = d.buttons || BUTTONS; renderButtons();
  } catch(e){}
}

function tab(btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  btn.classList.add('on'); $('p-'+btn.dataset.p).classList.add('on');
}
function fmtLast(iso){ if(!iso) return 'offline';
  const s=(Date.now()-new Date(iso))/1000;
  if(s<3600) return Math.max(1,Math.floor(s/60))+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago'; }

let SQUAD=[];
function trophyCells(m){
  if(m.trophy_level==null) return '';
  return '<div class="troph"><div class="lvl"><div class="v">'+m.trophy_level+'</div><div class="k">LVL</div></div>'+
    '<div class="tcount"><span class="p">'+(m.platinum||0)+' ⚪</span><br>'+(m.gold||0)+' 🥇</div></div>';
}
function renderSquad(){
  const el=$('squad');
  if(!SQUAD.length){ el.innerHTML='<div class="empty">Nobody linked yet.<br>'+
    '<a class="link-cta" href="/portal">Link your account</a> to show up here.</div>'; return; }
  el.innerHTML = SQUAD.map(m=>{
    const name=esc(m.online_id||m.mm_username||'Unknown');
    const av=m.avatar||'';
    // Show the game icon for both currently-playing and last-played (offline).
    const gsrc=(m.game_icon)||(m.recent_game_icon)||'';
    const gi=gsrc?'<img class="gicon" src="'+esc(gsrc)+'">':'';
    const avImg='<div class="avwrap">'+(av?'<img class="av" src="'+esc(av)+'">':'<div class="av"></div>')+gi+'</div>';
    // Game-centric: no online/offline. If they recently switched to a
    // whitelisted game they're "ON" it (glowing); otherwise show their last
    // game, dimmed. No timestamps.
    const g = m.game || m.recent_game;
    let st, cls='';
    if(m.playing && g){ st='<span class="game-badge">🎮 On <b>'+esc(g)+'</b></span>'; cls='playing'; }
    else if(g){ st='<span class="lastgame">'+esc(g)+'</span>'; }
    else { st='<span class="lastgame">no recent game</span>'; }
    const mm=m.mm_username?'<span class="mm">@'+esc(m.mm_username)+'</span>':'';
    return '<div class="row '+cls+'">'+avImg+'<div class="who"><div class="name"><span class="dot"></span>'+name+mm+'</div>'+
      '<div class="state">'+st+'</div></div>'+trophyCells(m)+'</div>';
  }).join('');
}
function renderBoard(){
  const el=$('lb');
  const ranked=SQUAD.filter(m=>m.trophy_level!=null)
    .sort((a,b)=>(b.trophy_level-a.trophy_level)||((b.platinum||0)-(a.platinum||0)));
  if(!ranked.length){ el.innerHTML='<div class="empty">No trophy data yet.</div>'; return; }
  const max=ranked[0].trophy_level||1;
  el.innerHTML = ranked.map((m,i)=>{
    const name=esc(m.online_id||m.mm_username||'Unknown'); const av=m.avatar||'';
    const rk=i<3?['🥇','🥈','🥉'][i]:'#'+(i+1);
    return '<div class="lb-row"><div class="rank">'+rk+'</div>'+
      (av?'<img class="av" style="width:42px;height:42px" src="'+esc(av)+'">':'<div class="av" style="width:42px;height:42px"></div>')+
      '<div class="who"><div class="name">'+name+'</div>'+
      '<div class="state">Lvl '+m.trophy_level+' · '+(m.platinum||0)+' plat · '+(m.gold||0)+'🥇 '+(m.silver||0)+'🥈 '+(m.bronze||0)+'🥉</div>'+
      '<div class="bar"><i style="width:'+Math.round((m.trophy_level/max)*100)+'%"></i></div></div></div>';
  }).join('');
}
// "Playing together": if 2+ people are on the SAME game right now, hype it +
// let you rally the group into it with one tap.
function renderTogether(){
  const el=$('together'); el.innerHTML='';
  const counts={};
  SQUAD.filter(m=>m.playing && m.game).forEach(m=>{
    (counts[m.game]=counts[m.game]||{n:0,icon:m.game_icon,who:[]});
    counts[m.game].n++; counts[m.game].who.push(m.online_id||m.mm_username);
  });
  const top=Object.entries(counts).filter(([g,d])=>d.n>=2)
    .sort((a,b)=>b[1].n-a[1].n)[0];
  if(!top) return;
  const [game,d]=top;
  const icon=d.icon?'<img class="gicon2" src="'+esc(d.icon)+'">':'';
  el.innerHTML='<div class="together" onclick="rally('+JSON.stringify(game).replace(/"/g,'&quot;')+')">'+
    icon+'<div class="t-main"><div class="t-title">🔥 '+d.n+' in '+esc(game)+'</div>'+
    '<div class="t-sub">'+esc(d.who.join(', '))+' — squad\'s live!</div></div>'+
    '<div class="t-go">Rally ▶</div></div>';
}
async function rally(game){
  try {
    const r=await fetch('/v2/squad',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:'🎮🔥 Squad\'s on '+game+'! Who else is hopping in? 💥'})});
    toast(r.ok?'Rallied! 🎮':'Failed');
  } catch(e){ toast('Network error'); }
}
function renderStats(){
  const el=$('statgrid');
  const plat=SQUAD.reduce((a,m)=>a+(m.platinum||0),0);
  const lvls=SQUAD.filter(m=>m.trophy_level!=null).map(m=>m.trophy_level);
  const topLvl=lvls.length?Math.max(...lvls):0;
  // most common recent game across the squad
  const g={}; SQUAD.forEach(m=>{ const n=m.game||m.recent_game; if(n) g[n]=(g[n]||0)+1; });
  const fav=Object.entries(g).sort((a,b)=>b[1]-a[1])[0];
  el.innerHTML=
    '<div class="stile"><div class="sv">'+plat+'</div><div class="sl">⚪ Platinums</div></div>'+
    '<div class="stile"><div class="sv">'+topLvl+'</div><div class="sl">🏆 Top Level</div></div>'+
    '<div class="stile"><div class="sv" style="font-size:13px">'+(fav?esc(fav[0]).slice(0,14):'—')+'</div><div class="sl">🎯 Squad Fav</div></div>';
}
async function loadSquad(){
  try {
    const {squad=[]}=await (await fetch('/api/squad')).json();
    SQUAD=squad;
    const playing=squad.filter(m=>m.playing).length;
    $('livecount').innerHTML = playing ? ('<b>'+playing+'</b> 🎮 in a game') : 'nobody in a game';
    // On-a-game members float to the top.
    squad.sort((a,b)=> (b.playing?1:0)-(a.playing?1:0));
    renderTogether(); renderStats(); renderSquad(); renderBoard();
  } catch(e){ $('squad').innerHTML='<div class="empty">Couldn\'t load squad.</div>'; }
}
loadSquad(); setInterval(loadSquad, 30000);

// ── Hype Meter ───────────────────────────────────────────────────────────────
async function loadHype(){
  try {
    const d = await (await fetch('/api/hype')).json();
    $('hypeLabel').textContent = d.label || '—';
    $('hypeCount').textContent = d.count ?? '—';
    const fill = $('hypeFill');
    fill.className = 'hype-fill ' + (d.level || 'cold');
    // defer width so transition fires after class change
    requestAnimationFrame(()=>{ fill.style.width = (d.pct||0) + '%'; });
  } catch(e){}
}
loadHype(); setInterval(loadHype, 60000);

// ── Pipeline / Montage status ──────────────────────────────────────────────
function fmtAgo(ts) {
  if (!ts) return 'never';
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60)   return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function fmtCountdown(ts) {
  const s = Math.floor(ts - Date.now()/1000);
  if (s <= 0) return 'imminent';
  const d = Math.floor(s/86400), h = Math.floor((s%86400)/3600);
  return d + 'd ' + h + 'h away';
}
function dotClass(svc) {
  if (!svc) return 'dot-down';
  if (svc.status === 'ok') return 'dot-ok';
  if (svc.status === 'error') return 'dot-warn';
  return 'dot-down';
}
function svcLabel(svc) {
  if (!svc || svc.status === 'down') return 'unreachable';
  return svc.status === 'ok' ? (svc.ms != null ? svc.ms+'ms' : 'ok') : 'error';
}
async function loadPipeline() {
  try {
    const d = await (await fetch('/api/pipeline-status')).json();
    const sv = d.services || {};
    const lm = d.last_montage;
    const monthLabel = d.next_build_month || '';
    $('pipeline-inner').innerHTML = `
<div class="pip-section">
  <p class="pip-title">Services</p>
  <div class="svc-row"><span class="svc-dot ${dotClass(sv.psn_messenger)}"></span>
    <span class="svc-name">PSN Messenger</span><span class="svc-meta">${svcLabel(sv.psn_messenger)}</span></div>
  <div class="svc-row"><span class="svc-dot ${dotClass(sv.psn_montage)}"></span>
    <span class="svc-name">Montage Engine</span><span class="svc-meta">${svcLabel(sv.psn_montage)}</span></div>
  <div class="svc-row"><span class="svc-dot ${dotClass(sv.wa_bridge)}"></span>
    <span class="svc-name">WhatsApp Bridge</span><span class="svc-meta">${svcLabel(sv.wa_bridge)}</span></div>
</div>
<div class="pip-section">
  <p class="pip-title">This Month — ${monthLabel}</p>
  <div class="big-stat">
    <div class="bstat"><div class="bv">${d.clips_this_month ?? 0}</div><div class="bl">Clips Captured</div></div>
    <div class="bstat"><div class="bv">${fmtCountdown(d.next_build_ts)}</div><div class="bl">Until Build</div></div>
  </div>
  ${d.last_clip_at ? `<p class="last-clip-note">Last clip: <b>${fmtAgo(d.last_clip_at)}</b> from <b>${esc(d.last_clip_sender||'')}</b></p>` : '<p class="last-clip-note">No clips captured yet this month</p>'}
</div>
<div class="pip-section">
  <p class="pip-title">Next Auto-Build</p>
  <div class="pip-build">
    <div class="pb-label">Scheduled</div>
    <div class="pb-date">${d.next_build_label || '—'}</div>
    <div class="pb-countdown">${fmtCountdown(d.next_build_ts)} · auto-send to Goopers</div>
  </div>
</div>
${lm ? `<div class="pip-section">
  <p class="pip-title">Last Montage</p>
  <div class="last-montage">
    <div class="lm-row"><span class="lm-key">Version</span><span class="lm-val">v${lm.version} · ${lm.year}-${String(lm.month).padStart(2,'0')}</span></div>
    <div class="lm-row"><span class="lm-key">Clips</span><span class="lm-val">${lm.clips} included</span></div>
    <div class="lm-row"><span class="lm-key">Duration</span><span class="lm-val">${lm.duration}s</span></div>
    <div class="lm-row"><span class="lm-key">Sent to group</span><span class="lm-val"><span class="sent-badge ${lm.sent?'yes':'no'}">${lm.sent?'✓ Sent':'Not sent'}</span></span></div>
  </div>
</div>` : ''}
${(d.clips||[]).length ? `<div class="pip-section">
  <p class="pip-title">Clips This Month (${(d.clips||[]).length})</p>
  <div class="clip-list">
  ${(d.clips||[]).map(c => {
    const statusDot = c.included === true ? '<span class="cdot cdot-in">✓</span>'
      : c.included === false ? '<span class="cdot cdot-ex">✕</span>'
      : '<span class="cdot cdot-pend">·</span>';
    const reasonTxt = c.reason ? `<span class="creason">${esc(c.reason.replace('_',' '))}</span>` : '';
    return `<div class="clip-row">${statusDot}<span class="csender">${esc(c.sender)}</span><span class="cdur">${c.duration}s</span>${reasonTxt}<span class="cage">${fmtAgo(c.at)}</span></div>`;
  }).join('')}
  </div>
</div>` : '<p class="last-clip-note" style="margin-top:8px">No clips captured this month yet</p>'}`;
  } catch(e) {
    $('pipeline-inner').innerHTML = '<div class="card"><div class="empty">Could not load pipeline status.</div></div>';
  }
}
loadPipeline();
setInterval(loadPipeline, 30000);

function toggleUserMenu(){
  const m=$('userMenu'); if(!m) return; m.classList.toggle('open');
}
document.addEventListener('click', e => {
  const btn=$('userBtn'), menu=$('userMenu');
  if(btn && menu && !btn.contains(e.target) && !menu.contains(e.target))
    menu.classList.remove('open');
});

// ── Passkey registration ────────────────────────────────────────────────────
function _b64url(buf){
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
}
function _fromB64url(s){
  const pad='='.repeat((4-s.length%4)%4);
  return Uint8Array.from(atob((s+pad).replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0)).buffer;
}
async function _doRegisterPasskey(onSuccess) {
  if(!window.PublicKeyCredential) throw new Error('Passkeys not supported in this browser');
  const br = await fetch('/auth/passkey/register/begin',{method:'POST'});
  if(!br.ok) throw new Error((await br.json()).error || 'Failed to start');
  const {passkeyId, options} = await br.json();

  options.challenge = _fromB64url(options.challenge);
  options.user.id   = _fromB64url(options.user.id);
  if(options.excludeCredentials)
    options.excludeCredentials = options.excludeCredentials.map(c=>({...c,id:_fromB64url(c.id)}));

  const cred = await navigator.credentials.create({publicKey: options});
  const credential = {
    id: cred.id, rawId: _b64url(cred.rawId), type: cred.type,
    response: {
      clientDataJSON:   _b64url(cred.response.clientDataJSON),
      attestationObject:_b64url(cred.response.attestationObject),
    },
  };
  const passkeyName = (navigator.userAgentData?.platform || navigator.platform || 'Device') + ' passkey';
  const cr = await fetch('/auth/passkey/register/complete',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({passkeyId, credential, passkeyName}),
  });
  if(!cr.ok) throw new Error('Registration failed');
  if(onSuccess) onSuccess();
}

// Legacy shortcut (called from old spots if any)
async function registerPasskey(){
  $('userMenu')?.classList.remove('open');
  try { await _doRegisterPasskey(); toast('🔑 Passkey added!'); }
  catch(e){ if(e.name!=='NotAllowedError') toast('Error: '+e.message); }
}

// ── Settings modal ─────────────────────────────────────────────────────────
function openSettings(){
  $('userMenu')?.classList.remove('open');
  $('settingsOverlay').classList.add('open');
  loadPasskeys();
}
function closeSettings(){ $('settingsOverlay').classList.remove('open'); }

function switchTab(name){
  document.querySelectorAll('.stab').forEach((t,i)=>{
    const names=['passkeys','security','psn'];
    t.classList.toggle('active', names[i]===name);
  });
  document.querySelectorAll('.spanel').forEach(p=>{
    p.classList.toggle('active', p.id==='tab-'+name);
  });
  if(name==='psn') loadPsnStatus();
}

async function loadPsnStatus(){
  const el=$('psnStatus');
  if(!el) return;
  el.innerHTML='<span style="color:var(--dim)">Loading…</span>';
  try {
    const r = await fetch('/auth/settings/psn');
    const d = await r.json();

    // ── My account (everyone including admin, once claimed) ───────────
    if(d.linked){
      const linked = d.linked_at ? new Date(d.linked_at*1000).toLocaleDateString() : null;
      const expiry = d.refresh_expires_at ? new Date(d.refresh_expires_at*1000) : null;
      const expired = expiry && expiry < new Date();
      let html =
        `<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <span style="font-size:28px">🎮</span>
          <div>
            <div style="color:#fff;font-weight:700;font-size:16px">${d.online_id||'—'}</div>
            ${linked?`<div style="font-size:11px;color:var(--dim)">Linked ${linked}</div>`:''}
          </div>
          <span style="margin-left:auto;font-size:12px;padding:3px 9px;border-radius:20px;font-weight:700;
            background:${expired?'rgba(255,60,60,.15)':'rgba(0,220,120,.12)'};
            color:${expired?'#ff6060':'#00dc78'};
            border:1px solid ${expired?'rgba(255,60,60,.3)':'rgba(0,220,120,.3)'}">
            ${expired?'Expired':'Active'}
          </span>
        </div>
        ${expired?'<div style="font-size:12.5px;color:#ff9060;margin-bottom:10px">⚠️ Token expired — re-link to refresh.</div>':''}`;
      // Admin: also show all accounts below
      if(d.admin && d.users && d.users.length){
        html += _adminUsersHtml(d.users);
      }
      el.innerHTML = html;
      return;
    }

    // ── Not yet claimed — show unclaimed list ─────────────────────────
    const unclaimed = d.unclaimed || [];
    let html = '';
    if(unclaimed.length){
      html += `<p style="font-size:12.5px;color:var(--dim);margin:0 0 12px">Is one of these yours? Tap to claim it.</p>`;
      html += unclaimed.map(u => {
        const dt = u.linked_at ? new Date(u.linked_at*1000).toLocaleDateString() : '';
        return `<div class="pk-row" style="margin-bottom:8px">
          <div class="pk-info">
            <span class="pk-name">${u.online_id||u.mm_username||'Unknown'}</span>
            ${dt?`<span class="pk-date">Linked ${dt}</span>`:''}
          </div>
          <button class="smodal-btn" style="width:auto;margin:0;padding:6px 13px;font-size:12px"
            onclick="claimPsn('${u.key}','${u.online_id||u.mm_username||''}')">This is mine</button>
        </div>`;
      }).join('');
    } else {
      html = '<span style="color:var(--dim);font-size:13.5px">No unassigned accounts found.<br>Use the button below to link a new one.</span>';
    }
    // Admin: also show full list below the claim section
    if(d.admin && d.users && d.users.length){
      html += _adminUsersHtml(d.users);
    }
    el.innerHTML = html;
  } catch(e){ el.innerHTML='<span style="color:#ff7070">Could not load PSN status.</span>'; }
}

function _adminUsersHtml(users){
  if(!users.length) return '';
  return `<div style="margin-top:18px;padding-top:14px;border-top:1px solid rgba(255,255,255,.07)">
    <p style="font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin:0 0 10px">All accounts</p>` +
    users.map(u => {
      const expiry = u.refresh_expires_at ? new Date(u.refresh_expires_at*1000) : null;
      const expired = expiry && expiry < new Date();
      const claimed = !!u.zitadel_user_id;
      const dt = u.linked_at ? new Date(u.linked_at*1000).toLocaleDateString() : '';
      return `<div class="pk-row" style="margin-bottom:8px">
        <div class="pk-info">
          <span class="pk-name">${u.online_id||u.mm_username||'Unknown'}</span>
          <span class="pk-date">${dt?'Linked '+dt:''}${claimed?' · claimed':' · unclaimed'}</span>
        </div>
        <span style="font-size:11px;padding:3px 8px;border-radius:20px;font-weight:700;white-space:nowrap;
          background:${expired?'rgba(255,60,60,.15)':'rgba(0,220,120,.12)'};
          color:${expired?'#ff6060':'#00dc78'};
          border:1px solid ${expired?'rgba(255,60,60,.3)':'rgba(0,220,120,.3)'}">
          ${expired?'Expired':'Active'}
        </span>
      </div>`;
    }).join('') + `</div>`;
}

async function claimPsn(key, name){
  if(!confirm('Claim "'+name+'" as your PlayStation account?')) return;
  const r = await fetch('/auth/settings/psn/claim',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key}),
  });
  if(r.ok){ loadPsnStatus(); }
  else { alert('Could not claim — it may already be assigned.'); }
}

function _pkMsg(msg, type){ const el=$('pkMsg'); el.className='smsg '+type; el.textContent=msg; }
function _pwMsg(msg, type){ const el=$('pwMsg'); el.className='smsg '+type; el.textContent=msg; }

async function loadPasskeys(){
  const el=$('pkList'); el.innerHTML='<div class="pk-empty">Loading…</div>';
  try {
    const r = await fetch('/auth/settings/passkeys');
    if(!r.ok){ el.innerHTML='<div class="pk-empty">Could not load passkeys.</div>'; return; }
    const {passkeys} = await r.json();
    if(!passkeys || !passkeys.length){ el.innerHTML='<div class="pk-empty">No passkeys yet.</div>'; return; }
    el.innerHTML = passkeys.map(pk => {
      const dt = pk.changeDate ? new Date(pk.changeDate).toLocaleDateString() : '';
      return `<div class="pk-row">
        <div class="pk-info">
          <span class="pk-name">${pk.name||'Passkey'}</span>
          ${dt?`<span class="pk-date">Added ${dt}</span>`:''}
        </div>
        <button class="pk-del" onclick="deletePasskey('${pk.id}')">Remove</button>
      </div>`;
    }).join('');
  } catch(e){ el.innerHTML='<div class="pk-empty">Error loading passkeys.</div>'; }
}

async function deletePasskey(id){
  if(!confirm('Remove this passkey?')) return;
  const r = await fetch('/auth/settings/passkeys/'+id, {method:'DELETE'});
  if(r.ok){ _pkMsg('Passkey removed.','ok'); loadPasskeys(); }
  else { _pkMsg('Could not remove passkey.','err'); }
}

async function addPasskeyFromSettings(){
  _pkMsg('','');
  try {
    await _doRegisterPasskey(()=>{ _pkMsg('Passkey added!','ok'); loadPasskeys(); });
  } catch(e){
    if(e.name!=='NotAllowedError') _pkMsg('Error: '+e.message,'err');
  }
}

async function changePassword(){
  _pwMsg('','');
  const cur=$('pwCur').value, nw=$('pwNew').value, conf=$('pwConf').value;
  if(!cur||!nw){ _pwMsg('Fill in all fields.','err'); return; }
  if(nw!==conf){ _pwMsg('New passwords do not match.','err'); return; }
  if(nw.length<8){ _pwMsg('Password must be at least 8 characters.','err'); return; }
  const r = await fetch('/auth/settings/password',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({currentPassword:cur, newPassword:nw}),
  });
  const d = await r.json();
  if(r.ok){ _pwMsg('Password updated!','ok'); $('pwCur').value=''; $('pwNew').value=''; $('pwConf').value=''; }
  else { _pwMsg(d.error||'Failed to update password.','err'); }
}
</script>
</body></html>"""

import os
import json
import logging
from fastapi import FastAPI, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from psnawp_api import PSNAWP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NPSSO_TOKEN = os.environ.get("NPSSO_TOKEN")
GROUP_ID = os.environ.get("GROUP_ID")
# Auto-Squad: a separate PSN group (everyone except wolfie/IG_Juicy) that the
# "Squad Up" Stream Deck button rallies. Created 2026-08-15; overridable via env.
SQUAD_GROUP_ID = os.environ.get("SQUAD_GROUP_ID", "213250d833ccce334b651e2ee15e365c97468e02-869")
# Self-service linking portal: shared passcode friends type before the form
# shows (keeps randos who find the URL out). Set PORTAL_PASSCODE in the env.
PORTAL_PASSCODE = os.environ.get("PORTAL_PASSCODE", "")

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


def _rate_limit(key: str) -> None:
    """Raise HTTP 429 if `key` exceeded its window. Sliding-window counter."""
    limit = _RL_LIMITS.get(key)
    if not limit:
        return
    max_calls, window = limit
    now = _time.time()
    with _rl_lock:
        hits = [t for t in _rl_hits.get(key, []) if now - t < window]
        if len(hits) >= max_calls:
            retry = round(window - (now - hits[0]), 1)
            raise HTTPException(
                status_code=429,
                detail=f"Slow down — try again in {retry}s.",
                headers={"Retry-After": str(int(retry) + 1)},
            )
        hits.append(now)
        _rl_hits[key] = hits


class MessageRequest(BaseModel):
    message: str


@app.get("/health")
def health():
    return {"status": "ok", "user": client.online_id, "group": GROUP_ID}


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
    psn_messenger = PSNMessenger(psn_auth, GROUP_ID)
    logger.info("v2: PSN auth initialized with token persistence")
    _v2_available = True
except Exception as e:
    logger.warning(f"v2: PSN auth failed to initialize: {e}")
    _v2_available = False


@app.get("/v2/health")
def v2_health():
    if not _v2_available:
        raise HTTPException(status_code=503, detail="v2 auth not initialized")
    return {"status": "ok", "version": "v2", "group": GROUP_ID}


@app.post("/v2/send")
def v2_send_message(req: MessageRequest):
    _rate_limit("psn_send")
    if not _v2_available:
        raise HTTPException(status_code=503, detail="v2 auth not initialized")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    try:
        success = psn_messenger.send_message(req.message.strip())
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


# === Auto-Squad: rally the squad group (everyone except wolfie/IG_Juicy) ===

# A separate messenger bound to the squad group, reusing the same auth.
try:
    _squad_messenger = PSNMessenger(psn_auth, SQUAD_GROUP_ID) if _v2_available else None
except Exception as e:  # noqa: BLE001
    logger.warning(f"squad: messenger init failed: {e}")
    _squad_messenger = None


class SquadRequest(BaseModel):
    message: str | None = None


@app.post("/v2/squad")
def v2_squad(req: SquadRequest | None = None):
    """Post a 'squad up' rally message to the dedicated squad group."""
    _rate_limit("psn_send")
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
async def roast_once():
    """Send one roast immediately."""
    _rate_limit("roast")
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


def _gate_page(error: str = "") -> str:
    """Full-page passcode lock. Nothing else is shown until it's entered."""
    err = (
        f'<div class="err">⚠️ {error}</div>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Enter passcode</title>
<style>
  :root {{ color-scheme:dark; }}
  * {{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }}
  html,body {{ margin:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:#eaf0ff; min-height:100dvh; display:flex; align-items:center;
    justify-content:center; padding:24px; background:#070b18; position:relative;
    overflow:hidden; }}
  body::before, body::after {{ content:""; position:fixed; inset:-30% -10%; z-index:-1;
    background:
      radial-gradient(45% 45% at 22% 20%, rgba(0,112,209,.4), transparent 60%),
      radial-gradient(42% 42% at 80% 24%, rgba(124,92,255,.38), transparent 60%),
      radial-gradient(50% 45% at 55% 92%, rgba(0,163,255,.3), transparent 62%);
    filter:blur(30px); animation:drift 18s ease-in-out infinite alternate; }}
  body::after {{ animation-duration:26s; animation-direction:alternate-reverse; opacity:.65; }}
  @keyframes drift {{ from {{ transform:translate3d(-3%,-2%,0) scale(1); }}
    to {{ transform:translate3d(4%,3%,0) scale(1.12); }} }}
  .lock {{ width:100%; max-width:400px; text-align:center;
    background:rgba(23,31,54,.72); border:1px solid rgba(120,140,190,.2);
    border-radius:26px; padding:38px 30px 30px;
    box-shadow:0 30px 80px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.06);
    backdrop-filter:blur(22px) saturate(140%); -webkit-backdrop-filter:blur(22px) saturate(140%);
    animation:rise .55s cubic-bezier(.2,.8,.2,1) both; }}
  @keyframes rise {{ from {{ opacity:0; transform:translateY(18px) scale(.97); }} }}
  .badge {{ width:74px; height:74px; margin:0 auto 18px; border-radius:22px;
    display:grid; place-items:center; font-size:34px;
    background:linear-gradient(135deg,#0070d1,#7c5cff);
    box-shadow:0 12px 34px rgba(0,112,209,.5); animation:pop .5s cubic-bezier(.2,1.4,.4,1) both; }}
  @keyframes pop {{ from {{ transform:scale(.4); opacity:0; }} }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  p {{ color:#9fb0d4; font-size:13.5px; line-height:1.55; margin:0 0 22px; }}
  input {{ width:100%; padding:15px; border-radius:14px; text-align:center;
    border:1px solid rgba(140,160,255,.25); background:rgba(6,11,24,.6);
    color:#eaf0ff; font-size:19px; letter-spacing:4px; -webkit-appearance:none;
    transition:border .15s, box-shadow .15s; }}
  input:focus {{ outline:none; border-color:#00a3ff; box-shadow:0 0 0 3px rgba(0,163,255,.25); }}
  button {{ width:100%; margin-top:14px; padding:15px; border:none; border-radius:14px;
    font-size:16px; font-weight:700; cursor:pointer; color:#fff;
    background:linear-gradient(135deg,#0070d1,#00a3ff);
    box-shadow:0 12px 30px rgba(0,112,209,.45); transition:transform .07s, box-shadow .15s; }}
  button:active {{ transform:scale(.975); }}
  button:hover {{ box-shadow:0 14px 36px rgba(0,112,209,.6); }}
  .err {{ background:rgba(255,107,139,.12); border:1px solid rgba(255,107,139,.4);
    color:#ffc0cd; padding:11px; border-radius:12px; font-size:13px; margin-bottom:16px; }}
</style></head>
<body>
  <form class="lock" method="post" action="/portal/unlock">
    <div class="badge">🔒</div>
    <h1>Squad access</h1>
    <p>This page is protected. Enter the passcode the host gave you to continue.</p>
    {err}
    <input name="passcode" type="text" inputmode="text" autocomplete="off"
      autofocus placeholder="passcode" aria-label="Passcode">
    <button type="submit">Unlock →</button>
  </form>
</body></html>"""


def _portal_page(error: str = "", ok: str = "") -> str:
    """Render the single-page portal wizard (only reached once unlocked)."""
    from portal import NPSSO_TOKEN_URL, PSN_LOGIN_URL, mattermost_usernames

    # On success we replace the whole wizard with a celebration screen.
    if ok:
        return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
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


# --- Passcode gate (whole page is locked until the code is entered once) ---
#
# We set a cookie holding a hash of the passcode after a correct unlock. Every
# portal view checks it; the form/link routes 403 without it. One unlock per
# device, not per submit.

import hashlib

_GATE_COOKIE = "portal_gate"


def _gate_value() -> str:
    """Opaque cookie value proving the passcode was entered (not the code itself)."""
    return hashlib.sha256(f"psn-portal::{PORTAL_PASSCODE}".encode()).hexdigest()


def _is_unlocked(request) -> bool:
    if not PORTAL_PASSCODE:
        return True
    return request.cookies.get(_GATE_COOKIE) == _gate_value()


def _set_gate_cookie(resp) -> None:
    # 30-day gate; httponly so page JS can't read it, samesite lax for the redirect.
    resp.set_cookie(
        _GATE_COOKIE, _gate_value(), max_age=2592000,
        httponly=True, samesite="lax", secure=True, path="/",
    )


# --- Site-wide passcode enforcement -----------------------------------------
#
# Every public request must be unlocked. We reuse the same cookie gate above and
# apply it to ALL routes via one middleware, so the dashboard, /api/squad, etc.
# are no longer world-readable. Two carve-outs:
#   * OPEN_PATHS: the unlock form + container healthcheck must work pre-unlock.
#   * Direct LAN/Tailscale IP access (the Stream Deck buttons) bypasses the gate
#     -- those hosts are already network-restricted and carry no cookie.
_OPEN_PATHS = {"/portal/unlock", "/health"}

# The only public hostname; anything else (bare IPs from the tailnet/LAN) is a
# trusted direct hit and skips the gate.
_PUBLIC_HOST = os.environ.get("PORTAL_PUBLIC_HOST", "psn.qureshi.io")


@app.middleware("http")
async def _passcode_gate(request: Request, call_next):
    if not PORTAL_PASSCODE:
        return await call_next(request)

    path = request.url.path
    if path in _OPEN_PATHS or _is_unlocked(request):
        return await call_next(request)

    # Requests that don't arrive on the public hostname are direct IP hits from
    # the tailnet/LAN (e.g. Stream Deck) -- let them through.
    host = (request.headers.get("host") or "").split(":")[0]
    if host != _PUBLIC_HOST:
        return await call_next(request)

    # Locked: show the gate for browser navigations, 403 for anything else.
    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        return HTMLResponse(_gate_page(), status_code=401)
    return JSONResponse({"detail": "passcode required"}, status_code=401)


@app.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request):
    if not _is_unlocked(request):
        return HTMLResponse(_gate_page())
    return HTMLResponse(_portal_page())


@app.post("/portal/unlock", response_class=HTMLResponse)
def portal_unlock(passcode: str = Form("")):
    if PORTAL_PASSCODE and passcode.strip() != PORTAL_PASSCODE:
        return HTMLResponse(
            _gate_page(error="That code isn't right — ask the host."),
            status_code=403,
        )
    # Correct (or no passcode configured): set cookie and send to the wizard.
    resp = RedirectResponse(url="/portal", status_code=303)
    _set_gate_cookie(resp)
    return resp


@app.post("/portal/link", response_class=HTMLResponse)
def portal_link(
    request: Request,
    npsso: str = Form(...),
    mm_username: str = Form(""),
):
    if not _is_unlocked(request):
        return HTMLResponse(_gate_page(error="Enter the passcode first."), status_code=403)
    try:
        result = portal_mod.link_user(npsso, mm_username=mm_username.strip())
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
def portal_users(key: str = ""):
    """Admin: list linked users (no secrets). Gated by the same passcode."""
    if PORTAL_PASSCODE and key != PORTAL_PASSCODE:
        raise HTTPException(status_code=403, detail="forbidden")
    return {"users": portal_mod.list_users()}


# === Squad Dashboard (psn.qureshi.io home) ===
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
# Single-shot edge flag: True while at least one person is on ARC. We alert once
# on the 0->1 transition and don't alert again until it drops back to 0.
_arc_alerted = False


def _check_arc_alert(squad: list[dict]) -> None:
    """Post ONE tagging message to Squad Alerts when ARC play starts."""
    global _arc_alerted
    if not mm_client.available():
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
        # Nobody on ARC anymore -> re-arm for the next session.
        _arc_alerted = False


@app.on_event("startup")
async def _start_squad_poller():
    global _poller_started
    if _poller_started or not _v2_available:
        return
    _poller_started = True

    import asyncio

    async def _loop():
        while True:
            try:
                # Force a real refresh by bypassing the freshness check: reset
                # the cache timestamp so squad_status re-sweeps, then call it.
                psn_data._cache["at"] = 0.0
                squad = await asyncio.to_thread(psn_data.squad_status, psn_auth)
                await asyncio.to_thread(_check_arc_alert, squad)
            except Exception as e:  # noqa: BLE001
                logger.debug("squad poller tick failed: %s", e)
            await asyncio.sleep(60)

    asyncio.create_task(_loop())
    logger.info("squad presence poller started (60s)")


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
def api_add_button(req: CustomButtonRequest):
    """Turn a user's line into an AI-flavored permanent soundboard button.

    Uses the same Bedrock model as the roast bot to punch up the text, saves it
    to /data/soundboard.json, and (optionally) sends it to the group right away.
    """
    _rate_limit("custom_add")
    if req.send:
        _rate_limit("psn_send")
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
def dashboard():
    return HTMLResponse(_dashboard_html())




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


def _dashboard_html() -> str:
    return _DASHBOARD_TMPL.replace("__SOUNDBOARD__", _soundboard_json())


_DASHBOARD_TMPL = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>The Squad · PSN</title>
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
    padding:5px; border-radius:14px; margin:14px 0; backdrop-filter:blur(18px);
    -webkit-backdrop-filter:blur(18px); }
  .tab { flex:1; text-align:center; padding:10px; border-radius:10px; font-size:13px;
    font-weight:700; color:var(--dim); cursor:pointer; border:none; background:none;
    font-family:"Rajdhani",sans-serif; letter-spacing:.5px; text-transform:uppercase;
    transition:background .15s,color .15s,box-shadow .15s; }
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
  .on .av { border-color:rgba(140,255,43,.6); box-shadow:0 0 14px rgba(140,255,43,.4); }
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
  .on .dot { background:var(--lime); box-shadow:0 0 10px var(--lime); }
  .on .name { color:#eaffd4; }
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
</style></head>
<body><div class="wrap">
  <div class="top">
    <div class="logo">🎮</div>
    <div><h1>The Squad</h1><p class="tag">Tap to blast the group · live PSN status</p></div>
    <div class="live" id="livecount"></div>
  </div>

  <div class="tabs">
    <button class="tab on" data-p="squad" onclick="tab(this)">Squad</button>
    <button class="tab" data-p="lb" onclick="tab(this)">🏆 Leaderboard</button>
  </div>
  <div id="together"></div>
  <div class="statgrid" id="statgrid"></div>
  <div class="panel on" id="p-squad"><div class="card" id="squad"><div class="spin">Loading squad…</div></div></div>
  <div class="panel" id="p-lb"><div class="card" id="lb"><div class="spin">Loading leaderboard…</div></div></div>
  <p style="text-align:center;margin:16px 0"><a class="link-cta" href="/portal">＋ Link your PlayStation account</a></p>
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
    const r = await fetch('/v2/squad',{method:'POST',headers:{'Content-Type':'application/json'},
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
    let st;
    if(m.playing) st='<span class="game-badge">Playing <b>'+esc(m.game)+'</b></span>';
    else if(m.online) st='Online'+(m.platform?' · '+esc(m.platform):'');
    // Offline: just show their last game (no timestamps / "last online").
    else if(m.recent_game) st='<span class="lastgame">'+esc(m.recent_game)+'</span>';
    else st='<span class="lastgame">—</span>';
    const mm=m.mm_username?'<span class="mm">@'+esc(m.mm_username)+'</span>':'';
    const cls=m.playing?'on playing':(m.online?'on':'');
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
    const playing=squad.filter(m=>m.playing).length, online=squad.filter(m=>m.online).length;
    $('livecount').innerHTML = squad.length ? ('<b>'+online+'</b> online'+(playing?' · '+playing+' 🎮':'')) : '';
    renderTogether(); renderStats(); renderSquad(); renderBoard();
  } catch(e){ $('squad').innerHTML='<div class="empty">Couldn\'t load squad.</div>'; }
}
loadSquad(); setInterval(loadSquad, 30000);
</script>
</body></html>"""

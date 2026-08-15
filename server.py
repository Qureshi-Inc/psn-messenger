import os
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
    roast_bot.start()
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


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(_dashboard_html())




# The dashboard's fun "soundboard" buttons. Each posts a canned message to the
# squad PSN group via /v2/squad {message}. Kept in one list so it's easy to add
# more. `cls` picks a color; `msg` None means it hits a non-message action path.
_SOUNDBOARD = [
    {"label": "👉👌 Have you ever?", "msg": "Have you ever? 👉👌", "cls": "c1"},
    {"label": "🧊☕ Iced Cap STORY", "msg": "🧊☕ Iced Cap STORRYYY! 📖✨", "cls": "c2"},
    {"label": "🙅‍♂️ Never", "msg": "Never 🙅‍♂️❌", "cls": "c3"},
    {"label": "💧 Water Break", "msg": "💧 Water break! 🚰💦", "cls": "c4"},
    {"label": "🎬 Zubi Clip It", "msg": "🎬 ZUBI CLIP IT!! 📸🔥 That was insane!", "cls": "c5"},
    {"label": "🎮 Squad Up", "msg": "🎮🔥 SQUAD UP! Who's hopping on? 🕹️💥", "cls": "c1"},
    {"label": "🕹️ Game Time", "msg": "🎮🔥 Let's party up y'all. It's GAME TIME! 🕹️💥", "cls": "c2"},
    {"label": "😴 Wake Up", "msg": "😴⏰ WAKE UP! The squad needs you! 🎮", "cls": "c4"},
    {"label": "🏆 Clutch!", "msg": "🏆🔥 CLUTCH!! What a play! 👏👏", "cls": "c5"},
    {"label": "💀 GG EZ", "msg": "💀 GG EZ 😎🎮", "cls": "c3"},
    {"label": "🍕 Food Break", "msg": "🍕 Food break y'all — brb 🤤", "cls": "c4"},
    {"label": "🔥 Roast Now", "path": "/roast/once", "cls": "roast"},
]


def _soundboard_json() -> str:
    import json as _json
    return _json.dumps(_SOUNDBOARD)


def _dashboard_html() -> str:
    return _DASHBOARD_TMPL.replace("__SOUNDBOARD__", _soundboard_json())


_DASHBOARD_TMPL = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>The Squad · PSN</title>
<style>
  :root { color-scheme:dark;
    --bg:#070b18; --card:rgba(20,27,49,.72); --line:rgba(120,140,190,.16);
    --txt:#eaf0ff; --dim:#9fb0d4; --psn:#0070d1; --psn2:#00a3ff; --accent:#7c5cff;
    --ok:#2ee6a0; --gold:#ffd24a; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html,body { margin:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:var(--txt); min-height:100dvh; background:var(--bg);
    padding:0 0 40px; position:relative; overflow-x:hidden; }
  body::before,body::after { content:""; position:fixed; inset:-30% -10%; z-index:-2;
    background:
      radial-gradient(42% 42% at 20% 14%, rgba(0,112,209,.4), transparent 60%),
      radial-gradient(40% 40% at 82% 20%, rgba(124,92,255,.34), transparent 60%),
      radial-gradient(48% 42% at 55% 94%, rgba(0,163,255,.26), transparent 62%);
    filter:blur(32px); animation:drift 22s ease-in-out infinite alternate; }
  body::after { animation-duration:30s; animation-direction:alternate-reverse; opacity:.6; }
  @keyframes drift { to { transform:translate3d(4%,3%,0) scale(1.12); } }
  .wrap { max-width:760px; margin:0 auto; padding:0 14px; }

  .top { display:flex; align-items:center; gap:12px; padding:18px 2px 12px; }
  .logo { width:44px; height:44px; border-radius:14px; flex:none; display:grid;
    place-items:center; font-size:24px; background:linear-gradient(135deg,var(--psn),var(--accent));
    box-shadow:0 10px 28px rgba(0,112,209,.5); }
  h1 { font-size:20px; margin:0; letter-spacing:.2px; }
  .tag { color:var(--dim); font-size:12px; margin:2px 0 0; }
  .top .live { margin-left:auto; text-align:right; font-size:12px; color:var(--dim); }
  .top .live b { color:var(--ok); font-size:15px; }

  /* STICKY SOUNDBOARD -- the hero. Always at top, compact, tappable. */
  .board-wrap { position:sticky; top:0; z-index:20; padding:8px 0 12px;
    background:linear-gradient(180deg, rgba(7,11,24,.94) 60%, rgba(7,11,24,0));
    backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px); }
  .board-title { font-size:11px; letter-spacing:1.5px; color:var(--dim);
    text-transform:uppercase; margin:0 4px 8px; font-weight:700; }
  .board { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
  .snd { border:none; border-radius:14px; padding:14px 8px; font-size:12.5px;
    font-weight:700; cursor:pointer; color:#fff; line-height:1.25; min-height:58px;
    display:flex; align-items:center; justify-content:center; text-align:center;
    box-shadow:0 6px 18px rgba(0,0,0,.35); transition:transform .07s, filter .12s, box-shadow .12s; }
  .snd:active { transform:scale(.94); }
  .snd:hover { filter:brightness(1.12); }
  .snd.flash { animation:flash .5s ease; }
  @keyframes flash { 0%{ box-shadow:0 0 0 0 rgba(46,230,160,.7);} 100%{ box-shadow:0 0 0 14px rgba(46,230,160,0);} }
  .c1 { background:linear-gradient(135deg,#0070d1,#00a3ff); }
  .c2 { background:linear-gradient(135deg,#7c5cff,#a06bff); }
  .c3 { background:linear-gradient(135deg,#e0533a,#c0392b); }
  .c4 { background:linear-gradient(135deg,#12b866,#0aa0a0); }
  .c5 { background:linear-gradient(135deg,#ff9d3a,#ffd24a); color:#3a2400; }
  .roast { background:linear-gradient(135deg,#e0533a,#ff6f3a); }

  .tabs { display:flex; gap:6px; background:var(--card); border:1px solid var(--line);
    padding:5px; border-radius:15px; margin:14px 0; backdrop-filter:blur(18px);
    -webkit-backdrop-filter:blur(18px); }
  .tab { flex:1; text-align:center; padding:10px; border-radius:11px; font-size:13.5px;
    font-weight:700; color:var(--dim); cursor:pointer; border:none; background:none;
    transition:background .15s,color .15s; }
  .tab.on { background:linear-gradient(135deg,var(--psn),var(--accent)); color:#fff; }
  .panel { display:none; } .panel.on { display:block; animation:fade .3s ease both; }
  @keyframes fade { from { opacity:0; transform:translateY(6px); } }

  .card { background:var(--card); border:1px solid var(--line); border-radius:18px;
    padding:8px 16px; backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); }
  .row { display:flex; align-items:center; gap:13px; padding:13px 4px;
    border-bottom:1px solid rgba(255,255,255,.05); }
  .row:last-child { border-bottom:none; }
  .avwrap { position:relative; flex:none; }
  .av { width:50px; height:50px; border-radius:13px; background:#22304f; object-fit:cover; display:block; }
  .gicon { position:absolute; right:-6px; bottom:-6px; width:26px; height:26px;
    border-radius:8px; object-fit:cover; border:2px solid #0e1424; box-shadow:0 2px 8px rgba(0,0,0,.6); }
  .who { flex:1; min-width:0; }
  .name { font-weight:700; font-size:15px; display:flex; align-items:center; }
  .mm { color:#6f7ea3; font-size:12px; font-weight:500; margin-left:6px; }
  .state { font-size:12.5px; color:var(--dim); margin-top:3px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dot { width:9px; height:9px; border-radius:50%; margin-right:7px; flex:none;
    background:#4a5677; display:inline-block; }
  .on .dot { background:var(--ok); box-shadow:0 0 8px var(--ok); }
  .playing .dot { background:var(--ok); box-shadow:0 0 11px var(--ok); animation:pulse 1.8s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow:0 0 3px var(--ok); opacity:.6; } }
  .game-badge { color:#9dffd6; font-weight:600; } .game-badge b { color:#eafff2; }
  .troph { display:flex; gap:9px; align-items:center; flex:none; }
  .lvl { text-align:center; }
  .lvl .v { font-size:17px; font-weight:800; line-height:1;
    background:linear-gradient(135deg,var(--gold),#ff9d3a);
    -webkit-background-clip:text; background-clip:text; color:transparent; }
  .lvl .k { font-size:9px; color:var(--dim); letter-spacing:.5px; }
  .tcount { font-size:11.5px; color:var(--dim); text-align:right; line-height:1.5; }
  .tcount .p { color:#6fd0ff; font-weight:700; }

  .lb-row { display:flex; align-items:center; gap:13px; padding:13px 4px;
    border-bottom:1px solid rgba(255,255,255,.05); }
  .lb-row:last-child { border-bottom:none; }
  .rank { width:30px; text-align:center; font-size:17px; font-weight:800; color:var(--dim); flex:none; }
  .bar { height:7px; border-radius:99px; background:rgba(255,255,255,.08); margin-top:6px; overflow:hidden; }
  .bar > i { display:block; height:100%; background:linear-gradient(90deg,var(--psn2),var(--accent)); }

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

  <div class="board-wrap">
    <div class="board-title">🔊 Soundboard — tap to send</div>
    <div class="board" id="board"></div>
  </div>

  <div class="tabs">
    <button class="tab on" data-p="squad" onclick="tab(this)">Squad</button>
    <button class="tab" data-p="lb" onclick="tab(this)">🏆 Leaderboard</button>
  </div>
  <div class="panel on" id="p-squad"><div class="card" id="squad"><div class="spin">Loading squad…</div></div></div>
  <div class="panel" id="p-lb"><div class="card" id="lb"><div class="spin">Loading leaderboard…</div></div></div>
  <p style="text-align:center;margin-top:16px"><a class="link-cta" href="/portal">＋ Link your PlayStation account</a></p>
</div>
<div class="toast" id="toast"></div>
<script>
const SOUNDBOARD = __SOUNDBOARD__;
const $ = id => document.getElementById(id);
const esc = s => (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const toast = m => { const t=$('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2000); };

// Build the soundboard
$('board').innerHTML = SOUNDBOARD.map((b,i)=>
  '<button class="snd '+(b.cls||'c1')+'" data-i="'+i+'" onclick="fire(this)">'+esc(b.label)+'</button>'
).join('');
async function fire(el){
  const b = SOUNDBOARD[el.dataset.i];
  el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),500);
  try {
    let r;
    if(b.path){ r = await fetch(b.path,{method:'POST'}); }
    else { r = await fetch('/v2/squad',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:b.msg})}); }
    toast(r.ok ? 'Sent! 🎮' : 'Failed ('+r.status+')');
  } catch(e){ toast('Network error'); }
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
    const gi=(m.playing&&m.game_icon)?'<img class="gicon" src="'+esc(m.game_icon)+'">':'';
    const avImg='<div class="avwrap">'+(av?'<img class="av" src="'+esc(av)+'">':'<div class="av"></div>')+gi+'</div>';
    let st;
    if(m.playing) st='<span class="game-badge">Playing <b>'+esc(m.game)+'</b></span>';
    else if(m.online) st='Online'+(m.platform?' · '+esc(m.platform):'');
    else if(m.recent_game) st='Last played '+esc(m.recent_game)+' · '+fmtLast(m.last_online);
    else st='Last online '+fmtLast(m.last_online);
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
async function loadSquad(){
  try {
    const {squad=[]}=await (await fetch('/api/squad')).json();
    SQUAD=squad;
    const playing=squad.filter(m=>m.playing).length, online=squad.filter(m=>m.online).length;
    $('livecount').innerHTML = squad.length ? ('<b>'+online+'</b> online'+(playing?' · '+playing+' 🎮':'')) : '';
    renderSquad(); renderBoard();
  } catch(e){ $('squad').innerHTML='<div class="empty">Couldn\'t load squad.</div>'; }
}
loadSquad(); setInterval(loadSquad, 30000);
</script>
</body></html>"""

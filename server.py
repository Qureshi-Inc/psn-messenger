import os
import logging
from fastapi import FastAPI, HTTPException, Form
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


def _portal_page(error: str = "", ok: str = "") -> str:
    """Render the single-page portal. Passcode gate is enforced server-side on
    submit; the gate field simply travels with the form."""
    from portal import NPSSO_TOKEN_URL, PSN_LOGIN_URL, mattermost_usernames

    banner = ""
    if ok:
        banner = f'<div class="msg ok">✅ {ok}</div>'
    elif error:
        banner = f'<div class="msg err">⚠️ {error}</div>'
    passcode_field = (
        '<label>Passcode<input name="passcode" type="password" '
        'autocomplete="off" placeholder="ask the host" required></label>'
        if PORTAL_PASSCODE
        else ""
    )
    # "Who are you?" dropdown of Mattermost users, so each link ties to a person
    # (like the Apple Music re-link page). Falls back to a text field if the
    # user list can't be fetched.
    names = mattermost_usernames()
    if names:
        opts = '<option value="" disabled selected>Select your name…</option>' + "".join(
            f'<option value="{n}">{n}</option>' for n in names
        )
        who_field = (
            '<label>Who are you? (Mattermost)</label>'
            f'<select name="mm_username" required>{opts}</select>'
        )
    else:
        who_field = (
            '<label>Who are you? (Mattermost username)</label>'
            '<input name="mm_username" placeholder="your mattermost username" required>'
        )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link your PlayStation</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
    background:#0b1020; color:#e9edf5; display:flex; min-height:100vh;
    align-items:center; justify-content:center; padding:20px; }}
  .card {{ width:100%; max-width:440px; background:#141b31; border:1px solid #26324f;
    border-radius:16px; padding:26px; box-shadow:0 12px 40px rgba(0,0,0,.4); }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  p.sub {{ margin:0 0 20px; color:#9aa7c4; font-size:13px; line-height:1.5; }}
  ol {{ padding-left:18px; margin:0 0 18px; color:#c3ccdf; font-size:13px; line-height:1.7; }}
  a.btn, button {{ display:block; width:100%; text-align:center; text-decoration:none;
    padding:13px; border-radius:11px; font-size:15px; font-weight:600; border:none;
    cursor:pointer; margin-bottom:10px; }}
  a.psn {{ background:#0070d1; color:#fff; }}
  a.token {{ background:#1c2947; color:#cfe0ff; border:1px solid #2f4570; }}
  label {{ display:block; font-size:12px; color:#9aa7c4; margin:14px 0 6px; }}
  input, textarea, select {{ width:100%; padding:11px; border-radius:10px; border:1px solid #2c3a5c;
    background:#0e1526; color:#e9edf5; font-size:14px; }}
  textarea {{ min-height:70px; resize:vertical; font-family:ui-monospace,monospace; }}
  button.link {{ background:#22c55e; color:#04210f; margin-top:14px; }}
  button.paste {{ background:#2b3a5e; color:#cfe0ff; }}
  .msg {{ padding:11px; border-radius:10px; font-size:13px; margin-bottom:16px; }}
  .msg.ok {{ background:#0d3320; border:1px solid #1c6b3f; color:#8ff0b6; }}
  .msg.err {{ background:#3a1420; border:1px solid #7a2740; color:#ffb0c0; }}
  .hint {{ font-size:11px; color:#6f7ea3; margin-top:4px; }}
</style></head>
<body><div class="card">
  <h1>🎮 Link your PlayStation</h1>
  <p class="sub">Connect your PSN account once. After this you never have to
    come back &mdash; it stays linked automatically.</p>
  {banner}
  <ol>
    <li>Sign in to PlayStation.</li>
    <li>Open the token page &mdash; you'll see <code>{{"npsso":"..."}}</code>.</li>
    <li>Copy it, paste below, tap <b>Link</b>.</li>
  </ol>
  <a class="btn psn" href="{PSN_LOGIN_URL}" target="_blank" rel="noopener">1 · Sign in to PlayStation</a>
  <a class="btn token" href="{NPSSO_TOKEN_URL}" target="_blank" rel="noopener">2 · Get my token</a>
  <form method="post" action="/portal/link" id="f">
    {passcode_field}
    {who_field}
    <label>3 · Paste your token here</label>
    <textarea name="npsso" id="npsso" placeholder='{{"npsso":"..."}} or just the value' required></textarea>
    <div class="hint">Tip: paste the whole <code>{{"npsso":"..."}}</code> line — we'll sort it out.</div>
    <button type="button" class="paste" onclick="pasteToken()">📋 Paste from clipboard</button>
    <button type="submit" class="link">🔗 Link my account</button>
  </form>
  <script>
    async function pasteToken() {{
      try {{
        const t = await navigator.clipboard.readText();
        if (t) document.getElementById('npsso').value = t.trim();
      }} catch (e) {{ /* clipboard blocked; user pastes manually */ }}
    }}
  </script>
</div></body></html>"""


@app.get("/portal", response_class=HTMLResponse)
def portal_home():
    return _portal_page()


@app.post("/portal/link", response_class=HTMLResponse)
def portal_link(
    npsso: str = Form(...),
    passcode: str = Form(""),
    mm_username: str = Form(""),
):
    if PORTAL_PASSCODE and passcode != PORTAL_PASSCODE:
        return HTMLResponse(_portal_page(error="Wrong passcode."), status_code=403)
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
    who = result.get("online_id") or result.get("mm_username") or "your account"
    return HTMLResponse(
        _portal_page(ok=f"{who} is linked! You're all set — nothing else to do.")
    )


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
    """Live presence + avatars for every linked account (JSON, for the UI)."""
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
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Squad · PSN</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;
    background:radial-gradient(1200px 600px at 50% -10%,#182a4e 0%,#0b1020 55%),#0b1020;
    color:#e9edf5; min-height:100vh; }
  .wrap { max-width:820px; margin:0 auto; padding:22px 16px 60px; }
  header { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
  h1 { font-size:22px; margin:0; }
  .sub { color:#9aa7c4; font-size:13px; margin:0 0 20px; }
  .actions { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:10px; margin-bottom:26px; }
  .act { border:none; border-radius:13px; padding:15px 12px; font-size:15px; font-weight:700;
    cursor:pointer; color:#fff; transition:transform .06s ease, filter .15s ease; }
  .act:active { transform:scale(.97); }
  .act:hover { filter:brightness(1.1); }
  .squad { background:#0070d1; }  .game { background:#1b7f3b; }
  .roast { background:#c0392b; }  .roaststop { background:#3a4664; }
  .card { background:#141b31; border:1px solid #26324f; border-radius:16px; padding:18px; }
  .row { display:flex; align-items:center; gap:13px; padding:11px 6px;
    border-bottom:1px solid #202b45; }
  .row:last-child { border-bottom:none; }
  .av { width:46px; height:46px; border-radius:12px; background:#22304f; object-fit:cover;
    flex:none; }
  .who { flex:1; min-width:0; }
  .name { font-weight:600; font-size:15px; }
  .mm { color:#6f7ea3; font-size:12px; }
  .state { font-size:13px; color:#9aa7c4; margin-top:2px; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px;
    vertical-align:middle; background:#4a5677; }
  .on .dot { background:#2ecc71; box-shadow:0 0 8px #2ecc71; }
  .on .name { color:#eafff2; }
  .game-badge { color:#8ff0b6; }
  .empty { color:#7d8ab0; text-align:center; padding:26px 10px; font-size:14px; }
  .toast { position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
    background:#1b2540; border:1px solid #33436b; color:#e9edf5;
    padding:11px 18px; border-radius:12px; font-size:14px; opacity:0; pointer-events:none;
    transition:opacity .2s; }
  .toast.show { opacity:1; }
  .foot { margin-top:22px; text-align:center; }
  .foot a { color:#5a9bff; font-size:13px; text-decoration:none; }
  .spin { color:#7d8ab0; text-align:center; padding:24px; }
</style></head>
<body><div class="wrap">
  <header>
    <h1>🎮 The Squad</h1>
    <span class="sub" id="onlinecount"></span>
  </header>
  <p class="sub">Live PlayStation status · tap an action to rally everyone</p>

  <div class="actions">
    <button class="act squad" onclick="act('/v2/squad','Squad Up sent! 🎮')">🎮 Squad Up</button>
    <button class="act game" onclick="act('/v2/send','Game Time sent! 🕹️','🎮🔥 Let\\'s party up y\\'all. It\\'s GAME TIME! 🕹️💥')">🕹️ Game Time</button>
    <button class="act roast" onclick="act('/roast/once','Roast fired 🔥')">🔥 Roast Now</button>
    <button class="act roaststop" onclick="act('/roast/stop','Roast bot stopped')">🛑 Stop Roast</button>
  </div>

  <div class="card" id="squad"><div class="spin">Loading squad…</div></div>

  <div class="foot"><a href="/portal">＋ Link a PlayStation account</a></div>
</div>
<div class="toast" id="toast"></div>
<script>
const toast = (m) => { const t=document.getElementById('toast'); t.textContent=m;
  t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2200); };

async function act(path, okMsg, message) {
  try {
    const opts = { method:'POST' };
    if (message) { opts.headers={'Content-Type':'application/json'};
      opts.body=JSON.stringify({message}); }
    const r = await fetch(path, opts);
    toast(r.ok ? okMsg : 'Failed ('+r.status+')');
  } catch(e) { toast('Network error'); }
}

function fmtLast(iso){ if(!iso) return 'offline';
  const d=new Date(iso), now=new Date(), s=(now-d)/1000;
  if(s<3600) return Math.max(1,Math.floor(s/60))+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago'; }

async function loadSquad(){
  try {
    const r = await fetch('/api/squad'); const {squad=[]} = await r.json();
    const el = document.getElementById('squad');
    const online = squad.filter(m=>m.online).length;
    document.getElementById('onlinecount').textContent =
      squad.length ? (online+' / '+squad.length+' online') : '';
    if(!squad.length){ el.innerHTML =
      '<div class="empty">No one linked yet.<br>Share the link below to add the squad.</div>';
      return; }
    el.innerHTML = squad.map(m=>{
      const name = m.online_id || m.mm_username || 'Unknown';
      const av = m.avatar || '';
      const avImg = av ? '<img class="av" src="'+av+'" alt="">' : '<div class="av"></div>';
      let state;
      if(m.online){ state = m.game
        ? '<span class="game-badge">Playing '+m.game+'</span>'
        : 'Online'+(m.platform?' · '+m.platform:''); }
      else state = 'Last online '+fmtLast(m.last_online);
      const mm = m.mm_username ? '<span class="mm"> @'+m.mm_username+'</span>' : '';
      return '<div class="row '+(m.online?'on':'')+'">'+avImg+
        '<div class="who"><div class="name"><span class="dot"></span>'+name+mm+'</div>'+
        '<div class="state">'+state+'</div></div></div>';
    }).join('');
  } catch(e){
    document.getElementById('squad').innerHTML =
      '<div class="empty">Couldn\\'t load squad status.</div>';
  }
}
loadSquad();
setInterval(loadSquad, 30000);  // refresh every 30s
</script>
</body></html>"""

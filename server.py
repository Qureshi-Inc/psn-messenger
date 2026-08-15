import os
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from psnawp_api import PSNAWP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NPSSO_TOKEN = os.environ.get("NPSSO_TOKEN")
GROUP_ID = os.environ.get("GROUP_ID")
# Auto-Squad: a separate PSN group (everyone except wolfie/IG_Juicy) that the
# "Squad Up" Stream Deck button rallies. Created 2026-08-15; overridable via env.
SQUAD_GROUP_ID = os.environ.get("SQUAD_GROUP_ID", "213250d833ccce334b651e2ee15e365c97468e02-869")

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

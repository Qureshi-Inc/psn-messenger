import os
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from psnawp_api import PSNAWP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NPSSO_TOKEN = os.environ.get("NPSSO_TOKEN")
GROUP_ID = os.environ.get("GROUP_ID")

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

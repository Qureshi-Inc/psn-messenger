"""AI Roast Bot — generates roasts using Bedrock and sends to PSN group."""

import asyncio
import json
import logging
import random
import time

import boto3
import httpx

logger = logging.getLogger(__name__)

PSN_API_URL = "http://127.0.0.1:3000/v2/send"

INSIDER_LINES = [
    "{name} you hella weakk!",
    "{name}, have you ever? 👉👌",
    "you hear that shit!!",
    "{name} is COOKED 💀",
    "{name} down bad fr fr",
    "bro {name} is free",
    "{name} just vibing in the lobby again",
    "someone check on {name} 😭",
    "iced cap merchant {name}",
    "{name} rage quit incoming",
    "did {name} just die AGAIN?",
    "{name} playing like its his first day",
    "yo {name} extract for once challenge (IMPOSSIBLE)",
]

FRIENDS_CONTEXT = """
You are a savage but funny roast bot in a PSN gaming group chat. Generate short, punchy roasts (1-2 sentences max) about these friends:

MUTASIF:
- Professional services guy (corporate drone energy)
- Always leaves games and takes FOREVER to come back
- Really good at Arc Raiders PvE, learning PvP
- Does well in PvP sometimes but still learning the ropes
- Known for disappearing mid-session

SAMAD:
- Always drives a truck (trucker lifestyle)
- Comes on rarely
- Always wants to play Call of Duty instead of Arc Raiders
- FULL of excuses when he loses: "my R3 is broken", "my screen is tilted"
- King of cope

BRENDEN:
- Very skilled player, typically #1
- In Arc Raiders he pushes too aggressively, causing squad wipes
- At times his aggression costs the team
- Good at all games but his over-confidence gets the squad killed

ZUBI:
- Addicted to iced caps (Tim Hortons)
- Hardcore PvP guy on Arc Raiders
- Recently has been the ONLY one dying and not extracting in trios
- The supposed PvP god who can't survive

RULES:
- Roast ONE random person per message
- MAXIMUM 100 characters. Must fit in a PS notification preview.
- One short sentence only. No essays.
- Be MEAN. Ruthless. Millennial energy. No mercy.
- Profanity is encouraged. Swear freely.
- Reference their specific traits/habits
- Use gaming lingo, slang, and abbreviations
- Don't use hashtags or act like a bot
- Sound like a toxic friend roasting in 10 words or less
- Think Xbox 360 lobby energy meets group chat savagery
- Be vulgar, creative, and absolutely ruthless
"""

_client = boto3.client("bedrock-runtime", region_name="us-east-1")
_running = False
_task = None


def generate_roast() -> str:
    """Generate a roast — mix of AI and insider one-liners."""
    friends = ["Mutasif", "Samad", "Brenden", "Zubi"]
    target = random.choice(friends)

    # 30% chance of insider one-liner, 70% AI generated
    if random.random() < 0.3:
        line = random.choice(INSIDER_LINES).format(name=target)
        logger.info("Insider roast for %s: %s", target, line)
        return line

    response = _client.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 150,
            "temperature": 1.0,
            "messages": [
                {
                    "role": "user",
                    "content": f"{FRIENDS_CONTEXT}\n\nGenerate a roast about {target}. Just the roast text, nothing else."
                }
            ]
        }),
    )

    result = json.loads(response["body"].read())
    roast = result["content"][0]["text"].strip()
    logger.info("AI roast for %s: %s", target, roast[:50])
    return roast


def generate_single_roast() -> str:
    """Generate one roast immediately (for on-demand button)."""
    return generate_roast()


def send_roast(message: str) -> bool:
    """Send roast to PSN group."""
    with httpx.Client(timeout=10) as client:
        resp = client.post(PSN_API_URL, json={"message": message})
        return resp.status_code < 300


async def roast_loop():
    """Main loop — sends first roast immediately, then every 5-7 minutes."""
    global _running
    logger.info("Roast bot started 🔥")

    # Send first roast immediately
    try:
        roast = generate_roast()
        success = send_roast(roast)
        if success:
            logger.info("First roast sent: %s", roast[:50])
    except Exception as e:
        logger.error("First roast failed: %s", e)

    while _running:
        logger.info("Next roast in %d seconds", delay)

        # Sleep in small chunks so we can stop quickly
        for _ in range(delay):
            if not _running:
                break
            await asyncio.sleep(1)

        if not _running:
            break

        # Send next roast
        try:
            roast = generate_roast()
            success = send_roast(roast)
            if success:
                logger.info("Roast sent: %s", roast[:50])
            else:
                logger.error("Failed to send roast")
        except Exception as e:
            logger.error("Roast generation failed: %s", e)

    logger.info("Roast bot stopped")


def start():
    """Start the roast loop."""
    global _running, _task
    if _running:
        return False
    _running = True
    _task = asyncio.create_task(roast_loop())
    return True


def stop():
    """Stop the roast loop."""
    global _running, _task
    if not _running:
        return False
    _running = False
    if _task:
        _task.cancel()
        _task = None
    return True


def is_running() -> bool:
    return _running

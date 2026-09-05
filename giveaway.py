import json
import random
from datetime import date
from pathlib import Path
from threading import Lock

POOL_FILE    = Path("/data/giveaway_pool.json")
HISTORY_FILE = Path("/data/giveaway_history.json")
_lock        = Lock()


def _load_pool() -> dict:
    if POOL_FILE.exists():
        return json.loads(POOL_FILE.read_text())
    return {"cycle": 1, "members": [], "prize": ""}


def _save_pool(pool: dict):
    POOL_FILE.write_text(json.dumps(pool, indent=2))


def _load_history() -> list:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def _append_history(entry: dict):
    history = _load_history()
    history.append(entry)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def _dedup(members: list[dict]) -> list[dict]:
    seen: set = set()
    out = []
    for m in members:
        if m["id"] not in seen:
            seen.add(m["id"])
            out.append(m)
    return out

def seed(all_members: list[dict]) -> dict:
    """Initialise the pool from platform members. Safe to re-call if already seeded."""
    with _lock:
        pool = _load_pool()
        if pool["members"]:
            return {"status": "already_seeded", "count": len(pool["members"])}
        pool["members"] = _dedup([{"id": m["id"], "display": m["display"]} for m in all_members])
        _save_pool(pool)
        return {"status": "seeded", "count": len(pool["members"])}


def reset(all_members: list[dict]) -> dict:
    """Force-reset pool to all members, incrementing the cycle."""
    with _lock:
        pool = _load_pool()
        pool["cycle"] += 1
        pool["members"] = _dedup([{"id": m["id"], "display": m["display"]} for m in all_members])
        _save_pool(pool)
        return {"status": "reset", "cycle": pool["cycle"], "count": len(pool["members"])}


def run_draw(all_members: list[dict]) -> dict:
    """
    Run this month's draw. Idempotent — if month already drawn, returns existing entry.
    """
    month = date.today().strftime("%Y-%m")
    with _lock:
        history = _load_history()
        existing = next((e for e in history if e.get("month") == month), None)
        if existing:
            return {"status": "already_drawn", **existing}

        pool = _load_pool()

        if not pool["members"]:
            pool["cycle"] += 1
            pool["members"] = [{"id": m["id"], "display": m["display"]} for m in all_members]
            _save_pool(pool)

        auto = len(pool["members"]) == 1
        winner = pool["members"][0] if auto else random.choice(pool["members"])

        pool["members"] = [m for m in pool["members"] if m["id"] != winner["id"]]
        _save_pool(pool)

        entry = {
            "cycle": pool["cycle"],
            "month": month,
            "winner_id": winner["id"],
            "winner": winner["display"],
            "auto": auto,
            "prize": pool.get("prize", ""),
        }
        _append_history(entry)
        return {"status": "drawn", **entry}


def set_prize(prize: str) -> dict:
    with _lock:
        pool = _load_pool()
        pool["prize"] = prize
        _save_pool(pool)
        return {"prize": prize}


def add_member(member: dict) -> dict:
    """Add a member to the current pool. member must have {id, display}."""
    with _lock:
        pool = _load_pool()
        if any(m["id"] == member["id"] for m in pool["members"]):
            return {"status": "already_in_pool"}
        pool["members"].append({"id": member["id"], "display": member["display"]})
        _save_pool(pool)
        return {"status": "added", "count": len(pool["members"])}


def remove_member(member_id: str) -> dict:
    """Remove a member from the current pool by id."""
    with _lock:
        pool = _load_pool()
        before = len(pool["members"])
        pool["members"] = [m for m in pool["members"] if m["id"] != member_id]
        _save_pool(pool)
        removed = before - len(pool["members"])
        return {"status": "removed" if removed else "not_found", "count": len(pool["members"])}


def get_state() -> dict:
    with _lock:
        pool = _load_pool()
        history = _load_history()
        latest = history[-1] if history else None
        cycle_winners = [e for e in history if e.get("cycle") == pool["cycle"]]
        total_in_cycle = len(pool["members"]) + len(cycle_winners)
        return {
            "cycle": pool["cycle"],
            "pool_remaining": len(pool["members"]),
            "pool_members": pool["members"],
            "total_in_cycle": total_in_cycle,
            "latest_winner": latest,
            "prize": pool.get("prize", ""),
            "seeded": len(pool["members"]) > 0 or bool(history),
        }

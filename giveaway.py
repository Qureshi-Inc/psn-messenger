import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DB_PATH = Path("/data/giveaway.db")
_lock = Lock()

VALID_STATUSES = {"draft", "open", "locked", "drawn", "revealed", "closed"}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with _lock:
        with _conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    prize TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    draw_at TEXT,
                    reveal_at TEXT,
                    created_at TEXT NOT NULL,
                    drawn_at TEXT,
                    revealed_at TEXT,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS giveaway_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                    member_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    UNIQUE(giveaway_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS giveaway_draws (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                    draw_number INTEGER NOT NULL DEFAULT 1,
                    winner_id TEXT NOT NULL,
                    winner_name TEXT NOT NULL,
                    drawn_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    invalidation_reason TEXT,
                    invalidated_at TEXT,
                    manifest_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS rotation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle INTEGER NOT NULL,
                    member_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    won_at TEXT NOT NULL,
                    giveaway_id INTEGER REFERENCES giveaways(id),
                    UNIQUE(cycle, member_id)
                );
                CREATE TABLE IF NOT EXISTS rotation_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    cycle INTEGER NOT NULL DEFAULT 1
                );
                INSERT OR IGNORE INTO rotation_meta(id, cycle) VALUES(1, 1);
            """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row) if row else None


def _get_cycle(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT cycle FROM rotation_meta WHERE id=1").fetchone()
    return row["cycle"] if row else 1


def get_rotation_state(all_members: list[dict]) -> dict:
    with _conn() as c:
        cycle = _get_cycle(c)
        won_rows = c.execute(
            "SELECT member_id, display_name, won_at FROM rotation_history WHERE cycle=?", (cycle,)
        ).fetchall()
        won_ids = {r["member_id"] for r in won_rows}
        total = len(all_members)
        eligible = [m for m in all_members if m["id"] not in won_ids]
        return {
            "cycle": cycle,
            "total_members": total,
            "won_count": len(won_ids),
            "eligible_count": len(eligible),
            "eligible": eligible,
            "won_members": [dict(r) for r in won_rows],
        }


def create_giveaway(title: str, prize: str, draw_at: str | None, reveal_at: str | None) -> dict:
    with _lock:
        with _conn() as c:
            now = _now()
            cur = c.execute(
                "INSERT INTO giveaways(title, prize, status, draw_at, reveal_at, created_at) VALUES(?,?,?,?,?,?)",
                (title, prize, "draft", draw_at, reveal_at, now),
            )
            gid = cur.lastrowid
            return {"id": gid, "status": "draft", "title": title, "prize": prize,
                    "draw_at": draw_at, "reveal_at": reveal_at, "created_at": now}


def update_giveaway(gid: int, **kwargs) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] == "closed":
                return {"error": "cannot edit a closed giveaway"}
            allowed = {"title", "prize", "draw_at", "reveal_at"}
            updates = {k: v for k, v in kwargs.items() if k in allowed}
            if not updates:
                return {"error": "no valid fields"}
            sets = ", ".join(f"{k}=?" for k in updates)
            c.execute(f"UPDATE giveaways SET {sets} WHERE id=?", (*updates.values(), gid))
            return get_giveaway(gid)


def publish_giveaway(gid: int, all_members: list[dict]) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] != "draft":
                return {"error": f"must be draft, is '{row['status']}'"}
            cycle = _get_cycle(c)
            won_ids = {r["member_id"] for r in c.execute(
                "SELECT member_id FROM rotation_history WHERE cycle=?", (cycle,)).fetchall()}
            eligible = [m for m in all_members if m["id"] not in won_ids]
            if not eligible:
                return {"error": "no eligible members — everyone has won this rotation"}
            now = _now()
            for m in eligible:
                try:
                    c.execute(
                        "INSERT INTO giveaway_entries(giveaway_id, member_id, display_name, added_at) VALUES(?,?,?,?)",
                        (gid, m["id"], m["display"], now),
                    )
                except sqlite3.IntegrityError:
                    pass
            c.execute("UPDATE giveaways SET status='open' WHERE id=?", (gid,))
            return {"status": "ok", "entries": len(eligible)}


def lock_giveaway(gid: int) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT status FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] != "open":
                return {"error": f"must be open, is '{row['status']}'"}
            c.execute("UPDATE giveaways SET status='locked' WHERE id=?", (gid,))
            return {"status": "ok"}


def draw_winner(gid: int) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] not in ("open", "locked"):
                return {"error": f"must be open or locked, is '{row['status']}'"}
            entries = c.execute(
                "SELECT * FROM giveaway_entries WHERE giveaway_id=?", (gid,)
            ).fetchall()
            if not entries:
                return {"error": "no entries in this giveaway"}
            # Build manifest hash
            sorted_ids = sorted(e["member_id"] for e in entries)
            manifest_hash = hashlib.sha256("|".join(sorted_ids).encode()).hexdigest()[:16]
            # Invalidate any prior active draws
            now = _now()
            c.execute(
                "UPDATE giveaway_draws SET status='invalidated', invalidation_reason='new draw', invalidated_at=? "
                "WHERE giveaway_id=? AND status='active'",
                (now, gid),
            )
            # Pick winner
            winner = secrets.choice(entries)
            draw_num = (c.execute(
                "SELECT COUNT(*) as cnt FROM giveaway_draws WHERE giveaway_id=?", (gid,)
            ).fetchone()["cnt"] or 0) + 1
            c.execute(
                "INSERT INTO giveaway_draws(giveaway_id, draw_number, winner_id, winner_name, drawn_at, status, manifest_hash) "
                "VALUES(?,?,?,?,?,?,?)",
                (gid, draw_num, winner["member_id"], winner["display_name"], now, "active", manifest_hash),
            )
            c.execute("UPDATE giveaways SET status='drawn', drawn_at=? WHERE id=?", (now, gid))
            return {"status": "ok", "winner": winner["display_name"]}


def reveal_winner(gid: int) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] != "drawn":
                return {"error": f"must be drawn, is '{row['status']}'"}
            draw = c.execute(
                "SELECT * FROM giveaway_draws WHERE giveaway_id=? AND status='active'", (gid,)
            ).fetchone()
            if not draw:
                return {"error": "no active draw found"}
            now = _now()
            cycle = _get_cycle(c)
            # Write to rotation history
            try:
                c.execute(
                    "INSERT INTO rotation_history(cycle, member_id, display_name, won_at, giveaway_id) VALUES(?,?,?,?,?)",
                    (cycle, draw["winner_id"], draw["winner_name"], now, gid),
                )
            except sqlite3.IntegrityError:
                pass  # already recorded
            c.execute("UPDATE giveaways SET status='revealed', revealed_at=? WHERE id=?", (now, gid))
            # Check if all members in rotation have won — if so, increment cycle
            all_members_in_rotation = c.execute(
                "SELECT COUNT(DISTINCT member_id) as cnt FROM giveaway_entries WHERE giveaway_id=?", (gid,)
            ).fetchone()["cnt"]
            total_won = c.execute(
                "SELECT COUNT(*) as cnt FROM rotation_history WHERE cycle=?", (cycle,)
            ).fetchone()["cnt"]
            # We only auto-advance cycle if everyone in this giveaway has won this cycle
            # (simplified: just record the win — cycle management is manual or via close)
            return {"status": "ok", "winner": draw["winner_name"]}


def close_giveaway(gid: int, all_members: list[dict]) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT status FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] != "revealed":
                return {"error": f"must be revealed, is '{row['status']}'"}
            now = _now()
            c.execute("UPDATE giveaways SET status='closed', closed_at=? WHERE id=?", (now, gid))
            # Check if cycle is complete: all members have won
            cycle = _get_cycle(c)
            all_ids = {m["id"] for m in all_members}
            won_ids = {r["member_id"] for r in c.execute(
                "SELECT member_id FROM rotation_history WHERE cycle=?", (cycle,)).fetchall()}
            if all_ids and all_ids <= won_ids:
                c.execute("UPDATE rotation_meta SET cycle=? WHERE id=1", (cycle + 1,))
            return {"status": "ok"}


def invalidate_and_redraw(gid: int, reason: str) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] not in ("drawn", "revealed"):
                return {"error": f"must be drawn or revealed, is '{row['status']}'"}
            now = _now()
            draw = c.execute(
                "SELECT * FROM giveaway_draws WHERE giveaway_id=? AND status='active'", (gid,)
            ).fetchone()
            if draw:
                c.execute(
                    "UPDATE giveaway_draws SET status='invalidated', invalidation_reason=?, invalidated_at=? "
                    "WHERE id=?",
                    (reason, now, draw["id"]),
                )
                # Remove disqualified winner from entries so they cannot win again in this redraw
                c.execute(
                    "DELETE FROM giveaway_entries WHERE giveaway_id=? AND member_id=?",
                    (gid, draw["winner_id"]),
                )
                # If revealed, undo rotation entry
                if row["status"] == "revealed":
                    cycle = _get_cycle(c)
                    c.execute(
                        "DELETE FROM rotation_history WHERE cycle=? AND member_id=?",
                        (cycle, draw["winner_id"]),
                    )
                    c.execute("UPDATE giveaways SET status='locked', revealed_at=NULL WHERE id=?", (gid,))
                else:
                    c.execute("UPDATE giveaways SET status='locked' WHERE id=?", (gid,))
    # Re-draw outside the lock but using the locking draw_winner
    return draw_winner(gid)


def get_giveaway(gid: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
        if not row:
            return None
        g = _row_to_dict(row)
        g["entries"] = [_row_to_dict(r) for r in c.execute(
            "SELECT * FROM giveaway_entries WHERE giveaway_id=? ORDER BY added_at", (gid,)
        ).fetchall()]
        g["draws"] = [_row_to_dict(r) for r in c.execute(
            "SELECT * FROM giveaway_draws WHERE giveaway_id=? ORDER BY draw_number", (gid,)
        ).fetchall()]
        active_draw = next((d for d in g["draws"] if d["status"] == "active"), None)
        g["active_draw"] = active_draw
        return g


def get_active_giveaway() -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM giveaways WHERE status != 'closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return get_giveaway(row["id"])


def list_past_giveaways(limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM giveaways WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [get_giveaway(r["id"]) for r in rows]


def add_entry(giveaway_id: int, member_id: str, display_name: str) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT status FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] == "closed":
                return {"error": "giveaway is closed"}
            try:
                c.execute(
                    "INSERT INTO giveaway_entries(giveaway_id, member_id, display_name, added_at) VALUES(?,?,?,?)",
                    (giveaway_id, member_id, display_name, _now()),
                )
                return {"status": "added"}
            except sqlite3.IntegrityError:
                return {"status": "already_in_giveaway"}


def remove_entry(giveaway_id: int, member_id: str) -> dict:
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT status FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
            if not row:
                return {"error": "not_found"}
            if row["status"] == "closed":
                return {"error": "giveaway is closed"}
            cur = c.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id=? AND member_id=?",
                (giveaway_id, member_id),
            )
            return {"status": "removed" if cur.rowcount else "not_found"}

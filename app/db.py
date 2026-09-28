"""SQLite persistence with explicit room and attachment authorization checks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any
import uuid


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str


@dataclass(frozen=True)
class Room:
    public_id: str
    title: str
    visibility: str
    owner_user_id: int | None


class Store:
    def __init__(self, db_path: Path): self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rooms (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS room_members (room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (room_id, user_id));
                CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), content TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_room_created_idx ON messages(room_id, id);
            """)
            self._add_column(conn, "rooms", "visibility", "TEXT NOT NULL DEFAULT 'shared' CHECK (visibility IN ('shared', 'private'))")
            self._add_column(conn, "rooms", "owner_user_id", "INTEGER")
            self._add_column(conn, "rooms", "archived_at", "TEXT")
            conn.execute("""CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, stored_filename TEXT NOT NULL UNIQUE,
                mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, message_id INTEGER UNIQUE REFERENCES messages(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL)""")
            self._add_column(conn, "attachments", "gallery_hidden_at", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS attachments_room_owner_idx ON attachments(room_id, owner_user_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS message_attachments (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                attachment_id INTEGER NOT NULL UNIQUE REFERENCES attachments(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                PRIMARY KEY (message_id, position))""")

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        if name not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def bootstrap_user(self, username: str, display_name: str, password_hash: str) -> User:
        with self._connect() as conn:
            conn.execute("""INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name, password_hash=excluded.password_hash""", (username, display_name, password_hash, _now()))
        user = self.user_by_username(username); assert user is not None; return user

    def user_by_username(self, username: str) -> User | None:
        with self._connect() as conn: row = conn.execute("SELECT id, username, display_name FROM users WHERE username=?", (username,)).fetchone()
        return User(**dict(row)) if row else None

    def password_hash_for(self, username: str) -> str | None:
        with self._connect() as conn: row = conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        return row["password_hash"] if row else None

    def create_session(self, token_hash: str, csrf_token: str, user_id: int) -> None:
        with self._connect() as conn: conn.execute("INSERT INTO sessions (token_hash,user_id,csrf_token,expires_at) VALUES (?,?,?,?)", (token_hash, user_id, csrf_token, (datetime.now(UTC) + timedelta(days=14)).isoformat()))

    def session_user(self, token_hash: str, csrf_token: str | None = None) -> User | None:
        with self._connect() as conn: row = conn.execute("""SELECT u.id,u.username,u.display_name,s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?""", (token_hash, _now())).fetchone()
        if not row or (csrf_token is not None and row["csrf_token"] != csrf_token): return None
        return User(id=row["id"], username=row["username"], display_name=row["display_name"])

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn: conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def create_shared_room(self, title: str, member_ids: list[int], owner_user_id: int | None = None) -> Room:
        if len(set(member_ids)) < 2: raise ValueError("Shared rooms require both members")
        return self._create_room(title, "shared", member_ids, owner_user_id)

    def create_private_room(self, title: str, owner_user_id: int) -> Room:
        return self._create_room(title, "private", [owner_user_id], owner_user_id)

    def _create_room(self, title: str, visibility: str, member_ids: list[int], owner_user_id: int | None) -> Room:
        title = title.strip()
        if not title or len(title) > 120: raise ValueError("Room title must be 1–120 characters")
        public_id = uuid.uuid4().hex
        with self._connect() as conn:
            cursor = conn.execute("INSERT INTO rooms (public_id,title,visibility,owner_user_id,created_at) VALUES (?,?,?,?,?)", (public_id,title,visibility,owner_user_id,_now()))
            conn.executemany("INSERT INTO room_members (room_id,user_id) VALUES (?,?)", [(cursor.lastrowid, member_id) for member_id in sorted(set(member_ids))])
        return Room(public_id, title, visibility, owner_user_id)

    def rooms_for_user(self, user_id: int) -> list[Room]:
        with self._connect() as conn: rows = conn.execute("""SELECT r.public_id,r.title,r.visibility,r.owner_user_id FROM rooms r JOIN room_members m ON m.room_id=r.id WHERE m.user_id=? AND r.archived_at IS NULL ORDER BY r.id""", (user_id,)).fetchall()
        return [Room(**dict(row)) for row in rows]

    def room_activity_for_user(self, user_id: int) -> list[dict[str, str | None]]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT r.public_id,CAST(MAX(msg.id) AS TEXT) AS latest_message_id
                FROM rooms r JOIN room_members m ON m.room_id=r.id
                LEFT JOIN messages msg ON msg.room_id=r.id
                WHERE m.user_id=? AND r.archived_at IS NULL
                GROUP BY r.id ORDER BY r.id""", (user_id,)).fetchall()
        return [{"id": row["public_id"], "latestMessageId": row["latest_message_id"]} for row in rows]

    def gallery_for_user(self, user_id: int, include_removed: bool = False) -> list[dict[str, Any]]:
        hidden_clause = "IS NOT NULL" if include_removed else "IS NULL"
        with self._connect() as conn:
            rows = conn.execute(f"""SELECT a.public_id,a.mime_type,a.width,a.height,a.gallery_hidden_at,
                    r.public_id AS room_id,r.title AS room_title,r.visibility,m.created_at
                FROM attachments a
                JOIN rooms r ON r.id=a.room_id
                JOIN room_members rm ON rm.room_id=r.id
                LEFT JOIN message_attachments ma ON ma.attachment_id=a.id
                JOIN messages m ON m.id=COALESCE(a.message_id,ma.message_id) AND m.room_id=r.id
                WHERE rm.user_id=? AND r.archived_at IS NULL AND a.gallery_hidden_at {hidden_clause}
                ORDER BY m.id DESC,COALESCE(ma.position,0),a.id DESC""", (user_id,)).fetchall()
        return [{
            "id": row["public_id"],
            "url": f"/api/attachments/{row['public_id']}/image",
            "mimeType": row["mime_type"],
            "width": row["width"],
            "height": row["height"],
            "roomId": row["room_id"],
            "roomTitle": row["room_title"],
            "visibility": row["visibility"],
            "createdAt": row["created_at"],
            "hidden": row["gallery_hidden_at"] is not None,
        } for row in rows]

    def set_gallery_hidden(self, attachment_public_id: str, user: User, hidden: bool) -> None:
        with self._connect() as conn:
            row = conn.execute("""SELECT a.id FROM attachments a
                JOIN room_members rm ON rm.room_id=a.room_id
                JOIN rooms r ON r.id=a.room_id
                WHERE a.public_id=? AND rm.user_id=? AND r.archived_at IS NULL
                    AND (a.message_id IS NOT NULL OR EXISTS (
                        SELECT 1 FROM message_attachments ma WHERE ma.attachment_id=a.id))""",
                (attachment_public_id, user.id)).fetchone()
            if not row:
                raise PermissionError("Attachment not found")
            conn.execute("UPDATE attachments SET gallery_hidden_at=? WHERE id=?",
                         (_now() if hidden else None, row["id"]))

    def search_messages(self, user_id: int, query: str, limit: int = 60) -> list[dict[str, Any]]:
        escaped = query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
        with self._connect() as conn:
            rows = conn.execute("""SELECT CAST(msg.id AS TEXT) AS message_id,msg.role,msg.content,msg.created_at,
                    COALESCE(u.display_name,'Pip') AS author,r.public_id AS room_id,r.title AS room_title,r.visibility
                FROM messages msg
                JOIN rooms r ON r.id=msg.room_id
                JOIN room_members rm ON rm.room_id=r.id
                LEFT JOIN users u ON u.id=msg.author_user_id
                WHERE rm.user_id=? AND r.archived_at IS NULL AND msg.content LIKE ? ESCAPE '!'
                ORDER BY msg.id DESC LIMIT ?""", (user_id, f"%{escaped}%", limit)).fetchall()
        results = []
        needle = query.strip().casefold()
        for row in rows:
            content = row["content"]
            match_at = content.casefold().find(needle)
            start = max(0, match_at - 100)
            end = min(len(content), start + 320)
            snippet = content[start:end]
            if start:
                snippet = "…" + snippet
            if end < len(content):
                snippet += "…"
            results.append({
            "messageId": row["message_id"],
            "role": row["role"],
            "content": snippet,
            "createdAt": row["created_at"],
            "author": row["author"],
            "roomId": row["room_id"],
            "roomTitle": row["room_title"],
            "visibility": row["visibility"],
            })
        return results

    def rename_room(self, room_public_id: str, user: User, title: str) -> Room:
        title = title.strip()
        if not title or len(title) > 120: raise ValueError("Room title must be 1–120 characters")
        with self._connect() as conn:
            room = self._room_for_member(conn, room_public_id, user.id)
            conn.execute("UPDATE rooms SET title=? WHERE id=?", (title, room["id"]))
            row = conn.execute("SELECT public_id,title,visibility,owner_user_id FROM rooms WHERE id=?", (room["id"],)).fetchone()
        return Room(**dict(row))

    def archive_room(self, room_public_id: str, user: User) -> None:
        with self._connect() as conn:
            room = self._room_for_member(conn, room_public_id, user.id)
            conn.execute("UPDATE rooms SET archived_at=? WHERE id=?", (_now(), room["id"]))

    def add_message(self, room_public_id: str, user: User, role: str, content: str, attachment_public_id: str | None = None, attachment_public_ids: list[str] | None = None) -> dict[str, Any]:
        if role not in {"user", "assistant"}: raise ValueError("Unsupported message role")
        content = content.strip()
        if not content or len(content) > 8000: raise ValueError("Messages must be 1–8,000 characters")
        with self._connect() as conn:
            room = self._room_for_member(conn, room_public_id, user.id, require_active=True)
            cursor = conn.execute("INSERT INTO messages (room_id,author_user_id,role,content,created_at) VALUES (?,?,?,?,?)", (room["id"], user.id if role == "user" else None, role, content, _now()))
            ids = list(dict.fromkeys([*(attachment_public_ids or []), *([attachment_public_id] if attachment_public_id else [])]))
            if len(ids) > 4: raise ValueError("Attach up to four images")
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(f"""SELECT id,public_id FROM attachments WHERE public_id IN ({placeholders}) AND room_id=? AND owner_user_id=? AND message_id IS NULL""", (*ids, room["id"], user.id)).fetchall()
                if len(rows) != len(ids): raise PermissionError("Attachment not found")
                by_public_id = {row["public_id"]: row["id"] for row in rows}
                first = ids[0]
                changed = conn.execute("UPDATE attachments SET message_id=? WHERE public_id=? AND message_id IS NULL", (cursor.lastrowid, first)).rowcount
                if changed != 1: raise PermissionError("Attachment not found")
                conn.executemany("INSERT INTO message_attachments (message_id,attachment_id,position) VALUES (?,?,?)",
                                 [(cursor.lastrowid, by_public_id[public_id], position)
                                  for position, public_id in enumerate(ids[1:], start=1)])
            return self._message_for_id(conn, cursor.lastrowid)

    def messages_for_room(self, room_public_id: str, user: User) -> list[dict[str, Any]]:
        with self._connect() as conn:
            room = self._room_for_member(conn, room_public_id, user.id)
            rows = conn.execute("SELECT m.id FROM messages m WHERE m.room_id=? ORDER BY m.id", (room["id"],)).fetchall()
            return [self._message_for_id(conn, row["id"]) for row in rows]

    def create_attachment(self, room_public_id: str, user: User, filename: str, mime_type: str, width: int, height: int) -> str:
        with self._connect() as conn:
            room = self._room_for_member(conn, room_public_id, user.id, require_active=True)
            public_id = uuid.uuid4().hex
            conn.execute("INSERT INTO attachments (public_id,room_id,owner_user_id,stored_filename,mime_type,width,height,created_at) VALUES (?,?,?,?,?,?,?,?)", (public_id,room["id"],user.id,filename,mime_type,width,height,_now()))
        return public_id

    def attachment_for_member(self, attachment_public_id: str, user: User, room_public_id: str | None = None, require_bound: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("""SELECT a.* FROM attachments a JOIN room_members m ON m.room_id=a.room_id JOIN rooms r ON r.id=a.room_id WHERE a.public_id=? AND m.user_id=?""", (attachment_public_id,user.id)).fetchone()
            mapped = conn.execute("SELECT 1 FROM message_attachments WHERE attachment_id=?", (row["id"],)).fetchone() if row else None
            if not row or (room_public_id and row["room_id"] != self._room_for_member(conn, room_public_id, user.id)["id"]) or (require_bound and row["message_id"] is None and not mapped): raise PermissionError("Attachment not found")
            return dict(row)

    def ensure_default_room(self, member_ids: list[int], title: str = "Shared conversation") -> Room:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT public_id,title,visibility,owner_user_id FROM rooms WHERE title=?",
                (title,),
            ).fetchone()
        return Room(**dict(row)) if row else self.create_shared_room(title, member_ids)

    def _message_for_id(self, conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
        row = conn.execute("""SELECT m.id,m.role,m.content,m.created_at,u.display_name FROM messages m LEFT JOIN users u ON u.id=m.author_user_id WHERE m.id=?""", (message_id,)).fetchone()
        data: dict[str, Any] = {"id":str(row["id"]),"role":row["role"],"content":row["content"],"createdAt":row["created_at"],"author":row["display_name"] or "Together"}
        attachments = conn.execute("""SELECT a.public_id,a.mime_type,a.width,a.height,0 AS position FROM attachments a
            WHERE a.message_id=? UNION ALL
            SELECT a.public_id,a.mime_type,a.width,a.height,ma.position FROM message_attachments ma
            JOIN attachments a ON a.id=ma.attachment_id WHERE ma.message_id=? ORDER BY position""", (message_id, message_id)).fetchall()
        data["attachments"] = [{"id":a["public_id"],"mimeType":a["mime_type"],"width":a["width"],"height":a["height"],"url":f"/api/attachments/{a['public_id']}/image"} for a in attachments]
        if data["attachments"]: data["attachment"] = data["attachments"][0]
        return data

    def _room_for_member(self, conn: sqlite3.Connection, public_id: str, user_id: int, require_active: bool = False) -> sqlite3.Row:
        room = conn.execute("""SELECT r.id,r.public_id,r.title,r.visibility,r.owner_user_id,r.archived_at FROM rooms r JOIN room_members m ON m.room_id=r.id WHERE r.public_id=? AND m.user_id=?""", (public_id,user_id)).fetchone()
        if not room or (require_active and room["archived_at"] is not None): raise PermissionError("Room not found")
        return room

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); return conn


def _now() -> str: return datetime.now(UTC).isoformat()

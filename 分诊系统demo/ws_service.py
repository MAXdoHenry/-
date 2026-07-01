from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "hospital_consult.db"
TIME_FORMAT = "%Y-%m-%d %H:%M"
CHAT_SECRET_KEY = "demo-secret-key-for-hospital-consult-system"
CHAT_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 8

app = FastAPI(title="Hospital Consult Realtime Chat")


def now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def verify_chat_token(token: str) -> Optional[int]:
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        CHAT_SECRET_KEY.encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual = base64url_decode(signature_part)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        payload = json.loads(base64url_decode(payload_part).decode("utf-8"))
    except Exception:
        return None
    issued_at = int(payload.get("iat", 0))
    if issued_at <= 0 or time.time() - issued_at > CHAT_TOKEN_MAX_AGE_SECONDS:
        return None
    user_id = payload.get("user_id")
    return int(user_id) if isinstance(user_id, int) else None


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def get_user_with_hospital(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                u.id AS id,
                u.name AS name,
                u.department AS department,
                u.role AS role,
                u.title AS title,
                u.avatar AS avatar,
                h.id AS hospital_id,
                h.name AS hospital_name,
                h.path AS hospital_path,
                h.level AS hospital_level
            FROM users u
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


def get_relation_status(user_id: int, friend_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT status
            FROM friend_relations
            WHERE user_id = ? AND friend_id = ?
            """,
            (user_id, friend_id),
        ).fetchone()
        return row["status"] if row else ""
    finally:
        conn.close()


def is_friend(user_id: int, friend_id: int) -> bool:
    return get_relation_status(user_id, friend_id) == "accepted"


def is_group_member(group_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id
            FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def private_room(user_id: int, friend_id: int) -> str:
    left, right = sorted([user_id, friend_id])
    return f"private:{left}:{right}"


def group_room(group_id: int) -> str:
    return f"group:{group_id}"


def send_private_message(sender_id: int, receiver_id: int, content: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO private_messages (
                    sender_id, receiver_id, content, send_time, is_read, is_deleted
                )
                VALUES (?, ?, ?, ?, 0, 0)
                """,
                (sender_id, receiver_id, content, now_text()),
            )
            message_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT *
                FROM private_messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            return row_to_dict(row)
    finally:
        conn.close()


def send_group_message(group_id: int, sender: Dict[str, Any], content: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO group_messages (
                    group_id, sender_id, sender_name, sender_hospital,
                    sender_department, content, send_time,
                    attachment_path, attachment_name, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 0)
                """,
                (
                    group_id,
                    sender["id"],
                    sender["name"],
                    sender["hospital_name"],
                    sender["department"],
                    content,
                    now_text(),
                ),
            )
            message_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT *
                FROM group_messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            return row_to_dict(row)
    finally:
        conn.close()


def mark_private_messages_read(user_id: int, friend_id: int) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE private_messages
                SET is_read = 1
                WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
                """,
                (friend_id, user_id),
            )
    finally:
        conn.close()


def mark_group_read(group_id: int, user_id: int) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE group_members
                SET last_read_time = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (now_text(), group_id, user_id),
            )
    finally:
        conn.close()


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.user_ids: Dict[WebSocket, int] = {}

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        self.user_ids[websocket] = user_id

    def join(self, room: str, websocket: WebSocket) -> None:
        self.rooms.setdefault(room, set()).add(websocket)

    def leave_all(self, websocket: WebSocket) -> None:
        self.user_ids.pop(websocket, None)
        empty_rooms: List[str] = []
        for room, members in self.rooms.items():
            members.discard(websocket)
            if not members:
                empty_rooms.append(room)
        for room in empty_rooms:
            self.rooms.pop(room, None)

    async def broadcast(self, room: str, payload: Dict[str, Any]) -> None:
        stale: List[WebSocket] = []
        for websocket in list(self.rooms.get(room, set())):
            try:
                await websocket.send_json(payload)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            self.leave_all(websocket)


manager = ConnectionManager()


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    user_id = verify_chat_token(token)
    user = get_user_with_hospital(user_id) if user_id else None
    if user_id is None or user is None:
        await websocket.close(code=4401)
        return

    await manager.connect(websocket, user_id)
    manager.join(f"user:{user_id}", websocket)
    await websocket.send_json({"type": "connected", "user_id": user_id})

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")
            chat_type = data.get("chat_type")
            target_id = int(data.get("target_id", 0) or 0)

            if event_type == "join":
                if chat_type == "private":
                    if not is_friend(user_id, target_id):
                        await websocket.send_json({"type": "error", "message": "只能加入好友私聊"})
                        continue
                    manager.join(private_room(user_id, target_id), websocket)
                    mark_private_messages_read(user_id, target_id)
                    await websocket.send_json(
                        {"type": "joined", "chat_type": "private", "target_id": target_id}
                    )
                elif chat_type == "group":
                    if not is_group_member(target_id, user_id):
                        await websocket.send_json({"type": "error", "message": "不是该群成员"})
                        continue
                    manager.join(group_room(target_id), websocket)
                    mark_group_read(target_id, user_id)
                    await websocket.send_json(
                        {"type": "joined", "chat_type": "group", "target_id": target_id}
                    )

            elif event_type == "message":
                content = str(data.get("content", "")).strip()
                if not content:
                    await websocket.send_json({"type": "error", "message": "消息不能为空"})
                    continue
                if chat_type == "private":
                    if not is_friend(user_id, target_id):
                        await websocket.send_json({"type": "error", "message": "只能给好友发消息"})
                        continue
                    message = send_private_message(user_id, target_id, content)
                    await manager.broadcast(
                        private_room(user_id, target_id),
                        {
                            "type": "message",
                            "chat_type": "private",
                            "message": message,
                        },
                    )
                    await manager.broadcast(
                        f"user:{target_id}",
                        {
                            "type": "conversation_update",
                            "chat_type": "private",
                            "target_id": user_id,
                            "content": content,
                            "send_time": message["send_time"],
                        },
                    )
                elif chat_type == "group":
                    if not is_group_member(target_id, user_id):
                        await websocket.send_json({"type": "error", "message": "不是该群成员"})
                        continue
                    message = send_group_message(target_id, user, content)
                    await manager.broadcast(
                        group_room(target_id),
                        {
                            "type": "message",
                            "chat_type": "group",
                            "message": message,
                        },
                    )
                else:
                    await websocket.send_json({"type": "error", "message": "未知聊天类型"})

            elif event_type == "read":
                if chat_type == "private" and target_id:
                    mark_private_messages_read(user_id, target_id)
                elif chat_type == "group" and target_id:
                    mark_group_read(target_id, user_id)

    except WebSocketDisconnect:
        manager.leave_all(websocket)

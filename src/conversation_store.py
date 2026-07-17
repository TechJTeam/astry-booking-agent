"""PostgresConversationStore — lưu bền vững lịch sử hội thoại (đáp ứng BR1-2 SRS: Audit &
Training AI), thay cho MemoryConversationStore mặc định của `vanna` (in-RAM, mất khi restart).

Ghi vào schema Postgres RIÊNG của agent (`booking_agent`) trong CÙNG server/database với
astry-pos-be — xem `db/setup_schema.sql` để tạo schema + role riêng (`booking_agent_svc`)
trước khi dùng store này. Role đó không có quyền gì trên schema `public` mà Prisma của
astry-pos-be quản lý, nên việc dùng chung server không vi phạm quyết định kiến trúc "agent
không ghi thẳng DB nghiệp vụ của astry-pos-be" — đây là 1 schema hoàn toàn khác, chỉ chứa
lịch sử chat.

Toàn bộ `Conversation` (bao gồm messages, tool_calls) được lưu nguyên dạng JSONB thay vì trải
ra nhiều cột/bảng quan hệ, để không phải đồng bộ schema mỗi khi model `Message`/`Conversation`
của `vanna` đổi field."""
from __future__ import annotations

import os
from typing import List, Optional

import asyncpg
from vanna.core.storage import Conversation, ConversationStore, Message
from vanna.core.user import User

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversations_user_id_updated_at_idx
    ON conversations (user_id, updated_at DESC);
"""


class PostgresConversationStore(ConversationStore):
    def __init__(self, dsn: Optional[str] = None):
        self._dsn = dsn or os.environ["CONVERSATION_STORE_URL"]
        self._pool: Optional[asyncpg.Pool] = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                await conn.execute(CREATE_TABLE_SQL)
        return self._pool

    async def create_conversation(
        self, conversation_id: str, user: User, initial_message: str
    ) -> Conversation:
        conversation = Conversation(
            id=conversation_id,
            user=user,
            messages=[Message(role="user", content=initial_message)],
        )
        await self._save(conversation)
        return conversation

    async def get_conversation(self, conversation_id: str, user: User) -> Optional[Conversation]:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT data FROM conversations WHERE id = $1 AND user_id = $2",
            conversation_id,
            user.id,
        )
        if row is None:
            return None
        return Conversation.model_validate_json(row["data"])

    async def update_conversation(self, conversation: Conversation) -> None:
        await self._save(conversation)

    async def delete_conversation(self, conversation_id: str, user: User) -> bool:
        pool = await self._get_pool()
        result = await pool.execute(
            "DELETE FROM conversations WHERE id = $1 AND user_id = $2", conversation_id, user.id
        )
        return int(result.split()[-1]) == 1

    async def list_conversations(
        self, user: User, limit: int = 50, offset: int = 0
    ) -> List[Conversation]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT data FROM conversations WHERE user_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3",
            user.id,
            limit,
            offset,
        )
        return [Conversation.model_validate_json(r["data"]) for r in rows]

    async def _save(self, conversation: Conversation) -> None:
        pool = await self._get_pool()
        payload = conversation.model_dump_json()
        await pool.execute(
            """
            INSERT INTO conversations (id, user_id, data, updated_at)
            VALUES ($1, $2, $3::jsonb, $4)
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
            """,
            conversation.id,
            conversation.user.id,
            payload,
            conversation.updated_at,
        )

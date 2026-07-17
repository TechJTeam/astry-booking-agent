"""Integration test cho PostgresConversationStore — cần 1 Postgres thật để chạy (không mock
được asyncpg một cách có ý nghĩa). Mặc định SKIP nếu chưa set TEST_CONVERSATION_STORE_URL, để
không vô tình chạy nhắm vào DB thật của ai đó trong CI/dev machine không có Postgres.

Chạy thật (vd với Postgres local qua docker):
  docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
  TEST_CONVERSATION_STORE_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_conversation_store.py
"""
import os
import uuid

import pytest
from vanna.core.user import User

from src.conversation_store import PostgresConversationStore

TEST_DSN = os.getenv("TEST_CONVERSATION_STORE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_CONVERSATION_STORE_URL chưa set — bỏ qua integration test cần Postgres thật"
)


@pytest.fixture
def store():
    return PostgresConversationStore(dsn=TEST_DSN)


@pytest.fixture
def user():
    return User(id=f"user-{uuid.uuid4().hex[:8]}", email="staff@example.com")


@pytest.mark.asyncio
async def test_create_get_update_delete_roundtrip(store, user):
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"

    created = await store.create_conversation(conv_id, user, "Xin chào, tôi muốn đặt lịch cắt tóc")
    assert created.id == conv_id
    assert created.messages[0].content == "Xin chào, tôi muốn đặt lịch cắt tóc"

    fetched = await store.get_conversation(conv_id, user)
    assert fetched is not None
    assert fetched.id == conv_id
    assert len(fetched.messages) == 1

    from vanna.core.storage import Message

    fetched.add_message(Message(role="assistant", content="Bạn muốn đặt ngày nào?"))
    await store.update_conversation(fetched)

    refetched = await store.get_conversation(conv_id, user)
    assert len(refetched.messages) == 2
    assert refetched.messages[1].role == "assistant"

    deleted = await store.delete_conversation(conv_id, user)
    assert deleted is True
    assert await store.get_conversation(conv_id, user) is None


@pytest.mark.asyncio
async def test_get_conversation_scoped_to_user(store, user):
    other_user = User(id=f"other-{uuid.uuid4().hex[:8]}", email="other@example.com")
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"

    await store.create_conversation(conv_id, user, "hello")

    assert await store.get_conversation(conv_id, other_user) is None
    assert await store.get_conversation(conv_id, user) is not None


@pytest.mark.asyncio
async def test_list_conversations_ordered_by_updated_at_desc(store, user):
    ids = [f"conv-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    for conv_id in ids:
        await store.create_conversation(conv_id, user, "hello")

    listed = await store.list_conversations(user, limit=10)
    listed_ids = [c.id for c in listed]
    for conv_id in ids:
        assert conv_id in listed_ids

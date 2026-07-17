import base64
import json
import os

import pytest
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory

os.environ.setdefault("POS_API_BASE_URL", "http://pos-be.test")
os.environ.setdefault("POS_INTERNAL_API_KEY", "test-internal-key")


def _b64url(data: dict) -> str:
    raw = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_fake_jwt(**claims) -> str:
    header = _b64url({"alg": "RS256", "typ": "JWT"})
    payload = _b64url(
        {
            "sub": "staff-1",
            "email": "staff@example.com",
            "preferred_username": "staff1",
            "salon_id": "salon-123",
            "realm_access": {"roles": ["MANAGER"]},
            **claims,
        }
    )
    return f"{header}.{payload}.fakesignature"


@pytest.fixture
def fake_jwt() -> str:
    return make_fake_jwt()


@pytest.fixture
def tool_context(fake_jwt) -> ToolContext:
    user = User(
        id="staff-1",
        email="staff@example.com",
        group_memberships=["MANAGER"],
        metadata={"jwt": fake_jwt, "salon_id": "salon-123", "roles": ["MANAGER"]},
    )
    return ToolContext(
        user=user,
        conversation_id="conv-1",
        request_id="req-1",
        agent_memory=DemoAgentMemory(),
    )

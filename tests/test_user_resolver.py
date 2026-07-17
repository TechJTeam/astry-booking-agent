import pytest
from vanna.core.user import RequestContext

from src.user_resolver import JwtForwardUserResolver, MissingJwtError
from tests.conftest import make_fake_jwt


@pytest.mark.asyncio
async def test_resolve_user_extracts_claims_and_keeps_raw_jwt():
    token = make_fake_jwt(email="manager@salon.com", salon_id="salon-xyz")
    ctx = RequestContext(headers={"Authorization": f"Bearer {token}"})
    resolver = JwtForwardUserResolver()

    user = await resolver.resolve_user(ctx)

    assert user.email == "manager@salon.com"
    assert "MANAGER" in user.group_memberships
    assert user.metadata["jwt"] == token
    assert user.metadata["salon_id"] == "salon-xyz"


@pytest.mark.asyncio
async def test_resolve_user_missing_jwt_raises():
    resolver = JwtForwardUserResolver()
    ctx = RequestContext(headers={})

    with pytest.raises(MissingJwtError):
        await resolver.resolve_user(ctx)

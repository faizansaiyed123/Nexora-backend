"""
End-to-end API tests for Offering Matches endpoints (/v1/offering-matches).
Tests match creation, retrieval, listing, partial updating, deletion, RBAC, and tenant isolation.
"""

import uuid
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.security import create_access_token
from backend.db.session import AsyncSessionLocal
from backend.main import app
from backend.models.client import ClientModel, UserModel
from backend.models.competitor import CompetitorModel, SourceModel
from backend.models.enums import (
    ClientStatusEnum,
    CompetitorStatusEnum,
    MatchStatusEnum,
    OfferingTypeEnum,
    RoleEnum,
    SourceTypeEnum,
)
from backend.models.offering import OfferingModel


@pytest_asyncio.fixture
async def setup_offering_matches_env():
    """Create test clients, users, offerings, competitors, and sources."""
    async with AsyncSessionLocal() as session:
        # Client A
        client_a = ClientModel(
            id=uuid.uuid4(),
            name=f"Tenant Alpha {uuid.uuid4().hex[:6]}",
            slug=f"tenant-alpha-{uuid.uuid4().hex[:6]}",
            status=ClientStatusEnum.ACTIVE,
        )
        session.add(client_a)

        admin_a = UserModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            email=f"admin_a_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password="fakehashpassword123",
            full_name="Admin Alpha",
            role=RoleEnum.ORG_ADMIN,
            is_active=True,
            email_verified=True,
        )
        session.add(admin_a)

        analyst_a = UserModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            email=f"analyst_a_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password="fakehashpassword123",
            full_name="Analyst Alpha",
            role=RoleEnum.ANALYST,
            is_active=True,
            email_verified=True,
        )
        session.add(analyst_a)

        # Offering for Client A
        offering_a = OfferingModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            name="Alpha Ultra Monitor 27",
            offering_type=OfferingTypeEnum.CUSTOM,
            base_price=349.99,
            currency="USD",
        )
        session.add(offering_a)

        # Competitor and Source for Client A
        competitor_a = CompetitorModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            name="Rival Electronics Corp",
            domain="rival-electronics.com",
            status=CompetitorStatusEnum.ACTIVE,
        )
        session.add(competitor_a)

        source_a = SourceModel(
            id=uuid.uuid4(),
            competitor_id=competitor_a.id,
            name="Rival Official Store",
            base_url="https://rival-electronics.com",
            source_type=SourceTypeEnum.OFFICIAL_STORE,
        )
        session.add(source_a)

        # Client B (for tenant isolation)
        client_b = ClientModel(
            id=uuid.uuid4(),
            name=f"Tenant Beta {uuid.uuid4().hex[:6]}",
            slug=f"tenant-beta-{uuid.uuid4().hex[:6]}",
            status=ClientStatusEnum.ACTIVE,
        )
        session.add(client_b)

        admin_b = UserModel(
            id=uuid.uuid4(),
            client_id=client_b.id,
            email=f"admin_b_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password="fakehashpassword123",
            full_name="Admin Beta",
            role=RoleEnum.ORG_ADMIN,
            is_active=True,
            email_verified=True,
        )
        session.add(admin_b)

        await session.commit()
        await session.refresh(client_a)
        await session.refresh(admin_a)
        await session.refresh(analyst_a)
        await session.refresh(offering_a)
        await session.refresh(competitor_a)
        await session.refresh(source_a)
        await session.refresh(client_b)
        await session.refresh(admin_b)

        token_admin_a, _, _ = create_access_token(
            user_id=admin_a.id,
            client_id=client_a.id,
            role=admin_a.role,
            email=admin_a.email,
        )
        token_analyst_a, _, _ = create_access_token(
            user_id=analyst_a.id,
            client_id=client_a.id,
            role=analyst_a.role,
            email=analyst_a.email,
        )
        token_admin_b, _, _ = create_access_token(
            user_id=admin_b.id,
            client_id=client_b.id,
            role=admin_b.role,
            email=admin_b.email,
        )

        yield {
            "client_a": client_a,
            "offering_a": offering_a,
            "competitor_a": competitor_a,
            "source_a": source_a,
            "token_admin_a": token_admin_a,
            "token_analyst_a": token_analyst_a,
            "client_b": client_b,
            "token_admin_b": token_admin_b,
        }

        # Cleanup
        async with AsyncSessionLocal() as cleanup_session:
            ca = await cleanup_session.get(ClientModel, client_a.id)
            cb = await cleanup_session.get(ClientModel, client_b.id)
            if ca:
                await cleanup_session.delete(ca)
            if cb:
                await cleanup_session.delete(cb)
            await cleanup_session.commit()


@pytest.mark.asyncio
async def test_offering_matches_lifecycle_and_rbac(setup_offering_matches_env):
    ctx = setup_offering_matches_env
    token_admin_a = ctx["token_admin_a"]
    token_analyst_a = ctx["token_analyst_a"]
    token_admin_b = ctx["token_admin_b"]
    offering_id = str(ctx["offering_a"].id)
    source_id = str(ctx["source_a"].id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # -------------------------------------------------------------
        # 1. POST /v1/offering-matches - Create match as Admin
        # -------------------------------------------------------------
        create_payload = {
            "offering_id": offering_id,
            "source_id": source_id,
            "target_url": "https://rival-electronics.com/monitors/ultra-27",
            "match_status": "APPROVED",
            "confidence_score": 0.95,
            "extraction_selectors": {
                "price_selector": "span.price-val",
                "title_selector": "h1.prod-heading",
            },
            "is_active": True,
        }

        resp = await client.post(
            "/v1/offering-matches",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json=create_payload,
        )
        assert resp.status_code == 201, f"Create match failed: {resp.text}"
        match_data = resp.json()
        assert match_data["offering_id"] == offering_id
        assert match_data["source_id"] == source_id
        assert match_data["match_status"] == "APPROVED"
        assert match_data["confidence_score"] == 0.95
        assert match_data["extraction_selectors"]["price_selector"] == "span.price-val"
        match_id = match_data["id"]

        # -------------------------------------------------------------
        # 2. RBAC: Analyst cannot create matches
        # -------------------------------------------------------------
        resp_forbidden = await client.post(
            "/v1/offering-matches",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
            json=create_payload,
        )
        assert resp_forbidden.status_code == 403

        # -------------------------------------------------------------
        # 3. GET /v1/offering-matches - List matches as Analyst
        # -------------------------------------------------------------
        resp_list = await client.get(
            "/v1/offering-matches",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
        )
        assert resp_list.status_code == 200
        matches_list = resp_list.json()
        assert len(matches_list) >= 1
        assert any(m["id"] == match_id for m in matches_list)

        # -------------------------------------------------------------
        # 4. GET /v1/offering-matches/{match_id} - Fetch single match
        # -------------------------------------------------------------
        resp_get = await client.get(
            f"/v1/offering-matches/{match_id}",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
        )
        assert resp_get.status_code == 200
        assert resp_get.json()["id"] == match_id

        # -------------------------------------------------------------
        # 5. Tenant Isolation: Client B cannot access Client A's match
        # -------------------------------------------------------------
        resp_b_get = await client.get(
            f"/v1/offering-matches/{match_id}",
            headers={"Authorization": f"Bearer {token_admin_b}"},
        )
        assert resp_b_get.status_code == 404

        # -------------------------------------------------------------
        # 6. PATCH /v1/offering-matches/{match_id} - Update match as Admin
        # -------------------------------------------------------------
        update_payload = {
            "confidence_score": 0.99,
            "extraction_selectors": {
                "price_selector": "span.sale-price",
            },
        }
        resp_update = await client.patch(
            f"/v1/offering-matches/{match_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json=update_payload,
        )
        assert resp_update.status_code == 200
        updated = resp_update.json()
        assert updated["confidence_score"] == 0.99
        assert updated["extraction_selectors"]["price_selector"] == "span.sale-price"

        # -------------------------------------------------------------
        # 7. DELETE /v1/offering-matches/{match_id} - Delete match as Admin
        # -------------------------------------------------------------
        resp_del = await client.delete(
            f"/v1/offering-matches/{match_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_del.status_code == 204

        # Verify 404 after deletion
        resp_after_del = await client.get(
            f"/v1/offering-matches/{match_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_after_del.status_code == 404

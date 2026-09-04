"""
Comprehensive End-to-end API tests for Offerings & Dynamic Catalog endpoints (/v1/offerings).
Tests:
- Single offering CRUD lifecycle (Create, Read, Update, Soft Archive)
- Multi-field Search, Filtering, and Pagination
- Backward compatibility (base_price, source_url, dynamic_attributes, identifiers, meta_info, is_active)
- Bulk ingestion (up to 1,000 items) with index-attributed error feedback
- Bulk soft-archive
- Monitoring toggle
- Dynamic field definitions CRUD and auto-discovery
- Catalog Export (CSV with flattened dynamic attributes and JSON)
- SSRF prevention & URL validation error handling
- Multi-tenant isolation & RBAC enforcement (ADMIN required for mutations, ANALYST for reads)
"""

from datetime import datetime, timezone
from decimal import Decimal
import io
import uuid
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.security import create_access_token
from backend.db.session import AsyncSessionLocal
from backend.main import app
from backend.models.client import ClientModel, UserModel
from backend.models.competitor import CompetitorModel, OfferingMatchModel, SourceModel
from backend.models.enums import (
    AvailabilityStatusEnum,
    ClientStatusEnum,
    CompetitorStatusEnum,
    CreatedViaEnum,
    MatchStatusEnum,
    OfferingTypeEnum,
    RoleEnum,
    SourceTypeEnum,
)
from backend.models.observation import SnapshotModel
from backend.models.offering import DynamicFieldDefinitionModel, OfferingModel


@pytest_asyncio.fixture
async def setup_tenants():
    """Create test clients and users with ADMIN and ANALYST roles."""
    async with AsyncSessionLocal() as session:
        # Client A
        client_a = ClientModel(
            id=uuid.uuid4(),
            name=f"Tenant Alpha {uuid.uuid4().hex[:6]}",
            slug=f"tenant-alpha-{uuid.uuid4().hex[:6]}",
            status=ClientStatusEnum.ACTIVE,
            max_tracked_offerings=500,
        )
        session.add(client_a)

        # Admin user in Client A
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

        # Analyst user in Client A
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

        # Client B (for tenant isolation tests)
        client_b = ClientModel(
            id=uuid.uuid4(),
            name=f"Tenant Beta {uuid.uuid4().hex[:6]}",
            slug=f"tenant-beta-{uuid.uuid4().hex[:6]}",
            status=ClientStatusEnum.ACTIVE,
            max_tracked_offerings=500,
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
            "admin_a": admin_a,
            "analyst_a": analyst_a,
            "token_admin_a": token_admin_a,
            "token_analyst_a": token_analyst_a,
            "client_b": client_b,
            "admin_b": admin_b,
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
async def test_full_offering_lifecycle_and_rbac(setup_tenants):
    ctx = setup_tenants
    token_admin_a = ctx["token_admin_a"]
    token_analyst_a = ctx["token_analyst_a"]
    token_admin_b = ctx["token_admin_b"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. POST /v1/offerings - Create offering as Admin (using legacy compatible payload)
        create_payload = {
            "name": "Wireless Noise Cancelling Headphones",
            "offering_type": "PRODUCT",
            "base_price": 299.99,
            "currency": "USD",
            "source_url": "https://example.com/products/headphones",
            "description": "Premium wireless active noise cancelling headphones.",
            "identifiers": {
                "sku": "WH-1000XM5",
                "barcode": "4548736132580",
                "asin": "B09XS7JWHH",
            },
            "dynamic_attributes": {
                "brand": "Sony",
                "color": "Silver",
                "battery_life_hours": 30,
                "driver_size_mm": 30,
            },
            "meta_info": {
                "category": "Audio",
                "department": "Electronics",
                "tags": ["anc", "bluetooth", "premium"],
            },
            "created_via": "MANUAL",
        }

        resp = await client.post(
            "/v1/offerings",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json=create_payload,
        )
        assert resp.status_code == 201, f"Create failed: {resp.text}"
        offering_data = resp.json()
        assert offering_data["name"] == "Wireless Noise Cancelling Headphones"
        assert offering_data["identifiers"]["sku"] == "WH-1000XM5"
        assert offering_data["dynamic_attributes"]["brand"] == "Sony"
        assert offering_data["meta_info"]["category"] == "Audio"
        assert float(offering_data["base_price"]) == 299.99
        assert float(offering_data["current_price"]) == 299.99
        assert offering_data["is_active"] is True
        assert offering_data["is_archived"] is False
        offering_id = offering_data["id"]

        # 2. RBAC: Analyst cannot create offering (requires admin)
        resp_forbidden = await client.post(
            "/v1/offerings",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
            json=create_payload,
        )
        assert resp_forbidden.status_code == 403

        # 3. GET /v1/offerings - List offerings as Analyst (paginated response)
        resp_list = await client.get(
            "/v1/offerings",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
        )
        assert resp_list.status_code == 200
        data_list = resp_list.json()
        assert "items" in data_list
        assert data_list["total_count"] >= 1
        found_ids = [item["id"] for item in data_list["items"]]
        assert offering_id in found_ids

        # 4. GET /v1/offerings/{offering_id} - Fetch single offering details
        resp_get = await client.get(
            f"/v1/offerings/{offering_id}",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
        )
        assert resp_get.status_code == 200
        assert resp_get.json()["id"] == offering_id
        assert "matches" in resp_get.json()

        # 5. Tenant Isolation: Client B cannot view Client A's offering
        resp_b_get = await client.get(
            f"/v1/offerings/{offering_id}",
            headers={"Authorization": f"Bearer {token_admin_b}"},
        )
        assert resp_b_get.status_code == 404

        # 6. PATCH /v1/offerings/{offering_id} - Update offering as Admin
        update_payload = {
            "base_price": 279.99,
            "dynamic_attributes": {
                "brand": "Sony",
                "color": "Midnight Silver",
                "battery_life_hours": 32,
            },
        }
        resp_update = await client.patch(
            f"/v1/offerings/{offering_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json=update_payload,
        )
        assert resp_update.status_code == 200
        updated = resp_update.json()
        assert float(updated["base_price"]) == 279.99
        assert updated["dynamic_attributes"]["color"] == "Midnight Silver"
        assert updated["name"] == "Wireless Noise Cancelling Headphones"
        assert updated["identifiers"]["sku"] == "WH-1000XM5"

        # 7. POST /v1/offerings/{offering_id}/toggle-monitoring - Toggle monitoring
        resp_toggle = await client.post(
            f"/v1/offerings/{offering_id}/toggle-monitoring",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_toggle.status_code == 200
        assert resp_toggle.json()["is_monitored"] is False

        # 8. DELETE /v1/offerings/{offering_id} - Soft Archive offering as Admin
        resp_del = await client.delete(
            f"/v1/offerings/{offering_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_del.status_code == 204

        # Verify it is excluded from normal list
        resp_list_after = await client.get(
            "/v1/offerings",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert offering_id not in [item["id"] for item in resp_list_after.json()["items"]]

        # Verify it appears when include_archived=true
        resp_list_archived = await client.get(
            "/v1/offerings?include_archived=true",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert offering_id in [item["id"] for item in resp_list_archived.json()["items"]]


@pytest.mark.asyncio
async def test_bulk_ingestion_and_bulk_archive(setup_tenants):
    ctx = setup_tenants
    token_admin_a = ctx["token_admin_a"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Bulk Ingest 3 offerings (including 1 with an SSRF invalid URL)
        bulk_payload = {
            "items": [
                {
                    "name": "Gaming Keyboard K1",
                    "offering_type": "PRODUCT",
                    "sku": "KB-K1-RGB",
                    "current_price": 89.99,
                    "currency": "USD",
                    "market": "US",
                    "url": "https://example.com/keyboard-k1",
                    "category": "Gaming",
                    "attributes": {"switch_type": "Mechanical Cherry MX Red", "rgb": True},
                },
                {
                    "name": "Gaming Mouse M1",
                    "offering_type": "PRODUCT",
                    "sku": "MS-M1-BLK",
                    "current_price": 49.99,
                    "currency": "USD",
                    "market": "US",
                    "url": "https://example.com/mouse-m1",
                    "category": "Gaming",
                    "attributes": {"dpi": 16000, "wireless": True},
                },
                {
                    "name": "Malicious Offering Attempt",
                    "offering_type": "PRODUCT",
                    "sku": "BAD-ITEM-1",
                    "current_price": 10.00,
                    "url": "http://169.254.169.254/latest/meta-data/",
                    "attributes": {"danger": True},
                },
            ]
        }

        resp = await client.post(
            "/v1/offerings/bulk",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json=bulk_payload,
        )
        assert resp.status_code == 200
        bulk_res = resp.json()
        assert bulk_res["total_processed"] == 3
        assert bulk_res["successful_count"] == 2
        assert bulk_res["failed_count"] == 1
        assert len(bulk_res["errors"]) == 1
        assert bulk_res["errors"][0]["index"] == 2
        assert "validation failed" in bulk_res["errors"][0]["error"]

        # Verify Dynamic Fields auto-discovery worked
        resp_fields = await client.get(
            "/v1/offerings/fields",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_fields.status_code == 200
        field_names = [f["field_name"] for f in resp_fields.json()]
        assert "switch_type" in field_names
        assert "dpi" in field_names
        assert "rgb" in field_names

        # Fetch list to get created IDs
        resp_list = await client.get(
            "/v1/offerings?category=Gaming",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        created_items = resp_list.json()["items"]
        created_ids = [item["id"] for item in created_items]
        assert len(created_ids) >= 2

        # 2. Bulk Soft-Archive
        resp_archive = await client.post(
            "/v1/offerings/bulk-archive",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json={"offering_ids": created_ids},
        )
        assert resp_archive.status_code == 200
        arch_res = resp_archive.json()
        assert arch_res["archived_count"] >= 2


@pytest.mark.asyncio
async def test_dynamic_fields_crud(setup_tenants):
    ctx = setup_tenants
    token_admin_a = ctx["token_admin_a"]
    token_analyst_a = ctx["token_analyst_a"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        field_payload = {
            "field_name": "screen_refresh_rate_hz",
            "display_name": "Screen Refresh Rate (Hz)",
            "data_type": "NUMBER",
            "unit": "Hz",
            "category": "Display Specs",
            "description": "Panel refresh rate in Hertz.",
            "is_selected": True,
            "confidence": "HIGH",
            "is_required": False,
            "is_comparable": True,
        }

        # 1. Create Field Definition
        resp_create = await client.post(
            "/v1/offerings/fields",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json=field_payload,
        )
        assert resp_create.status_code == 201
        created_field = resp_create.json()
        assert created_field["field_name"] == "screen_refresh_rate_hz"
        field_id = created_field["id"]

        # 2. List Field Definitions
        resp_list = await client.get(
            "/v1/offerings/fields",
            headers={"Authorization": f"Bearer {token_analyst_a}"},
        )
        assert resp_list.status_code == 200
        assert any(f["id"] == field_id for f in resp_list.json())

        # 3. Delete Field Definition
        resp_del = await client.delete(
            f"/v1/offerings/fields/{field_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_del.status_code == 204


@pytest.mark.asyncio
async def test_catalog_export_csv_and_json(setup_tenants):
    ctx = setup_tenants
    token_admin_a = ctx["token_admin_a"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create an offering with dynamic attributes
        await client.post(
            "/v1/offerings",
            headers={"Authorization": f"Bearer {token_admin_a}"},
            json={
                "name": "Exportable 4K Monitor",
                "offering_type": "PRODUCT",
                "sku": "MON-4K-EXP",
                "current_price": 499.00,
                "currency": "USD",
                "attributes": {
                    "panel_type": "IPS",
                    "hdr_support": True,
                },
            },
        )

        # 1. Export CSV
        resp_csv = await client.get(
            "/v1/offerings/export?format=csv",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_csv.status_code == 200
        assert "text/csv" in resp_csv.headers["content-type"]
        csv_text = resp_csv.text
        assert "MON-4K-EXP" in csv_text
        assert "attr_panel_type" in csv_text
        assert "attr_hdr_support" in csv_text

        # 2. Export JSON
        resp_json = await client.get(
            "/v1/offerings/export?format=json",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp_json.status_code == 200
        assert "application/json" in resp_json.headers["content-type"]
        json_data = resp_json.json()
        assert isinstance(json_data, list)
        assert any(item["sku"] == "MON-4K-EXP" for item in json_data)


@pytest.mark.asyncio
async def test_competitor_enrichment_analytics(setup_tenants):
    ctx = setup_tenants
    client_a = ctx["client_a"]
    token_admin_a = ctx["token_admin_a"]

    async with AsyncSessionLocal() as session:
        # Create offering
        offering = OfferingModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            name="Ultra Fast Router",
            offering_type=OfferingTypeEnum.PRODUCT,
            current_price=Decimal("199.99"),
            currency="USD",
            market="US",
            sku="RTR-100",
        )
        session.add(offering)

        # Create competitor & source
        competitor = CompetitorModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            name="Rival Tech",
            domain="rivaltech.com",
            status=CompetitorStatusEnum.ACTIVE,
        )
        session.add(competitor)

        source = SourceModel(
            id=uuid.uuid4(),
            competitor_id=competitor.id,
            name="Rival Tech Store",
            base_url="https://rivaltech.com",
            source_type=SourceTypeEnum.OFFICIAL_STORE,
        )
        session.add(source)

        # Create Match
        match = OfferingMatchModel(
            id=uuid.uuid4(),
            offering_id=offering.id,
            source_id=source.id,
            target_url="https://rivaltech.com/product/router-r1",
            match_status=MatchStatusEnum.APPROVED,
            confidence_score=0.95,
            is_active=True,
        )
        session.add(match)

        # Create live price snapshot
        now = datetime.now(timezone.utc)
        snapshot = SnapshotModel(
            id=uuid.uuid4(),
            offering_match_id=match.id,
            current_price=179.99,
            previous_price=199.99,
            price_difference=-20.00,
            percentage_difference=-10.0,
            current_availability=AvailabilityStatusEnum.IN_STOCK,
            last_observed_at=now,
        )
        session.add(snapshot)
        await session.commit()
        offering_id = offering.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Fetch offering details with enriched metrics
        resp = await client.get(
            f"/v1/offerings/{offering_id}",
            headers={"Authorization": f"Bearer {token_admin_a}"},
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["competitor_matches_count"] == 1
        assert float(detail["min_competitor_price"]) == 179.99
        assert float(detail["avg_competitor_price"]) == 179.99
        assert float(detail["price_delta_percent"]) == -10.00
        assert len(detail["matches"]) == 1
        assert detail["matches"][0]["competitor_name"] == "Rival Tech"
        assert float(detail["matches"][0]["current_price"]) == 179.99

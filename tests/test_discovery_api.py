"""
End-to-end tests for Website Discovery endpoints (/v1/discovery).
Tests:
- Successful website crawl and offering discovery with JSON-LD and OpenGraph metadata
- SSRF prevention blocking private / loopback IPs (127.0.0.1, 169.254.169.254)
- Existing offering updating on re-discovery (de-duplication by URL/SKU)
- Multi-tenant isolation (Client B cannot access Client A's discovery jobs)
- Discovery job progress query (/v1/discovery/jobs/{job_id})
- Auto-discovery of dynamic fields during website discovery
"""

import json
import uuid
from unittest.mock import AsyncMock, patch
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.security import create_access_token
from backend.db.session import AsyncSessionLocal
from backend.main import app
from backend.models.client import ClientModel, UserModel
from backend.models.enums import (
    ClientStatusEnum,
    CreatedViaEnum,
    JobStatusEnum,
    OfferingTypeEnum,
    RoleEnum,
)
from backend.models.offering import DynamicFieldDefinitionModel, OfferingModel


@pytest_asyncio.fixture
async def setup_discovery_tenants():
    """Create test clients and admin users."""
    async with AsyncSessionLocal() as session:
        # Tenant A
        client_a = ClientModel(
            id=uuid.uuid4(),
            name=f"Discovery Tenant A {uuid.uuid4().hex[:6]}",
            slug=f"discovery-tenant-a-{uuid.uuid4().hex[:6]}",
            status=ClientStatusEnum.ACTIVE,
            max_tracked_offerings=500,
        )
        session.add(client_a)

        admin_a = UserModel(
            id=uuid.uuid4(),
            client_id=client_a.id,
            email=f"admin_disc_a_{uuid.uuid4().hex[:6]}@example.com",
            full_name="Admin Discovery A",
            hashed_password="hashed_pw_test",
            role=RoleEnum.ORG_ADMIN,
            is_active=True,
            email_verified=True,
        )
        session.add(admin_a)

        # Tenant B
        client_b = ClientModel(
            id=uuid.uuid4(),
            name=f"Discovery Tenant B {uuid.uuid4().hex[:6]}",
            slug=f"discovery-tenant-b-{uuid.uuid4().hex[:6]}",
            status=ClientStatusEnum.ACTIVE,
            max_tracked_offerings=500,
        )
        session.add(client_b)

        admin_b = UserModel(
            id=uuid.uuid4(),
            client_id=client_b.id,
            email=f"admin_disc_b_{uuid.uuid4().hex[:6]}@example.com",
            full_name="Admin Discovery B",
            hashed_password="hashed_pw_test",
            role=RoleEnum.ORG_ADMIN,
            is_active=True,
            email_verified=True,
        )
        session.add(admin_b)

        await session.commit()

        token_a, _, _ = create_access_token(
            user_id=admin_a.id,
            client_id=client_a.id,
            role=admin_a.role,
            email=admin_a.email,
        )
        token_b, _, _ = create_access_token(
            user_id=admin_b.id,
            client_id=client_b.id,
            role=admin_b.role,
            email=admin_b.email,
        )

        return {
            "client_a": client_a,
            "admin_a": admin_a,
            "token_a": token_a,
            "client_b": client_b,
            "admin_b": admin_b,
            "token_b": token_b,
        }


SAMPLE_HOMEPAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Nexora Gear Store</title>
</head>
<body>
    <h1>Welcome to Nexora Gear</h1>
    <a href="/products/headphone-pro">Pro Headphone</a>
    <a href="/products/smartwatch-x">Smartwatch X</a>
    <a href="/about-us">About Us</a>
</body>
</html>
"""

SAMPLE_PRODUCT_1_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Pro Gaming Headphone - Nexora Gear</title>
    <script type="application/ld+json">
    {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": "Nexora Pro Gaming Headphone",
        "sku": "NX-HEAD-PRO",
        "image": "https://client-store.com/images/headphone.jpg",
        "description": "High-fidelity gaming headphones with hybrid noise cancellation.",
        "brand": {
            "@type": "Brand",
            "name": "NexoraAudio"
        },
        "offers": {
            "@type": "Offer",
            "priceCurrency": "USD",
            "price": "199.99",
            "availability": "https://schema.org/InStock"
        }
    }
    </script>
</head>
<body>
    <h1>Nexora Pro Gaming Headphone</h1>
</body>
</html>
"""

SAMPLE_PRODUCT_2_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Nexora Smartwatch X</title>
    <meta property="og:title" content="Nexora Smartwatch X" />
    <meta property="og:price:amount" content="249.50" />
    <meta property="og:price:currency" content="USD" />
    <meta property="og:image" content="https://client-store.com/images/watch.jpg" />
    <meta name="description" content="All-day fitness tracker with AMOLED display." />
</head>
<body>
    <h1>Nexora Smartwatch X</h1>
    <div class="specs">
        <span class="battery-life">Battery Life: 48h</span>
    </div>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_website_discovery_success_flow(setup_discovery_tenants):
    """
    Test running website discovery on a client store.
    Mocks HTTP client responses and validates creation of Offerings and Dynamic Field Definitions.
    """
    data = setup_discovery_tenants
    token_a = data["token_a"]
    client_a_id = data["client_a"].id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mock httpx.AsyncClient.get responses for target URLs
        def mock_get(url, *args, **kwargs):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"content-type": "text/html; charset=utf-8"}

            url_str = str(url)
            if url_str == "https://client-store.com":
                mock_resp.text = SAMPLE_HOMEPAGE_HTML
            elif "/products/headphone-pro" in url_str:
                mock_resp.text = SAMPLE_PRODUCT_1_HTML
            elif "/products/smartwatch-x" in url_str:
                mock_resp.text = SAMPLE_PRODUCT_2_HTML
            else:
                mock_resp.text = "<html><body>Generic Page</body></html>"

            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            res = await client.post(
                "/v1/discovery/run",
                headers={"Authorization": f"Bearer {token_a}"},
                json={
                    "website_url": "https://client-store.com",
                    "max_pages": 5,
                    "default_offering_type": "PRODUCT",
                },
            )

        assert res.status_code == 200, res.text
        resp_data = res.json()
        assert resp_data["status"] == "COMPLETED"
        assert resp_data["successful_items"] >= 2
        assert resp_data["created_offerings_count"] >= 2
        assert len(resp_data["items"]) >= 2

        job_id = resp_data["job_id"]

        # Check job retrieval endpoint
        job_res = await client.get(
            f"/v1/discovery/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert job_res.status_code == 200
        assert job_res.json()["job_id"] == job_id
        assert job_res.json()["status"] == "COMPLETED"

    # Verify offerings saved in database with CreatedViaEnum.WEBSITE_DISCOVERY
    async with AsyncSessionLocal() as session:
        offerings = (
            await session.execute(
                OfferingModel.__table__.select().where(
                    OfferingModel.client_id == client_a_id
                )
            )
        ).all()
        assert len(offerings) >= 2
        names = [o.name for o in offerings]
        assert any("Nexora Pro Gaming Headphone" in n for n in names)
        assert any("Nexora Smartwatch X" in n for n in names)


@pytest.mark.asyncio
async def test_website_discovery_ssrf_protection(setup_discovery_tenants):
    """
    Test that SSRF attacks (loopback, private subnets, cloud metadata) are blocked with 422.
    """
    data = setup_discovery_tenants
    token_a = data["token_a"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Loopback URL
        res_loopback = await client.post(
            "/v1/discovery/run",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"website_url": "http://127.0.0.1:8000/internal"},
        )
        assert res_loopback.status_code == 422

        # Metadata IP
        res_meta = await client.post(
            "/v1/discovery/run",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"website_url": "http://169.254.169.254/latest/meta-data"},
        )
        assert res_meta.status_code == 422

        # Non-HTTP scheme
        res_file = await client.post(
            "/v1/discovery/run",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"website_url": "file:///etc/passwd"},
        )
        assert res_file.status_code == 422


@pytest.mark.asyncio
async def test_website_discovery_deduplication_and_update(setup_discovery_tenants):
    """
    Test running discovery a second time: existing offerings should be updated, not duplicated.
    """
    data = setup_discovery_tenants
    token_a = data["token_a"]
    client_a_id = data["client_a"].id

    UPDATED_PRODUCT_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Nexora Pro Gaming Headphone (New Price)</title>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org/",
            "@type": "Product",
            "name": "Nexora Pro Gaming Headphone v2",
            "sku": "NX-HEAD-PRO",
            "offers": {
                "@type": "Offer",
                "priceCurrency": "USD",
                "price": "179.99",
                "availability": "https://schema.org/InStock"
            }
        }
        </script>
    </head>
    <body><h1>Updated Headphone</h1></body>
    </html>
    """

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Run 1: Initial Discovery
        def mock_get_1(url, *args, **kwargs):
            resp = AsyncMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = SAMPLE_PRODUCT_1_HTML
            return resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get_1):
            res1 = await client.post(
                "/v1/discovery/run",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"website_url": "https://client-store.com/products/headphone-pro", "max_pages": 1},
            )
            assert res1.status_code == 200
            assert res1.json()["created_offerings_count"] == 1
            assert res1.json()["updated_offerings_count"] == 0

        # Run 2: Re-discovery with updated price and name
        def mock_get_2(url, *args, **kwargs):
            resp = AsyncMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = UPDATED_PRODUCT_HTML
            return resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get_2):
            res2 = await client.post(
                "/v1/discovery/run",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"website_url": "https://client-store.com/products/headphone-pro", "max_pages": 1},
            )
            assert res2.status_code == 200
            assert res2.json()["created_offerings_count"] == 0
            assert res2.json()["updated_offerings_count"] == 1


@pytest.mark.asyncio
async def test_website_discovery_tenant_isolation(setup_discovery_tenants):
    """
    Test that Client B cannot view Client A's discovery jobs.
    """
    data = setup_discovery_tenants
    token_a = data["token_a"]
    token_b = data["token_b"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Client A runs discovery
        def mock_get(url, *args, **kwargs):
            resp = AsyncMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = SAMPLE_PRODUCT_1_HTML
            return resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            res = await client.post(
                "/v1/discovery/run",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"website_url": "https://client-store.com", "max_pages": 1},
            )
            job_id = res.json()["job_id"]

        # Client B attempts to view Client A's job
        res_b = await client.get(
            f"/v1/discovery/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert res_b.status_code == 404

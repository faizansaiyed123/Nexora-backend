"""End-to-end browser journey for Nexora using real API and PostgreSQL records."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal, engine
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications

BASE = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SHOTS = Path(os.getenv("NEXORA_SCREENSHOT_DIR", "docs/testing/visuals"))
EMAIL = "market-analyst@northstar.example.com"
PASSWORD = "Nexora-Strong!2026"
PASSWORD_CHANGED = "Nexora-Updated!2026"
PASSWORD_RESET = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
ORG = "Northstar Market Intelligence"
NAME = "Alex Morgan"
COMPETITOR = "Retail Product Reference"
COMPETITOR_EDITED = "Retail Product Reference (Enterprise)"
SOURCE = "Retail Product Listing"
SOURCE_EDITED = "Retail Product Listing (Structured)"
OFFERING = "Sony WH-1000XM5 Wireless Headphones"
DISCOVERED = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
FIXTURE = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/"
HIGH = FIXTURE + "retail-price-high.html"
LOW = FIXTURE + "retail-price-low.html"
DISCOVER = FIXTURE + "retail-discovery-product.html"
page_errors: list[str] = []


def shot(page, filename: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / filename), full_page=True, animations="disabled", caret="hide")
    print(f"Captured {filename}", flush=True)


def pick(page, field_name: str, value: str) -> None:
    field = page.locator("label.field").filter(has_text=field_name).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option", name=value, exact=True).last.click()


def request(page, method: str, path_fragment: str, action, expected: int):
    with page.expect_response(
        lambda response: path_fragment in response.url and response.request.method == method
    ) as event:
        action()
    response = event.value
    assert response.status == expected, f"{method} {path_fragment}: {response.status} {response.text()}"
    return response


def token(page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "Expected an active browser access token."
    return str(value)


def get_api(page, path: str, expected: int = 200):
    response = page.request.get(API + path, headers={"Authorization": f"Bearer {token(page)}"})
    assert response.status == expected, f"GET {path}: {response.status} {response.text()}"
    return response.json()


def mails() -> list[dict]:
    if not MAILBOX.exists():
        return []
    result = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("data"):
                result.append(item)
        except json.JSONDecodeError:
            pass
    return result


def mail_parts(item: dict) -> tuple[str, str, list[str]]:
    message = message_from_string(str(item["data"]))
    if message.is_multipart():
        body = "\n".join(
            part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            for part in message.walk()
            if part.get_content_type() == "text/plain" and part.get_payload(decode=True)
        )
    else:
        raw = message.get_payload(decode=True)
        body = raw.decode(message.get_content_charset() or "utf-8", "replace") if raw else str(message.get_payload())
    return str(message.get("Subject", "")), body, [str(x).lower() for x in item.get("rcpt_tos", [])]


def wait_mail(subject: str, recipient: str, offset: int, timeout: float = 35) -> tuple[dict, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in mails()[offset:]:
            title, body, recipients = mail_parts(item)
            if subject.lower() in title.lower() and recipient.lower() in recipients:
                return item, body
        time.sleep(0.25)
    raise AssertionError(f"No {subject!r} email for {recipient!r}; got {[mail_parts(m)[0] for m in mails()[offset:]]}")


def link_from_email(body: str, route: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body)
    assert match, f"No one-time {route} link found in the delivered message."
    return match.group(0).rstrip(".,);")


def sign_in(page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    response = request(
        page, "POST", "/v1/auth/login",
        lambda: page.get_by_role("button", name=re.compile("Sign in")).click(), 200
    )
    assert response.json()["access_token"]
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=15000)


def sign_out(page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=10000)


async def create_role_user(role: RoleEnum, client_id: uuid.UUID, suffix: str) -> str:
    email = f"{role.value.lower()}-{suffix}@northstar.example.com"
    try:
        async with AsyncSessionLocal() as session:
            exists = await session.scalar(select(UserModel).where(UserModel.email == email))
            if not exists:
                session.add(UserModel(
                    client_id=client_id,
                    email=email,
                    full_name=f"Northstar {role.value.title()}",
                    hashed_password=get_password_hash(ROLE_PASSWORD),
                    role=role,
                    is_active=True,
                    email_verified=True,
                ))
                await session.commit()
        return email
    finally:
        await engine.dispose()


async def sent_statuses(client_id: uuid.UUID) -> list[str]:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(NotificationOutboxModel.status).where(
                    NotificationOutboxModel.client_id == client_id,
                    NotificationOutboxModel.channel == "email",
                )
            )
            return list(result.scalars().all())
    finally:
        await engine.dispose()


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    MAILBOX.unlink(missing_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        # Public site, registration, real verification message and sign-in.
        page.goto(BASE + "/welcome", wait_until="networkidle")
        page.get_by_role("heading", name=re.compile("Understand the market")).wait_for()
        shot(page, "00-public-homepage.png")
        page.get_by_role("link", name=re.compile("Create workspace")).click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "01-registration-form.png")
        page.get_by_label("Organization name").fill(ORG)
        page.get_by_label("Your full name").fill(NAME)
        page.get_by_label("Work email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        mail_offset = len(mails())
        registration = request(
            page, "POST", "/v1/auth/register",
            lambda: page.get_by_role("button", name=re.compile("Create workspace")).click(), 201
        )
        assert registration.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "02-verification-email-sent.png")
        _, body = wait_mail("Verify your Nexora account", EMAIL, mail_offset)
        verify_url = link_from_email(body, "verify-email")
        assert urlparse(verify_url).path.endswith("/verify-email")
        page.goto(verify_url, wait_until="networkidle")
        shot(page, "03-email-verification-page.png")
        request(page, "GET", "/v1/auth/verify-email", lambda: page.get_by_role("button", name=re.compile("Verify email")).click(), 200)
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page, "05-login.png")
        sign_in(page, EMAIL, PASSWORD)
        shot(page, "06-dashboard-empty-workspace.png")
        user = get_api(page, "/v1/auth/me")
        client_id = uuid.UUID(user["client_id"])
        assert user["role"] == "ORG_ADMIN"

        # Competitor management, duplicate constraint and persisted edit.
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors-empty.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill(COMPETITOR)
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        made = request(page, "POST", "/v1/competitors", lambda: page.get_by_role("button", name="Save competitor", exact=True).click(), 201)
        competitor_id = made.json()["id"]
        page.get_by_text(COMPETITOR, exact=True).wait_for()
        shot(page, "08-competitor-created.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        rejected = request(page, "POST", "/v1/competitors", lambda: page.get_by_role("button", name="Save competitor", exact=True).click(), 403)
        page.get_by_text("A competitor with this domain already exists.").wait_for()
        shot(page, "09-duplicate-domain-rejected.png")
        page.get_by_role("button", name="Cancel", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=COMPETITOR).first
        row.locator("button").first.click()
        page.get_by_label("Name").fill(COMPETITOR_EDITED)
        request(page, "PATCH", f"/v1/competitors/{competitor_id}", lambda: page.get_by_role("button", name="Save competitor", exact=True).click(), 200)
        assert any(item["id"] == competitor_id and item["name"] == COMPETITOR_EDITED for item in get_api(page, "/v1/competitors"))

        # Source setup and collection controls.
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("button", name="Add source", exact=True).click()
        pick(page, "Competitor", COMPETITOR_EDITED)
        page.get_by_label("Source name").fill(SOURCE)
        page.get_by_label("Base URL").fill(HIGH)
        pick(page, "Source type", "OFFICIAL_STORE")
        pick(page, "Collection method", "HTTP_FAST")
        shot(page, "10-source-creation-form.png")
        source_response = request(page, "POST", "/v1/sources", lambda: page.get_by_role("button", name="Create source", exact=True).click(), 201)
        source_id = source_response.json()["id"]
        page.get_by_text(SOURCE, exact=True).wait_for()
        shot(page, "11-sources-created.png")
        page.get_by_title("Configuration", exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page, "12-source-configuration.png")
        request(page, "PATCH", f"/v1/sources/{source_id}/configuration", lambda: page.get_by_role("button", name="Save configuration", exact=True).click(), 200)
        cfg = get_api(page, f"/v1/sources/{source_id}")["configuration"]
        assert cfg["rate_limit_rpm"] == 60 and cfg["max_retries"] == 1
        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill(SOURCE_EDITED)
        request(page, "PATCH", f"/v1/sources/{source_id}", lambda: page.get_by_role("button", name="Save changes", exact=True).click(), 200)
        page.get_by_text(SOURCE_EDITED, exact=True).wait_for()
        shot(page, "13-source-configuration-saved.png")

        # Catalog: create, inspect, edit, monitoring, dynamic attributes and archive lifecycle.
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("button", name="Add offering", exact=True).click()
        page.get_by_label("Name").fill(OFFERING)
        pick(page, "Offering type", "PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page, "14-offering-creation-form.png")
        offering_response = request(page, "POST", "/v1/offerings", lambda: page.get_by_role("button", name="Create offering", exact=True).click(), 201)
        offering_id = offering_response.json()["id"]
        page.get_by_text(OFFERING, exact=True).wait_for()
        shot(page, "15-catalog-offering-created.png")
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_role("button", name=OFFERING, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "16-offering-detail-and-history.png")
        page.keyboard.press("Escape")
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        request(page, "PATCH", f"/v1/offerings/{offering_id}", lambda: page.get_by_role("button", name="Save changes", exact=True).click(), 200)
        assert float(get_api(page, f"/v1/offerings/{offering_id}")["current_price"]) == 429.99
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        request(page, "POST", f"/v1/offerings/{offering_id}/toggle-monitoring", lambda: row.get_by_title("Toggle monitoring", exact=True).click(), 200)
        assert get_api(page, f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        request(page, "POST", f"/v1/offerings/{offering_id}/toggle-monitoring", lambda: row.get_by_title("Toggle monitoring", exact=True).click(), 200)
        assert get_api(page, f"/v1/offerings/{offering_id}")["is_monitored"] is True
        shot(page, "17-catalog-monitoring-active.png")
        page.get_by_role("button", name="Dynamic fields", exact=True).click()
        page.get_by_text("Dynamic field definitions", exact=True).wait_for()
        shot(page, "18-dynamic-fields.png")
        page.get_by_role("button", name="Add field", exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months")
        page.get_by_label("Category").fill("Commercial")
        shot(page, "19-dynamic-field-form.png")
        request(page, "POST", "/v1/offerings/fields", lambda: page.get_by_role("button", name="Create field", exact=True).click(), 201)
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "20-dynamic-field-created.png")
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        request(page, "DELETE", "/v1/offerings/fields/", lambda: field_row.locator("button.danger-icon").click(), 204)
        page.keyboard.press("Escape")

        # Bulk import and both export formats; archive only selected catalog record.
        page.get_by_role("button", name="Bulk import", exact=True).click()
        bulk = [
            {"name": "Sony ULT WEAR Wireless Headphones", "offering_type": "PRODUCT", "sku": "ULTWEAR-BLK", "current_price": "199.99", "currency": "USD", "market": "US", "url": "https://www.sony.com/electronics/headband-headphones/ult-wear", "category": "Electronics > Audio", "attributes": {"brand": "Sony", "color": "Black"}},
            {"name": "Sony LinkBuds S", "offering_type": "PRODUCT", "sku": "LINKBUDS-S-BLK", "current_price": "199.99", "currency": "USD", "market": "US", "url": "https://www.sony.com/electronics/truly-wireless/linkbuds-s", "category": "Electronics > Audio", "attributes": {"brand": "Sony", "fit": "In-ear"}},
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk))
        shot(page, "21-bulk-import-form.png")
        bulk_response = request(page, "POST", "/v1/offerings/bulk", lambda: page.get_by_role("button", name="Import offerings", exact=True).click(), 200)
        assert bulk_response.json()["successful_count"] == 2, bulk_response.text()
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        shot(page, "22-bulk-imported-catalog.png")
        with page.expect_download(timeout=15000) as csv_event:
            page.get_by_role("button", name="CSV", exact=True).click()
        csv_event.value.save_as("/tmp/nexora-export.csv")
        assert Path("/tmp/nexora-export.csv").stat().st_size > 0
        with page.expect_download(timeout=15000) as json_event:
            page.get_by_role("button", name="JSON", exact=True).click()
        json_event.value.save_as("/tmp/nexora-export.json")
        assert Path("/tmp/nexora-export.json").stat().st_size > 0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        archive_response = request(page, "POST", "/v1/offerings/bulk-archive", lambda: page.get_by_role("button", name="Archive 1", exact=True).click(), 200)
        assert archive_response.json()["archived_count"] == 1
        archive_check = get_api(page, "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")
        assert any(item["is_archived"] for item in archive_check["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING, exact=True).wait_for()
        shot(page, "23-catalog-import-export-and-archive.png")

        # Link the catalog product to a real public product page and collect its price.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("button", name="Add match", exact=True).click()
        pick(page, "Offering", OFFERING)
        pick(page, "Source", SOURCE_EDITED)
        page.get_by_label("Competitor target URL").fill(HIGH)
        pick(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "24-offering-match-form.png")
        match_response = request(page, "POST", "/v1/offering-matches", lambda: page.get_by_role("button", name="Create match", exact=True).click(), 201)
        match_id = match_response.json()["id"]
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        page.get_by_text(HIGH, exact=False).wait_for()
        shot(page, "25-offering-match-created.png")
        collection = request(page, "POST", "/v1/collections/run", lambda: row.get_by_title("Run collection", exact=True).click(), 201)
        payload = collection.json()
        assert payload["status"] == "COMPLETED" and payload["observation"]["price"] == "399.99", payload
        assert len(get_api(page, f"/v1/offerings/{offering_id}/history")) >= 1
        shot(page, "26-first-price-observation.png")

        # A real second price observation triggers the configured drop alert and notification outbox.
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        pick(page, "Alert type", "PERCENTAGE_DROP")
        pick(page, "Offering scope", OFFERING)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "27-alert-rule-form.png")
        rule_response = request(page, "POST", "/v1/alerts/rules", lambda: page.get_by_role("button", name="Create rule", exact=True).click(), 201)
        rule_id = rule_response.json()["id"]
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "28-alert-rule-created.png")
        page.get_by_role("link", name="Matches", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW)
        request(page, "PATCH", f"/v1/offering-matches/{match_id}", lambda: page.get_by_role("button", name="Save changes", exact=True).click(), 200)
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        collection2 = request(page, "POST", "/v1/collections/run", lambda: row.get_by_title("Run collection", exact=True).click(), 201)
        second = collection2.json()
        assert second["observation"]["price"] == "349.99", second
        assert second["snapshot"]["previous_price"] == "399.99", second
        assert second["snapshot"]["percentage_difference"].startswith("-12.50%"), second

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        logs = get_api(page, "/v1/alerts/logs")
        log = next((item for item in logs if item["title"] == "Competitor price dropped"), None)
        assert log and not log["is_read"], logs
        mail_offset = len(mails())
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        assert any(x["id"] == log["id"] and x["is_read"] for x in get_api(page, "/v1/alerts/logs"))
        shot(page, "29-alert-generated-and-read.png")
        delivery = dispatch_pending_notifications.run()
        assert int(delivery.get("sent", 0)) >= 1, delivery
        _, alert_body = wait_mail("Nexora Competitive Alert: Competitor price dropped", EMAIL, mail_offset)
        assert "Nexora detected a competitive intelligence event" in alert_body
        assert "SENT" in asyncio.run(sent_statuses(client_id))

        # Execution history and the collection job detail use saved backend jobs.
        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_text("ON DEMAND REFRESH", exact=True).wait_for()
        shot(page, "30-collection-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "31-collection-job-detail.png")
        page.keyboard.press("Escape")

        # SSRF validation and evidence-backed website discovery/upsert.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        blocked = request(page, "POST", "/v1/discovery/run", lambda: page.get_by_role("button", name="Run discovery", exact=True).click(), 422)
        shot(page, "32-private-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVER)
        pick(page, "Default type", "PRODUCT")
        discovered = request(page, "POST", "/v1/discovery/run", lambda: page.get_by_role("button", name="Run discovery", exact=True).click(), 200).json()
        assert discovered["status"] == "COMPLETED" and discovered["created_offerings_count"] == 1, discovered
        assert discovered["items"][0]["name"] == DISCOVERED, discovered
        page.get_by_text(DISCOVERED, exact=True).wait_for(timeout=15000)
        shot(page, "33-discovery-product-created.png")
        job = get_api(page, f"/v1/discovery/jobs/{discovered['job_id']}")
        assert job["status"] == "COMPLETED" and job["items"][0]["sku"] == "WF1000XM5-BLK"
        updated_discovery = request(page, "POST", "/v1/discovery/run", lambda: page.get_by_role("button", name="Run discovery", exact=True).click(), 200).json()
        assert updated_discovery["created_offerings_count"] == 0 and updated_discovery["updated_offerings_count"] == 1, updated_discovery
        matches = get_api(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(item["sku"] == "WF1000XM5-BLK" for item in matches["items"]) == 1
        shot(page, "34-discovery-upsert-history.png")

        # Workspace profile and password lifecycle.
        page.locator("button.profile-trigger").click()
        page.get_by_text("Account settings", exact=True).click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "35-workspace-settings.png")
        for label, value in [
            ("Company name", "Northstar Analytics"),
            ("Country", "US"),
            ("Default currency", "USD"),
            ("Market", "US"),
            ("Industry", "Consumer Electronics"),
            ("Timezone", "America/New_York"),
        ]:
            page.get_by_label(label).fill(value)
        request(page, "PATCH", "/v1/auth/profile", lambda: page.get_by_role("button", name="Save profile", exact=True).click(), 200)
        assert get_api(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"
        shot(page, "36-workspace-profile-updated.png")
        page.get_by_label("Current password").fill(PASSWORD)
        page.get_by_label("New password").fill(PASSWORD_CHANGED)
        request(page, "POST", "/v1/auth/change-password", lambda: page.get_by_role("button", name="Update password", exact=True).click(), 200)
        assert page.request.post(API + "/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}).status == 401
        assert page.request.post(API + "/v1/auth/login", json={"email": EMAIL, "password": PASSWORD_CHANGED}).status == 200
        shot(page, "37-password-changed.png")

        sign_out(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        mail_offset = len(mails())
        request(page, "POST", "/v1/auth/forgot-password", lambda: page.get_by_role("button", name="Send reset link", exact=True).click(), 200)
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "38-password-reset-requested.png")
        _, reset_body = wait_mail("Reset your Nexora password", EMAIL, mail_offset)
        page.goto(link_from_email(reset_body, "reset-password"), wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        shot(page, "39-password-reset-form.png")
        page.get_by_label("New password").fill(PASSWORD_RESET)
        request(page, "POST", "/v1/auth/reset-password", lambda: page.get_by_role("button", name="Update password", exact=True).click(), 200)
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "40-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        sign_in(page, EMAIL, PASSWORD_RESET)
        shot(page, "41-dashboard-after-password-reset.png")

        # The role UI reflects persisted account permissions; API coverage separately proves tenant isolation.
        suffix = uuid.uuid4().hex[:10]
        viewer = asyncio.run(create_role_user(RoleEnum.VIEWER, client_id, suffix))
        analyst = asyncio.run(create_role_user(RoleEnum.ANALYST, client_id, suffix))
        sign_out(page)
        sign_in(page, analyst, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        denied = page.request.post(API + "/v1/competitors", headers={"Authorization": f"Bearer {token(page)}"}, json={"name": "Not Allowed", "domain": f"forbidden-{suffix}.example"})
        assert denied.status == 403, denied.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_role("button", name="Run discovery", exact=True).wait_for()
        shot(page, "42-analyst-workspace.png")
        sign_out(page)
        sign_in(page, viewer, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        denied = page.request.post(API + "/v1/competitors", headers={"Authorization": f"Bearer {token(page)}"}, json={"name": "Viewer Write Attempt", "domain": f"viewer-{suffix}.example"})
        assert denied.status == 403, denied.text()
        shot(page, "43-viewer-read-only-workspace.png")

        # Small-screen layout sanity for the public entry page.
        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.on("pageerror", lambda error: page_errors.append(str(error)))
        mobile.goto(BASE + "/welcome", wait_until="networkidle")
        dimensions = mobile.evaluate("() => ({viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth})")
        assert dimensions["content"] <= dimensions["viewport"], dimensions
        shot(mobile, "44-mobile-homepage.png")

        assert not page_errors, "Browser runtime errors: " + " | ".join(page_errors)
        shots = list(SHOTS.glob("*.png"))
        assert len(shots) >= 45, f"Expected 45 walkthrough screenshots, found {len(shots)}"
        print(f"Completed UI/API/PostgreSQL journey with {len(shots)} screenshots.", flush=True)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

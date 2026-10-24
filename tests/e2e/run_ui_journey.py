"""Nexora end-to-end browser journey.

The app uses the real frontend, FastAPI routes, PostgreSQL records and Redis-backed
sessions/rate limits. No application API responses are intercepted or mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse
import uuid

from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy import func, select

from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.alert import AlertLogModel, AlertRuleModel
from backend.models.client import UserModel
from backend.models.competitor import CompetitorModel, OfferingMatchModel, SourceModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.models.observation import JobModel, ObservationModel, SnapshotModel
from backend.models.offering import OfferingModel
from backend.workers.tasks import dispatch_pending_notifications

FRONTEND_URL = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API_URL = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SCREENSHOTS = Path(os.getenv("NEXORA_SCREENSHOT_DIR", "docs/walkthrough/screenshots"))
EMAIL = "market-analyst@northstar.example.com"
PASSWORD = "Nexora-Strong!2026"
CHANGED_PASSWORD = "Nexora-Updated!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
ORGANIZATION = "Northstar Market Intelligence"
FULL_NAME = "Alex Morgan"
COMPETITOR = "Retail Product Reference"
SOURCE = "Retail Product Listing"
OFFERING = "Sony WH-1000XM5 Wireless Headphones"
DISCOVERED_OFFERING = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
RAW_ROOT = "https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures"
PRICE_HIGH_URL = RAW_ROOT + "/retail-price-high.html"
PRICE_LOW_URL = RAW_ROOT + "/retail-price-low.html"
DISCOVERY_URL = RAW_ROOT + "/retail-discovery-product.html"
browser_errors: list[str] = []


def shot(page: Page, name: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(SCREENSHOTS / name),
        full_page=True,
        animations="disabled",
        caret="hide",
    )
    print(f"Captured {name}", flush=True)


def choose(page: Page, label: str, option_text: str) -> None:
    field = page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option").filter(has_text=option_text).last.click()


def token(page: Page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "No access token in the signed-in browser context."
    return str(value)


def api_get(page: Page, path: str, expected: int = 200):
    response = page.request.get(
        API_URL + path,
        headers={"Authorization": f"Bearer {token(page)}"},
    )
    assert response.status == expected, f"GET {path}: {response.status} {response.text()}"
    return response.json()


def mailbox() -> list[dict]:
    if not MAILBOX.exists():
        return []
    rows = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("data"):
                rows.append(item)
        except json.JSONDecodeError:
            continue
    return rows


def mail_details(item: dict) -> tuple[str, str, list[str]]:
    message = message_from_string(str(item["data"]))
    subject = str(message.get("Subject", ""))
    parts = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    parts.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        body = "\n".join(parts)
    else:
        raw = message.get_payload(decode=True)
        body = raw.decode(message.get_content_charset() or "utf-8", "replace") if raw else str(message.get_payload())
    return subject, body, [str(value).lower() for value in item.get("rcpt_tos", [])]


def wait_mail(subject_part: str, recipient: str, start: int, timeout: float = 40) -> tuple[dict, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = mailbox()
        for row in rows[start:]:
            subject, body, recipients = mail_details(row)
            if subject_part.lower() in subject.lower() and recipient.lower() in recipients:
                return row, body
        time.sleep(0.25)
    received = [mail_details(row)[0] for row in mailbox()[start:]]
    raise AssertionError(f"Mail {subject_part!r} to {recipient!r} not received; subjects: {received}")


def mail_link(body: str, route: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body)
    assert match, f"No one-time /{route} link found in delivered email."
    return match.group(0).rstrip(".,);")


def sign_in(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(lambda r: r.url.endswith("/v1/auth/login") and r.request.method == "POST") as event:
        page.get_by_role("button", name=re.compile("Sign in")).click()
    assert event.value.status == 200, event.value.text()
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=20000)


def sign_out(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=15000)


async def create_role_user(client_id: uuid.UUID, role: RoleEnum, suffix: str) -> str:
    email = f"{role.value.lower()}-{suffix}@northstar.example.com"
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.email == email))
        if user:
            return email
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


async def verify_postgres(
    client_id: uuid.UUID,
    competitor_id: str,
    source_id: str,
    offering_id: str,
    match_id: str,
    rule_id: str,
    log_id: str,
    discovery_sku: str,
) -> dict:
    async with AsyncSessionLocal() as session:
        competitor = await session.scalar(select(CompetitorModel).where(
            CompetitorModel.id == uuid.UUID(competitor_id),
            CompetitorModel.client_id == client_id,
        ))
        source = await session.scalar(select(SourceModel).where(SourceModel.id == uuid.UUID(source_id)))
        offering = await session.scalar(select(OfferingModel).where(
            OfferingModel.id == uuid.UUID(offering_id),
            OfferingModel.client_id == client_id,
        ))
        match = await session.scalar(select(OfferingMatchModel).where(OfferingMatchModel.id == uuid.UUID(match_id)))
        rule = await session.scalar(select(AlertRuleModel).where(
            AlertRuleModel.id == uuid.UUID(rule_id),
            AlertRuleModel.client_id == client_id,
        ))
        log = await session.scalar(select(AlertLogModel).where(
            AlertLogModel.id == uuid.UUID(log_id),
        ))
        snapshot = await session.scalar(select(SnapshotModel).where(
            SnapshotModel.offering_match_id == uuid.UUID(match_id),
        ))
        observation_count = await session.scalar(select(func.count()).select_from(ObservationModel).where(
            ObservationModel.offering_match_id == uuid.UUID(match_id),
        ))
        job_count = await session.scalar(select(func.count()).select_from(JobModel).where(
            JobModel.source_id == uuid.UUID(source_id),
        ))
        discovered = await session.scalar(select(OfferingModel).where(
            OfferingModel.client_id == client_id,
            OfferingModel.sku == discovery_sku,
        ))
        notification_statuses = list((await session.execute(select(
            NotificationOutboxModel.status
        ).where(
            NotificationOutboxModel.client_id == client_id,
            NotificationOutboxModel.channel == "email",
        ))).scalars().all())

        assert competitor is not None and competitor.name == COMPETITOR, "Competitor not persisted for the tenant."
        assert source is not None and source.name == SOURCE, "Source record not persisted."
        assert offering is not None and str(offering.current_price) in {"429.9900", "429.99", "429.990000"}, "Edited offering price not persisted."
        assert match is not None and match.match_status.value == "APPROVED", "Match not persisted in approved state."
        assert rule is not None and rule.is_active, "Alert rule not persisted."
        assert log is not None and log.is_read, "Read state not persisted in PostgreSQL."
        assert snapshot is not None and str(snapshot.current_price).startswith("349.99"), "Latest price snapshot missing."
        assert int(observation_count or 0) >= 2, f"Expected two durable price observations; got {observation_count}."
        assert int(job_count or 0) >= 2, f"Expected collection/discovery jobs; got {job_count}."
        assert discovered is not None, "Discovered catalog item is missing from PostgreSQL."
        assert "SENT" in notification_statuses, f"Alert email outbox did not reach SENT: {notification_statuses}"
        return {
            "tenant_id": str(client_id),
            "competitor_id": competitor_id,
            "source_id": source_id,
            "offering_id": offering_id,
            "match_id": match_id,
            "snapshot_price": str(snapshot.current_price),
            "observations": int(observation_count or 0),
            "jobs": int(job_count or 0),
            "discovered_sku": discovery_sku,
            "notification_statuses": notification_statuses,
        }


def main() -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    MAILBOX.parent.mkdir(parents=True, exist_ok=True)
    if MAILBOX.exists():
        MAILBOX.unlink()

    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        # Public entry, registration, delivered verification email, and sign-in.
        page.goto(FRONTEND_URL + "/welcome", wait_until="networkidle")
        page.get_by_role("heading", name=re.compile("Understand the market")).wait_for()
        shot(page, "00-public-homepage.png")
        page.get_by_role("link", name=re.compile("Create workspace")).click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "01-registration-form.png")
        page.get_by_label("Organization name").fill(ORGANIZATION)
        page.get_by_label("Your full name").fill(FULL_NAME)
        page.get_by_label("Work email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        before_verification = len(mailbox())
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/register") and r.request.method == "POST") as event:
            page.get_by_role("button", name=re.compile("Create workspace")).click()
        assert event.value.status == 201, event.value.text()
        assert event.value.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "02-verification-email-sent.png")
        _, verification_body = wait_mail("Verify your Nexora account", EMAIL, before_verification)
        verification_url = mail_link(verification_body, "verify-email")
        assert urlparse(verification_url).hostname == "127.0.0.1"
        page.goto(verification_url, wait_until="networkidle")
        shot(page, "03-email-verification.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/verify-email") and r.request.method == "GET") as event:
            page.get_by_role("button", name=re.compile("Verify email")).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page, "05-login.png")
        sign_in(page, EMAIL, PASSWORD)
        shot(page, "06-dashboard-empty-workspace.png")
        me = page.request.get(API_URL + "/v1/auth/me", headers={"Authorization": f"Bearer {token(page)}"})
        assert me.status == 200, me.text()
        me_payload = me.json()
        assert me_payload["role"] == "ORG_ADMIN"
        client_id = uuid.UUID(me_payload["client_id"])

        # Competitor directory, create/edit, duplicate guard and tenant boundary.
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill(COMPETITOR)
        page.get_by_label("Domain").fill("raw.githubusercontent.com")
        shot(page, "08-competitor-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/competitors") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 201, event.value.text()
        competitor_id = event.value.json()["id"]
        page.get_by_text(COMPETITOR, exact=True).wait_for()
        shot(page, "09-competitor-created.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Product Reference")
        page.get_by_label("Domain").fill("raw.githubusercontent.com")
        with page.expect_response(lambda r: r.url.endswith("/v1/competitors") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 403, event.value.text()
        page.get_by_text("A competitor with this domain already exists.").wait_for()
        shot(page, "10-duplicate-competitor-rejected.png")
        page.get_by_role("button", name="Cancel", exact=True).click()
        competitor_row = page.locator("tbody tr").filter(has_text=COMPETITOR).first
        competitor_row.locator("button").first.click()
        page.get_by_label("Name").fill("Retail Product Reference")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/competitors/{competitor_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_text("Retail Product Reference", exact=True).wait_for()

        # Add the collection source and persist operating configuration.
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("button", name="Add source", exact=True).click()
        choose(page, "Competitor", "Retail Product Reference")
        page.get_by_label("Source name").fill(SOURCE)
        page.get_by_label("Base URL").fill(PRICE_HIGH_URL)
        choose(page, "Source type", "OFFICIAL_STORE")
        choose(page, "Collection method", "HTTP_FAST")
        shot(page, "11-source-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/sources") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create source", exact=True).click()
        assert event.value.status == 201, event.value.text()
        source_id = event.value.json()["id"]
        page.get_by_text(SOURCE, exact=True).wait_for()
        shot(page, "12-source-created.png")
        page.get_by_title("Configuration", exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page, "13-source-configuration-form.png")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/sources/{source_id}/configuration") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save configuration", exact=True).click()
        assert event.value.status == 200, event.value.text()
        source_config = page.request.get(API_URL + f"/v1/sources/{source_id}", headers={"Authorization": f"Bearer {token(page)}"})
        assert source_config.status == 200 and source_config.json()["configuration"]["max_retries"] == 1
        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill(SOURCE)
        with page.expect_response(lambda r: r.url.endswith(f"/v1/sources/{source_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        shot(page, "14-source-configuration-saved.png")

        # Catalog CRUD, details/history, monitoring, dynamic fields, bulk import/export/archive.
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("button", name="Add offering", exact=True).click()
        page.get_by_label("Name").fill(OFFERING)
        choose(page, "Offering type", "PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page, "15-offering-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create offering", exact=True).click()
        assert event.value.status == 201, event.value.text()
        offering_id = event.value.json()["id"]
        page.get_by_text(OFFERING, exact=True).wait_for()
        shot(page, "16-offering-created.png")
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_role("button", name=OFFERING, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "17-offering-details.png")
        page.keyboard.press("Escape")
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        for _ in range(2):
            row = page.locator("tbody tr").filter(has_text=OFFERING).first
            with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method == "POST") as event:
                row.get_by_title("Toggle monitoring", exact=True).click()
            assert event.value.status == 200, event.value.text()
        assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is True
        shot(page, "18-offering-monitoring.png")
        page.get_by_role("button", name="Dynamic fields", exact=True).click()
        page.get_by_text("Dynamic field definitions", exact=True).wait_for()
        shot(page, "19-dynamic-fields.png")
        page.get_by_role("button", name="Add field", exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months")
        page.get_by_label("Category").fill("Commercial")
        shot(page, "20-dynamic-field-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/fields") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create field", exact=True).click()
        assert event.value.status == 201, event.value.text()
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "21-dynamic-field-created.png")
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(lambda r: "/v1/offerings/fields/" in r.url and r.request.method == "DELETE") as event:
            field_row.locator("button.danger-icon").click()
        assert event.value.status == 204, event.value.text()
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")

        page.get_by_role("button", name="Bulk import", exact=True).click()
        bulk_items = [
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}},
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk_items))
        shot(page, "22-bulk-import-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/bulk") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Import offerings", exact=True).click()
        assert event.value.status == 200 and event.value.json()["successful_count"] == 2, event.value.text()
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        shot(page, "23-bulk-imported-catalog.png")
        for label, filename in [("CSV","/tmp/nexora-export.csv"),("JSON","/tmp/nexora-export.json")]:
            with page.expect_download(timeout=20000) as event:
                page.get_by_role("button", name=label, exact=True).click()
            event.value.save_as(filename)
            assert Path(filename).stat().st_size > 0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/bulk-archive") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        assert event.value.status == 200 and event.value.json()["archived_count"] == 1
        archive_readback = api_get(page, "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")
        assert any(row["is_archived"] for row in archive_readback["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING, exact=True).wait_for()
        shot(page, "24-catalog-import-export-archive.png")

        # Offer-to-competitor mapping, real HTTP scrape, persisted observations and snapshot.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("button", name="Add match", exact=True).click()
        choose(page, "Offering", OFFERING)
        choose(page, "Source", SOURCE)
        page.get_by_label("Competitor target URL").fill(PRICE_HIGH_URL)
        choose(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "25-match-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offering-matches") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create match", exact=True).click()
        assert event.value.status == 201, event.value.text()
        match_id = event.value.json()["id"]
        page.get_by_text(PRICE_HIGH_URL, exact=False).wait_for()
        shot(page, "26-match-created.png")
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        with page.expect_response(lambda r: "/v1/collections/run" in r.url and r.request.method == "POST") as event:
            row.get_by_title("Run collection", exact=True).click()
        first = event.value
        assert first.status in (200, 201), first.text()
        first_data = first.json()
        assert first_data["status"] == "COMPLETED", first_data
        assert first_data["observation"]["price"] == "399.99", first_data
        assert first_data["observation"]["http_status_code"] == 200, first_data
        assert api_get(page, f"/v1/offerings/{offering_id}/history"), "First collection history missing."
        shot(page, "27-first-price-observation.png")

        # An actual alert rule is triggered by the next lower HTTP-collected price.
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page, "Alert type", "PERCENTAGE_DROP")
        choose(page, "Offering scope", OFFERING)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "28-alert-rule-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/alerts/rules") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create rule", exact=True).click()
        assert event.value.status == 201, event.value.text()
        rule_id = event.value.json()["id"]
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "29-alert-rule-created.png")
        page.get_by_role("link", name="Matches", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(PRICE_LOW_URL)
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offering-matches/{match_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        with page.expect_response(lambda r: "/v1/collections/run" in r.url and r.request.method == "POST") as event:
            row.get_by_title("Run collection", exact=True).click()
        second = event.value
        assert second.status in (200, 201), second.text()
        second_data = second.json()
        assert second_data["observation"]["price"] == "349.99", second_data
        assert second_data["snapshot"]["previous_price"] == "399.99", second_data
        assert second_data["snapshot"]["percentage_difference"].startswith("-12.50%"), second_data
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=20000)
        logs = api_get(page, "/v1/alerts/logs")
        alert = next((item for item in logs if item["title"] == "Competitor price dropped"), None)
        assert alert is not None and alert["is_read"] is False, logs
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        logs = api_get(page, "/v1/alerts/logs")
        assert any(item["id"] == alert["id"] and item["is_read"] for item in logs)
        shot(page, "30-alert-triggered-and-read.png")

        # The delivery worker sends the real alert outbox item to an isolated STARTTLS inbox.
        before_alert_email = len(mailbox())
        worker_response = dispatch_pending_notifications.run()
        assert int(worker_response.get("sent", 0)) >= 1, worker_response
        _, alert_body = wait_mail("Nexora Competitive Alert: Competitor price dropped", EMAIL, before_alert_email)
        assert "Nexora detected a competitive intelligence event" in alert_body
        shot(page, "31-alert-email-delivered.png")

        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_role("heading", name="Jobs", exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH", exact=False).wait_for()
        shot(page, "32-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "33-job-detail.png")
        page.keyboard.press("Escape")

        # SSRF boundary and structured-data discovery/upsert.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert event.value.status == 422, event.value.text()
        shot(page, "34-private-discovery-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        choose(page, "Default type", "PRODUCT")
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        discovery_response = event.value
        assert discovery_response.status == 200, discovery_response.text()
        discovery = discovery_response.json()
        assert discovery["status"] == "COMPLETED", discovery
        assert discovery["created_offerings_count"] == 1, discovery
        assert discovery["items"][0]["name"] == DISCOVERED_OFFERING, discovery
        page.get_by_text(DISCOVERED_OFFERING, exact=True).wait_for(timeout=20000)
        shot(page, "35-discovery-created.png")
        job_id = discovery["job_id"]
        readback = api_get(page, f"/v1/discovery/jobs/{job_id}")
        assert readback["items"][0]["sku"] == "WF1000XM5-BLK"
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        again = event.value
        assert again.status == 200, again.text()
        assert again.json()["created_offerings_count"] == 0 and again.json()["updated_offerings_count"] == 1, again.text()
        listed = api_get(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(item["sku"] == "WF1000XM5-BLK" for item in listed["items"]) == 1
        shot(page, "36-discovery-upsert.png")

        # Workspace settings persist independently and password rotation invalidates old credentials.
        page.locator("a.sidebar-status").click()
        page.get_by_label("Company name").wait_for()
        shot(page, "37-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/profile") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save profile", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert api_get(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"
        shot(page, "38-workspace-profile-saved.png")
        page.get_by_label("Current password").fill(PASSWORD)
        page.get_by_label("New password").fill(CHANGED_PASSWORD)
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/change-password") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert event.value.status == 200, event.value.text()
        old_login = page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert old_login.status == 401, old_login.text()
        new_login = page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": CHANGED_PASSWORD})
        assert new_login.status == 200, new_login.text()
        shot(page, "39-password-changed.png")

        # Reset link is delivered, consumed once, then the new password signs in via UI.
        sign_out(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        before_reset_mail = len(mailbox())
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/forgot-password") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Send reset link", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "40-reset-requested.png")
        _, reset_body = wait_mail("Reset your Nexora password", EMAIL, before_reset_mail)
        reset_url = mail_link(reset_body, "reset-password")
        page.goto(reset_url, wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        shot(page, "41-reset-form.png")
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/reset-password") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "42-reset-completed.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        sign_in(page, EMAIL, RESET_PASSWORD)
        shot(page, "43-dashboard-after-reset.png")

        # Real persisted role users: analyst can read/run; viewer is read-only.
        role_suffix = uuid.uuid4().hex[:8]
        analyst = asyncio.run(create_role_user(client_id, RoleEnum.ANALYST, role_suffix))
        viewer = asyncio.run(create_role_user(client_id, RoleEnum.VIEWER, role_suffix))
        sign_out(page)
        sign_in(page, analyst, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        denied = page.request.post(API_URL + "/v1/competitors",
            headers={"Authorization": f"Bearer {token(page)}"},
            json={"name":"Unauthorized Analyst Write","domain":"forbidden-analyst.example"})
        assert denied.status == 403, denied.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name="Run discovery", exact=True).is_visible()
        shot(page, "44-analyst-workspace.png")
        sign_out(page)
        sign_in(page, viewer, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        denied = page.request.post(API_URL + "/v1/competitors",
            headers={"Authorization": f"Bearer {token(page)}"},
            json={"name":"Unauthorized Viewer Write","domain":"forbidden-viewer.example"})
        assert denied.status == 403, denied.text()
        shot(page, "45-viewer-read-only.png")

        # Responsive public page and final direct PostgreSQL evidence.
        mobile = context.new_page()
        mobile.set_viewport_size({"width":390,"height":844})
        mobile.goto(FRONTEND_URL + "/welcome", wait_until="networkidle")
        dimensions = mobile.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        assert dimensions["content"] <= dimensions["viewport"], dimensions
        shot(mobile, "46-mobile-homepage.png")

        persisted = asyncio.run(verify_postgres(
            client_id, competitor_id, source_id, offering_id, match_id, rule_id,
            alert["id"], "WF1000XM5-BLK",
        ))
        (SCREENSHOTS / "postgres-evidence.json").write_text(json.dumps(persisted, indent=2), encoding="utf-8")
        assert not browser_errors, "Browser JavaScript errors: " + " | ".join(browser_errors)
        expected = [
            "00-public-homepage.png","01-registration-form.png","02-verification-email-sent.png",
            "03-email-verification.png","04-email-verified.png","05-login.png","06-dashboard-empty-workspace.png",
            "07-competitors.png","08-competitor-form.png","09-competitor-created.png","10-duplicate-competitor-rejected.png",
            "11-source-form.png","12-source-created.png","13-source-configuration-form.png","14-source-configuration-saved.png",
            "15-offering-form.png","16-offering-created.png","17-offering-details.png","18-offering-monitoring.png",
            "19-dynamic-fields.png","20-dynamic-field-form.png","21-dynamic-field-created.png","22-bulk-import-form.png",
            "23-bulk-imported-catalog.png","24-catalog-import-export-archive.png","25-match-form.png","26-match-created.png",
            "27-first-price-observation.png","28-alert-rule-form.png","29-alert-rule-created.png",
            "30-alert-triggered-and-read.png","31-alert-email-delivered.png","32-job-history.png","33-job-detail.png",
            "34-private-discovery-target-blocked.png","35-discovery-created.png","36-discovery-upsert.png",
            "37-workspace-settings.png","38-workspace-profile-saved.png","39-password-changed.png",
            "40-reset-requested.png","41-reset-form.png","42-reset-completed.png","43-dashboard-after-reset.png",
            "44-analyst-workspace.png","45-viewer-read-only.png","46-mobile-homepage.png",
        ]
        missing = [name for name in expected if not (SCREENSHOTS / name).is_file()]
        assert not missing, f"Screenshots missing: {missing}"
        print(json.dumps({"journey":"passed","screenshots":len(expected),"postgres":persisted}, indent=2), flush=True)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

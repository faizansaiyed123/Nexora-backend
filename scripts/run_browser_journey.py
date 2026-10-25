"""Browser journey for the Nexora product walkthrough.

Runs the real React app against the real FastAPI service and disposable PostgreSQL
records. Application API requests are never intercepted or replaced with mocks.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy import select

from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications

BASE_URL = os.getenv("NEXORA_FRONTEND_URL", "http://localhost:4173").rstrip("/")
API_URL = os.getenv("NEXORA_API_URL", "http://localhost:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SCREENSHOTS = Path(os.getenv("NEXORA_SCREENSHOTS_DIR", "docs/testing/visuals"))
EMAIL = "market-analyst@northstar.example.com"
INITIAL_PASSWORD = "Nexora-Strong!2026"
UPDATED_PASSWORD = "Nexora-Updated!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
ORGANIZATION = "Northstar Market Intelligence"
FULL_NAME = "Alex Morgan"
COMPETITOR_NAME = "Sony Retail Reference"
SOURCE_NAME = "Sony Product Page"
OFFERING_NAME = "Sony WH-1000XM5 Wireless Headphones"
DISCOVERED_NAME = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
HIGH_URL = (
    "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/"
    "tests/fixtures/retail-price-high.html"
)
LOW_URL = (
    "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/"
    "tests/fixtures/retail-price-low.html"
)
DISCOVERY_URL = (
    "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/"
    "tests/fixtures/retail-discovery-product.html"
)
javascript_errors: list[str] = []


def shot(page: Page, filename: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(SCREENSHOTS / filename),
        full_page=True,
        animations="disabled",
        caret="hide",
    )
    print(f"Screenshot: {filename}", flush=True)


def choose(page: Page, label: str, value: str) -> None:
    field = page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option", name=value, exact=True).last.click()


def browser_token(page: Page) -> str:
    token = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert token, "Expected a real browser access token."
    return str(token)


def api_get(page: Page, path: str, expected: int = 200):
    response = page.request.get(
        API_URL + path,
        headers={"Authorization": f"Bearer {browser_token(page)}"},
    )
    assert response.status == expected, f"GET {path}: {response.status} {response.text()}"
    return response.json()


def wait_for_message(subject_part: str, recipient: str, offset: int, timeout: int = 40):
    def read_messages():
        if not MAILBOX.exists():
            return []
        rows = []
        for line in MAILBOX.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("data"):
                rows.append(row)
        return rows

    def parts(row):
        message = message_from_string(str(row["data"]))
        if message.is_multipart():
            pieces = []
            for part in message.walk():
                if part.get_content_type() == "text/plain":
                    raw = part.get_payload(decode=True)
                    if raw:
                        pieces.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
            body = "\n".join(pieces)
        else:
            raw = message.get_payload(decode=True)
            body = raw.decode(message.get_content_charset() or "utf-8", "replace") if raw else str(message.get_payload())
        return str(message.get("Subject", "")), body, [str(x).lower() for x in row.get("rcpt_tos", [])]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = read_messages()
        for row in rows[offset:]:
            subject, body, recipients = parts(row)
            if subject_part.lower() in subject.lower() and recipient.lower() in recipients:
                return row, body
        time.sleep(0.25)
    recent = [parts(row)[0] for row in read_messages()[offset:]]
    raise AssertionError(f"Email {subject_part!r} not delivered to {recipient}; recent subjects: {recent}")


def link_from_email(body: str, route: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body)
    assert match, f"Expected a {route} link in the delivered message."
    return match.group(0).rstrip(".,);")


def in_worker(callable_):
    # Database and worker helpers have their own event loop, separate from Playwright.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(callable_).result()


async def create_role_user(role: RoleEnum, client_id: uuid.UUID, suffix: str) -> str:
    email = f"{role.value.lower()}-{suffix}@northstar.example.com"
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.email == email))
        if user:
            return email
        session.add(
            UserModel(
                client_id=client_id,
                email=email,
                full_name=f"Northstar {role.value.title()}",
                hashed_password=get_password_hash(ROLE_PASSWORD),
                role=role,
                is_active=True,
                email_verified=True,
            )
        )
        await session.commit()
    return email


async def outbox_statuses(client_id: uuid.UUID) -> list[str]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(NotificationOutboxModel.status).where(
                NotificationOutboxModel.client_id == client_id,
                NotificationOutboxModel.channel == "email",
            )
        )
        return list(rows.scalars().all())


def login(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(
        lambda r: r.url.endswith("/v1/auth/login") and r.request.method == "POST"
    ) as event:
        page.get_by_role("button", name=re.compile("Sign in")).click()
    assert event.value.status == 200, event.value.text()
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=15000)


def sign_out(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=12000)


def main() -> None:
    if MAILBOX.exists():
        MAILBOX.unlink()
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    competitor_name = COMPETITOR_NAME
    source_name = SOURCE_NAME

    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            accept_downloads=True,
            device_scale_factor=1,
        )
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: javascript_errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        # Public entry and email-backed account creation.
        page.goto(BASE_URL + "/welcome", wait_until="networkidle")
        page.get_by_role("heading", name=re.compile("Understand the market")).wait_for()
        shot(page, "00-public-homepage.png")
        page.get_by_role("link", name=re.compile("Create workspace")).click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "01-registration-form.png")
        page.get_by_label("Organization name").fill(ORGANIZATION)
        page.get_by_label("Your full name").fill(FULL_NAME)
        page.get_by_label("Work email").fill(EMAIL)
        page.get_by_label("Password").fill(INITIAL_PASSWORD)
        mail_offset = len(MAILBOX.read_text().splitlines()) if MAILBOX.exists() else 0
        with page.expect_response(
            lambda r: r.url.endswith("/v1/auth/register") and r.request.method == "POST"
        ) as registration_event:
            page.get_by_role("button", name=re.compile("Create workspace")).click()
        assert registration_event.value.status == 201, registration_event.value.text()
        assert registration_event.value.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "02-verification-email-sent.png")
        _, verification_body = wait_for_message("Verify your Nexora account", EMAIL, mail_offset)
        verification_url = link_from_email(verification_body, "verify-email")
        assert urlparse(verification_url).path.endswith("/verify-email")
        page.goto(verification_url, wait_until="networkidle")
        shot(page, "03-email-verification-page.png")
        with page.expect_response(
            lambda r: r.url.endswith("/v1/auth/verify-email") and r.request.method == "GET"
        ) as verify_event:
            page.get_by_role("button", name=re.compile("Verify email")).click()
        assert verify_event.value.status == 200, verify_event.value.text()
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page, "05-login.png")
        login(page, EMAIL, INITIAL_PASSWORD)
        shot(page, "06-dashboard-empty-workspace.png")
        me = api_get(page, "/v1/auth/me")
        assert me["role"] == "ORG_ADMIN"
        client_id = uuid.UUID(me["client_id"])

        # Competitor management and duplicate protection.
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors-empty.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill(competitor_name)
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: r.url.endswith("/v1/competitors") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 201, event.value.text()
        competitor = event.value.json()
        competitor_id = competitor["id"]
        page.get_by_text(competitor_name, exact=True).wait_for()
        shot(page, "08-competitor-created.png")

        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: r.url.endswith("/v1/competitors") and r.request.method == "POST") as duplicate_event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert duplicate_event.value.status == 403, duplicate_event.value.text()
        page.get_by_text("A competitor with this domain already exists.").wait_for()
        shot(page, "09-duplicate-domain-rejected.png")
        page.get_by_role("button", name="Cancel", exact=True).click()

        row = page.locator("tbody tr").filter(has_text=competitor_name).first
        row.locator("button").first.click()
        page.get_by_label("Name").fill("Sony Retail Reference")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/competitors/{competitor_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 200, event.value.text()
        competitor_name = "Sony Retail Reference"
        assert any(r["id"] == competitor_id and r["name"] == competitor_name for r in api_get(page, "/v1/competitors"))

        # Source creation, safe collection settings, and source editing.
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("heading", name="Sources", exact=True).wait_for()
        page.get_by_role("button", name="Add source", exact=True).click()
        choose(page, "Competitor", competitor_name)
        page.get_by_label("Source name").fill(source_name)
        page.get_by_label("Base URL").fill(HIGH_URL)
        choose(page, "Source type", "OFFICIAL_STORE")
        choose(page, "Collection method", "HTTP_FAST")
        shot(page, "10-source-creation-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/sources") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create source", exact=True).click()
        assert event.value.status == 201, event.value.text()
        source_id = event.value.json()["id"]
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "11-sources-created.png")

        page.get_by_title("Configuration", exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page, "12-source-configuration.png")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/sources/{source_id}/configuration") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save configuration", exact=True).click()
        assert event.value.status == 200, event.value.text()
        configured_source = api_get(page, f"/v1/sources/{source_id}")
        assert configured_source["configuration"]["rate_limit_rpm"] == 60
        assert configured_source["configuration"]["max_retries"] == 1

        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill(source_name + " — Structured")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/sources/{source_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        source_name = source_name + " — Structured"
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "13-source-configuration-saved.png")

        # Catalog lifecycle, dynamic attributes, bulk import/export and archive.
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("heading", name="Offerings", exact=True).wait_for()
        page.get_by_role("button", name="Add offering", exact=True).click()
        page.get_by_label("Name").fill(OFFERING_NAME)
        choose(page, "Offering type", "PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page, "14-offering-creation-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create offering", exact=True).click()
        assert event.value.status == 201, event.value.text()
        offering_id = event.value.json()["id"]
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "15-catalog-offering-created.png")

        page.locator("tbody tr").filter(has_text=OFFERING_NAME).first.get_by_role("button", name=OFFERING_NAME, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "16-offering-detail-and-history.png")
        page.keyboard.press("Escape")

        page.locator("tbody tr").filter(has_text=OFFERING_NAME).first.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert float(api_get(page, f"/v1/offerings/{offering_id}")["current_price"]) == 429.99

        for desired_state in (False, True):
            row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
            with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method == "POST") as event:
                row.get_by_title("Toggle monitoring", exact=True).click()
            assert event.value.status == 200, event.value.text()
            assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is desired_state
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
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/fields") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create field", exact=True).click()
        assert event.value.status == 201, event.value.text()
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "20-dynamic-field-created.png")
        with page.expect_response(lambda r: "/v1/offerings/fields/" in r.url and r.request.method == "DELETE") as event:
            page.locator("div.list-row").filter(has_text="Review Cycle").last.locator("button.danger-icon").click()
        assert event.value.status == 204, event.value.text()
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")

        page.get_by_role("button", name="Bulk import", exact=True).click()
        page.get_by_label("Offering JSON").fill(json.dumps([
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}},
        ]))
        shot(page, "21-bulk-import-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/bulk") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Import offerings", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert event.value.json()["successful_count"] == 2, event.value.text()
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        shot(page, "22-bulk-imported-catalog.png")
        for fmt in ("CSV", "JSON"):
            with page.expect_download(timeout=20000) as download_event:
                page.get_by_role("button", name=fmt, exact=True).click()
            target = Path("/tmp") / f"nexora-catalog.{fmt.lower()}"
            download_event.value.save_as(str(target))
            assert target.is_file() and target.stat().st_size > 0

        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/bulk-archive") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert event.value.json()["archived_count"] == 1
        archived = api_get(page, "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")
        assert any(row["is_archived"] for row in archived["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "23-catalog-import-export-and-archive.png")

        # Match the catalogue offering to a collection source.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("heading", name="Offering matches", exact=True).wait_for()
        page.get_by_role("button", name="Add match", exact=True).click()
        choose(page, "Offering", OFFERING_NAME)
        choose(page, "Source", source_name)
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        choose(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "24-offering-match-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offering-matches") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create match", exact=True).click()
        assert event.value.status == 201, event.value.text()
        match_id = event.value.json()["id"]
        page.get_by_text(HIGH_URL, exact=False).wait_for()
        match_row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        shot(page, "25-offering-match-created.png")

        with page.expect_response(lambda r: "/v1/collections/run" in r.url and r.request.method == "POST") as event:
            match_row.get_by_title("Run collection", exact=True).click()
        first = event.value
        assert first.status in (200, 201), first.text()
        first_payload = first.json()
        assert first_payload["status"] == "COMPLETED", first_payload
        assert first_payload["observation"]["price"] == "399.99", first_payload
        assert first_payload["observation"]["http_status_code"] == 200, first_payload
        assert len(api_get(page, f"/v1/offerings/{offering_id}/history")) >= 1
        shot(page, "26-first-price-observation.png")

        # Alert rule, lower second observation, durable notification, and read state.
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("heading", name="Alerts", exact=True).wait_for()
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page, "Alert type", "PERCENTAGE_DROP")
        choose(page, "Offering scope", OFFERING_NAME)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "27-alert-rule-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/alerts/rules") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create rule", exact=True).click()
        assert event.value.status == 201, event.value.text()
        rule_id = event.value.json()["id"]
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "28-alert-rule-created.png")

        page.get_by_role("link", name="Matches", exact=True).click()
        page.locator("tbody tr").filter(has_text=OFFERING_NAME).first.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offering-matches/{match_id}") and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: "/v1/collections/run" in r.url and r.request.method == "POST") as event:
            row.get_by_title("Run collection", exact=True).click()
        second = event.value
        assert second.status in (200, 201), second.text()
        second_payload = second.json()
        assert second_payload["observation"]["price"] == "349.99", second_payload
        assert second_payload["snapshot"]["previous_price"] == "399.99", second_payload
        assert second_payload["snapshot"]["percentage_difference"].startswith("-12.50%"), second_payload

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        logs = api_get(page, "/v1/alerts/logs")
        log = next((item for item in logs if item["title"] == "Competitor price dropped"), None)
        assert log and not log["is_read"], logs
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        assert any(item["id"] == log["id"] and item["is_read"] for item in api_get(page, "/v1/alerts/logs"))
        shot(page, "29-alert-generated-and-read.png")

        mail_offset = len(MAILBOX.read_text().splitlines()) if MAILBOX.exists() else 0
        dispatch_result = in_worker(dispatch_pending_notifications.run)
        assert int(dispatch_result.get("sent", 0)) >= 1, dispatch_result
        _, alert_body = wait_for_message("Nexora Competitive Alert: Competitor price dropped", EMAIL, mail_offset)
        assert "Nexora detected a competitive intelligence event" in alert_body
        outbox = in_worker(lambda: asyncio.run(outbox_statuses(client_id)))
        assert "SENT" in outbox, outbox

        # Jobs are the persistent execution history behind the collection feed.
        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_role("heading", name="Jobs", exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH", exact=False).wait_for()
        shot(page, "30-collection-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "31-collection-job-detail.png")
        page.keyboard.press("Escape")

        # SSRF boundary followed by structured website discovery and idempotent upsert.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_role("heading", name="Website discovery", exact=True).wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert event.value.status == 422, event.value.text()
        shot(page, "32-private-target-blocked.png")

        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        choose(page, "Default type", "PRODUCT")
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        discovery = event.value
        assert discovery.status == 200, discovery.text()
        payload = discovery.json()
        assert payload["status"] == "COMPLETED", payload
        assert payload["created_offerings_count"] == 1, payload
        assert payload["items"][0]["name"] == DISCOVERED_NAME, payload
        page.get_by_text(DISCOVERED_NAME, exact=True).wait_for(timeout=15000)
        shot(page, "33-discovery-product-created.png")
        discovery_job = api_get(page, f"/v1/discovery/jobs/{payload['job_id']}")
        assert discovery_job["items"][0]["sku"] == "WF1000XM5-BLK"

        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        update = event.value
        assert update.status == 200, update.text()
        update_payload = update.json()
        assert update_payload["created_offerings_count"] == 0 and update_payload["updated_offerings_count"] == 1, update_payload
        catalog = api_get(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(1 for item in catalog["items"] if item["sku"] == "WF1000XM5-BLK") == 1
        shot(page, "34-discovery-upsert-history.png")

        # Workspace profile and password lifecycle.
        page.locator(".sidebar-status").click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        shot(page, "35-workspace-settings.png")
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
        shot(page, "36-workspace-profile-updated.png")

        page.get_by_label("Current password").fill(INITIAL_PASSWORD)
        page.get_by_label("New password").fill(UPDATED_PASSWORD)
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/change-password") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert event.value.status == 200, event.value.text()
        old_login = page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": INITIAL_PASSWORD})
        assert old_login.status == 401, old_login.text()
        new_login = page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": UPDATED_PASSWORD})
        assert new_login.status == 200, new_login.text()
        shot(page, "37-password-changed.png")

        # Change-password revokes the old sessions. Start a fresh UI session before recovery.
        page.evaluate("() => { localStorage.removeItem('nexora.access'); localStorage.removeItem('nexora.refresh'); localStorage.removeItem('nexora.user'); }")
        page.goto(BASE_URL + "/login", wait_until="networkidle")
        login(page, EMAIL, UPDATED_PASSWORD)
        sign_out(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        mail_offset = len(MAILBOX.read_text().splitlines()) if MAILBOX.exists() else 0
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/forgot-password") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Send reset link", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "38-password-reset-requested.png")
        _, reset_body = wait_for_message("Reset your Nexora password", EMAIL, mail_offset)
        reset_url = link_from_email(reset_body, "reset-password")
        page.goto(reset_url, wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        shot(page, "39-password-reset-form.png")
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/reset-password") and r.request.method == "POST") as event:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "40-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login(page, EMAIL, RESET_PASSWORD)
        shot(page, "41-dashboard-after-password-reset.png")

        # Persisted role accounts exercise both visible controls and server-side authorization.
        viewer_email = in_worker(lambda: asyncio.run(create_role_user(RoleEnum.VIEWER, client_id, "workspace")))
        analyst_email = in_worker(lambda: asyncio.run(create_role_user(RoleEnum.ANALYST, client_id, "workspace")))
        sign_out(page)
        login(page, analyst_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        analyst_write = page.request.post(
            API_URL + "/v1/competitors",
            headers={"Authorization": f"Bearer {browser_token(page)}"},
            json={"name": "Not Allowed", "domain": "forbidden-analyst.example"},
        )
        assert analyst_write.status == 403, analyst_write.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name="Run discovery", exact=True).is_visible()
        shot(page, "42-analyst-workspace.png")

        sign_out(page)
        login(page, viewer_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        viewer_write = page.request.post(
            API_URL + "/v1/competitors",
            headers={"Authorization": f"Bearer {browser_token(page)}"},
            json={"name": "Viewer Write Attempt", "domain": "viewer-write.example"},
        )
        assert viewer_write.status == 403, viewer_write.text()
        shot(page, "43-viewer-read-only-workspace.png")

        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(BASE_URL + "/welcome", wait_until="networkidle")
        dimensions = mobile.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        assert dimensions["content"] <= dimensions["viewport"], dimensions
        shot(mobile, "44-mobile-homepage.png")

        assert not javascript_errors, "Browser JavaScript errors: " + " | ".join(javascript_errors)
        expected = [
            "00-public-homepage.png", "01-registration-form.png", "02-verification-email-sent.png",
            "03-email-verification-page.png", "04-email-verified.png", "05-login.png",
            "06-dashboard-empty-workspace.png", "07-competitors-empty.png", "08-competitor-created.png",
            "09-duplicate-domain-rejected.png", "10-source-creation-form.png", "11-sources-created.png",
            "12-source-configuration.png", "13-source-configuration-saved.png", "14-offering-creation-form.png",
            "15-catalog-offering-created.png", "16-offering-detail-and-history.png", "17-catalog-monitoring-active.png",
            "18-dynamic-fields.png", "19-dynamic-field-form.png", "20-dynamic-field-created.png",
            "21-bulk-import-form.png", "22-bulk-imported-catalog.png", "23-catalog-import-export-and-archive.png",
            "24-offering-match-form.png", "25-offering-match-created.png", "26-first-price-observation.png",
            "27-alert-rule-form.png", "28-alert-rule-created.png", "29-alert-generated-and-read.png",
            "30-collection-job-history.png", "31-collection-job-detail.png", "32-private-target-blocked.png",
            "33-discovery-product-created.png", "34-discovery-upsert-history.png", "35-workspace-settings.png",
            "36-workspace-profile-updated.png", "37-password-changed.png", "38-password-reset-requested.png",
            "39-password-reset-form.png", "40-password-reset-complete.png", "41-dashboard-after-password-reset.png",
            "42-analyst-workspace.png", "43-viewer-read-only-workspace.png", "44-mobile-homepage.png",
        ]
        missing = [name for name in expected if not (SCREENSHOTS / name).is_file()]
        assert not missing, f"Walkthrough images not created: {missing}"
        print(f"Completed the Nexora UI-to-API-to-PostgreSQL journey; {len(expected)} screenshots captured.", flush=True)
        browser.close()
        context.close()


if __name__ == "__main__":
    main()

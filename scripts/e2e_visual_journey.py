from __future__ import annotations

"""Real Nexora browser workflow: visible React UI -> FastAPI -> disposable PostgreSQL.

No application API calls are intercepted. Public product fixtures are fetched through
Nexora's real HTTP collection/discovery clients. CI runs this against an isolated DB,
Redis and a local STARTTLS email sink; it does not use production credentials.
"""
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
from sqlalchemy import select

from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal, engine
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications


FRONTEND = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SHOTS = Path("docs/walkthrough/screenshots")
EMAIL = "market-analyst@northstar.example.com"
PASSWORD = "Nexora-Strong!2026"
CHANGED_PASSWORD = "Nexora-Updated!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
ORG = "Northstar Market Intelligence"
FULL_NAME = "Alex Morgan"
HIGH_URL = "https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures/retail-price-high.html"
LOW_URL = "https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures/retail-price-low.html"
DISCOVERY_URL = "https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures/retail-discovery-product.html"
OFFERING_NAME = "Sony WH-1000XM5 Wireless Headphones"
DISCOVERED_NAME = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
browser_errors: list[str] = []


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / name), full_page=True, animations="disabled", caret="hide")
    print(f"SCREENSHOT {name}", flush=True)


def select_option(page: Page, label: str, option: str) -> None:
    field = page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option", name=option, exact=True).last.click()


def token(page: Page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "Expected a real authenticated browser session."
    return str(value)


def api_get(page: Page, path: str, expected: int = 200):
    response = page.request.get(API + path, headers={"Authorization": f"Bearer {token(page)}"})
    assert response.status == expected, f"GET {path}: {response.status} {response.text()}"
    return response.json()


def messages() -> list[dict]:
    if not MAILBOX.exists():
        return []
    rows = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("data"):
                rows.append(value)
        except json.JSONDecodeError:
            pass
    return rows


def decode_message(row: dict) -> tuple[str, str, list[str]]:
    message = message_from_string(str(row["data"]))
    subject = str(message.get("Subject", ""))
    if message.is_multipart():
        body_parts = []
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    body_parts.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        body = "\n".join(body_parts)
    else:
        raw = message.get_payload(decode=True)
        body = raw.decode(message.get_content_charset() or "utf-8", "replace") if raw else str(message.get_payload())
    recipients = [str(address).lower() for address in row.get("rcpt_tos", [])]
    return subject, body, recipients


def wait_email(subject_fragment: str, recipient: str, start: int, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in messages()[start:]:
            subject, body, recipients = decode_message(row)
            if subject_fragment.lower() in subject.lower() and recipient.lower() in recipients:
                return body
        time.sleep(0.25)
    received = [decode_message(row)[0] for row in messages()[start:]]
    raise AssertionError(f"Email {subject_fragment!r} not delivered to {recipient!r}; received: {received}")


def extract_link(body: str, path: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(path)}\?token=[^\s<>]+", body)
    assert match, f"Expected one-time {path} URL in delivered email."
    return match.group(0).rstrip(".,);")


def login(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/login" and r.request.method == "POST") as event:
        page.get_by_role("button", name=re.compile("Sign in")).click()
    assert event.value.status == 200, event.value.text()
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=15000)


def logout(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=10000)


async def create_role_user(client_id: uuid.UUID, role: RoleEnum, suffix: str) -> str:
    email = f"{role.value.lower()}-{suffix}@northstar.example.com"
    try:
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(select(UserModel).where(UserModel.email == email))
            if existing:
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
    if MAILBOX.exists():
        MAILBOX.unlink()

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.on("dialog", lambda dialog: dialog.accept())

        # Public signup -> actual SMTP verification email -> one-time verification.
        page.goto(FRONTEND + "/welcome", wait_until="networkidle")
        page.get_by_role("heading", name=re.compile("Understand the market")).wait_for()
        shot(page, "00-public-homepage.png")
        page.get_by_role("link", name=re.compile("Create workspace")).first.click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "01-registration-form.png")
        page.get_by_label("Organization name").fill(ORG)
        page.get_by_label("Your full name").fill(FULL_NAME)
        page.get_by_label("Work email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        mail_start = len(messages())
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/register" and r.request.method == "POST") as event:
            page.get_by_role("button", name=re.compile("Create workspace")).click()
        assert event.value.status == 201, event.value.text()
        assert event.value.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "02-verification-email-sent.png")
        body = wait_email("Verify your Nexora account", EMAIL, mail_start)
        page.goto(extract_link(body, "verify-email"), wait_until="networkidle")
        shot(page, "03-email-verification-page.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/verify-email" and r.request.method == "GET") as event:
            page.get_by_role("button", name=re.compile("Verify email")).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page, "05-login.png")
        login(page, EMAIL, PASSWORD)
        shot(page, "06-dashboard-empty-workspace.png")
        me = api_get(page, "/v1/auth/me")
        assert me["role"] == "ORG_ADMIN"
        client_id = uuid.UUID(me["client_id"])

        # Competitor lifecycle and duplicate-domain rule.
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors-empty.png")
        page.get_by_role("button", name="Add competitor").click()
        page.get_by_label("Name").fill("Retail Product Reference")
        page.get_by_label("Domain").fill("raw.githubusercontent.com")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/competitors" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Save competitor").click()
        assert event.value.status == 201, event.value.text()
        competitor = event.value.json()
        competitor_id = competitor["id"]
        page.get_by_text("Retail Product Reference", exact=True).wait_for()
        shot(page, "08-competitor-created.png")
        page.get_by_role("button", name="Add competitor").click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("raw.githubusercontent.com")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/competitors" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Save competitor").click()
        assert event.value.status == 403, event.value.text()
        shot(page, "09-duplicate-domain-rejected.png")
        page.get_by_role("button", name="Cancel", exact=True).click()

        row = page.locator("tbody tr").filter(has_text="Retail Product Reference").first
        row.locator("button").first.click()
        page.get_by_label("Name").fill("Retail Price Reference")
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/competitors/{competitor_id}" and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save competitor").click()
        assert event.value.status == 200, event.value.text()
        competitor_name = "Retail Price Reference"
        page.get_by_text(competitor_name, exact=True).wait_for()
        assert any(r["id"] == competitor_id and r["name"] == competitor_name for r in api_get(page, "/v1/competitors"))

        # Source configuration includes custom headers, selectors and request policy.
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("button", name="Add source").click()
        select_option(page, "Competitor", competitor_name)
        page.get_by_label("Source name").fill("Retail Product Listing")
        page.get_by_label("Base URL").fill(HIGH_URL)
        select_option(page, "Source type", "OFFICIAL_STORE")
        select_option(page, "Collection method", "HTTP_FAST")
        shot(page, "10-source-creation-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/sources" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create source").click()
        assert event.value.status == 201, event.value.text()
        source = event.value.json()
        source_id = source["id"]
        source_name = "Retail Product Listing"
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "11-source-created.png")
        page.get_by_title("Configuration", exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page, "12-source-configuration.png")
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/sources/{source_id}/configuration" and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save configuration").click()
        assert event.value.status == 200, event.value.text()
        source_readback = api_get(page, f"/v1/sources/{source_id}")
        assert source_readback["configuration"]["rate_limit_rpm"] == 60
        assert source_readback["configuration"]["max_retries"] == 1
        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill("Retail Product Listing (Structured)")
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/sources/{source_id}" and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes").click()
        assert event.value.status == 200, event.value.text()
        source_name = "Retail Product Listing (Structured)"
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "13-source-configuration-saved.png")

        # Offering lifecycle: create, detail/history, edit, monitor, dynamic schema,
        # bulk import, CSV/JSON export, archive and persisted read-back.
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("button", name="Add offering").click()
        page.get_by_label("Name").fill(OFFERING_NAME)
        select_option(page, "Offering type", "PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page, "14-offering-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/offerings" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create offering").click()
        assert event.value.status == 201, event.value.text()
        offering_id = event.value.json()["id"]
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "15-offering-created.png")

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_role("button", name=OFFERING_NAME, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "16-offering-detail-and-history.png")
        page.get_by_role("button", name="Close dialog").click()
        page.locator("tbody tr").filter(has_text=OFFERING_NAME).first.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/offerings/{offering_id}" and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes").click()
        assert event.value.status == 200, event.value.text()
        assert float(api_get(page, f"/v1/offerings/{offering_id}")["current_price"]) == 429.99

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/offerings/{offering_id}/toggle-monitoring" and r.request.method == "POST") as event:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert event.value.status == 200
        assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/offerings/{offering_id}/toggle-monitoring" and r.request.method == "POST") as event:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert event.value.status == 200
        assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is True
        shot(page, "17-offering-monitoring.png")

        page.get_by_role("button", name="Dynamic fields").click()
        page.get_by_text("Dynamic field definitions", exact=True).wait_for()
        shot(page, "18-dynamic-fields.png")
        page.get_by_role("button", name="Add field").click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months")
        page.get_by_label("Category").fill("Commercial")
        shot(page, "19-dynamic-field-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/offerings/fields" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create field").click()
        assert event.value.status == 201, event.value.text()
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "20-dynamic-field-created.png")
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(lambda r: urlparse(r.url).path.startswith("/v1/offerings/fields/") and r.request.method == "DELETE") as event:
            field_row.locator("button.danger-icon").click()
        assert event.value.status == 204, event.value.text()
        page.get_by_role("button", name="Close dialog").click()

        page.get_by_role("button", name="Bulk import").click()
        bulk = [
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}},
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk))
        shot(page, "21-bulk-import-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/offerings/bulk" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Import offerings").click()
        assert event.value.status == 200, event.value.text()
        assert event.value.json()["successful_count"] == 2, event.value.text()
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        shot(page, "22-bulk-imported-catalog.png")
        csv_path = Path("/tmp/nexora-catalog.csv")
        with page.expect_download() as event:
            page.get_by_role("button", name="CSV", exact=True).click()
        event.value.save_as(str(csv_path))
        assert csv_path.stat().st_size > 0
        json_path = Path("/tmp/nexora-catalog.json")
        with page.expect_download() as event:
            page.get_by_role("button", name="JSON", exact=True).click()
        event.value.save_as(str(json_path))
        assert json_path.stat().st_size > 0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/offerings/bulk-archive" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert event.value.json()["archived_count"] == 1
        assert page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).count() == 0
        archived = api_get(page, "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=10")
        assert any(row["is_archived"] for row in archived["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "23-catalog-import-export-archive.png")

        # Match, real internet HTTP collection, price snapshot delta, alert event and email.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("button", name="Add match").click()
        select_option(page, "Offering", OFFERING_NAME)
        select_option(page, "Source", source_name)
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        select_option(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "24-match-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/offering-matches" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create match").click()
        assert event.value.status == 201, event.value.text()
        match_id = event.value.json()["id"]
        page.get_by_text(HIGH_URL, exact=False).wait_for()
        shot(page, "25-match-created.png")
        match_row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/collections/run" and r.request.method == "POST") as event:
            match_row.get_by_title("Run collection", exact=True).click()
        first = event.value
        assert first.status == 201, first.text()
        first_payload = first.json()
        assert first_payload["status"] == "COMPLETED", first_payload
        assert first_payload["observation"]["price"] == "399.99", first_payload
        assert first_payload["observation"]["http_status_code"] == 200
        shot(page, "26-first-price-observation.png")
        assert len(api_get(page, f"/v1/offerings/{offering_id}/history")) >= 1

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("button", name="New rule").click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        select_option(page, "Alert type", "PERCENTAGE_DROP")
        select_option(page, "Offering scope", OFFERING_NAME)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "27-alert-rule-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/alerts/rules" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Create rule").click()
        assert event.value.status == 201, event.value.text()
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "28-alert-rule-created.png")

        page.get_by_role("link", name="Matches", exact=True).click()
        page.locator("tbody tr").filter(has_text=OFFERING_NAME).first.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        with page.expect_response(lambda r: urlparse(r.url).path == f"/v1/offering-matches/{match_id}" and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save changes").click()
        assert event.value.status == 200, event.value.text()
        match_row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/collections/run" and r.request.method == "POST") as event:
            match_row.get_by_title("Run collection", exact=True).click()
        second = event.value
        assert second.status == 201, second.text()
        second_payload = second.json()
        assert second_payload["observation"]["price"] == "349.99", second_payload
        assert second_payload["snapshot"]["previous_price"] == "399.99", second_payload
        assert second_payload["snapshot"]["percentage_difference"].startswith("-12.50%"), second_payload

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        logs = api_get(page, "/v1/alerts/logs")
        alert_log = next((row for row in logs if row["title"] == "Competitor price dropped"), None)
        assert alert_log and not alert_log["is_read"], logs
        mail_start = len(messages())
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        assert any(row["id"] == alert_log["id"] and row["is_read"] for row in api_get(page, "/v1/alerts/logs"))
        shot(page, "29-alert-generated-and-read.png")
        dispatch_result = dispatch_pending_notifications.run()
        assert int(dispatch_result.get("sent", 0)) >= 1, dispatch_result
        alert_email = wait_email("Nexora Competitive Alert: Competitor price dropped", EMAIL, mail_start)
        assert "Nexora detected a competitive intelligence event" in alert_email
        assert "SENT" in asyncio.run(sent_statuses(client_id))

        # Job history and details are the same persisted collection execution.
        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_text("ON DEMAND REFRESH", exact=False).wait_for()
        shot(page, "30-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "31-job-detail.png")
        page.get_by_role("button", name="Close dialog").click()

        # Discovery blocks loopback targets, then persists real structured product data.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/discovery/run" and r.request.method == "POST") as event:
            page.get_by_role("button", name=re.compile("Run discovery")).click()
        assert event.value.status == 422, event.value.text()
        shot(page, "32-private-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/discovery/run" and r.request.method == "POST") as event:
            page.get_by_role("button", name=re.compile("Run discovery")).click()
        assert event.value.status == 200, event.value.text()
        discovery = event.value.json()
        assert discovery["status"] == "COMPLETED", discovery
        assert discovery["created_offerings_count"] == 1, discovery
        assert discovery["items"][0]["name"] == DISCOVERED_NAME, discovery
        page.get_by_text(DISCOVERED_NAME, exact=True).wait_for()
        shot(page, "33-discovery-product-created.png")
        fresh_job = api_get(page, f"/v1/discovery/jobs/{discovery['job_id']}")
        assert fresh_job["items"][0]["sku"] == "WF1000XM5-BLK"
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/discovery/run" and r.request.method == "POST") as event:
            page.get_by_role("button", name=re.compile("Run discovery")).click()
        assert event.value.status == 200, event.value.text()
        rerun = event.value.json()
        assert rerun["created_offerings_count"] == 0 and rerun["updated_offerings_count"] == 1, rerun
        catalog = api_get(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(row["sku"] == "WF1000XM5-BLK" for row in catalog["items"]) == 1
        shot(page, "34-discovery-upsert-history.png")

        # Organization profile and password lifecycle.
        page.locator("a.sidebar-status").click()
        page.get_by_label("Company name").wait_for()
        shot(page, "35-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/profile" and r.request.method == "PATCH") as event:
            page.get_by_role("button", name="Save profile").click()
        assert event.value.status == 200, event.value.text()
        assert api_get(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"
        shot(page, "36-settings-profile-updated.png")
        page.get_by_label("Current password").fill(PASSWORD)
        page.get_by_label("New password").fill(CHANGED_PASSWORD)
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/change-password" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Update password").click()
        assert event.value.status == 200, event.value.text()
        assert page.request.post(API + "/v1/auth/login", data={"email": EMAIL, "password": PASSWORD}).status == 401
        assert page.request.post(API + "/v1/auth/login", data={"email": EMAIL, "password": CHANGED_PASSWORD}).status == 200
        shot(page, "37-password-changed.png")

        # Reset password by the link from the actual delivered email.
        logout(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        reset_mail_start = len(messages())
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/forgot-password" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Send reset link").click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "38-password-reset-requested.png")
        reset_body = wait_email("Reset your Nexora password", EMAIL, reset_mail_start)
        page.goto(extract_link(reset_body, "reset-password"), wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        shot(page, "39-password-reset-form.png")
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(lambda r: urlparse(r.url).path == "/v1/auth/reset-password" and r.request.method == "POST") as event:
            page.get_by_role("button", name="Update password").click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "40-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login(page, EMAIL, RESET_PASSWORD)
        shot(page, "41-dashboard-after-reset.png")

        # Persisted analyst and viewer users exercise real RBAC on UI and API.
        suffix = uuid.uuid4().hex[:10]
        analyst_email = asyncio.run(create_role_user(client_id, RoleEnum.ANALYST, suffix))
        viewer_email = asyncio.run(create_role_user(client_id, RoleEnum.VIEWER, suffix))
        logout(page)
        login(page, analyst_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        denied = page.request.post(API + "/v1/competitors", headers={"Authorization": f"Bearer {token(page)}"}, data={"name":"Not Allowed","domain":"forbidden.example"})
        assert denied.status == 403, denied.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name=re.compile("Run discovery")).count() == 1
        shot(page, "42-analyst-permissions.png")

        logout(page)
        login(page, viewer_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        read_only = page.request.get(API + "/v1/offerings", headers={"Authorization": f"Bearer {token(page)}"})
        assert read_only.status == 200, read_only.text()
        denied = page.request.post(API + "/v1/competitors", headers={"Authorization": f"Bearer {token(page)}"}, data={"name":"Viewer Write Attempt","domain":"viewer-write.example"})
        assert denied.status == 403, denied.text()
        shot(page, "43-viewer-read-only.png")

        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(FRONTEND + "/welcome", wait_until="networkidle")
        size = mobile.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        assert size["content"] <= size["viewport"], size
        shot(mobile, "44-mobile-homepage.png")

        if browser_errors:
            raise AssertionError("Browser JavaScript errors: " + " | ".join(browser_errors))
        print(f"FULL_STACK_E2E_PASS screenshots={len(list(SHOTS.glob('*.png')))}", flush=True)
        browser.close()
        context.close()


if __name__ == "__main__":
    main()

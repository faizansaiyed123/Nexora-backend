"""Browser-led Nexora walkthrough against real FastAPI, PostgreSQL and Redis."""
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

from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy import select

from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.alert import AlertLogModel
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications

BASE_URL = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API_URL = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SCREENSHOTS = Path(os.getenv("NEXORA_SCREENSHOT_DIR", "../docs/walkthrough/screenshots"))
EMAIL = "market-analyst@northstar.example.com"
INITIAL_PASSWORD = "Nexora-Strong!2026"
UPDATED_PASSWORD = "Nexora-Updated!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
ORGANIZATION = "Northstar Market Intelligence"
FULL_NAME = "Alex Morgan"
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
js_errors: list[str] = []


def shot(page: Page, filename: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / filename), full_page=True, animations="disabled", caret="hide")
    print(f"SCREENSHOT {filename}", flush=True)


def response_matches(response, path: str, method: str) -> bool:
    return urlparse(response.url).path.rstrip("/") == path.rstrip("/") and response.request.method == method


def choose(page: Page, label: str, option: str) -> None:
    field = page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    field.page.get_by_role("option", name=re.compile(re.escape(option), re.I)).last.click()


def token(page: Page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "Expected a persisted access token after sign-in."
    return str(value)


def api_get(page: Page, path: str, expected: int = 200):
    result = page.request.get(API_URL + path, headers={"Authorization": f"Bearer {token(page)}"})
    assert result.status == expected, f"GET {path}: {result.status} {result.text()}"
    return result.json()


def messages() -> list[dict]:
    if not MAILBOX.exists():
        return []
    results = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("data"):
                results.append(item)
        except json.JSONDecodeError:
            pass
    return results


def mail_parts(item: dict) -> tuple[str, str, list[str]]:
    message = message_from_string(str(item["data"]))
    subject = str(message.get("Subject", ""))
    if message.is_multipart():
        bodies = []
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    bodies.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        body = "\n".join(bodies)
    else:
        raw = message.get_payload(decode=True)
        body = raw.decode(message.get_content_charset() or "utf-8", "replace") if raw else str(message.get_payload())
    return subject, body, [str(x).lower() for x in item.get("rcpt_tos", [])]


def wait_mail(subject_fragment: str, recipient: str, offset: int, timeout: float = 35) -> tuple[dict, str]:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        for item in messages()[offset:]:
            subject, body, rcpts = mail_parts(item)
            if subject_fragment.lower() in subject.lower() and recipient.lower() in rcpts:
                return item, body
        time.sleep(0.25)
    subjects = [mail_parts(item)[0] for item in messages()[offset:]]
    raise AssertionError(f"Expected {subject_fragment!r} to {recipient!r}; received {subjects!r}")


def one_time_url(body: str, route: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body)
    assert match, f"No one-time {route} link found in delivered email."
    return match.group(0).rstrip(".,);")


async def add_role_user(client_id: uuid.UUID, role: RoleEnum) -> str:
    email = f"northstar-{role.value.lower()}@northstar.example.com"
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


async def outbox_statuses(client_id: uuid.UUID) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(NotificationOutboxModel.status).where(
            NotificationOutboxModel.client_id == client_id,
            NotificationOutboxModel.channel == "email",
        ))
        return list(result.scalars().all())


def sign_in(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(lambda r: response_matches(r, "/v1/auth/login", "POST")) as event:
        page.get_by_role("button", name=re.compile("Sign in", re.I)).click()
    assert event.value.status == 200, event.value.text()
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=15000)


def sign_out(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_role("menuitem", name="Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=10000)


def main() -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    if MAILBOX.exists():
        MAILBOX.unlink()

    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: js_errors.append(str(error)))

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
        before_register_mail = len(messages())
        with page.expect_response(lambda r: response_matches(r, "/v1/auth/register", "POST")) as event:
            page.get_by_role("button", name=re.compile("Create workspace", re.I)).click()
        assert event.value.status == 201, event.value.text()
        assert event.value.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "02-verification-email-sent.png")
        _, verification_body = wait_mail("Verify your Nexora account", EMAIL, before_register_mail)
        page.goto(one_time_url(verification_body, "verify-email"), wait_until="networkidle")
        shot(page, "03-email-verification-page.png")
        with page.expect_response(lambda r: response_matches(r, "/v1/auth/verify-email", "GET")) as event:
            page.get_by_role("button", name=re.compile("Verify email", re.I)).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page, "05-login.png")
        sign_in(page, EMAIL, INITIAL_PASSWORD)
        shot(page, "06-dashboard-empty-workspace.png")

        me = api_get(page, "/v1/auth/me")
        client_id = uuid.UUID(me["client_id"])
        assert me["role"] == "ORG_ADMIN"

        # Competitor create, duplicate-domain guard, edit, and persisted read-back.
        competitor_name = "Retail Product Reference"
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors-empty.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Retail Product Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: response_matches(r, "/v1/competitors", "POST")) as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 201, event.value.text()
        competitor_id = event.value.json()["id"]
        page.get_by_text(competitor_name, exact=True).wait_for()
        shot(page, "08-competitor-created.png")

        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Raw Host")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: response_matches(r, "/v1/competitors", "POST")) as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 403, event.value.text()
        page.get_by_text("A competitor with this domain already exists.").wait_for()
        shot(page, "09-duplicate-domain-rejected.png")
        page.get_by_role("button", name="Cancel", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=competitor_name).first
        row.locator("button").first.click()
        page.get_by_label("Name").fill("Retail Product Reference · Sony")
        with page.expect_response(lambda r: response_matches(r, f"/v1/competitors/{competitor_id}", "PATCH")) as event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert event.value.status == 200, event.value.text()
        competitor_name = "Retail Product Reference · Sony"
        assert any(x["id"] == competitor_id and x["name"] == competitor_name for x in api_get(page, "/v1/competitors"))

        # Source configuration: real HTTP collection against deterministic structured retail pages.
        source_name = "Sony Retail Product Page"
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("heading", name="Sources", exact=True).wait_for()
        page.get_by_role("button", name="Add source", exact=True).click()
        choose(page, "Competitor", competitor_name)
        page.get_by_label("Source name").fill(source_name)
        page.get_by_label("Base URL").fill(HIGH_URL)
        choose(page, "Source type", "OFFICIAL_STORE")
        choose(page, "Collection method", "HTTP_FAST")
        shot(page, "10-source-creation-form.png")
        with page.expect_response(lambda r: response_matches(r, "/v1/sources", "POST")) as event:
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
        with page.expect_response(lambda r: response_matches(r, f"/v1/sources/{source_id}/configuration", "PATCH")) as event:
            page.get_by_role("button", name="Save configuration", exact=True).click()
        assert event.value.status == 200, event.value.text()
        source_state = api_get(page, f"/v1/sources/{source_id}")
        assert source_state["configuration"]["rate_limit_rpm"] == 60
        assert source_state["configuration"]["max_retries"] == 1

        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill(source_name + " (Structured)")
        with page.expect_response(lambda r: response_matches(r, f"/v1/sources/{source_id}", "PATCH")) as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        source_name += " (Structured)"
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "13-source-configuration-saved.png")

        # Catalog CRUD, detail/history, dynamic fields, import/export and archive.
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
        with page.expect_response(lambda r: response_matches(r, "/v1/offerings", "POST")) as event:
            page.get_by_role("button", name="Create offering", exact=True).click()
        assert event.value.status == 201, event.value.text()
        offering_id = event.value.json()["id"]
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "15-catalog-offering-created.png")

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_role("button", name=OFFERING_NAME, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "16-offering-detail-and-history.png")
        page.get_by_role("button", name="Close dialog", exact=True).click()

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r: response_matches(r, f"/v1/offerings/{offering_id}", "PATCH")) as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert float(api_get(page, f"/v1/offerings/{offering_id}")["current_price"]) == 429.99

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: response_matches(r, f"/v1/offerings/{offering_id}/toggle-monitoring", "POST")) as event:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: response_matches(r, f"/v1/offerings/{offering_id}/toggle-monitoring", "POST")) as event:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert event.value.status == 200
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
        with page.expect_response(lambda r: response_matches(r, "/v1/offerings/fields", "POST")) as event:
            page.get_by_role("button", name="Create field", exact=True).click()
        assert event.value.status == 201, event.value.text()
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "20-dynamic-field-created.png")
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(lambda r: "/v1/offerings/fields/" in urlparse(r.url).path and r.request.method == "DELETE") as event:
            field_row.locator("button.danger-icon").click()
        assert event.value.status == 204, event.value.text()
        page.get_by_role("button", name="Close dialog", exact=True).click()

        page.get_by_role("button", name="Bulk import", exact=True).click()
        bulk_payload = [
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}},
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk_payload))
        shot(page, "21-bulk-import-form.png")
        with page.expect_response(lambda r: response_matches(r, "/v1/offerings/bulk", "POST")) as event:
            page.get_by_role("button", name="Import offerings", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert event.value.json()["successful_count"] == 2, event.value.text()
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        shot(page, "22-bulk-imported-catalog.png")

        for fmt, filename in [("CSV", "/tmp/nexora-catalog.csv"), ("JSON", "/tmp/nexora-catalog.json")]:
            with page.expect_download(timeout=15000) as download:
                page.get_by_role("button", name=fmt, exact=True).click()
            download.value.save_as(filename)
            assert Path(filename).is_file() and Path(filename).stat().st_size > 0

        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(lambda r: response_matches(r, "/v1/offerings/bulk-archive", "POST")) as event:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert event.value.json()["archived_count"] == 1
        assert api_get(page, "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")["items"][0]["is_archived"]
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "23-catalog-import-export-and-archive.png")

        # Match lifecycle and genuine HTTP price collection, recorded to snapshot/history tables.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("heading", name="Offering matches", exact=True).wait_for()
        page.get_by_role("button", name="Add match", exact=True).click()
        choose(page, "Offering", OFFERING_NAME)
        choose(page, "Source", source_name)
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        choose(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "24-offering-match-form.png")
        with page.expect_response(lambda r: response_matches(r, "/v1/offering-matches", "POST")) as event:
            page.get_by_role("button", name="Create match", exact=True).click()
        assert event.value.status == 201, event.value.text()
        match_id = event.value.json()["id"]
        match_row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        page.get_by_text(HIGH_URL, exact=False).wait_for()
        shot(page, "25-offering-match-created.png")

        with page.expect_response(lambda r: response_matches(r, "/v1/collections/run", "POST")) as event:
            match_row.get_by_title("Run collection", exact=True).click()
        first = event.value
        assert first.status in (200, 201), first.text()
        observation1 = first.json()
        assert observation1["status"] == "COMPLETED", observation1
        assert observation1["observation"]["price"] == "399.99", observation1
        assert observation1["observation"]["http_status_code"] == 200, observation1
        assert len(api_get(page, f"/v1/offerings/{offering_id}/history")) >= 1
        shot(page, "26-first-price-observation.png")

        # The observed price change feeds a persistent alert rule and outbound email.
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("heading", name="Alerts", exact=True).wait_for()
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page, "Alert type", "PERCENTAGE_DROP")
        choose(page, "Offering scope", OFFERING_NAME)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "27-alert-rule-form.png")
        with page.expect_response(lambda r: response_matches(r, "/v1/alerts/rules", "POST")) as event:
            page.get_by_role("button", name="Create rule", exact=True).click()
        assert event.value.status == 201, event.value.text()
        rule_id = event.value.json()["id"]
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "28-alert-rule-created.png")

        page.get_by_role("link", name="Matches", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        with page.expect_response(lambda r: response_matches(r, f"/v1/offering-matches/{match_id}", "PATCH")) as event:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert event.value.status == 200, event.value.text()
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(lambda r: response_matches(r, "/v1/collections/run", "POST")) as event:
            row.get_by_title("Run collection", exact=True).click()
        second = event.value
        assert second.status in (200, 201), second.text()
        observation2 = second.json()
        assert observation2["observation"]["price"] == "349.99", observation2
        assert observation2["snapshot"]["previous_price"] == "399.99", observation2
        assert observation2["snapshot"]["percentage_difference"].startswith("-12.50%"), observation2

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        alert_logs = api_get(page, "/v1/alerts/logs")
        alert_log = next((item for item in alert_logs if item["title"] == "Competitor price dropped"), None)
        assert alert_log and not alert_log["is_read"], alert_logs
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        assert any(item["id"] == alert_log["id"] and item["is_read"] for item in api_get(page, "/v1/alerts/logs"))
        shot(page, "29-alert-generated-and-read.png")

        email_offset = len(messages())
        delivered = dispatch_pending_notifications.run()
        assert int(delivered.get("sent", 0)) >= 1, delivered
        _, alert_body = wait_mail("Nexora Competitive Alert: Competitor price dropped", EMAIL, email_offset)
        assert "competitive intelligence event" in alert_body.lower()
        assert "SENT" in asyncio.run(outbox_statuses(client_id))

        # Job list/detail reflect collection jobs already committed by the API.
        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_role("heading", name="Jobs", exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH", exact=False).wait_for()
        shot(page, "30-collection-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "31-collection-job-detail.png")
        page.get_by_role("button", name="Close dialog", exact=True).click()

        # Discovery blocks private network targets and persists evidence-backed offerings.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_role("heading", name="Website discovery", exact=True).wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r: response_matches(r, "/v1/discovery/run", "POST")) as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert event.value.status == 422, event.value.text()
        shot(page, "32-private-target-blocked.png")

        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        choose(page, "Default type", "PRODUCT")
        with page.expect_response(lambda r: response_matches(r, "/v1/discovery/run", "POST")) as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert event.value.status == 200, event.value.text()
        discovery1 = event.value.json()
        assert discovery1["status"] == "COMPLETED", discovery1
        assert discovery1["created_offerings_count"] == 1, discovery1
        assert discovery1["items"][0]["name"] == DISCOVERED_NAME, discovery1
        page.get_by_text(DISCOVERED_NAME, exact=True).wait_for(timeout=15000)
        shot(page, "33-discovery-product-created.png")
        job_id = discovery1["job_id"]
        assert api_get(page, f"/v1/discovery/jobs/{job_id}")["items"][0]["sku"] == "WF1000XM5-BLK"
        with page.expect_response(lambda r: response_matches(r, "/v1/discovery/run", "POST")) as event:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert event.value.status == 200, event.value.text()
        discovery2 = event.value.json()
        assert discovery2["created_offerings_count"] == 0, discovery2
        assert discovery2["updated_offerings_count"] == 1, discovery2
        items = api_get(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")["items"]
        assert sum(item["sku"] == "WF1000XM5-BLK" for item in items) == 1, items
        shot(page, "34-discovery-upsert-history.png")

        # Workspace settings and password lifecycle use the real email link and real API.
        page.locator("button.profile-trigger").click()
        page.get_by_role("menuitem", name="Account settings", exact=True).click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        page.get_by_label("Organization name").wait_for()
        shot(page, "35-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r: response_matches(r, "/v1/auth/profile", "PATCH")) as event:
            page.get_by_role("button", name="Save profile", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert api_get(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"
        shot(page, "36-workspace-profile-updated.png")

        page.get_by_label("Current password").fill(INITIAL_PASSWORD)
        page.get_by_label("New password").fill(UPDATED_PASSWORD)
        with page.expect_response(lambda r: response_matches(r, "/v1/auth/change-password", "POST")) as event:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert event.value.status == 200, event.value.text()
        assert page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": INITIAL_PASSWORD}).status == 401
        assert page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": UPDATED_PASSWORD}).status == 200
        shot(page, "37-password-changed.png")

        sign_out(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        reset_offset = len(messages())
        with page.expect_response(lambda r: response_matches(r, "/v1/auth/forgot-password", "POST")) as event:
            page.get_by_role("button", name="Send reset link", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "38-password-reset-requested.png")
        _, reset_body = wait_mail("Reset your Nexora password", EMAIL, reset_offset)
        page.goto(one_time_url(reset_body, "reset-password"), wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        shot(page, "39-password-reset-form.png")
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(lambda r: response_matches(r, "/v1/auth/reset-password", "POST")) as event:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert event.value.status == 200, event.value.text()
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "40-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        sign_in(page, EMAIL, RESET_PASSWORD)
        shot(page, "41-dashboard-after-password-reset.png")

        viewer_email = asyncio.run(add_role_user(client_id, RoleEnum.VIEWER))
        analyst_email = asyncio.run(add_role_user(client_id, RoleEnum.ANALYST))

        sign_out(page)
        sign_in(page, analyst_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        analyst_token = token(page)
        denied = page.request.post(API_URL + "/v1/competitors", headers={"Authorization": f"Bearer {analyst_token}"}, json={"name":"Not Allowed","domain":"forbidden.example"})
        assert denied.status == 403, denied.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name="Run discovery", exact=True).is_visible()
        shot(page, "42-analyst-workspace.png")

        sign_out(page)
        sign_in(page, viewer_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("heading", name="Offerings", exact=True).wait_for()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        viewer_token = token(page)
        assert page.request.get(API_URL + "/v1/offerings", headers={"Authorization": f"Bearer {viewer_token}"}).status == 200
        denied = page.request.post(API_URL + "/v1/competitors", headers={"Authorization": f"Bearer {viewer_token}"}, json={"name":"Viewer Write Attempt","domain":"viewer-write.example"})
        assert denied.status == 403, denied.text()
        shot(page, "43-viewer-read-only-workspace.png")

        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(BASE_URL + "/welcome", wait_until="networkidle")
        dimensions = mobile.evaluate("() => ({viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth})")
        assert dimensions["content"] <= dimensions["viewport"], dimensions
        shot(mobile, "44-mobile-homepage.png")

        assert not js_errors, "Browser JavaScript errors: " + " | ".join(js_errors)
        print("NEXORA_BROWSER_WALKTHROUGH_PASS screenshots=45", flush=True)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

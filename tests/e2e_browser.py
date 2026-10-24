"""End-to-end browser walkthrough for Nexora's real connected stack."""
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
from backend.db.session import AsyncSessionLocal, engine
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications


FRONTEND_URL = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API_URL = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SCREENSHOTS = Path(os.getenv("NEXORA_SCREENSHOTS_DIR", "../docs/walkthrough/screenshots"))
EMAIL = "market-analyst@northstar.example.com"
PASSWORD = "Nexora-Strong!2026"
CHANGED_PASSWORD = "Nexora-Changed!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
HIGH_URL = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-high.html"
LOW_URL = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-low.html"
DISCOVERY_URL = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-discovery-product.html"
JS_ERRORS: list[str] = []
DB_LOOP = asyncio.new_event_loop()


def db_run(coro):
    return DB_LOOP.run_until_complete(coro)


def snap(page: Page, filename: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / filename), full_page=True, animations="disabled", caret="hide")
    print(f"Screenshot: {filename}", flush=True)


def ready(page: Page, heading: str) -> None:
    page.locator(".content h2").filter(has_text=heading).last.wait_for(timeout=15000)


def choose(page: Page, label: str, option: str) -> None:
    field = page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option", name=option, exact=True).last.click()


def access_token(page: Page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "No signed-in access token was saved in the browser."
    return str(value)


def api_get(page: Page, path: str, expected: int = 200):
    response = page.request.get(API_URL + path, headers={"Authorization": f"Bearer {access_token(page)}"})
    assert response.status == expected, f"GET {path}: {response.status}: {response.text()}"
    return response.json()


def read_mailbox() -> list[dict]:
    if not MAILBOX.exists():
        return []
    result = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("data"):
                result.append(row)
        except json.JSONDecodeError:
            continue
    return result


def mail_content(row: dict) -> tuple[str, str, list[str]]:
    message = message_from_string(str(row["data"]))
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
    return subject, body, [str(x).lower() for x in row.get("rcpt_tos", [])]


def wait_email(subject: str, recipient: str, offset: int) -> tuple[dict, str]:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        for row in read_mailbox()[offset:]:
            actual_subject, body, recipients = mail_content(row)
            if subject.lower() in actual_subject.lower() and recipient.lower() in recipients:
                return row, body
        time.sleep(0.25)
    seen = [mail_content(x)[0] for x in read_mailbox()[offset:]]
    raise AssertionError(f"Expected email {subject!r} to {recipient!r}; received {seen}")


def link_from_email(body: str, route: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body)
    assert match, f"No {route} link found in the delivered email."
    return match.group(0).rstrip(".,);")


async def seed_user(client_id: uuid.UUID, role: RoleEnum, suffix: str) -> str:
    email = f"{role.value.lower()}-{suffix}@northstar.example.com"
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


async def outbox_states(client_id: uuid.UUID) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(NotificationOutboxModel.status).where(
            NotificationOutboxModel.client_id == client_id,
            NotificationOutboxModel.channel == "email",
        ))
        return list(result.scalars().all())


def login_ui(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(lambda r: r.url.endswith("/v1/auth/login") and r.request.method == "POST") as event:
        page.get_by_role("button", name=re.compile("Sign in")).click()
    assert event.value.status == 200, event.value.text()
    page.locator(".topbar-title h1").filter(has_text="Dashboard").wait_for(timeout=15000)


def sign_out(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=15000)


def main() -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    if MAILBOX.exists():
        MAILBOX.unlink()

    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True, device_scale_factor=1)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: JS_ERRORS.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        # Account creation and real one-time email verification.
        page.goto(FRONTEND_URL + "/welcome", wait_until="networkidle")
        assert page.get_by_role("heading", name=re.compile("Understand the market")).is_visible()
        snap(page, "00-public-homepage.png")
        page.get_by_role("link", name=re.compile("Create workspace")).click()
        page.get_by_label("Organization name").wait_for()
        snap(page, "01-registration-form.png")
        page.get_by_label("Organization name").fill("Northstar Market Intelligence")
        page.get_by_label("Your full name").fill("Alex Morgan")
        page.get_by_label("Work email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        signup_offset = len(read_mailbox())
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/register") and r.request.method == "POST") as signup:
            page.get_by_role("button", name=re.compile("Create workspace")).click()
        assert signup.value.status == 201, signup.value.text()
        assert signup.value.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        snap(page, "02-verification-email-sent.png")
        _, verification_body = wait_email("Verify your Nexora account", EMAIL, signup_offset)
        verify_url = link_from_email(verification_body, "verify-email")
        assert urlparse(verify_url).path.endswith("/verify-email")
        page.goto(verify_url, wait_until="networkidle")
        snap(page, "03-email-verification.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/verify-email") and r.request.method == "GET") as verify:
            page.get_by_role("button", name=re.compile("Verify email")).click()
        assert verify.value.status == 200, verify.value.text()
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        snap(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login_ui(page, EMAIL, PASSWORD)
        snap(page, "05-workspace-dashboard.png")
        me = api_get(page, "/v1/auth/me")
        assert me["role"] == "ORG_ADMIN", me
        client_id = uuid.UUID(me["client_id"])

        # Competitors: create, duplicate-domain validation, update and persisted readback.
        page.get_by_role("link", name="Competitors", exact=True).click()
        ready(page, "Competitors")
        snap(page, "06-competitor-directory.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Retail Product Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: r.url.endswith("/v1/competitors") and r.request.method == "POST") as create_comp:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert create_comp.value.status == 201, create_comp.value.text()
        competitor_id = create_comp.value.json()["id"]
        page.get_by_text("Retail Product Reference", exact=True).wait_for()
        snap(page, "07-competitor-created.png")

        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: r.url.endswith("/v1/competitors") and r.request.method == "POST") as duplicate:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert duplicate.value.status == 403, duplicate.value.text()
        page.get_by_text("A competitor with this domain already exists.").wait_for()
        snap(page, "08-duplicate-domain-blocked.png")
        page.get_by_role("button", name="Cancel", exact=True).click()

        row = page.locator("tbody tr").filter(has_text="Retail Product Reference").first
        row.locator("button").first.click()
        page.get_by_label("Name").fill("Retail Product Intelligence")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/competitors/{competitor_id}") and r.request.method == "PATCH") as edit_comp:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert edit_comp.value.status == 200, edit_comp.value.text()
        competitor_name = "Retail Product Intelligence"
        assert any(x["id"] == competitor_id and x["name"] == competitor_name for x in api_get(page, "/v1/competitors"))

        # Data source, custom extraction configuration and saved settings.
        page.get_by_role("link", name="Sources", exact=True).click()
        ready(page, "Sources")
        page.get_by_role("button", name="Add source", exact=True).click()
        choose(page, "Competitor", competitor_name)
        page.get_by_label("Source name").fill("Retail Product Listing")
        high_url = HIGH_URL
        page.get_by_label("Base URL").fill(high_url)
        choose(page, "Source type", "OFFICIAL_STORE")
        choose(page, "Collection method", "HTTP_FAST")
        snap(page, "09-source-creation-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/sources") and r.request.method == "POST") as create_source:
            page.get_by_role("button", name="Create source", exact=True).click()
        assert create_source.value.status == 201, create_source.value.text()
        source_id = create_source.value.json()["id"]
        page.get_by_text("Retail Product Listing", exact=True).wait_for()
        page.get_by_title("Configuration", exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill('{"title_selector":"h1"}')
        snap(page, "10-source-configuration.png")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/sources/{source_id}/configuration") and r.request.method == "PATCH") as conf:
            page.get_by_role("button", name="Save configuration", exact=True).click()
        assert conf.value.status == 200, conf.value.text()
        s_read = page.request.get(API_URL + f"/v1/sources/{source_id}", headers={"Authorization": f"Bearer {access_token(page)}"})
        assert s_read.status == 200 and s_read.json()["configuration"]["rate_limit_rpm"] == 60
        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill("Retail Product Listing (Structured)")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/sources/{source_id}") and r.request.method == "PATCH") as edit_source:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert edit_source.value.status == 200, edit_source.value.text()
        source_name = "Retail Product Listing (Structured)"
        page.get_by_text(source_name, exact=True).wait_for()
        snap(page, "11-source-ready.png")

        # Canonical offerings, details, edit, monitoring, dynamic fields, bulk and exports.
        page.get_by_role("link", name="Offerings", exact=True).click()
        ready(page, "Offerings")
        page.get_by_role("button", name="Add offering", exact=True).click()
        offering_name = "Sony WH-1000XM5 Wireless Headphones"
        page.get_by_label("Name").fill(offering_name)
        choose(page, "Offering type", "PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        snap(page, "12-offering-editor.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings") and r.request.method == "POST") as create_offering:
            page.get_by_role("button", name="Create offering", exact=True).click()
        assert create_offering.value.status == 201, create_offering.value.text()
        offering_id = create_offering.value.json()["id"]
        page.get_by_text(offering_name, exact=True).wait_for()
        snap(page, "13-catalog-offering.png")
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        row.get_by_role("button", name=offering_name, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        snap(page, "14-offering-detail.png")
        page.keyboard.press("Escape")
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}") and r.request.method == "PATCH") as edit_offering:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert edit_offering.value.status == 200, edit_offering.value.text()
        assert float(api_get(page, f"/v1/offerings/{offering_id}")["current_price"]) == 429.99
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method == "POST") as pause:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert pause.value.status == 200 and api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method == "POST") as resume:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert resume.value.status == 200 and api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is True
        snap(page, "15-monitoring-controls.png")

        page.get_by_role("button", name="Dynamic fields", exact=True).click()
        page.get_by_text("Dynamic field definitions", exact=True).wait_for()
        snap(page, "16-dynamic-fields.png")
        page.get_by_role("button", name="Add field", exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months")
        page.get_by_label("Category").fill("Commercial")
        snap(page, "17-dynamic-field-editor.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/fields") and r.request.method == "POST") as field_create:
            page.get_by_role("button", name="Create field", exact=True).click()
        assert field_create.value.status == 201, field_create.value.text()
        page.get_by_text("Review Cycle", exact=True).wait_for()
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(lambda r: "/v1/offerings/fields/" in r.url and r.request.method == "DELETE") as field_delete:
            field_row.locator("button.danger-icon").click()
        assert field_delete.value.status == 204, field_delete.value.text()
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")

        page.get_by_role("button", name="Bulk import", exact=True).click()
        bulk = [
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}},
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk))
        snap(page, "18-bulk-import-form.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/bulk") and r.request.method == "POST") as bulk_event:
            page.get_by_role("button", name="Import offerings", exact=True).click()
        assert bulk_event.value.status == 200 and bulk_event.value.json()["successful_count"] == 2, bulk_event.value.text()
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        with page.expect_download() as csv_event:
            page.get_by_role("button", name="CSV", exact=True).click()
        csv_file = Path("/tmp/nexora-catalog.csv")
        csv_event.value.save_as(str(csv_file))
        assert csv_file.is_file() and csv_file.stat().st_size > 0
        with page.expect_download() as json_event:
            page.get_by_role("button", name="JSON", exact=True).click()
        json_file = Path("/tmp/nexora-catalog.json")
        json_event.value.save_as(str(json_file))
        assert json_file.is_file() and json_file.stat().st_size > 0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(lambda r: r.url.endswith("/v1/offerings/bulk-archive") and r.request.method == "POST") as archive:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        assert archive.value.status == 200 and archive.value.json()["archived_count"] == 1
        archived = page.request.get(API_URL + "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20",headers={"Authorization":f"Bearer {access_token(page)}"})
        assert archived.status == 200 and any(x["is_archived"] for x in archived.json()["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(offering_name, exact=True).wait_for()
        snap(page, "19-catalog-bulk-export-archive.png")

        # Map catalog to a source URL; collect real HTML and persist price observations.
        page.get_by_role("link", name="Matches", exact=True).click()
        ready(page, "Offering matches")
        page.get_by_role("button", name="Add match", exact=True).click()
        choose(page, "Offering", offering_name)
        choose(page, "Source", source_name)
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        choose(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        snap(page, "20-offering-match-editor.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/offering-matches") and r.request.method == "POST") as match_create:
            page.get_by_role("button", name="Create match", exact=True).click()
        assert match_create.value.status == 201, match_create.value.text()
        match_id = match_create.value.json()["id"]
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        page.get_by_text(HIGH_URL, exact=False).wait_for()
        snap(page, "21-offering-match-created.png")
        with page.expect_response(lambda r: "/v1/collections/run" in r.url and r.request.method == "POST") as collect_one:
            row.get_by_title("Run collection", exact=True).click()
        first = collect_one.value
        assert first.status == 201, first.text()
        one = first.json()
        assert one["status"] == "COMPLETED" and one["observation"]["price"] == "399.99" and one["observation"]["http_status_code"] == 200, one
        assert api_get(page, f"/v1/offerings/{offering_id}/history"), "The first price must be persisted in history."
        snap(page, "22-first-live-price-observation.png")

        # Create a real alert rule; a second lower-price observation produces an event and email.
        page.get_by_role("link", name="Alerts", exact=True).click()
        ready(page, "Alerts")
        snap(page, "23-alert-center.png")
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page, "Alert type", "PERCENTAGE_DROP")
        choose(page, "Offering scope", offering_name)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        snap(page, "24-alert-rule-editor.png")
        with page.expect_response(lambda r: r.url.endswith("/v1/alerts/rules") and r.request.method == "POST") as rule:
            page.get_by_role("button", name="Create rule", exact=True).click()
        assert rule.value.status == 201, rule.value.text()
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        page.get_by_role("link", name="Matches", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        with page.expect_response(lambda r: r.url.endswith(f"/v1/offering-matches/{match_id}") and r.request.method == "PATCH") as edit_match:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert edit_match.value.status == 200, edit_match.value.text()
        row = page.locator("tbody tr").filter(has_text=offering_name).first
        with page.expect_response(lambda r: "/v1/collections/run" in r.url and r.request.method == "POST") as collect_two:
            row.get_by_title("Run collection", exact=True).click()
        second = collect_two.value
        assert second.status == 201, second.text()
        two = second.json()
        assert two["observation"]["price"] == "349.99" and two["snapshot"]["previous_price"] == "399.99" and two["snapshot"]["percentage_difference"].startswith("-12.50%"), two
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        alert_log = next((x for x in api_get(page, "/v1/alerts/logs") if x["title"] == "Competitor price dropped"), None)
        assert alert_log and not alert_log["is_read"], alert_log
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        assert any(x["id"] == alert_log["id"] and x["is_read"] for x in api_get(page, "/v1/alerts/logs"))
        snap(page, "25-alert-event-and-read-state.png")
        alert_mail_offset = len(read_mailbox())
        sent = dispatch_pending_notifications.run()
        assert int(sent.get("sent", 0)) >= 1, sent
        _, alert_body = wait_email("Nexora Competitive Alert: Competitor price dropped", EMAIL, alert_mail_offset)
        assert "Nexora detected a competitive intelligence event" in alert_body
        assert "SENT" in db_run(outbox_states(client_id))

        # Collection job detail reflects those same saved runs.
        page.get_by_role("link", name="Jobs", exact=True).click()
        ready(page, "Jobs")
        page.locator("tbody tr").filter(has_text="ON DEMAND REFRESH").first.wait_for(timeout=15000)
        snap(page, "26-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        snap(page, "27-job-detail.png")
        page.keyboard.press("Escape")

        # Discovery blocks private destinations, then extracts real structured product markup.
        page.get_by_role("link", name="Discovery", exact=True).click()
        ready(page, "Website discovery")
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as blocked:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert blocked.value.status == 422, blocked.value.text()
        snap(page, "28-private-discovery-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        choose(page, "Default type", "PRODUCT")
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as discovery:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        d = discovery.value
        assert d.status == 200, d.text()
        discovered = d.json()
        product_name = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
        assert discovered["status"] == "COMPLETED" and discovered["created_offerings_count"] == 1 and discovered["items"][0]["name"] == product_name, discovered
        page.get_by_text(product_name, exact=True).wait_for(timeout=15000)
        snap(page, "29-discovered-product.png")
        assert api_get(page, f"/v1/discovery/jobs/{discovered['job_id']}")["items"][0]["sku"] == "WF1000XM5-BLK"
        with page.expect_response(lambda r: r.url.endswith("/v1/discovery/run") and r.request.method == "POST") as discovery_again:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        again = discovery_again.value
        assert again.status == 200 and again.json()["created_offerings_count"] == 0 and again.json()["updated_offerings_count"] == 1, again.text()
        catalog = api_get(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(x["sku"] == "WF1000XM5-BLK" for x in catalog["items"]) == 1
        snap(page, "30-discovery-history-and-upsert.png")

        # Workspace settings, password change and email-based password reset.
        page.locator("button.profile-trigger").click()
        page.get_by_text("Account settings", exact=True).click()
        ready(page, "Settings")
        page.get_by_label("Organization name").wait_for()
        snap(page, "31-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/profile") and r.request.method == "PATCH") as profile_update:
            page.get_by_role("button", name="Save profile", exact=True).click()
        assert profile_update.value.status == 200, profile_update.value.text()
        assert api_get(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"
        snap(page, "32-workspace-profile-updated.png")
        page.get_by_label("Current password").fill(PASSWORD)
        page.get_by_label("New password").fill(CHANGED_PASSWORD)
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/change-password") and r.request.method == "POST") as change_password:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert change_password.value.status == 200, change_password.value.text()
        assert page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}).status == 401
        assert page.request.post(API_URL + "/v1/auth/login", json={"email": EMAIL, "password": CHANGED_PASSWORD}).status == 200
        snap(page, "33-password-changed.png")
        sign_out(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        reset_offset = len(read_mailbox())
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/forgot-password") and r.request.method == "POST") as forgot:
            page.get_by_role("button", name="Send reset link", exact=True).click()
        assert forgot.value.status == 200, forgot.value.text()
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        snap(page, "34-password-reset-requested.png")
        _, reset_body = wait_email("Reset your Nexora password", EMAIL, reset_offset)
        reset_url = link_from_email(reset_body, "reset-password")
        page.goto(reset_url, wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        snap(page, "35-password-reset-form.png")
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(lambda r: r.url.endswith("/v1/auth/reset-password") and r.request.method == "POST") as reset:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert reset.value.status == 200, reset.value.text()
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        snap(page, "36-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login_ui(page, EMAIL, RESET_PASSWORD)
        snap(page, "37-dashboard-after-reset.png")

        # Real persisted role users demonstrate UI restrictions and API enforcement.
        viewer_email = db_run(seed_user(client_id, RoleEnum.VIEWER, "walkthrough"))
        analyst_email = db_run(seed_user(client_id, RoleEnum.ANALYST, "walkthrough"))
        sign_out(page)
        login_ui(page, analyst_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        ready(page, "Competitors")
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        analyst_write = page.request.post(API_URL + "/v1/competitors", headers={"Authorization": f"Bearer {access_token(page)}"}, json={"name": "Not Allowed", "domain": "forbidden.example"})
        assert analyst_write.status == 403, analyst_write.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name="Run discovery", exact=True).is_visible()
        snap(page, "38-analyst-workspace.png")
        sign_out(page)
        login_ui(page, viewer_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        ready(page, "Offerings")
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        viewer_read = page.request.get(API_URL + "/v1/offerings", headers={"Authorization": f"Bearer {access_token(page)}"})
        assert viewer_read.status == 200, viewer_read.text()
        viewer_write = page.request.post(API_URL + "/v1/competitors", headers={"Authorization": f"Bearer {access_token(page)}"}, json={"name": "Viewer Write Attempt", "domain": "viewer-write.example"})
        assert viewer_write.status == 403, viewer_write.text()
        snap(page, "39-viewer-read-only-workspace.png")

        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(FRONTEND_URL + "/welcome", wait_until="networkidle")
        dims = mobile.evaluate("() => ({viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth})")
        assert dims["page"] <= dims["viewport"], dims
        snap(mobile, "40-mobile-homepage.png")
        assert not JS_ERRORS, "Browser JavaScript errors: " + " | ".join(JS_ERRORS)
        print("Nexora browser journey completed: 41 screenshots captured.", flush=True)
        db_run(engine.dispose())
        browser.close()
        context.close()
    DB_LOOP.close()


if __name__ == "__main__":
    main()

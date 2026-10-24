"""End-to-end browser journey for Nexora's public site and private workspace.

The browser drives the real React frontend. Requests hit the running FastAPI process,
and persistent state is read back from PostgreSQL/API endpoints. No app API is mocked.
Run only against the disposable CI database and local STARTTLS inbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy import select

FRONTEND = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SCREENSHOTS = Path(os.getenv("NEXORA_SCREENSHOTS_DIR", "docs/walkthrough/screenshots"))

EMAIL = "market-analyst@northstar.example.com"
INITIAL_PASSWORD = "Nexora-Strong!2026"
CHANGED_PASSWORD = "Nexora-Changed!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
ORGANIZATION = "Northstar Market Intelligence"
FULL_NAME = "Alex Morgan"
HIGH_URL = (
    "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/"
    "tests/fixtures/retail-price-high.html"
)
LOW_URL = HIGH_URL.replace("retail-price-high.html", "retail-price-low.html")
DISCOVERY_URL = HIGH_URL.replace(
    "retail-price-high.html", "retail-discovery-product.html"
)
OFFERING_NAME = "Sony WH-1000XM5 Wireless Headphones"
DISCOVERED_NAME = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
javascript_errors: list[str] = []


def shot(page: Page, name: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(SCREENSHOTS / name),
        full_page=True,
        animations="disabled",
        caret="hide",
    )
    print(f"Screenshot: {name}", flush=True)


def choose(page: Page, field_name: str, option_name: str) -> None:
    field = page.locator("label.field").filter(has_text=field_name).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option", name=re.compile(re.escape(option_name))).last.click()


def token(page: Page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "Expected an authenticated browser session."
    return str(value)


def get_json(page: Page, route: str, expected_status: int = 200):
    response = page.request.get(
        API + route,
        headers={"Authorization": f"Bearer {token(page)}"},
    )
    assert response.status == expected_status, (
        f"GET {route}: {response.status}: {response.text()}"
    )
    return response.json()


def check_response(event, expected_status: int, description: str):
    response = event.value
    assert response.status == expected_status, (
        f"{description}: HTTP {response.status}: {response.text()}"
    )
    return response


def read_mailbox() -> list[dict]:
    if not MAILBOX.exists():
        return []
    items: list[dict] = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("data"):
                items.append(item)
        except json.JSONDecodeError:
            continue
    return items


def mail_parts(item: dict) -> tuple[str, str, list[str]]:
    message = message_from_string(str(item["data"]))
    subject = str(message.get("Subject", ""))
    if message.is_multipart():
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(
                        payload.decode(
                            part.get_content_charset() or "utf-8", "replace"
                        )
                    )
        body = "\n".join(parts)
    else:
        payload = message.get_payload(decode=True)
        body = (
            payload.decode(message.get_content_charset() or "utf-8", "replace")
            if payload
            else str(message.get_payload())
        )
    return subject, body, [
        str(address).lower() for address in item.get("rcpt_tos", [])
    ]


def wait_mail(subject_text: str, recipient: str, offset: int, timeout: float = 40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in read_mailbox()[offset:]:
            subject, body, recipients = mail_parts(item)
            if subject_text.lower() in subject.lower() and recipient.lower() in recipients:
                return item, body
        time.sleep(0.25)
    subjects = [mail_parts(item)[0] for item in read_mailbox()[offset:]]
    raise AssertionError(
        f"No {subject_text!r} email delivered to {recipient}; received: {subjects}"
    )


def message_link(body: str, route: str) -> str:
    match = re.search(
        rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body
    )
    assert match, f"No one-time link for {route} was present in the delivered email."
    return match.group(0).rstrip(".,);")


def helper(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "tests/ci_helpers.py", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return result.stdout.strip()


def add_role_user(client_id: uuid.UUID, role: str, label: str) -> str:
    email = f"{role.lower()}-{label}@northstar.example.com"
    helper("seed-role", str(client_id), role, email)
    return email


def login_ui(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(
        lambda response: response.url.endswith("/v1/auth/login")
        and response.request.method == "POST"
    ) as event:
        page.get_by_role("button", name=re.compile("Sign in")).click()
    check_response(event, 200, "Sign in")
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=15000)


def logout_ui(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out", exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=10000)


def main() -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    MAILBOX.parent.mkdir(parents=True, exist_ok=True)
    MAILBOX.write_text("", encoding="utf-8")

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            accept_downloads=True,
            device_scale_factor=1,
        )
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: javascript_errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        # Public entry, registration, delivered verification email, sign-in.
        page.goto(FRONTEND + "/welcome", wait_until="networkidle")
        page.get_by_role("heading", name=re.compile("Understand the market")).wait_for()
        shot(page, "01-public-home.png")
        page.get_by_role("link", name=re.compile("Create workspace")).click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "02-registration-form.png")
        page.get_by_label("Organization name").fill(ORGANIZATION)
        page.get_by_label("Your full name").fill(FULL_NAME)
        page.get_by_label("Work email").fill(EMAIL)
        page.get_by_label("Password").fill(INITIAL_PASSWORD)
        verification_offset = len(read_mailbox())
        with page.expect_response(
            lambda response: response.url.endswith("/v1/auth/register")
            and response.request.method == "POST"
        ) as registration:
            page.get_by_role("button", name=re.compile("Create workspace")).click()
        registration_response = check_response(registration, 201, "Workspace registration")
        assert registration_response.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "03-verification-email-sent.png")

        _, verification_body = wait_mail(
            "Verify your Nexora account", EMAIL, verification_offset
        )
        verification_url = message_link(verification_body, "verify-email")
        assert urlparse(verification_url).path.endswith("/verify-email")
        page.goto(verification_url, wait_until="networkidle")
        shot(page, "04-email-verification-page.png")
        with page.expect_response(
            lambda response: response.url.split("?")[0].endswith("/v1/auth/verify-email")
        ) as verification:
            page.get_by_role("button", name=re.compile("Verify email")).click()
        check_response(verification, 200, "Verify email")
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "05-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login_ui(page, EMAIL, INITIAL_PASSWORD)
        shot(page, "06-empty-dashboard.png")
        me = get_json(page, "/v1/auth/me")
        assert me["role"] == "ORG_ADMIN"
        client_id = uuid.UUID(me["client_id"])

        # Competitor lifecycle and duplicate domain rule.
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Retail Product Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/competitors")
            and response.request.method == "POST"
        ) as competitor_create:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        competitor_resp = check_response(competitor_create, 201, "Create competitor")
        competitor = competitor_resp.json()
        competitor_id = competitor["id"]
        page.get_by_text("Retail Product Reference", exact=True).wait_for()
        shot(page, "08-competitor-created.png")

        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/competitors")
            and response.request.method == "POST"
        ) as duplicate_event:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        duplicate = check_response(duplicate_event, 403, "Duplicate domain guard")
        assert "already exists" in duplicate.text().lower()
        shot(page, "09-duplicate-domain-guard.png")
        page.get_by_role("button", name="Cancel", exact=True).click()

        # Source setup, configuration, and edit.
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("heading", name="Sources", exact=True).wait_for()
        page.get_by_role("button", name="Add source", exact=True).click()
        choose(page, "Competitor", "Retail Product Reference")
        page.get_by_label("Source name").fill("Retail Product Listing")
        page.get_by_label("Base URL").fill(HIGH_URL)
        choose(page, "Source type", "OFFICIAL_STORE")
        choose(page, "Collection method", "HTTP_FAST")
        shot(page, "10-source-form.png")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/sources")
            and response.request.method == "POST"
        ) as source_create:
            page.get_by_role("button", name="Create source", exact=True).click()
        source_resp = check_response(source_create, 201, "Create source")
        source_id = source_resp.json()["id"]
        page.get_by_text("Retail Product Listing", exact=True).wait_for()
        page.get_by_title("Configuration", exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page, "11-source-configuration.png")
        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/sources/{source_id}/configuration")
            and response.request.method == "PATCH"
        ) as source_config:
            page.get_by_role("button", name="Save configuration", exact=True).click()
        check_response(source_config, 200, "Update source configuration")
        source_readback = get_json(page, f"/v1/sources/{source_id}")
        assert source_readback["configuration"]["rate_limit_rpm"] == 60
        assert source_readback["configuration"]["max_retries"] == 1

        page.get_by_title("Edit source", exact=True).click()
        page.get_by_label("Source name").fill("Retail Product Listing - Structured")
        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/sources/{source_id}")
            and response.request.method == "PATCH"
        ) as source_edit:
            page.get_by_role("button", name="Save changes", exact=True).click()
        check_response(source_edit, 200, "Edit source")
        source_name = "Retail Product Listing - Structured"
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "12-source-configured.png")

        # Product catalog CRUD, detail/history, monitoring and dynamic fields.
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
        page.get_by_label("Product URL").fill(
            "https://www.sony.com/electronics/headband-headphones/wh-1000xm5"
        )
        page.get_by_label("Description").fill(
            "Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth."
        )
        page.get_by_label("Dynamic attributes (JSON)").fill(
            '{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}'
        )
        shot(page, "13-offering-form.png")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/offerings")
            and response.request.method == "POST"
        ) as offering_create:
            page.get_by_role("button", name="Create offering", exact=True).click()
        offering_resp = check_response(offering_create, 201, "Create offering")
        offering_id = offering_resp.json()["id"]
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "14-catalog-created.png")

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_role("button", name=OFFERING_NAME, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "15-offering-detail.png")
        page.keyboard.press("Escape")

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/offerings/{offering_id}")
            and response.request.method == "PATCH"
        ) as offering_edit:
            page.get_by_role("button", name="Save changes", exact=True).click()
        check_response(offering_edit, 200, "Edit offering")
        detail = get_json(page, f"/v1/offerings/{offering_id}")
        assert float(detail["current_price"]) == 429.99
        assert detail["attributes"]["battery_life_hours"] == 30

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring")
        ) as toggle_off:
            row.get_by_title("Toggle monitoring", exact=True).click()
        check_response(toggle_off, 200, "Pause offering monitoring")
        assert get_json(page, f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring")
        ) as toggle_on:
            row.get_by_title("Toggle monitoring", exact=True).click()
        check_response(toggle_on, 200, "Resume offering monitoring")
        assert get_json(page, f"/v1/offerings/{offering_id}")["is_monitored"] is True
        shot(page, "16-monitoring-enabled.png")

        page.get_by_role("button", name="Dynamic fields", exact=True).click()
        page.get_by_text("Dynamic field definitions", exact=True).wait_for()
        shot(page, "17-dynamic-fields.png")
        page.get_by_role("button", name="Add field", exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months")
        page.get_by_label("Category").fill("Commercial")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/offerings/fields")
            and response.request.method == "POST"
        ) as field_create:
            page.get_by_role("button", name="Create field", exact=True).click()
        check_response(field_create, 201, "Create dynamic field")
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "18-dynamic-field-created.png")
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(
            lambda response: "/v1/offerings/fields/" in response.url
            and response.request.method == "DELETE"
        ) as field_delete:
            field_row.locator("button.danger-icon").click()
        check_response(field_delete, 204, "Delete temporary dynamic field")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")

        # Bulk import/export/archive is performed via the real catalog UI.
        page.get_by_role("button", name="Bulk import", exact=True).click()
        bulk_rows = [
            {
                "name": "Sony ULT WEAR Wireless Headphones",
                "offering_type": "PRODUCT",
                "sku": "ULTWEAR-BLK",
                "current_price": "199.99",
                "currency": "USD",
                "market": "US",
                "url": "https://www.sony.com/electronics/headband-headphones/ult-wear",
                "category": "Electronics > Audio",
                "attributes": {"brand": "Sony", "color": "Black"},
            },
            {
                "name": "Sony LinkBuds S",
                "offering_type": "PRODUCT",
                "sku": "LINKBUDS-S-BLK",
                "current_price": "199.99",
                "currency": "USD",
                "market": "US",
                "url": "https://www.sony.com/electronics/truly-wireless/linkbuds-s",
                "category": "Electronics > Audio",
                "attributes": {"brand": "Sony", "fit": "In-ear"},
            },
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk_rows))
        shot(page, "19-bulk-import-form.png")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/offerings/bulk")
            and response.request.method == "POST"
        ) as bulk_import:
            page.get_by_role("button", name="Import offerings", exact=True).click()
        bulk_resp = check_response(bulk_import, 200, "Bulk import offerings")
        assert bulk_resp.json()["successful_count"] == 2
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()

        csv_file = Path("/tmp/nexora-catalog.csv")
        json_file = Path("/tmp/nexora-catalog.json")
        with page.expect_download(timeout=15000) as csv_event:
            page.get_by_role("button", name="CSV", exact=True).click()
        csv_event.value.save_as(str(csv_file))
        assert csv_file.exists() and csv_file.stat().st_size > 0
        with page.expect_download(timeout=15000) as json_event:
            page.get_by_role("button", name="JSON", exact=True).click()
        json_event.value.save_as(str(json_file))
        assert json_file.exists() and json_file.stat().st_size > 0

        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(
            lambda response: response.url.endswith("/v1/offerings/bulk-archive")
            and response.request.method == "POST"
        ) as archive_bulk:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        archive_resp = check_response(archive_bulk, 200, "Bulk archive")
        assert archive_resp.json()["archived_count"] == 1
        archive_readback = get_json(
            page,
            "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20",
        )
        assert any(row["is_archived"] for row in archive_readback["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING_NAME, exact=True).wait_for()
        shot(page, "20-catalog-import-export-archive.png")

        # Map competitor item and collect the first real price observation.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("heading", name="Offering matches", exact=True).wait_for()
        page.get_by_role("button", name="Add match", exact=True).click()
        choose(page, "Offering", OFFERING_NAME)
        choose(page, "Source", source_name)
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        choose(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "21-offering-match-form.png")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/offering-matches")
            and response.request.method == "POST"
        ) as match_create:
            page.get_by_role("button", name="Create match", exact=True).click()
        match_resp = check_response(match_create, 201, "Create offering match")
        match_id = match_resp.json()["id"]
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        page.get_by_text(HIGH_URL, exact=False).wait_for()
        shot(page, "22-offering-match-created.png")
        with page.expect_response(
            lambda response: "/v1/collections/run" in response.url
            and response.request.method == "POST"
        ) as collect_high:
            row.get_by_title("Run collection", exact=True).click()
        high_resp = collect_high.value
        assert high_resp.status in (200, 201), high_resp.text()
        high = high_resp.json()
        assert high["status"] == "COMPLETED", high
        assert high["observation"]["price"] == "399.99", high
        assert high["observation"]["http_status_code"] == 200, high
        history = get_json(page, f"/v1/offerings/{offering_id}/history")
        assert len(history) >= 1, history
        shot(page, "23-first-price-observation.png")

        # An actual alert rule is triggered by the lower second observation.
        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("heading", name="Alerts", exact=True).wait_for()
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page, "Alert type", "PERCENTAGE_DROP")
        choose(page, "Offering scope", OFFERING_NAME)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "24-alert-rule-form.png")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/alerts/rules")
            and response.request.method == "POST"
        ) as alert_create:
            page.get_by_role("button", name="Create rule", exact=True).click()
        alert_resp = check_response(alert_create, 201, "Create alert rule")
        rule_id = alert_resp.json()["id"]
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "25-alert-rule-created.png")

        page.get_by_role("link", name="Matches", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        with page.expect_response(
            lambda response: response.url.endswith(f"/v1/offering-matches/{match_id}")
            and response.request.method == "PATCH"
        ) as match_edit:
            page.get_by_role("button", name="Save changes", exact=True).click()
        check_response(match_edit, 200, "Update matched URL")

        row = page.locator("tbody tr").filter(has_text=OFFERING_NAME).first
        with page.expect_response(
            lambda response: "/v1/collections/run" in response.url
            and response.request.method == "POST"
        ) as collect_low:
            row.get_by_title("Run collection", exact=True).click()
        low_resp = collect_low.value
        assert low_resp.status in (200, 201), low_resp.text()
        low = low_resp.json()
        assert low["observation"]["price"] == "349.99", low
        assert low["snapshot"]["previous_price"] == "399.99", low
        assert low["snapshot"]["percentage_difference"].startswith("-12.50%"), low

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        logs = get_json(page, "/v1/alerts/logs")
        log = next((item for item in logs if item["title"] == "Competitor price dropped"), None)
        assert log is not None and log["is_read"] is False, logs
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        logs_after = get_json(page, "/v1/alerts/logs")
        assert any(item["id"] == log["id"] and item["is_read"] for item in logs_after)

        mail_offset = len(read_mailbox())
        dispatch = json.loads(helper("dispatch-notifications"))
        assert int(dispatch.get("sent", 0)) >= 1, dispatch
        _, alert_body = wait_mail(
            "Nexora Competitive Alert: Competitor price dropped", EMAIL, mail_offset
        )
        assert "Nexora detected a competitive intelligence event" in alert_body
        status_rows = json.loads(helper("notification-statuses", str(client_id)))
        assert "SENT" in status_rows, status_rows
        shot(page, "26-alert-triggered-and-read.png")

        # Job telemetry and detail connect to the collection records above.
        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_role("heading", name="Jobs", exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH", exact=False).wait_for()
        shot(page, "27-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "28-job-detail.png")
        page.keyboard.press("Escape")

        # Discovery enforces the public-URL boundary and imports evidenced product data.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_role("heading", name="Website discovery", exact=True).wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/discovery/run")
            and response.request.method == "POST"
        ) as blocked_discovery:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        blocked = check_response(blocked_discovery, 422, "Private discovery target rejected")
        assert "url" in blocked.text().lower() or "prohibited" in blocked.text().lower()
        shot(page, "29-private-target-blocked.png")

        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        choose(page, "Default type", "PRODUCT")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/discovery/run")
            and response.request.method == "POST"
        ) as discovery:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        discovery_resp = check_response(discovery, 200, "Run website discovery")
        discovery_data = discovery_resp.json()
        assert discovery_data["status"] == "COMPLETED", discovery_data
        assert discovery_data["created_offerings_count"] == 1, discovery_data
        assert discovery_data["items"][0]["name"] == DISCOVERED_NAME, discovery_data
        page.get_by_text(DISCOVERED_NAME, exact=True).wait_for(timeout=15000)
        readback = get_json(page, f"/v1/discovery/jobs/{discovery_data['job_id']}")
        assert readback["items"][0]["sku"] == "WF1000XM5-BLK", readback
        shot(page, "30-discovery-product-created.png")

        with page.expect_response(
            lambda response: response.url.endswith("/v1/discovery/run")
            and response.request.method == "POST"
        ) as discovery_again:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        updated_discovery = check_response(discovery_again, 200, "Re-run discovery").json()
        assert updated_discovery["created_offerings_count"] == 0, updated_discovery
        assert updated_discovery["updated_offerings_count"] == 1, updated_discovery
        catalog_after = get_json(
            page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20"
        )
        assert sum(item["sku"] == "WF1000XM5-BLK" for item in catalog_after["items"]) == 1
        shot(page, "31-discovery-upsert.png")

        # Workspace settings and password changes, followed by emailed reset.
        page.get_by_text("Account settings", exact=True).click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        page.get_by_label("Company name").wait_for()
        shot(page, "32-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(
            lambda response: response.url.endswith("/v1/auth/profile")
            and response.request.method == "PATCH"
        ) as profile_update:
            page.get_by_role("button", name="Save profile", exact=True).click()
        check_response(profile_update, 200, "Update workspace profile")
        assert get_json(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"

        page.get_by_label("Current password").fill(INITIAL_PASSWORD)
        page.get_by_label("New password").fill(CHANGED_PASSWORD)
        with page.expect_response(
            lambda response: response.url.endswith("/v1/auth/change-password")
            and response.request.method == "POST"
        ) as password_change:
            page.get_by_role("button", name="Update password", exact=True).click()
        check_response(password_change, 200, "Change password")
        old_login = page.request.post(
            API + "/v1/auth/login", json={"email": EMAIL, "password": INITIAL_PASSWORD}
        )
        assert old_login.status == 401, old_login.text()
        new_login = page.request.post(
            API + "/v1/auth/login", json={"email": EMAIL, "password": CHANGED_PASSWORD}
        )
        assert new_login.status == 200, new_login.text()
        shot(page, "33-workspace-profile-and-password.png")

        logout_ui(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        reset_offset = len(read_mailbox())
        with page.expect_response(
            lambda response: response.url.endswith("/v1/auth/forgot-password")
            and response.request.method == "POST"
        ) as forgot:
            page.get_by_role("button", name="Send reset link", exact=True).click()
        check_response(forgot, 200, "Request password reset")
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "34-password-reset-requested.png")
        _, reset_body = wait_mail("Reset your Nexora password", EMAIL, reset_offset)
        reset_url = message_link(reset_body, "reset-password")
        page.goto(reset_url, wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(
            lambda response: response.url.endswith("/v1/auth/reset-password")
            and response.request.method == "POST"
        ) as reset:
            page.get_by_role("button", name="Update password", exact=True).click()
        check_response(reset, 200, "Complete password reset")
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "35-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login_ui(page, EMAIL, RESET_PASSWORD)
        shot(page, "36-dashboard-after-reset.png")

        # Role-aware UI and server permissions use persisted users and genuine tokens.
        role_suffix = uuid.uuid4().hex[:10]
        viewer_email = add_role_user(client_id, "VIEWER", role_suffix)
        analyst_email = add_role_user(client_id, "ANALYST", role_suffix)

        logout_ui(page)
        login_ui(page, analyst_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        analyst_write = page.request.post(
            API + "/v1/competitors",
            headers={"Authorization": f"Bearer {token(page)}"},
            json={"name": "Analyst forbidden write", "domain": "analyst-forbidden.example"},
        )
        assert analyst_write.status == 403, analyst_write.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name="Run discovery", exact=True).is_visible()
        shot(page, "37-analyst-permissions.png")

        logout_ui(page)
        login_ui(page, viewer_email, ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        viewer_read = page.request.get(
            API + "/v1/offerings",
            headers={"Authorization": f"Bearer {token(page)}"},
        )
        assert viewer_read.status == 200, viewer_read.text()
        viewer_write = page.request.post(
            API + "/v1/competitors",
            headers={"Authorization": f"Bearer {token(page)}"},
            json={"name": "Viewer forbidden write", "domain": "viewer-forbidden.example"},
        )
        assert viewer_write.status == 403, viewer_write.text()
        shot(page, "38-viewer-read-only.png")

        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(FRONTEND + "/welcome", wait_until="networkidle")
        dimensions = mobile.evaluate(
            "() => ({viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth})"
        )
        assert dimensions["content"] <= dimensions["viewport"], dimensions
        shot(mobile, "39-mobile-home.png")

        if javascript_errors:
            raise AssertionError("Uncaught browser errors: " + " | ".join(javascript_errors))
        required = [
            "01-public-home.png", "02-registration-form.png",
            "03-verification-email-sent.png", "04-email-verification-page.png",
            "05-email-verified.png", "06-empty-dashboard.png",
            "07-competitors.png", "08-competitor-created.png",
            "09-duplicate-domain-guard.png", "10-source-form.png",
            "11-source-configuration.png", "12-source-configured.png",
            "13-offering-form.png", "14-catalog-created.png",
            "15-offering-detail.png", "16-monitoring-enabled.png",
            "17-dynamic-fields.png", "18-dynamic-field-created.png",
            "19-bulk-import-form.png", "20-catalog-import-export-archive.png",
            "21-offering-match-form.png", "22-offering-match-created.png",
            "23-first-price-observation.png", "24-alert-rule-form.png",
            "25-alert-rule-created.png", "26-alert-triggered-and-read.png",
            "27-job-history.png", "28-job-detail.png",
            "29-private-target-blocked.png", "30-discovery-product-created.png",
            "31-discovery-upsert.png", "32-workspace-settings.png",
            "33-workspace-profile-and-password.png", "34-password-reset-requested.png",
            "35-password-reset-complete.png", "36-dashboard-after-reset.png",
            "37-analyst-permissions.png", "38-viewer-read-only.png",
            "39-mobile-home.png",
        ]
        missing = [name for name in required if not (SCREENSHOTS / name).is_file()]
        assert not missing, f"Missing walkthrough screenshots: {missing}"
        print(f"Nexora end-to-end journey passed; {len(required)} screenshots captured.", flush=True)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

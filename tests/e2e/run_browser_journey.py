"""Browser-driven Nexora walkthrough against a live FastAPI + PostgreSQL stack."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

BASE = os.getenv("NEXORA_FRONTEND_URL", "http://127.0.0.1:4173").rstrip("/")
API = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAILBOX = Path(os.getenv("NEXORA_MAILBOX", "/tmp/nexora-mailbox.jsonl"))
SHOTS = Path(os.getenv("NEXORA_SCREENSHOTS", "docs/walkthrough/screenshots"))
INITIAL_PASSWORD = "Nexora-Strong!2026"
CHANGED_PASSWORD = "Nexora-Changed!2026"
RESET_PASSWORD = "Nexora-Final!2026"
ROLE_PASSWORD = "Nexora-Role!2026"
USER_EMAIL = "market-analyst@northstar.example.com"
ORG = "Northstar Market Intelligence"
FULL_NAME = "Alex Morgan"
HIGH_URL = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-high.html"
LOW_URL = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-low.html"
DISCOVERY_URL = "https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-discovery-product.html"
OFFERING = "Sony WH-1000XM5 Wireless Headphones"
DISCOVERED = "Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
errors: list[str] = []


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / name), full_page=True, animations="disabled", caret="hide")
    print(f"Screenshot: {name}", flush=True)


def select_option(page: Page, label: str, option: str) -> None:
    field = page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option", name=re.compile(re.escape(option), re.I)).last.click()


def token(page: Page) -> str:
    value = page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value, "Expected a signed-in browser session."
    return str(value)


def api_get(page: Page, path: str, expected: int = 200):
    response = page.request.get(API + path, headers={"Authorization": f"Bearer {token(page)}"})
    assert response.status == expected, f"GET {path}: {response.status} {response.text()}"
    return response.json()


def call_support(command: str, *args: str):
    script = Path(__file__).with_name("support.py")
    result = subprocess.run(
        [sys.executable, str(script), command, *args],
        check=True, capture_output=True, text=True, timeout=90,
        env=os.environ.copy(),
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def mails() -> list[dict]:
    if not MAILBOX.exists():
        return []
    found = []
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if isinstance(entry, dict) and entry.get("data"):
                found.append(entry)
        except json.JSONDecodeError:
            continue
    return found


def parsed_mail(item: dict) -> tuple[str, str, list[str]]:
    msg = message_from_string(str(item.get("data", "")))
    subject = str(msg.get("Subject", ""))
    if msg.is_multipart():
        bodies = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    bodies.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        body = "\n".join(bodies)
    else:
        raw = msg.get_payload(decode=True)
        body = raw.decode(msg.get_content_charset() or "utf-8", "replace") if raw else str(msg.get_payload())
    recipients = [str(x).lower() for x in item.get("rcpt_tos", [])]
    return subject, body, recipients


def wait_mail(subject_part: str, recipient: str, start: int, timeout: float = 40) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in mails()[start:]:
            subject, body, recipients = parsed_mail(entry)
            if subject_part.casefold() in subject.casefold() and recipient.casefold() in recipients:
                return body
        time.sleep(0.25)
    subjects = [parsed_mail(x)[0] for x in mails()[start:]]
    raise AssertionError(f"Expected email {subject_part!r} for {recipient}; recent subjects: {subjects}")


def find_link(body: str, route: str) -> str:
    match = re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+", body)
    assert match, f"No {route} one-time link found in the delivered message."
    return match.group(0).rstrip(".,);")


def login(page: Page, email: str, password: str) -> None:
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(
        lambda r: urlparse(r.url).path.endswith("/v1/auth/login") and r.request.method == "POST"
    ) as result:
        page.get_by_role("button", name=re.compile("Sign in")).click()
    assert result.value.status == 200, result.value.text()
    page.get_by_role("heading", name="Dashboard", exact=True).wait_for(timeout=15000)


def logout(page: Page) -> None:
    page.locator("button.profile-trigger").click()
    page.get_by_role("menuitem", name="Sign out").click()
    page.get_by_label("Work email").wait_for(timeout=15000)


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    if MAILBOX.exists():
        MAILBOX.unlink()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda dialog: dialog.accept())

        # Public entry, account creation, real STARTTLS verification and sign-in.
        page.goto(BASE + "/welcome", wait_until="networkidle")
        page.get_by_role("heading", name=re.compile("Understand the market")).wait_for()
        shot(page, "00-public-homepage.png")
        page.get_by_role("link", name=re.compile("Create workspace")).first.click()
        page.get_by_label("Organization name").wait_for()
        shot(page, "01-registration-form.png")
        page.get_by_label("Organization name").fill(ORG)
        page.get_by_label("Your full name").fill(FULL_NAME)
        page.get_by_label("Work email").fill(USER_EMAIL)
        page.get_by_label("Password").fill(INITIAL_PASSWORD)
        mail_count = len(mails())
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/auth/register") and r.request.method == "POST") as registered:
            page.get_by_role("button", name=re.compile("Create workspace")).click()
        assert registered.value.status == 201, registered.value.text()
        assert registered.value.json()["email_verification_required"] is True
        page.get_by_role("heading", name="Verification required").wait_for()
        shot(page, "02-verification-email-sent.png")
        verify_link = find_link(wait_mail("Verify your Nexora account", USER_EMAIL, mail_count), "verify-email")
        page.goto(verify_link, wait_until="networkidle")
        shot(page, "03-email-verification-page.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/auth/verify-email") and r.request.method == "GET") as verified:
            page.get_by_role("button", name=re.compile("Verify email")).click()
        assert verified.value.status == 200, verified.value.text()
        page.get_by_role("link", name="Continue to sign in", exact=True).wait_for()
        shot(page, "04-email-verified.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page, "05-login.png")
        login(page, USER_EMAIL, INITIAL_PASSWORD)
        shot(page, "06-dashboard-empty-workspace.png")
        profile = api_get(page, "/v1/auth/me")
        client_id = str(profile["client_id"])
        assert profile["role"] == "ORG_ADMIN"

        # Competitor management, duplicate domain guard and update.
        competitor_name = "Retail Product Reference Draft"
        final_competitor = "Retail Product Reference"
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        shot(page, "07-competitors-empty.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill(competitor_name)
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/competitors") and r.request.method == "POST") as created_competitor:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert created_competitor.value.status == 201, created_competitor.value.text()
        competitor_id = created_competitor.value.json()["id"]
        page.get_by_text(competitor_name, exact=True).wait_for()
        shot(page, "08-competitor-created.png")
        page.get_by_role("button", name="Add competitor", exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/competitors") and r.request.method == "POST") as duplicate:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert duplicate.value.status == 403, duplicate.value.text()
        page.get_by_text("A competitor with this domain already exists.").wait_for()
        shot(page, "09-duplicate-domain-rejected.png")
        page.get_by_role("button", name="Cancel", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=competitor_name).first
        row.locator("button").first.click()
        page.get_by_label("Name").fill(final_competitor)
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/competitors/{competitor_id}") and r.request.method == "PATCH") as changed_competitor:
            page.get_by_role("button", name="Save competitor", exact=True).click()
        assert changed_competitor.value.status == 200, changed_competitor.value.text()
        competitor_name = final_competitor
        assert any(x["id"] == competitor_id and x["name"] == competitor_name for x in api_get(page, "/v1/competitors"))

        # Source management and persisted source configuration.
        source_name = "Retail Product Listing"
        page.get_by_role("link", name="Sources", exact=True).click()
        page.get_by_role("heading", name="Sources", exact=True).wait_for()
        page.get_by_role("button", name="Add source", exact=True).click()
        select_option(page, "Competitor", competitor_name)
        page.get_by_label("Source name").fill(source_name)
        page.get_by_label("Base URL").fill(HIGH_URL)
        select_option(page, "Source type", "OFFICIAL_STORE")
        select_option(page, "Collection method", "HTTP_FAST")
        shot(page, "10-source-creation-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/sources") and r.request.method == "POST") as created_source:
            page.get_by_role("button", name="Create source", exact=True).click()
        assert created_source.value.status == 201, created_source.value.text()
        source_id = created_source.value.json()["id"]
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
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/sources/{source_id}/configuration") and r.request.method == "PATCH") as configured:
            page.get_by_role("button", name="Save configuration", exact=True).click()
        assert configured.value.status == 200, configured.value.text()
        saved_source = api_get(page, f"/v1/sources/{source_id}")
        assert saved_source["configuration"]["rate_limit_rpm"] == 60
        assert saved_source["configuration"]["max_retries"] == 1
        page.get_by_title("Edit source", exact=True).click()
        source_name = "Retail Product Listing (Structured)"
        page.get_by_label("Source name").fill(source_name)
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/sources/{source_id}") and r.request.method == "PATCH") as edited_source:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert edited_source.value.status == 200, edited_source.value.text()
        page.get_by_text(source_name, exact=True).wait_for()
        shot(page, "13-source-configuration-saved.png")

        # Canonical catalogue, update, monitoring, custom field, bulk import/export/archive.
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("heading", name="Offerings", exact=True).wait_for()
        page.get_by_role("button", name="Add offering", exact=True).click()
        page.get_by_label("Name").fill(OFFERING)
        select_option(page, "Offering type", "PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page, "14-offering-creation-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/offerings") and r.request.method == "POST") as created_offering:
            page.get_by_role("button", name="Create offering", exact=True).click()
        assert created_offering.value.status == 201, created_offering.value.text()
        offering_id = created_offering.value.json()["id"]
        page.get_by_text(OFFERING, exact=True).wait_for()
        shot(page, "15-catalog-offering-created.png")
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_role("button", name=OFFERING, exact=True).click()
        page.get_by_text("CURRENT PRICE", exact=True).wait_for()
        shot(page, "16-offering-detail-and-history.png")
        page.get_by_role("button", name="Close dialog").click()
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/offerings/{offering_id}") and r.request.method == "PATCH") as edited_offering:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert edited_offering.value.status == 200, edited_offering.value.text()
        assert float(api_get(page, f"/v1/offerings/{offering_id}")["current_price"]) == 429.99
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method == "POST") as paused:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert paused.value.status == 200
        assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method == "POST") as resumed:
            row.get_by_title("Toggle monitoring", exact=True).click()
        assert resumed.value.status == 200
        assert api_get(page, f"/v1/offerings/{offering_id}")["is_monitored"] is True
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
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/offerings/fields") and r.request.method == "POST") as field_created:
            page.get_by_role("button", name="Create field", exact=True).click()
        assert field_created.value.status == 201, field_created.value.text()
        page.get_by_text("Review Cycle", exact=True).wait_for()
        shot(page, "20-dynamic-field-created.png")
        field_row = page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(lambda r: "/v1/offerings/fields/" in urlparse(r.url).path and r.request.method == "DELETE") as field_deleted:
            field_row.locator("button.danger-icon").click()
        assert field_deleted.value.status == 204
        page.get_by_role("button", name="Close dialog").click()

        page.get_by_role("button", name="Bulk import", exact=True).click()
        import_rows = [
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}},
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(import_rows))
        shot(page, "21-bulk-import-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/offerings/bulk") and r.request.method == "POST") as bulk_import:
            page.get_by_role("button", name="Import offerings", exact=True).click()
        assert bulk_import.value.status == 200, bulk_import.value.text()
        assert bulk_import.value.json()["successful_count"] == 2
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        shot(page, "22-bulk-imported-catalog.png")
        for fmt in ("CSV", "JSON"):
            with page.expect_download(timeout=15000) as downloaded:
                page.get_by_role("button", name=fmt, exact=True).click()
            path = Path(f"/tmp/nexora-export-{fmt.lower()}")
            downloaded.value.save_as(str(path))
            assert path.exists() and path.stat().st_size > 0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones", exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones", exact=True).check()
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/offerings/bulk-archive") and r.request.method == "POST") as archived:
            page.get_by_role("button", name="Archive 1", exact=True).click()
        assert archived.value.status == 200, archived.value.text()
        assert archived.value.json()["archived_count"] == 1
        archive_readback = api_get(page, "/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true")
        assert any(row["is_archived"] for row in archive_readback["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING, exact=True).wait_for()
        shot(page, "23-catalog-import-export-and-archive.png")

        # Collection creates real observation/snapshot records and a triggered alert.
        page.get_by_role("link", name="Matches", exact=True).click()
        page.get_by_role("heading", name="Offering matches", exact=True).wait_for()
        page.get_by_role("button", name="Add match", exact=True).click()
        select_option(page, "Offering", OFFERING)
        select_option(page, "Source", source_name)
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        select_option(page, "Status", "APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page, "24-offering-match-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/offering-matches") and r.request.method == "POST") as created_match:
            page.get_by_role("button", name="Create match", exact=True).click()
        assert created_match.value.status == 201, created_match.value.text()
        match_id = created_match.value.json()["id"]
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        page.get_by_text(HIGH_URL, exact=False).wait_for()
        shot(page, "25-offering-match-created.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/collections/run") and r.request.method == "POST") as first_collect:
            row.get_by_title("Run collection", exact=True).click()
        first = first_collect.value
        assert first.status in (200, 201), first.text()
        first_data = first.json()
        assert first_data["status"] == "COMPLETED", first_data
        assert first_data["observation"]["price"] == "399.99", first_data
        history = api_get(page, f"/v1/offerings/{offering_id}/history")
        assert len(history) >= 1, history
        shot(page, "26-first-price-observation.png")

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_role("heading", name="Alerts", exact=True).wait_for()
        page.get_by_role("button", name="New rule", exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        select_option(page, "Alert type", "PERCENTAGE_DROP")
        select_option(page, "Offering scope", OFFERING)
        page.get_by_label("Threshold").fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page, "27-alert-rule-form.png")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/alerts/rules") and r.request.method == "POST") as rule:
            page.get_by_role("button", name="Create rule", exact=True).click()
        assert rule.value.status == 201, rule.value.text()
        rule_id = rule.value.json()["id"]
        page.get_by_text("Sony retail price drop", exact=True).wait_for()
        shot(page, "28-alert-rule-created.png")

        page.get_by_role("link", name="Matches", exact=True).click()
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        row.get_by_title("Edit", exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        with page.expect_response(lambda r: urlparse(r.url).path.endswith(f"/v1/offering-matches/{match_id}") and r.request.method == "PATCH") as changed_match:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert changed_match.value.status == 200, changed_match.value.text()
        row = page.locator("tbody tr").filter(has_text=OFFERING).first
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/collections/run") and r.request.method == "POST") as second_collect:
            row.get_by_title("Run collection", exact=True).click()
        second = second_collect.value
        assert second.status in (200, 201), second.text()
        second_data = second.json()
        assert second_data["observation"]["price"] == "349.99", second_data
        assert second_data["snapshot"]["previous_price"] == "399.99", second_data
        assert str(second_data["snapshot"]["percentage_difference"]).startswith("-12.50%"), second_data

        page.get_by_role("link", name="Alerts", exact=True).click()
        page.get_by_text("Competitor price dropped", exact=True).wait_for(timeout=15000)
        logs = api_get(page, "/v1/alerts/logs")
        alert_log = next((item for item in logs if item["title"] == "Competitor price dropped"), None)
        assert alert_log is not None, logs
        assert not alert_log["is_read"]
        page.get_by_title("Mark as read", exact=True).click()
        page.get_by_title("Read", exact=True).wait_for()
        logs_after = api_get(page, "/v1/alerts/logs")
        assert any(item["id"] == alert_log["id"] and item["is_read"] for item in logs_after)
        shot(page, "29-alert-generated-and-read.png")
        mail_start = len(mails())
        dispatched = call_support("dispatch-notifications")
        assert int(dispatched.get("sent", 0)) >= 1, dispatched
        alert_body = wait_mail("Nexora Competitive Alert: Competitor price dropped", USER_EMAIL, mail_start)
        assert "competitive intelligence event" in alert_body.lower()
        mail_statuses = call_support("notification-statuses", client_id)
        assert "SENT" in mail_statuses, mail_statuses

        page.get_by_role("link", name="Jobs", exact=True).click()
        page.get_by_role("heading", name="Jobs", exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH", exact=True).wait_for()
        shot(page, "30-collection-job-history.png")
        page.get_by_role("button", name="Details", exact=True).first.click()
        page.get_by_text("Job ID", exact=True).wait_for()
        shot(page, "31-collection-job-detail.png")
        page.get_by_role("button", name="Close dialog").click()

        # SSRF rejection then evidence-based product discovery and idempotent upsert.
        page.get_by_role("link", name="Discovery", exact=True).click()
        page.get_by_role("heading", name="Website discovery", exact=True).wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/discovery/run") and r.request.method == "POST") as blocked:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        assert blocked.value.status == 422, blocked.value.text()
        shot(page, "32-private-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        select_option(page, "Default type", "PRODUCT")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/discovery/run") and r.request.method == "POST") as discovered:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        discovery = discovered.value
        assert discovery.status == 200, discovery.text()
        discovery_data = discovery.json()
        assert discovery_data["status"] == "COMPLETED", discovery_data
        assert discovery_data["created_offerings_count"] == 1, discovery_data
        assert discovery_data["items"][0]["name"] == DISCOVERED, discovery_data
        page.get_by_text(DISCOVERED, exact=True).wait_for(timeout=15000)
        shot(page, "33-discovery-product-created.png")
        job = api_get(page, f"/v1/discovery/jobs/{discovery_data['job_id']}")
        assert job["items"][0]["sku"] == "WF1000XM5-BLK"
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/discovery/run") and r.request.method == "POST") as rediscovered:
            page.get_by_role("button", name="Run discovery", exact=True).click()
        again = rediscovered.value
        assert again.status == 200, again.text()
        again_data = again.json()
        assert again_data["created_offerings_count"] == 0 and again_data["updated_offerings_count"] == 1, again_data
        offerings = api_get(page, "/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(1 for item in offerings["items"] if item.get("sku") == "WF1000XM5-BLK") == 1
        shot(page, "34-discovery-upsert-history.png")

        # Workspace profile and password change, then one-time reset by delivered email.
        page.locator("button.profile-trigger").click()
        page.get_by_role("menuitem", name="Account settings").click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        page.get_by_label("Company name").wait_for()
        shot(page, "35-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/auth/profile") and r.request.method == "PATCH") as updated_profile:
            page.get_by_role("button", name="Save profile", exact=True).click()
        assert updated_profile.value.status == 200, updated_profile.value.text()
        assert api_get(page, "/v1/auth/profile")["company_name"] == "Northstar Analytics"
        shot(page, "36-workspace-profile-updated.png")
        page.get_by_label("Current password").fill(INITIAL_PASSWORD)
        page.get_by_label("New password").fill(CHANGED_PASSWORD)
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/auth/change-password") and r.request.method == "POST") as changed_password:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert changed_password.value.status == 200, changed_password.value.text()
        old_login = page.request.post(API + "/v1/auth/login", data=json.dumps({"email": USER_EMAIL, "password": INITIAL_PASSWORD}), headers={"Content-Type": "application/json"})
        assert old_login.status == 401, old_login.text()
        new_login = page.request.post(API + "/v1/auth/login", data=json.dumps({"email": USER_EMAIL, "password": CHANGED_PASSWORD}), headers={"Content-Type": "application/json"})
        assert new_login.status == 200, new_login.text()
        shot(page, "37-password-changed.png")

        logout(page)
        page.get_by_role("link", name="Forgot password?", exact=True).click()
        page.get_by_label("Work email").fill(USER_EMAIL)
        reset_mail_start = len(mails())
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/auth/forgot-password") and r.request.method == "POST") as forgot:
            page.get_by_role("button", name="Send reset link", exact=True).click()
        assert forgot.value.status == 200, forgot.value.text()
        page.get_by_role("heading", name="Check your inbox", exact=True).wait_for()
        shot(page, "38-password-reset-requested.png")
        reset_link = find_link(wait_mail("Reset your Nexora password", USER_EMAIL, reset_mail_start), "reset-password")
        page.goto(reset_link, wait_until="networkidle")
        page.get_by_label("New password").wait_for()
        shot(page, "39-password-reset-form.png")
        page.get_by_label("New password").fill(RESET_PASSWORD)
        with page.expect_response(lambda r: urlparse(r.url).path.endswith("/v1/auth/reset-password") and r.request.method == "POST") as reset_done:
            page.get_by_role("button", name="Update password", exact=True).click()
        assert reset_done.value.status == 200, reset_done.value.text()
        page.get_by_role("heading", name="Password updated", exact=True).wait_for()
        shot(page, "40-password-reset-complete.png")
        page.get_by_role("link", name="Continue to sign in", exact=True).click()
        login(page, USER_EMAIL, RESET_PASSWORD)
        shot(page, "41-dashboard-after-password-reset.png")

        roles = call_support("create-roles", client_id, "walkthrough")
        logout(page)
        login(page, roles["analyst"], ROLE_PASSWORD)
        page.get_by_role("link", name="Competitors", exact=True).click()
        page.get_by_role("heading", name="Competitors", exact=True).wait_for()
        assert page.get_by_role("button", name="Add competitor", exact=True).count() == 0
        denied = page.request.post(API + "/v1/competitors", data=json.dumps({"name":"Not Allowed","domain":"analyst-forbidden.example"}), headers={"Authorization": f"Bearer {token(page)}", "Content-Type":"application/json"})
        assert denied.status == 403, denied.text()
        page.get_by_role("link", name="Discovery", exact=True).click()
        assert page.get_by_role("button", name="Run discovery", exact=True).is_visible()
        shot(page, "42-analyst-workspace.png")

        logout(page)
        login(page, roles["viewer"], ROLE_PASSWORD)
        page.get_by_role("link", name="Offerings", exact=True).click()
        page.get_by_role("heading", name="Offerings", exact=True).wait_for()
        assert page.get_by_role("button", name="Add offering", exact=True).count() == 0
        assert page.get_by_role("button", name="Bulk import", exact=True).count() == 0
        assert page.get_by_role("button", name="Dynamic fields", exact=True).count() == 0
        read_only = page.request.get(API + "/v1/offerings", headers={"Authorization": f"Bearer {token(page)}"})
        assert read_only.status == 200, read_only.text()
        denied = page.request.post(API + "/v1/competitors", data=json.dumps({"name":"Viewer Write Attempt","domain":"viewer-write.example"}), headers={"Authorization": f"Bearer {token(page)}", "Content-Type":"application/json"})
        assert denied.status == 403, denied.text()
        shot(page, "43-viewer-read-only-workspace.png")

        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(BASE + "/welcome", wait_until="networkidle")
        dimensions = mobile.evaluate("() => ({viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth})")
        assert dimensions["page"] <= dimensions["viewport"], dimensions
        shot(mobile, "44-mobile-homepage.png")

        if errors:
            raise AssertionError("Browser JavaScript errors: " + " | ".join(errors))
        expected = [f"{i:02d}-" for i in range(45)]
        files = sorted(path.name for path in SHOTS.glob("*.png"))
        missing = [prefix for prefix in expected if not any(name.startswith(prefix) for name in files)]
        assert not missing, f"Walkthrough captures missing for sequence: {missing}"
        print(f"Completed Nexora UI-to-API-to-PostgreSQL journey: {len(files)} screenshots.", flush=True)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

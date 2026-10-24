"""Nexora's complete browser journey: React UI -> FastAPI -> PostgreSQL and email."""
from __future__ import annotations
import asyncio, json, os, re, subprocess, sys, threading, time, uuid
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import Page, sync_playwright
from sqlalchemy import select
from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal, engine
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel

FRONTEND=os.getenv("NEXORA_FRONTEND_URL","http://127.0.0.1:4173").rstrip("/")
API=os.getenv("NEXORA_API_URL","http://127.0.0.1:8000").rstrip("/")
MAILBOX=Path(os.getenv("NEXORA_MAILBOX","/tmp/nexora-mailbox.jsonl"))
SHOTS=Path(os.getenv("NEXORA_SCREENSHOT_DIR","docs/walkthrough/screenshots"))
EMAIL=f"market-analyst-{os.getenv('GITHUB_RUN_ID',uuid.uuid4().hex[:8])}@northstar.example.com"
ORG="Northstar Market Intelligence"
PERSON="Alex Morgan"
PASSWORD="Nexora-Strong!2026"
CHANGED_PASSWORD="Nexora-Updated!2026"
RESET_PASSWORD="Nexora-Final!2026"
ROLE_PASSWORD="Nexora-Role!2026"
COMPETITOR="Retail Product Reference"
SOURCE="Retail Product Listing"
OFFERING="Sony WH-1000XM5 Wireless Headphones"
DISCOVERED="Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
HIGH_URL="https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-high.html"
LOW_URL="https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-low.html"
DISCOVERY_URL="https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-discovery-product.html"
BROWSER_ERRORS=[]
def run_async(coro):
    """Run database coroutines on a dedicated loop outside Playwright's sync loop."""
    outcome = {}

    async def execute():
        try:
            outcome["value"] = await coro
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            await engine.dispose()

    def run():
        asyncio.run(execute())

    thread = threading.Thread(target=run, name="nexora-browser-db", daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]

def shot(page: Page, name: str):
    SHOTS.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(SHOTS/name),full_page=True,animations="disabled",caret="hide")
    print("SCREENSHOT "+name,flush=True)

def choose(page: Page, label: str, option: str):
    field=page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option",name=re.compile(rf"^{re.escape(option)}(?:\s|$)")).last.click()

def token(page: Page):
    value=page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value,"Expected the signed-in browser to hold an access token."
    return str(value)

def get_api(page: Page,path: str,expected=200):
    response=page.request.get(API+path,headers={"Authorization":"Bearer "+token(page)})
    assert response.status==expected,f"GET {path}: {response.status} {response.text()}"
    return response.json()

def emails():
    if not MAILBOX.exists(): return []
    rows=[]
    for line in MAILBOX.read_text(encoding="utf-8").splitlines():
        try:
            item=json.loads(line)
            if isinstance(item,dict) and item.get("data"): rows.append(item)
        except json.JSONDecodeError: pass
    return rows

def parse_email(item):
    msg=message_from_string(str(item["data"]))
    if msg.is_multipart():
        body="\n".join(
            part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8","replace")
            for part in msg.walk() if part.get_content_type()=="text/plain" and part.get_payload(decode=True)
        )
    else:
        raw=msg.get_payload(decode=True)
        body=raw.decode(msg.get_content_charset() or "utf-8","replace") if raw else str(msg.get_payload())
    return str(msg.get("Subject","")),[str(x).lower() for x in item.get("rcpt_tos",[])],body

def wait_email(subject,recipient,start,timeout=40):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        for item in emails()[start:]:
            title,recipients,body=parse_email(item)
            if subject.lower() in title.lower() and recipient.lower() in recipients: return body
        time.sleep(.25)
    raise AssertionError(f"Missing email {subject!r}; received {[parse_email(x)[0] for x in emails()[start:]]}")

def email_link(body,path):
    found=re.search(rf"https?://[^\s<>]+/{re.escape(path)}\?token=[^\s<>]+",body)
    assert found,f"Expected one-time {path} link in the email body."
    return found.group(0).rstrip(".,);")

def expect(page: Page,path: str,method="POST"):
    return page.expect_response(lambda r:r.url.startswith(API+path) and r.request.method==method)

def submit(page: Page,label: str,path: str,method="POST",status=201):
    with expect(page,path,method) as observed:
        page.get_by_role("button",name=label,exact=True).click()
    response=observed.value
    assert response.status==status,f"{method} {path}: {response.status} {response.text()}"
    return response

def login(page,email,password):
    page.get_by_label("Work email").fill(email)
    page.locator('input[autocomplete="current-password"]').fill(password)
    response=submit(page,"Sign in","/v1/auth/login",status=200)
    assert response.json().get("access_token")
    page.get_by_role("heading",name="Dashboard",exact=True).wait_for(timeout=15000)

def logout(page):
    page.locator("button.profile-trigger").click()
    page.get_by_text("Sign out",exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=10000)

async def make_role_user(client_id,role,suffix):
    email=f"{role.value.lower()}-{suffix}@northstar.example.com"
    async with AsyncSessionLocal() as session:
        user=await session.scalar(select(UserModel).where(UserModel.email==email))
        if not user:
            session.add(UserModel(client_id=client_id,email=email,full_name="Northstar "+role.value.title(),
                hashed_password=get_password_hash(ROLE_PASSWORD),role=role,is_active=True,email_verified=True))
            await session.commit()
    return email

async def get_outbox_statuses(client_id):
    async with AsyncSessionLocal() as session:
        result=await session.execute(select(NotificationOutboxModel.status).where(
            NotificationOutboxModel.client_id==client_id,NotificationOutboxModel.channel=="email"))
        return list(result.scalars().all())

def get_outbox_statuses_sync(client_id):
    code = """
import asyncio, json, sys, uuid
from sqlalchemy import select
from backend.db.session import AsyncSessionLocal
from backend.models.notification import NotificationOutboxModel
async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(NotificationOutboxModel.status).where(
            NotificationOutboxModel.client_id == uuid.UUID(sys.argv[1]),
            NotificationOutboxModel.channel == "email"))
        print(json.dumps(list(result.scalars().all())))
asyncio.run(main())
"""
    result=subprocess.run([sys.executable,"-c",code,str(client_id)],capture_output=True,text=True,check=True,env=os.environ.copy())
    lines=[line for line in result.stdout.splitlines() if line.strip().startswith("[")]
    assert lines,result.stdout+" "+result.stderr
    return json.loads(lines[-1])

def create_role_user_sync(client_id, role, suffix):
    code = """
import asyncio, sys, uuid
from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
async def main():
    client_id = uuid.UUID(sys.argv[1])
    role = RoleEnum(sys.argv[2])
    suffix = sys.argv[3]
    email = f"{role.value.lower()}-{suffix}@northstar.example.com"
    async with AsyncSessionLocal() as session:
        user = await session.scalar(__import__("sqlalchemy").select(UserModel).where(UserModel.email == email))
        if not user:
            session.add(UserModel(client_id=client_id,email=email,full_name="Northstar "+role.value.title(),
                hashed_password=get_password_hash("Nexora-Role!2026"),role=role,is_active=True,email_verified=True))
            await session.commit()
    print(email)
asyncio.run(main())
"""
    result=subprocess.run([sys.executable,"-c",code,str(client_id),role.value,suffix],capture_output=True,text=True,check=True,env=os.environ.copy())
    lines=[line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines,result.stdout+" "+result.stderr
    return lines[-1]

def dispatch_email_worker():
    code="import json; from backend.workers.tasks import dispatch_pending_notifications; print(json.dumps(dispatch_pending_notifications.run()))"
    result=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,check=True,env=os.environ.copy())
    lines=[line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert lines,result.stdout+" "+result.stderr
    return json.loads(lines[-1])

def verify_postgres_sync(client_id, competitor_id, source_id, offering_id, match_id,
                         rule_id, circuit_rule_id, alert_id, isolated_client_id, super_admin_email):
    """Verify the browser journey's important records with fresh PostgreSQL queries."""
    code = r"""import asyncio, json, sys, uuid
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from backend.db.session import AsyncSessionLocal
from backend.models.alert import AlertLogModel, AlertRuleModel
from backend.models.client import ClientModel, UserModel
from backend.models.competitor import CompetitorModel, OfferingMatchModel, SourceModel
from backend.models.enums import CircuitStateEnum, RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.models.observation import JobModel, ObservationModel, SnapshotModel
from backend.models.offering import OfferingModel

async def main():
    (tenant_id, competitor_id, source_id, offering_id, match_id, rule_id,
     circuit_rule_id, alert_id, isolated_id) = [uuid.UUID(x) for x in sys.argv[1:10]]
    super_admin_email = sys.argv[10]
    async with AsyncSessionLocal() as session:
        competitor = await session.scalar(select(CompetitorModel).where(
            CompetitorModel.id == competitor_id,
            CompetitorModel.client_id == tenant_id))
        source = await session.scalar(select(SourceModel).options(
            selectinload(SourceModel.configuration)).where(SourceModel.id == source_id))
        offering = await session.scalar(select(OfferingModel).where(
            OfferingModel.id == offering_id, OfferingModel.client_id == tenant_id))
        match = await session.scalar(select(OfferingMatchModel).where(
            OfferingMatchModel.id == match_id))
        rule = await session.scalar(select(AlertRuleModel).where(
            AlertRuleModel.id == rule_id, AlertRuleModel.client_id == tenant_id))
        circuit_rule = await session.scalar(select(AlertRuleModel).where(
            AlertRuleModel.id == circuit_rule_id, AlertRuleModel.client_id == tenant_id))
        alert = await session.scalar(select(AlertLogModel).where(AlertLogModel.id == alert_id))
        circuit_alert = await session.scalar(select(AlertLogModel).where(
            AlertLogModel.alert_rule_id == circuit_rule_id,
            AlertLogModel.title == "Competitor source circuit breaker tripped"))
        snapshot = await session.scalar(select(SnapshotModel).where(
            SnapshotModel.offering_match_id == match_id))
        observations = await session.scalar(select(func.count()).select_from(
            ObservationModel).where(ObservationModel.offering_match_id == match_id))
        jobs = await session.scalar(select(func.count()).select_from(JobModel).where(
            JobModel.source_id == source_id))
        discovered = await session.scalar(select(OfferingModel).where(
            OfferingModel.client_id == tenant_id, OfferingModel.sku == "WF1000XM5-BLK"))
        notification_statuses = list((await session.execute(select(
            NotificationOutboxModel.status).where(
                NotificationOutboxModel.client_id == tenant_id,
                NotificationOutboxModel.channel == "email"))).scalars().all())
        isolated_client = await session.scalar(select(ClientModel).where(ClientModel.id == isolated_id))
        isolated_competitors = await session.scalar(select(func.count()).select_from(
            CompetitorModel).where(CompetitorModel.client_id == isolated_id))
        isolated_offerings = await session.scalar(select(func.count()).select_from(
            OfferingModel).where(OfferingModel.client_id == isolated_id))
        isolated_alerts = await session.scalar(select(func.count()).select_from(
            AlertRuleModel).where(AlertRuleModel.client_id == isolated_id))
        super_admin = await session.scalar(select(UserModel).where(
            UserModel.email == super_admin_email, UserModel.client_id == tenant_id))
        assert competitor is not None and competitor.name == "Retail Product Reference", "Competitor not found in PostgreSQL."
        assert source is not None and source.name == "Retail Product Listing Structured", "Updated source name was not saved in PostgreSQL."
        assert source.circuit_state == CircuitStateEnum.OPEN and source.failure_count >= 5, "Source circuit state was not saved."
        assert source.configuration is not None and source.configuration.max_retries == 1, "Source configuration was not saved."
        assert offering is not None and str(offering.current_price).startswith("429.99"), "Updated catalog price was not saved."
        assert match is not None and match.match_status.value == "APPROVED" and match.target_url.endswith("retail-price-missing.html"), "Final match state was not saved."
        assert rule is not None and rule.is_active, "Price-drop rule was not saved as active."
        assert circuit_rule is not None and circuit_rule.is_active, "Circuit-breaker rule was not saved."
        assert alert is not None and alert.is_read, "Price-drop alert's read status was not saved."
        assert circuit_alert is not None, "Circuit-breaker alert log was not saved."
        assert snapshot is not None and str(snapshot.current_price).startswith("349.99"), "Latest snapshot was not saved."
        assert int(observations or 0) >= 2, f"Expected persisted observations, got {observations}."
        assert int(jobs or 0) >= 7, f"Expected successful and failed collection jobs, got {jobs}."
        assert discovered is not None, "Discovered offering was not saved."
        assert isolated_client is not None and isolated_client.id != tenant_id, "Isolated organization was not saved separately."
        assert int(isolated_competitors or 0) == 0 and int(isolated_offerings or 0) == 0 and int(isolated_alerts or 0) == 0, "New tenant was not empty in PostgreSQL."
        assert super_admin is not None and super_admin.role == RoleEnum.SUPER_ADMIN, "Super-admin identity was not saved."
        assert "SENT" in notification_statuses, f"Email outbox status not persisted: {notification_statuses}"
        print(json.dumps({
            "tenant_id": str(tenant_id),
            "isolated_tenant_id": str(isolated_id),
            "competitor_id": str(competitor.id),
            "source_id": str(source.id),
            "offering_id": str(offering.id),
            "match_id": str(match.id),
            "snapshot_price": str(snapshot.current_price),
            "observations": int(observations or 0),
            "jobs": int(jobs or 0),
            "discovered_sku": discovered.sku,
            "source_circuit_state": source.circuit_state.value,
            "notification_statuses": notification_statuses,
            "price_alert_is_read": alert.is_read,
            "circuit_alert_saved": True,
            "super_admin_saved": True,
            "isolated_tenant_empty": True
        }))
asyncio.run(main())
"""
    args = [
        str(client_id), str(competitor_id), str(source_id), str(offering_id),
        str(match_id), str(rule_id), str(circuit_rule_id), str(alert_id),
        str(isolated_client_id), str(super_admin_email),
    ]
    result = subprocess.run([sys.executable, "-c", code, *args], capture_output=True,
                            text=True, check=True, env=os.environ.copy())
    json_lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert json_lines, result.stdout + " " + result.stderr
    return json.loads(json_lines[-1])


def main():
    SHOTS.mkdir(parents=True,exist_ok=True)
    # A run must publish only screenshots captured by this run. Remove stale
    # PNGs and old DB evidence left by a prior failed journey.
    for stale_image in SHOTS.glob("*.png"):
        stale_image.unlink()
    (SHOTS / "postgres-evidence.json").unlink(missing_ok=True)
    MAILBOX.unlink(missing_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        context=browser.new_context(viewport={"width":1440,"height":1000},accept_downloads=True)
        page=context.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror",lambda error:BROWSER_ERRORS.append(str(error)))
        page.on("dialog",lambda dialog:dialog.accept())

        # Entry, account creation and real email verification.
        page.goto(FRONTEND+"/welcome",wait_until="networkidle")
        page.get_by_role("heading",name=re.compile("Understand the market")).wait_for()
        shot(page,"00-public-homepage.png")
        page.get_by_role("link",name=re.compile("Create workspace")).first.click()
        page.get_by_label("Organization name").wait_for()
        shot(page,"01-registration-form.png")
        page.get_by_label("Organization name").fill(ORG)
        page.get_by_label("Your full name").fill(PERSON)
        page.get_by_label("Work email").fill(EMAIL)
        page.locator('input[autocomplete="new-password"]').fill(PASSWORD)
        mail_start=len(emails())
        registration=submit(page,"Create workspace","/v1/auth/register",status=201)
        assert registration.json()["email_verification_required"] is True
        page.get_by_role("heading",name="Verification required",exact=True).wait_for()
        shot(page,"02-verification-email-sent.png")
        initial_verify_url=email_link(wait_email("Verify your Nexora account",EMAIL,mail_start),"verify-email")
        assert urlparse(initial_verify_url).path.endswith("/verify-email")

        # Resending verification is an actual account action: it returns a
        # second email and the newest one-time link is used for activation.
        resend_start=len(emails())
        resend=submit(page,"Resend email","/v1/auth/resend-verification",status=200)
        assert resend.status==200,resend.text()
        verify_url=email_link(wait_email("Verify your Nexora account",EMAIL,resend_start),"verify-email")
        page.get_by_text("A new verification link has been sent. Check your inbox.",exact=True).wait_for()
        shot(page,"02b-verification-email-resent.png")
        assert urlparse(verify_url).path.endswith("/verify-email")
        page.goto(verify_url,wait_until="networkidle")
        shot(page,"03-email-verification-page.png")
        verified=submit(page,"Verify email","/v1/auth/verify-email","GET",200)
        assert verified.json().get("message")
        page.get_by_role("link",name="Continue to sign in",exact=True).wait_for()
        shot(page,"04-email-verified.png")
        page.get_by_role("link",name="Continue to sign in",exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page,"05-login.png")
        login(page,EMAIL,PASSWORD)
        shot(page,"06-dashboard-starting-workspace.png")
        identity=get_api(page,"/v1/auth/me")
        assert identity["role"]=="ORG_ADMIN"
        client_id=uuid.UUID(identity["client_id"])

        # Competitor configuration, duplicate-domain guard and update.
        page.get_by_role("link",name="Competitors",exact=True).click()
        page.locator(".content h2").filter(has_text="Competitors").wait_for()
        shot(page,"07-competitors.png")
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill("Retail Product Reference (initial)")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        competitor=submit(page,"Save competitor","/v1/competitors").json()
        competitor_id=competitor["id"]
        page.get_by_text("Retail Product Reference (initial)",exact=True).wait_for()
        shot(page,"08-competitor-created.png")
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        duplicate=submit(page,"Save competitor","/v1/competitors",status=403)
        assert "already exists" in duplicate.text().lower()
        shot(page,"09-duplicate-domain-protection.png")
        page.get_by_role("button",name="Cancel",exact=True).click()
        row=page.locator("tbody tr").filter(has_text="Retail Product Reference (initial)").first
        row.locator("button").first.click()
        page.get_by_label("Name").fill(COMPETITOR)
        updated=submit(page,"Save competitor",f"/v1/competitors/{competitor_id}","PATCH",200)
        assert updated.json()["name"]==COMPETITOR
        assert any(c["id"]==competitor_id and c["name"]==COMPETITOR for c in get_api(page,"/v1/competitors"))

        # Source and source configuration.
        page.get_by_role("link",name="Sources",exact=True).click()
        page.locator(".content h2").filter(has_text="Sources").wait_for()
        page.get_by_role("button",name="Add source",exact=True).click()
        choose(page,"Competitor",COMPETITOR)
        page.get_by_label("Source name").fill(SOURCE)
        page.get_by_label("Base URL").fill(HIGH_URL)
        choose(page,"Source type","OFFICIAL_STORE")
        choose(page,"Collection method","HTTP_FAST")
        shot(page,"10-source-form.png")
        source=submit(page,"Create source","/v1/sources").json()
        source_id=source["id"]
        page.get_by_text(SOURCE,exact=True).wait_for()
        shot(page,"11-source-created.png")
        page.get_by_title("Configuration",exact=True).click()
        page.get_by_label("Rate / minute").fill("60")
        page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25")
        page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page,"12-source-settings.png")
        config=submit(page,"Save configuration",f"/v1/sources/{source_id}/configuration","PATCH",200).json()
        assert config["rate_limit_rpm"]==60 and config["max_retries"]==1

        page.get_by_title("Configuration",exact=True).click()
        page.get_by_label("Require JavaScript rendering").check()
        js_config=submit(page,"Save configuration",f"/v1/sources/{source_id}/configuration","PATCH",200).json()
        assert js_config["requires_javascript"] is True,js_config
        page.get_by_title("Configuration",exact=True).click()
        assert page.get_by_label("Require JavaScript rendering").is_checked()
        shot(page,"13b-source-js-rendering-enabled.png")
        page.get_by_label("Require JavaScript rendering").uncheck()
        config=submit(page,"Save configuration",f"/v1/sources/{source_id}/configuration","PATCH",200).json()
        assert config["requires_javascript"] is False,config

        page.get_by_title("Edit source",exact=True).click()
        page.get_by_label("Source name").fill(SOURCE+" Structured")
        source_edit=submit(page,"Save changes",f"/v1/sources/{source_id}","PATCH",200).json()
        assert source_edit["name"]==SOURCE+" Structured"
        source_rows=get_api(page,"/v1/sources")
        assert any(s["id"]==source_id and s["configuration"]["max_retries"]==1 for s in source_rows)
        shot(page,"13-source-settings-saved.png")

        # Catalog CRUD, dynamic fields, bulk import/export and archive.
        page.get_by_role("link",name="Offerings",exact=True).click()
        page.locator(".content h2").filter(has_text="Offerings").wait_for()
        page.get_by_role("button",name="Add offering",exact=True).click()
        page.get_by_label("Name").fill(OFFERING)
        choose(page,"Offering type","PRODUCT")
        page.get_by_label("SKU", exact=True).fill("WH1000XM5-BLK")
        page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page,"14-offering-form.png")
        offering=submit(page,"Create offering","/v1/offerings").json()
        offering_id=offering["id"]
        page.get_by_text(OFFERING,exact=True).wait_for()
        shot(page,"15-catalog-offering-created.png")
        page.locator("tbody tr").filter(has_text=OFFERING).first.get_by_role("button",name=OFFERING,exact=True).click()
        page.get_by_text("CURRENT PRICE",exact=True).wait_for()
        shot(page,"16-offering-details.png")
        page.keyboard.press("Escape")
        page.get_by_role("dialog").wait_for(state="detached")
        page.locator("tbody tr").filter(has_text=OFFERING).first.get_by_title("Edit",exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        edit=submit(page,"Save changes",f"/v1/offerings/{offering_id}","PATCH",200).json()
        assert float(edit["current_price"])==429.99
        assert float(get_api(page,f"/v1/offerings/{offering_id}")["current_price"])==429.99
        row=page.locator("tbody tr").filter(has_text=OFFERING).first
        with expect(page,f"/v1/offerings/{offering_id}/toggle-monitoring","POST") as toggle1:
            row.get_by_title("Toggle monitoring",exact=True).click()
        assert toggle1.value.status==200
        assert get_api(page,f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row=page.locator("tbody tr").filter(has_text=OFFERING).first
        with expect(page,f"/v1/offerings/{offering_id}/toggle-monitoring","POST") as toggle2:
            row.get_by_title("Toggle monitoring",exact=True).click()
        assert toggle2.value.status==200
        assert get_api(page,f"/v1/offerings/{offering_id}")["is_monitored"] is True
        shot(page,"17-monitoring-controls.png")
        page.get_by_role("button",name="Dynamic fields",exact=True).click()
        page.get_by_text("Dynamic field definitions",exact=True).wait_for()
        shot(page,"18-dynamic-fields.png")
        page.get_by_role("button",name="Add field",exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months")
        page.get_by_label("Category").fill("Commercial")
        shot(page,"19-dynamic-field-form.png")
        field=submit(page,"Create field","/v1/offerings/fields").json()
        page.get_by_text("Review Cycle",exact=True).wait_for()
        shot(page,"20-dynamic-field-created.png")
        field_row=page.locator("div.list-row").filter(has_text="Review Cycle").last
        with expect(page,"/v1/offerings/fields/","DELETE") as remove_field:
            field_row.locator("button.danger-icon").click()
        assert remove_field.value.status==204
        page.keyboard.press("Escape")
        page.get_by_role("button",name="Bulk import",exact=True).click()
        bulk=[
          {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
          {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}}
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(bulk))
        shot(page,"21-bulk-import-form.png")
        imported=submit(page,"Import offerings","/v1/offerings/bulk",status=200).json()
        assert imported["successful_count"]==2,imported
        page.get_by_text("Sony ULT WEAR Wireless Headphones",exact=True).wait_for()
        shot(page,"22-bulk-import-results.png")

        # Row-level import validation persists valid rows while reporting blocked URLs.
        page.get_by_role("button",name="Bulk import",exact=True).click()
        import_with_one_rejected_row = [
          {"name":"AWS EC2 Compute Optimized","offering_type":"PLAN","sku":"AWS-EC2-ONDEMAND-001","current_price":"1200.00","currency":"USD","market":"US","category":"Cloud Computing > Compute","description":"Compute-optimized cloud instance pricing example","attributes":{"brand":"Amazon Web Services","vcpu":16,"memory_gib":64}},
          {"name":"Private Endpoint Price Test","offering_type":"PRODUCT","sku":"NXR-SSRF-BLOCKED-001","current_price":"0.99","currency":"USD","market":"US","category":"Security > Validation","url":"http://127.0.0.1:8000/v1/health","attributes":{"purpose":"invalid private-network URL must be rejected"}}
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(import_with_one_rejected_row))
        row_error_result=submit(page,"Import offerings","/v1/offerings/bulk",status=200).json()
        assert row_error_result["successful_count"]==1 and row_error_result["failed_count"]==1,row_error_result
        page.get_by_text("Bulk import results",exact=True).wait_for()
        page.get_by_text("Row 2: Private Endpoint Price Test",exact=True).wait_for()
        page.get_by_text("URL validation failed",exact=False).wait_for()
        assert any(error["index"]==1 and "URL validation failed" in error["error"] for error in row_error_result["errors"]),row_error_result
        shot(page,"22b-bulk-import-row-errors.png")
        page.get_by_role("button",name="Dismiss import results",exact=True).click()

        csv_path,json_path=Path("/tmp/nexora-catalog.csv"),Path("/tmp/nexora-catalog.json")
        with page.expect_download() as csv_dl:
            page.get_by_role("button",name="CSV",exact=True).click()
        csv_dl.value.save_as(str(csv_path))
        assert csv_path.exists() and csv_path.stat().st_size>0
        with page.expect_download() as json_dl:
            page.get_by_role("button",name="JSON",exact=True).click()
        json_dl.value.save_as(str(json_path))
        assert json_path.exists() and json_path.stat().st_size>0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones",exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones",exact=True).check()
        archived=submit(page,"Archive 1","/v1/offerings/bulk-archive",status=200).json()
        assert archived["archived_count"]==1
        archived_read=get_api(page,"/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")
        assert any(x["is_archived"] for x in archived_read["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING,exact=True).wait_for()
        shot(page,"23-catalog-import-export-archive.png")

        # Single-item archive is a distinct UI action from bulk archive.
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony LinkBuds S")
        linkbuds_row=page.locator("tbody tr").filter(has_text="Sony LinkBuds S").first
        linkbuds_row.wait_for()
        linkbuds_id=get_api(page,"/v1/offerings?q=Sony%20LinkBuds%20S&page=1&page_size=20")["items"][0]["id"]
        with expect(page,f"/v1/offerings/{linkbuds_id}","DELETE") as single_archive:
            linkbuds_row.get_by_title("Archive",exact=True).click()
        assert single_archive.value.status==204,single_archive.value.text()
        archived_linkbuds=get_api(page,f"/v1/offerings?q=Sony%20LinkBuds%20S&include_archived=true&page=1&page_size=20")
        assert any(row["id"]==linkbuds_id and row["is_archived"] for row in archived_linkbuds["items"]),archived_linkbuds
        page.get_by_placeholder("Search name, SKU, category…").fill("")
        page.get_by_text(OFFERING,exact=True).wait_for()
        shot(page,"23b-single-offering-archived.png")

        # A fixture catalogue gives the real UI/API a two-page list without relying
        # on any live retailer returning a stable catalogue size.
        catalog_models = [
          ("Bose QuietComfort Ultra Headphones","Bose","Audio > Headphones",429.99),
          ("Bose QuietComfort Headphones","Bose","Audio > Headphones",349.99),
          ("Bose QuietComfort Ultra Earbuds","Bose","Audio > Earbuds",299.99),
          ("Bose SoundLink Max Speaker","Bose","Audio > Speakers",399.99),
          ("Apple AirPods Pro (2nd generation) USB-C","Apple","Audio > Earbuds",249.99),
          ("Apple AirPods Max (USB-C)","Apple","Audio > Headphones",549.99),
          ("Beats Studio Pro","Beats","Audio > Headphones",349.99),
          ("Beats Solo 4","Beats","Audio > Headphones",199.99),
          ("Samsung Galaxy Buds3 Pro","Samsung","Audio > Earbuds",249.99),
          ("Samsung Galaxy Buds FE","Samsung","Audio > Earbuds",99.99),
          ("Samsung Galaxy Buds3","Samsung","Audio > Earbuds",179.99),
          ("Google Pixel Buds Pro 2","Google","Audio > Earbuds",229.99),
          ("Google Pixel Buds A-Series","Google","Audio > Earbuds",99.99),
          ("Sennheiser Momentum 4 Wireless","Sennheiser","Audio > Headphones",349.99),
          ("Sennheiser HD 660S2","Sennheiser","Audio > Headphones",599.99),
          ("Sennheiser Momentum True Wireless 4","Sennheiser","Audio > Earbuds",299.99),
          ("JBL Tour One M2","JBL","Audio > Headphones",299.99),
          ("JBL Live 770NC","JBL","Audio > Headphones",199.99),
          ("JBL Tour Pro 3","JBL","Audio > Earbuds",299.99),
          ("JBL Charge 5","JBL","Audio > Speakers",179.99),
          ("JBL Flip 6","JBL","Audio > Speakers",129.99),
          ("Anker Soundcore Space Q45","Anker","Audio > Headphones",149.99),
          ("Anker Soundcore Liberty 4 NC","Anker","Audio > Earbuds",99.99),
          ("Anker Soundcore Motion X600","Anker","Audio > Speakers",199.99),
          ("Logitech MX Master 3S","Logitech","Computing > Mice",99.99),
          ("Logitech MX Keys S","Logitech","Computing > Keyboards",109.99),
          ("Logitech G Pro X Superlight 2","Logitech","Gaming > Mice",159.99),
          ("Dell UltraSharp U2723QE","Dell","Computing > Monitors",629.99),
          ("Dell XPS 13","Dell","Computing > Laptops",1199.99),
          ("Dell P2723DE","Dell","Computing > Monitors",399.99),
          ("Lenovo ThinkPad X1 Carbon Gen 12","Lenovo","Computing > Laptops",1899.99),
          ("Lenovo ThinkPad T14 Gen 5","Lenovo","Computing > Laptops",1399.99),
          ("Lenovo Yoga 9i Gen 9","Lenovo","Computing > Laptops",1499.99),
          ("HP Spectre x360 14","HP","Computing > Laptops",1499.99),
          ("HP EliteBook 840 G11","HP","Computing > Laptops",1599.99),
          ("ASUS Zenbook 14 OLED","ASUS","Computing > Laptops",999.99),
          ("ASUS ROG Zephyrus G16","ASUS","Gaming > Laptops",1999.99),
          ("Microsoft Surface Laptop 7","Microsoft","Computing > Laptops",1299.99),
          ("Microsoft Surface Pro 11","Microsoft","Computing > Tablets",999.99),
          ("Acer Swift Go 14","Acer","Computing > Laptops",799.99),
          ("Acer Predator Helios Neo 16","Acer","Gaming > Laptops",1399.99),
          ("Xiaomi Redmi Note 13 Pro+","Xiaomi","Mobile > Phones",399.99),
          ("Xiaomi Pad 6","Xiaomi","Mobile > Tablets",349.99),
          ("OnePlus 12","OnePlus","Mobile > Phones",799.99),
          ("OnePlus Buds Pro 3","OnePlus","Audio > Earbuds",179.99),
          ("Dyson V15 Detect","Dyson","Home > Floor Care",749.99),
          ("Philips Airfryer 3000 Series","Philips","Home > Kitchen",149.99),
          ("Canon EOS R6 Mark II","Canon","Photography > Cameras",2499.99),
          ("Fujifilm X-T5","Fujifilm","Photography > Cameras",1699.99),
          ("Garmin Forerunner 965","Garmin","Wearables > Sports Watches",599.99),
          ("TP-Link Deco XE75 Mesh Wi-Fi System","TP-Link","Networking > Wi-Fi",299.99)
        ]
        pagination_payload = [
          {"name":name,"offering_type":"PRODUCT","sku":f"NXR-PAGE-{index:03d}","current_price":price,"currency":"USD","market":"US","category":category,"description":"Illustrative catalog record used to exercise Nexora pagination in an isolated review workspace.","attributes":{"brand":brand,"review_fixture":True}}
          for index,(name,brand,category,price) in enumerate(catalog_models,1)
        ]
        assert len(pagination_payload)==51,len(pagination_payload)
        page.get_by_role("button",name="Bulk import",exact=True).click()
        page.get_by_label("Offering JSON").fill(json.dumps(pagination_payload))
        pagination_import=submit(page,"Import offerings","/v1/offerings/bulk",status=200).json()
        assert pagination_import["successful_count"]==51 and pagination_import["failed_count"]==0,pagination_import
        page.get_by_role("button",name="Dismiss import results",exact=True).click()
        page.get_by_text("Page 1 of 2",exact=True).wait_for()
        assert page.locator("tbody tr").count()==50,page.locator("tbody tr").count()
        page.get_by_role("button",name="Next",exact=True).click()
        page.get_by_text("Page 2 of 2",exact=True).wait_for()
        assert page.locator("tbody tr").count()==4,page.locator("tbody tr").count()
        shot(page,"23c-catalog-pagination.png")
        page.get_by_role("button",name="Previous",exact=True).click()
        page.get_by_text("Page 1 of 2",exact=True).wait_for()

        # Type filtering uses the persisted plan record created by the earlier partial import.
        page.get_by_role("button",name="All types",exact=True).click()
        page.get_by_role("option",name="PLAN",exact=True).last.click()
        page.get_by_text("AWS EC2 Compute Optimized",exact=True).wait_for()
        assert page.locator("tbody tr").count()==1
        shot(page,"23d-catalog-type-filter.png")
        page.get_by_role("button",name="PLAN",exact=True).click()
        page.get_by_role("option",name="All types",exact=True).last.click()
        page.get_by_text(OFFERING,exact=True).wait_for()

        # A matched competitor URL creates a real price observation and snapshot.
        page.get_by_role("link",name="Matches",exact=True).click()
        page.locator(".content h2").filter(has_text="Offering matches").wait_for()
        page.get_by_role("button",name="Add match",exact=True).click()
        choose(page,"Offering",OFFERING)
        choose(page,"Source",SOURCE+" Structured")
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        choose(page,"Status","APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page,"24-offering-match-form.png")
        match=submit(page,"Create match","/v1/offering-matches").json()
        match_id=match["id"]
        page.get_by_text(HIGH_URL,exact=False).first.wait_for()
        shot(page,"25-offering-match-created.png")

        # Exercise the Matches screen's delete action with an alternative URL,
        # then confirm the row is absent after a fresh API read. This temporary
        # record is removed before the primary match changes to the same URL.
        page.get_by_role("button",name="Add match",exact=True).click()
        choose(page,"Offering",OFFERING)
        choose(page,"Source",SOURCE+" Structured")
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        choose(page,"Status","SUGGESTED")
        page.get_by_label("Confidence (0–1)").fill("0.50")
        temporary_match=submit(page,"Create match","/v1/offering-matches").json()
        temporary_match_id=temporary_match["id"]
        assert any(row["id"]==temporary_match_id for row in get_api(page,"/v1/offering-matches"))
        page.locator("tbody tr").filter(has_text=LOW_URL).wait_for()
        shot(page,"25b-temporary-match-created.png")
        temporary_row=page.locator("tbody tr").filter(has_text=LOW_URL).first
        with expect(page,f"/v1/offering-matches/{temporary_match_id}","DELETE") as delete_match_event:
            temporary_row.locator("button.danger-icon").click()
        assert delete_match_event.value.status==204,delete_match_event.value.text()
        assert all(row["id"]!=temporary_match_id for row in get_api(page,"/v1/offering-matches"))
        assert page.locator("tbody tr").filter(has_text=LOW_URL).count()==0
        shot(page,"25c-temporary-match-deleted.png")

        with expect(page,"/v1/collections/run","POST") as collect1:
            page.locator("tbody tr").filter(has_text=OFFERING).first.get_by_title("Run collection",exact=True).click()
        first_response=collect1.value
        assert first_response.status in (200,201),first_response.text()
        first=first_response.json()
        assert first["status"]=="COMPLETED" and first["observation"]["http_status_code"]==200,first
        assert first["observation"]["price"]=="399.99",first
        assert len(get_api(page,f"/v1/offerings/{offering_id}/history"))>=1
        shot(page,"26-first-price-observation.png")
        page.get_by_role("link",name="Dashboard",exact=True).click()
        page.get_by_role("heading",name="Know what changed before the market tells you.",exact=True).wait_for()
        page.get_by_text(OFFERING,exact=True).wait_for()
        shot(page,"27-dashboard-with-market-data.png")

        # Price-drop rule persists an alert, routes an email, and can be acknowledged.
        page.get_by_role("link",name="Alerts",exact=True).click()
        page.locator(".content h2").filter(has_text="Alerts").wait_for()
        page.get_by_role("button",name="New rule",exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page,"Alert type","PERCENTAGE_DROP")
        choose(page,"Offering scope",OFFERING)
        page.get_by_label("Threshold",exact=True).fill("10")
        page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page,"28-alert-rule-form.png")
        rule=submit(page,"Create rule","/v1/alerts/rules").json()
        rule_id=rule["id"]
        page.get_by_text("Sony retail price drop",exact=True).wait_for()
        shot(page,"29-alert-rule-created.png")
        rule_row=page.locator("div.list-row").filter(has_text="Sony retail price drop")
        rule_row.get_by_title("Pause rule",exact=True).click()
        page.get_by_text("PAUSED",exact=True).wait_for()
        page.locator("div.list-row").filter(has_text="Sony retail price drop").get_by_title("Activate rule",exact=True).click()
        page.get_by_text("ACTIVE",exact=True).wait_for()
        page.get_by_role("link",name="Matches",exact=True).click()
        page.locator("tbody tr").filter(has_text=OFFERING).first.get_by_title("Edit",exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW_URL)
        submit(page,"Save changes",f"/v1/offering-matches/{match_id}","PATCH",200)
        with expect(page,"/v1/collections/run","POST") as collect2:
            page.locator("tbody tr").filter(has_text=OFFERING).first.get_by_title("Run collection",exact=True).click()
        second_response=collect2.value
        assert second_response.status in (200,201),second_response.text()
        second=second_response.json()
        assert second["observation"]["price"]=="349.99",second
        assert second["snapshot"]["previous_price"]=="399.99",second
        assert second["snapshot"]["percentage_difference"].startswith("-12.50%"),second
        mail_start=len(emails())
        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_text("Competitor price dropped",exact=True).wait_for(timeout=15000)
        logs=get_api(page,"/v1/alerts/logs")
        alert=next((x for x in logs if x["title"]=="Competitor price dropped"),None)
        assert alert and not alert["is_read"],logs
        page.get_by_title("Mark as read",exact=True).click()
        page.get_by_title("Read",exact=True).wait_for()
        assert any(x["id"]==alert["id"] and x["is_read"] for x in get_api(page,"/v1/alerts/logs"))
        shot(page,"30-alert-generated-and-read.png")
        delivery=dispatch_email_worker()
        assert int(delivery.get("sent",0))>=1,delivery
        mail_body=wait_email("Nexora Competitive Alert: Competitor price dropped",EMAIL,mail_start)
        assert "Nexora detected a competitive intelligence event" in mail_body
        assert "SENT" in get_outbox_statuses_sync(client_id)

        # Repeated real source failures open the circuit and generate a persistent
        # operational alert. A final request is rejected before another job starts.
        page.get_by_role("button",name="New rule",exact=True).click()
        page.get_by_label("Rule name").fill("Source outage circuit breaker")
        choose(page,"Alert type","CIRCUIT_BREAKER_TRIPPED")
        page.get_by_label("Deliver through email channel").uncheck()
        shot(page,"29b-circuit-breaker-rule-form.png")
        circuit_rule=submit(page,"Create rule","/v1/alerts/rules").json()
        circuit_rule_id=circuit_rule["id"]
        assert circuit_rule["alert_type"]=="CIRCUIT_BREAKER_TRIPPED",circuit_rule

        page.get_by_role("link",name="Matches",exact=True).click()
        match_row=page.locator("tbody tr").filter(has_text=OFFERING).first
        match_row.get_by_title("Edit",exact=True).click()
        missing_url="https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/retail-price-missing.html"
        page.get_by_label("Competitor target URL").fill(missing_url)
        edited_match=submit(page,"Save changes",f"/v1/offering-matches/{match_id}","PATCH",200).json()
        assert edited_match["target_url"]==missing_url,edited_match

        with expect(page,"/v1/collections/run","POST") as first_failed_collection:
            page.locator("tbody tr").filter(has_text=OFFERING).first.get_by_title("Run collection",exact=True).click()
        first_failure=first_failed_collection.value
        assert first_failure.status==502,first_failure.text()

        for attempt in range(2,6):
            failure=page.request.post(
                API+f"/v1/collections/run?offering_match_id={match_id}",
                headers={"Authorization":"Bearer "+token(page)},
            )
            assert failure.status==502,f"Failed collection attempt {attempt} returned {failure.status}: {failure.text()}"

        source_state=get_api(page,f"/v1/sources/{source_id}")
        assert source_state["circuit_state"]=="OPEN",source_state
        assert source_state["failure_count"]>=5,source_state

        circuit_block=page.request.post(
            API+f"/v1/collections/run?offering_match_id={match_id}",
            headers={"Authorization":"Bearer "+token(page)},
        )
        assert circuit_block.status==503,circuit_block.text()
        assert "circuit breaker is open" in circuit_block.text().lower(),circuit_block.text()

        page.get_by_role("link",name="Sources",exact=True).click()
        page.locator(".content h2").filter(has_text="Sources").wait_for()
        page.get_by_text("OPEN",exact=True).wait_for()
        page.get_by_text("FAILING",exact=True).wait_for()
        shot(page,"29c-source-circuit-open.png")

        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_text("Competitor source circuit breaker tripped",exact=True).wait_for(timeout=15000)
        circuit_logs=get_api(page,"/v1/alerts/logs")
        assert any(log["title"]=="Competitor source circuit breaker tripped" for log in circuit_logs),circuit_logs
        shot(page,"29d-circuit-breaker-alert.png")

        # Collection execution history and saved job details.
        page.get_by_role("link",name="Jobs",exact=True).click()
        page.locator(".content h2").filter(has_text="Jobs").wait_for()
        page.locator("tbody tr").filter(has_text="ON DEMAND REFRESH").first.wait_for()
        shot(page,"31-collection-job-history.png")
        page.get_by_role("button",name="Details",exact=True).first.click()
        page.get_by_text("Job ID",exact=True).wait_for()
        shot(page,"32-collection-job-details.png")
        page.get_by_role("button",name="Close dialog").click()

        # Discovery validates public targets, persists structured catalog data and updates in place.
        page.get_by_role("link",name="Discovery",exact=True).click()
        page.locator(".content h2").filter(has_text="Website discovery").wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        blocked=submit(page,"Run discovery","/v1/discovery/run",status=422)
        assert any(word in blocked.text().lower() for word in ("invalid","prohibited","private")),blocked.text()
        shot(page,"33-private-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY_URL)
        choose(page,"Default type","PRODUCT")
        discovery=submit(page,"Run discovery","/v1/discovery/run",status=200).json()
        assert discovery["status"]=="COMPLETED" and discovery["created_offerings_count"]==1,discovery
        assert discovery["items"][0]["name"]==DISCOVERED,discovery
        page.get_by_text(DISCOVERED,exact=True).wait_for(timeout=15000)
        shot(page,"34-discovery-product-created.png")
        persisted=get_api(page,f"/v1/discovery/jobs/{discovery['job_id']}")
        assert persisted["items"][0]["sku"]=="WF1000XM5-BLK"
        updated_discovery=submit(page,"Run discovery","/v1/discovery/run",status=200).json()
        assert updated_discovery["created_offerings_count"]==0 and updated_discovery["updated_offerings_count"]==1,updated_discovery
        catalog=get_api(page,"/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(x["sku"]=="WF1000XM5-BLK" for x in catalog["items"])==1
        shot(page,"35-discovery-update-history.png")

        # Workspace profile, password change, recovery email and one-time reset link.
        page.locator("button.profile-trigger").click()
        page.get_by_text("Account settings",exact=True).click()
        page.locator(".content h2").filter(has_text="Settings").wait_for()
        page.get_by_label("Company name").wait_for()
        shot(page,"36-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        profile=submit(page,"Save profile","/v1/auth/profile","PATCH",200).json()
        assert profile["company_name"]=="Northstar Analytics"
        assert get_api(page,"/v1/auth/profile")["company_name"]=="Northstar Analytics"
        shot(page,"37-workspace-profile-saved.png")
        page.locator('input[autocomplete="current-password"]').fill(PASSWORD)
        page.locator('input[autocomplete="new-password"]').fill(CHANGED_PASSWORD)
        submit(page,"Update password","/v1/auth/change-password",status=200)
        old_login=page.request.post(API+"/v1/auth/login",data=json.dumps({"email":EMAIL,"password":PASSWORD}),headers={"Content-Type":"application/json"})
        assert old_login.status==401,old_login.text()
        new_login=page.request.post(API+"/v1/auth/login",data=json.dumps({"email":EMAIL,"password":CHANGED_PASSWORD}),headers={"Content-Type":"application/json"})
        assert new_login.status==200,new_login.text()
        shot(page,"38-password-changed.png")
        logout(page)
        page.get_by_role("link",name="Forgot password?",exact=True).click()
        page.get_by_role("heading",name="Reset access without losing your workspace.",exact=True).wait_for(timeout=10000)
        reset_email=page.locator('form input[type="email"]').last
        reset_email.wait_for(state="visible",timeout=10000)
        reset_email.fill("")
        reset_email.type(EMAIL,delay=8)
        reset_email.press("Tab")
        assert reset_email.input_value()==EMAIL, "Password recovery email was not entered into the recovery form."
        print("PASSWORD_RESET_EMAIL_INPUT "+reset_email.input_value(),flush=True)
        reset_start=len(emails())
        submit(page,"Send reset link","/v1/auth/forgot-password",status=200)
        page.get_by_role("heading",name="Check your inbox",exact=True).wait_for()
        shot(page,"39-password-reset-requested.png")
        reset_url=email_link(wait_email("Reset your Nexora password",EMAIL,reset_start),"reset-password")
        page.goto(reset_url,wait_until="networkidle")
        page.locator('input[autocomplete="new-password"]').wait_for()
        shot(page,"40-password-reset-form.png")
        page.locator('input[autocomplete="new-password"]').fill(RESET_PASSWORD)
        submit(page,"Update password","/v1/auth/reset-password",status=200)
        page.get_by_role("heading",name="Password updated",exact=True).wait_for()
        shot(page,"41-password-reset-complete.png")
        page.get_by_role("link",name="Continue to sign in",exact=True).click()
        login(page,EMAIL,RESET_PASSWORD)
        shot(page,"42-dashboard-after-reset.png")

        # Role views enforce read-only access on both the interface and server.
        suffix=uuid.uuid4().hex[:8]
        viewer_email=create_role_user_sync(client_id,RoleEnum.VIEWER,suffix)
        analyst_email=create_role_user_sync(client_id,RoleEnum.ANALYST,suffix)
        logout(page)
        login(page,analyst_email,ROLE_PASSWORD)
        page.get_by_role("link",name="Competitors",exact=True).click()
        assert page.get_by_role("button",name="Add competitor",exact=True).count()==0
        analyst_token=token(page)
        analyst_write=page.request.post(API+"/v1/competitors",headers={"Authorization":"Bearer "+analyst_token},data=json.dumps({"name":"Not Allowed","domain":"blocked-"+suffix+".example"}))
        assert analyst_write.status==403,analyst_write.text()
        page.get_by_role("link",name="Discovery",exact=True).click()
        assert page.get_by_role("button",name="Run discovery",exact=True).is_visible()
        shot(page,"43-analyst-workspace.png")
        logout(page)
        login(page,viewer_email,ROLE_PASSWORD)
        page.get_by_role("link",name="Offerings",exact=True).click()
        assert page.get_by_role("button",name="Add offering",exact=True).count()==0
        assert page.get_by_role("button",name="Bulk import",exact=True).count()==0
        assert page.get_by_role("button",name="Dynamic fields",exact=True).count()==0
        viewer_token=token(page)
        viewer_read=page.request.get(API+"/v1/offerings",headers={"Authorization":"Bearer "+viewer_token})
        assert viewer_read.status==200,viewer_read.text()
        viewer_write=page.request.post(API+"/v1/competitors",headers={"Authorization":"Bearer "+viewer_token},data=json.dumps({"name":"Viewer Write Attempt","domain":"viewer-"+suffix+".example"}))
        assert viewer_write.status==403,viewer_write.text()
        shot(page,"44-viewer-read-only-workspace.png")

        # Super-admin controls are available and the server authorizes a real write.
        super_admin_email=create_role_user_sync(client_id,RoleEnum.SUPER_ADMIN,suffix)
        logout(page)
        login(page,super_admin_email,ROLE_PASSWORD)
        page.get_by_role("link",name="Competitors",exact=True).click()
        page.get_by_role("button",name="Add competitor",exact=True).wait_for()
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill("Super Admin Review Competitor")
        page.get_by_label("Domain").fill("super-admin-"+suffix+".example")
        super_admin_create=submit(page,"Save competitor","/v1/competitors").json()
        assert super_admin_create["name"]=="Super Admin Review Competitor",super_admin_create
        assert any(row["id"]==super_admin_create["id"] for row in get_api(page,"/v1/competitors"))
        shot(page,"45-super-admin-workspace.png")

        # Exercise source deletion on a separate temporary source, so the source
        # used for collection evidence remains available for direct DB checks.
        page.get_by_role("link",name="Sources",exact=True).click()
        page.get_by_role("button",name="Add source",exact=True).click()
        choose(page,"Competitor","Super Admin Review Competitor")
        temp_source_name="Disposable source for deletion"
        page.get_by_label("Source name").fill(temp_source_name)
        page.get_by_label("Base URL").fill(HIGH_URL)
        choose(page,"Source type","OFFICIAL_STORE")
        choose(page,"Collection method","HTTP_FAST")
        temporary_source=submit(page,"Create source","/v1/sources").json()
        temp_source_id=temporary_source["id"]
        assert any(row["id"]==temp_source_id for row in get_api(page,"/v1/sources"))
        page.get_by_text(temp_source_name,exact=True).wait_for()
        shot(page,"45b-super-admin-source-created.png")
        temp_source_row=page.locator("tbody tr").filter(has_text=temp_source_name).first
        with expect(page,f"/v1/sources/{temp_source_id}","DELETE") as delete_source_event:
            temp_source_row.locator("button.danger-icon").click()
        assert delete_source_event.value.status==204,delete_source_event.value.text()
        assert all(row["id"]!=temp_source_id for row in get_api(page,"/v1/sources"))
        shot(page,"45c-super-admin-source-deleted.png")

        # Delete the temporary competitor after its source has been removed.
        page.get_by_role("link",name="Competitors",exact=True).click()
        temp_competitor_row=page.locator("tbody tr").filter(has_text="Super Admin Review Competitor").first
        with expect(page,f"/v1/competitors/{super_admin_create['id']}","DELETE") as delete_competitor_event:
            temp_competitor_row.locator("button.danger-icon").click()
        assert delete_competitor_event.value.status==204,delete_competitor_event.value.text()
        competitor_lookup=page.request.get(
            API+f"/v1/competitors/{super_admin_create['id']}",
            headers={"Authorization":"Bearer "+token(page)})
        assert competitor_lookup.status==404,competitor_lookup.text()
        shot(page,"45d-super-admin-competitor-deleted.png")

        # Alert rules also support deletion; isolate this temporary rule from the
        # live price-drop and circuit-breaker rules used earlier in the journey.
        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_role("button",name="New rule",exact=True).click()
        temp_rule_name="Disposable alert rule for deletion"
        page.get_by_label("Rule name").fill(temp_rule_name)
        choose(page,"Alert type","PRICE_CHANGE")
        choose(page,"Offering scope","All offerings")
        page.get_by_label("Threshold",exact=True).fill("1")
        page.get_by_label("Cooldown (minutes)").fill("60")
        temporary_rule=submit(page,"Create rule","/v1/alerts/rules").json()
        temp_rule_id=temporary_rule["id"]
        assert any(row["id"]==temp_rule_id for row in get_api(page,"/v1/alerts/rules"))
        temp_rule_row=page.locator("div.list-row").filter(has_text=temp_rule_name).last
        with expect(page,f"/v1/alerts/rules/{temp_rule_id}","DELETE") as delete_rule_event:
            temp_rule_row.get_by_title("Delete rule",exact=True).click()
        assert delete_rule_event.value.status==204,delete_rule_event.value.text()
        assert all(row["id"]!=temp_rule_id for row in get_api(page,"/v1/alerts/rules"))
        shot(page,"45e-super-admin-alert-rule-deleted.png")

        # Create a second tenant through the public registration and real mail-verification
        # flow; prove its records and permissions are separate from the original tenant.
        isolated_suffix=uuid.uuid4().hex[:8]
        isolated_email=f"isolated-owner-{isolated_suffix}@northstar.example.com"
        isolated_org=f"Contoso Audio Intelligence {isolated_suffix}"
        logout(page)
        page.goto(FRONTEND+"/register",wait_until="networkidle")
        page.get_by_label("Organization name").fill(isolated_org)
        page.get_by_label("Your full name").fill("Jordan Lee")
        page.get_by_label("Work email").fill(isolated_email)
        page.locator('input[autocomplete="new-password"]').fill(PASSWORD)
        isolated_mail_start=len(emails())
        isolated_registration=submit(page,"Create workspace","/v1/auth/register",status=201).json()
        assert isolated_registration["email_verification_required"] is True,isolated_registration
        isolated_verify_url=email_link(wait_email("Verify your Nexora account",isolated_email,isolated_mail_start),"verify-email")
        page.goto(isolated_verify_url,wait_until="networkidle")
        isolated_verified=submit(page,"Verify email","/v1/auth/verify-email","GET",200)
        assert isolated_verified.json().get("message"),isolated_verified.text()
        page.get_by_role("link",name="Continue to sign in",exact=True).click()
        login(page,isolated_email,PASSWORD)
        isolated_identity=get_api(page,"/v1/auth/me")
        isolated_client_id=uuid.UUID(isolated_identity["client_id"])
        assert isolated_client_id != client_id,"Separate organizations must receive separate tenant IDs."
        isolated_competitors=get_api(page,"/v1/competitors")
        isolated_offerings=get_api(page,"/v1/offerings?page=1&page_size=50")
        isolated_alerts=get_api(page,"/v1/alerts/rules")
        isolated_jobs=get_api(page,"/v1/jobs")
        assert isolated_competitors==[],isolated_competitors
        assert isolated_offerings["total_count"]==0,isolated_offerings
        assert isolated_alerts==[],isolated_alerts
        assert isolated_jobs==[],isolated_jobs
        foreign_competitor=page.request.get(API+f"/v1/competitors/{competitor_id}",headers={"Authorization":"Bearer "+token(page)})
        foreign_offering=page.request.get(API+f"/v1/offerings/{offering_id}",headers={"Authorization":"Bearer "+token(page)})
        assert foreign_competitor.status==404,foreign_competitor.text()
        assert foreign_offering.status==404,foreign_offering.text()
        page.get_by_role("heading",name="Know what changed before the market tells you.",exact=True).wait_for()
        shot(page,"46-isolated-tenant-dashboard.png")

        page.get_by_role("link",name="Competitors",exact=True).click()
        page.get_by_text("No competitors configured",exact=True).wait_for()
        assert get_api(page,"/v1/competitors")==[]
        shot(page,"47-isolated-tenant-competitors.png")

        page.get_by_role("link",name="Offerings",exact=True).click()
        page.get_by_text("Your catalog is empty",exact=True).wait_for()
        assert get_api(page,"/v1/offerings?page=1&page_size=50")["total_count"]==0
        shot(page,"48-isolated-tenant-catalog.png")

        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_text("No rules configured",exact=True).wait_for()
        assert get_api(page,"/v1/alerts/rules")==[]
        shot(page,"49-isolated-tenant-alerts.png")

        # Return to the first tenant before mobile checks.
        logout(page)
        login(page,EMAIL,RESET_PASSWORD)

        # Check the authenticated workspace and the logged-out public entry separately.
        mobile_dashboard=context.new_page()
        mobile_dashboard.set_viewport_size({"width":390,"height":844})
        mobile_dashboard.goto(FRONTEND+"/",wait_until="networkidle")
        dashboard_dimensions=mobile_dashboard.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        dashboard_overflow=mobile_dashboard.evaluate("""() => Array.from(document.querySelectorAll("body *")).map(element => {
          const rect=element.getBoundingClientRect(); const style=getComputedStyle(element);
          return {tag:element.tagName.toLowerCase(),id:element.id||"",
            className:typeof element.className==="string"?element.className.slice(0,100):"",
            text:(element.innerText||"").replace(/\\s+/g," ").slice(0,60),
            left:Math.round(rect.left),right:Math.round(rect.right),width:Math.round(rect.width),
            position:style.position,overflowX:style.overflowX};
        }).filter(item=>item.width>0&&(item.right>innerWidth+1||item.left< -1))
          .sort((a,b)=>b.right-a.right).slice(0,30)""")
        print("MOBILE_DASHBOARD_DIAGNOSTICS "+json.dumps({"dimensions":dashboard_dimensions,"elements":dashboard_overflow}),flush=True)
        assert dashboard_dimensions["content"]<=dashboard_dimensions["viewport"],{"dimensions":dashboard_dimensions,"overflow":dashboard_overflow}
        shot(mobile_dashboard,"50-mobile-dashboard.png")

        mobile_context=browser.new_context(viewport={"width":390,"height":844})
        mobile=mobile_context.new_page()
        mobile.goto(FRONTEND+"/welcome",wait_until="networkidle")
        home_dimensions=mobile.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        home_overflow=mobile.evaluate("""() => Array.from(document.querySelectorAll("body *")).map(element => {
          const rect=element.getBoundingClientRect(); const style=getComputedStyle(element);
          return {tag:element.tagName.toLowerCase(),id:element.id||"",
            className:typeof element.className==="string"?element.className.slice(0,100):"",
            text:(element.innerText||"").replace(/\\s+/g," ").slice(0,60),
            left:Math.round(rect.left),right:Math.round(rect.right),width:Math.round(rect.width),
            position:style.position,overflowX:style.overflowX};
        }).filter(item=>item.width>0&&(item.right>innerWidth+1||item.left< -1))
          .sort((a,b)=>b.right-a.right).slice(0,30)""")
        print("MOBILE_HOME_DIAGNOSTICS "+json.dumps({"dimensions":home_dimensions,"elements":home_overflow}),flush=True)
        assert home_dimensions["content"]<=home_dimensions["viewport"],{"dimensions":home_dimensions,"overflow":home_overflow}
        shot(mobile,"51-mobile-homepage.png")
        mobile_context.close()

        database_evidence = verify_postgres_sync(
            client_id, competitor_id, source_id, offering_id, match_id, rule_id,
            circuit_rule_id, alert["id"], isolated_client_id, super_admin_email,
        )
        (SHOTS / "postgres-evidence.json").write_text(
            json.dumps(database_evidence, indent=2), encoding="utf-8"
        )
        print("POSTGRESQL_READBACK " + json.dumps(database_evidence), flush=True)
        assert not BROWSER_ERRORS,"Frontend JavaScript errors: "+" | ".join(BROWSER_ERRORS)
        expected_names=[f"{i:02d}-"+name for i,name in [
            (0,"public-homepage.png"),(1,"registration-form.png"),(2,"verification-email-sent.png"),(3,"email-verification-page.png"),
            (4,"email-verified.png"),(5,"login.png"),(6,"dashboard-starting-workspace.png"),(7,"competitors.png"),
            (8,"competitor-created.png"),(9,"duplicate-domain-protection.png"),(10,"source-form.png"),(11,"source-created.png"),
            (12,"source-settings.png"),(13,"source-settings-saved.png"),(14,"offering-form.png"),(15,"catalog-offering-created.png"),
            (16,"offering-details.png"),(17,"monitoring-controls.png"),(18,"dynamic-fields.png"),(19,"dynamic-field-form.png"),
            (20,"dynamic-field-created.png"),(21,"bulk-import-form.png"),(22,"bulk-import-results.png"),(23,"catalog-import-export-archive.png"),
            (24,"offering-match-form.png"),(25,"offering-match-created.png"),(26,"first-price-observation.png"),(27,"dashboard-with-market-data.png"),
            (28,"alert-rule-form.png"),(29,"alert-rule-created.png"),(30,"alert-generated-and-read.png"),(31,"collection-job-history.png"),
            (32,"collection-job-details.png"),(33,"private-target-blocked.png"),(34,"discovery-product-created.png"),(35,"discovery-update-history.png"),
            (36,"workspace-settings.png"),(37,"workspace-profile-saved.png"),(38,"password-changed.png"),(39,"password-reset-requested.png"),
            (40,"password-reset-form.png"),(41,"password-reset-complete.png"),(42,"dashboard-after-reset.png"),(43,"analyst-workspace.png"),
            (44,"viewer-read-only-workspace.png"),(45,"super-admin-workspace.png"),(46,"isolated-tenant-dashboard.png"),
            (47,"isolated-tenant-competitors.png"),(48,"isolated-tenant-catalog.png"),(49,"isolated-tenant-alerts.png"),
            (50,"mobile-dashboard.png"),(51,"mobile-homepage.png"),
        ]]
        expected_names += ["02b-verification-email-resent.png","13b-source-js-rendering-enabled.png","22b-bulk-import-row-errors.png","23b-single-offering-archived.png","23c-catalog-pagination.png","23d-catalog-type-filter.png","25b-temporary-match-created.png","25c-temporary-match-deleted.png","29b-circuit-breaker-rule-form.png","29c-source-circuit-open.png","29d-circuit-breaker-alert.png","45b-super-admin-source-created.png","45c-super-admin-source-deleted.png","45d-super-admin-competitor-deleted.png","45e-super-admin-alert-rule-deleted.png"]
        missing=[name for name in expected_names if not (SHOTS/name).is_file()]
        assert not missing,"Missing visitor journey screens: "+str(missing)
        print(f"VISITOR JOURNEY COMPLETE: {len(expected_names)} screenshots captured; UI, API, PostgreSQL and mail flows completed.",flush=True)
        context.close()
        browser.close()

if __name__=="__main__":
    main()

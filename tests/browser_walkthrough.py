"""Nexora's complete browser journey: React UI -> FastAPI -> PostgreSQL and email."""
from __future__ import annotations
import asyncio, json, os, re, subprocess, sys, time, uuid
from email import message_from_string
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import Page, sync_playwright
from sqlalchemy import select
from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel

FRONTEND=os.getenv("NEXORA_FRONTEND_URL","http://127.0.0.1:4173").rstrip("/")
API=os.getenv("NEXORA_API_URL","http://127.0.0.1:8000").rstrip("/")
MAILBOX=Path(os.getenv("NEXORA_MAILBOX","/tmp/nexora-mailbox.jsonl"))
SHOTS=Path("docs/walkthrough/screenshots")
EMAIL="market-analyst@northstar.example.com"
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
ASYNC_LOOP=asyncio.new_event_loop()

def run_async(coro):
    return ASYNC_LOOP.run_until_complete(coro)

def shot(page: Page, name: str):
    SHOTS.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(SHOTS/name),full_page=True,animations="disabled",caret="hide")
    print("SCREENSHOT "+name,flush=True)

def choose(page: Page, label: str, option: str):
    field=page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option",name=option,exact=True).last.click()

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

def dispatch_email_worker():
    code="import json; from backend.workers.tasks import dispatch_pending_notifications; print(json.dumps(dispatch_pending_notifications.run()))"
    result=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,check=True,env=os.environ.copy())
    lines=[line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert lines,result.stdout+" "+result.stderr
    return json.loads(lines[-1])

def main():
    SHOTS.mkdir(parents=True,exist_ok=True)
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
        verify_url=email_link(wait_email("Verify your Nexora account",EMAIL,mail_start),"verify-email")
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
        page.get_by_role("heading",name="Competitors",exact=True).wait_for()
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
        page.get_by_role("heading",name="Sources",exact=True).wait_for()
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
        page.get_by_title("Edit source",exact=True).click()
        page.get_by_label("Source name").fill(SOURCE+" Structured")
        source_edit=submit(page,"Save changes",f"/v1/sources/{source_id}","PATCH",200).json()
        assert source_edit["name"]==SOURCE+" Structured"
        source_rows=get_api(page,"/v1/sources")
        assert any(s["id"]==source_id and s["configuration"]["max_retries"]==1 for s in source_rows)
        shot(page,"13-source-settings-saved.png")

        # Catalog CRUD, dynamic fields, bulk import/export and archive.
        page.get_by_role("link",name="Offerings",exact=True).click()
        page.get_by_role("heading",name="Offerings",exact=True).wait_for()
        page.get_by_role("button",name="Add offering",exact=True).click()
        page.get_by_label("Name").fill(OFFERING)
        choose(page,"Offering type","PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK")
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
        page.get_by_role("button",name="Close dialog").click()
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

        # A matched competitor URL creates a real price observation and snapshot.
        page.get_by_role("link",name="Matches",exact=True).click()
        page.get_by_role("heading",name="Offering matches",exact=True).wait_for()
        page.get_by_role("button",name="Add match",exact=True).click()
        choose(page,"Offering",OFFERING)
        choose(page,"Source",SOURCE+" Structured")
        page.get_by_label("Competitor target URL").fill(HIGH_URL)
        choose(page,"Status","APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98")
        shot(page,"24-offering-match-form.png")
        match=submit(page,"Create match","/v1/offering-matches").json()
        match_id=match["id"]
        page.get_by_text(HIGH_URL,exact=False).wait_for()
        shot(page,"25-offering-match-created.png")
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
        page.get_by_role("heading",name="Alerts",exact=True).wait_for()
        page.get_by_role("button",name="New rule",exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page,"Alert type","PERCENTAGE_DROP")
        choose(page,"Offering scope",OFFERING)
        page.get_by_label("Threshold").fill("10")
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
        assert "SENT" in run_async(get_outbox_statuses(client_id))

        # Collection execution history and saved job details.
        page.get_by_role("link",name="Jobs",exact=True).click()
        page.get_by_role("heading",name="Jobs",exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH",exact=False).wait_for()
        shot(page,"31-collection-job-history.png")
        page.get_by_role("button",name="Details",exact=True).first.click()
        page.get_by_text("Job ID",exact=True).wait_for()
        shot(page,"32-collection-job-details.png")
        page.get_by_role("button",name="Close dialog").click()

        # Discovery validates public targets, persists structured catalog data and updates in place.
        page.get_by_role("link",name="Discovery",exact=True).click()
        page.get_by_role("heading",name="Website discovery",exact=True).wait_for()
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
        page.get_by_role("link",name="Settings",exact=True).click()
        page.get_by_role("heading",name="Settings",exact=True).wait_for()
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
        page.get_by_label("Current password").fill(PASSWORD)
        page.locator('input[autocomplete="new-password"]').fill(CHANGED_PASSWORD)
        submit(page,"Update password","/v1/auth/change-password",status=200)
        old_login=page.request.post(API+"/v1/auth/login",data=json.dumps({"email":EMAIL,"password":PASSWORD}),headers={"Content-Type":"application/json"})
        assert old_login.status==401,old_login.text()
        new_login=page.request.post(API+"/v1/auth/login",data=json.dumps({"email":EMAIL,"password":CHANGED_PASSWORD}),headers={"Content-Type":"application/json"})
        assert new_login.status==200,new_login.text()
        shot(page,"38-password-changed.png")
        logout(page)
        page.get_by_role("link",name="Forgot password?",exact=True).click()
        page.get_by_label("Work email").fill(EMAIL)
        reset_start=len(emails())
        submit(page,"Send reset link","/v1/auth/forgot-password",status=200)
        page.get_by_role("heading",name="Check your inbox",exact=True).wait_for()
        shot(page,"39-password-reset-requested.png")
        reset_url=email_link(wait_email("Reset your Nexora password",EMAIL,reset_start),"reset-password")
        page.goto(reset_url,wait_until="networkidle")
        page.get_by_label("New password").wait_for()
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
        viewer_email=run_async(make_role_user(client_id,RoleEnum.VIEWER,suffix))
        analyst_email=run_async(make_role_user(client_id,RoleEnum.ANALYST,suffix))
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

        mobile=context.new_page()
        mobile.set_viewport_size({"width":390,"height":844})
        mobile.goto(FRONTEND+"/welcome",wait_until="networkidle")
        dimensions=mobile.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        assert dimensions["content"]<=dimensions["viewport"],dimensions
        shot(mobile,"45-mobile-homepage.png")
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
            (44,"viewer-read-only-workspace.png"),(45,"mobile-homepage.png")
        ]]
        missing=[name for name in expected_names if not (SHOTS/name).is_file()]
        assert not missing,"Missing visitor journey screens: "+str(missing)
        print(f"VISITOR JOURNEY COMPLETE: {len(expected_names)} screenshots captured; UI, API, PostgreSQL and mail flows completed.",flush=True)
        context.close()
        browser.close()
    run_async(AsyncSessionLocal.kw["bind"].dispose()) if False else None
    ASYNC_LOOP.close()

if __name__=="__main__":
    main()

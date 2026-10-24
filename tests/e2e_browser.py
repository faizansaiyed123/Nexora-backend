"""Nexora browser walkthrough against a running frontend, FastAPI and PostgreSQL."""
from __future__ import annotations
import asyncio, json, os, re, time, uuid
from email import message_from_string
from pathlib import Path
from playwright.sync_api import Page, sync_playwright
from sqlalchemy import select
from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications

WEB=os.getenv("NEXORA_FRONTEND_URL","http://127.0.0.1:4173").rstrip("/")
API=os.getenv("NEXORA_API_URL","http://127.0.0.1:8000").rstrip("/")
MAIL=Path(os.getenv("NEXORA_MAILBOX","/tmp/nexora-mailbox.jsonl"))
SHOTS=Path(os.getenv("NEXORA_SCREENSHOT_DIR","docs/walkthrough/screenshots"))
EMAIL="market-analyst@northstar.example.com"
PASSWORD="Nexora-Strong!2026"
CHANGED="Nexora-Updated!2026"
RESET="Nexora-Final!2026"
ROLE_PASSWORD="Nexora-Role!2026"
PRODUCT="Sony WH-1000XM5 Wireless Headphones"
DISCOVERED="Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
HIGH="https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures/retail-price-high.html"
LOW="https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures/retail-price-low.html"
DISCOVERY="https://raw.githubusercontent.com/faizansaiyed123/Nexora-backend/main/tests/fixtures/retail-discovery-product.html"
JS_ERRORS=[]

def shot(page: Page, name: str):
    SHOTS.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(SHOTS/name),full_page=True,animations="disabled",caret="hide")
    print(f"SCREENSHOT: {name}",flush=True)

def choose(page: Page,label: str,option: str):
    field=page.locator("label.field").filter(has_text=label).last
    field.locator("button.select-trigger").click()
    page.get_by_role("option",name=re.compile(re.escape(option),re.I)).last.click()

def token(page: Page)->str:
    value=page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value,"The UI login should store an access token"
    return str(value)

def get(page: Page,path: str,status: int=200):
    response=page.request.get(API+path,headers={"Authorization":f"Bearer {token(page)}"})
    assert response.status==status,f"GET {path}: {response.status}: {response.text()}"
    return response.json()

def emails():
    if not MAIL.exists(): return []
    out=[]
    for line in MAIL.read_text(encoding="utf-8").splitlines():
        try:
            item=json.loads(line)
            if item.get("data"): out.append(item)
        except (ValueError,AttributeError): pass
    return out

def parsed_mail(item):
    msg=message_from_string(item["data"])
    payload=msg.get_payload(decode=True)
    body=payload.decode(msg.get_content_charset() or "utf-8","replace") if payload else str(msg.get_payload())
    return str(msg.get("Subject","")),body,[str(x).lower() for x in item.get("rcpt_tos",[])]

def wait_mail(subject: str,recipient: str,offset: int=0,timeout: int=40):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        for item in emails()[offset:]:
            title,body,recipients=parsed_mail(item)
            if subject.casefold() in title.casefold() and recipient.lower() in recipients: return body
        time.sleep(.25)
    raise AssertionError(f"No email matching {subject!r} for {recipient!r}")

def mail_link(body: str,route: str):
    match=re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+",body)
    assert match,f"Missing {route} link in delivered message"
    return match.group(0).rstrip(".,);")

def login(page: Page,email: str,password: str):
    page.get_by_label("Work email").fill(email)
    page.get_by_label("Password").fill(password)
    with page.expect_response(lambda r:"/v1/auth/login" in r.url and r.request.method=="POST") as event:
        page.get_by_role("button",name=re.compile("Sign in")).click()
    assert event.value.status==200,event.value.text()
    page.get_by_role("heading",name="Dashboard",exact=True).wait_for(timeout=20000)

def logout(page: Page):
    page.locator("button.profile-trigger").click()
    page.get_by_role("menuitem",name="Sign out",exact=True).click()
    page.get_by_label("Work email").wait_for(timeout=15000)

async def create_role(client_id: uuid.UUID,role: RoleEnum,suffix: str):
    email=f"{role.value.lower()}-{suffix}@northstar.example.com"
    async with AsyncSessionLocal() as db:
        db.add(UserModel(client_id=client_id,email=email,full_name=f"Northstar {role.value.title()}",
            hashed_password=get_password_hash(ROLE_PASSWORD),role=role,is_active=True,email_verified=True))
        await db.commit()
    return email

async def has_sent_outbox(client_id: uuid.UUID):
    async with AsyncSessionLocal() as db:
        rows=await db.execute(select(NotificationOutboxModel.status).where(
            NotificationOutboxModel.client_id==client_id,NotificationOutboxModel.channel=="email"))
        return "SENT" in rows.scalars().all()

def main():
    SHOTS.mkdir(parents=True,exist_ok=True); MAIL.parent.mkdir(parents=True,exist_ok=True); MAIL.write_text("")
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport={"width":1440,"height":1000},accept_downloads=True)
        page=context.new_page(); page.set_default_timeout(12000)
        page.on("pageerror",lambda err:JS_ERRORS.append(str(err)))
        page.on("dialog",lambda dialog:dialog.accept())

        # Registration, real verification email, and sign-in.
        page.goto(WEB+"/welcome",wait_until="networkidle")
        page.get_by_role("heading",name=re.compile("Understand the market")).wait_for()
        shot(page,"00-public-homepage.png")
        page.get_by_role("link",name=re.compile("Create workspace")).first.click()
        page.get_by_label("Organization name").wait_for()
        shot(page,"01-registration-form.png")
        page.get_by_label("Organization name").fill("Northstar Market Intelligence")
        page.get_by_label("Your full name").fill("Alex Morgan")
        page.get_by_label("Work email").fill(EMAIL); page.get_by_label("Password").fill(PASSWORD)
        offset=len(emails())
        with page.expect_response(lambda r:"/v1/auth/register" in r.url and r.request.method=="POST") as event:
            page.get_by_role("button",name=re.compile("Create workspace")).click()
        assert event.value.status==201,event.value.text()
        assert event.value.json()["email_verification_required"] is True
        page.get_by_role("heading",name="Verification required",exact=True).wait_for()
        shot(page,"02-verification-email-sent.png")
        page.goto(mail_link(wait_mail("Verify your Nexora account",EMAIL,offset),"verify-email"),wait_until="networkidle")
        shot(page,"03-email-verification.png")
        with page.expect_response(lambda r:"/v1/auth/verify-email" in r.url and r.request.method=="GET") as event:
            page.get_by_role("button",name=re.compile("Verify email")).click()
        assert event.value.status==200,event.value.text()
        page.get_by_role("link",name="Continue to sign in",exact=True).click()
        page.get_by_label("Work email").wait_for()
        shot(page,"04-login.png")
        login(page,EMAIL,PASSWORD); shot(page,"05-dashboard.png")
        me=get(page,"/v1/auth/me"); client_id=uuid.UUID(me["client_id"])
        assert me["role"]=="ORG_ADMIN"

        # Competitor management, including duplicate-domain protection.
        page.get_by_role("link",name="Competitors",exact=True).click()
        page.get_by_role("heading",name="Competitors",exact=True).wait_for()
        shot(page,"06-competitors.png")
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill("Retail Product Reference")
        page.get_by_label("Domain").fill("raw.githubusercontent.com")
        with page.expect_response(lambda r:r.url.endswith("/v1/competitors") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Save competitor",exact=True).click()
        assert event.value.status==201,event.value.text()
        competitor_id=event.value.json()["id"]
        page.get_by_text("Retail Product Reference",exact=True).wait_for()
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference")
        page.get_by_label("Domain").fill("raw.githubusercontent.com")
        with page.expect_response(lambda r:r.url.endswith("/v1/competitors") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Save competitor",exact=True).click()
        assert event.value.status==403,event.value.text()
        shot(page,"07-duplicate-domain-prevented.png")
        page.get_by_role("button",name="Cancel",exact=True).click()
        page.locator("tbody tr").filter(has_text="Retail Product Reference").first.locator("button.icon-btn").first.click()
        page.get_by_label("Name").fill("Retail Price Reference")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/competitors/{competitor_id}") and r.request.method=="PATCH") as event:
            page.get_by_role("button",name="Save competitor",exact=True).click()
        assert event.value.status==200,event.value.text()
        competitor="Retail Price Reference"
        assert any(x["id"]==competitor_id and x["name"]==competitor for x in get(page,"/v1/competitors"))

        # Scraper source and configuration.
        page.get_by_role("link",name="Sources",exact=True).click()
        page.get_by_role("heading",name="Sources",exact=True).wait_for()
        page.get_by_role("button",name="Add source",exact=True).click()
        choose(page,"Competitor",competitor)
        page.get_by_label("Source name").fill("Structured Retail Product")
        page.get_by_label("Base URL").fill(HIGH)
        choose(page,"Source type","OFFICIAL_STORE"); choose(page,"Collection method","HTTP_FAST")
        shot(page,"08-source-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/sources") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Create source",exact=True).click()
        assert event.value.status==201,event.value.text()
        source_id=event.value.json()["id"]
        page.get_by_title("Configuration",exact=True).click()
        page.get_by_label("Rate / minute").fill("60"); page.get_by_label("Delay seconds").fill("0.1")
        page.get_by_label("Timeout seconds").fill("25"); page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}")
        shot(page,"09-source-configuration.png")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/sources/{source_id}/configuration") and r.request.method=="PATCH") as event:
            page.get_by_role("button",name="Save configuration",exact=True).click()
        assert event.value.status==200,event.value.text()
        assert get(page,f"/v1/sources/{source_id}")["configuration"]["rate_limit_rpm"]==60
        page.get_by_title("Edit source",exact=True).click()
        page.get_by_label("Source name").fill("Structured Retail Product (Verified)")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/sources/{source_id}") and r.request.method=="PATCH") as event:
            page.get_by_role("button",name="Save changes",exact=True).click()
        assert event.value.status==200,event.value.text()
        source_name="Structured Retail Product (Verified)"
        shot(page,"10-source-configuration-saved.png")

        # Catalog lifecycle: create/edit/detail/monitoring/fields/import/export/archive.
        page.get_by_role("link",name="Offerings",exact=True).click()
        page.get_by_role("heading",name="Offerings",exact=True).wait_for()
        page.get_by_role("button",name="Add offering",exact=True).click()
        page.get_by_label("Name").fill(PRODUCT); choose(page,"Offering type","PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK"); page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD"); page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page,"11-catalog-offering-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Create offering",exact=True).click()
        assert event.value.status==201,event.value.text()
        offering_id=event.value.json()["id"]; page.get_by_text(PRODUCT,exact=True).wait_for()
        shot(page,"12-catalog-offering-created.png")
        page.locator("tbody tr").filter(has_text=PRODUCT).first.get_by_role("button",name=PRODUCT,exact=True).click()
        page.get_by_text("CURRENT PRICE",exact=True).wait_for(); shot(page,"13-offering-detail.png")
        page.get_by_role("button",name="Close dialog",exact=True).click()
        page.locator("tbody tr").filter(has_text=PRODUCT).first.get_by_title("Edit",exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/offerings/{offering_id}") and r.request.method=="PATCH") as event:
            page.get_by_role("button",name="Save changes",exact=True).click()
        assert event.value.status==200,event.value.text()
        assert float(get(page,f"/v1/offerings/{offering_id}")["current_price"])==429.99
        row=page.locator("tbody tr").filter(has_text=PRODUCT).first
        with page.expect_response(lambda r:r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method=="POST") as event:
            row.get_by_title("Toggle monitoring",exact=True).click()
        assert event.value.status==200
        assert get(page,f"/v1/offerings/{offering_id}")["is_monitored"] is False
        row=page.locator("tbody tr").filter(has_text=PRODUCT).first
        with page.expect_response(lambda r:r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method=="POST") as event:
            row.get_by_title("Toggle monitoring",exact=True).click()
        assert event.value.status==200
        shot(page,"14-monitoring-toggle.png")

        page.get_by_role("button",name="Dynamic fields",exact=True).click()
        page.get_by_text("Dynamic field definitions",exact=True).wait_for()
        page.get_by_role("button",name="Add field",exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months")
        page.get_by_label("Display name").fill("Review Cycle"); page.get_by_label("Data type").fill("NUMBER")
        page.get_by_label("Unit").fill("months"); page.get_by_label("Category").fill("Commercial")
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings/fields") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Create field",exact=True).click()
        assert event.value.status==201,event.value.text()
        shot(page,"15-dynamic-field-created.png")
        field_row=page.locator("div.list-row").filter(has_text="Review Cycle").last
        with page.expect_response(lambda r:"/v1/offerings/fields/" in r.url and r.request.method=="DELETE") as event:
            field_row.locator("button.danger-icon").click()
        assert event.value.status==204,event.value.text()
        page.get_by_role("button",name="Close dialog",exact=True).click()

        page.get_by_role("button",name="Bulk import",exact=True).click()
        items=[
            {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
            {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}}
        ]
        page.get_by_label("Offering JSON").fill(json.dumps(items)); shot(page,"16-bulk-import-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings/bulk") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Import offerings",exact=True).click()
        assert event.value.status==200,event.value.text(); assert event.value.json()["successful_count"]==2
        page.get_by_text("Sony ULT WEAR Wireless Headphones",exact=True).wait_for(); shot(page,"17-bulk-imported.png")
        csv_file,json_file=Path("/tmp/nexora.csv"),Path("/tmp/nexora.json")
        with page.expect_download() as download: page.get_by_role("button",name="CSV",exact=True).click()
        download.value.save_as(str(csv_file))
        with page.expect_download() as download: page.get_by_role("button",name="JSON",exact=True).click()
        download.value.save_as(str(json_file))
        assert csv_file.stat().st_size>0 and json_file.stat().st_size>0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones",exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones",exact=True).check()
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings/bulk-archive") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Archive 1",exact=True).click()
        assert event.value.status==200,event.value.text()
        archived=get(page,"/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")
        assert any(x["is_archived"] for x in archived["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill(""); page.get_by_text(PRODUCT,exact=True).wait_for()
        shot(page,"18-catalog-export-and-archive.png")

        # Real external HTML collection creates price history; second collection triggers a rule.
        page.get_by_role("link",name="Matches",exact=True).click()
        page.get_by_role("heading",name="Offering matches",exact=True).wait_for()
        page.get_by_role("button",name="Add match",exact=True).click()
        choose(page,"Offering",PRODUCT); choose(page,"Source",source_name)
        page.get_by_label("Competitor target URL").fill(HIGH); choose(page,"Status","APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98"); shot(page,"19-match-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offering-matches") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Create match",exact=True).click()
        assert event.value.status==201,event.value.text(); match_id=event.value.json()["id"]
        row=page.locator("tbody tr").filter(has_text=PRODUCT).first; shot(page,"20-offering-match.png")
        with page.expect_response(lambda r:"/v1/collections/run" in r.url and r.request.method=="POST") as event:
            row.get_by_title("Run collection",exact=True).click()
        first=event.value; assert first.status in (200,201),first.text()
        first_data=first.json(); assert first_data["status"]=="COMPLETED",first_data
        assert first_data["observation"]["price"]=="399.99",first_data
        history=get(page,f"/v1/offerings/{offering_id}/history"); assert history,history
        shot(page,"21-first-price-observation.png")

        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_role("heading",name="Alerts",exact=True).wait_for()
        page.get_by_role("button",name="New rule",exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page,"Alert type","PERCENTAGE_DROP"); choose(page,"Offering scope",PRODUCT)
        page.get_by_label("Threshold").fill("10"); page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page,"22-alert-rule-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/alerts/rules") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Create rule",exact=True).click()
        assert event.value.status==201,event.value.text(); shot(page,"23-alert-rule-created.png")

        page.get_by_role("link",name="Matches",exact=True).click()
        page.locator("tbody tr").filter(has_text=PRODUCT).first.get_by_title("Edit",exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW)
        with page.expect_response(lambda r:r.url.endswith(f"/v1/offering-matches/{match_id}") and r.request.method=="PATCH") as event:
            page.get_by_role("button",name="Save changes",exact=True).click()
        assert event.value.status==200,event.value.text()
        with page.expect_response(lambda r:"/v1/collections/run" in r.url and r.request.method=="POST") as event:
            page.locator("tbody tr").filter(has_text=PRODUCT).first.get_by_title("Run collection",exact=True).click()
        second=event.value.json()
        assert event.value.status in (200,201),event.value.text()
        assert second["observation"]["price"]=="349.99",second
        assert second["snapshot"]["previous_price"]=="399.99",second
        assert second["snapshot"]["percentage_difference"].startswith("-12.50%"),second
        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_text("Competitor price dropped",exact=True).wait_for(timeout=20000)
        logs=get(page,"/v1/alerts/logs")
        alert=next((x for x in logs if x["title"]=="Competitor price dropped"),None)
        assert alert and not alert["is_read"],logs
        page.get_by_title("Mark as read",exact=True).click(); page.get_by_title("Read",exact=True).wait_for()
        assert any(x["id"]==alert["id"] and x["is_read"] for x in get(page,"/v1/alerts/logs"))
        shot(page,"24-alert-triggered-and-read.png")
        sent=dispatch_pending_notifications.run(); assert int(sent.get("sent",0))>=1,sent
        email_body=wait_mail("Nexora Competitive Alert: Competitor price dropped",EMAIL)
        assert "Nexora detected a competitive intelligence event" in email_body
        assert asyncio.run(has_sent_outbox(client_id))

        # Jobs and safe catalog discovery.
        page.get_by_role("link",name="Jobs",exact=True).click()
        page.get_by_role("heading",name="Jobs",exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH",exact=False).wait_for(); shot(page,"25-job-history.png")
        page.get_by_role("button",name="Details",exact=True).first.click()
        page.get_by_text("Job ID",exact=True).wait_for(); shot(page,"26-job-details.png")
        page.get_by_role("button",name="Close dialog",exact=True).click()
        page.get_by_role("link",name="Discovery",exact=True).click()
        page.get_by_role("heading",name="Website discovery",exact=True).wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health")
        page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r:r.url.endswith("/v1/discovery/run") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Run discovery",exact=True).click()
        assert event.value.status==422,event.value.text(); shot(page,"27-private-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY); choose(page,"Default type","PRODUCT")
        with page.expect_response(lambda r:r.url.endswith("/v1/discovery/run") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Run discovery",exact=True).click()
        discovered=event.value; assert discovered.status==200,discovered.text()
        result=discovered.json()
        assert result["status"]=="COMPLETED" and result["created_offerings_count"]==1,result
        assert result["items"][0]["name"]==DISCOVERED,result
        page.get_by_text(DISCOVERED,exact=True).wait_for(timeout=20000); shot(page,"28-discovery-created.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/discovery/run") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Run discovery",exact=True).click()
        again=event.value.json()
        assert again["created_offerings_count"]==0 and again["updated_offerings_count"]==1,again
        catalog=get(page,"/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(x["sku"]=="WF1000XM5-BLK" for x in catalog["items"])==1
        shot(page,"29-discovery-upsert.png")

        # Account profile/password lifecycle uses real API and one-time email links.
        page.get_by_role("link",name="Settings",exact=True).click()
        page.get_by_role("heading",name="Settings",exact=True).wait_for()
        page.get_by_label("Organization name").wait_for(); shot(page,"30-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics")
        page.get_by_label("Country").fill("US"); page.get_by_label("Default currency").fill("USD")
        page.get_by_label("Market").fill("US"); page.get_by_label("Industry").fill("Consumer Electronics")
        page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/profile") and r.request.method=="PATCH") as event:
            page.get_by_role("button",name="Save profile",exact=True).click()
        assert event.value.status==200,event.value.text()
        assert get(page,"/v1/auth/profile")["company_name"]=="Northstar Analytics"
        page.get_by_label("Current password").fill(PASSWORD); page.get_by_label("New password").fill(CHANGED)
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/change-password") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Update password",exact=True).click()
        assert event.value.status==200,event.value.text()
        assert page.request.post(API+"/v1/auth/login",json={"email":EMAIL,"password":PASSWORD}).status==401
        assert page.request.post(API+"/v1/auth/login",json={"email":EMAIL,"password":CHANGED}).status==200
        shot(page,"31-password-changed.png")

        page.evaluate("() => localStorage.clear()"); page.goto(WEB+"/login",wait_until="networkidle")
        page.get_by_role("link",name="Forgot password?",exact=True).click()
        page.get_by_label("Work email").fill(EMAIL); offset=len(emails())
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/forgot-password") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Send reset link",exact=True).click()
        assert event.value.status==200,event.value.text(); shot(page,"32-password-reset-requested.png")
        reset_url=mail_link(wait_mail("Reset your Nexora password",EMAIL,offset),"reset-password")
        page.goto(reset_url,wait_until="networkidle"); page.get_by_label("New password").fill(RESET)
        shot(page,"33-password-reset-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/reset-password") and r.request.method=="POST") as event:
            page.get_by_role("button",name="Update password",exact=True).click()
        assert event.value.status==200,event.value.text()
        page.get_by_role("heading",name="Password updated",exact=True).wait_for(); shot(page,"34-password-reset-complete.png")
        page.get_by_role("link",name="Continue to sign in",exact=True).click(); login(page,EMAIL,RESET)
        shot(page,"35-dashboard-after-reset.png")

        # Role checks use persistent role-bearing records and issued tokens.
        suffix=uuid.uuid4().hex[:8]
        viewer=asyncio.run(create_role(client_id,RoleEnum.VIEWER,suffix))
        analyst=asyncio.run(create_role(client_id,RoleEnum.ANALYST,suffix))
        logout(page); login(page,analyst,ROLE_PASSWORD)
        page.get_by_role("link",name="Competitors",exact=True).click()
        assert page.get_by_role("button",name="Add competitor",exact=True).count()==0
        response=page.request.post(API+"/v1/competitors",headers={"Authorization":f"Bearer {token(page)}"},json={"name":"Denied","domain":"denied.example"})
        assert response.status==403,response.text()
        page.get_by_role("link",name="Discovery",exact=True).click()
        page.get_by_role("button",name="Run discovery",exact=True).wait_for(); shot(page,"36-analyst-permissions.png")
        logout(page); login(page,viewer,ROLE_PASSWORD)
        page.get_by_role("link",name="Offerings",exact=True).click()
        assert page.get_by_role("button",name="Add offering",exact=True).count()==0
        assert page.get_by_role("button",name="Bulk import",exact=True).count()==0
        response=page.request.post(API+"/v1/competitors",headers={"Authorization":f"Bearer {token(page)}"},json={"name":"Viewer Write Attempt","domain":"viewer-write.example"})
        assert response.status==403,response.text(); shot(page,"37-viewer-read-only.png")

        mobile=context.new_page(); mobile.set_viewport_size({"width":390,"height":844})
        mobile.goto(WEB+"/welcome",wait_until="networkidle")
        dimensions=mobile.evaluate("() => ({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})")
        assert dimensions["scroll"]<=dimensions["width"],dimensions
        shot(mobile,"38-mobile-homepage.png")
        if JS_ERRORS: raise AssertionError("Unhandled browser JavaScript errors: "+" | ".join(JS_ERRORS))
        expected=[
            "00-public-homepage.png","01-registration-form.png","02-verification-email-sent.png","03-email-verification.png","04-login.png","05-dashboard.png",
            "06-competitors.png","07-duplicate-domain-prevented.png","08-source-form.png","09-source-configuration.png","10-source-configuration-saved.png",
            "11-catalog-offering-form.png","12-catalog-offering-created.png","13-offering-detail.png","14-monitoring-toggle.png","15-dynamic-field-created.png",
            "16-bulk-import-form.png","17-bulk-imported.png","18-catalog-export-and-archive.png","19-match-form.png","20-offering-match.png",
            "21-first-price-observation.png","22-alert-rule-form.png","23-alert-rule-created.png","24-alert-triggered-and-read.png","25-job-history.png",
            "26-job-details.png","27-private-target-blocked.png","28-discovery-created.png","29-discovery-upsert.png","30-workspace-settings.png",
            "31-password-changed.png","32-password-reset-requested.png","33-password-reset-form.png","34-password-reset-complete.png","35-dashboard-after-reset.png",
            "36-analyst-permissions.png","37-viewer-read-only.png","38-mobile-homepage.png"]
        missing=[name for name in expected if not (SHOTS/name).is_file()]
        assert not missing,f"Missing screenshots: {missing}"
        print(f"PASS: UI -> FastAPI -> PostgreSQL; alert email delivered; {len(expected)} screenshots captured.",flush=True)
        browser.close(); context.close()

if __name__=="__main__": main()

"""Full Nexora browser journey against the real FastAPI/PostgreSQL stack."""
from __future__ import annotations
import asyncio, json, os, re, time, uuid
from email import message_from_string
from pathlib import Path
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel
from backend.workers.tasks import dispatch_pending_notifications

BASE=os.getenv("NEXORA_FRONTEND_URL","http://127.0.0.1:4173").rstrip("/")
API=os.getenv("NEXORA_API_URL","http://127.0.0.1:8000").rstrip("/")
MAIL=Path(os.getenv("NEXORA_MAILBOX","/tmp/nexora-mailbox.jsonl"))
SHOTS=Path(os.getenv("NEXORA_SCREENSHOT_DIR","docs/walkthrough/screenshots"))
EMAIL="market-analyst@northstar.example.com"
PW="Nexora-Strong!2026"; PW2="Nexora-Updated!2026"; PW3="Nexora-Final!2026"; ROLE_PW="Nexora-Role!2026"
ORG="Northstar Market Intelligence"; PERSON="Alex Morgan"
COMP="Retail Product Reference"; SRC="Retail Product Listing"
ITEM="Sony WH-1000XM5 Wireless Headphones"
DISCOVERED="Sony WF-1000XM5 Truly Wireless Noise Canceling Earbuds"
FIX="https://cdn.jsdelivr.net/gh/faizansaiyed123/Nexora-backend@main/tests/fixtures/"
HIGH=FIX+"retail-price-high.html"; LOW=FIX+"retail-price-low.html"; DISCOVERY=FIX+"retail-discovery-product.html"
js_errors=[]

def shot(page,name):
    SHOTS.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(SHOTS/name),full_page=True,animations="disabled",caret="hide")
    print("Captured "+name,flush=True)

def choose(page,label,value):
    field=page.locator("label.field").filter(has=page.locator(".field-label").get_by_text(label,exact=True)).last
    field.locator("button.select-trigger").click()
    field.get_by_role("option",name=re.compile(re.escape(value),re.I)).last.click()

def token(page):
    value=page.evaluate("() => localStorage.getItem('nexora.access')")
    assert value,"Missing browser access token"; return value

def get(page,path,status=200):
    r=page.request.get(API+path,headers={"Authorization":"Bearer "+token(page)})
    assert r.status==status,f"GET {path}: {r.status} {r.text()}"
    return r.json()

def mails():
    if not MAIL.exists(): return []
    result=[]
    for line in MAIL.read_text(encoding="utf-8").splitlines():
        try:
            row=json.loads(line)
            if row.get("data"): result.append(row)
        except (ValueError,AttributeError): pass
    return result

def mail_parts(row):
    msg=message_from_string(row["data"])
    if msg.is_multipart():
        body="\n".join((p.get_payload(decode=True) or b"").decode(p.get_content_charset() or "utf-8","replace") for p in msg.walk() if p.get_content_type()=="text/plain")
    else:
        raw=msg.get_payload(decode=True); body=raw.decode(msg.get_content_charset() or "utf-8","replace") if raw else str(msg.get_payload())
    return str(msg.get("Subject","")),body,[str(x).lower() for x in row.get("rcpt_tos",[])]

def wait_mail(subject,recipient,start,timeout=35):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        for row in mails()[start:]:
            title,body,recipients=mail_parts(row)
            if subject.lower() in title.lower() and recipient.lower() in recipients: return body
        time.sleep(.25)
    raise AssertionError(f"Missing {subject!r} for {recipient}; received {[mail_parts(r)[0] for r in mails()[start:]]}")

def mail_link(body,route):
    found=re.search(rf"https?://[^\s<>]+/{re.escape(route)}\?token=[^\s<>]+",body)
    assert found,f"Missing {route} link in delivered email"
    return found.group(0).rstrip(".,);")

def login(page,email,password):
    page.get_by_label("Work email").fill(email); page.get_by_label("Password").fill(password)
    with page.expect_response(lambda r:r.url.endswith("/v1/auth/login") and r.request.method=="POST") as ev:
        page.get_by_role("button",name=re.compile("Sign in")).click()
    assert ev.value.status==200,ev.value.text()
    page.get_by_role("heading",name="Dashboard",exact=True).wait_for(timeout=15000)

def signout(page):
    page.locator("button.profile-trigger").click()
    page.get_by_role("menuitem",name="Sign out",exact=True).click()
    page.get_by_label("Work email").wait_for()

async def role_user(client_id,role,suffix):
    address=f"{role.value.lower()}-{suffix}@northstar.example.com"
    async with AsyncSessionLocal() as s:
        if await s.scalar(select(UserModel).where(UserModel.email==address)): return address
        s.add(UserModel(client_id=client_id,email=address,full_name=f"Northstar {role.value.title()}",hashed_password=get_password_hash(ROLE_PW),role=role,is_active=True,email_verified=True))
        await s.commit()
    return address

async def outbox_states(client_id):
    async with AsyncSessionLocal() as s:
        r=await s.execute(select(NotificationOutboxModel.status).where(NotificationOutboxModel.client_id==client_id,NotificationOutboxModel.channel=="email"))
        return list(r.scalars().all())

def main():
    SHOTS.mkdir(parents=True,exist_ok=True)
    if MAIL.exists(): MAIL.unlink()
    with sync_playwright() as p:
        browser=p.chromium.launch()
        context=browser.new_context(viewport={"width":1440,"height":1000},accept_downloads=True,device_scale_factor=1)
        page=context.new_page(); page.set_default_timeout(12000)
        page.on("pageerror",lambda e:js_errors.append(str(e))); page.on("dialog",lambda d:d.accept())

        page.goto(BASE+"/welcome",wait_until="networkidle")
        page.get_by_role("heading",name=re.compile("Understand the market")).wait_for()
        shot(page,"00-public-homepage.png")
        page.get_by_role("link",name=re.compile("Create workspace")).first.click()
        page.get_by_label("Organization name").wait_for(); shot(page,"01-registration-form.png")
        page.get_by_label("Organization name").fill(ORG); page.get_by_label("Your full name").fill(PERSON)
        page.get_by_label("Work email").fill(EMAIL); page.get_by_label("Password").fill(PW)
        mail_start=len(mails())
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/register") and r.request.method=="POST") as ev:
            page.get_by_role("button",name=re.compile("Create workspace")).click()
        assert ev.value.status==201,ev.value.text()
        assert ev.value.json()["email_verification_required"] is True
        page.get_by_role("heading",name="Verification required",exact=True).wait_for(); shot(page,"02-verification-email-sent.png")
        verify=mail_link(wait_mail("Verify your Nexora account",EMAIL,mail_start),"verify-email")
        page.goto(verify,wait_until="networkidle"); shot(page,"03-email-verification-page.png")
        with page.expect_response(lambda r:r.url.startswith(API+"/v1/auth/verify-email") and r.request.method=="GET") as ev:
            page.get_by_role("button",name=re.compile("Verify email")).click()
        assert ev.value.status==200,ev.value.text()
        page.get_by_role("link",name="Continue to sign in",exact=True).wait_for(); shot(page,"04-email-verified.png")
        page.get_by_role("link",name="Continue to sign in",exact=True).click()
        page.get_by_label("Work email").wait_for(); shot(page,"05-sign-in.png")
        login(page,EMAIL,PW); shot(page,"06-empty-workspace-dashboard.png")
        user=get(page,"/v1/auth/me"); assert user["role"]=="ORG_ADMIN"; tenant=uuid.UUID(user["client_id"])

        # Competitor create, duplicate-domain handling, edit and API read-back.
        page.get_by_role("link",name="Competitors",exact=True).click(); page.get_by_role("heading",name="Competitors",exact=True).wait_for()
        shot(page,"07-competitors.png")
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill(COMP); page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r:r.url.endswith("/v1/competitors") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Save competitor",exact=True).click()
        assert ev.value.status==201,ev.value.text(); competitor_id=ev.value.json()["id"]
        page.get_by_text(COMP,exact=True).wait_for(); shot(page,"08-competitor-created.png")
        page.get_by_role("button",name="Add competitor",exact=True).click()
        page.get_by_label("Name").fill("Duplicate Retail Reference"); page.get_by_label("Domain").fill("cdn.jsdelivr.net")
        with page.expect_response(lambda r:r.url.endswith("/v1/competitors") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Save competitor",exact=True).click()
        assert ev.value.status==403,ev.value.text()
        page.get_by_text("A competitor with this domain already exists.").wait_for(); shot(page,"09-duplicate-domain-protection.png")
        page.get_by_role("button",name="Cancel",exact=True).click()
        page.locator("tbody tr").filter(has_text=COMP).first.locator("button").first.click()
        page.get_by_label("Name").fill("Retail Product Reference")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/competitors/{competitor_id}") and r.request.method=="PATCH") as ev:
            page.get_by_role("button",name="Save competitor",exact=True).click()
        assert ev.value.status==200,ev.value.text()
        competitor_name="Retail Product Reference"
        assert any(x["id"]==competitor_id and x["name"]==competitor_name for x in get(page,"/v1/competitors"))

        # Add a source and persist a custom collection configuration.
        page.get_by_role("link",name="Sources",exact=True).click()
        page.get_by_role("heading",name="Sources",exact=True).wait_for()
        page.get_by_role("button",name="Add source",exact=True).click()
        choose(page,"Competitor",competitor_name); page.get_by_label("Source name").fill(SRC); page.get_by_label("Base URL").fill(HIGH)
        choose(page,"Source type","OFFICIAL_STORE"); choose(page,"Collection method","HTTP_FAST")
        shot(page,"10-source-create-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/sources") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Create source",exact=True).click()
        assert ev.value.status==201,ev.value.text(); source_id=ev.value.json()["id"]
        page.get_by_text(SRC,exact=True).wait_for(); shot(page,"11-source-created.png")
        page.get_by_title("Configuration",exact=True).click()
        page.get_by_label("Rate / minute").fill("60"); page.get_by_label("Delay seconds").fill("0")
        page.get_by_label("Timeout seconds").fill("25"); page.get_by_label("Max retries").fill("1")
        page.get_by_label("Custom headers (JSON)").fill('{"Accept-Language":"en-US,en;q=0.9"}')
        page.get_by_label("Extraction selectors (JSON)").fill("{}"); shot(page,"12-source-configuration.png")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/sources/{source_id}/configuration") and r.request.method=="PATCH") as ev:
            page.get_by_role("button",name="Save configuration",exact=True).click()
        assert ev.value.status==200,ev.value.text()
        assert get(page,f"/v1/sources/{source_id}")["configuration"]["rate_limit_rpm"]==60
        page.locator("tbody tr").filter(has_text=SRC).get_by_title("Edit source").click()
        page.get_by_label("Source name").fill(SRC+" (Structured)")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/sources/{source_id}") and r.request.method=="PATCH") as ev:
            page.get_by_role("button",name="Save changes",exact=True).click()
        assert ev.value.status==200,ev.value.text(); source_name=SRC+" (Structured")
        shot(page,"13-source-configuration-saved.png")

        # Catalog lifecycle: create/edit/detail, monitoring, field definition, import/export/archive.
        page.get_by_role("link",name="Offerings",exact=True).click(); page.get_by_role("heading",name="Offerings",exact=True).wait_for()
        page.get_by_role("button",name="Add offering",exact=True).click()
        page.get_by_label("Name").fill(ITEM); choose(page,"Offering type","PRODUCT")
        page.get_by_label("SKU").fill("WH1000XM5-BLK"); page.get_by_label("Current price").fill("449.99")
        page.get_by_label("Currency").fill("USD"); page.get_by_label("Market").fill("US")
        page.get_by_label("Category").fill("Electronics > Audio")
        page.get_by_label("Product URL").fill("https://www.sony.com/electronics/headband-headphones/wh-1000xm5")
        page.get_by_label("Description").fill("Wireless over-ear headphones with active noise cancellation and multipoint Bluetooth.")
        page.get_by_label("Dynamic attributes (JSON)").fill('{"brand":"Sony","color":"Black","battery_life_hours":30,"noise_cancellation":true}')
        shot(page,"14-offering-create-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Create offering",exact=True).click()
        assert ev.value.status==201,ev.value.text(); offering_id=ev.value.json()["id"]
        page.get_by_text(ITEM,exact=True).wait_for(); shot(page,"15-catalog-offering-created.png")
        page.locator("tbody tr").filter(has_text=ITEM).get_by_role("button",name=ITEM,exact=True).click()
        page.get_by_text("CURRENT PRICE",exact=True).wait_for(); shot(page,"16-offering-detail-and-history.png")
        page.get_by_role("button",name="Close dialog",exact=True).click()
        page.locator("tbody tr").filter(has_text=ITEM).get_by_title("Edit",exact=True).click()
        page.get_by_label("Current price").fill("429.99")
        with page.expect_response(lambda r:r.url.endswith(f"/v1/offerings/{offering_id}") and r.request.method=="PATCH") as ev:
            page.get_by_role("button",name="Save changes",exact=True).click()
        assert ev.value.status==200,ev.value.text(); assert float(get(page,f"/v1/offerings/{offering_id}")["current_price"])==429.99
        for should_be in (False,True):
            row=page.locator("tbody tr").filter(has_text=ITEM).first
            with page.expect_response(lambda r:r.url.endswith(f"/v1/offerings/{offering_id}/toggle-monitoring") and r.request.method=="POST") as ev:
                row.get_by_title("Toggle monitoring",exact=True).click()
            assert ev.value.status==200; assert get(page,f"/v1/offerings/{offering_id}")["is_monitored"] is should_be
        shot(page,"17-catalog-monitoring.png")
        page.get_by_role("button",name="Dynamic fields",exact=True).click()
        page.get_by_text("Dynamic field definitions",exact=True).wait_for(); shot(page,"18-dynamic-field-registry.png")
        page.get_by_role("button",name="Add field",exact=True).click()
        page.get_by_label("Machine name").fill("review_cycle_months"); page.get_by_label("Display name").fill("Review Cycle")
        page.get_by_label("Data type").fill("NUMBER"); page.get_by_label("Unit").fill("months"); page.get_by_label("Category").fill("Commercial")
        shot(page,"19-dynamic-field-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings/fields") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Create field",exact=True).click()
        assert ev.value.status==201,ev.value.text(); page.get_by_text("Review Cycle",exact=True).wait_for()
        shot(page,"20-dynamic-field-created.png")
        with page.expect_response(lambda r:"/v1/offerings/fields/" in r.url and r.request.method=="DELETE") as ev:
            page.locator("div.list-row").filter(has_text="Review Cycle").last.locator("button.danger-icon").click()
        assert ev.value.status==204,ev.value.text()
        page.get_by_role("button",name="Close dialog",exact=True).click()
        import_items=[
          {"name":"Sony ULT WEAR Wireless Headphones","offering_type":"PRODUCT","sku":"ULTWEAR-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/headband-headphones/ult-wear","category":"Electronics > Audio","attributes":{"brand":"Sony","color":"Black"}},
          {"name":"Sony LinkBuds S","offering_type":"PRODUCT","sku":"LINKBUDS-S-BLK","current_price":"199.99","currency":"USD","market":"US","url":"https://www.sony.com/electronics/truly-wireless/linkbuds-s","category":"Electronics > Audio","attributes":{"brand":"Sony","fit":"In-ear"}}]
        page.get_by_role("button",name="Bulk import",exact=True).click()
        page.get_by_label("Offering JSON").fill(json.dumps(import_items)); shot(page,"21-bulk-import-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings/bulk") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Import offerings",exact=True).click()
        assert ev.value.status==200,ev.value.text(); assert ev.value.json()["successful_count"]==2
        page.get_by_text("Sony ULT WEAR Wireless Headphones",exact=True).wait_for(); shot(page,"22-bulk-imported-catalog.png")
        for name,filename in (("CSV","/tmp/nexora-export.csv"),("JSON","/tmp/nexora-export.json")):
            with page.expect_download(timeout=15000) as ev: page.get_by_role("button",name=name,exact=True).click()
            ev.value.save_as(filename); assert Path(filename).is_file() and Path(filename).stat().st_size>0
        page.get_by_placeholder("Search name, SKU, category…").fill("Sony ULT WEAR")
        page.get_by_text("Sony ULT WEAR Wireless Headphones",exact=True).wait_for()
        page.get_by_label("Select Sony ULT WEAR Wireless Headphones",exact=True).check()
        with page.expect_response(lambda r:r.url.endswith("/v1/offerings/bulk-archive") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Archive 1",exact=True).click()
        assert ev.value.status==200,ev.value.text()
        assert any(x["is_archived"] for x in get(page,"/v1/offerings?q=Sony%20ULT%20WEAR&include_archived=true&page=1&page_size=20")["items"])
        page.get_by_placeholder("Search name, SKU, category…").fill(""); page.get_by_text(ITEM,exact=True).wait_for()
        shot(page,"23-catalog-import-export-archive.png")

        # Connect the catalog offering to a source and persist real fetched price observations.
        page.get_by_role("link",name="Matches",exact=True).click()
        page.get_by_role("heading",name="Offering matches",exact=True).wait_for()
        page.get_by_role("button",name="Add match",exact=True).click()
        choose(page,"Offering",ITEM); choose(page,"Source",source_name)
        page.get_by_label("Competitor target URL").fill(HIGH); choose(page,"Status","APPROVED")
        page.get_by_label("Confidence (0–1)").fill("0.98"); shot(page,"24-offering-match-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/offering-matches") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Create match",exact=True).click()
        assert ev.value.status==201,ev.value.text(); match_id=ev.value.json()["id"]
        page.get_by_text(HIGH,exact=False).wait_for(); shot(page,"25-offering-match-created.png")
        with page.expect_response(lambda r:"/v1/collections/run" in r.url and r.request.method=="POST") as ev:
            page.locator("tbody tr").filter(has_text=ITEM).get_by_title("Run collection",exact=True).click()
        first=ev.value; assert first.status in (200,201),first.text()
        snap1=first.json(); assert snap1["status"]=="COMPLETED" and snap1["observation"]["price"]=="399.99",snap1
        assert get(page,f"/v1/offerings/{offering_id}/history"),"Collection history must persist"
        shot(page,"26-first-price-observation.png")

        # A threshold rule fires when a second real HTTP collection observes a lower price.
        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_role("heading",name="Alerts",exact=True).wait_for()
        page.get_by_role("button",name="New rule",exact=True).click()
        page.get_by_label("Rule name").fill("Sony retail price drop")
        choose(page,"Alert type","PERCENTAGE_DROP"); choose(page,"Offering scope",ITEM)
        page.get_by_label("Threshold").fill("10"); page.get_by_label("Cooldown (minutes)").fill("60")
        shot(page,"27-alert-rule-form.png")
        with page.expect_response(lambda r:r.url.endswith("/v1/alerts/rules") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Create rule",exact=True).click()
        assert ev.value.status==201,ev.value.text(); page.get_by_text("Sony retail price drop",exact=True).wait_for()
        shot(page,"28-alert-rule-created.png")
        page.get_by_role("link",name="Matches",exact=True).click()
        page.locator("tbody tr").filter(has_text=ITEM).get_by_title("Edit",exact=True).click()
        page.get_by_label("Competitor target URL").fill(LOW)
        with page.expect_response(lambda r:r.url.endswith(f"/v1/offering-matches/{match_id}") and r.request.method=="PATCH") as ev:
            page.get_by_role("button",name="Save changes",exact=True).click()
        assert ev.value.status==200,ev.value.text()
        with page.expect_response(lambda r:"/v1/collections/run" in r.url and r.request.method=="POST") as ev:
            page.locator("tbody tr").filter(has_text=ITEM).get_by_title("Run collection",exact=True).click()
        second=ev.value; assert second.status in (200,201),second.text()
        snap2=second.json(); assert snap2["observation"]["price"]=="349.99" and snap2["snapshot"]["previous_price"]=="399.99",snap2
        assert str(snap2["snapshot"]["percentage_difference"]).startswith("-12.50%"),snap2
        page.get_by_role("link",name="Alerts",exact=True).click()
        page.get_by_text("Competitor price dropped",exact=True).wait_for(timeout=15000)
        logs=get(page,"/v1/alerts/logs"); log=next((x for x in logs if x["title"]=="Competitor price dropped"),None)
        assert log and not log["is_read"],logs
        page.get_by_title("Mark as read",exact=True).click(); page.get_by_title("Read",exact=True).wait_for()
        assert any(x["id"]==log["id"] and x["is_read"] for x in get(page,"/v1/alerts/logs"))
        shot(page,"29-alert-triggered-and-read.png")
        mail_start=len(mails()); result=dispatch_pending_notifications.run(); assert int(result.get("sent",0))>=1,result
        alert=wait_mail("Nexora Competitive Alert: Competitor price dropped",EMAIL,mail_start)
        assert "competitive intelligence event" in alert.lower()
        assert "SENT" in asyncio.run(outbox_states(tenant)),asyncio.run(outbox_states(tenant))

        page.get_by_role("link",name="Jobs",exact=True).click(); page.get_by_role("heading",name="Jobs",exact=True).wait_for()
        page.get_by_text("ON DEMAND REFRESH",exact=False).wait_for(); shot(page,"30-collection-job-history.png")
        page.get_by_role("button",name="Details",exact=True).first.click(); page.get_by_text("Job ID",exact=True).wait_for()
        shot(page,"31-collection-job-detail.png"); page.get_by_role("button",name="Close dialog",exact=True).click()

        # The server rejects private destinations, then discovers a real structured product page.
        page.get_by_role("link",name="Discovery",exact=True).click()
        page.get_by_role("heading",name="Website discovery",exact=True).wait_for()
        page.get_by_label("Website URL").fill("http://127.0.0.1:8000/v1/health"); page.get_by_label("Max pages").fill("1")
        with page.expect_response(lambda r:r.url.endswith("/v1/discovery/run") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Run discovery",exact=True).click()
        assert ev.value.status==422,ev.value.text(); shot(page,"32-private-target-blocked.png")
        page.get_by_label("Website URL").fill(DISCOVERY); choose(page,"Default type","PRODUCT")
        with page.expect_response(lambda r:r.url.endswith("/v1/discovery/run") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Run discovery",exact=True).click()
        assert ev.value.status==200,ev.value.text(); discovered=ev.value.json()
        assert discovered["status"]=="COMPLETED" and discovered["created_offerings_count"]==1,discovered
        assert discovered["items"][0]["name"]==DISCOVERED,discovered
        page.get_by_text(DISCOVERED,exact=True).wait_for(); shot(page,"33-discovered-catalog-item.png")
        assert get(page,f"/v1/discovery/jobs/{discovered['job_id']}")["items"][0]["sku"]=="WF1000XM5-BLK"
        with page.expect_response(lambda r:r.url.endswith("/v1/discovery/run") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Run discovery",exact=True).click()
        again=ev.value; assert again.status==200,again.text()
        again_json=again.json(); assert again_json["created_offerings_count"]==0 and again_json["updated_offerings_count"]==1,again_json
        catalog=get(page,"/v1/offerings?q=Sony%20WF-1000XM5&page=1&page_size=20")
        assert sum(x["sku"]=="WF1000XM5-BLK" for x in catalog["items"])==1
        shot(page,"34-discovery-upsert-history.png")

        # Workspace settings and password lifecycle (change, email reset, one-time token).
        page.locator("button.profile-trigger").click()
        page.get_by_role("menuitem",name="Account settings",exact=True).click()
        page.get_by_role("heading",name="Settings",exact=True).wait_for(); shot(page,"35-workspace-settings.png")
        page.get_by_label("Company name").fill("Northstar Analytics"); page.get_by_label("Country").fill("US")
        page.get_by_label("Default currency").fill("USD"); page.get_by_label("Market").fill("US")
        page.get_by_label("Industry").fill("Consumer Electronics"); page.get_by_label("Timezone").fill("America/New_York")
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/profile") and r.request.method=="PATCH") as ev:
            page.get_by_role("button",name="Save profile",exact=True).click()
        assert ev.value.status==200,ev.value.text(); assert get(page,"/v1/auth/profile")["company_name"]=="Northstar Analytics"
        shot(page,"36-workspace-profile-saved.png")
        page.get_by_label("Current password").fill(PW); page.get_by_label("New password").fill(PW2)
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/change-password") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Update password",exact=True).click()
        assert ev.value.status==200,ev.value.text()
        assert page.request.post(API+"/v1/auth/login",json={"email":EMAIL,"password":PW}).status==401
        assert page.request.post(API+"/v1/auth/login",json={"email":EMAIL,"password":PW2}).status==200
        shot(page,"37-password-changed.png")
        signout(page); page.get_by_role("link",name="Forgot password?",exact=True).click()
        page.get_by_label("Work email").fill(EMAIL); reset_start=len(mails())
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/forgot-password") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Send reset link",exact=True).click()
        assert ev.value.status==200,ev.value.text(); page.get_by_role("heading",name="Check your inbox",exact=True).wait_for()
        shot(page,"38-password-reset-requested.png")
        reset=mail_link(wait_mail("Reset your Nexora password",EMAIL,reset_start),"reset-password")
        page.goto(reset,wait_until="networkidle"); page.get_by_label("New password").wait_for(); shot(page,"39-password-reset-form.png")
        page.get_by_label("New password").fill(PW3)
        with page.expect_response(lambda r:r.url.endswith("/v1/auth/reset-password") and r.request.method=="POST") as ev:
            page.get_by_role("button",name="Update password",exact=True).click()
        assert ev.value.status==200,ev.value.text(); page.get_by_role("heading",name="Password updated",exact=True).wait_for()
        shot(page,"40-password-reset-complete.png")
        page.get_by_role("link",name="Continue to sign in",exact=True).click(); login(page,EMAIL,PW3)
        shot(page,"41-dashboard-after-reset.png")

        # Stored role identities demonstrate analyst capabilities and viewer read-only restrictions.
        viewer=asyncio.run(role_user(tenant,RoleEnum.VIEWER,"walkthrough"))
        analyst=asyncio.run(role_user(tenant,RoleEnum.ANALYST,"walkthrough"))
        signout(page); login(page,analyst,ROLE_PW)
        page.get_by_role("link",name="Competitors",exact=True).click()
        assert page.get_by_role("button",name="Add competitor",exact=True).count()==0
        response=page.request.post(API+"/v1/competitors",headers={"Authorization":"Bearer "+token(page)},json={"name":"Denied","domain":"denied-analyst.example"})
        assert response.status==403,response.text()
        page.get_by_role("link",name="Discovery",exact=True).click()
        assert page.get_by_role("button",name="Run discovery",exact=True).is_visible()
        shot(page,"42-analyst-workspace.png")
        signout(page); login(page,viewer,ROLE_PW)
        page.get_by_role("link",name="Offerings",exact=True).click()
        assert page.get_by_role("button",name="Add offering",exact=True).count()==0
        assert page.get_by_role("button",name="Bulk import",exact=True).count()==0
        assert page.get_by_role("button",name="Dynamic fields",exact=True).count()==0
        response=page.request.get(API+"/v1/offerings",headers={"Authorization":"Bearer "+token(page)})
        assert response.status==200,response.text()
        response=page.request.post(API+"/v1/competitors",headers={"Authorization":"Bearer "+token(page)},json={"name":"Denied viewer","domain":"denied-viewer.example"})
        assert response.status==403,response.text()
        shot(page,"43-viewer-read-only-workspace.png")

        mobile=context.new_page(); mobile.set_viewport_size({"width":390,"height":844})
        mobile.goto(BASE+"/welcome",wait_until="networkidle")
        dims=mobile.evaluate("() => ({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
        assert dims["content"]<=dims["viewport"],dims
        shot(mobile,"44-mobile-homepage.png")
        assert not js_errors,"Browser JavaScript errors: "+" | ".join(js_errors)
        names=[
          "00-public-homepage.png","01-registration-form.png","02-verification-email-sent.png","03-email-verification-page.png","04-email-verified.png","05-sign-in.png","06-empty-workspace-dashboard.png","07-competitors.png","08-competitor-created.png","09-duplicate-domain-protection.png",
          "10-source-create-form.png","11-source-created.png","12-source-configuration.png","13-source-configuration-saved.png","14-offering-create-form.png","15-catalog-offering-created.png","16-offering-detail-and-history.png","17-catalog-monitoring.png","18-dynamic-field-registry.png","19-dynamic-field-form.png","20-dynamic-field-created.png","21-bulk-import-form.png","22-bulk-imported-catalog.png","23-catalog-import-export-archive.png",
          "24-offering-match-form.png","25-offering-match-created.png","26-first-price-observation.png","27-alert-rule-form.png","28-alert-rule-created.png","29-alert-triggered-and-read.png","30-collection-job-history.png","31-collection-job-detail.png","32-private-target-blocked.png","33-discovered-catalog-item.png","34-discovery-upsert-history.png","35-workspace-settings.png","36-workspace-profile-saved.png","37-password-changed.png","38-password-reset-requested.png","39-password-reset-form.png","40-password-reset-complete.png","41-dashboard-after-reset.png","42-analyst-workspace.png","43-viewer-read-only-workspace.png","44-mobile-homepage.png"]
        missing=[name for name in names if not (SHOTS/name).is_file()]
        assert not missing,"Missing walkthrough screenshots: "+str(missing)
        browser.close(); context.close()
        print(f"Completed Nexora full-stack browser journey; captured {len(names)} screenshots.",flush=True)

if __name__=="__main__": main()

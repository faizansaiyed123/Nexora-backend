"""Focused end-to-end collection test with existing user."""
import asyncio
import httpx

BASE = "http://127.0.0.1:8000"
TOKEN = None
EMAIL = "testuser@nexora.io"
PASSWORD = "SecurePass123!"


async def auth():
    global TOKEN
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{BASE}/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if r.status_code != 200:
            print(f"AUTH FAILED: {r.status_code} {r.text[:300]}")
            return False
        TOKEN = r.json()["access_token"]
    print(f"Token: {TOKEN[:30]}...")
    return True


def hdr():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


async def create_entities():
    async with httpx.AsyncClient(timeout=30) as c:
        comps = (await c.get(f"{BASE}/v1/competitors", headers=hdr())).json()
        if not comps:
            comp = (await c.post(f"{BASE}/v1/competitors", json={"name": "TestCompetitor", "domain": "example.com"}, headers=hdr())).json()
        else:
            comp = comps[0]
        src = (await c.post(f"{BASE}/v1/sources", json={"competitor_id": comp["id"], "name": "TestSource", "base_url": "https://www.example.com", "source_type": "OFFICIAL_STORE"}, headers=hdr())).json()
        off = (await c.post(f"{BASE}/v1/offerings", json={"name": "TestOffering", "offering_type": "PRODUCT", "current_price": 100.00, "currency": "USD", "url": "https://www.example.com/test", "market": "US", "is_monitored": True}, headers=hdr())).json()
        match = (await c.post(f"{BASE}/v1/offering-matches", json={"offering_id": off["id"], "source_id": src["id"], "target_url": "https://www.example.com/test", "match_status": "APPROVED", "confidence_score": 1.0}, headers=hdr())).json()
        return match["id"]


async def run_collection(match_id):
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{BASE}/v1/collections/run?offering_match_id={match_id}", headers=hdr())
        if r.status_code != 200:
            return {"status": "FAILED", "error": r.text[:500], "http_status": r.status_code}
        return r.json()


async def discover(url, max_pages=1):
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{BASE}/v1/discovery/run", json={"website_url": url, "max_pages": max_pages, "default_offering_type": "PRODUCT"}, headers=hdr())
        if r.status_code != 200:
            return {"status": "FAILED", "error": r.text[:500], "http_status": r.status_code}
        return r.json()


async def main():
    if not await auth():
        return
    match_id = await create_entities()
    print(f"\nMatch created: {match_id}")

    print("\n=== TEST 1: example.com (static HTML) ===")
    r = await run_collection(match_id)
    obs = r.get('observation', {})
    attrs = obs.get('extracted_attributes', {})
    print(f"  status={r.get('status')} price={obs.get('price')} currency={obs.get('currency')}")
    print(f"  extraction_status={obs.get('extraction_status')}")
    print(f"  auto_rendered={attrs.get('auto_rendered')} possible_challenge={attrs.get('possible_challenge')} "
          f"collection_engine={attrs.get('collection_engine')}")

    print("\n=== TEST 2: httpbin.org/json (JSON API) ===")
    r2 = await discover("https://httpbin.org/json")
    print(f"  status={r2.get('status')} items={r2.get('total_items_processed')} "
          f"created={r2.get('created_offerings_count')} pages={r2.get('total_pages_crawled')}")
    if r2.get('items'):
        for it in r2['items'][:3]:
            print(f"    - {it.get('name')} | {it.get('price')} {it.get('currency')} | {it.get('action')}")

    print("\n=== TEST 3: api.github.com (JSON API, niche: dev tools) ===")
    r3 = await discover("https://api.github.com")
    print(f"  status={r3.get('status')} items={r3.get('total_items_processed')} "
          f"created={r3.get('created_offerings_count')}")

    print("\n=== TEST 4: github.com/pricing (SaaS pricing page) ===")
    r4 = await discover("https://github.com/pricing", max_pages=1)
    print(f"  status={r4.get('status')} items={r4.get('total_items_processed')} "
          f"created={r4.get('created_offerings_count')} pages={r4.get('total_pages_crawled')}")
    if r4.get('items'):
        for it in r4['items'][:5]:
            print(f"    - {it.get('name')} | {it.get('price')} {it.get('currency')} | {it.get('action')}")

    print("\n=== TEST 5: zalando.de (bot-protected, fashion niche) ===")
    r5 = await discover("https://www.zalando.de", max_pages=1)
    print(f"  status={r5.get('status')} items={r5.get('total_items_processed')} "
          f"created={r5.get('created_offerings_count')}")
    if r5.get('items'):
        for it in r5['items'][:3]:
            print(f"    - {it.get('name')} | {it.get('price')} {it.get('currency')} | {it.get('action')}")


asyncio.run(main())
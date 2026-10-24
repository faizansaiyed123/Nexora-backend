from backend.services.discovery_service import WebsiteDiscoveryService


def test_discovery_accepts_regular_html_and_json() -> None:
    assert WebsiteDiscoveryService._is_discoverable_content("text/html; charset=utf-8", "<html><body>Product</body></html>")
    assert WebsiteDiscoveryService._is_discoverable_content("application/xhtml+xml", "<html/>")
    assert WebsiteDiscoveryService._is_discoverable_content("application/json", '{"name":"Product"}')


def test_discovery_accepts_html_mislabelled_as_plain_text() -> None:
    html = """<!doctype html>
    <html><head><script type="application/ld+json">{"@type":"Product"}</script></head>
    <body><h1>Evidence-backed product</h1></body></html>"""
    assert WebsiteDiscoveryService._is_discoverable_content("text/plain; charset=utf-8", html)


def test_discovery_rejects_unstructured_plain_text_and_binary() -> None:
    assert not WebsiteDiscoveryService._is_discoverable_content("text/plain; charset=utf-8", "just ordinary words")
    assert not WebsiteDiscoveryService._is_discoverable_content("application/octet-stream", "<html><body>Product</body></html>")

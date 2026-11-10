import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="hg-v060-newest-api-test-")
spec = importlib.util.spec_from_file_location("server_newest_api", ROOT / "app" / "web" / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules["server_newest_api"] = server
spec.loader.exec_module(server)

assert not hasattr(server, "_NewestPageParser"), "Recent Updates must not scrape the official HTML page"

calls = []
def fake_edge(method, path, body=None, token=None, route="auto"):
    calls.append((method, path, token))
    if "page=1" in path:
        return {"items": [
            {"id": 1, "title": "older", "updateTime": "2026-11-08 10:00:00"},
            {"id": 2, "title": "newest", "updateTime": "2026-11-10 10:00:00"},
        ]}
    return {"items": []}

server._edge = fake_edge
server._rewrite_images = lambda value: value

result = server.newest(page=1, authorization="Bearer sample-token")
assert [x["id"] for x in result["items"]] == [2, 1]
assert result["meta"]["source"] == "official_api:/api/videos"
assert result["meta"]["sort_mode"] == "update_time_then_listing_time"
assert result["meta"]["page"] == 1
assert result["meta"]["page_count"] == 1
assert calls and all(method == "GET" and path.startswith("/api/videos?") for method, path, token in calls)
assert all(token == "sample-token" for method, path, token in calls)
print("PASS: Recent Updates is sourced from official JSON API and sorted by update time")

# Pagination is applied after the bounded official API scan.
items = [{"id": i, "title": str(i), "updateTime": 1794243577 + i} for i in range(1, 46)]
server.recent = lambda limit, authorization=None: {
    "items": items[:limit],
    "meta": {"sort_mode": "update_time_then_listing_time", "pages_scanned": 5}
}
page2 = server.newest(page=2, authorization=None)
assert [x["id"] for x in page2["items"]] == list(range(21, 41))
assert page2["meta"]["page_count"] == 3
print("PASS: Recent Updates pagination")

# Verify the app itself only requests its local adapter; no official website page is loaded in an iframe.
html = (ROOT / "app" / "web" / "www" / "index.html").read_text("utf-8")
assert "huangguoai.com/newest" not in html
assert "iframe" not in html.lower()
assert "官方 API 最近更新" in html or "黄果官方 API" in html
print("PASS: independent client UI; no official website embedding or HTML scrape link")

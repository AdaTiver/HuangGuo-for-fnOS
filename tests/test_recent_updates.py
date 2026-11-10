import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="hg-v040-recent-test-")
os.environ.pop("HGHUB_PROXY", None)
spec = importlib.util.spec_from_file_location("server_recent", ROOT / "app" / "web" / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules["server_recent"] = server
spec.loader.exec_module(server)

# Timestamp parsing supports epoch seconds/milliseconds and ISO-style date strings.
assert server._recent_parse_time(1794243577) == 1794243577.0
assert server._recent_parse_time(1794243577000) == 1794243577.0
assert server._recent_parse_time("2026-11-10T12:00:00Z") is not None
assert server._recent_parse_time("not-a-date") is None
print("PASS: recent timestamp parsing")

# Update time has priority; listing/creation time is used only when update time is absent.
a = {"id": 1, "updateTime": "2026-11-09 10:00:00", "createTime": "2026-11-01 10:00:00"}
b = {"id": 2, "createTime": "2026-11-10 10:00:00"}
c = {"id": 3, "update_time": 1794243577}
assert server._recent_sort_info(a)[1] == "update"
assert server._recent_sort_info(b)[1] == "listing"
assert server._recent_sort_info(c)[1] == "update"
print("PASS: update time preferred; listing time fallback")

# The endpoint scans the existing listing route, de-duplicates IDs, and sorts by timestamp.
pages = []
def fake_edge(method, path, token=None):
    pages.append(path)
    if "page=1" in path:
        return {"items": [
            {"id": 11, "title": "older update", "updateTime": "2026-11-08 10:00:00"},
            {"id": 12, "title": "new listing", "createTime": "2026-11-10 10:00:00"},
            {"id": 13, "title": "newest update", "update_time": "2026-11-10 11:00:00"},
        ]}
    return {"items": []}
server._edge = fake_edge
result = server.recent(limit=120, authorization=None)
ids = [x["id"] for x in result["items"]]
assert ids == [13, 12, 11], ids
assert result["meta"]["updated_time_count"] == 2
assert result["meta"]["listing_time_count"] == 1
assert result["meta"]["no_time_count"] == 0
assert len(pages) == 1
print("PASS: recent endpoint ordering, metadata, and early-stop")

# No date fields: preserve upstream ordering and report the fallback clearly.
server._edge = lambda *a, **kw: {"items": [{"id": 21}, {"id": 22}]}
result = server.recent(limit=40, authorization=None)
assert [x["id"] for x in result["items"]] == [21, 22]
assert result["meta"]["sort_mode"] == "upstream_order_no_time_fields"
print("PASS: stable upstream order when no timestamps are supplied")

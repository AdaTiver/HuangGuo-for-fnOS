import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="hg-v060-api-map-test-")
spec = importlib.util.spec_from_file_location("server_api_map", ROOT / "app" / "web" / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules["server_api_map"] = server
spec.loader.exec_module(server)

calls = []
def fake_edge(method, path, body=None, token=None, route="auto"):
    calls.append((method, path, body, token, route))
    if path.startswith("/api/account/loginByPassword"):
        return {"token": "sample-token", "user": {"id": 7}}
    return {"items": [], "video": {"id": 123}}

server._edge = fake_edge
server._rewrite_images = lambda value: value

server.feed(page=2, size=30, authorization="Bearer feed-token")
assert calls[-1][:4] == ("GET", "/api/videos?page=2&size=30", None, "feed-token")

server.category(name="都市", page=3, size=50, authorization="Bearer category-token")
assert calls[-1][:4] == ("GET", "/api/videos/category/%E9%83%BD%E5%B8%82?page=3&size=40", None, "category-token")

server.rank(kind="potential", authorization="Bearer rank-token")
assert calls[-1][:4] == ("GET", "/api/ranks/potential", None, "rank-token")

server.search(q="系统短剧", page=2, size=50, authorization="Bearer search-token")
assert calls[-1][0] == "GET" and calls[-1][1].startswith("/api/search?q=")
assert "page=2" in calls[-1][1] and "size=40" in calls[-1][1] and calls[-1][3] == "search-token"

server.video_detail(video_id=123, authorization="Bearer detail-token")
assert calls[-1][:4] == ("GET", "/api/videos/123", None, "detail-token")

login = server.login({"username": "test-user", "password": "test-password"})
assert calls[-1][:3] == ("POST", "/api/account/loginByPassword", {"username": "test-user", "password": "test-password"})
assert login["token"] == "sample-token"

server.me(authorization="Bearer me-token")
assert calls[-1][:4] == ("GET", "/api/account/me", None, "me-token")

print("PASS: feed/category/rank/search/detail/login/account all map to official API routes")

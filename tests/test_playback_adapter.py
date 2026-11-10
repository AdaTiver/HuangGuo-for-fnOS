import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="hg-v039-test-")
os.environ.pop("HGHUB_PROXY", None)

spec = importlib.util.spec_from_file_location("server", APP / "web" / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules["server"] = server
spec.loader.exec_module(server)
server._media_url = lambda purpose, url, route="auto": f"/media/{purpose}?mock=1&route={route}"

# The default route must be direct and must not inherit any machine proxy.
assert server.APP_VERSION == "0.7.0"
assert server._route(None) == "auto"
assert server._load_proxy_settings()["enabled"] is False
assert server._session_for_route("auto") is server.DIRECT_SESSION
print("PASS: proxy is disabled by default and auto route is direct")

calls = []
def fake_edge(method, path, body=None, token=None, route="auto"):
    calls.append((method, path, body, token, route))
    return {"streams": {
        "main": "https://cdn.example.test/episode/main.m3u8",
        "hevc": "",
        "preview": "https://cdn.example.test/episode/preview.m3u8",
    }}
server._edge = fake_edge
out = server.play(323, 27, None, "auto")
assert out["stream_source"] == "main"
assert out["media_type"] == "hls"
assert calls[-1] == ("POST", "/api/videos/323/episodes/27/playback", {}, None, "auto")
print("PASS: new playback endpoint and streams.main priority")

server._edge = lambda *a, **kw: {"streams": {
    "main": "", "hevc": "https://cdn.example.test/episode/hevc.m3u8",
    "preview": "https://cdn.example.test/episode/preview.m3u8"}}
out = server.play(323, 27, None, "auto")
assert out["stream_source"] == "hevc"
print("PASS: HEVC fallback when main is empty")

server._edge = lambda *a, **kw: {"streams": {
    "main": "", "hevc": "", "preview": "https://cdn.example.test/episode/preview.m3u8"}}
try:
    server.play(323, 27, None, "auto")
    raise AssertionError("preview must not be used as a full episode")
except server.HTTPException as exc:
    assert exc.status_code == 502
    assert "预览流不会作为正片使用" in exc.detail
print("PASS: preview stream is not used as a full episode")

calls.clear()
server._edge = fake_edge
server.play(323, 27, "Bearer test-token", "direct")
assert calls[-1][3:] == ("test-token", "direct")
print("PASS: Bearer token and explicit direct route forwarding")

# HTTP proxy settings are stored persistently; the GET API representation never reveals a password.
settings = server._validate_proxy_settings({
    "enabled": True, "scheme": "http", "host": "127.0.0.1", "port": 7890,
    "username": "demo user", "password": "p@ss:word"
})
server._save_proxy_settings(settings)
public = server._public_proxy_settings()
assert public["enabled"] and public["password_set"] is True
assert "password" not in public
assert server._proxy_url(settings) == "http://demo%20user:p%40ss%3Aword@127.0.0.1:7890"
assert server._session_for_route("auto").proxies["https"] == server._proxy_url(settings)
assert oct(server.SETTINGS_FILE.stat().st_mode & 0o777) == "0o600"
print("PASS: HTTP proxy, credential encoding, and private persistent storage")

# Clearing a stored password works and does not expose the prior secret.
cleared = server._validate_proxy_settings({
    "enabled": False, "scheme": "http", "host": "127.0.0.1", "port": 7890,
    "username": "", "password": "", "clear_password": True
})
assert cleared["password"] == ""
server._save_proxy_settings(cleared)
assert server._public_proxy_settings()["password_set"] is False
print("PASS: saved proxy password can be cleared")

# SOCKS5 is bundled offline and routed through urllib3's supported SOCKS manager.
socks_settings = server._validate_proxy_settings({
    "enabled": True, "scheme": "socks5", "host": "localhost", "port": 7891
})
server._save_proxy_settings(socks_settings)
assert server._proxy_url(socks_settings) == "socks5h://localhost:7891"
import socks
from urllib3.contrib.socks import SOCKSProxyManager
manager = SOCKSProxyManager(server._proxy_url(socks_settings))
assert manager is not None
print("PASS: bundled SOCKS5 transport is importable offline")
assert server._proxy_session().proxies["https"] == "socks5h://localhost:7891"
print("PASS: SOCKS5 proxy session configuration")

# Verify the bundled SOCKS5 shim performs a real local CONNECT handshake.
import socket
import socketserver
import threading

class _SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        c = self.request
        head = c.recv(2)
        if len(head) != 2 or head[0] != 5:
            return
        methods = c.recv(head[1])
        c.sendall(b"\x05\x00")
        req = c.recv(4)
        if len(req) != 4 or req[0] != 5 or req[1] != 1:
            return
        atyp = req[3]
        if atyp == 1:
            c.recv(4)
        elif atyp == 4:
            c.recv(16)
        elif atyp == 3:
            n = c.recv(1)[0]
            c.recv(n)
        c.recv(2)
        c.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")

class _SocksServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

srv = _SocksServer(("127.0.0.1", 0), _SocksHandler)
thread = threading.Thread(target=srv.serve_forever, daemon=True)
thread.start()
sock = socks.create_connection(("example.com", 443), proxy_type=socks.PROXY_TYPE_SOCKS5,
                               proxy_addr="127.0.0.1", proxy_port=srv.server_address[1],
                               proxy_rdns=True, timeout=3)
assert sock.fileno() >= 0
sock.close()
srv.shutdown()
srv.server_close()
print("PASS: SOCKS5 remote-DNS CONNECT handshake")

# Exercise Requests -> urllib3.contrib.socks -> bundled socks.py end-to-end.
class _SocksHttpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        c = self.request
        head = c.recv(2)
        if len(head) != 2:
            return
        c.recv(head[1])
        c.sendall(b"\x05\x00")
        req = c.recv(4)
        atyp = req[3]
        if atyp == 1:
            c.recv(4)
        elif atyp == 4:
            c.recv(16)
        elif atyp == 3:
            n = c.recv(1)[0]
            c.recv(n)
        c.recv(2)
        c.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        c.settimeout(3)
        try:
            c.recv(8192)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        except OSError:
            pass

http_socks = _SocksServer(("127.0.0.1", 0), _SocksHttpHandler)
thread2 = threading.Thread(target=http_socks.serve_forever, daemon=True)
thread2.start()
sess = server.requests.Session()
sess.trust_env = False
proxy = "socks5h://127.0.0.1:" + str(http_socks.server_address[1])
sess.proxies.update({"http": proxy, "https": proxy})
response = sess.get("http://example.test/", timeout=4)
assert response.status_code == 200 and response.text == "OK"
sess.close()
http_socks.shutdown()
http_socks.server_close()
print("PASS: Requests HTTP traffic traverses the bundled SOCKS5 proxy")

# Test the in-app "Test Proxy" backend action against a local HTTP proxy.
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class _HttpProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

http_proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpProxyHandler)
http_proxy_thread = threading.Thread(target=http_proxy_server.serve_forever, daemon=True)
http_proxy_thread.start()
original_upstream = server.UPSTREAM
server.UPSTREAM = "http://test.example"
proxy_test = server.test_proxy_settings({
    "enabled": True, "scheme": "http", "host": "127.0.0.1",
    "port": http_proxy_server.server_address[1], "username": "", "password": ""
})
assert proxy_test["ok"] and proxy_test["status_code"] == 200
server.UPSTREAM = original_upstream
http_proxy_server.shutdown()
http_proxy_server.server_close()
print("PASS: in-app proxy test endpoint reports a successful HTTP proxy")

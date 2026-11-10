from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_VERSION = "0.7.0"
UPSTREAM = os.environ.get("HGHUB_BASE", "https://huangguoai.com").rstrip("/")
SECRET = os.environ.get("HGHUB_LOCAL_SECRET", "").encode() or os.urandom(32)
WWW = os.path.join(os.path.dirname(__file__), "www")
GATEWAY_PREFIX = "/app/huangguo-shortdrama"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Persistent fnOS package data. Proxy is deliberately OFF by default.
DATA_DIR = Path(os.environ.get("TRIM_PKGVAR") or "/tmp/huangguo-shortdrama")
SETTINGS_FILE = DATA_DIR / "settings.json"
_SETTINGS_LOCK = threading.RLock()
_SESSION_LOCK = threading.RLock()
_PROXY_SESSION = None
_PROXY_SESSION_KEY = None

DIRECT_SESSION = requests.Session()
DIRECT_SESSION.trust_env = False


def _default_proxy_settings():
    return {"enabled": False, "scheme": "http", "host": "", "port": 7890,
            "username": "", "password": ""}


def _load_proxy_settings():
    with _SETTINGS_LOCK:
        try:
            value = json.loads(SETTINGS_FILE.read_text("utf-8"))
            if not isinstance(value, dict):
                return _default_proxy_settings()
            out = _default_proxy_settings()
            out.update({k: value.get(k, out[k]) for k in out})
            out["enabled"] = bool(out["enabled"])
            out["scheme"] = "socks5" if str(out["scheme"]).lower() == "socks5" else "http"
            out["host"] = str(out["host"] or "").strip()
            out["username"] = str(out["username"] or "")
            out["password"] = str(out["password"] or "")
            try:
                out["port"] = int(out["port"])
            except (TypeError, ValueError):
                out["port"] = 7890
            return out
        except (OSError, ValueError, TypeError):
            return _default_proxy_settings()


def _validate_proxy_settings(payload: dict, *, keep_password=True):
    old = _load_proxy_settings()
    enabled = bool(payload.get("enabled", False))
    scheme = str(payload.get("scheme", "http")).lower().strip()
    if scheme not in ("http", "socks5"):
        raise HTTPException(status_code=400, detail="代理协议仅支持 HTTP 或 SOCKS5")
    host = str(payload.get("host", "")).strip()
    raw_port = payload.get("port", 7890)
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="代理端口必须是 1–65535 的整数")
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="代理端口必须是 1–65535 的整数")
    if host:
        if any(ch.isspace() for ch in host) or any(ch in host for ch in "/\\@?#"):
            raise HTTPException(status_code=400, detail="代理地址只填写 IP 或域名，不要填写协议、路径或端口")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                raise HTTPException(status_code=400, detail="代理服务器地址格式不正确")
            if len(host) > 253 or not re.fullmatch(r"(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*", host):
                raise HTTPException(status_code=400, detail="代理服务器地址格式不正确")
    if enabled and not host:
        raise HTTPException(status_code=400, detail="启用代理前请填写服务器 IP 或域名")
    username = str(payload.get("username", "") or "")
    if len(username) > 255:
        raise HTTPException(status_code=400, detail="代理用户名过长")
    password = str(payload.get("password", "") or "")
    if payload.get("clear_password"):
        password = ""
    elif not password and keep_password and old.get("password"):
        password = old["password"]
    if len(password) > 255:
        raise HTTPException(status_code=400, detail="代理密码过长")
    if enabled and ((username and not password) or (password and not username)):
        raise HTTPException(status_code=400, detail="代理用户名和密码需要同时填写")
    return {"enabled": enabled, "scheme": scheme, "host": host, "port": port,
            "username": username, "password": password}


def _save_proxy_settings(settings: dict):
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    with _SETTINGS_LOCK:
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=str(DATA_DIR))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_FILE)
            try:
                os.chmod(SETTINGS_FILE, 0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    _reset_proxy_session()


def _proxy_url(settings: dict) -> str:
    from urllib.parse import quote as _quote
    host = settings["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    auth = ""
    if settings.get("username"):
        auth = _quote(settings["username"], safe="") + ":" + _quote(settings.get("password", ""), safe="") + "@"
    scheme = "socks5h" if settings["scheme"] == "socks5" else "http"
    return f"{scheme}://{auth}{host}:{int(settings['port'])}"


def _reset_proxy_session():
    global _PROXY_SESSION, _PROXY_SESSION_KEY
    with _SESSION_LOCK:
        if _PROXY_SESSION is not None:
            _PROXY_SESSION.close()
        _PROXY_SESSION = None
        _PROXY_SESSION_KEY = None


def _proxy_session():
    global _PROXY_SESSION, _PROXY_SESSION_KEY
    settings = _load_proxy_settings()
    if not settings["enabled"]:
        raise HTTPException(status_code=400, detail="尚未在网络设置中启用代理")
    key = (settings["scheme"], settings["host"], settings["port"],
           settings["username"], settings["password"])
    with _SESSION_LOCK:
        if _PROXY_SESSION is None or _PROXY_SESSION_KEY != key:
            if settings["scheme"] == "socks5":
                import socks  # noqa: F401
            session = requests.Session()
            session.trust_env = False
            proxy_url = _proxy_url(settings)
            session.proxies.update({"http": proxy_url, "https": proxy_url})
            _PROXY_SESSION = session
            _PROXY_SESSION_KEY = key
        return _PROXY_SESSION


def _route(value: str | None) -> str:
    if value in (None, "", "auto"):
        return "auto"
    if value in ("direct", "proxy"):
        return value
    raise HTTPException(status_code=400, detail="线路参数无效")


def _uses_proxy(route="auto") -> bool:
    route = _route(route)
    return route != "direct" and bool(_load_proxy_settings()["enabled"])


def _session_for_route(route="auto"):
    route = _route(route)
    if route == "direct":
        return DIRECT_SESSION
    if route == "proxy":
        return _proxy_session()
    return _proxy_session() if _load_proxy_settings()["enabled"] else DIRECT_SESSION


def _public_proxy_settings(settings=None):
    settings = settings or _load_proxy_settings()
    return {"enabled": bool(settings["enabled"]), "scheme": settings["scheme"],
            "host": settings["host"], "port": settings["port"],
            "username": settings["username"], "password_set": bool(settings["password"])}


app = FastAPI(title="黄果短剧 fnOS 独立客户端", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WWW), name="static")


def _public_https(raw: str, route='auto') -> str:
    try:
        u = urlsplit(raw.strip())
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            raise ValueError("仅允许 HTTPS 公网地址")
        if u.port not in (None, 443):
            raise ValueError("不允许非标准 HTTPS 端口")
        host = u.hostname.encode("idna").decode("ascii")
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None:
            if not literal_ip.is_global:
                raise ValueError("目标地址不是公网地址")
        elif _uses_proxy(route):
            # In proxy mode, local DNS may intentionally return Mihomo Fake-IP ranges.
            # The proxy resolves the hostname; reject obvious local-only names here.
            if "." not in host or host.endswith((".local", ".lan", ".internal", ".localhost")):
                raise ValueError("不允许访问本地域名")
        else:
            infos = socket.getaddrinfo(host, u.port or 443, type=socket.SOCK_STREAM)
            addrs = [ipaddress.ip_address(x[4][0]) for x in infos]
            if not addrs or any(not ip.is_global for ip in addrs):
                raise ValueError("目标地址不是公网地址")
        return u.geturl()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"上游地址校验失败：{exc}")


def _request(method: str, url: str, *, headers: dict | None = None, json_body=None,
             data=None, stream=False, timeout=25, allow_redirects=True, route="auto"):
    route = _route(route)
    current = _public_media(url, route) if (urlsplit(url).scheme == "http") else _public_https(url, route)
    for hop in range(6):
        try:
            client = _session_for_route(route)
            resp = client.request(
                method, current, headers=headers or {}, json=json_body, data=data,
                timeout=timeout, stream=stream, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail="连接黄果上游失败（" + type(exc).__name__ + "），请检查网络或代理设置")
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("Location")
        resp.close()
        if not allow_redirects or not location or hop == 5:
            raise HTTPException(status_code=502, detail="上游重定向失败")
        nxt = urljoin(current, location)
        current = _public_media(nxt, route) if (urlsplit(nxt).scheme == "http") else _public_https(nxt, route)
    raise HTTPException(status_code=502, detail="上游重定向次数过多")


def _edge(method: str, path: str, body=None, token: str | None = None, route="auto"):
    envelope = {
        "method": method.upper(),
        "path": path,
        "body": base64.b64encode(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()).decode() if body is not None else "",
        "content_type": "application/json" if body is not None else "",
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": UPSTREAM + "/",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    resp = _request("POST", UPSTREAM + "/api/edge/request", headers=headers,
                    json_body=envelope, timeout=25, allow_redirects=False, route=route)
    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"上游返回非 JSON（HTTP {resp.status_code}）")
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="上游拒绝访问，请检查登录状态或稍后重试")
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"上游 HTTP {resp.status_code}")
    if payload.get("status") == 1:
        return payload.get("data")
    raise HTTPException(status_code=502, detail=str(payload.get("msg") or "黄果接口返回失败")[:180])


def _public_media(raw: str, route='auto') -> str:
    # CDN playlists can be public HTTP URLs; disallow private IPs and nonstandard ports.
    u = urlsplit(raw.strip())
    if u.scheme == "https":
        return _public_https(raw, route)
    if u.scheme != "http" or not u.hostname or u.username or u.password or u.port not in (None, 80):
        raise HTTPException(status_code=400, detail="媒体只允许公网 HTTP/HTTPS 地址")
    host = u.hostname.encode("idna").decode("ascii")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        raise HTTPException(status_code=400, detail="媒体不得访问本地或内网 IP")
    if addr is None and ("." not in host or host.endswith((".local", ".lan", ".internal", ".localhost"))):
        raise HTTPException(status_code=400, detail="媒体不得访问本地域名")
    if not _uses_proxy(route) and addr is None:
        try:
            if any(not ipaddress.ip_address(info[4][0]).is_global for info in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)):
                raise HTTPException(status_code=400, detail="媒体目标解析到私有网络")
        except OSError:
            raise HTTPException(status_code=400, detail="媒体域名无法解析")
    return raw.strip()

def _sig(purpose: str, url: str, route="auto") -> str:
    route = _route(route)
    return hmac.new(SECRET, (purpose + "\n" + route + "\n" + url).encode(), hashlib.sha256).hexdigest()


def _media_url(purpose: str, url: str, route="auto") -> str:
    route = _route(route)
    url = _public_media(url, route)
    return f"{GATEWAY_PREFIX}/media/{purpose}?u={quote(url, safe='')}&route={route}&sig={_sig(purpose, url, route)}"


def _verify_media(purpose: str, url: str, sig: str, route="auto"):
    route = _route(route)
    url = _public_media(url, route)
    if not hmac.compare_digest(_sig(purpose, url, route), sig or ""):
        raise HTTPException(status_code=403, detail="媒体链接签名无效或已失效")
    return url

IMAGE_KEYS = ("cover", "image", "thumb", "poster", "avatar", "banner", "pic")

def _rewrite_images(value, key=""):
    if isinstance(value, dict):
        return {k: _rewrite_images(v, k.lower()) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_images(v, key) for v in value]
    if isinstance(value, str) and value.startswith("https://") and any(x in key for x in IMAGE_KEYS):
        try:
            return _media_url("img", value)
        except HTTPException:
            return ""
    return value


def _token(authorization: str | None):
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _hls_rewrite(source_url: str, text: str, route="auto") -> str:
    out = []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.rstrip("\r")
        if not line:
            out.append(line)
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-CONTENT-STEERING:"):
                continue
            if line.startswith("#EXT-X-DATERANGE:"):
                # Keep timing metadata, remove externally-referenced ad resources.
                attrs = re.sub(r',?\s*(?:X-ASSET-URI|X-ASSET-LIST)="[^"]*"', "", line)
                out.append(attrs)
                continue
            def repl(m):
                target = urljoin(source_url, m.group(1))
                return 'URI="' + _media_url("seg", target, route) + '"'
            out.append(re.sub(r'URI="([^"]+)"', repl, line))
        else:
            out.append(_media_url("seg", urljoin(source_url, line.strip()), route))
    return "\n".join(out) + "\n"


def _decrypt_cover(data: bytes) -> tuple[bytes, str]:
    if data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")):
        mime = "image/jpeg" if data.startswith(b"\xff\xd8") else "image/png" if data.startswith(b"\x89PNG") else "image/gif" if data.startswith(b"GIF8") else "image/webp"
        return data, mime
    # Compatibility with the upstream HGhub client. Do not expose these values in UI/logs.
    key = b"f5d965df75336270"
    iv = b"97b60394abc2fbe1"
    if not data or len(data) % 16:
        raise HTTPException(status_code=502, detail="封面数据格式不正确")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    try:
        plain = unpadder.update(plain) + unpadder.finalize()
    except ValueError:
        raise HTTPException(status_code=502, detail="封面数据无法解码")
    if plain.startswith(b"\xff\xd8"):
        return plain, "image/jpeg"
    if plain.startswith(b"\x89PNG"):
        return plain, "image/png"
    if plain.startswith(b"GIF8"):
        return plain, "image/gif"
    if plain.startswith(b"RIFF") and plain[8:12] == b"WEBP":
        return plain, "image/webp"
    raise HTTPException(status_code=502, detail="无法识别封面格式")


@app.get("/")
def index():
    return FileResponse(os.path.join(WWW, "index.html"))


# Serve the HLS.js browser player from this NAS instead of forcing each
# browser/phone to reach an overseas CDN. The NAS obtains one copy through
# its configured proxy, and retains that copy in app data for future launches.
# No user-provided URL can be fetched by this endpoint.
_HLS_LIB_URLS = (
    "https://cdn.jsdelivr.net/npm/hls.js@1.6.19/dist/hls.min.js",
    "https://unpkg.com/hls.js@1.6.19/dist/hls.min.js",
)
_HLS_LIB_LOCK = threading.Lock()


def _hls_library_bytes() -> bytes:
    cache_dir = Path(os.environ.get("TRIM_PKGVAR") or "/tmp/huangguo-shortdrama")
    filename = cache_dir / "hls-1.6.19.min.js"
    with _HLS_LIB_LOCK:
        if filename.is_file():
            data = filename.read_bytes()
            if 20_000 < len(data) < 2_000_000 and b"<html" not in data[:300].lower():
                return data
        failures = []
        for url in _HLS_LIB_URLS:
            try:
                response = _request("GET", url, timeout=20, route="auto", stream=True)
                try:
                    if not response.ok:
                        failures.append("CDN HTTP " + str(response.status_code))
                        continue
                    buf = bytearray()
                    for part in response.iter_content(chunk_size=65536):
                        buf.extend(part)
                        if len(buf) > 1_900_000:
                            raise ValueError("播放器组件超过大小限制")
                    data = bytes(buf)
                    if len(data) <= 20_000 or b"<html" in data[:300].lower():
                        raise ValueError("CDN 未返回有效播放器代码")
                    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    # Atomic write: never cache a partial download.
                    with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=".hls-", delete=False) as f:
                        f.write(data)
                        part_path = Path(f.name)
                    part_path.replace(filename)
                    return data
                finally:
                    response.close()
            except (HTTPException, requests.RequestException, OSError, ValueError) as exc:
                # Keep logs generic: don't disclose proxy credentials or URLs.
                failures.append(type(exc).__name__)
        raise HTTPException(status_code=502, detail="NAS 未能获取播放器组件，请检查网络或代理设置。" + ("/".join(failures[:2]) if failures else ""))


@app.get("/vendor/hls.min.js", include_in_schema=False)
def local_hls_library():
    return Response(_hls_library_bytes(), media_type="application/javascript", headers={
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/settings/proxy")
def get_proxy_settings():
    return _public_proxy_settings()


@app.post("/api/settings/proxy")
def save_proxy_settings(payload: dict):
    settings = _validate_proxy_settings(payload, keep_password=True)
    _save_proxy_settings(settings)
    return {"ok": True, "settings": _public_proxy_settings(settings)}


@app.post("/api/settings/proxy/test")
def test_proxy_settings(payload: dict):
    settings = _validate_proxy_settings({**payload, "enabled": True}, keep_password=True)
    if not settings["host"]:
        raise HTTPException(status_code=400, detail="请先填写代理服务器地址")
    session = requests.Session()
    session.trust_env = False
    session.proxies.update({"http": _proxy_url(settings), "https": _proxy_url(settings)})
    started = time.monotonic()
    try:
        response = session.get(UPSTREAM + "/", headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                              timeout=(8, 15), allow_redirects=True, stream=True)
        status = response.status_code
        response.close()
        if status == 407:
            raise HTTPException(status_code=502, detail="代理要求身份验证，请检查用户名和密码")
        if status >= 500:
            raise HTTPException(status_code=502, detail=f"代理已连接，但目标网站返回 HTTP {status}")
        elapsed = round((time.monotonic() - started) * 1000)
        return {"ok": True, "status_code": status, "elapsed_ms": elapsed,
                "message": f"代理连接成功（HTTP {status}，{elapsed} ms）"}
    except HTTPException:
        raise
    except Exception as exc:
        # Do not include exception text: it may contain a credential-bearing proxy URL.
        raise HTTPException(status_code=502, detail="代理连接失败（" + type(exc).__name__ + "），请检查地址、端口、协议和认证信息")
    finally:
        session.close()


@app.get("/api/health")
def health():
    return {"ok": True, "app": "huangguo-shortdrama", "version": APP_VERSION, "upstream": "configured"}

@app.get("/api/boot")
def boot():
    return {"version": APP_VERSION, "upstream_name": "黄果短剧", "guest_mode": True}

@app.get("/api/feed")
def feed(page: int = 1, size: int = 20, authorization: str | None = Header(default=None)):
    return _rewrite_images(_edge("GET", f"/api/videos?page={max(1,page)}&size={min(max(1,size),40)}", token=_token(authorization)))


# Recent-updates view: collect a bounded window from the same documented video
# listing endpoint, then sort by update time; if absent, use listing/creation time.
_RECENT_ID_KEYS = ("id", "video_id", "videoId", "vid", "vod_id", "spu_id")
_RECENT_UPDATE_KEYS = (
    "update_time", "updateTime", "updated_at", "updatedAt", "last_update_time",
    "lastUpdateTime", "last_episode_update_time", "lastEpisodeUpdateTime",
    "latest_update_time", "latestUpdateTime", "latest_episode_time",
    "latestEpisodeTime", "modify_time", "modifyTime", "modified_at", "modifiedAt",
    "update_timestamp", "updateTimestamp", "lastUpdate", "updateAt", "update_date",
    "updateDate", "last_update", "lastUpdate",
)
_RECENT_LISTING_KEYS = (
    "create_time", "createTime", "created_at", "createdAt", "publish_time",
    "publishTime", "release_time", "releaseTime", "online_time", "onlineTime",
    "listing_time", "listingTime", "add_time", "addTime", "publish_date", "publishDate",
)


def _recent_find(obj, keys):
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return value
        for value in obj.values():
            found = _recent_find(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _recent_find(value, keys)
            if found not in (None, ""):
                return found
    return None


def _recent_items(payload):
    """Find a video list in the upstream response without treating episodes as videos."""
    if isinstance(payload, list):
        candidates = [x for x in payload if isinstance(x, dict) and _recent_find(x, _RECENT_ID_KEYS) is not None]
        if candidates:
            return candidates
        for value in payload:
            found = _recent_items(value)
            if found:
                return found
    elif isinstance(payload, dict):
        for key in ("items", "list", "records", "videos"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = [x for x in value if isinstance(x, dict) and _recent_find(x, _RECENT_ID_KEYS) is not None]
                if candidates:
                    return candidates
        for key in ("data", "result", "results", "payload"):
            if key in payload:
                found = _recent_items(payload[key])
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _recent_items(value)
                if found:
                    return found
    return []


def _recent_parse_time(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 100_000_000_000:
            number /= 1000.0
        # Reject small counters such as episode counts.
        return number if number >= 500_000_000 else None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{9,16}(?:\.\d+)?", raw):
        try:
            return _recent_parse_time(float(raw))
        except ValueError:
            return None
    normal = raw.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    normal = re.sub(r"\s+", " ", normal)
    if normal.endswith("Z"):
        normal = normal[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normal)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
            try:
                parsed = datetime.strptime(normal, fmt)
                break
            except ValueError:
                pass
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _recent_sort_info(item):
    updated = _recent_parse_time(_recent_find(item, _RECENT_UPDATE_KEYS))
    if updated is not None:
        return updated, "update"
    listed = _recent_parse_time(_recent_find(item, _RECENT_LISTING_KEYS))
    if listed is not None:
        return listed, "listing"
    return None, "none"


@app.get("/api/recent")
def recent(limit: int = Query(default=120, ge=1, le=200), authorization: str | None = Header(default=None)):
    # Use the existing public video listing API rather than inventing a new upstream route.
    # Up to five pages (200 records) are scanned to produce a useful recent-updates view.
    token = _token(authorization)
    pages_to_scan = min(5, max(1, (limit + 39) // 40))
    collected = []
    seen = set()
    pages_scanned = 0
    for page_no in range(1, pages_to_scan + 1):
        payload = _edge("GET", f"/api/videos?page={page_no}&size=40", token=token)
        batch = _recent_items(payload)
        pages_scanned += 1
        if not batch:
            break
        for item in batch:
            item_id = _recent_find(item, _RECENT_ID_KEYS)
            dedupe_key = str(item_id) if item_id is not None else None
            if dedupe_key is not None and dedupe_key in seen:
                continue
            if dedupe_key is not None:
                seen.add(dedupe_key)
            collected.append(item)
        if len(batch) < 40:
            break
    decorated = []
    updated_count = 0
    listing_count = 0
    no_time_count = 0
    for index, item in enumerate(collected):
        stamp, source = _recent_sort_info(item)
        if source == "update":
            updated_count += 1
        elif source == "listing":
            listing_count += 1
        else:
            no_time_count += 1
        # Stable sort preserves upstream ordering for entries with no parseable date.
        decorated.append((stamp is not None, stamp if stamp is not None else float("-inf"), -index, item))
    if updated_count or listing_count:
        decorated.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        sort_mode = "update_time_then_listing_time"
    else:
        sort_mode = "upstream_order_no_time_fields"
    items = [row[3] for row in decorated[:limit]]
    return _rewrite_images({
        "items": items,
        "meta": {
            "pages_scanned": pages_scanned,
            "records_scanned": len(collected),
            "updated_time_count": updated_count,
            "listing_time_count": listing_count,
            "no_time_count": no_time_count,
            "sort_mode": sort_mode,
        },
    })


@app.get("/api/newest")
def newest(page: int = Query(default=1, ge=1, le=10),
           authorization: str | None = Header(default=None)):
    """Build Recent Updates only from the official JSON video-listing API.

    No official HTML page is scraped. We scan a bounded set of official
    /api/videos pages, sort by update time (listing time fallback), then page
    the sorted result for the client.
    """
    page_size = 20
    collected = recent(limit=200, authorization=authorization)
    all_items = collected.get("items", []) if isinstance(collected, dict) else []
    meta = collected.get("meta", {}) if isinstance(collected, dict) else {}
    page_count = max(1, (len(all_items) + page_size - 1) // page_size)
    current_page = min(max(1, page), page_count)
    start_index = (current_page - 1) * page_size
    page_items = all_items[start_index:start_index + page_size]
    return {
        "items": page_items,
        "meta": {
            "source": "official_api:/api/videos",
            "page": current_page,
            "page_size": page_size,
            "page_count": page_count,
            "scanned_count": len(all_items),
            "scan_limit": 200,
            "has_more_upstream": len(all_items) >= 200,
            "sort_mode": meta.get("sort_mode", "upstream_order_no_time_fields"),
            "updated_time_count": meta.get("updated_time_count", 0),
            "listing_time_count": meta.get("listing_time_count", 0),
            "no_time_count": meta.get("no_time_count", 0),
            "pages_scanned": meta.get("pages_scanned", 0),
        },
    }


@app.get("/api/category")
def category(name: str = Query(...), page: int = 1, size: int = 20, authorization: str | None = Header(default=None)):
    return _rewrite_images(_edge("GET", f"/api/videos/category/{quote(name, safe='')}?page={max(1,page)}&size={min(max(1,size),40)}", token=_token(authorization)))

@app.get("/api/rank")
def rank(kind: str = "hot", authorization: str | None = Header(default=None)):
    kind = kind if kind in ("hot", "recommend", "potential") else "hot"
    return _rewrite_images(_edge("GET", f"/api/ranks/{kind}", token=_token(authorization)))

@app.get("/api/search")
def search(q: str, page: int = 1, size: int = 20, authorization: str | None = Header(default=None)):
    from urllib.parse import urlencode
    query = urlencode({"q": q, "page": max(1, page), "size": min(max(1, size), 40)})
    return _rewrite_images(_edge("GET", f"/api/search?{query}", token=_token(authorization)))

@app.get("/api/video/{video_id}")
def video_detail(video_id: int, authorization: str | None = Header(default=None)):
    return _rewrite_images(_edge("GET", f"/api/videos/{video_id}", token=_token(authorization)))

# Interpret the documented playback response without disclosing signed playback URLs.
# The upstream client plays `video_url` as HLS even when no .m3u8 suffix exists.
_MEDIA_KEYS = ("video_url", "play_url", "videoUrl", "playUrl", "m3u8_url", "m3u8Url", "hls_url", "hlsUrl", "stream_url", "streamUrl", "video_url_hevc", "src", "url", "source")
_MEDIA_WRAPPERS = ("data", "result", "play", "video", "playback", "stream", "streams", "sources", "urls", "links", "episode", "episodes", "info")

def _media_candidate(value, key=""):
    # Never decode encrypted / signed data, assume unknown string is not an URL.
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("https://") or raw.startswith("http://"):
        # A generic `url` may point to a webpage, only accept recognized media extensions.
        if key in ("url", "src", "source") and not (".m3u8" in raw.lower() or ".mp4" in raw.lower()):
            return None
        return raw
    return None

def _find_play_url(value, depth=0):
    if depth > 6:
        return None
    if isinstance(value, list):
        for child in value[:12]:
            found = _find_play_url(child, depth + 1)
            if found: return found
    elif isinstance(value, dict):
        for key in _MEDIA_KEYS:
            found = _media_candidate(value.get(key), key)
            if found: return found
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                found = _find_play_url(nested, depth + 1)
                if found: return found
        for key in _MEDIA_WRAPPERS:
            if key in value:
                found = _find_play_url(value[key], depth + 1)
                if found: return found
    elif isinstance(value, str):
        return _media_candidate(value, "url")
    return None

def _safe_value_kind(v):
    # Only disclose type and protocol category; never content, host, token, path, key or IV.
    if v is None: return "null"
    if isinstance(v, bool): return "boolean"
    if isinstance(v, (int, float)): return "number"
    if isinstance(v, dict): return "object"
    if isinstance(v, list): return "array"
    if not isinstance(v, str): return type(v).__name__
    v = v.strip()
    if not v: return "empty"
    if v.startswith("https://"): return "https"
    if v.startswith("http://"): return "http"
    if v.startswith("//"): return "protocol-relative"
    if v.startswith("/"): return "relative-path"
    return "opaque-text"

def _play_shape(data):
    if not isinstance(data, dict):
        return "result=" + _safe_value_kind(data)
    # Privacy: only field presence + URL scheme, not values or full response.
    keys = ("video_url", "video_url_hevc", "video_api", "play_url", "data")
    return ", ".join(k + "=" + _safe_value_kind(data[k]) for k in keys if k in data) or "no common playback fields"

def _stream_url(value):
    """Accept only an explicit HTTP(S) URL from the documented streams object."""
    if isinstance(value, dict):
        for key in ("url", "src", "play_url", "video_url", "m3u8_url"):
            candidate = _stream_url(value.get(key))
            if candidate:
                return candidate
        return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith(("https://", "http://")):
        return raw
    return None


def _stream_state(value):
    """Return a safe diagnostic category; never expose the media URL itself."""
    if value is None:
        return "null"
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return "empty"
        if value.startswith("https://"):
            return "https"
        if value.startswith("http://"):
            return "http"
        if value.startswith("//"):
            return "protocol-relative"
        return "opaque"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


@app.get("/api/play/{video_id}")
def play(video_id: int, ep: int = 1, authorization: str | None = Header(default=None),
         x_hg_route: str | None = Header(default=None)):
    route = _route(x_hg_route)
    episode = max(1, ep)

    # HGhub's current playback endpoint is POST /episodes/{ep}/playback.
    # The JSON body is an empty object (base64 "e30=" inside /api/edge/request).
    # The legacy /play?ep= endpoint now returns empty video_url fields, so it
    # must not be treated as the primary source of media URLs.
    playback_path = f"/api/videos/{video_id}/episodes/{episode}/playback"
    data = _edge("POST", playback_path, body={}, token=_token(authorization), route=route)
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="新版播放接口返回格式异常（data 不是对象）")

    streams = data.get("streams") if isinstance(data.get("streams"), dict) else {}
    main_url = _stream_url(streams.get("main"))
    hevc_url = _stream_url(streams.get("hevc"))

    # Prefer the normal main stream. Use HEVC only if main is unavailable.
    # Preview is intentionally never used as a full-episode fallback.
    if main_url:
        url, selected_stream = main_url, "main"
    elif hevc_url:
        url, selected_stream = hevc_url, "hevc"
    else:
        raise HTTPException(
            status_code=502,
            detail=("新版播放接口未返回可用正片地址；短剧ID=" + str(video_id) +
                    "；请求集数=" + str(episode) +
                    "；接口=POST " + playback_path +
                    "；会话=" + ("已提供令牌" if _token(authorization) else "游客") +
                    "；streams.main=" + _stream_state(streams.get("main")) +
                    "，streams.hevc=" + _stream_state(streams.get("hevc")) +
                    "，streams.preview=" + _stream_state(streams.get("preview")) +
                    "（预览流不会作为正片使用）。")
        )

    # HLS URLs frequently have no .m3u8 suffix; retain the existing HLS
    # playlist/segment proxy so phones and browsers can play through the NAS.
    is_mp4 = ".mp4" in urlsplit(url).path.lower()
    try:
        proxied = _media_url("seg" if is_mp4 else "hls", url, route)
    except HTTPException as exc:
        raise HTTPException(status_code=502, detail="无法代理上游媒体（" + _safe_value_kind(url) + "）：「" + str(exc.detail) + "」")
    return {
        "video_url": proxied,
        "media_type": "file" if is_mp4 else "hls",
        "route": route,
        "stream_source": selected_stream,
        "episode": episode,
    }

@app.post("/api/login")
def login(payload: dict):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入账号和密码")
    data = _edge("POST", "/api/account/loginByPassword", {"username": username, "password": password})
    if not isinstance(data, dict) or not data.get("token"):
        raise HTTPException(status_code=502, detail="登录返回缺少会话令牌")
    return {"token": data["token"], "user": data.get("user")}

@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    token = _token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="尚未登录")
    return _edge("GET", "/api/account/me", token=token)

@app.post("/api/logout")
def logout(authorization: str | None = Header(default=None)):
    token = _token(authorization)
    if token:
        try:
            _edge("POST", "/api/account/logout", token=token)
        except HTTPException:
            pass
    return {"ok": True}

@app.get("/media/img")
def media_image(u: str, sig: str):
    target = _verify_media("img", u, sig)
    resp = _request("GET", target, headers={"User-Agent": UA, "Referer": UPSTREAM + "/", "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}, timeout=20, stream=False)
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"封面请求失败（HTTP {resp.status_code}）")
    if len(resp.content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="封面文件过大")
    data, mime = _decrypt_cover(resp.content)
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

@app.get("/media/hls")
def media_hls(u: str, sig: str, route: str = "auto"):
    target = _verify_media("hls", u, sig, route)
    resp = _request("GET", target, headers={"User-Agent": UA, "Referer": UPSTREAM + "/", "Accept": "*/*"}, timeout=25, stream=False, route=route)
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"播放列表请求失败（HTTP {resp.status_code}）")
    if len(resp.content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="播放列表过大")
    text = resp.content.decode("utf-8-sig", errors="replace")
    if not text.lstrip().startswith("#EXTM3U"):
        raise HTTPException(status_code=502, detail="上游未返回有效 HLS 播放列表")
    return Response(_hls_rewrite(target, text, route), media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

@app.get("/media/seg")
def media_segment(request: Request, u: str, sig: str, route: str = "auto"):
    target = _verify_media("seg", u, sig, route)
    headers = {"User-Agent": UA, "Referer": UPSTREAM + "/", "Accept": "*/*"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    resp = _request("GET", target, headers=headers, timeout=40, stream=True, route=route)
    if not resp.ok and resp.status_code != 206:
        resp.close()
        raise HTTPException(status_code=502, detail=f"媒体分片请求失败（HTTP {resp.status_code}）")
    ctype = resp.headers.get("Content-Type", "application/octet-stream")
    first = next(resp.iter_content(chunk_size=4096), b"")
    if "mpegurl" in ctype.lower() or first.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U"):
        rest = b"".join(resp.iter_content(chunk_size=65536))
        resp.close()
        text = (first + rest).decode("utf-8-sig", errors="replace")
        return Response(_hls_rewrite(target, text, route), media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    def stream_body():
        try:
            if first:
                yield first
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk
        finally:
            resp.close()
    out_headers = {"Cache-Control": "no-store", "Accept-Ranges": resp.headers.get("Accept-Ranges", "bytes"), "X-Content-Type-Options": "nosniff"}
    for key in ("Content-Length", "Content-Range"):
        if key in resp.headers:
            out_headers[key] = resp.headers[key]
    return StreamingResponse(stream_body(), status_code=resp.status_code, media_type=ctype, headers=out_headers)


class GatewayPathApp:
    """Only serve routes under fnOS's authenticated gateway prefix.

    fnOS routes requests to our Unix socket with /app/huangguo-shortdrama
    still attached. Strip it for internal FastAPI routing; never open root
    paths on the gateway socket. Authentication is performed by fnOS.
    """
    def __init__(self, application, prefix=GATEWAY_PREFIX):
        self.application = application
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.application(scope, receive, send)
        path = scope.get("path", "")
        if path == self.prefix:
            path = "/"
        elif path.startswith(self.prefix + "/"):
            path = path[len(self.prefix):]
        else:
            if scope["type"] == "http":
                from starlette.responses import PlainTextResponse
                return await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            await send({"type": "websocket.close", "code": 1008})
            return
        adjusted = dict(scope)
        adjusted["path"] = path
        adjusted["raw_path"] = path.encode("utf-8")
        adjusted["root_path"] = ""
        return await self.application(adjusted, receive, send)


gateway_app = GatewayPathApp(app)

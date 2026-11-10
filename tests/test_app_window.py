import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
config = json.loads((ROOT / "app" / "ui" / "config").read_text("utf-8"))
entries = config.get(".url", {})
assert list(entries) == ["huangguo.shortdrama.main"], entries
assert entries["huangguo.shortdrama.main"]["type"] == "iframe"
assert entries["huangguo.shortdrama.main"]["url"] == "/app/huangguo-shortdrama/"
assert entries["huangguo.shortdrama.main"]["gatewayPrefix"] == "/app/huangguo-shortdrama"
assert entries["huangguo.shortdrama.main"]["gatewaySocket"] == "app.sock"
assert "port" not in entries["huangguo.shortdrama.main"]

html = (ROOT / "app" / "web" / "www" / "index.html").read_text("utf-8")
assert "officialDirectBtn" not in html
assert 'id="officialBtn"' not in html
assert 'target="_blank"' not in html
assert ".top{\n  display:grid;" in html
assert "width:100vw" in html
print("PASS: single fnOS iframe app entry; no external official-site shortcut; continuous grid header")

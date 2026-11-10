"""In-process ASGI smoke tests for fnOS gateway routing."""
import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ['TRIM_PKGVAR'] = tempfile.mkdtemp(prefix='hgv070-gateway-')
spec = importlib.util.spec_from_file_location('hgv_gateway', ROOT / 'app/web/server.py')
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)

assert server.APP_VERSION == '0.7.0'
assert server.GATEWAY_PREFIX == '/app/huangguo-shortdrama'

async def get(path):
    responses = []
    scope = {
      'type':'http', 'http_version':'1.1','method':'GET','scheme':'http',
      'path':path,'raw_path':path.encode(),'root_path':'','query_string':b'',
      'headers':[(b'host',b'localhost')], 'server':('localhost', 80), 'client':('127.0.0.1', 12345)
    }
    async def receive():
        return {'type':'http.request','body':b'','more_body':False}
    async def send(message):
        responses.append(message)
    await server.gateway_app(scope,receive,send)
    return next(x['status'] for x in responses if x['type']=='http.response.start'), b''.join(x.get('body',b'') for x in responses if x['type']=='http.response.body')

for route in ['/app/huangguo-shortdrama', '/app/huangguo-shortdrama/', '/app/huangguo-shortdrama/api/health', '/app/huangguo-shortdrama/api/boot']:
    status, body=asyncio.run(get(route))
    assert status==200, (route,status,body[:300])

status,body=asyncio.run(get('/api/health'))
assert status==404
status,body=asyncio.run(get('/app/huangguo-shortdrama-evil/api/health'))
assert status==404
status,body=asyncio.run(get('/app/huangguo-shortdrama/does-not-exist'))
assert status==404

# Real gateway responses use prefixed media URLs, including HLS playlist references.
server._public_media = lambda url,route='auto':url
server._sig = lambda purpose,url,route='auto':'mock-sig'
url=server._media_url('hls', 'https://media.example.test/movie.m3u8')
assert url.startswith('/app/huangguo-shortdrama/media/hls?')
playlist=server._hls_rewrite('https://media.example.test/movie.m3u8', '#EXTM3U\n#EXTINF:5,\nsegment001.ts\n')
assert '/app/huangguo-shortdrama/media/seg?' in playlist

page=(ROOT/'app/web/www/index.html').read_text('utf-8')
assert 'src="/app/huangguo-shortdrama/vendor/hls.min.js"' in page
assert "fetch('/app/huangguo-shortdrama'+path" in page
assert '<iframe' not in page.lower()
print('PASS: UDS gateway prefixes, route isolation, HLS URLs and independent client assets')

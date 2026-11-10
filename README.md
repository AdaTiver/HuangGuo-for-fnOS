# HuangGuo ShortDrama v0.7.0 - fnOS unified gateway

This version uses fnOS unified gateway: **/app/huangguo-shortdrama/**.
The desktop entry has `gatewayPrefix`, `gatewaySocket`, `microApp`, and `type=iframe`; the package manifest sets `micro_app=true`.
The app-local service listens **only** on Unix socket `app.sock` in the installed target directory; it does not listen on TCP port 18787. No Docker and no extra deployed server.

The client UI is independently authored; it is not an iframe embedding the official website. fnOS embeds the *app's own* UI in the desktop app window. The API adapter calls Huangguo official APIs as before. Playback main stream and HEVC fallback, search, history, favorites and proxy settings are preserved. Recent Updates scans the official JSON API (up to 200 items).

**Remote access:** Once the user can open their existing fnOS desktop through an authorized remote channel, the HuangGuo app uses the *same* fnOS domain and login session without exposing its own port. This does not create new WAN access if the NAS itself is unreachable.

**Upgrade:** package app ID remains `huangguo.shortdrama`. Upgrade without uninstalling to preserve package data. Browser-side saved history and favorites remain linked to their origin; switching from direct port to unified gateway changes the browser origin/path, so existing browser localStorage may need to be copied manually. The user should not delete the old data beforehand.

**Known limitation:** This package has been locally verified with a Unix-socket mock of the fnOS route. Real fnOS gateway registration, socket file permissions and remote session handling need verification on a real NAS.

Build: `sh build_fpk.sh /mnt/data/HGV070.fpk`.

Licenses: retain existing `LICENSE`, `LICENSE-HGHUB.txt`, `PYTHON_LICENSE.txt`, `GLIBC_LICENSE.txt`, `THIRD_PARTY_LICENSES.txt`; the HGhub-derived portions are for noncommercial use only.

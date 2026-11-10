#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT=${1:-"$ROOT/HGV070.fpk"}
TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM
mkdir -p "$TMP/pkg"
cp -a "$ROOT/app" "$TMP/app"
cp -a "$ROOT/package/." "$TMP/pkg/"
# Build the payload archive using the exact top-level layout expected by fnOS.
tar -czf "$TMP/pkg/app.tgz" -C "$TMP/app" web ui runtime
MD5=$(md5sum "$TMP/pkg/app.tgz" | awk '{print $1}')
sed "s/^checksum              = .*/checksum              = $MD5/" "$TMP/pkg/manifest.template" > "$TMP/pkg/manifest"
rm -f "$TMP/pkg/manifest.template"
mkdir -p "$(dirname -- "$OUT")"
# FPK is a gzip-compressed outer tar containing app.tgz and package metadata.
tar -cf "$TMP/outer.tar" -C "$TMP/pkg" app.tgz cmd config wizard manifest ICON.PNG ICON_256.PNG LICENSE LICENSE-HGHUB.txt PYTHON_LICENSE.txt GLIBC_LICENSE.txt THIRD_PARTY_LICENSES.txt
gzip -n -c "$TMP/outer.tar" > "$OUT"
echo "Built: $OUT"
echo "app.tgz MD5: $MD5"

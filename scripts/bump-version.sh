#!/usr/bin/env bash
# Bump the app version everywhere it lives: iOS app + share extension (Info.plist) and Android (version.properties).
#   scripts/bump-version.sh 3.8.9 30
# tests/test_android_version.py and the apk workflow fail when iOS and Android differ.
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <version> <build>   e.g. $0 3.8.9 30" >&2; exit 2; }
VER="$1"; BUILD="$2"
cd "$(dirname "$0")/.."
for f in apps/ios/Info.plist apps/ios/Share/Info.plist; do
  [ -f "$f" ] || continue
  python3 - "$f" "$VER" "$BUILD" <<'PY'
import re, sys
f, ver, build = sys.argv[1:]
s = open(f, encoding="utf-8").read()
s = re.sub(r"(<key>CFBundleShortVersionString</key>\s*<string>)[^<]*(</string>)", rf"\g<1>{ver}\g<2>", s)
s = re.sub(r"(<key>CFBundleVersion</key>\s*<string>)[^<]*(</string>)", rf"\g<1>{build}\g<2>", s)
open(f, "w", encoding="utf-8").write(s)
PY
done
python3 - "$VER" "$BUILD" <<'PY'
import re, sys
ver, build = sys.argv[1:]
f = "apps/android/version.properties"
s = open(f, encoding="utf-8").read()
s = re.sub(r"(?m)^versionName=.*$", f"versionName={ver}", s)
s = re.sub(r"(?m)^versionCode=.*$", f"versionCode={build}", s)
open(f, "w", encoding="utf-8").write(s)
PY
echo "version $VER ($BUILD): iOS app + share, Android"

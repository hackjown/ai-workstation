#!/usr/bin/env bash
# Build an unsigned (fakesigned) IPA for TrollStore on Linux or macOS.
# Needs: clang + lld, an iPhoneOS SDK (github.com/theos/sdks), ldid (ProcursusTeam), python3-pil.
#   SDK=/path/iPhoneOS16.5.sdk LDID=/path/ldid ./build-ipa.sh
set -euo pipefail
cd "$(dirname "$0")"
SDK=${SDK:?set SDK=/path/to/iPhoneOS*.sdk}; LDID=${LDID:-ldid}
OUT=build; APP=$OUT/Payload/AIW.app
[ "$(uname)" = Linux ] && LDFLAG=-fuse-ld=lld
rm -rf $OUT && mkdir -p $APP
clang -target arm64-apple-ios14.0 -isysroot "$SDK" -fobjc-arc -O2 ${LDFLAG:-} \
  main.m -framework UIKit -framework WebKit -framework Foundation -framework Speech -framework AVFoundation -framework UserNotifications -o $APP/AIW
cp Info.plist $APP/
python3 - "$APP" <<'PY'
import sys; from PIL import Image
app=sys.argv[1]; src=Image.open('../web/icon-512.png').convert('RGB')
for name,px in [('AppIcon60x60@2x.png',120),('AppIcon60x60@3x.png',180),('AppIcon76x76~ipad.png',76),('AppIcon76x76@2x~ipad.png',152)]:
    src.resize((px,px),Image.LANCZOS).save(f'{app}/{name}')
PY
printf 'APPL????' > $APP/PkgInfo
"$LDID" -Sentitlements.plist $APP/AIW
(cd $OUT && zip -qry ../AIWorkstation.ipa Payload)
echo "OK -> $(pwd)/AIWorkstation.ipa"

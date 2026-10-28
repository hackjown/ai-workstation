#!/usr/bin/env bash
# Build the native SwiftUI app as a fakesigned IPA for TrollStore. Runs on macOS (Xcode command line tools + ldid):
#   ./build-ipa.sh            -> apps/ios/AIWorkstation.ipa
set -euo pipefail
cd "$(dirname "$0")"
LDID=${LDID:-ldid}
SDK=$(xcrun --sdk iphoneos --show-sdk-path)
OUT=build; APP=$OUT/Payload/AIW.app
rm -rf $OUT AIWorkstation.ipa && mkdir -p $APP
xcrun --sdk iphoneos swiftc -target arm64-apple-ios16.0 -sdk "$SDK" -O -parse-as-library -module-name Aiw \
  Sources/*.swift -o $APP/AIW
cp Info.plist $APP/
# 球球 page for the chat header (MateView.swift): same files the web app serves (+ the non-commercial LICENSE/NOTICE)
mkdir -p $APP/mate/vendor/emotionball && cp ../web/mate.html ../web/mate-map.js $APP/mate/ && cp ../web/vendor/emotionball/*.js ../web/vendor/emotionball/LICENSE ../web/vendor/emotionball/NOTICE.md $APP/mate/vendor/emotionball/
# takeover touch page (DesktopView.swift TakeoverWeb): takeover.html + takeover.js, inlined at load
mkdir -p $APP/takeover && cp ../web/takeover.html ../web/takeover.js $APP/takeover/
# file preview page (MarkdownPreview.swift): the web app's renderer (markdown-it + DOMPurify, vendored) and styles
mkdir -p $APP/mdview/vendor/markdown && cp ../web/mdview.html ../web/mdview.js ../web/mdview.css $APP/mdview/ && cp ../web/vendor/markdown/*.js ../web/vendor/markdown/LICENSE-* $APP/mdview/vendor/markdown/
python3 - "$APP" <<'PY'
import sys; from PIL import Image
app=sys.argv[1]; src=Image.open('../web/icon-512.png').convert('RGB')
for name,px in [('AppIcon60x60@2x.png',120),('AppIcon60x60@3x.png',180),('AppIcon76x76~ipad.png',76),('AppIcon76x76@2x~ipad.png',152)]:
    src.resize((px,px),Image.LANCZOS).save(f'{app}/{name}')
PY
printf 'APPL????' > $APP/PkgInfo
# AiwShare share extension (Share/): embedded in PlugIns, bundle id com.hackjown.aiworkstation.share (child of the app's),
# same App Group as the app. If it fails to build, the IPA still ships without it (AIW_SHARE=0 skips it on purpose).
EXT=$APP/PlugIns/AiwShare.appex
build_share() {
  mkdir -p $EXT || return 1
  xcrun --sdk iphoneos swiftc -target arm64-apple-ios16.0 -sdk "$SDK" -O -parse-as-library -application-extension \
    -module-name AiwShare Share/*.swift -o $EXT/AiwShare -Xlinker -e -Xlinker _NSExtensionMain || return 1
  cp Share/Info.plist $EXT/ || return 1
  for k in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$k $(/usr/libexec/PlistBuddy -c "Print :$k" Info.plist)" $EXT/Info.plist || return 1
  done
  "$LDID" -SShare/entitlements.plist $EXT/AiwShare || return 1
}
if [ "${AIW_SHARE:-1}" = "1" ]; then
  if build_share; then echo "AiwShare.appex embedded"; else echo "::warning::AiwShare extension failed to build; the IPA ships without it"; rm -rf $APP/PlugIns; fi
fi
"$LDID" -Sentitlements.plist $APP/AIW
(cd $OUT && zip -qry ../AIWorkstation.ipa Payload)
echo "OK -> $(pwd)/AIWorkstation.ipa"

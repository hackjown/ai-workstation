#!/usr/bin/env bash
# Build the native SwiftUI app as a fakesigned IPA for TrollStore. Runs on macOS (Xcode command line tools + ldid):
#   ./build-ipa.sh            -> apps/ios/AIWorkstation.ipa
set -euo pipefail
cd "$(dirname "$0")"
LDID=${LDID:-ldid}
SDK=$(xcrun --sdk iphoneos --show-sdk-path)
OUT=build; APP=$OUT/Payload/AIW.app
rm -rf $OUT AIWorkstation.ipa && mkdir -p $APP
# v3.9.27: AIW_ALARMKIT=1 compiles the AlarmKit path (needs the iOS 26 SDK); HealthKit/EventKit/Contacts/CoreLocation
# /AppIntents are autolinked. App Intents metadata (Shortcuts discovery) is only produced by an Xcode build.
SWIFT_FLAGS=""; [ "${AIW_ALARMKIT:-0}" = "1" ] && SWIFT_FLAGS="-D AIW_ALARMKIT"
xcrun --sdk iphoneos swiftc -target arm64-apple-ios16.0 -sdk "$SDK" -O -parse-as-library -module-name Aiw $SWIFT_FLAGS \
  Sources/*.swift -o $APP/AIW
cp Info.plist $APP/
# 球球 page for the chat header (MateView.swift): same files the web app serves (+ the non-commercial LICENSE/NOTICE)
mkdir -p $APP/mate/vendor/emotionball && cp ../web/mate.html ../web/mate-map.js $APP/mate/ && cp ../web/vendor/emotionball/*.js ../web/vendor/emotionball/LICENSE ../web/vendor/emotionball/NOTICE.md $APP/mate/vendor/emotionball/
# takeover touch page (DesktopView.swift TakeoverWeb): takeover.html + takeover.js, inlined at load
mkdir -p $APP/takeover && cp ../web/takeover.html ../web/takeover.js $APP/takeover/
# file preview page (MarkdownPreview.swift): the web app's renderer (markdown-it + DOMPurify, vendored) and styles
mkdir -p $APP/mdview/vendor/markdown && cp ../web/mdview.html ../web/mdview.js ../web/mdview.css $APP/mdview/ && cp ../web/vendor/markdown/*.js ../web/vendor/markdown/LICENSE-* $APP/mdview/vendor/markdown/
# App icon (the 球球 face): loose PNGs from the 1024 master for CFBundleIconFiles, then Assets.car via actool when it works
python3 - "$APP" <<'PY'
import sys; from PIL import Image
app=sys.argv[1]; src=Image.open('Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png').convert('RGB')
sizes=[('AppIcon20x20@2x.png',40),('AppIcon20x20@3x.png',60),('AppIcon29x29@2x.png',58),('AppIcon29x29@3x.png',87),
       ('AppIcon40x40@2x.png',80),('AppIcon40x40@3x.png',120),('AppIcon60x60@2x.png',120),('AppIcon60x60@3x.png',180),
       ('AppIcon20x20~ipad.png',20),('AppIcon20x20@2x~ipad.png',40),('AppIcon29x29~ipad.png',29),('AppIcon29x29@2x~ipad.png',58),
       ('AppIcon40x40~ipad.png',40),('AppIcon40x40@2x~ipad.png',80),('AppIcon76x76~ipad.png',76),('AppIcon76x76@2x~ipad.png',152),
       ('AppIcon83.5x83.5@2x~ipad.png',167)]
for name,px in sizes:
    src.resize((px,px),Image.LANCZOS).save(f'{app}/{name}')
PY
if xcrun actool --compile "$APP" --platform iphoneos --minimum-deployment-target 16.0 --app-icon AppIcon \
     --target-device iphone --target-device ipad --output-partial-info-plist $OUT/assets.plist Assets.xcassets >$OUT/actool.log 2>&1 \
   && [ -f "$APP/Assets.car" ]; then
  /usr/libexec/PlistBuddy -c "Add :CFBundleIcons:CFBundlePrimaryIcon:CFBundleIconName string AppIcon" $APP/Info.plist
  /usr/libexec/PlistBuddy -c "Add :CFBundleIcons~ipad:CFBundlePrimaryIcon:CFBundleIconName string AppIcon" $APP/Info.plist
  echo "Assets.car compiled (AppIcon)"
else
  echo "::warning::actool failed; the IPA uses the loose AppIcon PNGs only"; cat $OUT/actool.log || true; rm -f "$APP/Assets.car"
fi
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

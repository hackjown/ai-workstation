# Aiw for Android

A Kotlin WebView shell (minSdk 26) around the Aiw web console (apps/web). Same version as the iOS app
(`version.properties`, bumped together with `scripts/bump-version.sh <version> <build>`).

- First run: paste the web address (`http://192.168.50.188:28000/?token=…`) or scan its QR code; edit later from
  the launcher shortcut 服务器设置 (long-press the icon), the error page, or Back → 服务器设置.
- Cleartext HTTP is allowed (LAN servers). Uploads: camera photo/video, photos, files. Downloads: DownloadManager.
- Notifications: WorkManager polls `GET /api/notifications?unread=true` about every 15 min and shows takeover /
  question (`input`), `confirm` and `failed`; tapping opens the task (`?open=task/<id>`).
- CI: `.github/workflows/android.yml` builds a release APK signed with the committed sideload key
  `app/aiw-sideload.p12` (password `aiw-sideload`, not a secret; stable signature so updates install in place)
  and publishes it to the release `apk-latest` as `AIWorkstation.apk`.

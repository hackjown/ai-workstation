"""Android app (apps/android): same version as the iOS app, and the shell's wiring is in place."""
import plistlib
import re
import subprocess
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AND = ROOT / "apps" / "android"
IOS = ROOT / "apps" / "ios"


def _props():
    out = {}
    for line in (AND / "version.properties").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_android_version_matches_ios():
    ios = plistlib.loads((IOS / "Info.plist").read_bytes())
    p = _props()
    assert p["versionName"] == ios["CFBundleShortVersionString"], "run scripts/bump-version.sh <version> <build>"
    assert p["versionCode"] == ios["CFBundleVersion"], "versionCode must equal the iOS build number"
    assert p["versionCode"].isdigit()


def test_gradle_reads_version_file_and_min_sdk():
    g = (AND / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert 'version.properties' in g and 'getProperty("versionCode")' in g and 'getProperty("versionName")' in g
    assert re.search(r"minSdk\s*=\s*26", g)
    assert 'signingConfigs.getByName("sideload")' in g and (AND / "app" / "aiw-sideload.p12").exists()


def test_bump_script_covers_android(tmp_path):
    s = (ROOT / "scripts" / "bump-version.sh").read_text(encoding="utf-8")
    assert "apps/android/version.properties" in s and "apps/ios/Info.plist" in s and "apps/ios/Share/Info.plist" in s
    if not shutil.which("bash"):
        return
    # run it on a copy so the repo is untouched
    for rel in ("scripts/bump-version.sh", "apps/ios/Info.plist", "apps/ios/Share/Info.plist", "apps/android/version.properties"):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((ROOT / rel).read_bytes())
    subprocess.run(["bash", str(tmp_path / "scripts/bump-version.sh"), "9.9.9", "999"], check=True, capture_output=True)
    for rel in ("apps/ios/Info.plist", "apps/ios/Share/Info.plist"):
        p = plistlib.loads((tmp_path / rel).read_bytes())
        assert (p["CFBundleShortVersionString"], p["CFBundleVersion"]) == ("9.9.9", "999")
    t = (tmp_path / "apps/android/version.properties").read_text(encoding="utf-8")
    assert "versionName=9.9.9" in t and "versionCode=999" in t


def test_manifest_and_features():
    m = (AND / "app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert 'networkSecurityConfig="@xml/network_security_config"' in m
    assert 'cleartextTrafficPermitted="true"' in (AND / "app/src/main/res/xml/network_security_config.xml").read_text()
    assert 'android:scheme="aiw"' in m and "FileProvider" in m and "POST_NOTIFICATIONS" in m
    src = AND / "app/src/main/java/com/hackjown/aiw"
    main = (src / "MainActivity.kt").read_text(encoding="utf-8")
    for needle in ("javaScriptEnabled = true", "domStorageEnabled = true", "onShowFileChooser", "ACTION_IMAGE_CAPTURE",
                   "ACTION_VIDEO_CAPTURE", "DownloadManager", "canGoBack", "SwipeRefreshLayout", "ALGORITHMIC_DARKENING"):
        assert needle in main, needle
    app = (src / "AiwApp.kt").read_text(encoding="utf-8")
    assert "PeriodicWorkRequestBuilder<PollWorker>(15, TimeUnit.MINUTES)" in app
    poll = (src / "PollWorker.kt").read_text(encoding="utf-8")
    assert "/api/notifications?unread=true" in poll and '"input", "confirm", "failed"' in poll and "task/" in poll
    # the kinds polled are the ones the server raises for takeover/questions, confirmations and failures
    notify = (ROOT / "services/api/aiw_api/notify.py").read_text(encoding="utf-8")
    assert 'TASK_KINDS = ("done", "failed", "confirm", "input")' in notify


def test_apk_workflow_publishes_apk_latest():
    w = (ROOT / ".github/workflows/android.yml").read_text(encoding="utf-8")
    assert "tag_name: apk-latest" in w and "AIWorkstation.apk" in w and "assembleRelease" in w
    assert "apps/ios/Info.plist" in w   # an iOS bump rebuilds (and version-checks) the APK

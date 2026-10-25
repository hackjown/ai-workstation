"""V1.6: the photopea tool edits images in Photopea (Photoshop clone) through a bridge shipped to the VM."""
import base64
import importlib.util
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pp = _load("aiw_pp", "services/agent-runtime/agent_runtime/photopea.py")
br = _load("aiw_pp_bridge", "services/agent-runtime/agent_runtime/photopea_bridge.py")


def test_tool_is_registered():
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    assert "photopea" in LOOP_TOOLS and BY_NAME["photopea"].action is None
    assert "save" in BY_NAME["photopea"].params and "NotoSansSC" in BY_NAME["photopea"].description


def test_install_command_writes_the_bridge(tmp_path):
    cmd = pp.install_command()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env).stdout
    assert "installed" in out and "PP_OK" in out
    dest = tmp_path / ".config/aiw/photopea/pp.py"
    assert dest.read_bytes() == pp.BRIDGE_SRC.read_bytes()
    out2 = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env).stdout
    assert "present" in out2 and "PP_OK" in out2


def test_build_and_check_job():
    j = pp.build_job({"open": "uploads/a.psd", "save": ["out/a.png", "out/a.jpg:0.8"], "script": "x=1", "timeout": "5"})
    assert j["open"] == ["uploads/a.psd"] and j["timeout"] == 20 and j["keep"] is False
    assert pp.check_job(j) is None
    assert "at least one" in pp.check_job(pp.build_job({}))
    assert "arguments" in pp.check_job(pp.build_job({"script": "function f(){return arguments[0]}"}))
    assert "ppLog" in pp.check_job(pp.build_job({"script": "app.echoToOE('x')"}))
    assert "save" in pp.check_job(pp.build_job({"script": "app.activeDocument.saveToOE('png')"}))
    assert "format" in pp.check_job(pp.build_job({"open": ["a.png"], "save": ["out/a"]}))
    assert "keep=true" in pp.check_job(pp.build_job({"place": ["logo.png"], "script": "ppPlace('logo.png')"}))


def test_run_command_round_trips_the_job():
    j = pp.build_job({"open": ["中文.png"], "script": "var t='荣誉室';"})
    cmd = pp.run_command(j)
    b64 = cmd.split("--job-b64 ", 1)[1]
    assert json.loads(base64.b64decode(b64)) == j and "'" not in b64


def test_summarize_lists_layers_and_files():
    res = {"ok": True, "saved": [{"path": "/home/aiw/workspace/out/a.png", "bytes": 10}], "echo": ["size 400 300"],
           "info": [{"name": "a.psd", "width": 400, "height": 300, "active": True, "layers": [
               {"name": "标题", "type": "ArtLayer", "visible": True, "opacity": 100, "bounds": [20, 237, 182, 263],
                "text": "荣誉室", "font": "NotoSansSC-Bold", "size": 26, "color": "FFDD00"},
               {"name": "组", "type": "LayerSet", "visible": False, "layers": [{"name": "bg", "type": "ArtLayer"}]}]}]}
    s = pp.summarize(res)
    assert "saved out/a.png" in s and "TEXT '荣誉室'" in s and "bounds 20,237,182,263" in s
    assert "hidden" in s and "  - bg" in s and "read_image" in s
    assert pp.summarize({"ok": False, "error": "boom"}).startswith("photopea FAILED: boom")
    assert pp.parse_result("noise\nPPRESULT {\"ok\": true}\n") == {"ok": True}


def test_bridge_formats_and_paths():
    assert br._fmt("a.psd") == "psd" and br._fmt("a.jpg:0.7") == "jpg:0.7" and br._fmt("a.JPEG") == "jpg:0.92"
    assert br._fmt("b.png") == "png" and br._fmt("b.webp") == "webp:0.92"
    assert br._has_q("/x/a.jpg:0.8") and not br._has_q("/x/a.jpg")
    assert br._abs("~/workspace/out/a.png") == str(br.ROOT / "out/a.png")
    assert br._abs("/tmp/x.png") == "/tmp/x.png"


def test_prelude_avoids_names_that_kill_photopea():
    pre = br._prelude("j1", ["/w/logo.png"])
    assert "arguments" not in pre and "function log(" not in pre
    assert "ppLog(__p1" in pre and '"logo.png": "http://127.0.0.1:' in pre


def test_profile_allows_third_party_storage(tmp_path):
    br._allow_photopea_storage(tmp_path / "prof")
    d = json.loads((tmp_path / "prof/Default/Preferences").read_text())
    assert d["profile"]["block_third_party_cookies"] is False and d["profile"]["cookie_controls_mode"] == 0


def test_bridge_protocol_with_a_fake_page(tmp_path, monkeypatch):
    """Server + a thread that plays the browser page: close-all job, file sends, scripts, outputs, result."""
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), br.H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    br.S.__init__()
    seen = []
    stop = threading.Event()

    def page():
        try:
            _page()
        except OSError:  # the server shut down under a pending poll
            pass

    def _page():
        while not stop.is_set():
            r = op.open(base + "/poll?ready=1", timeout=30)
            if r.status != 200:
                continue
            job = json.loads(r.read())
            for i, _ in enumerate(job["sends"]):
                seen.append(("file", op.open(f"{base}/in/{job['id']}/{i}").read()))
            outs = []
            for s in job["scripts"]:
                seen.append(("script", s))
                if "saveToOE" in s:
                    outs.append(b"PNGDATA")
            echo = ['PPLOG ["size 400 300"]'] if any("ppLog" in s or "__pplog" in s for s in job["scripts"]) else []
            for i, o in enumerate(outs):
                op.open(urllib.request.Request(f"{base}/out/{job['id']}/{i}", o))
            op.open(urllib.request.Request(base + "/result", json.dumps(
                {"id": job["id"], "ok": True, "echo": echo, "nfiles": len(outs)}).encode()))

    threading.Thread(target=page, daemon=True).start()
    for _ in range(50):
        if br.S.ready:
            break
        time.sleep(0.05)
    src = tmp_path / "in.png"
    src.write_bytes(b"IMG")
    res = br._run_job({"open": [str(src)], "script": "var d=app.activeDocument;", "save": [str(tmp_path / "o/out.png")],
                       "timeout": 20})
    stop.set()
    srv.shutdown()
    assert res["ok"], res
    assert (tmp_path / "o/out.png").read_bytes() == b"PNGDATA"
    assert res["echo"] == ["size 400 300"]
    kinds = [k for k, _ in seen]
    assert seen[0][0] == "script" and "close" in seen[0][1]       # documents closed first
    assert ("file", b"IMG") in seen and kinds.count("script") == 3  # close, edit, save

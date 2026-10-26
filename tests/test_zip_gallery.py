"""A zip of screenshots is listed and its pictures served one by one, so the chat can show them."""
import io
import zipfile


def test_zip_images_listed_and_served(tmp_path):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app

    art = tmp_path / "art" / "s1" / "files"; art.mkdir(parents=True)
    z = art / "shots.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("shots/login.png", b"\x89PNGfake")
        f.writestr("readme.txt", "x")
        f.writestr("__MACOSX/shots/._login.png", b"junk")
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    with TestClient(app) as cl:
        h = {"Authorization": "Bearer t"}
        r = cl.get("/api/artifacts", params={"path": str(z), "images": 1}, headers=h)
        assert r.status_code == 200 and r.json() == [{"name": "shots/login.png", "size": 8}]
        r = cl.get("/api/artifacts", params={"path": str(z), "member": "shots/login.png"}, headers=h)
        assert r.status_code == 200 and r.content == b"\x89PNGfake" and r.headers["content-type"] == "image/png"
        assert cl.get("/api/artifacts", params={"path": str(z), "member": "readme.txt"}, headers=h).status_code == 404
        assert cl.get("/api/artifacts", params={"path": str(z)}, headers=h).content.startswith(b"PK")

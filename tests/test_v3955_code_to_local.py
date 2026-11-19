"""v3.9.55: every code task runs on the local VM (it has Antigravity), never on the design / media / office cloud."""
from aiw_api import roles as R
from test_v390_fix_roles import L, _s

CODE = ["修复早报页面图片不显示的 bug", "把 aiw_news.py 的 AI 科技栏改一下", "写个 python 脚本把 png 批量转 webp",
        "用 antigravity 给 juchang8 加一个接口", "apk 打包报错了，帮我看看", "前端 UI 改成深色，logo 放左上角",
        "【技能：dev】做一个设计稿生成器", "github 上那个仓库拉下来编译一下", "开发一个记账小程序"]
NOT_CODE = ["照着这张照片画施工图，导出 dxf", "把这个mp4剪成30秒", "把这个 excel 表格整理成周报 word",
            "给这张图片 P 掉水印", "帮我开发票", "写一个视频脚本文案", "设计一个 logo 海报"]


def test_code_goes_local_even_with_picture_words():
    s = _s()
    for p in CODE:
        assert R.is_code(p), p
        assert R.pick(s, p, "cloud-a") == L, p


def test_non_code_keeps_roles():
    s = _s()
    for p in NOT_CODE:
        assert not R.is_code(p), p
    assert R.pick(s, "照着这张照片画施工图，导出 dxf", L) == "cloud-a"
    assert R.pick(s, "给这张图片 P 掉水印", L) == "cloud-a"


def test_busy_local_still_gets_code():
    s = _s(busy={L})
    assert R.pick(s, "修一下登录接口的报错", L) == L


def test_named_computer_wins_and_switch_off():
    s = _s()
    assert R.pick(s, "用 云A 改一下这个 python 脚本", L) == "cloud-a"
    s.store.kv["machine_roles"] = {"code_to_local": False}
    assert R.pick(s, "修一下登录接口的报错", L) != L


def test_code_machine_choice():
    s = _s()
    s.store.ms.append({"id": "local-2", "name": "本地2"})
    assert R.code_machine(s) == L
    s.store.kv["machine_roles"] = {"code_machine": "local-2"}
    assert R.pick(s, "写个爬虫", L) == "local-2"
    s.store.kv["machine_roles"] = {"code_machine": "cloud-a"}   # a cloud id is ignored
    assert R.code_machine(s) == L


def test_dispatch_places_code_on_local():
    s = _s()
    ro = R.annotate(s, [{"id": L, "available": True}, {"id": "cloud-a", "available": True}])
    assert ro[0]["role"] == "code" and ro[0].get("available") is not False
    it = R.place_auto(s, [{"machine": "auto", "goal": "修复 brief_extras.py 的报错"},
                          {"machine": "auto", "goal": "剪一个 30 秒视频"}])
    assert [i["machine"] for i in it] == [L, "cloud-b"]


def test_show_lists_local_as_code():
    s = _s()
    row = next(m for m in R.show(s)["computers"] if m["id"] == L)
    assert row["kind"] == "local" and row["role"] == "code" and row["role_title"] == "代码开发"

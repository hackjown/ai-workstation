"""v3.9.44 (task-b09): a 姓名牌 .docx sent in the chat became a run of the 道闸图集 task, was classified as a code fix,
and its todos deadlocked (refused 8x) until the stuck check asked the user after the file was already delivered."""
from agent_runtime import intent as _in
from agent_runtime.devloop import DevLoop
from aiw_api import continuation as _cont

SIG = {"script_runs": ["python3 build_album.py"], "code_files": ["build_album.py"]}


def test_officecli_skill_is_not_dev_fix():
    assert _in.classify("【技能：officecli】\n你用这个技能来修改啊", SIG)["intent"] == _in.OTHER


def test_docx_edit_is_not_dev_fix():
    assert _in.classify("把姓名牌 .docx 里的名字改成庹芯语", SIG)["intent"] == _in.OTHER


def test_code_fixes_still_dev_fix():
    assert _in.classify("改了样式点击又进入不了详情，修复", SIG)["intent"] == _in.DEV_FIX
    assert _in.classify("把build_album.py改一下", SIG)["intent"] == _in.DEV_FIX
    assert _in.classify("【技能：dev】修一下", SIG)["forced"]
    assert _in.classify("【技能：officecli】把生成表格的 python 脚本修好", SIG)["intent"] == _in.DEV_FIX


class _Store:
    def __init__(self, prompt):
        self.t = {"id": "task-383", "prompt": prompt, "created_at": 1}

    def task(self, tid):
        return self.t if tid == "task-383" else None

    def continuations(self, root):
        return [self.t]

    def kv_get(self, k, d=None):
        return d

    def events(self, sid, limit=0):
        return []

    def artifacts(self, sid):
        return []

    def _x(self, *a):
        raise RuntimeError("no db")


def test_unrelated_attachment_is_not_a_followup():
    st = _Store("搜索科拓、捷顺两家公司的道闸图片，包括单杆、空降道闸、广告道闸等")
    assert not _cont.about_task(st, "task-383", "把名字改成庹芯语", [{"name": "姓名牌 .docx"}])
    assert _cont.about_task(st, "task-383", "这几张也加进去", [{"name": "道闸3.jpg"}])
    assert _cont.about_task(st, "task-383", "继续", [])
    assert _cont.about_task(st, "task-383", "改一下，单杆的多找几张", [])


def test_skill_mismatch_is_not_a_followup():
    st = _Store("【技能：huashu-motion】把 PPT 做成讲解视频")
    assert not _cont.about_task(st, "task-383", "【技能：officecli】改一下", [])
    assert _cont.about_task(st, "task-383", "【技能：huashu-motion】改一下配音", [])


def test_todos_do_not_deadlock_without_tracked_edits():
    lp = DevLoop()
    lp.todo_write(1, [{"id": "1", "content": "定位", "status": "in_progress"}, {"id": "2", "content": "替换", "status": "pending"}])
    ok, msg = lp.todo_write(5, [{"id": "1", "content": "定位", "status": "done"},
                                {"id": "2", "content": "替换", "status": "in_progress"}])
    assert ok, msg
    assert [t["status"] for t in lp.todos] == ["done", "in_progress"]


def test_refused_item_goes_pending_not_double_in_progress():
    lp = DevLoop()
    lp.todo_write(1, [{"id": "1", "content": "改源码", "status": "in_progress"}, {"id": "2", "content": "发文件", "status": "pending"}])
    lp.record_edit(3, "/data/home/workspace/a.py")
    ok, msg = lp.todo_write(4, [{"id": "1", "content": "改源码", "status": "done"},
                                {"id": "2", "content": "发文件", "status": "in_progress"}])
    assert not ok and "PARTLY REJECTED" in msg
    assert [t["status"] for t in lp.todos] == ["pending", "in_progress"]


def test_read_image_and_officecli_query_count_as_verification():
    lp = DevLoop()
    lp.record_edit(3, "/data/home/workspace/a.py")
    lp.post(4, "read_image", {"path": "out.png"}, True)
    assert lp.evidence_after(3)
    lp2 = DevLoop()
    lp2.record_edit(3, "/data/home/workspace/a.py")
    lp2.post(4, "shell", {"command": "officecli query x.docx ':contains(庹芯语)'"}, True)
    assert lp2.evidence_after(3)


def test_third_refusal_lets_it_through_flagged():
    lp = DevLoop()
    lp.todo_write(1, [{"id": "1", "content": "改源码", "status": "in_progress"}])
    lp.record_edit(3, "/data/home/workspace/a.py")
    for i in range(2):
        ok, _ = lp.todo_write(4 + i, [{"id": "1", "content": "改源码", "status": "done"}])
        assert not ok
    ok, _ = lp.todo_write(7, [{"id": "1", "content": "改源码", "status": "done"}])
    assert ok and lp.todos[0]["status"] == "done" and "未验证" in lp.todos[0]["evidence"]

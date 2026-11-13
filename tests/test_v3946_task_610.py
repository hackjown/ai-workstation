"""v3.9.46 (task-610): "建模" + 大厅.zip (a room scan) shared to the chat became a run of the 道闸图集 task (which had
made zips), and the long-continuation rule routed it as a code fix with build_album.py as the contract's source."""
import types

from aiw_api import continuation as _cont
from aiw_api.sessions import SessionManager

ALBUM = "搜索科拓、捷顺两家公司的道闸图片，包括单杆、空降道闸、广告道闸等\n道闸图集.zip"


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
        return [{"type": "agent.decision"}] * 60

    def artifacts(self, sid):
        return []


def test_scan_zip_is_not_a_run_of_the_album_task():
    st = _Store(ALBUM)
    assert not _cont.about_task(st, "task-383", "建模", [{"name": "大厅.zip"}])
    assert not _cont.about_task(st, "task-383", "解压看看", [{"name": "大厅.zip"}])
    assert _cont.about_task(st, "task-383", "这是道闸的压缩包", [{"name": "道闸补充.zip"}])


def test_scan_followup_of_a_scan_task_still_continues():
    st = _Store("建模\n附件：\n- ~/workspace/uploads/大厅.zip")
    assert _cont.about_task(st, "task-383", "再建一次模", [{"name": "大厅2.zip"}])


def _long(prompt):
    sm = SessionManager.__new__(SessionManager)
    sm.store = types.SimpleNamespace(continuations=lambda r: [{"id": "task-383", "session_id": "s1"}],
                                     events=lambda sid, limit=0: [{"type": "agent.decision"}] * 60)
    t = types.SimpleNamespace(source="continue", continues="task-383", id="task-610", prompt=prompt)
    return sm._long_run(t)


def test_new_goal_is_not_a_long_dev_run():
    assert not _long(ALBUM + "\n\n【接着上次继续做】用户补充：建模\n\n附件：\n- ~/workspace/uploads/大厅.zip")
    assert _long(ALBUM + "\n\n【接着上次继续做】用户补充：继续")
    assert _long(ALBUM)

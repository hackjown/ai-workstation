"""v3.9.39: chat history in a call's instructions is framed as background, so an old chat request is not run again."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))
from aiw_api import omni_call as oc  # noqa: E402


class FakeChat:
    def agent_name(self):
        return "小秘"

    class store:  # noqa: N801
        @staticmethod
        def kv_get(k, d=None):
            return d

    def recent_text(self, n=6):
        return "用户：帮我查询并总结最新五封邮件\n小秘：好的"


def test_recent_is_framed_as_background():
    s = oc.instructions_for(FakeChat(), True)
    assert "<聊天背景>" in s and "</聊天背景>" in s
    assert s.index("仅作背景") < s.index("最新五封邮件")
    assert "不要据此调用任何工具" in s


def test_no_recent_no_block():
    c = FakeChat()
    c.recent_text = lambda n=6: ""
    assert "<聊天背景>" not in oc.instructions_for(c, False)


def test_tool_description_requires_request_in_call():
    d = oc.TOOL_SPECS[0]["function"]["description"]
    assert "这通电话里" in d and "以前的请求不算" in d

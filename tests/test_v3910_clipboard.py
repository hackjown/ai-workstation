"""3.9.10 clipboard for the web takeover: the viewer has no paste, so text goes to the VM clipboard (xclip) through
/api/machines/<mid>/input {"t": "paste"} and the chosen key is pressed; /api/machines/<mid>/clipboard reads it back."""
import base64
import re

from aiw_api.remote_input import InputPump
from aiw_protocols.input_ops import clean, paste_script
from computer_runtime.providers.base import ActionResult


def test_paste_op_is_cleaned():
    assert clean({"t": "paste", "text": "a\nb", "key": "ctrl+shift+v"}) == {"t": "paste", "text": "a\nb", "key": "ctrl+shift+v"}
    assert clean({"t": "paste", "text": "x", "key": "rm -rf"})["key"] == "ctrl+v"      # unknown keys -> Ctrl+V
    assert clean({"t": "paste", "text": "x", "key": ""})["key"] == ""                    # only copy
    assert clean({"t": "paste", "text": ""}) is None
    assert len(clean({"t": "paste", "text": "长" * 200_000})["text"]) == 100_000


def test_paste_script_carries_the_text_base64_and_presses_the_key():
    text = '你好 "q" $HOME `x`\nline2'
    cmd = paste_script(clean({"t": "paste", "text": text, "key": "ctrl+shift+v"}))
    b64 = re.search(r"printf %s (\S+) \|", cmd).group(1)
    assert base64.b64decode(b64).decode() == text
    assert "xclip -selection clipboard" in cmd and ">/dev/null 2>&1" in cmd     # a forked xclip never holds the output
    assert "k=ctrl+shift+v;" in cmd and cmd.rstrip().endswith('xdotool key --clearmodifiers -- "$k"; }')
    assert "xdotool key" not in paste_script(clean({"t": "paste", "text": "x", "key": ""}))


class Comp:
    def __init__(self):
        self.calls = []

    async def execute(self, action, payload, timeout=None):
        self.calls.append((action, payload))
        if action == "computer.input":
            return ActionResult(True, {"applied": len(payload["ops"]), "v": 2, "width": 1280, "height": 800})
        return ActionResult(True, {"exit_code": 0, "stdout": ""})


async def test_paste_goes_by_shell_between_the_other_ops_in_order():
    c = Comp()
    p = InputPump(c, legacy=False)
    p.size = (1280, 800)
    p.push([{"t": "click", "x": 5, "y": 5}, {"t": "paste", "text": "abc"}, {"t": "key", "key": "Return"}])
    await p.drain()
    kinds = [a for a, _ in c.calls]
    assert kinds == ["computer.input", "shell.exec", "computer.input"] and p.applied == 3
    assert "xclip" in c.calls[1][1]["command"]

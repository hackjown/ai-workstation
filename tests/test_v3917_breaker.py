"""v3.9.17 item 3: 工具熔断 (+ BrowserOS 9210 start fixes)."""
from agent_runtime.breaker import Breaker, program, signature


def test_same_error_twice_trips_and_refuses():
    b = Breaker()
    assert b.feed("read_image", {"path": "a.png"}, False, "HTTP 502 Bad Gateway after 31s") is None
    note = b.feed("read_image", {"path": "b.png"}, False, "HTTP 502 Bad Gateway after 29s")
    assert note and "TOOL DISABLED" in note and "OCR" in note
    assert b.blocked("read_image", {}) and "read_image" in b.hidden()


def test_different_errors_do_not_trip():
    b = Breaker()
    b.feed("read_image", {}, False, "HTTP 502")
    assert b.feed("read_image", {}, False, "timeout after 60s") is None
    assert not b.blocked("read_image", {})


def test_shell_trips_per_program_not_whole_shell():
    b = Breaker()
    b.feed("shell", {"command": "sshpass -p x ssh root@h"}, False, "sshpass: command not found")
    n = b.feed("shell", {"command": "cd ~ && sshpass -p y ssh root@h uptime"}, False, "sshpass: command not found")
    assert n and "sshpass" in n and "ssh_exec" in n
    assert b.blocked("shell", {"command": "sshpass -p z ssh h"})
    assert not b.blocked("shell", {"command": "ls"})
    assert "shell" not in b.hidden()


def test_policy_refusals_are_not_tool_failures():
    b = Breaker()
    for _ in range(3):
        assert b.feed("shell", {"command": "sudo su"}, False, "forbidden by policy: shell: root shell") is None
    assert not b.tripped


def test_bos_trip_hides_all_bos_tools():
    b = Breaker()
    b.feed("bos_snapshot", {}, False, "BrowserOS: ConnectError: [Errno 111] Connection refused")
    b.feed("bos_snapshot", {}, False, "BrowserOS: ConnectError: [Errno 111] Connection refused")
    assert "bos_act" in b.hidden() and b.blocked("bos_navigate", {})


def test_signature_and_program():
    assert signature("timeout after 60s /tmp/a.py") == signature("timeout after 61s /home/x/b.py")
    assert program("cd ~/workspace && timeout 30 python3 x.py") == "python3"


def test_browseros_child_process_is_not_the_browser(tmp_path):
    from vm_agent.browser import browseros as B
    root = tmp_path / "proc"
    (root / "10").mkdir(parents=True)
    (root / "10" / "cmdline").write_bytes(b"/usr/lib/browserclaw/browserclaw\0--type=renderer\0")
    assert B._running(str(root)) is False          # only a leftover renderer: relaunch
    (root / "11").mkdir()
    (root / "11" / "cmdline").write_bytes(b"/usr/lib/browserclaw/browserclaw\0--no-first-run\0")
    assert B._running(str(root)) is True


def test_browseros_display_guess(tmp_path):
    from vm_agent.browser import browseros as B
    (tmp_path / "X1").touch()
    assert B.guess_display(str(tmp_path)) == ":1"
    assert B.guess_display(str(tmp_path / "none")) == ":0"


def test_setup_script_has_diag_and_reads_config_ports():
    from pathlib import Path
    s = (Path(__file__).parent.parent / "deploy" / "vm" / "browseros-setup.sh").read_text()
    assert "diag)" in s and "conf_port server" in s and "Singleton" in s and "$MCP_PORT" in s

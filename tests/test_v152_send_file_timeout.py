from tool_router.executor import _default_timeout


def test_send_file_gets_a_long_timeout():
    # task-a0e: a 3.4 MB PDF through the InstaBox tunnel missed the 30 s files.read timeout three times
    assert _default_timeout("files.read", {"path": "a.pdf", "binary": True}) >= 300
    assert _default_timeout("files.read", {"path": "a.txt"}) == 30
    assert _default_timeout("shell.exec", {"command": "ls"}) == 60

"""v3.9.18: the task view stops following new output once you scroll up, and older approvals fold."""
import pathlib, re, shutil, subprocess, json, pytest

WEB = pathlib.Path(__file__).resolve().parents[1] / "apps" / "web"


def test_follow_only_when_at_bottom():
    s = (WEB / "app.js").read_text()
    assert "S.stick" in s and 'addEventListener("wheel"' in s
    assert "nearBottom" not in s                      # the 140 px snap-back is gone
    assert 'details[data-k]' in s and 'data-k="steps:' in s


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_apv_log_folds_older_rows():
    js = """global.window=global;global.document={addEventListener(){},getElementById(){return null}};
require(%s);const evs=[];for(let i=0;i<9;i++)evs.push({type:"agent.confirm_answered",data:{approve:true,tool:"shell",title:"t"+i,risk:"low"}});
const h=apvLog(evs,"x");console.log(JSON.stringify({fold:h.includes("更早的 7 条"),key:h.includes('data-k="apv:x"'),few:apvLog(evs.slice(0,3),"x").includes("apv-fold")}));""" % json.dumps(str(WEB / "approval.js"))
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert out == {"fold": True, "key": True, "few": False}

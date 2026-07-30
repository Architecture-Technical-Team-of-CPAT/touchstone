"""补齐边角分支的覆盖：checks relay/service/main、pr_agent ping/log、preflight ping、
review_provider 配置异常、stack_rules/loop/govern/calibrate 的边角路径。离线 mock。"""
import json
import os


# ---------------- checks ----------------
from touchstone import checks


def test_run_relay_not_completed(monkeypatch):
    monkeypatch.setattr(checks.ghclient, "paginate_check_runs",
                        lambda *a, **k: {"check_runs": [{"name": "unit", "status": "in_progress"}]})
    passed, summ = checks._run_relay({"owner": "o", "repo": "r", "sha": "s", "token": "t"},
                                     {"source_check": "unit"})
    assert passed is None and "未完成" in summ


def test_run_service(monkeypatch):
    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"passed": True, "summary": "ok"}
    monkeypatch.setattr(checks.requests, "post", lambda *a, **k: _R())
    passed, summ = checks._run_service({"owner": "o", "repo": "r", "sha": "s"}, {"url": "http://x"})
    assert passed is True and summ == "ok"


def test_run_checks_unknown_type_and_isolation(monkeypatch):
    cfg = {"gate": {"status_name": "g"}, "checks": [
        {"name": "?", "type": "wat", "required": True},
        {"name": "boom", "type": "service", "url": "http://x", "required": False},
    ]}
    monkeypatch.setattr(checks.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    res = checks.run_checks(cfg, {"owner": "o", "repo": "r", "sha": "s", "token": "t"})
    assert res[0].passed is None and "未知插件类型" in res[0].summary
    assert res[1].passed is None and "插件异常" in res[1].summary


def test_checks_main_missing_findings_with_sha(monkeypatch, tmp_path):
    posted = {}
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda method, url, token, data=None, **k: posted.update(data or {}) or {})
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("TOUCHSTONE_HEAD_SHA", "deadbee")
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)                 # cwd 无 touchstone-findings.json
    checks.main()
    assert posted.get("conclusion") == "failure" and posted.get("head_sha") == "deadbee"


def test_checks_main_post_failure_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("post fail")))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("TOUCHSTONE_HEAD_SHA", "x")
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    checks.main()                                # 不抛


# ---------------- pr_agent_runner ----------------
from touchstone import pr_agent_runner as R


def test_ping_llm_success(monkeypatch):
    called = {}
    class _C:
        def __init__(self, **kw): pass
        @property
        def chat(self): return self
        @property
        def completions(self): return self
        def create(self, **kw): called.update(kw)
    monkeypatch.setattr(R, "openai", type("M", (), {"OpenAI": _C})(), raising=False)
    import openai
    monkeypatch.setattr(openai, "OpenAI", _C, raising=False)
    R._ping_llm("http://b", "k", "m")
    assert called["model"] == "m" and called["max_tokens"] == 1


def test_write_interaction_log_failure(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("TOUCHSTONE_INTERACTION_LOG", "/no/such/dir/ix.log")
    with caplog.at_level(logging.WARNING, logger="touchstone.pr_agent"):
        R._write_interaction_log({"x": 1})       # open 失败 → 走 except，不抛
    assert "交互日志写入失败" in caplog.text      # 留痕到日志，不静默


# ---------------- preflight ----------------
from touchstone import preflight as P


def test_preflight_ping_failure(monkeypatch):
    import urllib.error
    def boom(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(P.urllib.request, "urlopen", boom)
    ok, msg = P._ping("http://x")
    assert ok is False and "URLError" in msg


def test_preflight_ping_success(monkeypatch):
    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda *a, **k: _R())
    assert P._ping("http://x") == (True, "HTTP 200")


def test_preflight_main_net_branch(monkeypatch, capsys):
    monkeypatch.setattr(sys_mod("sys").argv, ["preflight", "--no-net"], raising=False) if False else None
    import sys
    monkeypatch.setattr(sys, "argv", ["preflight"])    # 不带 --no-net → 走 check_network
    monkeypatch.setattr(P, "check_network", lambda env: [("net", True, "ok")])
    for k in ("GITHUB_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("TOUCHSTONE_STANDARDS", os.path.join(
        os.path.dirname(P.__file__), "..", ".touchstone", "standards.yaml"))
    P.main()
    assert "配置就绪" in capsys.readouterr().out


def sys_mod(name):
    import sys
    return sys


# ---------------- review_provider ----------------
from touchstone import review_provider as RP


def test_load_nmap_bad_yaml(tmp_path):
    p = tmp_path / "pr-agent.yaml"
    p.write_text("normalization: [unclosed\n", encoding="utf-8")
    import os
    os.environ["TOUCHSTONE_PRAGENT"] = str(p)
    try:
        nmap = RP.load_nmap(str(tmp_path))      # YAMLError → 用默认
        assert "label_to_category" in nmap
    finally:
        os.environ.pop("TOUCHSTONE_PRAGENT", None)


def test_load_provider_cfg_missing(tmp_path):
    assert RP._load_provider_cfg(str(tmp_path)) == {}


def test_experience_injection_exception(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_EXPERIENCE_ENABLED", "true")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("TOUCHSTONE_EXPERIENCE_REF", raising=False)
    import sys
    monkeypatch.setitem(sys.modules, "learning_loop",
                        type("M", (), {"render_injection": lambda s: (_ for _ in ()).throw(RuntimeError("x")),
                                       "load_store": lambda: {}})())
    assert RP._experience_injection(".") == ""


# ---------------- stack_rules ----------------
from touchstone import stack_rules as SR


def test_stack_rules_unknown_applies_to_skipped():
    rules = [{"id": "X", "applies_to": "cobol", "pattern": "foo", "severity": "warn"}]
    # cobol 不在任何栈 → 无发现
    assert SR.check_stack_rules("--- a/x\n+++ b/x\n@@ +0,0 +1,1 @@\n+foo\n",
                                {r["id"]: r for r in rules}) == []


# ---------------- loop ----------------
from touchstone import loop
import pytest



# ---------------- govern ----------------
from touchstone import govern


def test_govern_detect_revert_shas_git_failures(monkeypatch):
    # git 不可用 → None（不伪装成空集；熔断据此失败收敛，而非谎报 0 revert/健康=fail-open）
    monkeypatch.setattr(govern.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no git")))
    assert govern.detect_revert_shas(".", "main") is None


def test_govern_detect_revert_shas_nonzero_exit_is_failure(monkeypatch):
    # git 非零退出（坏 base ref / 浅克隆缺历史）= 取不到真值 → None（旧实现无 check=True 吞错返空集=fail-open）
    class _Err:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(govern.subprocess, "run", lambda *a, **k: _Err())
    assert govern.detect_revert_shas(".", "bad-ref") is None


def test_govern_detect_revert_shas_clean_is_empty_set(monkeypatch):
    # 扫描成功、确无 revert → set()（与"失败→None"严格区分；熔断据此知"测了=0"而非"没测到"）
    class _R:
        returncode = 0
        stdout = "普通提交，无 revert\n"
    monkeypatch.setattr(govern.subprocess, "run", lambda *a, **k: _R())
    assert govern.detect_revert_shas(".", "main") == set()


def test_govern_detect_revert_shas_parses(monkeypatch):
    class _R:
        returncode = 0
        stdout = ("Merge revert\n\nThis reverts commit abc1234.\n"
                  "This reverts commit deadbeef9.\n")
    monkeypatch.setattr(govern.subprocess, "run", lambda *a, **k: _R())
    shas = govern.detect_revert_shas(".", "main")
    assert "abc1234" in shas and "deadbeef9" in shas


def test_govern_build_merge_records_marks_reverted():
    recs = [{"merged": True, "merge_commit_sha": "abc12345", "auto_handled": True},
            {"merged": False, "merge_commit_sha": "zzz", "auto_handled": False}]
    out = govern.build_merge_records(recs, {"abc1234"})
    assert len(out) == 1                            # 只 merged 的
    assert out[0]["reverted"] is True and out[0]["auto_handled"] is True


# ---------------- calibrate ----------------
from touchstone import calibrate


def test_calibrate_aggregate_empty():
    agg = calibrate.aggregate([])
    assert agg["total"] == 0 and agg["prs_with_findings"] == 0
    assert set(agg["by_risk"]) == {"high", "mid", "low"}

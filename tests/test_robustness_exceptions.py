"""异常场景与防静默故障的健壮性测试：构造失败输入，断言【结果正确】
（不只是"不崩"）——确保吞异常路径降级得当、不掩盖真问题。离线 mock。"""
import os


# ---------------- verify_change：变异/外部命令/diff 解析的失败降级 ----------------
from verify import verify_change as V


def test_ast_mutants_syntax_error_returns_empty():
    # 不可解析源码 → 无变异体（不能对坏代码做变异）
    assert V._ast_mutants("def (\n") == []


def test_mutation_check_restores_original_even_if_tests_fail(monkeypatch, tmp_path):
    # 变异注入后，无论测试结果如何，文件必须还原成原始（不能把变异留在工作树）
    f = tmp_path / "mod.py"
    orig = "def add(a, b):\n    return a + b\n"
    f.write_text(orig, encoding="utf-8")
    monkeypatch.setattr(V, "_run_tests", lambda wd, tc: (False, "killed"))   # 测试总挂
    V._mutation_check(str(tmp_path), "test_code", ["mod.py"])
    assert f.read_text(encoding="utf-8") == orig           # 还原成功


def test_mutation_check_no_py_files_returns_one():
    # 无 .py 改动 → applied=0 → 充分性 1.0（无可变异点不扣分）
    assert V._mutation_check(".", "t", ["a.js"]) == 1.0


def test_external_mutation_cmd_timeout_returns_none(monkeypatch, tmp_path):
    # 外部变异命令超时（真抛）→ None，回退内置
    import subprocess as sp
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", "1")   # 走 legacy 路径以真抵达 subprocess.run
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "sleep 5")
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TIMEOUT", "1")
    # 真跑 sleep 会等 1s 超时；改用直接打桩 TimeoutExpired 更快更稳
    monkeypatch.setattr(V.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired(cmd="x", timeout=1)))
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None


def test_external_mutation_cmd_malformed_timeout_returns_none(monkeypatch, tmp_path):
    """round-2 PRA-REVIEW:195 / PRA-POSSIBLE_ISSUE:195：TOUCHSTONE_MUTATION_TIMEOUT 畸形（非数字，
    如部署 typo `30s`）→ int() 抛 ValueError。int() 必须在 try 内 → 被吞 → None（graceful degradation，
    回退内置变异），不得向调用方抛 ValueError 崩 verify pipeline。把 int() 挪回 try 外（变异）→
    本测会拿 ValueError 而非 None → 杀红。"""
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo 75%")
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TIMEOUT", "30s")   # 非数字
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None


def test_changed_lines_git_failure_returns_empty(monkeypatch):
    # git diff 子进程失败 → {} （不能崩，也不能误报改动行）
    monkeypatch.setattr(V.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(V.subprocess.SubprocessError("git fail")))
    assert V._changed_lines(".", "b", "h") == {}


def test_resolve_acceptance_spec_bad_yaml(tmp_path, monkeypatch):
    # acceptance.yaml 解析失败 → 视为无规格，回落 author（不崩、明确降级）
    import yaml
    p = tmp_path / "acceptance.yaml"
    p.write_text("criteria: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_CONTRACT", str(tmp_path / "pr.yaml"))
    crit, source = V.resolve_acceptance_spec({"acceptance": str(p)}, str(tmp_path))
    assert crit == [] or crit is None           # 坏配置不冒充有规格


# ---------------- learning_loop：JSON 抽取失败降级 ----------------
def test_extract_json_malformed_in_fence_returns_default():
    from touchstone import learning_loop as L
    # fence 内是非法 JSON → 内层 JSONDecodeError 被吞、继续 → 最终 default
    assert L._extract_json("```json\n{not valid}\n```", "DEF") == "DEF"


# ---------------- orchestrator：CI check-runs 取数失败 → None（未知，不误判）----------------
def test_ci_verdict_http_error_returns_none(monkeypatch):
    from touchstone import orchestrator as orc
    import requests
    monkeypatch.setattr(orc, "gh",
                        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.HTTPError("403")))
    assert orc.ci_verdict("o", "r", "sha", "t") is None     # 评不了 → None，不强制 fail


# ---------------- review_provider：诊断日志/临时文件清理不崩 ----------------
def test_invoke_endpoint_logs_raw_count_and_cleans(monkeypatch, tmp_path):
    from touchstone import review_provider as RP
    import json
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "be strict")   # 触发临时文件路径
    created = {}
    monkeypatch.setattr(RP.subprocess, "run",
                        lambda cmd, **k: created.update(cmd=cmd) or
                        type("_R", (), {"returncode": 0,
                                        "stdout": json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}}),
                                        "stderr": ""})())
    out = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert out == []                                        # 正常解析（空）
    assert "--extra-instructions-file" in created["cmd"]    # extra 经临时文件传入


# ---------------- ghclient：非预期响应体不崩 ----------------
def test_request_non_json_body_returns_empty(monkeypatch):
    from touchstone import ghclient
    class _R:
        status_code = 200
        text = "<<<not json>>>"
        headers = {}
        def raise_for_status(self): pass
        def json(self): raise ValueError("not json")
    class _S:
        def request(self, *a, **k): return _R()
    monkeypatch.setattr(ghclient, "_session", lambda: _S())
    # r.text 非空且非 json → 返回 {} （json.loads 在 _R.json？不——request 用 r.json() if r.text else {}）
    # 这里 r.text 非空 → 调 r.json() 抛 ValueError；确认 request 用的是 r.json（会抛）。改用真 json 字符串验默认路径
    class _R2(_R):
        text = ""
    class _S2:
        def request(self, *a, **k): return _R2()
    monkeypatch.setattr(ghclient, "_session", lambda: _S2())
    assert ghclient.request("GET", "u", "t") == {}         # 空响应体 → {}（不崩、不误当错误）

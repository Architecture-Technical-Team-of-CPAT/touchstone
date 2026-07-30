"""可插拔检查框架 checks.py 的离线测试（无网络：转达/发布用打桩）。"""
import copy
import os
import threading
import time

import requests

from touchstone import checks
from touchstone import stack_rules
from helpers import build_diff

# 仅 touchstone-rules 一个必填内置检查（避开 relay/网络）
_ONLY_RULES_CFG = {"gate": {"status_name": "touchstone/gate"},
                   "checks": [{"name": "touchstone-rules", "type": "builtin",
                               "plugin": "touchstone-rules", "required": True}]}


# ---------------- 配置加载 ----------------
def test_load_config_defaults_when_missing(tmp_path):
    cfg = checks.load_config(str(tmp_path))
    assert cfg["gate"]["status_name"] == checks.DEFAULT_GATE
    assert cfg["checks"] == []


def test_load_config_reads_file(tmp_path, monkeypatch):
    p = tmp_path / "checks.yaml"
    p.write_text("gate:\n  status_name: x/gate\nchecks:\n  - name: a\n    required: true\n",
                 encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_CHECKS", str(p))
    cfg = checks.load_config(str(tmp_path))
    assert cfg["gate"]["status_name"] == "x/gate" and cfg["checks"][0]["name"] == "a"


# ---------------- 总闸汇总 ----------------
def _r(name, passed, required):
    return checks.CheckResult(name, passed, "", required)


def test_aggregate_gate_all_required_pass():
    assert checks.aggregate_gate([_r("a", True, True), _r("b", True, True)]) == "success"


def test_aggregate_gate_required_fail():
    assert checks.aggregate_gate([_r("a", True, True), _r("b", False, True)]) == "failure"


def test_aggregate_gate_required_neutral_is_fail():
    assert checks.aggregate_gate([_r("a", None, True)]) == "failure"   # 未知不算通过


def test_aggregate_gate_optional_fail_ok():
    assert checks.aggregate_gate([_r("a", True, True), _r("b", False, False)]) == "success"


def test_aggregate_gate_empty_policy_passes():
    assert checks.aggregate_gate([_r("a", False, False)]) == "success"  # 无 required → 不挡


# ---------------- 内置：touchstone-rules ----------------
def test_touchstone_rules_blocks_on_block_candidate():
    pr = {"contract_findings": [{"rule_id": "CTR-001", "severity": "block_candidate"}]}
    passed, summary = checks._check_touchstone_rules(pr, {})
    assert passed is False and "CTR-001" in summary


def test_touchstone_rules_passes_when_clean():
    pr = {"contract_findings": [{"rule_id": "TEST-001", "severity": "warn", "category": "weak_test"}]}
    passed, _ = checks._check_touchstone_rules(pr, {})
    assert passed is True


def test_touchstone_rules_blocks_on_category_contract():
    """category=contract（即便 severity=warn）也必须阻断——契约类发现走门禁不走建议。"""
    pr = {"contract_findings": [{"rule_id": "CTR-001", "severity": "warn", "category": "contract"}]}
    passed, summary = checks._check_touchstone_rules(pr, {})
    assert passed is False and "CTR-001" in summary


# ---------------- 端到端：确定性栈规则进总闸（F1/F3 回归）----------------
def test_ctr001_reaches_gate_and_blocks(rule_index):
    """CTR-001（破坏性契约变更）经 stack_rules 产出 block_candidate，进总闸 → failure。"""
    diff = build_diff([("src/api/handler.py", ["def breaking(): pass"], True)])
    sf = stack_rules.check_stack_rules(diff, rule_index)
    ctr = next(f for f in sf if f["rule_id"] == "CTR-001")
    assert ctr["severity"] == "block_candidate" and ctr["agent"] == "touchstone-rules"
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t", "files": [], "contract_findings": sf}
    assert checks.aggregate_gate(checks.run_checks(_ONLY_RULES_CFG, pr)) == "failure"


def test_warn_stack_rule_not_enforced_does_not_block(rule_index):
    """SPR-DI-001（warn、未固化）命中但不阻断——顾问式，仅 enforced 后才拦。"""
    diff = build_diff([("Svc.java", ["@Autowired", "private Foo foo;"], True)])
    sf = stack_rules.check_stack_rules(diff, rule_index)
    di = next(f for f in sf if f["rule_id"] == "SPR-DI-001")
    assert di["severity"] == "warn"
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t", "files": [], "contract_findings": sf}
    assert checks.aggregate_gate(checks.run_checks(_ONLY_RULES_CFG, pr)) == "success"


def test_enforced_warn_rule_escalates_to_block(rule_index):
    """被 govern 固化(enforced)的 warn 规则升级为 block_candidate → 阻断。"""
    ri = copy.deepcopy(rule_index)
    ri["SPR-DI-001"]["enforced"] = True
    diff = build_diff([("Svc.java", ["@Autowired", "private Foo foo;"], True)])
    sf = stack_rules.check_stack_rules(diff, ri)
    di = next(f for f in sf if f["rule_id"] == "SPR-DI-001")
    assert di["severity"] == "block_candidate"
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t", "files": [], "contract_findings": sf}
    assert checks.aggregate_gate(checks.run_checks(_ONLY_RULES_CFG, pr)) == "failure"


# ---------------- verify 插件：折入结果 + 可信绿（author 自报规格不算通过）----------
def test_verify_plugin_missing_is_neutral(tmp_path):
    passed, summary = checks._check_verify({}, {"result_file": str(tmp_path / "nope.json")})
    assert passed is None and "未运行" in summary


def test_verify_plugin_rejects_author_proposed_spec(tmp_path):
    import json
    p = tmp_path / "verify-result.json"
    p.write_text(json.dumps({"passed": True, "spec_source": "author_proposed"}), encoding="utf-8")
    passed, _ = checks._check_verify({}, {"result_file": str(p)})
    assert passed is False        # author 自报规格的绿不构成正确性认证


def test_verify_plugin_accepts_human_curated_and_regression(tmp_path):
    import json
    for src in ("human_curated", None):
        p = tmp_path / "verify-result.json"
        p.write_text(json.dumps({"passed": True, "spec_source": src}), encoding="utf-8")
        assert checks._check_verify({}, {"result_file": str(p)})[0] is True
    p.write_text(json.dumps({"passed": False, "spec_source": "human_curated"}), encoding="utf-8")
    assert checks._check_verify({}, {"result_file": str(p)})[0] is False


# ---------------- 转达：读已有 check-run ----------------
def test_relay_reads_existing_check(monkeypatch):
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda *a, **k: {"check_runs": [
                            {"name": "unit", "status": "completed", "conclusion": "success"}]})
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t"}
    passed, summary = checks._run_relay(pr, {"source_check": "unit"})
    assert passed is True and "unit=success" in summary


def test_relay_failure_and_missing(monkeypatch):
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda *a, **k: {"check_runs": [
                            {"name": "unit", "status": "completed", "conclusion": "failure"}]})
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t"}
    assert checks._run_relay(pr, {"source_check": "unit"})[0] is False
    assert checks._run_relay(pr, {"source_check": "nope"})[0] is None    # 未找到 → 中性


# ---------------- 编排：禁用跳过 / 插件隔离 / 发总闸 ----------------
def test_run_checks_skips_disabled_and_isolates_failure(monkeypatch):
    @checks.builtin("boom")
    def _boom(pr, cfg):
        raise RuntimeError("x")

    cfg = {"checks": [
        {"name": "off", "type": "builtin", "plugin": "touchstone-rules", "enabled": False},
        {"name": "crash", "type": "builtin", "plugin": "boom", "required": True}]}
    pr = {"contract_findings": []}
    results = checks.run_checks(cfg, pr)
    assert len(results) == 1 and results[0].name == "crash" and results[0].passed is None
    assert checks.aggregate_gate(results) == "failure"   # 崩了的 required → 总闸 fail


def test_post_gate_posts_single_status(monkeypatch):
    captured = {}

    def fake(method, url, token, data=None):
        captured["method"] = method
        captured["data"] = data
        return {}
    monkeypatch.setattr(checks.ghclient, "request", fake)
    pr = {"owner": "o", "repo": "r", "sha": "abc", "token": "t"}
    cfg = {"gate": {"status_name": "touchstone/gate"}}
    results = [_r("touchstone-rules", True, True), _r("verify", None, False)]
    gate, _ = checks.post_gate(pr, cfg, results)
    assert gate == "success"
    assert captured["method"] == "POST"
    assert captured["data"]["name"] == "touchstone/gate"
    assert captured["data"]["conclusion"] == "success"
    assert captured["data"]["head_sha"] == "abc"


# ---------------- gate CLI：聚合并发总闸 + 写回 touchstone-findings.json ----------
def _gate_cli(tmp_path, monkeypatch, findings):
    import json
    posted = {}
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda method, url, token, data=None, **k: posted.update(data or {}) or {})
    cy = tmp_path / "checks.yaml"
    cy.write_text("gate:\n  status_name: touchstone/gate\n"
                  "checks:\n  - {name: touchstone-rules, type: builtin, plugin: touchstone-rules, required: true}\n",
                  encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_CHECKS", str(cy))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "touchstone-findings.json").write_text(
        json.dumps({"sha": "s", "changed_files": ["a.py"], "findings": findings, "gate": None}),
        encoding="utf-8")
    checks.main()
    co = json.load(open(tmp_path / "touchstone-findings.json", encoding="utf-8"))
    return co, posted


def test_gate_cli_clean_writes_success(tmp_path, monkeypatch):
    co, posted = _gate_cli(tmp_path, monkeypatch, [])
    assert co["gate"] == "success" and posted["conclusion"] == "success"


def test_gate_cli_contract_block_writes_failure(tmp_path, monkeypatch):
    co, posted = _gate_cli(tmp_path, monkeypatch,
                           [{"agent": "contract-check", "rule_id": "CTR-001", "severity": "block_candidate"}])
    assert co["gate"] == "failure" and posted["conclusion"] == "failure"


# ============ required 接力检查 fail-closed（skipped 不算过）回归 ============
def _mk_relay_gh(monkeypatch, conclusion):
    monkeypatch.setattr(checks.ghclient, "paginate_check_runs",
        lambda *a, **k: {"check_runs": [
            {"name": "unit", "status": "completed", "conclusion": conclusion}]})

def test_relay_required_skipped_fails_closed(monkeypatch):
    """required 的接力检查，源 CI 被跳过（[skip ci]/路径过滤）不能算过——否则总闸被绕。"""
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t"}
    _mk_relay_gh(monkeypatch, "skipped")
    assert checks._run_relay(pr, {"source_check": "unit", "required": True})[0] is False
    _mk_relay_gh(monkeypatch, "neutral")
    assert checks._run_relay(pr, {"source_check": "unit", "required": True})[0] is False

def test_relay_non_required_skipped_still_ok(monkeypatch):
    """非 required 保持宽松（兼容既有流水线）；required 可用 allow_skipped 显式放宽。"""
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t"}
    _mk_relay_gh(monkeypatch, "skipped")
    assert checks._run_relay(pr, {"source_check": "unit"})[0] is True
    assert checks._run_relay(pr, {"source_check": "unit",
                                  "required": True, "allow_skipped": True})[0] is True


# ============ 防静默故障：坏配置 fail-closed（B）+ findings 缺失显式 failure（A）============
def test_load_config_malformed_yaml_is_config_error(tmp_path, monkeypatch):
    # 文件存在但 YAML 坏 → 标 _config_error（不当成空策略静默放行）
    p = tmp_path / "checks.yaml"
    p.write_text("gate: [unclosed\n  - : :\n", encoding="utf-8")   # 故意非法 YAML
    monkeypatch.setenv("TOUCHSTONE_CHECKS", str(p))
    cfg = checks.load_config(str(tmp_path))
    assert cfg.get("_config_error")


def test_load_config_unreadable_path_is_config_error(tmp_path, monkeypatch):
    # 路径存在但不可读（指向目录→IsADirectoryError；权限拒→PermissionError；均 OSError 子类、
    # 非 FileNotFoundError）= 配置坏了，须 fail-closed（与坏 YAML 同语义），不能映射成空策略静默放行。
    # 旧 bare `except OSError: data={}` 把这类降级成空策略 → aggregate_gate([]) → gate "success"
    # （与 YAMLError 的 fail-closed 自相矛盾）。本测锁死修复。
    d = tmp_path / "checks.yaml"
    d.mkdir()                       # TOUCHSTONE_CHECKS 指向目录 → open() 抛 IsADirectoryError(OSError)
    monkeypatch.setenv("TOUCHSTONE_CHECKS", str(d))
    cfg = checks.load_config(str(tmp_path))
    assert cfg.get("_config_error"), "不可读 checks.yaml 应标 _config_error（fail-closed），非空策略"


def test_post_gate_config_error_fails_closed(monkeypatch):
    posted = {}
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda method, url, token, data=None, **k: posted.update(data or {}) or {})
    pr = {"owner": "o", "repo": "r", "sha": "s", "token": "t"}
    cfg = {"gate": {"status_name": "touchstone/gate"},
           "_config_error": "checks.yaml 解析失败（boom）"}
    gate, _ = checks.post_gate(pr, cfg, [])                       # 空结果 + 坏配置
    assert gate == "failure"
    assert posted["conclusion"] == "failure"
    assert "checks.yaml 解析失败" in posted["output"]["summary"]  # summary 显式报警


def test_gate_cli_missing_findings_posts_failure(tmp_path, monkeypatch):
    # A：touchstone-findings.json 缺失 → 不静默 no-op，发 failure check-run 说明情况
    import json
    posted = {}
    monkeypatch.setattr(checks.ghclient, "request",
                        lambda method, url, token, data=None, **k: posted.update(data or {}) or {})
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("TOUCHSTONE_HEAD_SHA", "deadbee")
    monkeypatch.setenv("REPO_DIR", str(tmp_path))                 # 无 checks.yaml → 默认 gate 名
    monkeypatch.chdir(tmp_path)                                   # cwd 无 touchstone-findings.json
    checks.main()                                                 # 不抛、不静默 return
    assert posted.get("conclusion") == "failure"
    assert posted.get("head_sha") == "deadbee"
    assert "未产出结果" in posted["output"]["summary"]


# ============ service 类检查并行编排（慢检查并行；builtin/relay 仍串行）============
_PR = {"owner": "o", "repo": "r", "sha": "s", "token": "t", "files": []}


class _FakeResp:
    """假 requests.Response：只实现 _run_service 用到的 raise_for_status / json。"""
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _concurrent_post(active, payload=None, sleep=0.05):
    """造一个会记并发数 + sleep 的 requests.post 打桩，用于证真并行 / 测上限。"""
    lock = threading.Lock()

    def fake_post(url, json=None, timeout=None, **k):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(sleep)                 # 拉长到足以让另一个线程同时进入
        with lock:
            active["n"] -= 1
        return _FakeResp(payload if payload is not None else {"passed": True, "summary": url})
    return fake_post


def test_service_checks_run_in_parallel(monkeypatch):
    """两个 service 检查并行（峰值并发==2）；串行实现会是 1。结果仍按配置顺序。"""
    active = {"n": 0, "max": 0}
    monkeypatch.setattr(checks.requests, "post", _concurrent_post(active))
    cfg = {"checks": [
        {"name": "s1", "type": "service", "url": "http://a", "required": True},
        {"name": "s2", "type": "service", "url": "http://b", "required": True}]}
    results = checks.run_checks(cfg, _PR)
    assert active["max"] == 2                       # 铁证：两 service 真并行（串行会是 1）
    assert [r.name for r in results] == ["s1", "s2"]   # 顺序 = 配置顺序
    assert all(r.passed is True for r in results)


def test_service_failure_isolated_under_parallelism(monkeypatch):
    """一个 service 抛异常 → 记中性（插件隔离），不拖垮另一个 service、不波及总闸对其余的判定。"""
    def fake_post(url, json=None, timeout=None, **k):
        if "crash" in url:
            raise requests.ConnectionError("boom")
        return _FakeResp({"passed": True, "summary": "ok"})
    monkeypatch.setattr(checks.requests, "post", fake_post)
    cfg = {"checks": [
        {"name": "crash", "type": "service", "url": "http://crash", "required": True},
        {"name": "ok", "type": "service", "url": "http://ok", "required": True}]}
    results = checks.run_checks(cfg, _PR)
    by = {r.name: r for r in results}
    assert by["crash"].passed is None and "插件异常" in by["crash"].summary
    assert by["ok"].passed is True                  # crash 没连累 ok
    assert checks.aggregate_gate(results) == "failure"   # required crash 中性 → 总闸 fail


def test_service_passed_string_false_is_not_passed(monkeypatch):
    """service 把 passed 写成字符串 'false' 时不得当通过——回归锁。

    bug（_run_service 曾 `bool(d.get('passed'))`）：`bool('false') == True` 把「失败」误判为
    「通过」→ required service 假放行总闸。改回 bool() 即红（变异杀红）。"""
    def fake_post(payload):
        def _p(url, json=None, timeout=None, **k):
            return _FakeResp(payload)
        return _p
    monkeypatch.setattr(checks.requests, "post", fake_post({"passed": "false", "summary": "tests failed"}))
    passed, summary = checks._run_service(_PR, {"url": "http://svc"})
    assert passed is False, "字符串 'false' 须判失败（bool('false')==True 假放行 bug）"
    assert summary == "tests failed"
    # 真值字符串（大小写/变体）仍通过
    monkeypatch.setattr(checks.requests, "post", fake_post({"passed": "True"}))
    assert checks._run_service(_PR, {"url": "x"})[0] is True
    # 非白名单字符串 fail-closed（门禁对模糊输入不 lenient 放行）
    monkeypatch.setattr(checks.requests, "post", fake_post({"passed": "ok"}))
    assert checks._run_service(_PR, {"url": "x"})[0] is False


def test_service_order_preserved_when_interleaved_with_builtin(monkeypatch):
    """config = [builtin, service, builtin]：service 结果按配置位置回填，顺序不被并行打乱。"""
    monkeypatch.setattr(checks.requests, "post", _concurrent_post({"n": 0, "max": 0}, sleep=0.01))
    cfg = {"checks": [
        {"name": "b1", "type": "builtin", "plugin": "touchstone-rules", "required": True},
        {"name": "svc", "type": "service", "url": "http://x", "required": False},
        {"name": "b2", "type": "builtin", "plugin": "touchstone-rules", "required": True}]}
    results = checks.run_checks(cfg, dict(_PR, contract_findings=[]))
    assert [r.name for r in results] == ["b1", "svc", "b2"]
    assert results[1].passed is True                # service 在中间位、结果正确回填


def test_service_concurrency_capped(monkeypatch):
    """超过 _MAX_SERVICE_WORKERS 个 service：并发被上限压住（不无限起线程），但仍并行。"""
    active = {"n": 0, "max": 0}
    monkeypatch.setattr(checks.requests, "post", _concurrent_post(active, sleep=0.03))
    n = checks._MAX_SERVICE_WORKERS + 4
    cfg = {"checks": [{"name": f"s{i}", "type": "service", "url": f"http://x{i}", "required": True}
                      for i in range(n)]}
    results = checks.run_checks(cfg, _PR)
    assert 2 <= active["max"] <= checks._MAX_SERVICE_WORKERS   # 并行了且有上限
    assert len(results) == n
    assert all(r.passed is True for r in results)


def test_disabled_service_skipped(monkeypatch):
    """enabled=False 的 service 不进线程池、不产结果（与 builtin 跳过同语义）。"""
    active = {"n": 0, "max": 0}
    monkeypatch.setattr(checks.requests, "post", _concurrent_post(active))
    cfg = {"checks": [
        {"name": "on", "type": "service", "url": "http://a", "required": True},
        {"name": "off", "type": "service", "url": "http://b", "enabled": False, "required": True},
        {"name": "on2", "type": "service", "url": "http://c", "required": True}]}
    results = checks.run_checks(cfg, _PR)
    assert [r.name for r in results] == ["on", "on2"]   # off 被跳过、不进结果
    assert active["max"] <= 2                            # 没把 disabled 也并发进去


def test_scope_rules_corrupt_warns_missing_silent(tmp_path, capsys):
    # P2-1：可选配置【缺失】= 常态静默；【损坏】= 回落默认但必须可见（防静默变粗）
    from touchstone import contract_check as CC
    # 缺失：无告警
    rules = CC.load_scope_rules(str(tmp_path))
    assert rules and "scope-rules 加载失败" not in capsys.readouterr().err
    # 损坏：回落默认 + stderr 可见
    d = tmp_path / ".touchstone"; d.mkdir()
    (d / "scope-rules.yaml").write_text(": :\n  - [", encoding="utf-8")
    rules2 = CC.load_scope_rules(str(tmp_path))
    assert rules2 == rules                      # 回落内置默认
    assert "scope-rules 加载失败" in capsys.readouterr().err


def test_verify_plugin_malformed_passed_failclosed(tmp_path, monkeypatch):
    # verify-result.json 由执行 PR 代码的零密 job 产出（攻击者可影响）：畸形字符串
    # （bool() 恒真）必须 fail-closed 判 False——SECURITY.md 信任边界的代码化。
    import json as _json
    monkeypatch.chdir(tmp_path)
    for v, expect in [("ok", False), ("passed", False), ("true", True),
                      (True, True), (False, False), (None, False), (1, True)]:
        (tmp_path / "verify-result.json").write_text(
            _json.dumps({"passed": v, "spec_source": "contract"}), encoding="utf-8")
        got, _summary = checks._BUILTINS["verify"]({}, {})
        assert got is expect, (v, got)
    # author 自报规格的绿不算过（既有规则回归锚）
    (tmp_path / "verify-result.json").write_text(
        _json.dumps({"passed": True, "spec_source": "author_proposed"}), encoding="utf-8")
    got, _ = checks._BUILTINS["verify"]({}, {})
    assert got is False


def test_verify_plugin_non_dict_json_failclosed(tmp_path, monkeypatch):
    # PRA-POSSIBLE_ISSUE(#104 round-3)：verify-result.json 非 dict（数组/标量/字符串，
    # 攻击者可影响）时 d.get 崩插件。须 isinstance 兜底→中性（fail-closed：required 时总闸 fail），
    # 不靠 run_checks 的 except Exception 兜控制流、也不留崩栈。
    import json as _json
    monkeypatch.chdir(tmp_path)
    for payload in ("[1, 2, 3]", '"passed"', "42", "null", "true"):
        (tmp_path / "verify-result.json").write_text(payload, encoding="utf-8")
        got, summary = checks._BUILTINS["verify"]({}, {})
        assert got is None, payload                 # 中性、不崩
        assert "非对象" in summary, payload          # 明示、非崩栈
    # 对照：合法 dict 仍正常解析（回归锚）
    (tmp_path / "verify-result.json").write_text(
        _json.dumps({"passed": True, "spec_source": "human_curated"}), encoding="utf-8")
    assert checks._BUILTINS["verify"]({}, {})[0] is True

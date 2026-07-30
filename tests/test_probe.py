"""Probe（测试有效性探针）测试：§3 数据结构 → census → plan(含哨兵) → run/replay → to_findings。
静态部分纯内存快测；run/replay 各一条真跑 pytest 的端到端（hermetic 临时工程）。"""
import os
import sys
import textwrap
import pytest

from touchstone import probe
from touchstone import checklist


# ---------- 夹具：临时 Python 工程（贯穿案例 semver_range 的最小复刻）----------
_SRC = '''\
def parse_version(s):
    parts = s.split(".")
    if len(parts) < 3:
        raise ValueError(s)
    return tuple(int(p) for p in parts)

def resolve_range(spec, versions):
    if not spec:
        raise ValueError(spec)
    lower, upper = spec[0], spec[1]
    out = []
    for v in versions:
        if v >= lower and v < upper:
            out.append(v)
    return out
'''

def _mk_project(tmp_path, test_body):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "semver_range.py").write_text(_SRC, encoding="utf-8")
    (tmp_path / "tests" / "test_semver_range.py").write_text(textwrap.dedent(test_body), encoding="utf-8")
    return {"_repo_dir": str(tmp_path),
            "changed_files": [{"path": "pkg/semver_range.py", "added": 20, "hunks": [[1, 20, 0]]},
                              {"path": "tests/test_semver_range.py"}]}

_STRONG = '''
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from pkg.semver_range import parse_version, resolve_range
    def test_parse_version_basic():
        assert parse_version("1.2.3") == (1, 2, 3)
    def test_resolve_range_values():
        assert resolve_range([1, 5], [0, 1, 3, 5, 6]) == [1, 3]
'''
_WEAK = '''
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from pkg.semver_range import parse_version, resolve_range
    def test_parse_version_basic():
        assert parse_version("1.2.3") == (1, 2, 3)
    def test_resolve_range_runs():
        assert resolve_range([1, 5], [0, 1, 3, 5, 6]) is not None
'''

def _tc(sf):
    return probe.TestCommand(cmd=f"{sys.executable} -m pytest -q tests/test_semver_range.py", cwd=sf["_repo_dir"])


# ============================ §3 数据结构 ============================
def test_report_never_silent_counts_present():
    r = probe.ProbeRunReport(3, 3, probe.VerdictKind.KILLED, [], [], "ok", 0.5)
    for fld in ("plan_size", "executed", "sentinel_result", "verdicts", "census", "status", "kill_rate"):
        assert hasattr(r, fld)                       # 强制计数：任一缺失即报告非法

def test_fingerprint_stable_and_prefixed():
    a = probe.content_fingerprint("p.py", "f(...)", "CMP", "L3:x < y")
    assert a == probe.content_fingerprint("p.py", "f(...)", "CMP", "L3:x < y")   # 稳定
    assert a.startswith("mut-")
    assert probe.content_fingerprint("p.py", "f(...)", "CMP", "L3", sentinel=True).startswith("snt-")


# ============================ census（L0 静态）============================
def test_census_four_failure_modes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(textwrap.dedent('''
        import pytest
        def test_zero():
            y = compute()
        def test_trivial():
            assert True
        @pytest.mark.skip(reason="wip")
        def test_skipped():
            assert compute() == 1
        def test_swallow():
            try:
                risky()
            except Exception:
                pass
        def test_real():
            assert compute() == 2
    '''), encoding="utf-8")
    sf = {"_repo_dir": str(tmp_path), "changed_files": [{"path": "tests/test_x.py"}]}
    kinds = {i.kind for i in probe.census(sf, str(tmp_path))}
    assert {"zero_assertion", "trivial_assertion", "skip_counted_as_pass", "swallowed_exception"} <= kinds
    names = {i.test_name for i in probe.census(sf, str(tmp_path))}
    assert "test_real" not in names                  # 真断言不误报


# ============================ plan（含哨兵）============================
def test_plan_covers_operators_and_appends_sentinel(tmp_path):
    sf = _mk_project(tmp_path, _WEAK)
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=8), [])
    ops = {m.operator for m in ms if not m.is_sentinel}
    assert {"CMP", "BOOL", "RET", "EXC"} <= ops       # 覆盖最小高价值算子集
    sentinels = [m for m in ms if m.is_sentinel]
    assert len(sentinels) == 1 and sentinels[0].operator.startswith("SENTINEL")

def test_plan_respects_budget(tmp_path):
    sf = _mk_project(tmp_path, _WEAK)
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=3), [])
    assert len(ms) <= 3                               # 含哨兵不超预算

def test_plan_empty_on_non_code_diff(tmp_path):
    sf = {"_repo_dir": str(tmp_path), "changed_files": [{"path": "README.md", "hunks": [[1, 5, 0]]}]}
    assert probe.plan(sf, probe.ProbeBudget(), []) == []


# ============================ run / replay（动态·真跑 pytest）============================
@pytest.mark.slow
def test_run_weak_tests_yield_survivors_sentinel_killed(tmp_path):
    sf = _mk_project(tmp_path, _WEAK)
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=8), [])
    rep = probe.run(ms, _tc(sf), probe.ProbeBudget(max_mutants=8, per_mutant_timeout_s=60), [])
    assert rep.status == "ok"
    assert rep.sentinel_result is probe.VerdictKind.KILLED          # 链路自检通过
    assert any(v.kind is probe.VerdictKind.SURVIVED for v in rep.verdicts)   # 弱测试放过了变异
    assert rep.kill_rate is not None
    # 工作区恢复
    assert "v >= lower and v < upper" in (tmp_path / "pkg" / "semver_range.py").read_text()

@pytest.mark.slow
def test_run_invalid_when_sentinel_survives(tmp_path):
    sf = _mk_project(tmp_path, "\n    def test_nothing():\n        assert True\n")   # 无真测试→哨兵必存活
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=6), [])
    assert any(m.is_sentinel for m in ms)   # 哨兵必须存在（R1-07：前提失效应红，不应静默 skip）
    rep = probe.run(ms, _tc(sf), probe.ProbeBudget(max_mutants=6, per_mutant_timeout_s=60), [])
    assert rep.status == "invalid"
    assert rep.sentinel_result is not probe.VerdictKind.KILLED
    assert rep.kill_rate is None and rep.verdicts == ()             # 判决作废

@pytest.mark.slow
def test_replay_kills_after_strong_test(tmp_path):
    sf = _mk_project(tmp_path, _WEAK)
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=8), [])
    rep = probe.run(ms, _tc(sf), probe.ProbeBudget(max_mutants=8, per_mutant_timeout_s=60), [])
    # 选一个【值路径】存活者（CMP/BOOL/RET）——强测试断言返回值即能击杀；
    # EXC/空 spec 等未覆盖路径的存活是探针的正确产出，但不适合演示 replay 闭环。
    val_ops = {m.mutant_id: m for m in ms if m.operator in ("CMP", "BOOL", "RET")}
    survivor = next(v for v in rep.verdicts
                    if v.kind is probe.VerdictKind.SURVIVED and v.mutant_id in val_ops)
    mutant = val_ops[survivor.mutant_id]
    # 换成强测试后，同一变异体应被击杀（done_criteria 闭环）
    (tmp_path / "tests" / "test_semver_range.py").write_text(textwrap.dedent(_STRONG), encoding="utf-8")
    v = probe.replay(mutant, _tc(sf))
    assert v.kind is probe.VerdictKind.KILLED


# ============================ to_findings 接线 ============================
def test_to_findings_survived_shape_and_checklist_compat():
    rep = probe.ProbeRunReport(
        2, 2, probe.VerdictKind.KILLED,
        [probe.Verdict("mut-abc", probe.VerdictKind.SURVIVED, None, 1.0, "pkg/a.py", 12)],
        [], "ok", 0.0)
    fs = probe.to_findings(rep)
    f = fs[0]
    assert f["rule_id"] == "PROBE-SURVIVED" and f["file"] == "pkg/a.py" and f["line"] == 12
    assert f["done_criteria"] == {"kind": "deterministic", "spec": {"replay_mutant": "mut-abc"}}
    assert f["mutant_id"] == "mut-abc" and f["agent"] == "probe"
    assert checklist.from_findings(fs) is not None                  # checklist 能消费

def test_to_findings_invalid_is_p0_not_survived():
    rep = probe.ProbeRunReport(3, 1, probe.VerdictKind.SURVIVED, [], [], "invalid", None,
                               reason="哨兵未被击杀")
    fs = probe.to_findings(rep)
    assert len(fs) == 1 and fs[0]["rule_id"] == "PROBE-INVALID" and fs[0]["severity"] == "P0"

def test_to_findings_plan_empty_is_explicit_not_silent():
    rep = probe.ProbeRunReport(0, 0, probe.VerdictKind.INFRA_ERROR, [], [], "plan_empty", None,
                               reason="无可变异目标")
    fs = probe.to_findings(rep)
    assert len(fs) == 1 and fs[0]["rule_id"] == "PROBE-EMPTY"       # 没做事也显式汇报

def test_to_findings_killed_produces_no_finding():
    rep = probe.ProbeRunReport(2, 2, probe.VerdictKind.KILLED,
        [probe.Verdict("mut-k", probe.VerdictKind.KILLED, "t::x", 1.0, "pkg/a.py", 3)], [], "ok", 1.0)
    assert probe.to_findings(rep) == []                             # 击杀=好事，不产 Finding


# ============================ Round-1 销项回归（PR #133）============================
def test_run_invalid_when_no_sentinel_in_nonempty_plan(tmp_path):
    """R1-01：非空 plan 无哨兵 ⇒ invalid（链路自检不可用，绝不 fail-open 绿灯）。"""
    m = probe.Mutant("mut-nosent", "pkg/x.py", "f(...)", "CMP",
                     probe.SourceSpan("pkg/x.py", 1, 1), "a<b", "a<=b")
    rep = probe.run([m], probe.TestCommand(cmd="python -c pass", cwd=str(tmp_path)),
                    probe.ProbeBudget(per_mutant_timeout_s=5, total_timeout_s=10), [])
    assert rep.status == "invalid"
    assert rep.verdicts == () and rep.kill_rate is None
    assert "哨兵" in rep.reason


def test_plan_prioritizes_low_density_covered_module(tmp_path):
    """R1-02：同分支数的两个模块，被弱测试（census 命中）覆盖者优先被选点；
    哨兵不落在低密度覆盖面内。"""
    (tmp_path / "pkg").mkdir(); (tmp_path / "tests").mkdir()
    fn_src = "def f(a, b):\n    if a < b:\n        return a\n    return b\n"
    (tmp_path / "pkg" / "mod_weak.py").write_text(fn_src, encoding="utf-8")
    (tmp_path / "pkg" / "mod_strong.py").write_text(fn_src.replace("f(", "g("), encoding="utf-8")
    (tmp_path / "tests" / "test_weak.py").write_text(
        "from pkg.mod_weak import f\ndef test_f_runs():\n    assert f(1, 2) is not None\n", encoding="utf-8")
    sf = {"_repo_dir": str(tmp_path),
          "changed_files": [{"path": "pkg/mod_weak.py", "hunks": [[1, 4, 0]]},
                            {"path": "pkg/mod_strong.py", "hunks": [[1, 4, 0]]},
                            {"path": "tests/test_weak.py"}]}
    issues = [probe.CensusIssue("trivial_assertion", "tests/test_weak.py", "test_f_runs", "仅弱断言")]
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=3), issues)   # cap=2：只装得下高优先的
    real_paths = [m.path for m in ms if not m.is_sentinel]
    assert real_paths and all(p == "pkg/mod_weak.py" for p in real_paths)   # 弱覆盖模块排前
    sentinels = [m for m in ms if m.is_sentinel]
    assert sentinels and sentinels[0].path == "pkg/mod_strong.py"           # 哨兵避开低密度面


def test_mutation_sites_skip_docstrings():
    """R1-03：docstring / 裸字符串语句不作 CONST 位点（必然等价变异体）。"""
    import ast as _ast
    src = 'def f(x):\n    "doc here"\n    s = "real"\n    return x + 1\n'
    fn = _ast.parse(src).body[0]
    sites = probe._mutation_sites(fn, src, "p.py")
    originals = [s[1] for s in sites if s[0] == "CONST"]
    assert '"doc here"' not in originals
    assert '"real"' in originals                                            # 真实字符串常量仍是位点


def test_mutation_sites_boolop_multi_operand_flips_all():
    """133-6：多操作数 BoolOp `a and b and c` 翻全部 and→or。
    原 seg.replace(...,1) 只翻首个 → `a or b and c` 语义错位（And→Or 应整组翻）。"""
    import ast as _ast
    src = 'def f(a, b, c):\n    if a and b and c:\n        return 1\n'
    fn = _ast.parse(src).body[0]
    bool_sites = [s for s in probe._mutation_sites(fn, src, "p.py") if s[0] == "BOOL"]
    assert bool_sites, "应至少有一个 BOOL 位点"
    orig, mut = bool_sites[0][1], bool_sites[0][2]
    assert orig.count(" and ") >= 2                       # 多操作数前提
    assert " and " not in mut                             # 全翻，无残留
    assert mut.replace(" or ", " and ") == orig           # 全 or→and 可逆回原文


def test_node_byte_span_pins_correct_occurrence():
    """133-3/4/5：col_offset 精确字节定位。同名片段 'a and b' 在 if 行与 return 行各出现一次时，
    _node_byte_span 按 AST 节点的 (line, col) 各自锁定正确那处，不靠文本查找误中它行。"""
    import ast as _ast
    src = 'def f(a, b):\n    if a and b:\n        return a and b\n'
    fn = _ast.parse(src).body[0]
    bool_sites = [s for s in probe._mutation_sites(fn, src, "p.py") if s[0] == "BOOL"]
    assert len(bool_sites) == 2
    assert bool_sites[0][3].start_line == 2 and bool_sites[1][3].start_line == 3
    src_b = src.encode("utf-8")
    for span, expect_line in ((bool_sites[0][3], 2), (bool_sites[1][3], 3)):
        bs = probe._node_byte_span(src_b, span)
        assert bs is not None
        assert src_b[bs[0]:bs[1]].decode() == "a and b"                   # 切片恰为该位点原文
        assert src_b[:bs[0]].count(b"\n") + 1 == expect_line             # 落在 AST 节点所在行


def test_node_byte_span_returns_none_on_invalid_lines():
    """#137 review：行列越界/倒序/空源 → 早返回 None 触发 _find_in_window 兜底，不静默 clamp 出错位区间。
    锁：end_line 超过文件实际行数不再被 min() 偷偷夹回，而是 None（graceful fallback）。"""
    src_b = b"x = 1\ny = 2\n"                       # 2 行
    Span = probe.SourceSpan
    # start_line 越界
    assert probe._node_byte_span(src_b, Span("p", 99, 99, 0, 1)) is None
    # end_line 超出文件行数（旧 min() 会夹回 2，现返回 None）
    assert probe._node_byte_span(src_b, Span("p", 1, 99, 0, 1)) is None
    # end_line < start_line（倒序）
    assert probe._node_byte_span(src_b, Span("p", 2, 1, 0, 1)) is None
    # 空源
    assert probe._node_byte_span(b"", Span("p", 1, 1, 0, 1)) is None
    # 合法单行仍返有效区间
    bs = probe._node_byte_span(src_b, Span("p", 1, 1, 0, 3))
    assert bs is not None and src_b[bs[0]:bs[1]] == b"x ="



def test_inject_and_run_raises_on_restore_failure(tmp_path, monkeypatch):
    """133-7：_inject_and_run 恢复失败 → 抛 WorkspaceCorrupted（run 据此判 INVALID 中止防级联）。"""
    sf = _mk_project(tmp_path, _STRONG)
    mutant = next(m for m in probe.plan(sf, probe.ProbeBudget(max_mutants=8), []) if not m.is_sentinel)
    real_open = open
    def flaky(path, mode="r", *a, **kw):
        if mode == "wb":                                  # 恢复走 "wb"：模拟失败
            raise OSError("simulated restore failure")
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr(probe, "open", flaky, raising=False)
    tc = probe.TestCommand(cmd="python -c pass", cwd=sf["_repo_dir"])
    with pytest.raises(probe.WorkspaceCorrupted):
        probe._inject_and_run(mutant, tc, 60)


def test_run_invalid_when_restore_fails(tmp_path, monkeypatch):
    """133-7 端到端：恢复失败 → run() 判 INVALID 中止（不继续跑后续 mutant 以免基线被脏工作区污染）。"""
    sf = _mk_project(tmp_path, _STRONG)
    ms = probe.plan(sf, probe.ProbeBudget(max_mutants=8), [])
    real_open = open
    def flaky(path, mode="r", *a, **kw):
        if mode == "wb":
            raise OSError("simulated restore failure")
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr(probe, "open", flaky, raising=False)
    tc = probe.TestCommand(cmd="python -c pass", cwd=sf["_repo_dir"])
    rep = probe.run(ms, tc, probe.ProbeBudget(max_mutants=8, per_mutant_timeout_s=60), [])
    assert rep.status == "invalid"
    assert "恢复失败" in (rep.reason or "")


# ============================ Round-3/4 销项回归（PR #133/#134）============================
def test_census_no_false_positive_on_assert_in_except(tmp_path):
    """TS3-01：except 内 unittest 断言方法（self.assertEqual 等）是合法异常断言模式，
    不得误报 swallowed_exception；真吞错仍要抓。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_exc_detail(self):\n"
        "        try:\n"
        "            risky()\n"
        "        except ValueError as e:\n"
        "            self.assertEqual(str(e), 'expected')\n"
        "    def test_true_swallow(self):\n"
        "        try:\n"
        "            risky()\n"
        "        except Exception:\n"
        "            pass\n"
        "        assert done() == 1\n", encoding="utf-8")
    sf = {"_repo_dir": str(tmp_path), "changed_files": [{"path": "tests/test_x.py"}]}
    swallowed = {i.test_name for i in probe.census(sf, str(tmp_path)) if i.kind == "swallowed_exception"}
    assert "test_exc_detail" not in swallowed          # 合法模式不误报
    assert "test_true_swallow" in swallowed            # 真吞错仍命中


def test_verdict_invariant_killed_requires_killing_test():
    """TS4-01：KILLED 判决必须携带 killing_test——注释约定升级为结构硬校验。"""
    with pytest.raises(ValueError):
        probe.Verdict("mut-x", probe.VerdictKind.KILLED, None, 1.0)
    v = probe.Verdict("mut-x", probe.VerdictKind.KILLED, "t::case", 1.0)
    assert v.killing_test == "t::case"


def test_report_status_is_enum_and_str_compatible():
    """TS4-02：status 为 ReportStatus 枚举；str 混入保证与既有字面量比较兼容。"""
    assert probe.ReportStatus.OK == "ok" and probe.ReportStatus.INVALID == "invalid"
    assert set(probe.ReportStatus) == {probe.ReportStatus.OK, probe.ReportStatus.PLAN_EMPTY,
                                       probe.ReportStatus.INVALID}


def test_run_requires_census_issues_positionally(tmp_path):
    """TS4-03：census_issues 必传——可选默认会静默丢失 L0 检测层。"""
    import inspect
    sig = inspect.signature(probe.run)
    assert sig.parameters["census_issues"].default is inspect.Parameter.empty


def test_report_containers_are_immutable_tuples():
    """TS4-04：frozen 报告的 verdicts/census 归一为 tuple——不可变契约覆盖容器层。"""
    rep = probe.ProbeRunReport(1, 1, probe.VerdictKind.KILLED,
        [probe.Verdict("mut-k", probe.VerdictKind.KILLED, "t::x", 1.0)], [],
        probe.ReportStatus.OK, 1.0)
    assert isinstance(rep.verdicts, tuple) and isinstance(rep.census, tuple)
    assert not hasattr(rep.verdicts, "append")   # tuple 无 append，事后篡改判决被结构性阻断


def test_report_invariants_reject_contradictory_states():
    """TS4-05：报告自身不变量硬校验——矛盾报告不允许被构造（与 Verdict 同款）。"""
    K, S = probe.VerdictKind, probe.ReportStatus
    with pytest.raises(ValueError):
        probe.ProbeRunReport(0, 0, K.INFRA_ERROR, [], [], S.OK, None)          # 空计划不得报 ok
    with pytest.raises(ValueError):
        probe.ProbeRunReport(3, 3, K.SURVIVED, [], [], S.OK, 0.5)              # 哨兵未杀不得报 ok
    with pytest.raises(ValueError):
        probe.ProbeRunReport(3, 1, K.SURVIVED, [], [], S.INVALID, 0.5)         # invalid 不得带 kill_rate
    r = probe.ProbeRunReport(3, 3, K.KILLED, [], [], "ok", 0.5)                # 字面量构造 → 枚举归一
    assert r.status is S.OK

"""质量门禁命门：runner 可插拔 + 重构 regression_only。"""
import os

import pytest
from verify import verify_change as V
from verify import runners as R


# ---------------- select_runner / is_refactor ----------------
def test_select_runner_maven_on_pom(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert V.select_runner(str(tmp_path), []).lang == "maven"


def test_select_runner_maven_on_java(tmp_path):
    assert V.select_runner(str(tmp_path), ["x/Foo.java"]).lang == "maven"


def test_select_runner_python_default(tmp_path):
    assert V.select_runner(str(tmp_path), ["x/foo.py"]).lang == "python"


def test_select_runner_none_for_unsupported_language(tmp_path):
    assert V.select_runner(str(tmp_path), ["x/main.go"]) is None
    assert V.select_runner(str(tmp_path), ["x/app.ts"]) is None
    assert V.select_runner(str(tmp_path), []) is None          # 空改动也 None，不再误判 python


def test_verify_change_unsupported_language_is_neutral(tmp_path):
    res = V.verify_change(str(tmp_path), {}, ["x/main.go"], "b", "h", "targeted_tests",
                          {"base_url": "u", "api_key": "k", "model": "m"}, "")
    assert res.passed is None and res.mode == "unsupported"


def test_is_refactor():
    assert V.is_refactor({}, "refactor(openjiuwen): extract memory runtime rail")
    assert V.is_refactor({"intent": "重构 MemoryRuntimeRail"}, "")
    assert not V.is_refactor({"intent": "add new feature"}, "feat: x")


# ---------------- JaCoCo / PIT 解析 ----------------
def test_jacoco_changed_coverage(tmp_path):
    jd = tmp_path / "mod/target/site/jacoco"
    jd.mkdir(parents=True)
    (jd / "jacoco.xml").write_text(
        '<report><package name="p"><sourcefile name="Foo.java">'
        '<counter type="INSTRUCTION" missed="5" covered="20"/>'
        '<counter type="LINE" missed="2" covered="8"/></sourcefile></package></report>')
    cov = V._jacoco_changed_coverage(str(tmp_path), ["a/p/Foo.java", "b/README.md"])
    assert abs(cov - 0.8) < 1e-9


def test_pit_score(tmp_path):
    pd = tmp_path / "mod/target/pit-reports/202606"
    pd.mkdir(parents=True)
    (pd / "mutations.xml").write_text(
        '<mutations><mutation status="KILLED"/><mutation status="TIMED_OUT"/>'
        '<mutation status="SURVIVED"/></mutations>')
    assert abs(V._pit_score(str(tmp_path)) - 2 / 3) < 1e-9


def test_extract_java_signatures(tmp_path):
    (tmp_path / "Bar.java").write_text("public final class Bar {\n  public void doIt(int x){}\n}\n")
    sig = V._extract_java_signatures(str(tmp_path), ["Bar.java"])
    assert "class Bar" in sig and "method doIt" in sig


# ---------------- regression_only 编排（桩 runner + worktree）----------------
class _FakeRunner:
    lang = "maven"
    supports_spec_blind = False

    def __init__(self, suite=True, cov=0.9, mut=None):
        self._s, self._c, self._m = suite, cov, mut

    def run_suite(self, wd):
        return (self._s, "suite-out")

    def changed_coverage(self, wd, cf, changed_lines=None):
        return self._c

    def mutation(self, wd, cf, test_code=None):
        return self._m

    def extract_interface(self, wd, cf):
        return "iface"

    def run_generated(self, wd, code):
        return (True, "gen-out")

    def cover_generated(self, wd, code, cf, changed_lines=None):
        return self._c


@pytest.fixture
def stub_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_worktree", lambda repo, ref: str(tmp_path))
    monkeypatch.setattr(V, "_rm_worktree", lambda repo, d: None)


def test_regression_pass_when_green_and_covered(stub_worktree):
    r = V._verify_regression(".", _FakeRunner(True, 0.9), ["a/Foo.java"], "b", "h", "targeted_tests")
    assert r.passed and r.adequacy.verdict == "adequate" and r.mode == "regression_only"


def test_regression_fail_when_suite_red(stub_worktree):
    r = V._verify_regression(".", _FakeRunner(False, 0.9), ["a/Foo.java"], "b", "h", "targeted_tests")
    assert not r.passed


def test_regression_fail_when_coverage_low(stub_worktree):
    r = V._verify_regression(".", _FakeRunner(True, 0.3), ["a/Foo.java"], "b", "h", "targeted_tests")
    assert not r.passed and r.adequacy.verdict == "inadequate"


# ---------------- B1：变异【跑了但失败】不许掩盖成 adequate ----------------
class _MutFailsRunner:
    """变异运行失败的桩 runner：套件绿、覆盖高，唯独 mutation 抛 MutationRunError。
    用于锁 B1——失败曾被 return None 吞 → mut_ok=True → verdict adequate。"""
    lang = "maven"
    supports_spec_blind = False

    def run_suite(self, wd):
        return (True, "suite-out")

    def changed_coverage(self, wd, cf, changed_lines=None):
        return 0.95

    def mutation(self, wd, cf, test_code=None):
        raise V.MutationRunError("PIT boom（桩：targetClasses 未命中）")

    def extract_interface(self, wd, cf):
        return "iface"

    def run_generated(self, wd, code):
        return (True, "gen-out")

    def cover_generated(self, wd, code, cf, changed_lines=None):
        return 0.95


def test_verify_regression_mutation_failure_is_inadequate(stub_worktree):
    """B1: full_suite 下变异运行失败 → verdict inadequate + passed False + 原因进 evidence
    （曾失败被 None 吞 → mut_ok=True → adequate → 静默放过弱测试）。"""
    r = V._verify_regression(".", _MutFailsRunner(), ["a/Foo.java"], "b", "h", "full_suite")
    assert r.adequacy.verdict == "inadequate"
    assert r.passed is False
    assert "PIT boom" in r.evidence and "不掩盖" in r.evidence


def test_check_adequacy_mutation_failure_is_inadequate(monkeypatch, tmp_path):
    """B1: spec_blind 路径(check_adequacy)同样——变异失败 → inadequate（即便覆盖达标+哨兵成立）。
    未修时 mutation 抛出穿透 check_adequacy（未捕获 → 测试 ERROR）；本断言锁捕获后判 inadequate。"""
    runner = _MutFailsRunner()
    runner.run_generated = lambda wd, code: (False, "base-fail")   # 改前挂 = 哨兵成立
    adq = V.check_adequacy(runner, "code", ["a/Foo.java"], {}, str(tmp_path), str(tmp_path), "full_suite")
    assert adq.verdict == "inadequate"
    assert adq.mutation_score is None


# ---------------- 分发：重构 PR → regression_only ----------------
def test_dispatch_refactor_routes_to_regression(monkeypatch, tmp_path):
    hit = {}
    monkeypatch.setattr(V, "_verify_regression",
                        lambda *a, **k: hit.setdefault("y", True) or V.VerificationResult(True, "regression_only"))
    V.verify_change(str(tmp_path), {"intent": "refactor extract"}, ["x.py"], "b", "h",
                    "targeted_tests", {"base_url": "b", "api_key": "k", "model": "m"},
                    "refactor(x): y")
    assert hit.get("y")


def test_cheap_only_passes_without_runner(tmp_path):
    r = V.verify_change(str(tmp_path), {}, ["x.py"], "b", "h", "cheap_only",
                        {"base_url": "b", "api_key": "k", "model": "m"})
    assert r.passed and r.mode == "cheap_only"


# ============================================================================
# #4 质量门禁加固：改动行级覆盖 + Java 独立验收测试生成
# ============================================================================

# ---------------- 改动行级覆盖 ----------------
def test_parse_changed_lines():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -10,0 +11,2 @@\n+    a = 1\n+    b = 2\n"
            "@@ -20,1 +22,1 @@\n-old\n+    c = 3\n")
    cl = V.parse_changed_lines(diff)
    assert cl["x.py"] == {11, 12, 22}


def test_coverage_json_line_ratio():
    cov = {"files": {"x.py": {"executed_lines": [11, 12], "missing_lines": [22]}}}
    # 改动行 11,12,22 都可覆盖；执行到 11,12 → 2/3
    assert abs(V._coverage_json_line_ratio(cov, {"x.py": {11, 12, 22}}) - 2 / 3) < 1e-9
    # 全覆盖
    assert V._coverage_json_line_ratio(cov, {"x.py": {11, 12}}) == 1.0
    # 改动行不在可覆盖集合（如纯注释行）→ 视为 1.0（无可覆盖改动行）
    assert V._coverage_json_line_ratio(cov, {"x.py": {99}}) == 1.0


def test_jacoco_line_ratio(tmp_path):
    import xml.etree.ElementTree as ET
    xml = ('<report><sourcefile name="Foo.java">'
           '<line nr="11" mi="0" ci="3"/><line nr="12" mi="2" ci="0"/>'
           '<line nr="13" mi="0" ci="5"/></sourcefile></report>')
    root = ET.fromstring(xml)
    # 改动行 11,12 → 11 覆盖(ci>0)、12 未覆盖 → 1/2
    assert V._jacoco_line_ratio([root], {"Foo.java": {11, 12}}) == 0.5
    # 改动行 11,13 → 都覆盖 → 1.0
    assert V._jacoco_line_ratio([root], {"Foo.java": {11, 13}}) == 1.0


# ---------------- JUnit 放置 ----------------
def test_place_junit_with_package(tmp_path):
    code = "package com.x.y;\nimport org.junit.jupiter.api.Test;\npublic class FooSpecTest { }\n"
    cname, path = V._place_junit(str(tmp_path), code)
    assert cname == "FooSpecTest"
    assert path.endswith(os.path.join("src", "test", "java", "com", "x", "y", "FooSpecTest.java"))
    assert os.path.exists(path)


def test_place_junit_without_package(tmp_path):
    cname, path = V._place_junit(str(tmp_path), "class BarTest {}\n")
    assert cname == "BarTest"
    assert path.endswith(os.path.join("src", "test", "java", "BarTest.java"))


# ---------------- Java 独立验收测试生成（盲于实现）----------------
def test_generate_spec_blind_junit_prompt(monkeypatch):
    captured = {}

    def stub_llm(messages, **cfg):
        captured["sys"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return "```java\npublic class GenTest {}\n```"

    monkeypatch.setattr(V, "_llm", stub_llm)
    ts = V.generate_spec_blind_tests(["返回非负", "空输入抛异常"], "Foo: method bar()",
                                     {"base_url": "b", "api_key": "k", "model": "test-model"},
                                     framework="junit5")
    assert ts.source == "spec_blind" and ts.author_model == "test-model"
    assert "JUnit 5" in captured["sys"] and "看不到实现" in captured["sys"]
    assert "返回非负" in captured["user"] and "Foo: method bar()" in captured["user"]
    assert "class GenTest" in ts.code


def test_maven_supports_spec_blind():
    assert V.MavenRunner().supports_spec_blind is True


# ---------------- 语言无关独立验收测试分支（桩化端到端）----------------
class _SpecRunner:
    lang = "maven"
    supports_spec_blind = True

    def __init__(self):
        self.n = 0

    def extract_interface(self, wd, cf):
        return "iface"

    def run_generated(self, wd, code):
        self.n += 1
        return (True, "head") if self.n == 1 else (False, "base")   # 1=改后过, 2=改前挂(哨兵成立)

    def cover_generated(self, wd, code, cf, changed_lines=None):
        return 0.9

    def mutation(self, wd, cf, test_code=None):
        return None


def test_spec_blind_branch_language_agnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "select_runner", lambda repo, cf: _SpecRunner())
    monkeypatch.setattr(V, "_worktree", lambda repo, ref: str(tmp_path))
    monkeypatch.setattr(V, "_rm_worktree", lambda repo, d: None)
    monkeypatch.setattr(V, "_changed_lines", lambda *a: {"Foo.java": {11}})
    monkeypatch.setattr(V, "generate_spec_blind_tests",
                        lambda *a, **k: V.AcceptanceTestSet(code="class T{}", source="spec_blind",
                                                            author_model="m"))
    r = V.verify_change(str(tmp_path), {"intent": "add feature", "acceptance_criteria": ["x"]},
                        ["Foo.java"], "b", "h", "targeted_tests",
                        {"base_url": "b", "api_key": "k", "model": "m"}, "feat: x")
    assert r.passed and r.mode == "targeted_tests"
    assert r.adequacy.sentinel_passed is True and r.adequacy.changed_file_coverage == 0.9


# ---------------- 验收规格来源治理（acceptance_criteria 收口）----------------
def test_resolve_acceptance_spec_falls_back_to_author(tmp_path):
    crit, src = V.resolve_acceptance_spec(
        {"acceptance_criteria": ["author 自报"]}, str(tmp_path))
    assert crit == ["author 自报"] and src == "author_proposed"


def test_resolve_acceptance_spec_prefers_human_curated(tmp_path):
    import os
    os.makedirs(tmp_path / ".touchstone")
    (tmp_path / ".touchstone" / "acceptance.yaml").write_text(
        "acceptance_criteria:\n  - 人核准的验收点\n", encoding="utf-8")
    crit, src = V.resolve_acceptance_spec(
        {"acceptance_criteria": ["author 自报"]}, str(tmp_path))
    assert crit == ["人核准的验收点"] and src == "human_curated"


# ---------------- AST 级变异（替代字符串替换 toy）----------------
def test_ast_mutants_flips_operators():
    src = "def f(a, b):\n    if a > b:\n        return a + b\n    return 0\n"
    muts = V._ast_mutants(src)
    assert any("a <= b" in m for m in muts)    # 关系翻转 Gt->LtE
    assert any("a - b" in m for m in muts)     # 算术翻转 Add->Sub
    assert len(muts) >= 2


def test_mutation_check_strong_test_kills(tmp_path):
    (tmp_path / "m.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
    strong = ("import sys; sys.path.insert(0, '.')\nfrom m import f\n"
              "def test_f():\n    assert f(2, 3) == 5\n")
    assert V._mutation_check(str(tmp_path), strong, ["m.py"]) == 1.0   # a+b->a-b 被 5≠-1 抓到


def test_mutation_check_weak_test_survives(tmp_path):
    (tmp_path / "m.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
    weak = ("import sys; sys.path.insert(0, '.')\nfrom m import f\n"
            "def test_f():\n    assert f(2, 3) is not None\n")
    assert V._mutation_check(str(tmp_path), weak, ["m.py"]) == 0.0     # a-b 仍非 None → 变异存活


# ---------------- 边界：纯函数（评级/重构判定/diff 解析/覆盖率）----------------
def test_grade_verdicts_boundaries():
    assert V._grade(0.8, True, None).verdict == "adequate"
    assert V._grade(0.8, False, None).verdict == "inadequate"   # 哨兵不过
    assert V._grade(0.5, True, None).verdict == "inadequate"    # 覆盖 <0.6
    assert V._grade(0.6, True, None).verdict == "adequate"      # 覆盖 ==0.6（含等号）
    assert V._grade(0.9, True, 0.5).verdict == "inadequate"     # 变异 <0.6
    assert V._grade(0.9, True, 0.6).verdict == "adequate"       # 变异 ==0.6


def test_is_refactor_variants():
    assert V.is_refactor({"intent": "refactor user service"})
    assert V.is_refactor({}, "refactor(core): tidy")
    assert V.is_refactor({"intent": "重构鉴权模块"})
    assert not V.is_refactor({"intent": "add new endpoint"}, "feat: x")


def test_parse_changed_lines_and_coverage_ratio():
    # diff hunk 必须自洽：@@ -1,1 +1,3 @@ = 原 1 行、新 3 行（ctx + 2 added）
    diff = ("--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,3 @@\n ctx\n+new1\n+new2\n"
            "--- a/d.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-gone\n")
    assert V.parse_changed_lines(diff) == {"x.py": {2, 3}}      # 删除文件(/dev/null)不计
    cov = {"files": {"x.py": {"executed_lines": [2], "missing_lines": [3]}}}
    assert V._coverage_json_line_ratio(cov, {"x.py": {2, 3}}) == 0.5
    assert V._coverage_json_line_ratio(cov, {}) == 1.0          # 无可覆盖行 → 1.0
    assert V._coverage_json_line_ratio({}, {"x.py": {2}}) == 1.0  # 文件缺 → 1.0


# ============ 外部变异工具接缝（mutmut/cosmic-ray/PIT）回归 ============
def test_parse_mutation_output_takes_last_number():
    from verify import verify_change as V
    assert V._parse_mutation_output("killed 10/12\nscore: 83%") == 0.83
    assert V._parse_mutation_output("mutation score 0.6") == 0.6
    assert V._parse_mutation_output("no numbers here") is None

def test_external_mutation_cmd_used_when_set(monkeypatch, tmp_path):
    from verify import verify_change as V
    # #111 安全默认：旧 stdout 模板（无 {result_file}）未显式 opt-in → 弃 stdout → None（回退内置）
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo killed 3/4 = 75%")
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None
    # 显式 opt-in（部署方知情接受 spoof 风险）→ 回到 legacy stdout 解析
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", "1")
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) == 0.75
    monkeypatch.delenv("TOUCHSTONE_MUTATION_CMD")
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None   # 未设 → 回退内置


def test_external_mutation_score_result_file_trusted(monkeypatch, tmp_path):
    """#111 可信路径：模板含 {result_file} → 工具把分写进 runner 指定的临时文件，runner 读文件取分。"""
    from verify import verify_change as V
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo 83% > {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)   # 不需要 opt-in：{result_file} 即可信
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) == 0.83


def test_external_mutation_score_result_file_not_spoofed_by_stdout(monkeypatch, tmp_path):
    """#111 spoof-killer：命令往 stdout 喷假满分 99%，但写真击杀率 0.05 进 {result_file}。
    修复后取文件真值 0.05（< MUT_MIN），**不**取 stdout 的 0.99——堵 author 经 conftest 往 stdout
    末尾喷高数顶过变异门。若退回读 stdout（变异：弃文件改 parse stdout），本测拿到 0.99 即杀红。"""
    from verify import verify_change as V
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo killed 99%; echo 0.05 > {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)
    score = V.external_mutation_score(str(tmp_path), ["a.py"])
    assert score == 0.05                # 真值，非 stdout 的 0.99
    assert score < 0.6                  # 弱测试不会被顶过 MUT_MIN


def test_external_mutation_score_result_file_unwritten_not_stdout(monkeypatch, tmp_path):
    """#111：rc==0、命令往 stdout 喷假满分、但没往 {result_file} 写可解析分 → None（不退回 stdout 顶分）。
    命令**含 {result_file}** 故真进入 `_external_score_via_result_file`；`echo 99%` 喷 stdout、`true` 忽略
    {result_file} 不写（mkstemp 预建空文件）→ 读得空 → _parse_mutation_output('') → None。若可信路径
    错误退回 stdout 会拿到 0.99 → 杀红（堵住「文件空就 fallback stdout」的回归）。"""
    from verify import verify_change as V
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo 99%; true {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None


def test_external_mutation_score_result_file_random_filename(monkeypatch, tmp_path):
    """round-4 PRA-SECURITY:230 / PRA-REVIEW:230：结果文件名必须随机化。仅随机目录名 + 固定文件名
    score 仍可 glob('/tmp/<rand>/score') 命中（PR 代码跑为工具孙进程、可枚举任意子目录）。断言命令里
    结果文件 basename 是 secrets.token_hex(8)=16 hex 字符、非固定 'score'。变异（文件名改回 'score'）
    → basename=='score' → 杀红。"""
    import re
    captured = {}
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo 50% > {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)

    def fake_run(full, wd, to):
        captured["full"] = full
        m = re.search(r">\s*(\S+)", full)        # 结果文件路径（shlex.quote 对纯安全路径不加引号 → 裸或引号皆可能）
        if m:
            open(m.group(1).strip("';"), "w").write("50%")
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(R, "_run_mutation_cmd", fake_run)
    assert R.external_mutation_score(str(tmp_path), ["a.py"]) == 0.5
    m = re.search(r">\s*(\S+)", captured["full"])
    assert m, captured["full"]
    basename = os.path.basename(m.group(1).strip("';"))
    assert basename != "score", f"固定文件名 score 仍可 glob('/tmp/<rand>/score') 枚举: {basename}"
    assert re.fullmatch(r"[0-9a-f]{16}", basename), f"期望随机 hex 文件名(token_hex(8)): {basename}"


def test_external_mutation_score_cleans_nonempty_temp_dir(monkeypatch, tmp_path):
    """round-4 PRA-GENERAL:240：外部工具可能往 result_dir 写额外产物（日志/临时文件），os.rmdir 仅删
    空目录会让残留文件挡住删除、泄漏随机目录（每轮一份）。shutil.rmtree(ignore_errors=True) 容忍非空、
    保证清理。变异（rmtree 改回 os.rmdir）→ 非空目录删不掉、残留 → 断言目录已删失败 → 杀红。"""
    import re
    created = []
    real_mkdtemp = R.tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(d)
        return d

    def fake_run(full, wd, to):
        m = re.search(r">\s*(\S+)", full)        # 结果文件路径（shlex.quote 对纯安全路径不加引号 → 裸或引号皆可能）
        if m:
            open(m.group(1).strip("';"), "w").write("50%")
            # 工具顺手往同一目录写额外产物（os.rmdir 删不掉的元凶）
            open(os.path.join(os.path.dirname(m.group(1).strip("';")), "tool.log"), "w").write("x")
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(R.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(R, "_run_mutation_cmd", fake_run)
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo 50% > {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)
    assert R.external_mutation_score(str(tmp_path), ["a.py"]) == 0.5
    assert created, "mkdtemp 应被调用"
    assert not os.path.exists(created[0]), f"非空目录应被 shutil.rmtree 清理，仍残留: {created[0]}"


def test_external_mutation_score_replace_order_safe(monkeypatch, tmp_path):
    """round-4 PRA-REVIEW:233：chained .replace() 顺序注入面。PR 作者把文件命名成 '{result_file}'，
    files_sub 会含 '{result_file}' 字面。若先替 {files} 再替 {result_file}（旧顺序），第二次 replace 会
    把真 result 路径注入 {files} 位置、泄漏隐藏路径。换序（先 {result_file} 后 {files}；str.replace 单趟
    扫描、不对结果再扫描）可避。变异（换回旧顺序）→ {files} 位出现真路径而非文件名字面 → 杀红。"""
    import re
    captured = {}
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo 50% > {result_file}; touch {files}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)

    def fake_run(full, wd, to):
        captured["full"] = full
        m = re.search(r">\s*(\S+)", full)        # 结果文件路径（shlex.quote 对纯安全路径不加引号 → 裸或引号皆可能）
        if m:
            open(m.group(1).strip("';"), "w").write("50%")
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(R, "_run_mutation_cmd", fake_run)
    # 文件名是 PR 可控的 '{result_file}' 字面（注入触发条件）
    assert R.external_mutation_score(str(tmp_path), ["{result_file}"]) == 0.5
    # 换序正确：{files} 位保留文件名字面 '{result_file}'（quote 后），真路径不泄漏到此位
    assert "touch '{result_file}'" in captured["full"], captured["full"]


def test_external_mutation_score_result_file_first_line_only(monkeypatch, tmp_path):
    """round-5 PRA-REVIEW:245：结果文件可能含多指标（score:50%\\ncoverage:80%）。_parse_mutation_output
    取末数 → 0.80（错，应取击杀率 0.50）。修复：只读第一行（约定 score 在 line 1）。变异（readline
    改回 read 全文件 + 取末数）→ 0.80 → 杀红。"""
    import re
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "tool > {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)

    def fake_run(full, wd, to):
        m = re.search(r">\s*(\S+)", full)
        if m:
            # 工具写多指标报告：第一行 score，第二行 coverage
            open(m.group(1).strip("';"), "w").write("score: 50%\ncoverage: 80%\n")
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(R, "_run_mutation_cmd", fake_run)
    score = R.external_mutation_score(str(tmp_path), ["a.py"])
    assert score == 0.50, f"应取第一行击杀率 0.50，而非末数 coverage 0.80: {score}"


def test_external_mutation_score_result_file_append_spoof_blocked(monkeypatch, tmp_path):
    """round-5：攻击者（同 UID conftest）在工具写真击杀率后、runner 读前，往结果文件末尾 append 假
    满分。只读第一行（工具写的真分）→ append 到后续行不影响 → 安全。变异（readline 改回 read 全文件
    + 取末数）→ 0.99 → 杀红。"""
    import re
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "tool > {result_file}")
    monkeypatch.delenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", raising=False)

    def fake_run(full, wd, to):
        m = re.search(r">\s*(\S+)", full)
        if m:
            target = m.group(1).strip("';")
            open(target, "w").write("0.05\n")          # 工具写真（弱）击杀率
            with open(target, "a") as f:                # 攻击者 append 假满分
                f.write("0.99\n")
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(R, "_run_mutation_cmd", fake_run)
    score = R.external_mutation_score(str(tmp_path), ["a.py"])
    assert score == 0.05, f"append-spoof 应被只读第一行挡住，取工具真分 0.05: {score}"
    assert score < 0.6


def test_external_mutation_cmd_hostile_filename_not_executed(monkeypatch, tmp_path):
    """注入面回归锁：changed_files 的文件名是 PR author 可控输入。名为 `x; 命令;` 的
    文件不得在 shell 里逃逸执行——quote 后它只是 `true` 的一个普通参数。若逃逸，
    ① work_dir 会出现 INJECTED 文件，② 击杀率会被注入的 `echo 0.99` 篡改为 0.99。
    走 legacy stdout 路径（TRUST_STDOUT=1）以实际执行命令、抵达 {files} 替换点。"""
    from verify import verify_change as V
    hostile = "x.py; touch INJECTED; echo 0.99;"
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", "1")
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "true {files}; echo 0.5")
    assert V.external_mutation_score(str(tmp_path), [hostile]) == 0.5
    assert not (tmp_path / "INJECTED").exists()


def test_external_mutation_cmd_nonzero_rc_not_trusted(monkeypatch, tmp_path):
    """A5-F2：外部变异命令 rc!=0（崩溃/失败）→ 不信其 stdout，返回 None。docstring 既已承诺
    "命令失败→None"，rc!=0 即按承诺落空、不解析 stdout。外部工具崩溃前可能 print 一个误导性
    百分数（部分跑完 / segfault 前的汇总行 / 把进度当结果输出），旧实现照旧
    _parse_mutation_output(r.stdout) 取数 → 该数字直达 mutation_score → 顶过 MUT_MIN 判
    adequate → 弱测试骗过变异门（与 #79 B1 同类假过）。
    走 legacy stdout 路径（TRUST_STDOUT=1）以实际执行命令，rc 守卫才被真实抵达。
    `echo killed 90%; false` 模拟：stdout 有 90%，但整体 rc=1（false 退出码）。
    删 rc 守卫（变异）后本测会拿到 0.9 而非 None → 杀红。"""
    from verify import verify_change as V
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", "1")
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo killed 90%; false")
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None
    # 对照：rc=0 时正常解析（保留接缝行为、rc 守卫不误伤）——锁 test_external_mutation_cmd_used_when_set
    # 的 opt-in 同一侧。


# ---------------- 纯函数补测 ----------------


def test_extract_code_with_and_without_fence():
    assert V._extract_code("```python\nprint(1)\n```") == "print(1)"
    assert V._extract_code("bare code") == "bare code"


def test_extract_interface_skips_non_py_and_missing(tmp_path):
    (tmp_path / "m.py").write_text("def f(a):\n    return a\nclass C:\n    pass\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text("ignore", encoding="utf-8")
    out = V._extract_interface(str(tmp_path), ["m.py", "x.txt", "nope.py"])
    assert "def f(a)" in out and "class C" in out


def test_extract_interface_syntax_error_skipped(tmp_path):
    (tmp_path / "bad.py").write_text("def (", encoding="utf-8")
    assert V._extract_interface(str(tmp_path), ["bad.py"]) == "(未能抽取签名)"


def test_generate_spec_blind_pytest(monkeypatch):
    monkeypatch.setattr(V, "_llm", lambda msgs, **cfg: "```python\ndef test_x(): assert 1\n```")
    ts = V.generate_spec_blind_tests(["它应返回 1"], "def f():", {"base_url": "u", "api_key": "k", "model": "m"})
    assert ts.code == "def test_x(): assert 1" and ts.source == "spec_blind" and ts.author_model == "m"


def test_generate_spec_blind_junit(monkeypatch):
    monkeypatch.setattr(V, "_llm", lambda msgs, **cfg: "```java\n@Test void t(){}\n```")
    ts = V.generate_spec_blind_tests(["x"], "i", {"base_url": "u", "api_key": "k", "model": "m"}, framework="junit5")
    assert "@Test" in ts.code


class _FakeCov:
    """模拟 coverage.Coverage——用 analysis2（statements + missing）替代已废弃的
    CoverageData.lines/missing_lines（pr-agent 审计发现 missing_lines 不存在）。"""
    def __init__(self, lines_map, missing_map):
        # lines_map = executed lines, missing_map = not-executed lines
        # analysis2 返回 (filename, statements, excluded, missing, missing_formatted)
        self._lines = lines_map
        self._missing = missing_map
    def analysis2(self, path):
        executed = self._lines.get(path) or set()
        missing = self._missing.get(path) or set()
        statements = sorted(executed | missing)
        return (path, statements, [], sorted(missing), "")
    def get_data(self):
        class _D:
            def lines(_, p): return self._lines.get(p)
        return _D()


def test_coverage_ratio_file_level():
    cov = _FakeCov({"a.py": {1, 2}}, {"a.py": {3}})   # 执行{1,2} missing{3} → 2/3
    assert V._coverage_ratio(cov, ["a.py"]) == 2 / 3


def test_coverage_ratio_empty_files():
    assert V._coverage_ratio(_FakeCov({}, {}), []) == 0.0


def test_coverage_ratio_line_level():
    cov = _FakeCov({"a.py": {1, 2}}, {"a.py": set()})     # 行 {1,2} 全执行
    assert V._coverage_ratio(cov, ["a.py"], {"a.py": {1, 2}}) == 1.0


def test_coverage_ratio_line_level_none_coverable():
    cov = _FakeCov({"a.py": set()}, {"a.py": set()})
    assert V._coverage_ratio(cov, ["a.py"], {"a.py": {99}}) == 1.0  # 无可覆盖行 → 1.0


def test_coverage_ratio_line_level_list_input():
    # changed_lines 值为 list 时不应 set&list 抛 TypeError 静默失败（pr-agent 第3轮 :91 回归保护）
    cov = _FakeCov({"a.py": {1, 2}}, {"a.py": set()})
    assert V._coverage_ratio(cov, ["a.py"], {"a.py": [1, 2]}) == 1.0


def test_coverage_json_line_ratio_list_input():
    # 同一缺陷类：_coverage_json_line_ratio 的 & lines 也须容忍 list 输入
    cov = {"files": {"x.py": {"executed_lines": [11, 12], "missing_lines": [22]}}}
    assert abs(V._coverage_json_line_ratio(cov, {"x.py": [11, 12, 22]}) - 2 / 3) < 1e-9


def test_changed_file_coverage_no_py_returns_one():
    assert V._changed_file_coverage(".", "code", ["a.js"]) == 1.0


def test_changed_file_coverage_subprocess_path(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_run_coverage_subprocess",
                        lambda wd, args: _FakeCov({"a.py": {1}}, {"a.py": set()}))
    assert V._changed_file_coverage(str(tmp_path), "def test_x(): assert 1", ["a.py"]) == 1.0


def test_changed_file_coverage_exception_returns_zero(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("cov failed")
    monkeypatch.setattr(R, "_run_coverage_subprocess", boom)
    assert V._changed_file_coverage(str(tmp_path), "x", ["a.py"]) == 0.0


def test_run_coverage_subprocess_loads_on_test_failure(monkeypatch, tmp_path):
    # pytest 测试失败（退出码 1）时 coverage 仍写出 .coverage——不应因退出码非零丢采集
    # （pr-agent 第3轮 :72 回归保护）
    def fake_run(*a, **k):
        (tmp_path / ".coverage").write_text("")   # 模拟 coverage 落盘（即便测试失败）
        return R.subprocess.CompletedProcess(a[0], 1)

    class _Cov:
        def __init__(self, data_file=None):
            pass
        def load(self):
            pass

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    monkeypatch.setattr("coverage.Coverage", _Cov)
    assert R._run_coverage_subprocess(str(tmp_path), ["-m", "pytest", "-q", "x.py"]) is not None


def test_run_coverage_subprocess_raises_when_no_data(monkeypatch, tmp_path):
    # 起跑前已清空 .coverage；跑完仍无数据产出（coverage 崩溃/超时未落盘）→ raise，绝不加载 stale
    monkeypatch.setattr(R.subprocess, "run",
                        lambda *a, **k: R.subprocess.CompletedProcess(a[0], 1))
    with pytest.raises(RuntimeError, match="未产出数据"):
        R._run_coverage_subprocess(str(tmp_path), ["-m", "pytest", "-q", "x.py"])


def test_mutation_sites_finds_binary_ops():
    import ast
    tree = ast.parse("def f(a, b):\n    return a + b\n")
    sites = V._mutation_sites(tree)
    assert sites and any(isinstance(n, ast.BinOp) for n in sites)


def test_basename_lines_groups_by_basename():
    bl = V._basename_lines({"src/a.py": {1, 2}, "test/a.py": {3}})
    assert bl["a.py"] == {1, 2, 3}


def test_changed_lines_uses_git_diff(monkeypatch):
    class _R:
        returncode = 0
        stdout = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -0,0 +1,2 @@\n+new\n+new2\n"
    monkeypatch.setattr(V.subprocess, "run", lambda *a, **k: _R())
    cl = V._changed_lines(".", "b", "h")
    assert cl.get("f.py") == {1, 2}


def test_external_mutation_score_no_cmd_no_mutmut(monkeypatch, tmp_path):
    monkeypatch.delenv("TOUCHSTONE_MUTATION_CMD", raising=False)
    monkeypatch.setattr(V.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None


def test_suite_coverage_python_no_py(tmp_path):
    assert V._suite_coverage_python(str(tmp_path), ["a.js"]) == 1.0


def test_verify_regression_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_worktree", lambda repo, ref: str(tmp_path / ref))
    monkeypatch.setattr(V, "_rm_worktree", lambda repo, d: None)
    monkeypatch.setattr(V, "_changed_lines", lambda *a: {"a.py": {1}})

    class FR:
        lang = "pytest"
        def run_suite(self, d): return True, "ok"
        def changed_coverage(self, d, cf, cl): return 1.0
        def mutation(self, d, cf): return None
    r = V._verify_regression(str(tmp_path), FR(), ["a.py"], "b", "h", "regression_only")
    assert r.passed is True and r.mode == "regression_only" and r.head_tests_pass is True


def test_verify_regression_low_coverage_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_worktree", lambda repo, ref: str(tmp_path / ref))
    monkeypatch.setattr(V, "_rm_worktree", lambda repo, d: None)
    monkeypatch.setattr(V, "_changed_lines", lambda *a: {"a.py": {1}})

    class FR:
        lang = "maven"
        def run_suite(self, d): return True, "ok"
        def changed_coverage(self, d, cf, cl): return 0.1   # 低于 COV_MIN
        def mutation(self, d, cf): return None
    r = V._verify_regression(str(tmp_path), FR(), ["a.py"], "b", "h", "regression_only")
    assert r.passed is False


# ---------------- Runner 方法（mock 底层 subprocess/helper）----------------
def test_python_runner_run_suite(monkeypatch):
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))
    assert V.PythonRunner().run_suite("wd") == (True, "ok")


def test_python_runner_mutation_branches(monkeypatch, tmp_path):
    r = V.PythonRunner()
    # 外部变异工具有值 → 直接用
    monkeypatch.setattr(R, "external_mutation_score", lambda wd, cf: 0.7)
    assert r.mutation(str(tmp_path), ["a.py"], test_code="t") == 0.7
    # 外部返回 None + 有 test_code → _mutation_check
    monkeypatch.setattr(R, "external_mutation_score", lambda wd, cf: None)
    monkeypatch.setattr(R, "_mutation_check", lambda wd, tc, cf: 0.4)
    assert r.mutation(str(tmp_path), ["a.py"], test_code="t") == 0.4
    # 外部 None + 无 test_code → None
    assert r.mutation(str(tmp_path), ["a.py"]) is None


def test_python_runner_run_generated_and_cover(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_run_tests", lambda wd, tc: (True, "out"))
    assert V.PythonRunner().run_generated(str(tmp_path), "code") == (True, "out")
    monkeypatch.setattr(R, "_changed_file_coverage", lambda wd, tc, cf, cl=None: 0.9)
    assert V.PythonRunner().cover_generated(str(tmp_path), "code", ["a.py"]) == 0.9


def test_maven_runner_run_suite_and_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))
    r = V.MavenRunner()
    assert r.run_suite(str(tmp_path)) == (True, "ok")
    # mutation：mvn 成功 + 有分数 → _pit_score
    monkeypatch.setattr(R, "_pit_score", lambda wd: 0.55)
    assert r.mutation(str(tmp_path), ["A.java"]) == 0.55
    # mvn 失败 → MutationRunError（B1：曾 return None → _grade 当 mut_ok=True → 掩盖成 adequate）
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (False, "boom-detail"))
    with pytest.raises(V.MutationRunError):
        r.mutation(str(tmp_path), ["A.java"])


def test_maven_runner_mutation_no_report_raises(monkeypatch, tmp_path):
    """B1: PIT 退出码 0 但未产出 mutations.xml → MutationRunError（曾 _pit_score None → 掩盖 adequate）。"""
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))
    monkeypatch.setattr(R, "_pit_score", lambda wd: None)
    monkeypatch.setattr(R, "_pit_has_report", lambda wd: False)
    with pytest.raises(V.MutationRunError):
        V.MavenRunner().mutation(str(tmp_path), ["A.java"])


def test_maven_runner_mutation_zero_mutants_with_report_returns_one(monkeypatch, tmp_path):
    """对照：PIT 退出码 0 + 报告在但零变异点 = 无可变异 → 1.0（与 Python _mutation_check applied==0→1.0 对齐）。"""
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))
    monkeypatch.setattr(R, "_pit_score", lambda wd: None)
    monkeypatch.setattr(R, "_pit_has_report", lambda wd: True)
    assert V.MavenRunner().mutation(str(tmp_path), ["A.java"]) == 1.0


def test_pit_score_corrupt_report_raises(tmp_path):
    """A5-F1：mutations.xml 存在但损坏（PIT 崩溃中途写出截断 xml 等）→ _pit_score 抛 MutationRunError，
    而非返回 None。旧实现 ParseError 静默 continue→返回 None→上游 _pit_has_report=True→return 1.0
    （mutation_score 顶满 → MUT_MIN 判过 → 弱测试骗过 verify 变异门），即 #79 B1 未堵死的口子。"""
    pd = tmp_path / "mod/target/pit-reports/202606"
    pd.mkdir(parents=True)
    (pd / "mutations.xml").write_text('<mutations><mutation status="KILLED"')   # 截断，ET.parse 抛 ParseError
    with pytest.raises(V.MutationRunError):
        V._pit_score(str(tmp_path))


def test_pit_score_corrupt_report_message_has_parse_detail(tmp_path):
    """#98 二轮评审建议（PRA-GENERAL）：损坏报告的报错须带解析异常细节（行/列/原因），否则运维只
    知"报告损坏"却不知截断点、无法定位。锁 except ET.ParseError `as e` 与报错拼接：把 `as e`/
    细节拼接去掉 → 报错只剩路径、无 column N → 杀红。"""
    pd = tmp_path / "mod/target/pit-reports/202606"
    pd.mkdir(parents=True)
    (pd / "mutations.xml").write_text('<mutations><mutation status="KILLED"')   # 截断
    with pytest.raises(V.MutationRunError) as exc:
        V._pit_score(str(tmp_path))
    msg = str(exc.value)
    assert "mutations.xml" in msg                     # 仍带损坏报告路径
    assert "column" in msg                            # 带解析异常定位（仅来自捕获的 ParseError）


def test_pit_score_partial_corrupt_uses_parseable(tmp_path):
    """对照：同胞报告中至少一份可解析 → 以它为准、不抛（损坏同胞不阻塞，分数仍可信）。
    锁 `corrupt and not parseable` 的 not-parseable 半段——避免误把"有可用报告"当全坏。"""
    bad = tmp_path / "mod/target/pit-reports/202606"
    bad.mkdir(parents=True)
    (bad / "mutations.xml").write_text('<mutations><mutation status="KILLED"')      # 损坏
    good = tmp_path / "mod2/target/pit-reports/202607"
    good.mkdir(parents=True)
    (good / "mutations.xml").write_text(
        '<mutations><mutation status="KILLED"/><mutation status="SURVIVED"/></mutations>')  # 可解析
    assert abs(V._pit_score(str(tmp_path)) - 0.5) < 1e-9    # 用可解析那份：1 killed / 2 total


def test_pit_score_corrupt_masked_by_empty_report_raises(tmp_path):
    """#98 续：损坏同胞 + 一份【可解析但零变异】(total=0) 的报告 → 必抛 MutationRunError，不能返回 None。
    场景：多模块构建，模块 A 改动无变异点（合法零变异报告）、模块 B PIT 崩溃写出截断 xml。
    旧守卫只 `corrupt and not parseable`：parseable=True 把它压下 → total=0 → 返回 None
    → 上游 MavenRunner.mutation 经 _pit_has_report=True 路径顶满成 1.0 → 模块 B 的崩溃被无视、弱测试骗过门。
    锁 `corrupt and (not parseable or total == 0)` 的 total==0 半段。"""
    bad = tmp_path / "mod/target/pit-reports/202606"
    bad.mkdir(parents=True)
    (bad / "mutations.xml").write_text('<mutations><mutation status="KILLED"')      # 损坏（模块 B 崩溃）
    empty = tmp_path / "mod2/target/pit-reports/202607"
    empty.mkdir(parents=True)
    (empty / "mutations.xml").write_text('<mutations></mutations>')                 # 可解析、零变异点
    with pytest.raises(V.MutationRunError):
        V._pit_score(str(tmp_path))


def test_maven_runner_mutation_corrupt_masked_by_empty_raises(monkeypatch, tmp_path):
    """#98 端到端：损坏报告 + 可解析零变异报告 → MavenRunner.mutation 抛 MutationRunError，绝不返回 1.0。
    不桩 _pit_score/_pit_has_report——走真实解析路径，证"空报告掩护损坏报告"的假过口子已堵。"""
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))   # PIT 退出码 0
    bad = tmp_path / "mod/target/pit-reports/202606"
    bad.mkdir(parents=True)
    (bad / "mutations.xml").write_text('<mutations><mutation status="KILLED"')      # 损坏
    empty = tmp_path / "mod2/target/pit-reports/202607"
    empty.mkdir(parents=True)
    (empty / "mutations.xml").write_text('<mutations></mutations>')                 # 可解析、零变异
    with pytest.raises(V.MutationRunError):
        V.MavenRunner().mutation(str(tmp_path), ["A.java"])


def test_maven_runner_mutation_corrupt_report_raises(monkeypatch, tmp_path):
    """A5-F1 端到端：mvn 退出码 0 但产出的 mutations.xml 损坏 → MavenRunner.mutation 抛
    MutationRunError（→ check_adequacy/_verify_regression 据此判 inadequate），绝不返回 1.0 假过。
    不桩 _pit_score/_pit_has_report——走真实解析路径，证 false-pass 已被堵。"""
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))   # PIT 退出码 0
    pd = tmp_path / "mod/target/pit-reports/202606"
    pd.mkdir(parents=True)
    (pd / "mutations.xml").write_text('<mutations><mutation status="KILLED"')   # 损坏报告
    with pytest.raises(V.MutationRunError):
        V.MavenRunner().mutation(str(tmp_path), ["A.java"])


def test_maven_runner_changed_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_jacoco_changed_coverage", lambda wd, cf: 0.6)
    monkeypatch.setattr(R, "_jacoco_changed_line_coverage", lambda wd, cf, cl: 0.8)
    r = V.MavenRunner()
    assert r.changed_coverage(str(tmp_path), ["A.java"]) == 0.6               # 无 changed_lines
    assert r.changed_coverage(str(tmp_path), ["A.java"], {"A.java": {1}}) == 0.8


def test_maven_runner_run_generated_and_cover(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_place_junit", lambda wd, tc: ("TestX", "p"))
    monkeypatch.setattr(R, "_run", lambda cmd, wd, timeout=None: (True, "ok"))
    monkeypatch.setattr(R, "_jacoco_changed_coverage", lambda wd, cf: 0.9)
    r = V.MavenRunner()
    assert r.run_generated(str(tmp_path), "code") == (True, "ok")
    assert r.cover_generated(str(tmp_path), "code", ["A.java"]) == 0.9


def test_maven_runner_mvnw_preferred(monkeypatch, tmp_path):
    # 存在 mvnw → 用 ./mvnw
    (tmp_path / "mvnw").write_text("#!/bin/sh", encoding="utf-8")
    seen = {}
    def fake_run(cmd, wd, timeout=None):
        seen["cmd"] = cmd
        return (True, "ok")
    monkeypatch.setattr(R, "_run", fake_run)
    V.MavenRunner().run_suite(str(tmp_path))
    assert seen["cmd"][0] == "./mvnw"


# ---------------- worktree / CLI 配置错分支 ----------------
def test_worktree_success_and_rm(monkeypatch, tmp_path):
    monkeypatch.setattr(V.subprocess, "run", lambda *a, **k: V.subprocess.CompletedProcess(a[0], 0))
    dest = V._worktree(str(tmp_path), "HEAD")
    assert dest.startswith("/tmp") or "touchstone_wt" in dest
    V._rm_worktree(str(tmp_path), dest)              # 走 returncode==0 分支


def test_worktree_failure_raises_and_cleans(monkeypatch, tmp_path):
    def fake(*a, **k):
        if "worktree" in a[0] and "add" in a[0]:
            raise RuntimeError("add failed")
        return V.subprocess.CompletedProcess(a[0], 0)
    monkeypatch.setattr(V.subprocess, "run", fake)
    import pytest
    with pytest.raises(RuntimeError):
        V._worktree(str(tmp_path), "HEAD")


def test_rm_worktree_fallback_on_nonzero(monkeypatch, tmp_path):
    calls = []
    def fake(cmd, *a, **k):
        calls.append(cmd)
        return V.subprocess.CompletedProcess(cmd, 1)   # remove 失败 → 走 rmtree+prune 兜底
    monkeypatch.setattr(V.subprocess, "run", fake)
    d = tmp_path / "wt"
    d.mkdir()
    V._rm_worktree(str(tmp_path), str(d))
    assert any("prune" in c for c in calls)


def test_run_tests_pass_and_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(V.subprocess, "run",
                        lambda *a, **k: V.subprocess.CompletedProcess(a[0], 0, stdout="ok", stderr=""))
    assert V._run_tests(str(tmp_path), "def test_x(): assert 1") == (True, "ok")
    import subprocess as sp
    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd=a[0], timeout=1)
    monkeypatch.setattr(V.subprocess, "run", boom)
    assert V._run_tests(str(tmp_path), "x") == (False, "timeout")


def test_cli_missing_llm_env_exits_2():
    import subprocess, sys, os
    # -m 运行（第一轮起全仓标准；verify 内部为包导入，不再支持脚本式路径调用）
    r = subprocess.run([sys.executable, "-m", "verify.verify_change"],
                       capture_output=True, text=True, env={})
    assert r.returncode == 2 and "LLM" in r.stderr


def test_cli_missing_refs_exits_2(monkeypatch):
    import subprocess, sys, os
    env = {**os.environ, "LLM_BASE_URL": "u", "LLM_API_KEY": "k", "LLM_MODEL": "m",
           "PATH": os.environ.get("PATH", "")}
    # 去掉 BASE_REF/HEAD_REF
    env.pop("BASE_REF", None); env.pop("HEAD_REF", None)
    r = subprocess.run([sys.executable, "-m", "verify.verify_change"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "BASE_REF" in r.stderr


def test_run_success_timeout_notfound(monkeypatch):
    import subprocess as sp
    seq = [V.subprocess.CompletedProcess(["x"], 0, stdout="o", stderr=""),
           sp.TimeoutExpired(cmd=["x"], timeout=1),
           FileNotFoundError("nope")]
    def fake(cmd, *a, **k):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(V.subprocess, "run", fake)
    assert V._run(["x"], ".") == (True, "o")
    assert V._run(["x"], ".") == (False, "timeout")
    res = V._run(["x"], ".")
    assert res[0] is False and "命令不存在" in res[1]


def test_external_mutation_cmd_parse_and_fail(monkeypatch, tmp_path):
    # 走 legacy stdout 路径（TRUST_STDOUT=1）：设了 cmd + 正常百分数输出 → 解析击杀率
    monkeypatch.setenv("TOUCHSTONE_MUTATION_TRUST_STDOUT", "1")
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "echo mutation score: 75%")
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) == 0.75
    # cmd 抛错（rc!=0）→ None
    monkeypatch.setenv("TOUCHSTONE_MUTATION_CMD", "false")
    assert V.external_mutation_score(str(tmp_path), ["a.py"]) is None

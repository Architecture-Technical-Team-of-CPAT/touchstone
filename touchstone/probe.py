"""Probe（测试有效性探针）——回答「『测试全绿』本身可信吗」。

设计见 docs/touchstone-probe-design.md。分两层：L0 断言普查（静态·census）先行，
L1 变异探针（动态·plan/run）兜底。结果转标准 Finding 并入 checklist 四态收敛。
自身抗 fail-open：哨兵变异体必被击杀，否则本轮判无效（never silent）。

零第三方依赖：仅用标准库 ast / hashlib / subprocess（不引 mutmut/cosmic-ray）。
"""
import ast
import hashlib
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto


class WorkspaceCorrupted(RuntimeError):
    """源码恢复失败 → 工作区脏。后续 mutant 会以脏源码为基线 → 级联误判。
    _inject_and_run 在 finally 恢复失败时抛出；run() 捕获后判 INVALID 中止（R1-07 / PRA restoration cascade）。"""


# ============================ §3 核心数据结构 ============================
@dataclass(frozen=True)
class SourceSpan:
    """位点行列区间（1-based 行）。变异注入与判决可解释性的最小定位单元。"""
    path: str
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0


@dataclass(frozen=True)
class ProbeBudget:
    max_mutants: int = 30              # 单次探测变异体上限（含哨兵）
    per_mutant_timeout_s: int = 120    # 单变异体测试运行超时
    total_timeout_s: int = 1200        # 本轮探测总时长上限


@dataclass(frozen=True)
class Mutant:
    mutant_id: str                     # content_fingerprint(path+func_sig+operator+site_ctx)
    path: str                          # 仓库相对路径
    func_sig: str
    operator: str                      # CMP / BOOL / CONST / RET / EXC / SENTINEL(...)
    site: SourceSpan
    original: str                      # 原片段（用于注入定位与恢复校验）
    mutated: str                       # 变体片段
    is_sentinel: bool = False


class VerdictKind(Enum):
    KILLED = auto()
    SURVIVED = auto()
    TIMEOUT = auto()                   # 慢测试，不直接计入 survived；走 suspect/ack
    INFRA_ERROR = auto()               # 构建/注入/环境失败：本变异体判决无效，不产 Finding
    EQUIVALENT_SUSPECT = auto()        # 多轮补测仍存活：走 ack 人裁豁免


@dataclass(frozen=True)
class Verdict:
    mutant_id: str
    kind: VerdictKind
    killing_test: str | None           # 击杀者；KILLED 时必填（__post_init__ 硬校验）
    elapsed_s: float
    path: str = ""                     # 变异体所在文件（供 Finding 定位）
    line: int = 0

    def __post_init__(self):
        # 不变量硬校验：KILLED 必有击杀者标识（判决来源可追溯）。注释约定改为
        # 结构约束——与本模块抗 fail-open 立场一致（PR#134 R1 意见）。
        if self.kind is VerdictKind.KILLED and not self.killing_test:
            raise ValueError(f"Verdict 不变量违反：KILLED 判决必须携带 killing_test（mutant={self.mutant_id}）")


@dataclass(frozen=True)
class CensusIssue:                     # L0 产物
    kind: str                          # skip_counted_as_pass / zero_assertion / trivial_assertion / swallowed_exception
    test_path: str
    test_name: str
    evidence: str


@dataclass(frozen=True)
class TestCommand:
    """复用项目在 Touchstone 配置里声明的测试命令；Probe 不自行发现/调度测试。
    cmd 为 shell 字符串（如 'python -m pytest -q'）；targeted 可选，追加到 cmd 后做定向运行。"""
    cmd: str = "python -m pytest -q"
    cwd: str = "."
    env: dict = field(default_factory=dict)


class ReportStatus(str, Enum):
    """报告状态三态完备（做了/没得做/做废了），str 混入保证与既有 "ok" 字面量比较兼容。
    普通 str 状态机在条件判断中拼写错误会静默失效——改结构化杜绝（PR#134 R1 意见）。"""
    OK = "ok"
    PLAN_EMPTY = "plan_empty"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProbeRunReport:                  # never-silent 强制载体：任何计数缺失即非法
    plan_size: int                     # 计划变异体数（含哨兵）；0 时 status 必为 PLAN_EMPTY
    executed: int
    sentinel_result: VerdictKind       # 非 KILLED（ok 路径下）⇒ status=INVALID
    verdicts: tuple                    # tuple[Verdict]（不含哨兵；INVALID 时为空/被作废）
    census: tuple                      # tuple[CensusIssue]
    status: ReportStatus               # ReportStatus.OK / PLAN_EMPTY / INVALID（str 混入，兼容字面量比较）
    kill_rate: float | None            # killed/(killed+survived)；哨兵与 TIMEOUT 不计入；INVALID/PLAN_EMPTY 为 None
    reason: str = ""                   # PLAN_EMPTY/INVALID 的可读原因（never silent）

    def __post_init__(self):
        # frozen 只冻结字段绑定，不冻结容器内容——verdicts/census 归一为 tuple，
        # 使不可变契约在容器层同样成立（消费方无法事后 append/篡改判决，PR#134 R2 意见）。
        object.__setattr__(self, "verdicts", tuple(self.verdicts))
        object.__setattr__(self, "census", tuple(self.census))
        object.__setattr__(self, "status", ReportStatus(self.status))   # 字面量 → 枚举归一
        # 报告自身不变量硬校验（与 Verdict 同款，PR#134 R3 意见）：矛盾报告不允许被构造。
        if self.plan_size == 0 and self.status is not ReportStatus.PLAN_EMPTY:
            raise ValueError("ProbeRunReport 不变量违反：plan_size==0 时 status 必为 PLAN_EMPTY")
        if self.status is ReportStatus.OK and self.sentinel_result is not VerdictKind.KILLED:
            raise ValueError("ProbeRunReport 不变量违反：status=OK 要求哨兵 KILLED（链路自检未过不得报 ok）")
        if self.status in (ReportStatus.INVALID, ReportStatus.PLAN_EMPTY) and self.kill_rate is not None:
            raise ValueError("ProbeRunReport 不变量违反：INVALID/PLAN_EMPTY 报告不得携带 kill_rate")


_HEX = 6                               # mutant_id 短哈希位数（与数据流图 snt-9f2c71 一致）


def content_fingerprint(path, func_sig, operator, site_ctx, sentinel=False):
    """变异体跨轮身份：代码位点不变则稳定（接 lineage 记账）；代码变了自然失效触发重探。"""
    raw = f"{path}\x00{func_sig}\x00{operator}\x00{site_ctx}".encode("utf-8")
    return ("snt-" if sentinel else "mut-") + hashlib.sha1(raw).hexdigest()[:_HEX]


# ============================ Step 2 · L0 断言普查（census）============================
_ASSERT_CALLS = {"assertEqual", "assertNotEqual", "assertTrue", "assertFalse", "assertIn",
                 "assertRaises", "assertIsNone", "assertGreater", "assertLess", "assertRaisesRegex"}
_TRIVIAL_ASSERT_CALLS = {"assertIsNotNone", "assertIsInstance"}   # 恒真/弱断言（只证"有东西/是某类型"，不证值）


def _is_test_func(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _decorator_names(node):
    out = []
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        # pytest.mark.skip / skipif / xfail  或  unittest.skip
        parts = []
        while isinstance(t, ast.Attribute):
            parts.append(t.attr); t = t.value
        if isinstance(t, ast.Name):
            parts.append(t.id)
        out.append(".".join(reversed(parts)))
    return out


def _assert_stats(fn):
    """统计一个测试函数体内的断言情况：真断言数、弱断言数、是否吞异常。"""
    real = trivial = 0
    swallowed = False
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            # assert True / assert 1 等恒真
            v = n.test
            if isinstance(v, ast.Constant) and bool(v.value):
                trivial += 1
            else:
                real += 1
        elif isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if name in _ASSERT_CALLS or name == "raises" or name == "warns":
                real += 1
            elif name in _TRIVIAL_ASSERT_CALLS:
                trivial += 1
        elif isinstance(n, ast.With):
            for item in n.items:
                c = item.context_expr
                nm = c.func.attr if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) else ""
                if nm in ("raises", "warns"):          # with pytest.raises(...) 算真断言
                    real += 1
    # 吞错型：try/except 捕获后既不 re-raise 也不在 except 内断言/fail。
    # 断言识别口径与上方计数循环一致（_ASSERT_CALLS/_TRIVIAL_ASSERT_CALLS）——
    # except 内 self.assertEqual(str(e), ...) 是合法的异常断言模式，不得误报（PR#133 R3 意见）。
    def _is_assert_call(x):
        if not isinstance(x, ast.Call):
            return False
        name = x.func.attr if isinstance(x.func, ast.Attribute) else getattr(x.func, "id", "")
        return name in _ASSERT_CALLS or name in _TRIVIAL_ASSERT_CALLS or name in ("fail", "raises", "warns")
    for n in ast.walk(fn):
        if isinstance(n, ast.Try):
            for h in n.handlers:
                body = h.body
                has_raise = any(isinstance(x, ast.Raise) for x in ast.walk(h))
                has_assert = any(isinstance(x, ast.Assert) or _is_assert_call(x) for x in ast.walk(h))
                only_pass = all(isinstance(x, ast.Pass) for x in body)
                if (only_pass or not (has_raise or has_assert)):
                    swallowed = True
    return real, trivial, swallowed


def _changed_test_files(scope_facts, repo_dir):
    """触及增量的测试文件：优先 diff 里改到的 test_*.py；否则回落 tests/ 下引用了增量模块名的文件。"""
    changed = [f["path"] for f in (scope_facts or {}).get("changed_files", [])]
    tests = [p for p in changed if os.path.basename(p).startswith("test") and p.endswith(".py")]
    if tests:
        return tests
    # 回落：增量非测试模块名 → 在 tests/ 下找引用者
    mods = {os.path.splitext(os.path.basename(p))[0] for p in changed if p.endswith(".py")}
    found = []
    tdir = os.path.join(repo_dir, "tests")
    if mods and os.path.isdir(tdir):
        for root, _, files in os.walk(tdir):
            for fn in files:
                if fn.startswith("test") and fn.endswith(".py"):
                    fp = os.path.join(root, fn)
                    try:
                        txt = open(fp, encoding="utf-8").read()
                    except OSError:
                        continue
                    if any(m in txt for m in mods):
                        found.append(os.path.relpath(fp, repo_dir))
    return found


def census(scope_facts, repo_dir="."):
    """L0 断言普查：静态扫描触及增量代码的测试，产出四类问题。不运行任何测试。
    直接对标 AKDI 实证失效模式（skip 计通过 / 零断言 / 恒真断言 / 吞错）。"""
    issues = []
    for rel in _changed_test_files(scope_facts, repo_dir):
        abspath = os.path.join(repo_dir, rel)
        try:
            tree = ast.parse(open(abspath, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for fn in ast.walk(tree):
            if not _is_test_func(fn):
                continue
            decos = _decorator_names(fn)
            if any(d.endswith(("skip", "skipif", "xfail")) for d in decos):
                issues.append(CensusIssue("skip_counted_as_pass", rel, fn.name,
                                          f"@{[d for d in decos if d.endswith(('skip','skipif','xfail'))][0]}"))
                continue
            real, trivial, swallowed = _assert_stats(fn)
            if real == 0 and trivial == 0:
                issues.append(CensusIssue("zero_assertion", rel, fn.name, "函数体内无任何断言"))
            elif real == 0 and trivial > 0:
                issues.append(CensusIssue("trivial_assertion", rel, fn.name,
                                          f"仅恒真/弱断言 ×{trivial}（assert True / assertIsNotNone 类）"))
            if swallowed:
                issues.append(CensusIssue("swallowed_exception", rel, fn.name, "except 块吞错：既不 re-raise 也不断言/fail"))
    return issues


# ============================ Step 3 · 探针计划（plan，含哨兵选取）============================
_CMP_FLIP = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">", ast.Eq: "!=", ast.NotEq: "=="}
_CMP_SYM = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}


def _seg(src, node):
    return ast.get_source_segment(src, node)


def _span(path, node):
    return SourceSpan(path, node.lineno, getattr(node, "end_lineno", node.lineno),
                      node.col_offset, getattr(node, "end_col_offset", 0))


def _func_sig(fn):
    return f"{fn.name}(...)"


def _branch_count(fn):
    n = 0
    for x in ast.walk(fn):
        if isinstance(x, (ast.If, ast.For, ast.While, ast.BoolOp)):
            n += 1
        elif isinstance(x, ast.Compare):
            n += len(x.ops)
    return n


def _changed_line_ranges(scope_facts, path):
    """从 scope_facts 的 hunks 取该文件的改动行区间（target_start, +行数）。"""
    out = []
    for f in (scope_facts or {}).get("changed_files", []):
        if f["path"] != path:
            continue
        for start, added, _deleted in f.get("hunks", []):
            if added > 0:
                out.append((start, start + max(added, 1) - 1))
    return out


def _funcs_overlapping(tree, ranges):
    """返回与改动行区间重叠的函数节点（无区间信息则取全部函数——新增文件的常态）。"""
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not ranges:
        return funcs
    hit = []
    for fn in funcs:
        lo, hi = fn.lineno, getattr(fn, "end_lineno", fn.lineno)
        if any(not (hi < rs or lo > re_) for rs, re_ in ranges):
            hit.append(fn)
    return hit


def _mutation_sites(fn, src, path):
    """遍历函数体，产出 (operator, original, mutated, span) 候选。仅覆盖最小高价值算子集。
    docstring / 裸字符串语句（ast.Expr 位置的 Constant）不作 CONST 位点——变异它们
    必然是等价变异体，只会浪费预算并产出噪声 Finding（R1-03）。"""
    doc_consts = {id(n.value) for n in ast.walk(fn)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, str)}
    sites = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and id(n) in doc_consts:
            continue
        if isinstance(n, ast.Compare) and n.ops and type(n.ops[0]) in _CMP_FLIP:
            seg = _seg(src, n)
            if not seg:
                continue
            op = n.ops[0]
            sites.append(("CMP", seg, seg.replace(_CMP_SYM[type(op)], _CMP_FLIP[type(op)], 1), _span(path, n)))
        elif isinstance(n, ast.BoolOp):
            seg = _seg(src, n)
            if not seg:
                continue
            if isinstance(n.op, ast.And):
                # 翻全部 " and "（非仅首个）：a and b and z → a or b or z，对齐 BoolOp 单 op 类型语义
                sites.append(("BOOL", seg, seg.replace(" and ", " or "), _span(path, n)))
            else:
                sites.append(("BOOL", seg, seg.replace(" or ", " and "), _span(path, n)))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            seg = _seg(src, n)
            if seg and seg.startswith("not "):
                sites.append(("BOOL", seg, seg[4:], _span(path, n)))     # 删 not
        elif isinstance(n, ast.Return) and n.value is not None:
            seg = _seg(src, n)
            if seg and seg.strip() != "return None":
                sites.append(("RET", seg, "return None", _span(path, n)))
        elif isinstance(n, ast.Raise):
            seg = _seg(src, n)
            if seg:
                sites.append(("EXC", seg, "pass", _span(path, n)))       # 删 raise
        elif isinstance(n, ast.Constant):
            if isinstance(n.value, bool):
                continue
            if isinstance(n.value, int):
                seg = _seg(src, n)
                if seg:
                    sites.append(("CONST", seg, str(n.value + 1), _span(path, n)))
            elif isinstance(n.value, str) and n.value:
                seg = _seg(src, n)
                if seg:
                    sites.append(("CONST", seg, '""', _span(path, n)))
    return sites


def _low_density_modules(census_issues, repo_dir):
    """census 问题测试文件 → 其 import 的模块名集合。命中这些模块的增量源文件
    视作「低断言密度覆盖面」：变异优先级提高，哨兵选取排除（R1-02）。"""
    mods = set()
    for tp in {c.test_path for c in (census_issues or [])}:
        try:
            tree = ast.parse(open(os.path.join(repo_dir, tp), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[-1] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[-1])
    return mods


def plan(scope_facts, budget, census_issues):
    """由 ScopeFacts 圈定的增量函数集生成探针计划：按（分支数 × 低断言密度）降序选点，
    套用算子集，截断到 budget.max_mutants，并追加一个哨兵变异体。
    计划为空不是异常，但必须由调用方转写为 plan_empty 报告。"""
    repo_dir = (scope_facts or {}).get("_repo_dir", ".")
    py_files = [f["path"] for f in (scope_facts or {}).get("changed_files", [])
                if f["path"].endswith(".py") and not os.path.basename(f["path"]).startswith("test")]
    low_mods = _low_density_modules(census_issues, repo_dir)

    scored = []      # (priority, branch_count, in_low_density, fn_node, src, path)
    for path in py_files:
        try:
            src = open(os.path.join(repo_dir, path), encoding="utf-8").read()
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        ranges = _changed_line_ranges(scope_facts, path)
        in_low = os.path.splitext(os.path.basename(path))[0] in low_mods
        for fn in _funcs_overlapping(tree, ranges):
            bc = _branch_count(fn)
            # 优先级：分支数 × 低断言密度——被弱测试覆盖的文件（census 命中）按文件粒度加权
            scored.append((bc + (5 if in_low else 0), bc, in_low, fn, src, path))
    scored.sort(key=lambda t: -t[0])

    mutants, seen = [], set()
    cap = max(1, budget.max_mutants) - 1        # 预留 1 个名额给哨兵
    for _prio, _bc, _low, fn, src, path in scored:
        sig = _func_sig(fn)
        for operator, orig, mutated, span in _mutation_sites(fn, src, path):
            if len(mutants) >= cap:
                break
            ctx = f"{span.start_line}:{span.start_col}:{orig}"
            mid = content_fingerprint(path, sig, operator, ctx)
            if mid in seen:
                continue
            seen.add(mid)
            mutants.append(Mutant(mid, path, sig, operator, span, orig, mutated))
        if len(mutants) >= cap:
            break

    sentinel = _pick_sentinel(scored, census_issues)
    if sentinel is not None:
        mutants.append(sentinel)
    return mutants


def _pick_sentinel(scored, census_issues):
    """哨兵选取：挑【断言密度最高】（最可能被现有测试覆盖）的函数造一个必被击杀的扰动。
    实现：优先排除低密度覆盖面（census 命中的模块，in_low=True）内的函数，
    在其余函数中按分支数降序取第一个 CMP/RET 位点；若全部函数都在低密度面内，
    退回全量按分支数选（宁可有哨兵，run() 层再由「哨兵未击杀→invalid」兜底）。"""
    ordered = sorted(scored, key=lambda t: (t[2], -t[1]))    # in_low=False 优先，再按分支数降序
    for _prio, _bc, _low, fn, src, path in ordered:
        for operator, orig, mutated, span in _mutation_sites(fn, src, path):
            if operator in ("CMP", "RET"):
                sig = _func_sig(fn)
                ctx = f"sentinel:{span.start_line}:{orig}"
                mid = content_fingerprint(path, sig, f"SENTINEL({operator})", ctx, sentinel=True)
                return Mutant(mid, path, sig, f"SENTINEL({operator})", span, orig, mutated, is_sentinel=True)
    return None


# ============================ Step 4 · 执行与复验（run / replay）============================
import re as _re
_FAILED_RX = _re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", _re.M)


def _find_in_window(text, needle, span):
    """在位点行窗口内定位 needle 的字节偏移（避免替换到文件别处的同名片段）。"""
    lines = text.splitlines(keepends=True)
    start = sum(len(l) for l in lines[: max(0, span.start_line - 1)])
    end = sum(len(l) for l in lines[: min(len(lines), span.end_line)])
    idx = text.find(needle, start, end + len(needle))
    return idx


def _node_byte_span(original_bytes, span):
    """AST col_offset/end_col_offset 是行内 UTF-8 字节偏移 → 节点在文件中的字节起止。
    返回 (start_byte, end_byte) 或 None（行列越界）。精确注入用，免行窗口文本查找命中同名片段（133-3/4/5）。"""
    lines = original_bytes.splitlines(keepends=True)
    if not lines or not (1 <= span.start_line <= len(lines)):
        return None
    # end_line 越界（AST 节点声称跨到文件外/倒序）→ 早返回 None 触发 _find_in_window 兜底，
    # 不静默 clamp 出错位字节区间（#137 review：invalid lines 走 graceful fallback）。
    end_line = span.end_line or span.start_line
    if not (span.start_line <= end_line <= len(lines)):
        return None
    start_byte = sum(len(l) for l in lines[: span.start_line - 1]) + max(0, span.start_col)
    end_byte = sum(len(l) for l in lines[: end_line - 1]) + max(0, span.end_col)
    if end_byte < start_byte:
        return None
    return start_byte, end_byte


def _run_tests(test_cmd, timeout):
    """跑一次测试命令。返回 (rc, out, timed_out)。rc!=0 视为测试失败=击杀。"""
    env = dict(os.environ); env.update(test_cmd.env or {})
    try:
        cp = subprocess.run(shlex.split(test_cmd.cmd), cwd=test_cmd.cwd, env=env,
                            capture_output=True, text=True, timeout=timeout)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or ""), False
    except subprocess.TimeoutExpired as e:
        def _s(x):                      # stdout/stderr 各自归一：None/bytes/str → str（R1-04）
            if x is None:
                return ""
            return x if isinstance(x, str) else x.decode("utf-8", errors="replace")
        return None, _s(e.stdout) + _s(e.stderr), True


def _inject_and_run(mutant, test_cmd, timeout):
    """注入单个变异体 → 跑测 → 恢复源码（finally 保护），产出 Verdict。
    注入失败/环境异常 → INFRA_ERROR（与 SURVIVED 严格区分，不产 Finding）。"""
    abspath = os.path.join(test_cmd.cwd, mutant.path)
    t0 = time.monotonic()
    try:
        original_bytes = open(abspath, "rb").read()
    except OSError:
        return Verdict(mutant.mutant_id, VerdictKind.INFRA_ERROR, None, 0.0, mutant.path, mutant.site.start_line)
    text = original_bytes.decode("utf-8", errors="replace")
    # 优先 AST col_offset 精确字节切片（133-3/4/5）：消除行窗口文本查找命中同名片段的风险。
    # 仅在切片解码恰等于 mutant.original 时采用；否则回退 _find_in_window（多字节/行列漂移兜底）。
    mutated_text = None
    bs = _node_byte_span(original_bytes, mutant.site)
    if bs is not None:
        s, e = bs
        if original_bytes[s:e].decode("utf-8", errors="replace") == mutant.original:
            mutated_bytes = original_bytes[:s] + mutant.mutated.encode("utf-8") + original_bytes[e:]
            mutated_text = mutated_bytes.decode("utf-8", errors="replace")
    if mutated_text is None:
        idx = _find_in_window(text, mutant.original, mutant.site)
        if idx < 0:
            return Verdict(mutant.mutant_id, VerdictKind.INFRA_ERROR, None,
                           round(time.monotonic() - t0, 3), mutant.path, mutant.site.start_line)    # 注入落点失效：判无效
        mutated_text = text[:idx] + mutant.mutated + text[idx + len(mutant.original):]
    try:
        # 注入后先做语法自检：变体若不可解析 → INFRA_ERROR，不浪费一次测试运行
        try:
            ast.parse(mutated_text)
        except SyntaxError:
            return Verdict(mutant.mutant_id, VerdictKind.INFRA_ERROR, None,
                           round(time.monotonic() - t0, 3), mutant.path, mutant.site.start_line)
        open(abspath, "w", encoding="utf-8").write(mutated_text)
        rc, out, timed_out = _run_tests(test_cmd, timeout)
        elapsed = round(time.monotonic() - t0, 3)
        if timed_out:
            return Verdict(mutant.mutant_id, VerdictKind.TIMEOUT, None, elapsed, mutant.path, mutant.site.start_line)
        if rc != 0:
            m = _FAILED_RX.search(out)
            return Verdict(mutant.mutant_id, VerdictKind.KILLED, m.group(1) if m else "(unknown)", elapsed, mutant.path, mutant.site.start_line)
        return Verdict(mutant.mutant_id, VerdictKind.SURVIVED, None, elapsed, mutant.path, mutant.site.start_line)
    except Exception:
        return Verdict(mutant.mutant_id, VerdictKind.INFRA_ERROR, None, round(time.monotonic() - t0, 3), mutant.path, mutant.site.start_line)
    finally:
        try:
            open(abspath, "wb").write(original_bytes)   # 恢复必达：崩溃也不留脏工作区
        except OSError as err:
            print(f"[probe] 源码恢复失败，中止 run 以防级联污染：{abspath}", file=sys.stderr)
            raise WorkspaceCorrupted(abspath) from err    # B904：保留 OSError 链供诊断（磁盘满/权限等）


def _kill_rate(verdicts):
    killed = sum(1 for v in verdicts if v.kind is VerdictKind.KILLED)
    survived = sum(1 for v in verdicts if v.kind is VerdictKind.SURVIVED)
    denom = killed + survived
    return (killed / denom) if denom else None


def run(mutants, test_cmd, budget, census_issues):
    """逐个注入变异体、运行定向测试、恢复源码、记录判决。哨兵最先执行；
    哨兵未被击杀 → 立即终止并返回 status=invalid。任何路径都产出完整计数（never silent）。
    census_issues 为必传（无问题需显式传 []）：L0 结果是静态 Finding 的核心来源，
    可选默认极易被调用方遗漏而静默丢失整层检测（PR#134 R1 意见）。"""
    if not mutants:
        return ProbeRunReport(0, 0, VerdictKind.INFRA_ERROR, [], census_issues,
                              ReportStatus.PLAN_EMPTY, None, reason="diff 内无可变异目标（新增/改动的 Python 函数为空）")

    sentinels = [m for m in mutants if m.is_sentinel]
    reals = [m for m in mutants if not m.is_sentinel]

    # ① 哨兵最先执行：链路自检。无哨兵 = 链路自检不可用 = 本轮无效（R1-01：
    #    绝不允许「没自检但绿灯」——那正是本模块要防御的 fail-open 形态）。
    if not sentinels:
        return ProbeRunReport(len(mutants), 0, VerdictKind.INFRA_ERROR, [], census_issues,
                              ReportStatus.INVALID, None,
                              reason="无法选取哨兵变异体 → 链路自检不可用，本轮判决无效")
    try:
        sv = _inject_and_run(sentinels[0], test_cmd, budget.per_mutant_timeout_s)
    except WorkspaceCorrupted:
        return ProbeRunReport(len(mutants), 1, VerdictKind.INFRA_ERROR, [], census_issues,
                              ReportStatus.INVALID, None,
                              reason="哨兵源码恢复失败（工作区脏）→ 中止，本轮判决无效")
    sent_kind = sv.kind
    if sent_kind is not VerdictKind.KILLED:
        return ProbeRunReport(len(mutants), 1, sent_kind, [], census_issues, ReportStatus.INVALID, None,
                              reason=f"哨兵未被击杀（{sent_kind.name}）→ 探针链路故障，本轮判决全部作废")

    # ② 预算内逐个执行
    verdicts, t_start, executed = [], time.monotonic(), 1   # 哨兵已执行（此处必有哨兵）
    for m in reals:
        if time.monotonic() - t_start > budget.total_timeout_s:
            break                       # 总时长预算用尽：已执行的照常报告，未执行的不沉默（executed<plan_size 即体现）
        executed += 1
        try:
            verdicts.append(_inject_and_run(m, test_cmd, budget.per_mutant_timeout_s))
        except WorkspaceCorrupted:
            # 源码恢复失败 → 工作区脏：后续 mutant 基线会错 → 中止，判 INVALID（防级联误判）
            return ProbeRunReport(len(mutants), executed, sent_kind, verdicts, census_issues,
                                  ReportStatus.INVALID, _kill_rate(verdicts),
                                  reason="源码恢复失败（工作区脏）→ 中止以防后续 mutant 基线被污染")

    return ProbeRunReport(len(mutants), executed, sent_kind, verdicts, census_issues,
                          ReportStatus.OK, _kill_rate(verdicts))


def replay(mutant, test_cmd, budget=None):
    """定向复验单个变异体，用于下一轮验证 Probe 类 Finding 的 done_criteria（击杀即闭环）。
    成本为一次测试运行。注：接口取 Mutant（非仅 id）——仅凭 id 无法重建注入编辑。"""
    budget = budget or ProbeBudget()
    return _inject_and_run(mutant, test_cmd, budget.per_mutant_timeout_s)


# ============================ Step 5 · 转 Finding 接线（to_findings）============================
_CENSUS_META = {
    "skip_counted_as_pass": ("PROBE-CENSUS-SKIP", "warn",
        "移除 skip/xfail 或将其计入未通过口径；被跳过的用例不能算作护栏。"),
    "zero_assertion": ("PROBE-CENSUS-ZEROASSERT", "warn",
        "为该测试补断言：断到被测行为的可观测结果上，而非仅调用不校验。"),
    "trivial_assertion": ("PROBE-CENSUS-TRIVIAL", "warn",
        "把恒真/弱断言（assert True / assertIsNotNone）替换为对返回值/状态的实质断言。"),
    "swallowed_exception": ("PROBE-CENSUS-SWALLOW", "warn",
        "不要在测试里吞异常：捕获后应 re-raise 或对异常本身断言，否则失败被静默。"),
}


def _finding(rule_id, file, line, severity, rationale, fix_direction, done_criteria, mutant_id=None):
    """产出与 checklist 消费口径一致的 Finding dict（fix_direction / fix_reasoning / done_criteria）。"""
    f = {
        "rule_id": rule_id, "file": file, "line": line, "category": "test_effectiveness",
        "severity": severity, "confidence": 1.0, "rationale": rationale, "agent": "probe",
        "fix_direction": fix_direction, "fix_reasoning": rationale,
        "done_criteria": done_criteria, "suggested_fix": fix_direction,
    }
    if mutant_id:
        f["mutant_id"] = mutant_id            # 接 lineage 指纹：跨轮身份，replay 复验用
    return f


def to_findings(report):
    """report → 标准 Finding：
      survived            → open Finding（补断言/补场景；done_criteria=replay 击杀该变异体）
      timeout/equivalent  → pending_ack Finding（走 ack 人裁）
      census 问题         → 对应静态类 Finding
      invalid             → 不产常规 Finding，而是一条针对探针链路本身的 P0 Finding
      plan_empty          → 一条 info Finding（never silent：没做事也要显式说明，不给假绿灯）
    killed 不产 Finding（计入 kill_rate）。infra_error 不产 Finding（判决无效）。"""
    findings = []

    # 静态类（census）——任何 status 下都汇报
    for c in report.census:
        rule_id, sev, fix = _CENSUS_META.get(c.kind,
            ("PROBE-CENSUS", "warn", "改进该测试的断言有效性。"))
        findings.append(_finding(
            rule_id, c.test_path, 0, sev,
            f"L0 断言普查：{c.test_name} — {c.evidence}", fix,
            {"kind": "review", "spec": {"recheck": f"census:{c.kind}:{c.test_name}"}}))

    if report.status == ReportStatus.INVALID:
        findings.append(_finding(
            "PROBE-INVALID", "", 0, "P0",
            f"探针链路故障：{report.reason}",
            "排查测试命令是否真正执行、变异注入是否落盘、判决逻辑是否正确；修复后重跑探测。",
            {"kind": "deterministic", "spec": {"replay_sentinel_killed": True}}))
        return findings

    if report.status == ReportStatus.PLAN_EMPTY:
        findings.append(_finding(
            "PROBE-EMPTY", "", 0, "info",
            f"本轮探测无可变异目标：{report.reason}（探针已运行，非静默跳过）。",
            "若增量确含应被测试保护的逻辑，检查 ScopeFacts 是否正确圈定了改动函数。",
            {"kind": "review", "spec": {"recheck": "probe:plan_empty"}}))
        return findings

    # ok 路径：动态判决
    for v in report.verdicts:
        if v.kind is VerdictKind.SURVIVED:
            findings.append(_finding(
                "PROBE-SURVIVED", v.path, v.line, "warn",
                f"变异体 {v.mutant_id} 存活：测试跑过但未能检测该语义扰动（跑过了但没断住）。",
                "为对应函数补断言或补测试场景，使该扰动可被检测。",
                {"kind": "deterministic", "spec": {"replay_mutant": v.mutant_id}},
                mutant_id=v.mutant_id))
        elif v.kind in (VerdictKind.TIMEOUT, VerdictKind.EQUIVALENT_SUSPECT):
            findings.append(_finding(
                "PROBE-SUSPECT", "", 0, "warn",
                f"变异体 {v.mutant_id} 判决为 {v.kind.name}：未确证存活，需人工裁定（超时/疑似等价）。",
                "确认是慢测试还是真未覆盖；等价变异体经 touchstone-ack 豁免，否则补测试。",
                {"kind": "review", "spec": {"ack_or_kill": v.mutant_id}},
                mutant_id=v.mutant_id))
    return findings

#!/usr/bin/env python3
# ============================================================================
# verify/runners.py —— 语言 runner 层（执行/覆盖/变异的全部落地实现）
# ----------------------------------------------------------------------------
# 从 verify_change 拆出（第三轮工程化加固）。verify_change 只管【裁决编排】
# （plan/execute 两阶段、判过条件、充分性阶梯语义）；本模块管每种语言【怎么落地】：
#   PythonRunner —— pytest 执行 / coverage 改动覆盖 / AST 最小变异（或外置 mutmut）
#   MavenRunner  —— JUnit5 执行 / JaCoCo 改动覆盖 / PIT 变异率解析
# 新语言（Go/TS/…）在此新增 Runner 类并挂入 select_runner——这是原设计头注
# "换语言只需替换 LANG RUNNER" 的正式扩展点，verify_change 无需改动。
# ============================================================================

import ast
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile

TEST_TIMEOUT = 300


class MutationRunError(RuntimeError):
    """变异测试【跑了但未产出可用分数】——调用失败 / 未产出报告。

    与 mutation() 返回 None（=未跑：非高风险 / 非 full_suite）严格区分：None 表示
    "压根没跑变异"，下游 mut_ok 视作放行（不据此判不过）；本异常表示"跑了但失败"，
    没有变异证据可证明测试充分——下游必须保守判 inadequate，**不许掩盖成 adequate**。
    （B1：PIT 调用失败 / 空 mutations.xml 报告 曾被 return None 吞掉 → 被 _grade 当
    mut_ok=True → verdict adequate，静默放过弱测试。此异常是那道闸。）"""


# --- 接口抽取：只取公共签名、去实现（LANG RUNNER）----------------------------
def _extract_interface(work_dir, changed_files):
    """给独立验收测试作者'调什么'，不给'怎么实现'。"""
    sigs = []
    for fp in changed_files:
        if not fp.endswith(".py"):
            continue
        path = os.path.join(work_dir, fp)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except SyntaxError:
            continue
        mod = fp[:-3].replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = ", ".join(a.arg for a in node.args.args)
                sigs.append(f"{mod}: def {node.name}({args})")
            elif isinstance(node, ast.ClassDef):
                sigs.append(f"{mod}: class {node.name}")
    return "\n".join(sigs) or "(未能抽取签名)"


# --- 跑生成测试（LANG RUNNER）。返回 (passed, output) -------------------------
def _run_tests(work_dir, test_code):
    tf = os.path.join(work_dir, "_touchstone_spec_test.py")
    with open(tf, "w", encoding="utf-8") as f:
        f.write(test_code)
    try:
        r = subprocess.run(["python", "-m", "pytest", "-q", "_touchstone_spec_test.py"],
                           cwd=work_dir, capture_output=True, encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT)
        return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        if os.path.exists(tf):
            os.remove(tf)


# --- 改动文件覆盖率（简化：文件级；改动行级映射为后续细化）-------------------
def _run_coverage_subprocess(work_dir, pytest_args):
    """在 work_dir 跑 coverage run + pytest（子进程隔离），返回 coverage.Coverage 对象。
    用 coverage API 直接读 .coverage 数据文件（替代脆弱的 coverage json -o - stdout 解析）。
    清除旧 .coverage 防 stale data（pr-agent 第2轮评审意见）；测试失败时 coverage 仍会写出
    .coverage 数据，故不因 pytest 非零退出码而放弃采集——仅当数据文件未生成（coverage 自身
    崩溃/采集前 collection 错误/超时未落盘）才报错（pr-agent 第3轮评审意见）。"""
    cov_file = os.path.join(work_dir, ".coverage")
    if os.path.exists(cov_file):
        os.remove(cov_file)          # 清旧数据——防上次 run 的 stale coverage 误导
    r = subprocess.run(["python", "-m", "coverage", "run", "--source=."] + pytest_args,
                       cwd=work_dir, capture_output=True, encoding="utf-8", errors="replace",
                       timeout=TEST_TIMEOUT)
    # 测试失败（pytest 非零退出）时 coverage 仍会写出 .coverage——这是有效覆盖数据，不应因
    # 退出码非零而丢弃。仅当数据文件压根没产出（coverage 自身崩溃 / 采集前 collection 错误 /
    # 超时未及落盘）才判定失败并 raise。配合起跑前的清空，此时无 stale 可加载，亦满足第2轮
    # "校验子进程、不加载 stale 数据"的要求——校验手段从"退出码==0"改为"数据是否产出"，更精确。
    if not os.path.exists(cov_file):
        raise RuntimeError(f"coverage 未产出数据（exit {r.returncode}）：{(r.stderr or '')[-300:]}")
    import coverage
    cov = coverage.Coverage(data_file=cov_file)
    cov.load()
    return cov


def _coverage_ratio(cov, py_files, changed_lines=None):
    """从 coverage.Coverage 对象算覆盖率。改动行级（若有）优先，否则文件级。
    用 cov.analysis2（Coverage 对象方法）而非 data.missing_lines（CoverageData 没有
    此方法，会 AttributeError 被 except 吞掉静默返回 0.0——pr-agent 审计发现）。"""
    if changed_lines:
        coverable = covered = 0
        for path, lines in (changed_lines or {}).items():
            # lines 形参可能是 list（调用方传入）——set & list 会抛 TypeError 被上层 except
            # 吞掉导致覆盖率计算静默失败（pr-agent 第3轮 :91）。统一 cast 成 set 消除根因。
            line_set = set(lines) if lines else set()
            try:
                _, statements, _, missing, _ = cov.analysis2(path)
            except (KeyError, Exception):
                continue
            executed = set(statements) - set(missing)
            cov_set = (set(statements) | set(missing)) & line_set
            coverable += len(cov_set)
            covered += len(executed & cov_set)
        return (covered / coverable) if coverable else 1.0
    ratios = []
    for f in py_files:
        try:
            _, statements, _, missing, _ = cov.analysis2(f)
        except (KeyError, Exception):
            continue
        total = len(set(statements) | set(missing))
        executed = len(set(statements) - set(missing))
        ratios.append(executed / total if total else 0.0)
    return sum(ratios) / len(ratios) if ratios else 0.0


def _changed_file_coverage(work_dir, test_code, changed_files, changed_lines=None):
    py = [f for f in changed_files if f.endswith(".py")]
    if not py:
        return 1.0
    tf = os.path.join(work_dir, "_touchstone_spec_test.py")
    with open(tf, "w", encoding="utf-8") as f:
        f.write(test_code)
    try:
        cov = _run_coverage_subprocess(work_dir, ["-m", "pytest", "-q", "_touchstone_spec_test.py"])
        return _coverage_ratio(cov, py, changed_lines)
    except Exception as e:
        import sys
        print(f"[verify] 覆盖率测量失败（返回 0.0）: {type(e).__name__}: {e}", file=sys.stderr)
        return 0.0
    finally:
        if os.path.exists(tf):
            os.remove(tf)


# --- 最小变异（高风险）。返回击杀率（LANG RUNNER；生产应换 mutmut/cosmic-ray）--
# AST 级变异（stdlib ast 真解析，作用于语法节点，不碰注释/字符串；远强于字符串替换）：
#   关系 Eq<->NotEq / Lt<->GtE / Gt<->LtE，算术 Add<->Sub / Mult<->Div，
#   布尔 And<->Or，布尔常量 True<->False。每个可变异点产一个变异体。
# 说明：mutmut(2.x 要求 tests/ 目录、3.x 配置驱动且有状态)均跑"发现到的整套测试"，
#   与本处"临时 worktree + 仅跑生成的独立验收测试 + 只针对改动文件"不贴合；故 Python 侧
#   用作用域精确、可在离线验证的 AST 变异。Java 侧用成熟的 PIT(见 MavenRunner)。
def _parse_mutation_output(out):
    """从外部变异工具输出的【最后一个】形如 0.83 / 83% 的数取击杀率（0~1）；解析不出返回 None。"""
    import re as _re
    m = _re.findall(r"(\d+(?:\.\d+)?)\s*%|(?<![\d.])(0?\.\d+|1\.0|0|1)(?![\d.])", out or "")
    for pct, frac in reversed(m):
        if pct:
            return min(1.0, float(pct) / 100.0)
        if frac:
            return float(frac)
    return None


def external_mutation_score(work_dir, changed_files):
    """成熟工具接缝（对照 mutmut/cosmic-ray/PIT）：设 TOUCHSTONE_MUTATION_CMD 时改用外部命令
    算击杀率。命令在 work_dir 运行，支持两个占位符：
      {files}        —— 替换为改动文件列表。changed_files 来自被检 PR 的 diff，是【PR author
                       可控输入】：author 提交名为 `x;恶意命令;.py` 的文件即可在 verify 进程注入
                       执行（恰是 DANGER-001 点名的构造——门禁自身先过自己的门）。故每个文件名
                       必须 shlex.quote。命令模板本身启用 shell 是刻意的（部署方要写管道/重定向），
                       可信边界见 _run_mutation_cmd。
      {result_file}  —— runner 在系统 temp 下随机子目录里建的文件绝对路径。工具把【真击杀率】
                       写进它，runner 读该文件取分、**丢弃 stdout**。这是防伪主路径（见下）。

    信任阶梯（#111 防伪：外部变异命令最终要跑 PR 的测试，author 的 conftest/test 可往 stdout
    末尾喷一个高数——旧实现 _parse_mutation_output 取 stdout 最后一个数 → 顶过 MUT_MIN 判
    adequate，弱测试骗过变异门。与 #79 B1 同类假过）：
      · 模板含 {result_file}  → 跑命令、读结果文件第一行取分、弃 stdout（可信路径）。结果文件在
        系统 temp 的**随机名子目录**内、文件名亦随机（堵住固定模式 glob 定位 + stdout 廉价 spoof）；
        只读第一行防多指标报告误取末数与 append-spoof。**残留**：工具与 PR 测试同 UID、无 sandbox，
        同 UID 可 listdir 枚举 + 覆盖文件——非彻底闭环，真正闭环需不同 UID/sandbox（详见
        _external_score_via_result_file 的诚实标注）。
      · 模板不含 {result_file}：默认**不**解析 stdout → 返回 None（回退内置 AST 变异）。仅当显式
        设 TOUCHSTONE_MUTATION_TRUST_STDOUT=1 才回到旧 stdout 解析（兼容仅往 stdout 喷分数的
        工具，但部署方知情接受 spoof 风险）。

    命令失败（rc!=0）/ 超时 / 结果文件缺失或解析不出 / TIMEOUT 值畸形 → 一律返回 None，回退内置
    变异（A5-F2：崩溃工具可能在 stdout 喷误导性百分数，绝不据此判 adequate；int() 留在 try 内，
    畸形 TIMEOUT→ValueError→None 保 graceful degradation）。默认 off（未设 TOUCHSTONE_MUTATION_CMD
    → return None → 回退内置 AST 变异，不受此接缝影响）。"""
    cmd = os.environ.get("TOUCHSTONE_MUTATION_CMD")
    if not cmd:
        return None
    files_sub = " ".join(shlex.quote(f) for f in changed_files or [])
    try:
        timeout = int((os.environ.get("TOUCHSTONE_MUTATION_TIMEOUT") or "").strip() or "900")   # 留 try 内：畸形值→ValueError→None（保 graceful degradation）
        if "{result_file}" in cmd:
            return _external_score_via_result_file(cmd, files_sub, work_dir, timeout)
        if os.environ.get("TOUCHSTONE_MUTATION_TRUST_STDOUT") != "1":
            return None                 # #111 安全默认：旧 stdout 模板无显式 opt-in → 弃 stdout → 回退内置 AST 变异
        r = _run_mutation_cmd(cmd.replace("{files}", files_sub), work_dir, timeout)
        if r is None:
            return None                 # rc!=0 → 命令失败，不信 stdout（防崩溃工具的误导性输出顶满分）
        return _parse_mutation_output(r.stdout)
    except Exception:
        return None


def _run_mutation_cmd(full, work_dir, timeout):
    """跑外部变异命令、rc!=0 返回 None。**启用 shell**（subprocess 的 shell 模式）是
    TOUCHSTONE_MUTATION_CMD 模板契约的固有需求——部署方依赖 shell 管道/重定向（`&&`、`>`）写
    模板，非滥用。可信边界（DANGER-001「确需使用、已说明可信边界」）：PR 可控输入仅经 {files} 替换
    进 full，已在调用前 shlex.quote（注入面闭合）；{result_file}（若有）是 runner 自持随机路径、亦
    quote；模板字符串本身是部署可信 secret、非 PR 可控。故不改为 list 形式（会破坏模板契约的 shell
    语义）。rc!=0 → None（崩溃工具可能在 stdout 喷误导性百分数，绝不据此判 adequate，A5-F2）。"""
    r = subprocess.run(full, shell=True, cwd=work_dir, capture_output=True,
                       text=True, timeout=timeout)
    return None if r.returncode != 0 else r


def _external_score_via_result_file(cmd, files_sub, work_dir, timeout):
    """可信路径落地：runner 在系统 temp 下建**随机名子目录**、结果文件用**随机名**置于其内，工具
    把真击杀率写进文件**第一行**（约定 score 在 line 1），runner 读第一行取分、**完全不看 stdout**。
    随机目录名 + 随机文件名堵住了 #111 收口的廉价 spoof（往 stdout 喷假满分 + glob 固定模式定位结果
    文件）；只读第一行同时防住工具写多指标报告误取末数、以及 append-spoof（攻击者往文件末尾 append
    假分，不动第一行）。

    **诚实标注残留风险**（非密码学不可破，部署方按 opt-in 接缝知情接受）：工具与 PR 测试**同 UID**
    跑、且无 sandbox/chroot，故 /tmp 通常可被同 UID 枚举（`os.listdir('/tmp')` 发现随机目录、再
    listdir 找文件）；攻击者可在工具写真分后、runner 读前**覆盖/篡改第一行**。随机化 + 只读第一行把
    spoof 从「一行 print」抬到「同 UID 枚举 + 覆盖文件」的构造，**非彻底闭环**。真正闭环需把工具跑在
    不同 UID 或 sandbox/private-temp（0600）下——超本 PR scope，留作未来加固方向。"""
    result_dir = tempfile.mkdtemp(prefix=secrets.token_hex(6))
    # 文件名亦随机化：仅随机目录名 + 固定文件名 score 仍可 glob('/tmp/<rand>/score') 命中。文件名
    # secrets.token_hex 后顶层 /tmp 无固定前缀/后缀/文件名可 glob——堵住「固定模式 glob 定位」的廉价
    # spoof。残留：同 UID 仍可 os.listdir 枚举（见 docstring 诚实标注）。
    result_path = os.path.join(result_dir, secrets.token_hex(8))
    try:
        # 先替 {result_file} 再替 {files}：str.replace 单趟扫描、不对替换结果再扫描，故先替占位符
        # 可避开 files_sub（PR 可控文件名）含 "{result_file}" 字面时被第二次 replace 误伤、把真
        # result 路径注入 {files} 位置的注入面（PR 作者把文件命名成 {result_file}）。
        full = cmd.replace("{result_file}", shlex.quote(result_path)).replace("{files}", files_sub)
        r = _run_mutation_cmd(full, work_dir, timeout)
        if r is None:
            return None                 # rc!=0 → 命令失败，不信结果文件（工具崩溃可能留下中途/空文件）
        with open(result_path, "r", encoding="utf-8") as fh:
            # 只读第一行（约定：击杀率在 line 1）——避免工具写多指标报告（score:50%\ncoverage:80%）
            # 取末数误判、也防 append-spoof（攻击者往末尾 append 假分，不动第一行）。
            return _parse_mutation_output(fh.readline())
    finally:
        # shutil.rmtree(ignore_errors=True) 容忍目录非空：外部工具可能往 result_dir 写日志/临时产物，
        # os.rmdir 仅删空目录会让残留文件挡住删除、泄漏随机目录（每轮一份）；ignore_errors 同时兜底
        # 「文件未创建」与「目录非空」两种情形。
        shutil.rmtree(result_dir, ignore_errors=True)


_MUT_CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE,
            ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
            ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_MUT_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
            ast.FloorDiv: ast.Div, ast.Mod: ast.Mult, ast.Pow: ast.Mult,
            ast.LShift: ast.RShift, ast.RShift: ast.LShift,
            ast.BitOr: ast.BitAnd, ast.BitAnd: ast.BitOr, ast.BitXor: ast.BitAnd}
_MUT_BOOL = {ast.And: ast.Or, ast.Or: ast.And}
_MUT_UNARY = {ast.USub: ast.UAdd, ast.UAdd: ast.USub, ast.Not: None, ast.Invert: None}


def _mutation_sites(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and n.ops and type(n.ops[0]) in _MUT_CMP:
            out.append(n)
        elif isinstance(n, ast.BinOp) and type(n.op) in _MUT_BIN:
            out.append(n)
        elif isinstance(n, ast.BoolOp) and type(n.op) in _MUT_BOOL:
            out.append(n)
        elif isinstance(n, ast.UnaryOp) and type(n.op) in _MUT_UNARY:
            out.append(n)
        elif isinstance(n, ast.Constant) and isinstance(n.value, bool):
            out.append(n)
        elif isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            out.append(n)
    return out


def _ast_mutants(src):
    """对源码各可变异点各产一个变异体（一次一个），返回变异体源码列表。"""
    try:
        base = ast.parse(src)
    except SyntaxError:
        return []
    mutants = []
    for i in range(len(_mutation_sites(base))):
        tree = ast.parse(src)
        node = _mutation_sites(tree)[i]
        if isinstance(node, ast.Compare):
            node.ops[0] = _MUT_CMP[type(node.ops[0])]()
        elif isinstance(node, ast.BinOp):
            node.op = _MUT_BIN[type(node.op)]()
        elif isinstance(node, ast.BoolOp):
            node.op = _MUT_BOOL[type(node.op)]()
        elif isinstance(node, ast.UnaryOp):
            new_op = _MUT_UNARY[type(node.op)]
            if new_op is not None:
                node.op = new_op()               # USub↔UAdd
            else:
                node.op = ast.UAdd() if isinstance(node.operand, ast.Constant) else ast.Not()
                # Not/Invert → 降为恒等（移除否定）；粗近似——变异测试重在"改了什么"
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                node.value = not node.value       # True↔False
            elif isinstance(node.value, (int, float)):
                node.value = node.value + 1 if node.value != 0 else 1  # int/float ±1
        try:
            mutants.append(ast.unparse(ast.fix_missing_locations(tree)))
        except (ValueError, AttributeError):
            pass
    return mutants


def _mutation_check(work_dir, test_code, changed_files):
    """变异充分性：对改动的 .py 文件注入 AST 级变异，看生成的独立验收测试能否杀掉。
    击杀率 = 测试挂掉的变异数 / 注入数。"""
    applied = killed = 0
    for fp in [f for f in changed_files if f.endswith(".py")]:
        path = os.path.join(work_dir, fp)
        if not os.path.exists(path):
            continue
        orig = open(path, encoding="utf-8").read()
        try:
            for mut in _ast_mutants(orig):
                applied += 1
                with open(path, "w", encoding="utf-8") as f:
                    f.write(mut)               # 注入一个变异
                passed, _ = _run_tests(work_dir, test_code)
                if not passed:                 # 测试挂掉 = 变异被杀
                    killed += 1
        finally:
            with open(path, "w", encoding="utf-8") as f:
                f.write(orig)                  # 始终还原
    return (killed / applied) if applied else 1.0


# ============================================================================
# 语言运行器（runner 可插拔，设计 §6.4）
#   每个 runner：run_suite / changed_coverage / mutation / extract_interface /
#   run_generated / supports_spec_blind。换语言 = 换一个 runner。
# ============================================================================
def _run(cmd, work_dir, timeout=TEST_TIMEOUT):
    try:
        r = subprocess.run(cmd, cwd=work_dir, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError as e:
        return False, f"命令不存在: {e}"


class PythonRunner:
    lang = "python"
    supports_spec_blind = True

    def run_suite(self, work_dir):
        return _run(["python", "-m", "pytest", "-q"], work_dir)

    def changed_coverage(self, work_dir, changed_files, changed_lines=None):
        return _suite_coverage_python(work_dir, changed_files, changed_lines)

    def mutation(self, work_dir, changed_files, test_code=None):
        ext = external_mutation_score(work_dir, changed_files)
        if ext is not None:
            return ext
        return _mutation_check(work_dir, test_code, changed_files) if test_code else None

    def extract_interface(self, work_dir, changed_files):
        return _extract_interface(work_dir, changed_files)

    def run_generated(self, work_dir, test_code):
        return _run_tests(work_dir, test_code)

    def cover_generated(self, work_dir, test_code, changed_files, changed_lines=None):
        return _changed_file_coverage(work_dir, test_code, changed_files, changed_lines)


class MavenRunner:
    lang = "maven"
    supports_spec_blind = True    # JUnit 5 独立验收测试生成 + 放置到 src/test/java + mvn test

    def _mvn(self, work_dir, goals):
        exe = "./mvnw" if os.path.exists(os.path.join(work_dir, "mvnw")) else "mvn"
        return _run([exe, "-B", "-ntp", *goals], work_dir, timeout=TEST_TIMEOUT * 4)

    def run_suite(self, work_dir):
        return self._mvn(work_dir, ["verify"])

    def changed_coverage(self, work_dir, changed_files, changed_lines=None):
        return (_jacoco_changed_line_coverage(work_dir, changed_files, changed_lines)
                if changed_lines else _jacoco_changed_coverage(work_dir, changed_files))

    def mutation(self, work_dir, changed_files, test_code=None):
        ok, detail = self._mvn(work_dir, ["org.pitest:pitest-maven:mutationCoverage"])
        if not ok:
            # PIT 调用本身失败（mvn 非零退出/超时/mvn 不在）→ 绝不当"未跑"掩盖成 adequate
            raise MutationRunError(
                "PIT mutationCoverage 调用失败（mvn 非零退出/超时）："
                + (detail or "").strip()[-400:])
        score = _pit_score(work_dir)
        if score is not None:
            return score
        # PIT 退出码 0 但 _pit_score 取不到分数：区分"未产出报告"(失败) 与"报告零变异"(无可变异→通过)
        if _pit_has_report(work_dir):
            return 1.0   # 报告在、零变异点 = 无可变异（与 Python _mutation_check applied==0→1.0 对齐）
        raise MutationRunError(
            "PIT 退出码 0 但未产出 mutations.xml 报告（变异未实际执行："
            "疑似 targetClasses 未命中 / PIT 跳过被测模块）")

    def extract_interface(self, work_dir, changed_files):
        return _extract_java_signatures(work_dir, changed_files)

    def run_generated(self, work_dir, test_code):
        cname, _ = _place_junit(work_dir, test_code)
        return self._mvn(work_dir, ["test", f"-Dtest={cname}"])

    def cover_generated(self, work_dir, test_code, changed_files, changed_lines=None):
        cname, _ = _place_junit(work_dir, test_code)
        self._mvn(work_dir, ["test", f"-Dtest={cname}",
                             "org.jacoco:jacoco-maven-plugin:report"])
        return self.changed_coverage(work_dir, changed_files, changed_lines)


def select_runner(repo_dir, changed_files):
    if os.path.exists(os.path.join(repo_dir, "pom.xml")) or \
       any(f.endswith(".java") for f in (changed_files or [])):
        return MavenRunner()
    if any(f.endswith(".py") for f in (changed_files or [])):
        return PythonRunner()
    return None     # 非 Python/Java：verify 参考实现不支持 → verify_change 给中性结果，不再误生成 pytest



# --- JaCoCo 改动覆盖率 / PIT 变异率解析（Maven）------------------------------
def _jacoco_changed_coverage(work_dir, changed_files):
    import glob
    import xml.etree.ElementTree as ET
    java = {os.path.basename(f) for f in changed_files if f.endswith(".java")}
    if not java:
        return 1.0
    reports = (glob.glob(os.path.join(work_dir, "**/target/site/jacoco/jacoco.xml"), recursive=True)
               + glob.glob(os.path.join(work_dir, "**/target/site/jacoco-aggregate/jacoco.xml"),
                           recursive=True))
    covered = missed = 0
    for rep in reports:
        try:
            root = ET.parse(rep).getroot()
        except ET.ParseError:
            continue
        for sf in root.iter("sourcefile"):
            if sf.get("name") in java:
                for ctr in sf.findall("counter"):
                    if ctr.get("type") == "LINE":
                        covered += int(ctr.get("covered", 0))
                        missed += int(ctr.get("missed", 0))
    total = covered + missed
    return (covered / total) if total else 0.0


def _pit_has_report(work_dir):
    """PIT 是否产出了 mutations.xml 报告（只判"在不在"，不解析分数）。
    用于把 _pit_score 的 None 一分为二：报告在但零变异(=无可变异→通过) vs 报告不在(=失败)。"""
    import glob
    return bool(glob.glob(
        os.path.join(work_dir, "**/target/pit-reports/**/mutations.xml"), recursive=True))


def _pit_score(work_dir):
    import glob
    import xml.etree.ElementTree as ET
    reps = glob.glob(os.path.join(work_dir, "**/target/pit-reports/**/mutations.xml"),
                     recursive=True)
    killed = total = 0
    parseable = False
    corrupt = []
    for rep in reps:
        try:
            root = ET.parse(rep).getroot()
        except ET.ParseError as e:
            corrupt.append((rep, str(e)))      # 带解析异常细节（行/列/原因），否则只留路径无法定位截断点
            continue
        parseable = True
        for m in root.iter("mutation"):
            total += 1
            if m.get("status") in ("KILLED", "TIMED_OUT"):
                killed += 1
    # 报告存在但【全部】解析失败（PIT 崩溃中途写出截断 xml 等）：不能当"零变异"放过——
    # 上游 MavenRunner.mutation 会把 _pit_score 的 None 经 _pit_has_report=True 路径返回 1.0
    # （mutation_score 顶满 → MUT_MIN 判过 → 弱测试骗过 verify 门；恰是 #79 B1 没堵死的口子）。
    # 区分"无可变异(通过)"与"报告损坏(失败)"：损坏必抛 MutationRunError（→ 上游判 inadequate）。
    # 若至少有一份可解析报告【且含变异点】，则以它为准（忽略同胞损坏报告，分数仍可信）。
    #   - 仅 corrupt 全坏        → not parseable → 抛（A5-F1：全部损坏）。
    #   - corrupt + 可解析且有数据 → 用可解析那份的 killed/total（分数可信）。
    #   - corrupt + 可解析但【零变异】(total==0)：同胞损坏说明 PIT 有模块崩溃，此时 total=0 会经
    #     _pit_has_report=True 路径顶满成 1.0 —— 等于让崩溃模块的弱测试骗过门，故同样必抛。
    #     （无 corrupt 的零变异仍走 None→1.0，那是真正的"无可变异→通过"。）
    if corrupt and (not parseable or total == 0):
        # 报错带【每份损坏报告的解析异常细节】（路径 + 行/列/原因）—— #98 二轮评审建议：
        # 旧版只列路径，运维拿到"PIT 报告损坏"却不知截断在哪/为何坏，难定位。异常细节使该
        # 失败路径（正是 #98 要从静默里拽出来的）真正可诊断。
        detail = ", ".join(f"{rep} ({msg[:120]})" for rep, msg in corrupt)
        raise MutationRunError("mutations.xml 存在但解析失败或零变异且有同胞损坏报告"
                               "（疑 PIT 崩溃截断，不能当通过）："
                               + detail[:400])
    return (killed / total) if total else None


def _extract_java_signatures(work_dir, changed_files):
    sigs = []
    for fp in changed_files:
        if not fp.endswith(".java"):
            continue
        path = os.path.join(work_dir, fp)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        for k, n in re.findall(r"\b(class|interface|enum|record)\s+(\w+)", text):
            sigs.append(f"{fp}: {k} {n}")
        for m in re.findall(
                r"(?:public|protected)\s+[\w<>\[\],.?\s]+?\s+(\w+)\s*\([^;{]*\)\s*[{;]", text):
            sigs.append(f"{fp}: method {m}()")
    return "\n".join(sigs) or "(未能抽取签名)"


def _suite_coverage_python(work_dir, changed_files, changed_lines=None):
    py = [f for f in changed_files if f.endswith(".py")]
    if not py:
        return 1.0
    try:
        cov = _run_coverage_subprocess(work_dir, ["-m", "pytest", "-q"])
        return _coverage_ratio(cov, py, changed_lines)
    except Exception:
        return 0.0



def _coverage_json_line_ratio(cov_json, changed_lines):
    """coverage.py json + 改动行 → 改动行覆盖率（只计“可覆盖”的改动行）。纯函数。"""
    files = (cov_json or {}).get("files", {})
    coverable = covered = 0
    for path, lines in (changed_lines or {}).items():
        fd = files.get(path)
        if not fd:
            continue
        line_set = set(lines) if lines else set()   # 同 _coverage_ratio：防 list 输入致 set&list TypeError
        cov_set = (set(fd.get("executed_lines", [])) | set(fd.get("missing_lines", []))) & line_set
        coverable += len(cov_set)
        covered += len(set(fd.get("executed_lines", [])) & cov_set)
    return (covered / coverable) if coverable else 1.0


def _basename_lines(changed_lines):
    out = {}
    for path, lines in (changed_lines or {}).items():
        out.setdefault(os.path.basename(path), set()).update(lines)
    return out


def _jacoco_line_ratio(roots, basename_lines):
    """ET 根列表 + {basename: 改动行} → 改动行覆盖率(ci>0 视为已覆盖)。纯函数。"""
    coverable = covered = 0
    for root in roots:
        for sf in root.iter("sourcefile"):
            want = basename_lines.get(sf.get("name"))
            if not want:
                continue
            for ln in sf.findall("line"):
                if int(ln.get("nr", 0)) in want:
                    coverable += 1
                    if int(ln.get("ci", 0)) > 0:
                        covered += 1
    return (covered / coverable) if coverable else 1.0


def _jacoco_changed_line_coverage(work_dir, changed_files, changed_lines):
    import glob
    import xml.etree.ElementTree as ET
    reports = (glob.glob(os.path.join(work_dir, "**/target/site/jacoco/jacoco.xml"), recursive=True)
               + glob.glob(os.path.join(work_dir, "**/target/site/jacoco-aggregate/jacoco.xml"),
                           recursive=True))
    roots = []
    for rep in reports:
        try:
            roots.append(ET.parse(rep).getroot())
        except ET.ParseError:
            continue
    return _jacoco_line_ratio(roots, _basename_lines(changed_lines))


# --- 独立验收测试 JUnit 放置（Java）：解析 package/class 放入 src/test/java ----------
def _place_junit(work_dir, test_code):
    pkg = re.search(r"package\s+([\w.]+)\s*;", test_code)
    cls = re.search(r"(?:public\s+)?(?:final\s+)?class\s+(\w+)", test_code)
    cname = cls.group(1) if cls else "GeneratedSpecTest"
    parts = ["src", "test", "java"] + (pkg.group(1).split(".") if pkg else [])
    dest_dir = os.path.join(work_dir, *parts)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, cname + ".java")
    with open(path, "w", encoding="utf-8") as f:
        f.write(test_code)
    return cname, path


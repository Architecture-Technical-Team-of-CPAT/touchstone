# ============================================================================
# tests/test_import_hygiene.py —— 导入卫生守卫（结构性防线）
# ----------------------------------------------------------------------------
# 背景：包化改造后，任何对 sibling 模块的平铺导入（`import ghclient` /
# `from llm_budget import x`，无论顶层还是函数内）都是运行期地雷——
# ModuleNotFoundError。更糟的是它可能被两种方式掩盖：
#   ① 该分支缺测试覆盖；② 某个测试文件把 touchstone/ 子目录插进 sys.path。
# 第二轮加固实际抓到 5 处这样的地雷 + 1 处 path 污染。本测试把教训固化：
#   1. 包内与测试内不得出现 sibling 平铺导入（静态扫描，函数内也逃不掉）
#   2. 测试不得把 touchstone/、verify/ 子目录插进 sys.path（防掩盖）
# ============================================================================
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 包内全部模块名（sibling 平铺导入的黑名单）
_SIBLINGS = sorted(
    f[:-3] for f in os.listdir(os.path.join(ROOT, "touchstone"))
    if f.endswith(".py") and f != "__init__.py"
) + ["verify_change"]

_ALT = "|".join(_SIBLINGS)
# `import X` / `import X as y` / `import a, X as y`（任意缩进=顶层+函数内都抓）
_FLAT_IMPORT = re.compile(
    rf"^\s*import\s+(?:[\w.]+\s*,\s*)*(?:{_ALT})(?:\s+as\s+\w+)?\s*(?:,|$|#)", re.M)
# `from X import ...`
_FLAT_FROM = re.compile(rf"^\s*from\s+(?:{_ALT})\s+import\s", re.M)
# 测试把包子目录插进 sys.path（掩盖地雷的污染源）
_PATH_POISON = re.compile(r"""sys\.path\.insert\(.*(touchstone|verify)["']\s*\)""")


def _py_files(*dirs):
    for d in dirs:
        base = os.path.join(ROOT, d)
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _scan(pattern, path):
    text = open(path, encoding="utf-8").read()
    hits = []
    for m in pattern.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        line = text[m.start():text.find("\n", m.start())].strip()
        # 字符串字面量里的示例代码（如 test_verify 构造的被测文件内容）不算
        # ——粗判：行首在引号包围块内的场景由下面白名单排除
        hits.append((line_no, line))
    return hits


# 已知合法例外：test_verify.py 在【字符串字面量】里构造被测仓库的测试代码，
# 其中的 import 是测试数据不是本仓导入。按"行内容含转义/位于字符串"精确豁免成本高，
# 这里按 (文件, 行内容特征) 白名单管理——新增豁免必须在此登记并说明理由。
_ALLOW = {
    ("test_verify.py", "from m import f"),        # 字面量:被测仓库的最小测试文件内容
}


def _allowed(path, line):
    base = os.path.basename(path)
    return any(base == f and frag in line for f, frag in _ALLOW)


def test_no_flat_sibling_imports_in_package_and_tests():
    offenders = []
    for path in _py_files("touchstone", "verify", "tests"):
        for pat in (_FLAT_IMPORT, _FLAT_FROM):
            for line_no, line in _scan(pat, path):
                if not _allowed(path, line):
                    offenders.append(f"{os.path.relpath(path, ROOT)}:{line_no}: {line}")
    assert not offenders, (
        "发现 sibling 平铺导入（包化后必然 ModuleNotFoundError，可能被覆盖缺口掩盖）：\n"
        + "\n".join(offenders)
        + "\n修法：改为 `from touchstone import X` / `from verify import verify_change`")


def test_no_syspath_poisoning_in_tests():
    offenders = []
    for path in _py_files("tests"):
        if os.path.basename(path) == os.path.basename(__file__):
            continue
        for line_no, line in _scan(_PATH_POISON, path):
            offenders.append(f"{os.path.relpath(path, ROOT)}:{line_no}: {line}")
    assert not offenders, (
        "测试把包子目录插进了 sys.path——这会让平铺导入地雷在全量测试中被静默掩盖：\n"
        + "\n".join(offenders))


def test_render_reexport_compat():
    """orchestrator.render_* 再导出路径保持兼容（外部调用与既有测试不需改动）。
    v2：render_facts/render_findings 已随版面合并移除，再导出仅含仍存在的函数。"""
    from touchstone import orchestrator, render
    for name in ("render_report", "render_summary"):
        assert getattr(orchestrator, name) is getattr(render, name)

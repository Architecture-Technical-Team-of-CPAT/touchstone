# -*- coding: utf-8 -*-
# 守卫上下文（issue #139）测试：A 旋钮 / B 生成侧摘要 / C 判后核销面。
import os

from touchstone import guard_context as gc

_SRC = '''\
import os


def loader(path, mode):
    if not path:
        raise ValueError("path required")
    if mode not in ("r", "rb"):
        return None
    assert isinstance(path, str)
    if os.path.exists(path):
        try:
            data = open(path, mode).read()
            result = data.strip()          # ← 命中行（L12）：上有 4 层守卫
        except OSError:
            return None
        return result
    return None
'''


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "loader.py").write_text(_SRC, encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------- 底层提取

def test_guard_facts_extracts_all_guard_kinds(tmp_path):
    """命中行的路径守卫（if/try）、前置早退、前置断言全部被提取。"""
    facts = gc.guard_facts(_repo(tmp_path), "pkg/loader.py", 12)
    assert facts["fn"] == "loader"
    joined = " ".join(facts["path_guards"])
    assert "if os.path.exists(path)" in joined          # 条件路径守卫
    assert "try/except OSError" in joined               # try 守卫
    assert any("if not path" in e for e in facts["early_exits"])   # 早退（raise）
    assert any('mode not in' in e for e in facts["early_exits"])   # 早退（return）
    assert facts["asserts"] == 1                        # 前置断言


def test_guard_facts_fail_open_to_none(tmp_path):
    """非 py / 文件不存在 / 语法坏 → None（失败即空，绝不抛出）。"""
    repo = _repo(tmp_path)
    assert gc.guard_facts(repo, "pkg/loader.md", 3) is None
    assert gc.guard_facts(repo, "pkg/nope.py", 3) is None
    (tmp_path / "pkg" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    assert gc.guard_facts(repo, "pkg/bad.py", 1) is None


def test_facts_line_compact_and_empty_safe():
    assert gc.facts_line(None) == ""
    line = gc.facts_line({"fn": "f", "path_guards": ["if a < b"],
                          "early_exits": ["if not x"], "asserts": 2})
    assert "函数 f" in line and "if a < b" in line and "前置断言×2" in line


# ---------------------------------------------------------------- B：生成侧摘要

_DIFF = """\
diff --git a/pkg/loader.py b/pkg/loader.py
--- a/pkg/loader.py
+++ b/pkg/loader.py
@@ -11,3 +11,3 @@ def loader(path, mode):
         try:
-            data = open(path, mode).read()
+            data = open(path, mode).read()  # touched
             result = data.strip()
"""


def test_render_guard_digest_covers_hit_hunk(tmp_path):
    txt = gc.render_guard_digest(_DIFF, _repo(tmp_path))
    assert "守卫上下文" in txt and "pkg/loader.py" in txt
    assert "try/except OSError" in txt
    assert "不要报缺校验" in txt                         # 指令语义在场


def test_render_guard_digest_fail_open_to_empty(tmp_path):
    assert gc.render_guard_digest("@@@ 不是 diff @@@", _repo(tmp_path)) == ""
    assert gc.render_guard_digest("", _repo(tmp_path)) == ""


# ---------------------------------------------------------------- C：判后核销面

def test_sig_location_parses_both_shapes():
    assert gc._sig_location("PRA-REVIEW:touchstone/probe.py:160") == ("touchstone/probe.py", 160)
    assert gc._sig_location("RULE-X@pkg/loader.py:12") == ("pkg/loader.py", 12)
    assert gc._sig_location("SIZE-001::0") == (None, None)


def test_render_adjudication_only_open_items(tmp_path):
    repo = _repo(tmp_path)
    items = [{"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "open"},
             {"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "done"},
             {"sig": "SIZE-001::0", "status": "open"}]          # 无位置 → 跳过
    txt = gc.render_adjudication(items, repo)
    assert txt.count("pkg/loader.py:12") == 1                   # 只收 open、且不重复
    assert "不要再报同一问题" in txt
    assert gc.render_adjudication([], repo) == ""


def test_attach_guard_facts_open_only_and_idempotent(tmp_path):
    repo = _repo(tmp_path)
    cl = {"items": [{"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "open", "note": ""},
                    {"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "done", "note": ""}]}
    gc.attach_guard_facts(cl, repo)
    assert "try/except OSError" in cl["items"][0].get("guard", "")
    assert "guard" not in cl["items"][1]                        # done 不附着
    cl["items"][0]["guard"] = "已有事实不覆盖"
    gc.attach_guard_facts(cl, repo)
    assert cl["items"][0]["guard"] == "已有事实不覆盖"           # 幂等：不覆盖已有


# ---------------------------------------------------------------- A：扩窗旋钮

def test_patch_context_settings_defaults_and_env_override(monkeypatch):
    from touchstone import pr_agent_runner as r
    for k in ("TOUCHSTONE_DYNAMIC_CONTEXT_MAX", "TOUCHSTONE_PATCH_EXTRA_BEFORE",
              "TOUCHSTONE_PATCH_EXTRA_AFTER"):
        monkeypatch.delenv(k, raising=False)
    s = r._patch_context_settings()
    assert s == {"allow_dynamic_context": True,
                 "max_extra_lines_before_dynamic_context": 30,
                 "patch_extra_lines_before": 10, "patch_extra_lines_after": 3}
    monkeypatch.setenv("TOUCHSTONE_DYNAMIC_CONTEXT_MAX", "10")   # 消融回调到上游默认
    monkeypatch.setenv("TOUCHSTONE_PATCH_EXTRA_BEFORE", "5")
    monkeypatch.setenv("TOUCHSTONE_PATCH_EXTRA_AFTER", "not-a-number")   # 坏值回默认
    s = r._patch_context_settings()
    assert s["max_extra_lines_before_dynamic_context"] == 10
    assert s["patch_extra_lines_before"] == 5
    assert s["patch_extra_lines_after"] == 3


# ============================ PR #140 round-1 销项回归 ============================
_NESTED_SRC = '''\
def outer(x):
    def helper(y):
        assert y > 0
        if not y:
            raise ValueError("inner")
        return y * 2
    value = helper(x)
    result = value + 1        # ← 命中行（L8）：内层守卫不得泄漏
    return result
'''


def test_nested_function_guards_do_not_leak(tmp_path):
    """R1-01：闭包/内层 def 的 assert 与早退不得计入外层函数事实——
    虚假守卫会让核销面压制真报，比误报更危险。"""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "nested.py").write_text(_NESTED_SRC, encoding="utf-8")
    facts = gc.guard_facts(str(tmp_path), "pkg/nested.py", 8)
    assert facts["fn"] == "outer"
    assert facts["asserts"] == 0                                 # 内层 assert 不泄漏
    assert not any("not y" in e for e in facts["early_exits"])   # 内层早退不泄漏
    inner = gc.guard_facts(str(tmp_path), "pkg/nested.py", 6)    # 内层自身仍正常提取
    assert inner["fn"] == "helper" and inner["asserts"] == 1


def test_digest_parses_each_file_once(tmp_path, monkeypatch):
    """R1-02/03：hunk 多行探测每文件只 parse 一次（消除逐行重复读盘+重解析）。"""
    repo = _repo(tmp_path)
    calls = {"n": 0}
    real = gc._parse
    def counting(repo_dir, path):
        calls["n"] += 1
        return real(repo_dir, path)
    monkeypatch.setattr(gc, "_parse", counting)
    src_lines = _SRC.splitlines()                                 # 真实宽 hunk：全文件上下文 +
    body = [" " + l for l in src_lines]                           # 中段一行改动（跨度 = 文件全长）
    body[11] = "-" + src_lines[11]
    body.insert(12, "+" + src_lines[11] + "  # touched")
    wide = ("diff --git a/pkg/loader.py b/pkg/loader.py\n--- a/pkg/loader.py\n+++ b/pkg/loader.py\n"
            f"@@ -1,{len(src_lines)} +1,{len(src_lines)} @@\n" + "\n".join(body) + "\n")
    txt = gc.render_guard_digest(wide, repo)
    assert "pkg/loader.py" in txt
    assert calls["n"] == 1                                        # 全跨度多点探测仍只 parse 一次


# ============================ PR #140 round-2 销项回归 ============================
def test_kill_switch_gates_attach(tmp_path, monkeypatch):
    """R2-03：TOUCHSTONE_GUARD_CONTEXT_ENABLED=false 时 attach 面同样受控——
    杀开关不可被任一入口绕过。"""
    repo = _repo(tmp_path)
    cl = {"items": [{"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "open", "note": ""}]}
    monkeypatch.setenv("TOUCHSTONE_GUARD_CONTEXT_ENABLED", "false")
    assert gc.enabled() is False
    gc.attach_guard_facts(cl, repo)
    assert "guard" not in cl["items"][0]                          # 关闸 → 不附着
    monkeypatch.setenv("TOUCHSTONE_GUARD_CONTEXT_ENABLED", "true")
    gc.attach_guard_facts(cl, repo)
    assert "try/except OSError" in cl["items"][0]["guard"]        # 开闸 → 正常附着


def test_nested_set_cached_per_scope(tmp_path, monkeypatch):
    """R2-02：多点探测下嵌套排除集按 scope 缓存——同一函数跨点只计算一次。"""
    repo = _repo(tmp_path)
    calls = {"n": 0}
    real = gc._nested_set
    def counting(scope):
        calls["n"] += 1
        return real(scope)
    monkeypatch.setattr(gc, "_nested_set", counting)
    src_lines = _SRC.splitlines()
    body = [" " + l for l in src_lines]
    body[11] = "-" + src_lines[11]
    body.insert(12, "+" + src_lines[11] + "  # touched")
    wide = ("diff --git a/pkg/loader.py b/pkg/loader.py\n--- a/pkg/loader.py\n+++ b/pkg/loader.py\n"
            f"@@ -1,{len(src_lines)} +1,{len(src_lines)} @@\n" + "\n".join(body) + "\n")
    txt = gc.render_guard_digest(wide, repo)
    assert "pkg/loader.py" in txt
    # 全跨度探测覆盖 2 个 scope（module 级空行 + loader 函数）——每 scope 至多 1 次
    assert calls["n"] <= 2


# ============================ PR #140 round-3 销项回归 ============================
def test_parse_rejects_path_traversal(tmp_path):
    """R3-04：路径越界拒读——`../` 穿越与绝对路径均返回 None（守卫摘要进 LLM prompt，
    越界读取等于外送仓外文件内容）。仓内正常路径不受影响。"""
    repo = _repo(tmp_path)
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("def f():\n    return 1\n", encoding="utf-8")
    assert gc.guard_facts(repo, "../outside_secret.py", 1) is None      # 相对穿越拒读
    assert gc.guard_facts(repo, str(outside), 1) is None                # 绝对路径拒读
    assert gc.guard_facts(repo, "pkg/loader.py", 12) is not None        # 仓内不受影响


def test_continue_not_counted_as_early_exit(tmp_path):
    """R3-02：continue 不出函数——不得计为早退守卫（假守卫比漏守卫危险）。"""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "loopy.py").write_text(
        "def scan(items):\n"
        "    total = 0\n"
        "    for it in items:\n"
        "        if it is None:\n"
        "            continue\n"
        "        total += it\n"
        "    result = total * 2      # 命中行：循环外，continue 保护不到\n"
        "    return result\n", encoding="utf-8")
    facts = gc.guard_facts(str(tmp_path), "pkg/loopy.py", 7)
    assert not any("is None" in e for e in facts["early_exits"])        # continue 不计早退


# ============================ PR #140 round-4 销项回归 ============================
def test_kill_switch_gates_all_entry_points(tmp_path, monkeypatch):
    """R4-01/02：三个公共入口全部自闸——关闸后 digest/adjudication/attach 全部无输出，
    不依赖调用方记得挂（R2 attach 教训的完全体）。"""
    repo = _repo(tmp_path)
    monkeypatch.setenv("TOUCHSTONE_GUARD_CONTEXT_ENABLED", "false")
    assert gc.render_guard_digest(_DIFF, repo) == ""
    items = [{"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "open"}]
    assert gc.render_adjudication(items, repo) == ""
    monkeypatch.setenv("TOUCHSTONE_GUARD_CONTEXT_ENABLED", "true")
    assert "pkg/loader.py" in gc.render_guard_digest(_DIFF, repo)       # 开闸恢复
    assert "pkg/loader.py:12" in gc.render_adjudication(items, repo)


def test_adjudication_parses_each_file_once(tmp_path, monkeypatch):
    """R4-01：多个 open 项指向同一文件 → 只 parse 一次（与 digest 同款缓存口径）；
    已附着 guard 的项走快路径不触发任何解析。"""
    repo = _repo(tmp_path)
    calls = {"n": 0}
    real = gc._parse
    def counting(repo_dir, path):
        calls["n"] += 1
        return real(repo_dir, path)
    monkeypatch.setattr(gc, "_parse", counting)
    items = [{"sig": "PRA-REVIEW:pkg/loader.py:12", "status": "open"},
             {"sig": "PRA-GENERAL:pkg/loader.py:9", "status": "open"},
             {"sig": "PRA-X:pkg/loader.py:15", "status": "open", "guard": "已附着事实"}]
    txt = gc.render_adjudication(items, repo)
    assert txt.count("pkg/loader.py") == 3
    assert calls["n"] == 1                               # 同文件两项共享一次 parse；附着项零解析


# ============================ PR #140 round-5 销项回归 ============================
def test_attach_parses_each_file_once(tmp_path, monkeypatch):
    """R5-01：attach 多 open 项同文件 → 只 parse 一次（与 adjudication 同款口径）。"""
    repo = _repo(tmp_path)
    calls = {"n": 0}
    real = gc._parse
    def counting(repo_dir, path):
        calls["n"] += 1
        return real(repo_dir, path)
    monkeypatch.setattr(gc, "_parse", counting)
    cl = {"items": [{"sig": "PRA-A:pkg/loader.py:12", "status": "open", "note": ""},
                    {"sig": "PRA-B:pkg/loader.py:9", "status": "open", "note": ""}]}
    gc.attach_guard_facts(cl, repo)
    assert all("guard" in it for it in cl["items"])
    assert calls["n"] == 1


def test_digest_skips_guardless_entries_but_adjudication_keeps(tmp_path):
    """R5-02：零守卫条目 digest 侧跳过（不压误报纯耗预算）；adjudication 侧保留
    （「确实无守卫」对复核有信息量——提示该 finding 可能是真报）。"""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "bare.py").write_text(
        "def plain(a, b):\n    x = a + b\n    y = x * 2\n    return y\n", encoding="utf-8")
    bare_diff = ("diff --git a/pkg/bare.py b/pkg/bare.py\n--- a/pkg/bare.py\n+++ b/pkg/bare.py\n"
                 "@@ -2,2 +2,2 @@ def plain(a, b):\n-    x = a + b\n+    x = a + b  # touched\n"
                 "     y = x * 2\n")
    assert gc.render_guard_digest(bare_diff, str(tmp_path)) == ""       # 裸路径不进 digest
    txt = gc.render_adjudication(
        [{"sig": "PRA-Z:pkg/bare.py:3", "status": "open"}], str(tmp_path))
    assert "无守卫" in txt                                               # 核销面保留裸路径事实


# ============================ PR #140 round-6 销项回归 ============================
def test_conditional_nested_early_exits_not_counted(tmp_path):
    """R6-01：嵌套在条件块内的早退/断言只在外层条件下生效——不得计成无条件守卫
    （假守卫压制真报）；scope 直接子语句的守卫不受影响。"""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "cond.py").write_text(
        "def f(x, mode):\n"
        "    if not mode:\n"
        "        return None\n"                      # 直接子语句早退：应计
        "    if mode == 'strict':\n"
        "        if x is None:\n"
        "            raise ValueError('x')\n"        # 嵌套条件内早退：不计
        "        assert x > 0\n"                     # 嵌套条件内断言：不计
        "    y = (x or 0) + 1\n"                     # ← 命中行（L9）
        "    return y\n", encoding="utf-8")
    facts = gc.guard_facts(str(tmp_path), "pkg/cond.py", 9)
    assert any("not mode" in e for e in facts["early_exits"])            # 直接子语句仍计
    assert not any("is None" in e for e in facts["early_exits"])         # 嵌套早退不计
    assert facts["asserts"] == 0                                         # 嵌套断言不计


# ============================ PR #140 round-7 销项回归 ============================
def test_patch_context_settings_clamps_negative_to_zero(monkeypatch):
    """R7-02（pr_agent_runner._envi）：负的上下文行数 env 必须夹到 0，防 pr-agent
    扩窗/补丁行计数下溢。回归锁：去 max(0,·) 即透传 -5/-1/-100。"""
    from touchstone import pr_agent_runner as r
    monkeypatch.setenv("TOUCHSTONE_DYNAMIC_CONTEXT_MAX", "-5")
    monkeypatch.setenv("TOUCHSTONE_PATCH_EXTRA_BEFORE", "-1")
    monkeypatch.setenv("TOUCHSTONE_PATCH_EXTRA_AFTER", "-100")
    s = r._patch_context_settings()
    assert s["max_extra_lines_before_dynamic_context"] == 0
    assert s["patch_extra_lines_before"] == 0
    assert s["patch_extra_lines_after"] == 0


_CROSS_SRC = (
    "def first(x):\n"
    "    if x < 0:\n"
    "        return 0\n"
    "    return x + 1\n"                 # ← 命中行（first 末行）
    "def second(y):\n"                    # ln+1 探测落点：second 的 def 行
    "    assert y < 100\n"                # second 的守卫——不得借 ln+1 泄漏到 first
    "    if y > 50:\n"
    "        raise ValueError\n"
    "    return y\n"
)
_CROSS_DIFF = (
    "diff --git a/pkg/cross.py b/pkg/cross.py\n"
    "--- a/pkg/cross.py\n+++ b/pkg/cross.py\n"
    "@@ -1,7 +1,7 @@\n"
    " def first(x):\n"
    "     if x < 0:\n"
    "         return 0\n"
    "-    return x + 1\n"
    "+    return x + 2\n"
    " def second(y):\n"
    "     assert y < 100\n"
    "     if y > 50:\n"
)


def test_digest_no_cross_function_leak_at_boundary(tmp_path):
    """R7-01（guard_context 跨函数边界）：变更落在 first 末行、second 紧随其后的边界，
    ln+1 探测虽落到 second 的 def 行，但同函数不变量保证 second 的守卫不计入 first 的变更。
    first 的前置早退被正确记；second / 其守卫字面不泄漏。"""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "cross.py").write_text(_CROSS_SRC, encoding="utf-8")
    txt = gc.render_guard_digest(_CROSS_DIFF, str(tmp_path))
    assert "first" in txt and "if x < 0" in txt              # first 的早退正确记
    assert "second" not in txt and "100" not in txt          # second 不跨边界泄漏


# ============================ PR #140 round-8 销项回归 ============================
def test_try_finally_without_except_not_a_guard(tmp_path):
    """R8：纯 try/finally（无 except handler）只是清理、不挡异常——不得计成
    'try/except …' 假守卫（与 R3「宁漏勿假」一致）。try/except 对照仍正常计。
    回归锁：去 ``n.handlers`` 判定 → try/finally 被误记为 'try/except …'。"""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "tf.py").write_text(
        "def f(x):\n"
        "    try:\n"
        "        data = load(x)\n"            # ← 命中行：被 try 罩住，但只有 finally
        "        result = data.strip()\n"
        "    finally:\n"
        "        close(x)\n"
        "    return result\n", encoding="utf-8")
    facts = gc.guard_facts(str(tmp_path), "pkg/tf.py", 3)
    assert not any("try/except" in g for g in facts["path_guards"])    # try/finally 不计
    # 对照：try/except OSError 仍正常计
    (tmp_path / "pkg" / "te.py").write_text(
        "def g(x):\n"
        "    try:\n"
        "        data = load(x)\n"
        "        result = data.strip()\n"
        "    except OSError:\n"
        "        return None\n"
        "    return result\n", encoding="utf-8")
    facts2 = gc.guard_facts(str(tmp_path), "pkg/te.py", 3)
    assert any("try/except" in g and "OSError" in g for g in facts2["path_guards"])

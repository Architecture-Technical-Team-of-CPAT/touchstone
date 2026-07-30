# -*- coding: utf-8 -*-
# touchstone/guard_context.py —— 守卫上下文提取（评审误报改进，issue #139）
#
# 问题：评审引擎看 diff hunk 不看上下文守卫——命中行上方的 if 条件 / try 头 / 早退 /
# 断言被 diff 窗口截断切掉，LLM 看到「裸操作」而误报缺校验/未处理异常（外部反馈：
# 8 条中 2 误报 + 1 半误报均属此型）。
#
# 本模块提供确定性 AST 守卫事实，服务两个消费面（A 扩窗在 pr_agent_runner 侧）：
#   B（生成侧）render_guard_digest(diff, repo_dir)   —— 变更 hunk 的守卫摘要
#       → 注入 extra_instructions，评审生成时可见守卫，降低误报产出。
#   C（判后核销面）render_adjudication(open_items, repo_dir) —— 未销项 finding 的守卫事实
#       → 注入下一轮 extra_instructions：守卫已覆盖所指风险的 FP 不再被重报 →
#         签名不再现 → reconcile 按既有机制自动销项。同时事实附着 checklist 项
#         （item["guard"]），随 marker 持久化、随清单渲染呈现，供人以 waived 申报佐证。
#
# 边界与原则（与经验注入同款）：
#   · 只调建议、不进闸——守卫事实只影响评审建议质量，不参与 gate/loop 决策；
#   · 失败即空——任何解析失败返回空文本/None，绝不让评审链路因本模块降级；
#   · 纯 stdlib ast，零新依赖；只处理 .py 文件（其它语言留待后续）。

from __future__ import annotations

import ast
import os

_COND_MAX = 88          # 单条守卫条件源码截断长度


def enabled():
    """总开关（TOUCHSTONE_GUARD_CONTEXT_ENABLED，默认开）。所有入口共用本判定
    （PR#140 R2 意见 3：attach 面此前未挂闸，operator 关开关后清单仍渲染守卫行，
    杀开关被绕过）。"""
    return os.environ.get("TOUCHSTONE_GUARD_CONTEXT_ENABLED", "true").lower() in (
        "1", "true", "yes", "on")
_DIGEST_MAX = 4000      # B 摘要总预算（字符）
_ADJ_MAX = 2400         # C 核销段总预算（字符）


# ---------------------------------------------------------------- 底层提取

def _parse(repo_dir, path):
    """读文件 + ast 解析；失败返回 (None, None)（失败即空）。

    路径边界（PR#140 R3 意见 4）：path 来自 diff 解析/清单签名等外部输入，realpath
    归一后必须落在 repo_dir 内——`../` 穿越或绝对路径一律拒读（守卫摘要会进 LLM
    prompt，越界读取等于把仓外文件内容外送）。with 确保句柄确定性关闭（意见 1）。"""
    try:
        base = os.path.realpath(repo_dir)
        full = os.path.realpath(os.path.join(base, path))
        if full != base and not full.startswith(base + os.sep):
            return None, None                            # 越界拒读（失败即空，不抛）
        with open(full, encoding="utf-8") as f:
            src = f.read()
        return ast.parse(src), src
    except (OSError, SyntaxError, ValueError):
        return None, None


def _covers(node, line):
    end = getattr(node, "end_lineno", None)
    return node.lineno <= line <= (end if end is not None else node.lineno)


def _seg(src, node, limit=_COND_MAX):
    try:
        s = ast.get_source_segment(src, node) or ""
    except Exception:
        s = ""
    s = " ".join(s.split())
    return s[: limit - 1] + "…" if len(s) > limit else s


def _enclosing_function(tree, line):
    """覆盖 line 的最内层函数节点（无则 None）。"""
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _covers(n, line):
            if fn is None or n.lineno > fn.lineno:      # 更内层 = 起始行更靠后
                fn = n
    return fn


def guard_facts(repo_dir, path, line):
    """命中行的确定性守卫事实。返回 dict 或 None（非 py / 解析失败 / 行越界）。

    facts:
      fn            enclosing 函数名（模块级则 "<module>"）
      path_guards   包含命中行的条件路径守卫：["if a < b", "try/except OSError", …]（外→内）
      early_exits   同函数内、命中行之前的早退守卫：["if not x: raise ValueError(…)", …]
      asserts       同函数内、命中行之前的断言条数
    """
    if not path.endswith(".py"):
        return None
    tree, src = _parse(repo_dir, path)
    if tree is None:
        return None
    return _facts_from_tree(tree, src, line)


def _nested_set(scope):
    """scope 内嵌套函数（闭包/内层 def/lambda）的全部子节点 id 集——遍历时排除，
    防内层 assert/早退泄漏成外层【虚假守卫】（PR#140 R1）。只依赖 scope 不依赖 line。"""
    nested = set()
    for d in ast.walk(scope):
        if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and d is not scope:
            nested.update(id(x) for x in ast.walk(d) if x is not d)
    return nested


def _facts_from_tree(tree, src, line, scope_cache=None):
    """tree 级事实提取（PR#140 R1：调用方每文件 parse 一次，多行探测复用本函数，
    消除逐行重复读盘+重解析）。scope_cache（PR#140 R2 意见 2）：嵌套排除集只依赖
    scope，多点探测热循环传 dict 按 id(scope) 复用，免 O(span×scope_size) 重复遍历。"""
    fn = _enclosing_function(tree, line)
    scope = fn if fn is not None else tree
    if scope_cache is not None:
        nested = scope_cache.get(id(scope))
        if nested is None:
            nested = scope_cache[id(scope)] = _nested_set(scope)
    else:
        nested = _nested_set(scope)
    facts = {"fn": fn.name if fn is not None else "<module>",
             "path_guards": [], "early_exits": [], "asserts": 0}
    # ②③ 只认 scope 的直接子语句（PR#140 R6）：ast.walk 拍平后，嵌套在条件块里的
    # 早退/断言（如 if mode=="strict": 里的 if x is None: raise）只在外层条件下生效，
    # 计成无条件守卫即假守卫。宁漏勿假（R3 确立的失效方向）。①路径守卫不受影响——
    # 它按位置包含判定，本就只收真正罩住命中行的条件。
    direct = {id(x) for x in getattr(scope, "body", [])}
    for n in ast.walk(scope):
        if id(n) in nested:
            continue
        # ① 条件路径守卫：命中行落在其 body/orelse 内的 if/while，以及覆盖命中行的 try
        if isinstance(n, (ast.If, ast.While)) and _covers(n, line) and n.lineno < line:
            facts["path_guards"].append(("while " if isinstance(n, ast.While) else "if ")
                                        + _seg(src, n.test))
        # try/except 是异常路径守卫；纯 try/finally（无 handler）只是清理、不挡异常，
        # 不得计成 "try/except …" 假守卫（#140 R8：与 R3「宁漏勿假」一致）。
        elif isinstance(n, ast.Try) and n.handlers and _covers(n, line) and n.lineno < line:
            kinds = []
            for h in n.handlers:
                kinds.append(_seg(src, h.type, 40) if h.type is not None else "Exception")
            facts["path_guards"].append("try/except " + ("|".join(kinds) or "…"))
        # ② 前置早退：同 scope 内、命中行之前、body 含 raise/return/continue 的 if
        elif (isinstance(n, ast.If) and id(n) in direct
              and n.lineno < line and not _covers(n, line)):
            end = getattr(n, "end_lineno", n.lineno)
            # ast.Continue 只跳过本次循环迭代、不退出函数（PR#140 R3 意见 2）：命中行在
            # 循环外时把 continue 计成早退即【虚假守卫】——宁可漏掉少数循环内守卫（更安全
            # 的失效方向），不制造压制真报的假事实。
            if end < line and any(isinstance(x, (ast.Raise, ast.Return))
                                  for x in n.body):
                facts["early_exits"].append("if " + _seg(src, n.test))
        # ③ 前置断言
        elif isinstance(n, ast.Assert) and id(n) in direct and n.lineno < line:
            facts["asserts"] += 1
    facts["path_guards"].sort(key=lambda s: s)          # 稳定输出（walk 顺序不保证）
    facts["early_exits"] = facts["early_exits"][:6]
    facts["path_guards"] = facts["path_guards"][:6]
    if not (facts["path_guards"] or facts["early_exits"] or facts["asserts"]):
        return {"fn": facts["fn"], "path_guards": [], "early_exits": [], "asserts": 0}
    return facts


def facts_line(facts):
    """事实 → 单行紧凑文本（清单附着与注入共用）。空事实 → ""。"""
    if not facts:
        return ""
    parts = []
    if facts["path_guards"]:
        parts.append("路径守卫[" + " ∧ ".join(facts["path_guards"]) + "]")
    if facts["early_exits"]:
        parts.append("前置早退[" + " ; ".join(facts["early_exits"]) + "]")
    if facts["asserts"]:
        parts.append(f"前置断言×{facts['asserts']}")
    if not parts:
        return f"函数 {facts['fn']}：无守卫（裸路径）"
    return f"函数 {facts['fn']}：" + "；".join(parts)


# ---------------------------------------------------------------- B：生成侧守卫摘要

def render_guard_digest(diff_text, repo_dir, max_chars=_DIGEST_MAX):
    """变更 hunk → 守卫摘要文本（注入 extra_instructions）。失败/无内容返回 ""。

    diff 解析用 unidiff（与 contract_check 同一依赖）取精确变更行号——ScopeFacts 的
    hunk 三元组只有计数没有位置，不足以定位变更行。解析失败 → 摘要为空（失败即空）。
    """
    if not enabled():
        return ""                                        # 入口自闸（PR#140 R4：所有入口共用判定，
    try:                                                 # 纵深防御不依赖调用方记得挂）
        from unidiff import PatchSet                     # 与 contract_check 同一解析依赖
        # 精确变更行定位（PR#140 R5 修复暴露的探测缺陷）：ScopeFacts 的
        # [start, added, deleted] 只有计数没有位置——前导 context 多的 hunk 里，
        # 跨度扫描根本探不到变更行。改用 unidiff 取 target 文件中新增行的精确行号，
        # 每个变更行探测 {ln, ln+1}（+1 防恰落在 try:/if 守卫自身行被 lineno<line 排除），
        # 按 (path, 函数) 聚合取最富事实。
        lines, seen = [], set()
        for pf in PatchSet(diff_text or ""):
            path = pf.path
            if not path.endswith(".py"):
                continue
            hit_lines = sorted({l.target_line_no for h in pf for l in h
                                if l.is_added and l.target_line_no})
            if not hit_lines:                            # 纯删除 hunk：取删除点的 target 位置
                hit_lines = sorted({h.target_start for h in pf if h.target_start})
            if not hit_lines:
                continue
            tree, src = _parse(repo_dir, path)           # 每文件 parse 一次（PR#140 R1）
            if tree is None:
                continue
            _scopes = {}                                 # per-file scope 缓存（PR#140 R2）
            best_by_fn = {}                              # fn → (score, line, facts)
            for ln in hit_lines:
                # ln 为锚；ln+1 仅当落在同一函数内才采纳（#140 R7：防 ln 是某函数末行、
                # ln+1 落到下一函数，把邻接函数的守卫事实错记到本变更行）。ln 解析失败时
                # ln+1 仍可作兜底（无锚可比对）。
                f_base = _facts_from_tree(tree, src, ln, scope_cache=_scopes)
                candidates = []
                if f_base is not None:
                    candidates.append(f_base)
                f_next = _facts_from_tree(tree, src, ln + 1, scope_cache=_scopes)
                if f_next is not None and (f_base is None or f_next["fn"] == f_base["fn"]):
                    candidates.append(f_next)
                for f in candidates:
                    # 零守卫条目跳过（PR#140 R5）：digest 的用途是压「看 hunk 不看守卫」
                    # 型误报，「无守卫（裸路径）」不压任何误报、纯耗预算。注意 adjudication
                    # 面【保留】裸路径输出——那里「确实无守卫」对复核有信息量（提示该
                    # finding 可能是真报）。
                    score = len(f["path_guards"]) + len(f["early_exits"]) + (1 if f["asserts"] else 0)
                    if score == 0:
                        continue
                    cur = best_by_fn.get(f["fn"])
                    if cur is None or score > cur[0]:
                        best_by_fn[f["fn"]] = (score, ln, f)
            for fn, (_sc, ln, f) in sorted(best_by_fn.items(), key=lambda kv: kv[1][1]):
                key = (path, fn)
                if key in seen:
                    continue
                seen.add(key)
                fl = facts_line(f)
                if fl:
                    lines.append(f"- {path}:{ln} {fl}")
        if not lines:
            return ""
        head = ("【守卫上下文（确定性 AST 事实，工具生成）】以下为本次变更命中位置的"
                "上下文守卫。评审时请对照：若某风险已被下列守卫覆盖（条件路径/早退/断言），"
                "不要报缺校验、未处理异常类问题。\n")
        out = head + "\n".join(lines)
        return out[:max_chars]
    except Exception:
        return ""                                                # 失败即空


# ---------------------------------------------------------------- C：判后核销面

def _sig_location(sig):
    """checklist 签名 → (path, line)。解析不出返回 (None, None)。
    兼容 'PRA-REVIEW:touchstone/probe.py:160' 与 '…@path:line' 两种形态。"""
    s = (sig or "").split("@", 1)[-1]
    parts = s.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit():
        path = parts[0].split(":", 1)[-1] if ":" in parts[0] else parts[0]
        if path and ("/" in path or "." in path):       # 空/非路径形态（如 SIZE-001::0）拒收
            return path, int(parts[1])
    return None, None


def attach_guard_facts(checklist, repo_dir):
    """给 open 项附着守卫事实（item["guard"]，随 marker 持久化）。就地修改并返回。
    只附着、不判断——事实是给人（waived 佐证）与下一轮评审（核销注入）看的。"""
    if not enabled():
        return checklist                                 # 杀开关：attach 面同样受控（R2 意见 3）
    try:
        _trees, _scopes = {}, {}                         # 每文件单次 parse（PR#140 R5，
        for it in (checklist or {}).get("items", []):    #  与 adjudication 同款缓存口径）
            if it.get("status") != "open" or it.get("guard"):
                continue
            path, line = _sig_location(it.get("sig", ""))
            if path is None:
                continue
            if path not in _trees:
                _trees[path] = _parse(repo_dir, path)
            tree, src = _trees[path]
            if tree is None:
                continue
            fl = facts_line(_facts_from_tree(
                tree, src, line, scope_cache=_scopes.setdefault(path, {})))
            if fl:
                it["guard"] = fl
    except Exception:
        pass    # 静默豁免：守卫附着是纯增强，失败即不附着；抛出会拖垮清单主链（只调建议不进闸）
    return checklist


def render_adjudication(open_items, repo_dir, max_chars=_ADJ_MAX):
    """未销项 → 守卫事实核销段（注入下一轮 extra_instructions）。失败/无内容返回 ""。"""
    if not enabled():
        return ""                                        # 入口自闸（PR#140 R4，同 digest）
    try:
        lines, _trees, _scopes = [], {}, {}              # 每文件单次 parse + per-scope 缓存
        for it in open_items or []:                      # （PR#140 R4：多 item 同文件不重复读盘）
            if it.get("status") != "open":
                continue
            path, line = _sig_location(it.get("sig", ""))
            if path is None:
                continue
            fl = it.get("guard")                         # 已附着事实直用，免任何解析
            if not fl:
                if path not in _trees:
                    _trees[path] = _parse(repo_dir, path)
                tree, src = _trees[path]
                if tree is not None:
                    fl = facts_line(_facts_from_tree(
                        tree, src, line, scope_cache=_scopes.setdefault(path, {})))
            if fl:
                lines.append(f"- {it['sig']} → {fl}")
        if not lines:
            return ""
        head = ("【未销项守卫核对（确定性 AST 事实，工具生成）】以下为上轮未销项 finding "
                "命中位置的守卫事实。若该 finding 所指风险已被守卫覆盖，本轮不要再报同一问题；"
                "若守卫不足以覆盖，请在意见中说明为何不足。\n")
        out = head + "\n".join(lines)
        return out[:max_chars]
    except Exception:
        return ""

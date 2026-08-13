#!/usr/bin/env python3
# ============================================================================
# touchstone/render.py —— 评审报告渲染层（七段版面填充）
# ----------------------------------------------------------------------------
# 从 orchestrator 拆出（模块职责单一化）：orchestrator 编排链路，本模块只负责把
# 结构化结果填进版面。版面由 templates/review_report.md 唯一定义——模板是设计资产，
# 代码只填充、不定义版面（修订设计 §3 意见 4）。
# 拆分同时根治一处运行期地雷：原 render_findings 函数内的 `from llm_budget import`
# 平铺导入在移除 sys.path hack 后必然 ModuleNotFoundError，且因该分支缺测试覆盖 +
# 个别测试文件污染 sys.path 而在全量测试中被掩盖（单跑文件才炸）。现改为顶层包导入。
# ============================================================================

import os
import re
import sys

from touchstone.llm_budget import MAX_FINDINGS_IN_SUMMARY
from touchstone.checklist import sig_of          # 清单签名构造（finding → sig，做 findings↔清单项 join）

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "templates", "review_report.md")


def _load_template():
    """读七段版面模板（修订设计 §3 意见 4）。模板是设计资产：代码只填充，不定义版面。
    读取失败退回极简版面（防模板缺失把评审主链打断），并在 stderr 留痕。"""
    try:
        with open(_TEMPLATE_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"[warn] 版面模板读取失败（{e}），使用内置极简版面", file=sys.stderr)
        return "{{status}}\n\n{{alerts}}\n\n{{facts}}\n\n{{findings}}\n\n{{reference}}\n\n{{markers}}"


_TEMPLATE_SLOT_RE = re.compile(r"\{\{(\w+)\}\}")


def _fill_template(template, parts):
    """单遍填充版面 `{{占位符}}`（A2-F1 防注入）。

    此前用顺序 `for k,v: out = out.replace("{{k}}", v)` 累积替换——每次 replace 重扫整段 out，
    于是【已填入】的段落文本若含占位符（finding 的 rationale/banner 等内容里出现 `{{markers}}`、
    `{{checklist}}` 等——LLM 输出、对抗构造、或 legitimately 讨论模板），后续步骤会把它当模板
    占位符展开：把 markers/checklist 段内容注入 finding 文本（占位符注入 / 串段）。

    re.sub 单遍替换只扫模板一次、替换文本不再被重扫，故占位符出现在内容值里时保持字面。
    未知占位符保持原样（同旧 str.replace 对未匹配键的行为）；值统一 str() 以防非字符串。"""
    return _TEMPLATE_SLOT_RE.sub(lambda m: str(parts.get(m.group(1), m.group(0))), template)


def render_unreliable_callout(engine_status, ai_raw_count=0, added_lines=0, engine_detail=""):
    """本轮评审不可信时的置顶告警——[!CAUTION] 红框置顶，替代常规溯源/降级横幅。精简到两行：
    点明【失败环节】+ 后果 + 指向。具体可靠的原始错误详列在「验证与日志」段（本框不塞原始
    dump）。判定层（销项/收敛/放行）已由 review_reliable 挡住；本函数把同一信号接到呈现层。"""
    _WHERE = {
        "no_engine": "评审引擎未启动",
        "provider_failed": "取 PR 失败",
        "llm_failed": "LLM 调用失败",
        "skipped_large_diff": "diff 超预算被跳过",
    }
    where = _WHERE.get(engine_status) or f"疑似空收敛（约 {added_lines} 行改动却 {ai_raw_count} 建议）"
    tail = "；原始错误见下方「验证与日志」" if (engine_status != "ok" and engine_detail) else ""
    return "\n".join([
        "> [!CAUTION]",
        f"> **本轮 AI 评审不可信**：{where}。",
        f"> 请人工评审{tail}。",
    ])


# v2：_location（v1 位置串渲染）已移除——sig 兼作位置显示（checklist.sig_of 在构造时
# 防 `file:None`），不再单列位置行。位置精度兜底移至 sig_of 构造源（一处修两处一致）。


_REASONING_COLLAPSE_THRESHOLD = 200
_TEASER_MAX = 60                          # <details> summary 露首句/前 N 字（不展开也露关键点）
# 句末标点：CJK 全角（。！？）无条件算句末；ASCII 半角（.!?）仅在后接空白/串尾时算句末
# —— 否则版本号（0.2.3）、小数、缩写（e.g.）、域名里的 . 会被误判为句末，teaser 在
# 「版本号从 0.」处截断（实测）。CJK 文本无词间空格，全角句号后直接接下一句，故全角
# 不设边界条件。
#
# 已知局限（#168 round-4）：ASCII 句号直连 CJK 字符（无空格，如「fixed.版本号」）不
# 被识别为句末——lookahead 要求 . 后接空白/串尾。此类混合写法罕见（混排时通常用 。 或
# 加空格），此时 teaser overrun 到 max_len 硬截断，可接受；要识别需在 lookahead 加
# 「后接非 ASCII」分支，但会让 0.2.3 等边角更难推理，收益不抵复杂度。
_SENTENCE_END = re.compile(r"[。！？]|[.!?](?=\s|$)")


def _reasoning_teaser(reasoning, max_len=_TEASER_MAX):
    """取 reasoning 首句（或前 max_len 字）作 <details> summary 的关键信息预览。

    summary 不再是无信息标签「依据（N 字）」，而是露核心论断（如「版本号 0.2.3→0.2.5
    跳过了 0.2.4」）——author 扫清单时不展开也能判断这条依据是否值得细读（用户 #168 续：
    「把关键信息的 summary 展示出来」）。纯函数：截断处加 …；换行折叠为空格（summary 是
    单行内联文本，换行会破坏 CommonMark type-6 HTML block 的单块性）。"""
    s = reasoning.strip().replace("\n", " ").replace("\r", "")
    m = _SENTENCE_END.search(s)
    # 首句在预算内（m.end() 含末标点，≤ max_len）→ 取整句；首句超长 → 硬截断到 max_len。
    # 不留余量：严格保证 teaser ≤ max_len + 1（+1 是末尾 …），与 run-on 分支（s[:max_len]）
    # 一致——此前 max_len + 5 余量会让首句 teaser 长达 max_len+6，与 _TEASER_MAX 语义不符。
    first = s[:m.end()] if (m and m.end() <= max_len) else s[:max_len]
    first = first.rstrip()
    if len(first) < len(s):
        first += "…"
    return first


def _render_reasoning(reasoning, indent="   "):
    """渲染依据字段——长文折叠进 <details>（借鉴 pr-agent 上游 #2510 的 Agent Prompt 折叠）。

    pr-agent #2510 评审把详细 Issue description / Issue Context 放 <details> 折叠，默认只露
    标题 + 一句后果，降低视觉噪声。本系统同理：依据 ≤200 字符平铺（短依据是快速判读信号），
    超阈值折叠——summary 露首句/前若干字（关键信息预览，author 不展开也能判读），body 完整
    保留（author 需要细节时展开）。

    indent 参数：子列表项缩进空格数。编号列表（`1. `）用默认 3 空格；task list（`- [ ] `）
    传 2 空格。body 缩进 = indent + 2（`- ` 占 2 字符），使 body 留在子列表项内容区内
    （CommonMark type-6 HTML block 不脱出——见下方折叠分支详注）。

    约束（用户 #168 续——「不能是动态获取，不能影响通过 api 获取全量 review 意见信息」）：
    summary 与 body 均静态嵌入 markdown（非动态获取，点击展开无网络请求）；全文始终在
    details body 内——API 取评论原文即得全量 review 意见，折叠仅影响 GitHub UI 默认展开态、
    不丢一字；机器可读的 <!-- touchstone-checklist --> 结构化标记不在本函数，不受影响。

    纯函数：输入字符串，输出 markdown 片段（空输入返回空串）。"""
    if not reasoning:
        return ""
    body_indent = " " * (len(indent) + 2)   # indent + "- " 2 字符 → body 留在子列表项内容区
    if len(reasoning) <= _REASONING_COLLAPSE_THRESHOLD:
        return f"{indent}- 依据：{reasoning}"
    # 折叠：summary 露字数 + 首句预览（关键信息），body 完整保留（author 需细节时展开）。
    # 用 f-string 而非 .format()：reasoning 含 { 或 } 时（代码片段/JSON 示例），
    # .format(body=reasoning) 虽不解析值里的 {}（值不被二次扫描），但 .format() 调用
    # 形态易让评审/读者误判会炸——f-string 直接内联，无此视觉歧义（评审两轮均提此点）。
    # return 串开头不带 \n：调用方 `"\n" + _render_reasoning(...)` 已加换行，与短依据分支
    # （`f"{indent}- 依据：..."` 开头也无 \n）保持一致——避免折叠分支双换行（评审第三轮提）。
    #
    # <details> 是 CommonMark type-6 HTML block，遇到空行即终止。此前 summary 与 body 之间、
    # body 与 </details> 之间各有一空行 → <details> 在第一个空行处被截断成孤立开标签，
    # body 变成列表项里的松散段落（始终可见、不在折叠区内）、</details> 变孤立闭标签——
    # 表现为「点击展开」点了没反应（展开后空、正文跑到外面）。去空行让整段留在同一 HTML
    # block 内，<details> 才是完整可折叠元素（#167 review 实测回归）。
    #
    # body 同理须防 reasoning 自带空行（多段依据、含 \n\n 的代码片段）：原样嵌 {reasoning}
    # 时其内部空行同样会截断 HTML block（#168 round-2 PRA-POSSIBLE_ISSUE）。折叠 body 的
    # 空白（\s+→空格）成单行——内容一字不丢，仅丢多段排版（折叠区内的显示形态本就不重要）；
    # 机器可读的 <!-- touchstone-checklist --> marker 存的是 reasoning 原文，API 取全文不受影响。
    teaser = _reasoning_teaser(reasoning)
    body = re.sub(r"\s+", " ", reasoning).strip()
    # body 与 </details> 须缩进到与 <details> 同列（indent + 2：子列表项 "- " 占 2 字符）。
    # 此前 body 缩进不足会让 body 脱出子列表项的内容区，CommonMark 判其不属于该列表项 →
    # HTML block 在 body 行处截断 → <details> 变空壳、body 渲染成列表项外的松散段落（始终
    # 可见）—— GitHub 实测 body_html 证实。缩进到 body_indent 让 body 留在子列表项内、
    # HTML block 完整（#167/#168 回归修复，v2 参数化缩进以兼容编号列表与 task list）。
    return (f"{indent}- <details><summary>依据（{len(reasoning)} 字）：{teaser}</summary>\n"
            f"{body_indent}{body}\n"
            f"{body_indent}</details>")


# ---- v2 版面函数（2026-08 评审模板重设计：七段 → 六段，去冗余）-------------------------
# v2 移除了 v1 的 _finding_entry / render_facts（含修改范围+规则命中）/ render_findings
# （态势区+AI 评审）三函数——版面合并：态势区→render_status_line（①），AI 评审+清单→
# render_findings_checklist（④），静态检查→render_facts_v2（③，去修改范围+规则命中）。
# 状态标记/措辞从 checklist.py 迁入（呈现层常量归呈现层）。checklist.py 只保留数据层
# （reconcile/parse/marker），可见渲染统一由 render.py 负责。
_STATUS_MARK = {"open": "- [ ]", "done": "- [x]", "waived": "- [x]", "split": "- [x]"}
_STATUS_LABEL = {"open": "⬜ 待处理", "done": "✅ 已复核销项",
                 "waived": "🟡 待人核准（author 豁免）", "split": "🟡 待人核准（author 拆出）"}


def _render_done_criteria(dc):
    """达成判据行内容：deterministic=规则复检；review=人工复核问题。纯函数。"""
    _spec = (dc or {}).get("spec") or {}
    if (dc or {}).get("kind") == "deterministic":
        return f"规则 `{_spec.get('recheck', '?')}` 复检不再命中"
    if (dc or {}).get("kind") == "review":
        q = _spec.get("question", "")
        # q 非空=有具体复核问题；q 空=诚实降级（描述 reconcile 实际机制）。
        return f"需人工复核：{q}" if q else "下一轮复检不再命中即销项"
    return ""


def _render_finding_meta(f):
    """行尾元数据小字（rule/严重度/置信/来源）。task list 子项缩进 2 空格。
    用 .get() 兜底：清单项可能源自只含部分字段的 finding（如测试夹具缺 confidence）。"""
    conf = f.get("confidence")
    conf_str = f"{conf:.2f}" if conf is not None else "—"
    return (f"  - <sub>`{f.get('rule_id', '?')}` · {f.get('severity', '')} · "
            f"置信 {conf_str} · 来源 {f.get('agent', '')}</sub>")


def render_facts_v2(scope_facts, gate_line=""):
    """③ v2 静态检查区：仅确定性事实——敏感路径命中 + 门禁状态。
    修改范围与 PR UI 统计重复 → 删除（观测意见 7）。规则命中详情并入「评审发现与销项」段。
    无内容（简单 PR 无敏感路径/无门禁）时整段省略——去恒定噪声。"""
    if not scope_facts:
        return ""
    if not scope_facts.get("parse_ok", True):
        return f"### 静态检查\n\n- ⚠️ {scope_facts.get('parse_warning', 'diff 解析失败：范围事实未生效')}"
    lines = []
    hits = scope_facts.get("sensitive_hits", [])
    if hits:
        by_rule = {}
        for h in hits:
            by_rule.setdefault(h["rule"], []).append(h["path"])
        for rule, paths in sorted(by_rule.items()):
            shown = ", ".join(f"`{p}`" for p in paths[:5]) + ("…" if len(paths) > 5 else "")
            lines.append(f"- 敏感路径命中（{rule}）：{shown}")
    if gate_line:
        lines.append(f"- 门禁状态：{gate_line}")
    if not lines:
        return ""            # 只有修改范围（已删）→ 整段省略（观测意见 7：简单 PR 零噪声）
    return "### 静态检查\n\n" + "\n".join(lines)


def render_status_line(risk, loop_info=None, checklist=None, rounds_left=None,
                       review_reliable=True):
    """① v2 状态行（合并观测意见 1+6）：循环决策 + 轮次 + 销项率 + 风险等级 — 合成一行 blockquote，
    替代旧版「横幅反馈循环行 + 态势风险行」两行。escalate 的 reason 有诊断价值（为何升级），保留；
    continue/converged 的 reason 是对轮次/销项率的复述——已在状态行结构化呈现，不复读。"""
    _RISK = {"high": "高", "mid": "中", "low": "低"}
    _ACTION = {"read+arbitrate": "需人工评审后合入", "read": "建议人工过目", "skip": "无需人工介入"}
    _DECISION = {"continue": "🔁 继续", "converged": "✅ 收敛", "escalate": "⬆️ 升级到人"}
    _BLAST = {"cross_module_contract": "跨模块契约变更", "security_surface": "涉及安全面"}

    parts = []
    if loop_info:
        decision = loop_info[0]
        reason = loop_info[1] if len(loop_info) > 1 else ""
        parts.append(_DECISION.get(decision, decision))
        if decision == "escalate" and reason:
            parts.append(reason)         # escalate reason 有诊断价值（为何升级），保留
    cl = checklist or {}
    if cl.get("round"):
        round_part = f"第 {cl['round']} 轮"
        if rounds_left is not None:
            round_part += f" · 剩余 {rounds_left} 轮"
        parts.append(round_part)
    if cl.get("items") and cl.get("resolved_rate") is not None:
        # 真值检查（非 `is not None`）：空清单 items=[] 时 resolved_rate=1.0（_rate 空列表
        # 归一），若用 `is not None` 会漏过空列表显示「销项率 100%」——对零项清单是噪声，
        # 与 v2 去冗余目标矛盾（PRA-GENERAL round-1）。空清单无项可销，不显示销项率。
        rate = min(100, max(0, int(round(cl["resolved_rate"] * 100))))
        parts.append(f"销项率 {rate}%")

    band = _RISK.get(risk.get("risk_band"), "未定")
    action = _ACTION.get(risk.get("human_action"), "建议人工过目")
    if review_reliable:
        parts.append(f"风险等级：{band} — {action}")
    else:
        parts.append(f"风险等级：{band}（LLM 评审不可信）— 需人工评审")

    line = "> " + " · ".join(parts)
    factors = "、".join(_BLAST.get(b, b) for b in (risk.get("blast_radius") or []))
    if factors:                       # 无触发因子时不显「触发因子：无」——去冗余（观测意见 7 同纪律）
        line += f"\n> **触发因子：** {factors}"
    return line


def render_findings_checklist(findings, checklist, review_reliable=True):
    """④ v2 评审发现与销项（合并观测意见 2+3+4）：AI 评审 + 待解决问题清单合为一段。
    所有发现（确定性规则命中 + LLM 建议）作为 - [ ] task list，每条含完整详情 + 销项状态。
    保留 GitHub 原生 checkbox（用户明确要求）。sig 兼作位置与 ack 锚点（不再单列位置/锚点行）。
    样板「销项跟踪：…见上方」删除（详情已在同段，观测意见 3）。

    findings↔checklist 按 sig join：开放项有当前 finding（显示完整详情 + 最新 direction），
    已销项项可能无当前 finding（用清单存储的 direction/reasoning 历史快照，sparse 显示）。
    排序：开放项在前（按置信降序），已销项项在后（不再抢注意力）。"""
    cl = checklist or {"round": 0, "items": [], "resolved_rate": 1.0}
    items = cl.get("items", [])
    if not items:
        return ""    # 无清单项（干净 PR / 全销项）→ 整段省略（与 ③/⑤「无内容整段省略」纪律一致，
                     # PRA-REVIEW round-2）。防静默故障溯源在 ② alerts 段（已端到端运行/0 条建议），
                     # 不靠此处的恒定标题——此前「### 评审发现与销项\n本次无可自改发现。」是干净
                     # PR 上的恒定噪声，与 v2 去冗余目标矛盾。

    # findings↔清单项 join 用 sig 建索引。重复 sig（同 rule:file:line 的多条 finding）
    # keep-first——与 from_findings 的去重（seen 集，keep-first）一致：清单项已是首条，
    # 索引也取首条才不出现「清单用首条 direction、元数据却取末条」的错配（PRA-GENERAL round-5）。
    finding_by_sig = {}
    for f in (findings or []):
        s = sig_of(f)
        if s not in finding_by_sig:
            finding_by_sig[s] = f

    def _sort_key(it):
        f = finding_by_sig.get(it["sig"])
        conf = f.get("confidence", 0) if f else 0
        return (0 if it["status"] == "open" else 1, -conf)

    total = len(items)
    n_open = sum(1 for it in items if it["status"] == "open")
    capped = total > MAX_FINDINGS_IN_SUMMARY
    head = f"### 评审发现与销项（共 {total} 条"
    if n_open:
        head += f"，待销项 {n_open}"
    if capped:
        head += f"，仅列前 {MAX_FINDINGS_IN_SUMMARY} 条"
    head += "）"
    lines = [head, ""]

    # 封顶：大 PR 产出大量条目 → 列表封顶避免撑破 GitHub 65536 字符限。开放项优先（sort_key
    # 使 open 排前），已销项项按预算余量跟进。超出部分折叠到尾注（完整清单在 marker 里有）。
    shown = sorted(items, key=_sort_key)[:MAX_FINDINGS_IN_SUMMARY]
    for it in shown:
        f = finding_by_sig.get(it["sig"])
        mark = _STATUS_MARK.get(it["status"], "- [ ]")
        label = _STATUS_LABEL.get(it["status"], "")
        # 开放项用当前发现的 direction（最新评审）；已销项用清单存储的（历史快照）
        if f:
            direction = f.get("fix_direction") or f.get("suggested_fix") or ""
            rationale = f.get("rationale") or ""
            reasoning = f.get("fix_reasoning") or ""
            dc = f.get("done_criteria") or {}
        else:
            direction = it.get("direction") or ""
            rationale = ""
            reasoning = it.get("reasoning") or ""
            dc = it.get("done_criteria") or {}
        # 标题：方向作标题（加粗）。无方向时按状态区分占位——open 项「待补」提示 author 补方向；
        # 已销项项（done/waived/split）方向是历史快照、本就可能未留存，「待补」会误导（待补=待办，
        # 但已销项无需再补）→ 标「已销项」（PRA-REVIEW round-3 data-loss）。
        if direction:
            title = f"**{direction}**"
        elif it["status"] == "open":
            title = "（待补修复方向）"
        else:
            title = "（已销项）"
        lines.append(f"{mark} {title}" + (f" {label}" if label else "") + f" — `{it['sig']}`")
        # rationale（问题陈述）作首条子项；与 direction 同文则省（去冗余，同 _finding_entry 纪律）
        if rationale and rationale != direction:
            lines.append(f"  - {rationale}")
        # reasoning（依据）与 rationale 或 direction 同文则省——成对去冗余（PRA-REVIEW round-4：
        # 已销项项 rationale="" 时 `reasoning != ""` 恒真，若 reasoning==direction 会复读标题，
        # 补 `!= direction` 守卫使两分支（open 有 finding / resolved 无 finding）去冗余一致）。
        if reasoning and reasoning != rationale and reasoning != direction:
            r = _render_reasoning(reasoning, indent="  ")   # task list 子项缩进 2 空格
            if r:
                lines.append(r)
        dc_line = _render_done_criteria(dc)
        if dc_line:
            lines.append(f"  - 达成判据：{dc_line}")
        if it.get("note"):
            lines.append(f"  - 说明：{it['note']}")
        if it.get("guard"):                    # 守卫事实（issue #139）：确定性 AST 事实，供 waived 佐证
            lines.append(f"  - 守卫事实：{it['guard']}")
        if f:
            lines.append(_render_finding_meta(f))
    if capped:
        lines.append("")
        lines.append(f"……另有 {total - MAX_FINDINGS_IN_SUMMARY} 条（超列表上限，完整清单见 marker）。")
    return "\n".join(lines)


def render_reference(verification_blocks=None, has_checklist_items=False):
    """⑤ v2 参考信息（观测意见 5）：验证/日志 + 申报指引，全部 <details> 折叠（默认不占屏）。
    无内容时整段省略。<details> 是 CommonMark type-6 HTML block——summary/body/</details>
    之间不得有空行（#168 回归）；行内 code 用 <code> 标签（HTML block 不解析 markdown）。"""
    blocks = []
    if verification_blocks:
        content = "\n\n".join(verification_blocks)
        blocks.append(f"<details><summary>验证与日志</summary>\n{content}\n</details>")
    if has_checklist_items:
        blocks.append("<details><summary>如何申报销项</summary>\n"
                      "发评论，内容为 <code>touchstone-ack</code> 代码块，每行 "
                      "<code>&lt;签名&gt;: done|waived: 理由|split: 链接</code>。"
                      "勾选/申报是输入信号，以评审方按达成判据复核后的本清单为准。\n"
                      "</details>")
    if not blocks:
        return ""
    return "### 参考信息\n\n" + "\n\n".join(blocks)


def render_report(risk, findings, alerts="", scope_facts=None, checklist=None,
                  rounds_left=None, loop_info=None, verification_blocks=None,
                  markers="", gate_line="",
                  review_reliable=True, engine_status="ok", ai_raw_count=0, added_lines=0,
                  engine_detail=""):
    """v2 六段版面（模板唯一定义，代码只填充）：
      ① 标题 + 状态行（循环 + 风险合一，观测意见 1+6）
      ② 告警（降级/CAUTION/溯源/同源提示，blockquote）
      ③ 静态检查（敏感路径/门禁，简单 PR 整段省略——观测意见 7）
      ④ 评审发现与销项（AI 评审 + 清单合一 - [ ] task list——观测意见 2+3+4）
      ⑤ 参考信息（验证/日志 + 申报指引，<details> 折叠——观测意见 5）
      ⑥ 机器 marker
    alerts 取代旧 banner 参数（不再含循环行——循环行归①状态行）。"""
    status = render_status_line(risk, loop_info, checklist, rounds_left, review_reliable)
    # ② 告警：不可信时 [!CAUTION] 置顶替代降级横幅；其余告警（det/llm/unverified/telemetry/
    # 溯源/同源提示）作为 blockquote 追加。可信时直接逐行包 blockquote。
    if not review_reliable:
        kept = []
        if alerts:
            for ln in alerts.split("\n"):
                if not ln.strip():
                    continue
                kept.append(ln)
        alerts_md = render_unreliable_callout(engine_status, ai_raw_count, added_lines, engine_detail)
        if kept:
            alerts_md += "\n\n" + "\n".join(("> " + ln if not ln.startswith(">") else ln) for ln in kept)
    elif alerts:
        alerts_md = "\n".join(("> " + ln if ln.strip() else ">") for ln in alerts.split("\n"))
    else:
        alerts_md = ""
    has_items = bool((checklist or {}).get("items"))
    parts = {
        "status": status,
        "alerts": alerts_md,
        "facts": render_facts_v2(scope_facts, gate_line) if scope_facts else "",
        "findings": render_findings_checklist(findings, checklist, review_reliable),
        "reference": render_reference(verification_blocks, has_items),
        "markers": markers or "",
    }
    out = _fill_template(_load_template(), parts)   # 单遍填充（A2-F1）：不重扫已填入内容，防占位符注入
    # 折叠空段落留下的多余空行；剥掉模板头部注释（HTML 注释会带进评论——只保留 marker 类注释）
    out = re.sub(r"<!-- =+\n.*?=+ -->\n?", "", out, flags=re.S)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def render_summary(risk, findings):
    label = {"high": "高", "mid": "中", "low": "低"}.get(risk["risk_band"], "未定")
    action = {"read+arbitrate": "需人工评审后合入", "read": "建议人工过目",
              "skip": "无需人工介入"}.get(risk["human_action"], "建议人工过目")
    lines = [
        "**Touchstone · ADVISORY**（不拦截合入，与人工审核并行）",
        "",
        f"风险等级：**{label}** — {action}",
    ]
    _blast = {"cross_module_contract": "跨模块契约变更", "security_surface": "涉及安全面"}
    if risk["blast_radius"]:
        lines.append("触发因子：" + "、".join(_blast.get(b, b) for b in risk["blast_radius"]))
    lines.append("")
    if not findings:
        lines.append("本次未发现规则范围内的问题。")
    else:
        lines.append(f"发现 {len(findings)} 条（按置信降序）：")
        for f in findings:
            lines.append(
                f"- `{f['rule_id']}` [{f.get('severity','')}] "
                f"conf={f['confidence']:.2f} · {f['agent']} · "
                f"`{f.get('file','?')}:{f.get('line','?')}`\n"
                f"  - {f.get('rationale','')}\n"
                f"  - 建议：{f.get('suggested_fix','')}"
            )
    return "\n".join(lines)


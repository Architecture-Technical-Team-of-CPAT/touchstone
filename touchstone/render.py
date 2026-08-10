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
        return "{{banner}}\n\n{{summary_line}}\n\n{{facts}}\n\n{{findings}}\n\n{{checklist}}\n\n{{verification}}\n\n{{markers}}"


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


def _location(f):
    """渲染位置串——行号缺失时不显示 `:None`（借鉴 pr-agent 上游 #2510 评审的定位精度）。

    pr-agent 偶不返回 relevant_lines_start（review 类 key_issues 的 start_line 缺失），
    此前 f.get('line','?') 对 line=None 返回 None（键在但值为 None），渲染为 `file:None`——
    既丑又让 author 误以为「行号是字面量 None」。行号缺失时只显示文件名，干净且不误导。

    review_provider.normalize 已做 line_start→line_end 回退（本 PR 配套），此处是渲染侧的
    最终兜底：两层联合保证 `:None` 永不出现在评审报告里。"""
    file_ = f.get("file") or "?"
    line_ = f.get("line")
    return f"{file_}:{line_}" if line_ is not None else file_


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


def _render_reasoning(reasoning):
    """渲染依据字段——长文折叠进 <details>（借鉴 pr-agent 上游 #2510 的 Agent Prompt 折叠）。

    pr-agent #2510 评审把详细 Issue description / Issue Context 放 <details> 折叠，默认只露
    标题 + 一句后果，降低视觉噪声。本系统同理：依据 ≤200 字符平铺（短依据是快速判读信号），
    超阈值折叠——summary 露首句/前若干字（关键信息预览，author 不展开也能判读），body 完整
    保留（author 需要细节时展开）。

    约束（用户 #168 续——「不能是动态获取，不能影响通过 api 获取全量 review 意见信息」）：
    summary 与 body 均静态嵌入 markdown（非动态获取，点击展开无网络请求）；全文始终在
    details body 内——API 取评论原文即得全量 review 意见，折叠仅影响 GitHub UI 默认展开态、
    不丢一字；机器可读的 <!-- touchstone-checklist --> 结构化标记不在本函数，不受影响。

    纯函数：输入字符串，输出 markdown 片段（空输入返回空串）。"""
    if not reasoning:
        return ""
    if len(reasoning) <= _REASONING_COLLAPSE_THRESHOLD:
        return f"   - 依据：{reasoning}"
    # 折叠：summary 露字数 + 首句预览（关键信息），body 完整保留（author 需细节时展开）。
    # 用 f-string 而非 .format()：reasoning 含 { 或 } 时（代码片段/JSON 示例），
    # .format(body=reasoning) 虽不解析值里的 {}（值不被二次扫描），但 .format() 调用
    # 形态易让评审/读者误判会炸——f-string 直接内联，无此视觉歧义（评审两轮均提此点）。
    # return 串开头不带 \n：调用方 `"\n" + _render_reasoning(...)` 已加换行，与短依据分支
    # （`f"   - 依据：..."` 开头也无 \n）保持一致——避免折叠分支双换行（评审第三轮提）。
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
    return (f"   - <details><summary>依据（{len(reasoning)} 字）：{teaser}</summary>\n"
            f"   {body}\n"
            f"   </details>")


def _finding_entry(i, f):
    """单条发现的渲染（规则命中与 AI 建议共用）：位置 — 问题 + 修复方向/依据/达成判据 + 行尾元数据。

    字段去冗余（借鉴 pr-agent 上游 #2510 评审写作）：title 行已含 rationale（一句话问题），
    修复方向若与 rationale 同文则不再复读（此前的「修复方向：<与标题完全相同的文字>」纯噪声）。
    依据字段早有同等去重守卫（reasoning != rationale 才显示），本处补齐对称。"""
    direction = f.get("fix_direction") or f.get("suggested_fix") or ""
    reasoning = f.get("fix_reasoning") or ""
    rationale = f.get("rationale") or ""
    dc = f.get("done_criteria") or {}
    _spec = dc.get("spec") or {}
    if dc.get("kind") == "deterministic":
        dc_line = f"规则 `{_spec.get('recheck', '?')}` 复检不再命中"
    elif dc.get("kind") == "review":
        q = _spec.get("question", "")
        # q 非空=有具体复核问题（设计意见 1 的复核判据，如「回滚路径是否覆盖跨模块调用失败」）；
        # q 空=诚实降级（model 来源在 normalize 层给不出具体问题，不再用「{direction}是否已解决」
        # 模板复读）。降级时如实描述 reconcile 实际机制：下一轮 sig 不再现即自动销项。
        dc_line = f"需人工复核：{q}" if q else "下一轮复检不再命中即销项"
    else:
        dc_line = ""
    e = f"{i}. **`{_location(f)}`** — {rationale}"
    if direction and direction != rationale:
        e += f"\n   - 修复方向：{direction}"
    if reasoning and reasoning != rationale:
        e += "\n" + _render_reasoning(reasoning)
    if dc_line:
        e += f"\n   - 达成判据：{dc_line}"
    e += (f"\n   - <sub>`{f['rule_id']}` · {f.get('severity','')} · "
          f"置信 {f['confidence']:.2f} · 来源 {f['agent']}</sub>")
    return e


def render_facts(scope_facts, gate_line="", lineage=None, rule_findings=None):
    """③ 静态检查区：不经 LLM 的确定性输出——修改范围 + 敏感路径命中 + 门禁 + 同源提示，
    以及确定性【规则命中的逐条发现】（contract/stack/size，可复现）。与「AI 评审」段并列同级
    H3，构成「确定性 vs LLM」两层视图。"""
    if not scope_facts and not rule_findings:
        return ""
    lines = ["### 静态检查", ""]
    if scope_facts and not scope_facts.get("parse_ok", True):
        lines.append(f"- ⚠️ {scope_facts.get('parse_warning', 'diff 解析失败：范围事实未生效')}")
        scope_facts = None      # 解析失败：跳过范围行，但仍渲染下方规则命中
    if scope_facts:
        t = scope_facts.get("totals", {})
        lines.append(f"- 修改范围：{t.get('files', 0)} 个文件（+{t.get('added', 0)} / −{t.get('deleted', 0)} 行）")
        hits = scope_facts.get("sensitive_hits", [])
        if hits:
            by_rule = {}
            for h in hits:
                by_rule.setdefault(h["rule"], []).append(h["path"])
            for rule, paths in sorted(by_rule.items()):
                shown = ", ".join(f"`{p}`" for p in paths[:5]) + ("…" if len(paths) > 5 else "")
                lines.append(f"- 敏感路径命中（{rule}）：{shown}")
        else:
            lines.append("- 敏感路径命中：无")
        if gate_line:
            lines.append(f"- 门禁状态：{gate_line}")
        if lineage and lineage.get("lineage"):
            entries = [e for e in lineage.get("lineage", []) if isinstance(e, dict) and "number" in e]
            if entries:
                hist = "、".join(f"#{e['number']}" for e in entries)
                lines.append(f"- ⚠️ 同源提示：与已关闭的 {hist} 内容同源，历史已消耗 "
                         f"{lineage.get('rounds_spent', 0)} 轮、继承未销项 "
                         f"{len(lineage.get('inherited_open_items', []))} 条，剩余轮次 "
                         f"{lineage.get('rounds_left', '?')}（重置需 `rounds-reset` label）")
    if rule_findings:
        shown = rule_findings[:MAX_FINDINGS_IN_SUMMARY]
        lines.append("")
        lines.append("#### 规则命中（可复现）")
        lines.append("")
        for i, f in enumerate(shown, 1):
            lines.append(_finding_entry(i, f))
        if len(rule_findings) > MAX_FINDINGS_IN_SUMMARY:
            lines.append("")
            lines.append(f"……另有 {len(rule_findings) - MAX_FINDINGS_IN_SUMMARY} 条（确定性核对已覆盖全文，见 check 标题/总闸）。")
    return "\n".join(lines)


def render_findings(risk, findings, review_reliable=True):
    """②态势区 + ④「AI 评审」（仅 LLM 发现）。
    态势区：「标签 + 人话」陈述行——风险等级（含"该怎么办"）与触发因子；verification_decision
      机器路由字段不入此区，降到「验证与日志」。
    AI 评审：仅 LLM（pr-agent）发现；确定性规则命中的逐条发现归「静态检查」段（render_facts）。
      `findings` 入参此处即为全部发现，函数内按来源过滤只渲染 LLM 部分。"""
    _RISK = {"high": "高", "mid": "中", "low": "低"}
    _ACTION = {"read+arbitrate": "需人工评审后合入", "read": "建议人工过目",
               "skip": "无需人工介入"}
    _BLAST = {"cross_module_contract": "跨模块契约变更", "security_surface": "涉及安全面"}
    band = _RISK.get(risk.get("risk_band"), "未定")
    action = _ACTION.get(risk.get("human_action"), "建议人工过目")
    factors = "、".join(_BLAST.get(b, b) for b in (risk.get("blast_radius") or []))
    if review_reliable:
        head = [f"> **风险等级：{band}** — {action}"]
    else:
        head = [f"> **风险等级：{band}** <sub>（仅确定性信号，LLM 评审不可信）</sub>"
                " — 需人工评审，原 AI 建议不采信"]
    if factors:                       # 无触发因子时不显「触发因子：无」——去冗余
        head.append(f"> **触发因子：** {factors}")

    ai_based = [f for f in (findings or []) if str(f.get("agent", "")).startswith("pr-agent")]
    total = len(ai_based)
    if not ai_based:
        return "\n".join(head), "### AI 评审\n\n本次 LLM 未提出建议。"
    cap = (f"，仅列前 {MAX_FINDINGS_IN_SUMMARY} 条" if total > MAX_FINDINGS_IN_SUMMARY else "")
    body = [f"### AI 评审（共 {total} 条{cap}）", ""]
    for i, f in enumerate(sorted(ai_based, key=lambda x: -x.get("confidence", 0))[:MAX_FINDINGS_IN_SUMMARY], 1):
        body.append(_finding_entry(i, f))
    if total > MAX_FINDINGS_IN_SUMMARY:
        body.append("")
        body.append(f"……另有 {total - MAX_FINDINGS_IN_SUMMARY} 条（超列表上限）。")
    return "\n".join(head), "\n".join(body)


def render_report(risk, findings, banner="", scope_facts=None, checklist_md="",
                  verification_md="", markers="", gate_line="", lineage=None,
                  review_reliable=True, engine_status="ok", ai_raw_count=0, added_lines=0,
                  engine_detail=""):
    """按七段版面模板填充评审报告（修订设计 §3 意见 4）。版面由模板唯一定义。"""
    head, findings_md = render_findings(risk, findings, review_reliable=review_reliable)
    summary_line = head          # ② 态势表：风险与建议动作一眼扫读
    rule_findings = [f for f in (findings or []) if not str(f.get("agent", "")).startswith("pr-agent")]
    # ① 状态横幅（降级说明/循环状态/0-发现溯源）统一 blockquote——与正文视觉区隔；
    #    评审不可信时 [!CAUTION] 告警置顶【替代】常规横幅的降级/溯源部分（原因已并入
    #    告警，避免同一信息两处重复），循环状态行仍保留在告警之后。
    if not review_reliable:
        # 不可信时 [!CAUTION] 告警置顶替代降级/溯源部分（原因已并入告警，避免重复）。
        # 但 banner 可能还载有与可信度无关的内容（det_warning/llm_notes/unverified_claims
        # 及循环状态行）--这些不能丢，作为 blockquote 追加在告警之后（pr-agent 评审意见：
        # 不可信时整块 banner 被丢弃会静默丢失重要通知）。
        kept = []
        if banner:
            for ln in banner.split("\n"):
                if not ln.strip():
                    continue
                kept.append(ln)
        banner = render_unreliable_callout(engine_status, ai_raw_count, added_lines, engine_detail)
        if kept:
            banner += "\n\n" + "\n".join(("> " + ln if not ln.startswith(">") else ln) for ln in kept)
    elif banner:
        banner = "\n".join(("> " + ln if ln.strip() else ">") for ln in banner.split("\n"))
    parts = {
        "banner": banner or "",
        "summary_line": summary_line,
        "facts": render_facts(scope_facts, gate_line, lineage, rule_findings=rule_findings) if (scope_facts or rule_findings) else "",
        "findings": findings_md,
        "checklist": checklist_md or "",
        "verification": verification_md or "",
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


"""修订设计（评审意见 1、2、3、4、5、6、7、10 落实）的行为测试。

按意见分组：
  意见 7  —— 范围事实 scope_facts：确定性范围/敏感路径/指纹/解析失败防静默
  意见 2  —— Finding 方向+依据：模型来源补丁降级、确定性来源保留精确修复通道
  意见 1  —— 达成判据：确定性/复核两档均产出
  意见 3  —— 收敛清单：状态机、复核销项、ack 协议、渲染/marker 往返、无推进
  意见 1+3—— loop_step 清单语义：收敛=清单销项完毕
  意见 10 —— 轮次台账：指纹相似度、同源检测、余额继承、rounds-reset、余额为零升级
  意见 4  —— 版面模板：七段齐备、模板注释不外泄
"""
import json

from touchstone import checklist as cl
from touchstone import contract_check as cc
from touchstone import lineage
from touchstone import loop
from touchstone import orchestrator
from touchstone import review_provider as rp
from helpers import build_diff


# ---------------- 意见 7：范围事实 ----------------
def test_scope_facts_files_totals_and_sensitive_hits():
    diff = build_diff([("auth/login.py", ["def login(): pass", "x = 1"], True),
                       ("db/migrations/001.sql", ["CREATE TABLE t (id int);"], True)])
    sf = cc.scope_facts(diff)
    assert sf["parse_ok"] and sf["totals"]["files"] == 2 and sf["totals"]["added"] == 3
    rules = {h["rule"] for h in sf["sensitive_hits"]}
    assert rules == {"security_surface", "cross_module_contract"}


def test_scope_facts_fingerprint_comparable_and_stable():
    diff = build_diff([("a.py", ["x = 1"], True)])
    f1, f2 = cc.scope_facts(diff)["fingerprint"], cc.scope_facts(diff)["fingerprint"]
    assert f1 == f2 and f1["fileset"] == ["a.py"] and f1["shape"]["a.py"] == [1, 0]


def test_scope_facts_parse_failure_not_silent():
    sf = cc.scope_facts("@@@ 不是合法 diff @@@\n+++ x")
    # 解析失败必须显式置位，不得让空结果被读成"干净"（防静默故障）
    assert sf["parse_ok"] is False and sf["parse_warning"]
    assert sf["changed_files"] == [] and sf["sensitive_hits"] == []


def test_scope_facts_hits_feed_blast_radius_deterministically():
    # 敏感路径命中但【零发现】：影响面照样点亮——模型漏报不再导致影响面漏判
    diff = build_diff([("auth/token.py", ["x = 1"], True)])
    sf = cc.scope_facts(diff)
    _, risk = rp.map_verdict([], scope_facts=sf)
    assert "security_surface" in risk["blast_radius"]


def test_load_scope_rules_repo_override(tmp_path):
    d = tmp_path / ".touchstone"
    d.mkdir()
    (d / "scope-rules.yaml").write_text(
        "factors:\n  security_surface:\n    - 'only_this'\n", encoding="utf-8")
    rules = cc.load_scope_rules(str(tmp_path))
    assert rules["security_surface"] == ["only_this"]          # 声明的 factor 整体替换
    assert rules["cross_module_contract"]                       # 未声明的保留默认


# ---------------- 意见 2：方向+依据，补丁降级 ----------------
def test_normalize_downgrades_patch_and_carries_direction_reasoning():
    raw = {"code_suggestions": [{
        "relevant_file": "a.py", "relevant_lines_start": 3,
        "one_sentence_summary": "将配置解析独立封装",
        "suggestion_content": "配置模块可独立封装、独立测试，降低本 PR 的评审面",
        "improved_code": "def load_config():\n    ...",       # 补丁——不得进任何建议字段
        "label": "maintainability"}]}
    f = rp.normalize(rp.parse_pr_agent(raw))[0]
    assert f["fix_direction"] == "将配置解析独立封装"
    assert "评审面" in f["fix_reasoning"]
    assert "deterministic_patch" not in f                       # 模型来源禁填精确修复
    blob = json.dumps(f, ensure_ascii=False)
    assert "load_config" not in blob                            # improved_code 已降级，不外泄
    assert f["suggested_fix"] == f["fix_direction"]             # 过渡别名=方向，不含补丁


def test_deterministic_finding_carries_direction_and_recheck_criteria():
    # 确定性来源（contract-check）：方向+依据+确定性判据（规则复检）
    diff = build_diff([("src/a.py", ["import os"], True)])
    fs = cc.check_contract_consistency(diff, {"scope": ["docs/*"]},
                                       {"SCOPE-001": {"severity": "warn"}})
    f = next(x for x in fs if x["rule_id"] == "SCOPE-001")
    assert f["fix_direction"] and f["fix_reasoning"]
    assert f["done_criteria"] == {"kind": "deterministic", "spec": {"recheck": "SCOPE-001"}}


def test_author_actionable_gates_on_fix_direction():
    ri = {}
    with_dir = {"rule_id": "X-1", "file": "a", "line": 1, "fix_direction": "改方向"}
    without = {"rule_id": "X-2", "file": "a", "line": 2}
    legacy = {"rule_id": "X-3", "file": "a", "line": 3, "suggested_fix": "旧字段仍受理"}
    acts = loop.author_actionable([with_dir, without, legacy], ri)
    assert {a["rule_id"] for a in acts} == {"X-1", "X-3"}


# ---------------- 意见 1：达成判据 ----------------
def test_review_source_gets_review_done_criteria():
    raw = {"review": {"key_issues_to_review": [{
        "relevant_file": "a.py", "start_line": 1,
        "issue_header": "边界未处理", "issue_content": "空输入分支缺失", "label": "possible issue"}]}}
    f = rp.normalize(rp.parse_pr_agent(raw))[0]
    assert f["done_criteria"]["kind"] == "review"
    # 诚实降级（见 review_provider.normalize 注释）：model 来源给不出具体复核问题，
    # question 留空而非复读 direction。direction 仍在 fix_direction 字段（不丢）。
    assert f["done_criteria"]["spec"]["question"] == ""
    assert f["fix_direction"] == "边界未处理"           # 方向不丢，仅判据不复读


def test_review_done_criteria_renders_honest_degradation():
    """model 来源判据 question 留空 → 渲染诚实降级行（不套 direction 模板复读）。

    设计意见 1 要求 review 类带「一句可回答的具体复核问题」（示例：回滚路径是否覆盖跨模块
    调用失败）。normalize 层给不出 → 诚实留空。渲染层退为「下一轮复检不再命中即销项」
    （reconcile 实际机制：sig 不再现即自动销项）。对比：确定性来源渲染「规则 X 复检不再命中」。
    v2：达成判据行由纯函数 _render_done_criteria 产出（从 _finding_entry 抽出，合并段复用）。"""
    from touchstone.render import _render_done_criteria
    # model 来源：question 空 → 诚实降级
    dc_model = {"kind": "review", "spec": {"question": ""}}
    assert _render_done_criteria(dc_model) == "下一轮复检不再命中即销项"
    # 带具体复核问题的 review（如 guard_context / 未来 prompt 工程产出）→ 渲染「需人工复核」
    dc_specific = {"kind": "review", "spec": {"question": "回滚路径是否覆盖跨模块调用失败？"}}
    assert _render_done_criteria(dc_specific) == "需人工复核：回滚路径是否覆盖跨模块调用失败？"
    # 确定性来源不变：规则复检
    dc_det = {"kind": "deterministic", "spec": {"recheck": "SCOPE-001"}}
    assert _render_done_criteria(dc_det) == "规则 `SCOPE-001` 复检不再命中"


# ---------------- 意见 3：收敛清单 ----------------
def _finding(rid, file="a.py", line=1, direction="改这里", kind="deterministic"):
    dc = ({"kind": "deterministic", "spec": {"recheck": rid}} if kind == "deterministic"
          else {"kind": "review", "spec": {"question": "解决了吗？"}})
    return {"rule_id": rid, "file": file, "line": line, "fix_direction": direction,
            "fix_reasoning": "依据", "done_criteria": dc}


# ---------------- 借鉴 pr-agent #2510：折叠长依据 ----------------
def _rf(rid, rationale="问题", direction="方向", reasoning="", line=1):
    """render.render_findings_checklist 用的最小 finding dict（v2：合并段单发现驱动）。"""
    return {"rule_id": rid, "file": "a.py", "line": line, "rationale": rationale,
            "fix_direction": direction, "fix_reasoning": reasoning,
            "done_criteria": {"kind": "review", "spec": {"question": "?"}},
            "severity": "warn", "confidence": 0.7, "agent": "pr-agent:review"}


def test_short_reasoning_rendered_inline():
    """短依据（≤200 字符）平铺不折叠——短依据是快速判读信号，折叠反而增操作。
    v2：_render_reasoning 是纯函数（indent 参数化），直接断言其输出（原经 _finding_entry 间接测）。"""
    from touchstone.render import _render_reasoning
    short = "这是一条短依据。" * 5   # ~45 字符
    out = _render_reasoning(short, indent="  ")    # task list 子项缩进 2 空格
    assert f"  - 依据：{short}" in out
    assert "<details>" not in out


def test_long_reasoning_collapsed_into_details():
    """长依据（>200 字符）折叠进 <details>——降低清单视觉噪声（借鉴 pr-agent #2510）。

    author 一眼扫清单只看标题+方向；需要依据细节时再点开。pr-agent 上游 #2510 同样把
    Issue description / Issue Context 放 <details> 折叠，默认只露标题 + 一句后果。
    v2：_render_reasoning 纯函数直接断言（indent 参数化兼容编号列表与 task list）。"""
    from touchstone.render import _render_reasoning, _TEASER_MAX
    long = "x" * 250
    out = _render_reasoning(long, indent="  ")
    assert "<details>" in out and "</details>" in out
    assert f"依据（{len(long)} 字）" in out             # summary 露字数
    assert long[:_TEASER_MAX] in out                    # summary 露前 60 字（关键信息预览）
    assert long in out                                  # 全文在 details body 内（API 取全文不变）


def test_details_summary_shows_first_sentence_teaser():
    """<details> summary 露首句/前若干字（关键信息预览），author 不展开也能判读依据要点。

    用户 #168 续——「计划把关键信息的 summary 展示出来，其他通过点击详情按钮来展开」：
    summary 从无信息标签「依据（N 字）」升级为首句预览（核心论断）。两条硬约束：
    (a) 不能动态获取——summary 与 body 均静态嵌入 markdown，点击展开无网络请求；
    (b) 不能影响 API 取全量 review 意见——body 完整保留 reasoning 全文（API 取评论原文即得全文）。"""
    from touchstone.render import _render_reasoning, _TEASER_MAX
    first_sentence = "版本号从 0.2.3 跳到 0.2.5，跳过了 0.2.4，需确认 CHANGELOG。"
    long = first_sentence + "后续展开论证" * 40            # > 200 字符阈值 + 首句 < max_len
    out = _render_reasoning(long)
    # summary 在首句处截断，露出核心论断（不展开也可见）
    assert "版本号从 0.2.3 跳到 0.2.5" in out
    assert "…" in out                                       # 截断标记（首句后还有内容）
    # body 完整保留全文（约束 b：API 取全文不变）
    assert long in out
    # 首句长度 < max_len → teaser 取整句（不被硬截断到 max_len）
    assert first_sentence in out


def test_details_summary_truncates_runon_sentence_to_max_len():
    """首句超长（无句末标点）→ teaser 硬截断到 _TEASER_MAX 字，加 …。"""
    from touchstone.render import _render_reasoning, _reasoning_teaser, _TEASER_MAX
    runon = "a" * 250                                       # 无句末标点
    teaser = _reasoning_teaser(runon)
    assert teaser.endswith("…")
    assert len(teaser) == _TEASER_MAX + 1                   # 60 字 + …
    out = _render_reasoning(runon)
    assert runon in out                                     # body 全文


def test_teaser_strict_max_len_when_first_sentence_exceeds():
    """首句末标点恰在 max_len 之外 → 硬截断到 max_len（不留余量），加 …。

    回归 #168 round-2「strict max length」：此前 max_len+5 余量会让首句 teaser 长达
    max_len+6，与 _TEASER_MAX 语义不符、与 run-on 分支（max_len+1）不一致。"""
    from touchstone.render import _reasoning_teaser, _TEASER_MAX
    # 首句 = (max_len+10) 个 a + 句号；句末在 max_len+11 处（> max_len）→ 走硬截断分支
    first_sentence = "a" * (_TEASER_MAX + 10) + "。"
    long = first_sentence + "后续展开论证" * 100
    teaser = _reasoning_teaser(long)
    assert teaser == "a" * _TEASER_MAX + "…"                # 硬截断到 max_len + …
    assert len(teaser) == _TEASER_MAX + 1                   # 严格 ≤ max_len + 1


def test_details_body_flattens_internal_blank_lines():
    """reasoning 自带空行（多段依据 / 含 \\n\\n 的代码片段）不破 <details> HTML block。

    回归 #168 round-2 PRA-POSSIBLE_ISSUE：body 原样嵌 {reasoning} 时其内部空行同样会
    截断 type-6 HTML block（与 summary/body 间空行同因）。折叠 body 空白（\\s+→空格）
    成单行——内容一字不丢，仅丢多段排版；marker 存 reasoning 原文，API 取全文不受影响。"""
    from touchstone.render import _render_reasoning
    long = ("第一段依据，说明问题。" + "详情" * 100 + "\n\n" +   # 段间空行
            "第二段依据，补充论证。" + "更多" * 100)
    out = _render_reasoning(long)
    assert "<details>" in out and "</details>" in out
    blk = out[out.index("<details>"):out.index("</details>") + len("</details>")]
    assert "\n\n" not in blk                          # 折叠后 body 无空行（HTML block 完整）
    # 内容不丢（两段都在 body 内）
    assert "第一段依据" in out and "第二段依据" in out


def test_details_block_has_no_blank_lines_inside():
    """回归 #167 review：<details> 是 CommonMark type-6 HTML block，遇空行即终止。
    若 </summary> 与 body、body 与 </details> 之间有空行，<details> 在首个空行处被截断成
    孤立开标签——body 变成列表项里的松散段落（始终可见、不在折叠区）、</details> 变孤立闭
    标签，表现即「点击展开」无反应（展开后空、正文跑到外面）。整段 <details>...</details>
    须留在同一 HTML block 内（无空行）才是完整可折叠元素。"""
    from touchstone.render import _render_reasoning
    long = "依据正文内容。" * 30          # > 200 字符阈值，触发折叠分支
    out = _render_reasoning(long)
    assert "<details>" in out and "</details>" in out
    # 取 <details> 到 </details> 的片段，断言其内无空行（无连续两个 \n）
    details_block = out[out.index("<details>"):out.index("</details>") + len("</details>")]
    assert "\n\n" not in details_block, (
        "<details> 块内含空行 → HTML block 在首个空行处截断，折叠失效（#167 回归）")


def test_details_body_indented_to_sublist_content_column():
    """回归 PR #171 review（fetchBody HTML 诊断）：<details> 在 `   - ` 子列表项里（col 5），
    body 与 </details> 此前在 col 3（脱出子列表项内容区）→ CommonMark 判 body 不属于该列表项
    → HTML block 在 body 行处截断 → <details> 变空壳、body 渲染成列表项外的松散段落（始终
    可见、折叠失效）。body 与 </details> 须缩进到 col 5（与 <details> 同列）留在子列表项内。

    检查 GitHub body_html 证实：修前 <details>summary 后紧跟 </details>（空壳）、body 在
    </details> 之外作为兄弟段落；修后 body 在 <details> 内。"""
    from touchstone.render import _render_reasoning
    out = _render_reasoning("依据正文内容。" * 30)            # > 200 字符，触发折叠
    lines = out.split("\n")
    # 第一行：`   - <details><summary>...`（col 3 缩进 + "- " + <details> at col 5）
    assert lines[0].startswith("   - <details>")
    # body 行（第二行）与 </details> 行（第三行）须在 col 5（5 空格），与 <details> 同列
    # 关键：修前是 3 空格（col 3）→ 脱出 `- ` 子列表项内容区（col ≥5）→ HTML block 截断、
    # body 渲染到折叠区外（GitHub body_html 实测：details 空壳 + body 作兄弟段落）
    body_indent = len(lines[1]) - len(lines[1].lstrip())
    close_indent = len(lines[2]) - len(lines[2].lstrip())
    assert body_indent >= 5, (
        f"body 缩进 {body_indent} < 5 → 脱出 `- ` 子列表项内容区 → HTML block 截断、折叠失效")
    assert close_indent >= 5 and lines[2].strip() == "</details>"


def test_ack_help_details_has_no_blank_lines_inside():
    """回归 #171：如何申报销项 <details> 内有空行（summary/body 间、body/</details> 间各一）
    → CommonMark type-6 HTML block 在首个空行处截断 → <details> 变空壳、申报指引正文渲染成
    <details> 之外的松散段落（始终可见）。去空行让整段留在同一 HTML block 内才可折叠。
    v2：申报指引迁至 render_reference（参考信息段）。"""
    from touchstone import render
    md = render.render_reference(verification_blocks=None, has_checklist_items=True)
    assert "<details>" in md and "</details>" in md
    block = md[md.index("<details>"):md.index("</details>") + len("</details>")]
    assert "\n\n" not in block, (
        "<details> 内含空行 → HTML block 截断、申报指引正文跑到折叠区外（始终可见）")



def test_reasoning_equal_to_rationale_not_rendered():
    """依据与 rationale 同文时不渲染（既有去重守卫，折叠改动不破坏此不变式）。
    v2：去重在 render_findings_checklist（合并段），经 from_findings 构造清单驱动断言。"""
    from touchstone import render
    same = "同文" * 150                                   # rationale 与 reasoning 完全相同（>200 字符）
    f = _rf("PRA-X", rationale=same, direction="方向", reasoning=same)
    body = render.render_findings_checklist([f], cl.from_findings([f]))
    assert "依据" not in body                            # 与 rationale 同文→不渲染
    assert "<details>" not in body


def test_empty_reasoning_omitted():
    """空依据不渲染（既不显式露行也不显式露 details）。
    v2：经 render_findings_checklist 驱动（合并段是依据渲染的唯一入口）。"""
    from touchstone import render
    f = _rf("PRA-X", reasoning="")
    body = render.render_findings_checklist([f], cl.from_findings([f]))
    assert "依据" not in body and "<details>" not in body


# ---------------- 借鉴 pr-agent #2510：字段去冗余 + 定位精度 ----------------
def test_finding_entry_omits_direction_when_equal_to_rationale():
    """修复方向与 rationale 同文时不复读（去字段冗余）。

    pr-agent 归一化层 normalize() 对 model 来源 finding 设 rationale=fix_direction=summary。
    v1 渲染为「标题=rationale」+「修复方向：rationale」（完全重复=纯噪声）；v2 标题即方向，
    rationale 与方向同文时子项省略——同一去冗余纪律、不同呈现（借鉴 pr-agent #2510）。"""
    from touchstone import render
    f = {"rule_id": "PRA-X", "file": "a.py", "line": 5, "rationale": "边界未处理",
         "fix_direction": "边界未处理", "fix_reasoning": "",
         "done_criteria": {"kind": "review", "spec": {"question": "?"}},
         "severity": "warn", "confidence": 0.7, "agent": "pr-agent:review"}
    body = render.render_findings_checklist([f], cl.from_findings([f]))
    assert body.count("边界未处理") == 1           # 标题里出现一次，不再复读
    assert "**边界未处理**" in body               # v2：方向作标题（加粗）


def test_finding_entry_keeps_direction_when_distinct_from_rationale():
    """确定性来源 rationale≠fix_direction → 修复方向保留（去冗余不误杀信息）。
    v2：方向作标题（加粗）、rationale 作首条子项，两者不同则都显示。"""
    from touchstone import render
    f = {"rule_id": "SCOPE-001", "file": "a.py", "line": 1, "rationale": "改动越界",
         "fix_direction": "收到 docs/ 下", "fix_reasoning": "",
         "done_criteria": {"kind": "deterministic", "spec": {"recheck": "SCOPE-001"}},
         "severity": "warn", "confidence": 1.0, "agent": "contract-check"}
    body = render.render_findings_checklist([f], cl.from_findings([f]))
    assert "**收到 docs/ 下**" in body            # 方向作标题（加粗）
    assert "改动越界" in body                      # rationale 作子项（不同→保留）


def test_finding_entry_no_colon_None_when_line_missing():
    """行号缺失时 sig 不含 `:None` 字面量（定位精度 + ack 锚点洁净）。

    pr-agent review 类偶不返回 start_line → line=None。v2 起 sig 兼作版面显示的位置串，
    故 `:None` 防护移至 sig_of 构造源（原 render._location 渲染侧兜底随 v1 移除）。line=None
    时 sig 省略行段（rule_id:file），既不渗 `:None` 进显示、又让 ack 锚点洁净可用。"""
    from touchstone import render
    f = {"rule_id": "PRA-Y", "file": "a.py", "line": None, "rationale": "问题",
         "fix_direction": "方向", "fix_reasoning": "",
         "done_criteria": {"kind": "review", "spec": {"question": "?"}},
         "severity": "warn", "confidence": 0.7, "agent": "pr-agent:review"}
    body = render.render_findings_checklist([f], cl.from_findings([f]))
    assert "a.py:None" not in body
    assert "PRA-Y:a.py" in body                    # 行号缺失→sig 省略行段，仍可作 ack 锚点


def test_normalize_line_falls_back_to_line_end():
    """line_start 缺失时 line_end 回退（定位精度，数据层兜底）。

    pr-agent review key_issues 偶带 end_line 不带 start_line；line=None 既丑（file:None）
    又让 sig 塌缩到 :None 字面量、跨轮 reconcile 全靠文件名。line_end 回退让 sig 带真实行号。"""
    raw = {"review": {"key_issues_to_review": [{
        "relevant_file": "a.py", "end_line": 42, "start_line": None,
        "issue_header": "X", "issue_content": "Y", "label": "possible issue"}]}}
    f = rp.normalize(rp.parse_pr_agent(raw))[0]
    assert f["line"] == 42                          # start 缺→用 end


def test_normalize_line_prefers_start_when_both_present():
    """line_start 与 line_end 都在→用 start（line_end 仅作 fallback，不替代）。"""
    raw = {"code_suggestions": [{
        "relevant_file": "a.py", "relevant_lines_start": 10, "relevant_lines_end": 20,
        "one_sentence_summary": "s", "label": "Possible issue"}]}
    f = rp.normalize(rp.parse_pr_agent(raw))[0]
    assert f["line"] == 10


def test_normalize_line_zero_start_not_treated_as_missing():
    """line_start=0 不被当缺失（`is not None` 而非 `or`，与 checklist.sig_of 一致）。

    GitHub diff 行号 1-based，0 罕见；但 0 是 falsy，若用 `or` 会被误判缺失→错误回退
    line_end。用 `is not None` 判定，0 作为真实行号保留。"""
    raw = {"code_suggestions": [{
        "relevant_file": "a.py", "relevant_lines_start": 0, "relevant_lines_end": 5,
        "one_sentence_summary": "s", "label": "Possible issue"}]}
    f = rp.normalize(rp.parse_pr_agent(raw))[0]
    assert f["line"] == 0                            # 0 不当缺失，不回退 end


def test_checklist_from_findings_all_open_and_dedup():
    f = _finding("R-1")
    c = cl.from_findings([f, dict(f)])          # 同签名去重
    assert len(c["items"]) == 1 and c["items"][0]["status"] == "open"
    assert c["resolved_rate"] == 0.0


def test_checklist_done_requires_recheck_pass():
    prev = cl.from_findings([_finding("R-1")])
    sig = prev["items"][0]["sig"]
    # 申报 done 但本轮仍命中 → 复核未通过，保持 open（勾选只是输入信号）
    cur = cl.reconcile(prev, {sig: {"verb": "done", "note": ""}}, [_finding("R-1")])
    assert cur["items"][0]["status"] == "open" and "复核未通过" in cur["items"][0]["note"]
    # 申报 done 且本轮不再命中 → 销项
    cur2 = cl.reconcile(prev, {sig: {"verb": "done", "note": ""}}, [])
    assert cur2["items"][0]["status"] == "done" and cur2["resolved_rate"] == 1.0


def test_checklist_waived_requires_note_split_requires_link():
    prev = cl.from_findings([_finding("R-1"), _finding("R-2", line=2)])
    s1, s2 = prev["items"][0]["sig"], prev["items"][1]["sig"]
    cur = cl.reconcile(prev, {s1: {"verb": "waived", "note": ""},
                              s2: {"verb": "split", "note": "https://x/pr/9"}},
                       [_finding("R-1"), _finding("R-2", line=2)])
    assert cur["items"][0]["status"] == "open"          # waived 无理由不受理
    assert cur["items"][1]["status"] == "split"


def test_checklist_unacked_but_fixed_resolves_and_new_findings_append():
    prev = cl.from_findings([_finding("R-1")])
    cur = cl.reconcile(prev, {}, [_finding("R-9", line=9)])
    by = {i["sig"]: i for i in cur["items"]}
    assert by[prev["items"][0]["sig"]]["status"] == "done"      # 复检不再命中即销项
    assert any(i["status"] == "open" and "R-9" in i["sig"] for i in cur["items"])


def test_checklist_ack_parse_and_render_marker_roundtrip():
    from touchstone import render
    body = "改好了\n```touchstone-ack\nR-1:a.py:1: done\nR-2:a.py:2: waived: 测试夹具\n```"
    acks = cl.parse_acks([body])
    assert acks["R-1:a.py:1"]["verb"] == "done"
    assert acks["R-2:a.py:2"] == {"verb": "waived", "note": "测试夹具"}
    c = cl.from_findings([_finding("R-1")])
    # v2：可见渲染（task list + 达成判据）在 render_findings_checklist，轮次在 render_status_line，
    # marker 在 render_marker。三处分离，各司其职。
    fc = render.render_findings_checklist([_finding("R-1")], c)
    assert "- [ ]" in fc and "达成判据" in fc
    st = render.render_status_line({"risk_band": "low", "human_action": "skip",
                                    "verification_decision": "cheap_only", "blast_radius": []},
                                   loop_info=("continue", "", ""), checklist=c, rounds_left=2)
    assert "剩余 2 轮" in st
    md = cl.render_marker(c)
    assert cl.parse_latest([md]) == c                     # marker 往返无损


def test_checklist_marker_survives_arrow_in_item_content():
    """A7-F2：清单项的 direction/reasoning/note 含字面 '-->'（评审方向提到 HTML 注释语法、
    或 author 的 waived note 带 -->）时，marker 不得被首个 '-->' 截断——parse_latest 仍无损
    取回完整清单。旧实现 body.find(_CLOSE, i) 命中内容里的 --> → JSON 截断 → json.loads 失败
    → 整条 marker 跳过（权威清单丢失 → 收敛跟踪断、resolved_rate 归零/承旧）。"""
    c = {"round": 2, "items": [
        {"sig": "R-1:a.py:1",
         "direction": "把注释 `<!-- 旧 -->` 替换为显式常量",
         "reasoning": "见 --> 标记处，魔法数应命名",
         "done_criteria": {"kind": "review", "spec": {"question": "替换了吗？"}},
         "status": "waived", "note": "author 宣称可豁免：<!-- x --> 是文档不是代码"}],
        "resolved_rate": 1.0}
    md = cl.render_marker(c)
    assert cl.parse_latest([md]) == c                     # 含 --> 仍往返无损


# ---------------- sig 归一化（闭环 PR #52 advisory 发现的换行 bug）----------------
def test_sig_of_strips_whitespace_in_file_and_line():
    # pr-agent 输出的 file/line 字段可能带尾换行/空格——sig 构造即归一化，不渗入签名
    assert cl.sig_of({"rule_id": "R", "file": "a.py\n", "line": " 12 "}) == "R:a.py:12"
    assert cl.sig_of({"rule_id": "R", "file": "a.py", "line": 1}) == "R:a.py:1"   # 正常输入不变


def test_loop_sig_normalizes_like_checklist():
    # loop._sig 委派 sig_of，保持两处同构 + 同归一化
    f = {"rule_id": "R-1", "file": "a.py\n", "line": 1}
    assert loop._sig(f) == cl.sig_of(f) == "R-1:a.py:1"


def test_remaining_rounds_decreases_across_rounds():
    # 闭环「剩余轮次永远 8」bug：orchestrator 旧实现传静态 ledger_budget−1，与当前轮无关。
    # 真实剩余须随当前轮递减：9 轮制下第 1 轮→8、第 4 轮→5、第 9 轮→0。
    assert loop.remaining_rounds(1, loop.MAX_ROUNDS) == loop.MAX_ROUNDS - 1   # 第 1 轮
    assert loop.remaining_rounds(4, loop.MAX_ROUNDS) == loop.MAX_ROUNDS - 4   # 第 4 轮（修复前恒显 8）
    assert loop.remaining_rounds(loop.MAX_ROUNDS, loop.MAX_ROUNDS) == 0       # 到顶
    assert loop.remaining_rounds(loop.MAX_ROUNDS + 3, loop.MAX_ROUNDS) == 0   # 超顶夹 0


def test_remaining_rounds_lineage_budget_binds():
    # 台账继承额度（同源历史）可硬压剩余：budget_left=2 时第 1 轮只剩 1（min(8, 1)）。
    assert loop.remaining_rounds(1, 2) == 1
    # budget_left 充裕时不绑定：第 4 轮、额度 8 → min(5, 7) = 5（与无 lineage 的 9 制一致）
    assert loop.remaining_rounds(4, 8) == loop.MAX_ROUNDS - 4
    # budget_left 耗尽 → 0
    assert loop.remaining_rounds(1, 1) == 0
    # None budget 退回自然剩余（防 ledger 缺字段）
    assert loop.remaining_rounds(3, None) == loop.MAX_ROUNDS - 3


def test_checklist_dirty_persisted_sig_matchable_by_clean_ack():
    """闭环 sig 换行 bug：旧 marker 的 sig 内嵌 \\n（pr-agent file 字段带尾换行），author 发的
    ack 是干净 sig。修复前 acks.get(item_sig) 恒 None——structurally 无法销项；修复后 reconcile
    加载时归一化 persisted sig、parse_acks 归一化 ack sig，两端命中。用 waived（仅经 ack 销项、
    不依赖 review_reliable）隔离可靠轮变量。"""
    prev = {"round": 1, "items": [{"sig": "R-1:a.py\n:1", "status": "open",
                                    "direction": "d", "reasoning": "r",
                                    "done_criteria": {"kind": "review", "spec": {"question": "q"}},
                                    "note": ""}]}
    acks = cl.parse_acks(["```touchstone-ack\nR-1:a.py:1: waived: 测试夹具\n```"])
    assert "R-1:a.py:1" in acks                                  # parse_acks 归一化出干净 key
    cur = cl.reconcile(prev, acks, [], review_reliable=False)    # 不可信轮：waived 仍应经 ack 销项
    assert cur["items"][0]["status"] == "waived"                 # 修复前：open（ack 没匹配上）


def test_checklist_dirty_sig_done_ack_records_ack_driven_close():
    """done 侧：脏 sig + 干净 done ack + 可靠轮 + 不再命中 → note 为「申报并经复核销项」（ack 命中），
    而非「复检未再命中，销项」（自动销项）。锁死 ack 确实匹配上，而非靠自动销项侥幸过。"""
    prev = {"round": 1, "items": [{"sig": "R-1:a.py\n:1", "status": "open",
                                    "direction": "d", "reasoning": "r",
                                    "done_criteria": {"kind": "review", "spec": {"question": "q"}},
                                    "note": ""}]}
    acks = cl.parse_acks(["```touchstone-ack\nR-1:a.py:1: done\n```"])
    cur = cl.reconcile(prev, acks, [], review_reliable=True)
    assert cur["items"][0]["status"] == "done"
    assert cur["items"][0]["note"] == "申报并经复核销项"          # ack 命中（修复前：自动销项的 note）


def test_detect_lineage_normalizes_inherited_dirty_sig():
    # 旧 PR 的清单 marker 带 file 尾换行的脏 sig → 台账继承时归一化（修复前原样含 \n）
    fp = {"fileset": ["a.py"], "shape": {"a.py": [10, 0]}, "fileset_hash": "x"}
    dirty_cl = {"round": 1, "resolved_rate": 0.0,
                "items": [{"sig": "R-1:a.py\n:1", "status": "open",
                                        "direction": "d", "reasoning": "r",
                                        "done_criteria": {"kind": "review", "spec": {"question": "q"}},
                                        "note": ""}]}
    bot_comment = {"user": {"login": "github-actions[bot]"},
                   "body": loop.render_marker(loop.LoopState(2, [], None)) + "\n" + cl.render_marker(dirty_cl)}
    api = _fake_api(closed_prs=[{"number": 41, "merged_at": None, "closed_at": "2026-07-01T00:00:00Z"}],
                    files_by_pr={41: [{"filename": "a.py", "additions": 10, "deletions": 0}]},
                    comments_by_pr={41: [bot_comment]})
    import datetime
    now = datetime.datetime(2026, 7, 4, tzinfo=datetime.timezone.utc)
    led = lineage.detect_lineage(fp, api, "o", "r", 42, now=now)
    assert len(led["inherited_open_items"]) == 1
    assert led["inherited_open_items"][0]["sig"] == "R-1:a.py:1"   # 归一化（修复前：R-1:a.py\n:1）


def test_checklist_no_progress_detection():
    prev = cl.from_findings([_finding("R-1")])
    same = cl.reconcile(prev, {}, [_finding("R-1")])      # 无申报且仍命中
    assert cl.no_progress(prev, same) is True
    assert cl.no_progress(None, same) is False            # 首轮不算无推进


# ---------------- 意见 1+3：loop_step 清单语义 ----------------
def test_loop_converges_only_when_checklist_resolved(rule_index):
    prev = cl.from_findings([_finding("R-1")])
    resolved = cl.reconcile(prev, {}, [])                 # 全销项
    dec, reason, _ = loop.loop_step([], rule_index, loop.LoopState(),
                                    checklist_pair=(prev, resolved))
    assert dec == "converged" and "销项" in reason
    # 清单未销项（仍命中）→ 无推进升级
    stuck = cl.reconcile(prev, {}, [_finding("R-1")])
    dec2, reason2, _ = loop.loop_step([_finding("R-1")], rule_index, loop.LoopState(),
                                      checklist_pair=(prev, stuck))
    assert dec2 == "escalate" and "无推进" in reason2


def test_loop_no_progress_not_fired_for_non_actionable_finding():
    """无推进闸只对 author 可自改的发现成立。correctness 发现不由 author ack 销项（归 verify/
    评审管），author_actionable 会将其排除（cur 为空）。此时它卡在清单不销项，不应判「无推进
    （含假修）」——那是把 author 无法着手的事归咎于 author（A1-F1 假升级）。修复前：no_progress
    只比销项计数 0<=0 即触发，不看是否还有可自改发现 → 误升级。"""
    findings = [{"rule_id": "COR-001", "category": "correctness",
                 "fix_direction": "fix the bug", "file": "a.py", "line": 1, "fix_reasoning": "r"}]
    ri = {"COR-001": {"id": "COR-001", "class": "correctness"}}
    assert loop.author_actionable(findings, ri) == []             # 前提：correctness 不可自改
    cur1 = cl.reconcile(None, {}, findings, round_no=1, review_reliable=True)
    d1, _, _ = loop.loop_step(findings, ri, loop.LoopState(0),
                              checklist_pair=(None, cur1), review_reliable=True)
    assert d1 == "continue"
    cur2 = cl.reconcile(cur1, {}, findings, round_no=2, review_reliable=True)   # 同发现再命中、无申报
    d2, reason2, _ = loop.loop_step(findings, ri, loop.LoopState(1, history=[[]]),
                                    checklist_pair=(cur1, cur2), review_reliable=True)
    assert d2 != "escalate", f"非可自改发现不应判无推进升级: {reason2!r}"       # 修复前：escalate
    assert "无推进" not in reason2 and "假修" not in reason2


def test_loop_no_progress_still_fires_for_actionable_stuck(rule_index):
    """对照：author 可自改的发现（有 fix_direction、非 correctness）卡住不销项 → 仍应判无推进
    升级。锁死 A1-F1 的守卫没有过度放宽（仅在 cur 为空时豁免）。"""
    f = _finding("R-1")
    assert loop.author_actionable([f], rule_index)                # 前提：R-1 可自改
    prev = cl.from_findings([f])
    stuck = cl.reconcile(prev, {}, [f])                           # 仍命中、无申报
    dec, reason, _ = loop.loop_step([f], rule_index, loop.LoopState(),
                                    checklist_pair=(prev, stuck))
    assert dec == "escalate" and "无推进" in reason               # 可自改发现卡住照旧升级


def test_loop_default_path_unchanged_without_checklist(rule_index):
    dec, _, st = loop.loop_step([], rule_index, loop.LoopState())
    assert dec == "converged" and st.round == 1


# ---------------- 意见 10：轮次台账 ----------------
def test_fingerprint_similarity_and_same_origin():
    a = {"fileset": ["a.py", "b.py"], "shape": {"a.py": [10, 2], "b.py": [5, 0]}}
    b = {"fileset": ["a.py", "b.py"], "shape": {"a.py": [10, 2], "b.py": [5, 0]}}
    hit, j, s = lineage.same_origin(a, b)
    assert hit and j == 1.0 and s == 1.0
    c = {"fileset": ["z.py"], "shape": {"z.py": [1, 0]}}
    assert lineage.same_origin(a, c)[0] is False
    assert lineage.fileset_jaccard([], []) == 0.0          # 空 diff 不构成同源证据


def test_recent_enough_handles_naive_datetime():
    """naive 时间戳（无 Z/偏移——非 GitHub 规范源/脏数据）与 aware 的 now 相减会抛 TypeError
    把整个 detect_lineage 带崩（A7-F3）。修复：无偏移即按 UTC 解释。语义对照：recent=True /
    old=False，与 aware(Z) 串一致；垃圾/空串仍返 False。"""
    import datetime
    now = datetime.datetime(2026, 7, 16, tzinfo=datetime.timezone.utc)
    # 修复前：下面两行任一抛 TypeError: can't subtract offset-naive and offset-aware datetimes
    assert lineage._recent_enough("2026-07-10T12:00:00", days=30, now=now) is True   # naive recent
    assert lineage._recent_enough("2026-04-16T12:00:00", days=30, now=now) is False  # naive old
    # aware(Z) 串语义不变（回归）
    assert lineage._recent_enough("2026-07-10T12:00:00Z", days=30, now=now) is True
    assert lineage._recent_enough("2026-04-16T12:00:00Z", days=30, now=now) is False
    # 垃圾/空仍 fail-closed
    assert lineage._recent_enough("not-a-date", now=now) is False
    assert lineage._recent_enough("", now=now) is False


def test_recent_enough_assumes_utc_for_naive_now():
    """对称守卫（round-2 finding PRA-POSSIBLE_ISSUE:lineage.py:79「Assume UTC for naive now」）：
    round-1 补了 naive `t` 的 UTC 解释却漏了 `now` → 调用方传 naive now 时 `now - t`
    （naive − aware）仍抛 TypeError，detect_lineage 仍可崩。修复：now 无偏移亦按 UTC 解释，
    与 t 同语义，避免「补了 t 漏 now」的半截修复。"""
    import datetime
    naive_now = datetime.datetime(2026, 7, 16, 12, 0, 0)          # 无 tzinfo
    # 修复前：now 仍 naive → (now - t) 抛 TypeError: can't subtract offset-naive and offset-aware
    assert lineage._recent_enough("2026-07-10T12:00:00", days=30, now=naive_now) is True   # recent
    assert lineage._recent_enough("2026-04-16T12:00:00", days=30, now=naive_now) is False  # old
    # aware(Z) 串 + naive now：t 经 Z→+00:00 已 aware，故 now 仍须补 UTC 才不抛
    assert lineage._recent_enough("2026-07-10T12:00:00Z", days=30, now=naive_now) is True


def test_detect_lineage_survives_naive_closed_at():
    """detect_lineage 遇到 naive 的 closed_at（A7-F3 harness 场景）不得抛 TypeError 崩整条
    台账继承——无偏移按 UTC 解释后照常判定。"""
    import datetime

    class _Api:
        def __call__(self, m, p):
            if "state=closed" in p:
                return [{"number": 99, "merged_at": None,
                         "closed_at": "2026-07-10T12:00:00",     # naive——修复前崩此处
                         "updated_at": "2026-07-10T12:00:00"}]
            return []
    fp = {"fileset": ["a.py"], "shape": {"a.py": [1, 1]}, "fileset_hash": "x"}
    # 修复前：TypeError；修复后：正常返回（不崩）
    led = lineage.detect_lineage(fp, _Api(), "o", "r", 100,
                                 now=datetime.datetime(2026, 7, 16, tzinfo=datetime.timezone.utc))
    assert isinstance(led, dict)                                  # 没崩、回了台账结构



def _fake_api(closed_prs, files_by_pr, comments_by_pr):
    def api(method, path):
        if "/pulls?" in path:
            return closed_prs
        for n, files in files_by_pr.items():
            if f"/pulls/{n}/files" in path:
                return files
        for n, cs in comments_by_pr.items():
            if f"/issues/{n}/comments" in path:
                return cs
        return []
    return api


def test_detect_lineage_inherits_rounds_and_open_items():
    fp = {"fileset": ["a.py"], "shape": {"a.py": [10, 0]}, "fileset_hash": "x"}
    old_cl = cl.from_findings([_finding("R-1")])
    bot_comment = {"user": {"login": "github-actions[bot]"},
                   "body": loop.render_marker(loop.LoopState(2, [], None)) + "\n"
                           + cl.render_marker(old_cl)}
    api = _fake_api(
        closed_prs=[{"number": 41, "merged_at": None, "closed_at": "2026-07-01T00:00:00Z"}],
        files_by_pr={41: [{"filename": "a.py", "additions": 10, "deletions": 0}]},
        comments_by_pr={41: [bot_comment]})
    import datetime
    now = datetime.datetime(2026, 7, 4, tzinfo=datetime.timezone.utc)
    led = lineage.detect_lineage(fp, api, "o", "r", 42, now=now)
    assert led["rounds_spent"] == 2 and led["rounds_left"] == loop.MAX_ROUNDS - 2
    assert led["lineage"][0]["number"] == 41
    assert len(led["inherited_open_items"]) == 1           # 历史欠账原样跟随


def test_detect_lineage_merged_pr_and_reset_label():
    fp = {"fileset": ["a.py"], "shape": {"a.py": [10, 0]}}
    api = _fake_api([{"number": 40, "merged_at": "2026-07-01T00:00:00Z",
                      "closed_at": "2026-07-01T00:00:00Z"}],
                    {40: [{"filename": "a.py", "additions": 10, "deletions": 0}]}, {})
    led = lineage.detect_lineage(fp, api, "o", "r", 42)
    assert led["lineage"] == []                            # 已合入的关闭不算刷轮次
    led2 = lineage.detect_lineage(fp, api, "o", "r", 42, current_labels=["rounds-reset"])
    assert led2["reset_by"] == "label:rounds-reset" and led2["rounds_left"] == loop.MAX_ROUNDS


def test_loop_escalates_when_ledger_exhausted(rule_index):
    led = {"rounds_spent": loop.MAX_ROUNDS, "rounds_left": 0}
    dec, reason, _ = loop.loop_step([_finding("R-1")], rule_index, loop.LoopState(), ledger=led)
    assert dec == "escalate" and "rounds-reset" in reason


def test_fake_marker_from_author_not_trusted_for_lineage():
    # author 伪造历史（虚报 0 轮）不被采信：trusted_bodies 只信 [bot] 评论
    fp = {"fileset": ["a.py"], "shape": {"a.py": [10, 0]}}
    fake = {"user": {"login": "evil-author"},
            "body": loop.render_marker(loop.LoopState(0, [], None))}
    real = {"user": {"login": "github-actions[bot]"},
            "body": loop.render_marker(loop.LoopState(3, [], None))}
    api = _fake_api([{"number": 41, "merged_at": None, "closed_at": "2026-07-01T00:00:00Z"}],
                    {41: [{"filename": "a.py", "additions": 10, "deletions": 0}]},
                    {41: [fake, real]})
    import datetime
    now = datetime.datetime(2026, 7, 4, tzinfo=datetime.timezone.utc)
    led = lineage.detect_lineage(fp, api, "o", "r", 42, now=now)
    assert led["rounds_spent"] == 3


# ---------------- 意见 4：版面模板 ----------------
def test_render_report_seven_sections_and_no_template_comment_leak():
    risk = {"risk_band": "high", "human_action": "read+arbitrate",
            "verification_decision": "targeted_tests", "blast_radius": ["security_surface"]}
    f = _finding("SEC-001", file="auth/x.py", direction="将凭据移至密钥管理")
    f.update({"severity": "block_candidate", "confidence": 1.0,
              "rationale": "疑似硬编码凭据", "agent": "contract-check"})
    diff = build_diff([("auth/x.py", ["k = 1"], True)])
    sf = cc.scope_facts(diff)
    md = orchestrator.render_report(
        risk, [f], scope_facts=sf, checklist=cl.from_findings([f]),
        loop_info=("continue", "待 author 逐项申报", ""),
        markers="<!-- touchstone-loop: {} -->")
    for token in ("AI Committer 代码检视", "敏感路径命中", "达成判据",
                  "评审发现与销项", "touchstone-loop"):
        assert token in md
    assert "版面模板" not in md                    # 模板头注释不外泄进评论
    assert "suggested_fix" not in md


def test_render_report_facts_show_parse_failure():
    risk = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
    bad = cc.scope_facts("@@@ 不是合法 diff @@@\n+++ x")
    assert bad["parse_ok"] is False
    md = orchestrator.render_report(risk, [], scope_facts=bad)
    assert "范围事实未生效" in md                  # 防静默故障传导到人可见层


def test_inherited_seed_checklist_not_judged_no_progress(rule_index):
    # 真实数据回放发现的缺陷回归：台账继承的未销项作为第 0 轮种子清单时，
    # 新 PR 第 1 轮不得被判无推进（author 尚未获得本 PR 的修改机会）。
    seed = {"round": 0, "items": cl.from_findings([_finding("SEC-001")])["items"]}
    r1 = cl.reconcile(seed, {}, [_finding("SEC-001")], round_no=1)
    assert cl.no_progress(seed, r1) is False
    dec, _, _ = loop.loop_step([_finding("SEC-001")], rule_index, loop.LoopState(),
                               checklist_pair=(seed, r1),
                               ledger={"rounds_spent": 1, "rounds_left": 2})
    assert dec == "continue"


# ---------------- review_reliable=False：抑制依赖复检的假销项 ----------------
def test_reconcile_unreliable_withholds_autoclose():
    # 仍命中没了（not still_firing）但评审不可信 -> 不自动销项，保持 open
    prev = cl.from_findings([_finding("R-1")])
    cur = cl.reconcile(prev, {}, [], review_reliable=False)   # 无申报、本轮未再命中
    it = cur["items"][0]
    assert it["status"] == "open" and "不可信" in it["note"]


def test_reconcile_reliable_autocloses_normally():
    # 对照：可靠时 not still_firing -> 自动销项（保旧行为）
    prev = cl.from_findings([_finding("R-1")])
    cur = cl.reconcile(prev, {}, [], review_reliable=True)
    assert cur["items"][0]["status"] == "done" and "复检未再命中" in cur["items"][0]["note"]


def test_reconcile_unreliable_withholds_done_ack():
    # author 申报 done + 本轮未命中，但评审不可信 -> done 不销项，待可靠轮复核
    prev = cl.from_findings([_finding("R-1")])
    sig = "R-1:a.py:1"
    cur = cl.reconcile(prev, {sig: {"verb": "done", "note": ""}}, [], review_reliable=False)
    it = cur["items"][0]
    assert it["status"] == "open" and "待可靠轮复核" in it["note"]


def test_reconcile_unreliable_still_accepts_waived():
    # waived 是人判断、不依赖 LLM 复检 -> 评审不可信时仍受理销项
    prev = cl.from_findings([_finding("R-1")])
    sig = "R-1:a.py:1"
    cur = cl.reconcile(prev, {sig: {"verb": "waived", "note": "测试夹具"}}, [],
                        review_reliable=False)
    assert cur["items"][0]["status"] == "waived"


def test_reconcile_unreliable_still_rejects_done_when_still_firing():
    # 仍命中 + done 申报 + 不可信 -> 复核未通过（与可靠时一致）
    prev = cl.from_findings([_finding("R-1")])
    sig = "R-1:a.py:1"
    cur = cl.reconcile(prev, {sig: {"verb": "done", "note": ""}}, [_finding("R-1")],
                        review_reliable=False)
    assert cur["items"][0]["status"] == "open" and "复核未通过" in cur["items"][0]["note"]


# ---------------- 假收敛守卫：已 done 项在本轮可靠复检再次命中必须重开 ----------------
def test_reconcile_done_refire_in_reliable_round_reopens():
    """上轮已 done（机器复核销项）的项，本轮可靠复检【再次命中同一签名】-> 销项未守住
    （修复回归/前轮销项过急/同一处又被 flag），必须重开为 open。修复前：done 项被
    `if status in RESOLVED: continue` 直接跳过，still_firing 永不评估 -> done 恒留 ->
    all_verified 谎报全部销项、resolved_rate 恒 100%，而该处仍被评审 flag（典型假收敛）。"""
    prev_done = cl.reconcile(cl.from_findings([_finding("R-1")]), {}, [])  # 第 1 轮：复检不再命中 -> done
    assert prev_done["items"][0]["status"] == "done"                       # 前提：确已销项
    # 第 2 轮：同一发现再次命中、评审可信、无新申报 -> 必须重开
    cur = cl.reconcile(prev_done, {}, [_finding("R-1")], round_no=2, review_reliable=True)
    it = cur["items"][0]
    assert it["status"] == "open"                                          # 重开（修复前：done）
    assert "重开" in it["note"] and "假收敛" in it["note"]
    assert cl.all_verified(cur) is False                                   # 修复前：True（假收敛）
    assert cur["resolved_rate"] == 0.0                                     # 修复前：1.0
    assert len(cur["items"]) == 1                                          # 不重复追加为 new item


def test_reconcile_done_refire_in_unreliable_round_stays_done():
    """对称守卫：不可信轮的「再次命中」不可靠（diff 被裁空/LLM 随机性），不得据此撤销销项
    冤枉 author——与「不可信轮不予销项」对称，双向都须可靠证据。done 项保持 done。"""
    prev_done = cl.reconcile(cl.from_findings([_finding("R-1")]), {}, [])
    cur = cl.reconcile(prev_done, {}, [_finding("R-1")], round_no=2, review_reliable=False)
    assert cur["items"][0]["status"] == "done"                             # 不在不可信轮撤销销项
    assert cl.all_verified(cur) is True


def test_reconcile_done_not_refiring_stays_done():
    """已 done 项本轮未再命中 -> 保持 done（不误重开）。锁死守卫只在「再次命中」时触发。"""
    prev_done = cl.reconcile(cl.from_findings([_finding("R-1")]), {}, [])
    cur = cl.reconcile(prev_done, {}, [], round_no=2, review_reliable=True)
    assert cur["items"][0]["status"] == "done" and cur["resolved_rate"] == 1.0


# ---------------- review_reliable=False：loop 不在不可信轮收敛 ----------------
def test_loop_unreliable_no_converge_round1_empty(rule_index):
    # PR #44 round-1 场景：首轮 diff 被裁空 -> 0 发现 -> 无清单项。可靠时会假收敛，
    # 不可信时兜底不收敛（回落 continue），人仍可合入。
    prev = cl.from_findings([])                       # 无历史清单项
    cur = cl.reconcile(prev, {}, [])                  # 本轮无发现
    dec, reason, _ = loop.loop_step([], rule_index, loop.LoopState(),
                                    checklist_pair=(prev, cur), review_reliable=False)
    assert dec != "converged" and "不可信" in reason


def test_loop_unreliable_no_converge_even_all_resolved(rule_index):
    # 清单经 done 全销项 + 无可自改发现，但评审不可信 -> 不收敛（原意图：不可信兜底）
    prev = cl.from_findings([_finding("R-1")])
    resolved = cl.reconcile(prev, {}, [], review_reliable=True)   # done 自动销项
    assert cl.all_verified(resolved)
    dec, reason, _ = loop.loop_step([], rule_index, loop.LoopState(),
                                    checklist_pair=(prev, resolved), review_reliable=False)
    assert dec != "converged" and "不可信" in reason


def test_loop_reliable_converges_normally(rule_index):
    # 对照：可靠时全销项 + 无可自改 -> 收敛（保旧行为）
    prev = cl.from_findings([_finding("R-1")])
    resolved = cl.reconcile(prev, {}, [], review_reliable=True)
    dec, _, _ = loop.loop_step([], rule_index, loop.LoopState(),
                               checklist_pair=(prev, resolved), review_reliable=True)
    assert dec == "converged"


# ---------------- 易读性改版：排版铁律回归（2026-07-04；v2 2026-08 六段重设计）----------------
def test_report_layout_invariants():
    """铁律（v2）：全文唯一 H2；③④⑤ 并列段一律 H3；状态行 blockquote（循环+风险合一）。
    v2 版面：七段→六段——AI 评审 + 清单合为「评审发现与销项」；验证/日志 + 申报指引折进「参考信息」。"""
    from touchstone import render, checklist as cl
    risk = {"risk_band": "mid", "human_action": "a", "verification_decision": "v",
            "blast_radius": ["x"]}
    f = {"rule_id": "R1", "severity": "warn", "confidence": 0.9, "agent": "pr-agent",
         "file": "a.py", "line": 1, "rationale": "r", "fix_direction": "d",
         "done_criteria": {"kind": "deterministic", "spec": {"recheck": "R1"}}}
    sf = {"parse_ok": True, "totals": {"files": 1, "added": 1, "deleted": 0}, "sensitive_hits": []}
    body = render.render_report(
        risk, [f], scope_facts=sf, checklist=cl.from_findings([f]),
        loop_info=("continue", "待 author 逐项申报", ""),
        verification_blocks=["📄 完整 LLM 交互日志：http://x"],
        markers="<!-- m -->", gate_line="1/1")
    lines = body.split("\n")
    h2 = [l for l in lines if l.startswith("## ")]
    h3 = [l for l in lines if l.startswith("### ") and not l.startswith("#### ")]
    assert len(h2) == 1 and "Touchstone · AI Committer 代码检视" in h2[0]  # 唯一 H2 承载品牌与定位
    # v2 六段：静态检查（③）+ 评审发现与销项（④）+ 参考信息（⑤）三段 H3 并列
    assert {l.split("（")[0] for l in h3} == {"### 静态检查", "### 评审发现与销项",
                                              "### 参考信息"}  # 并列段同级
    assert any(l.startswith("> ") for l in lines)                   # 状态行 blockquote
    assert "完整 LLM 交互日志：" in body
    assert "<details><summary>如何申报销项</summary>" in body        # 申报指引折叠（参考信息段）
    assert "风险等级：" in body and "触发因子" in body               # 状态行含风险+触发因子
    assert "| 风险等级 | 建议动作 | 验证建议 | 影响面 |" not in body    # 旧四列枚举表已移除


def test_render_report_does_not_rescan_substituted_placeholders():
    """A2-F1：render_report 此前顺序 `out = out.replace(...)` 累积替换会重扫已填入内容——若 finding
    文本含 `{{markers}}` 占位符（LLM 输出 / 对抗构造 / legitimately 讨论模板），【最后一步】的 markers
    替换会扫描整段 out、把 markers 段内容注入 finding 文本（占位符注入 / 串段）。修：单遍 re.sub 替换，
    替换文本不被重扫，占位符在内容值里保持字面。"""
    from touchstone import render, checklist as cl
    risk = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
    f = {"rule_id": "R1", "severity": "warn", "confidence": 0.9, "agent": "pr-agent",
         "file": "a.py", "line": 1,
         "rationale": "详见 {{markers}} 段",              # 故意带占位符——修复前会被展开
         "fix_direction": "d",
         "done_criteria": {"kind": "deterministic", "spec": {"recheck": "R1"}}}
    body = render.render_report(risk, [f], checklist=cl.from_findings([f]),
                                markers="<!-- ZZZ_SECRET_MARKER -->")
    # 修复前（顺序 replace）：finding 里的 {{markers}} 被最后一步 markers 替换展开成 markers 内容
    #   → "{{markers}}" 不在 body、ZZZ_SECRET_MARKER 注入 finding 文本
    # 修复后（单遍）：finding 里的 {{markers}} 保持字面、不被重扫展开
    assert "详见 {{markers}} 段" in body                  # finding 文本里的占位符保持字面（未被展开）


# ---------------- 不可信评审的呈现层接入（PR #44 教训回归）----------------
def test_unreliable_review_renders_caution_and_distrusts_action():
    """铁律：review_reliable=False 必须 [!CAUTION] 告警；状态行风险标注 LLM 不可信、不采信 skip 类建议；
    机器 marker 数据不受展示覆盖影响（由调用方原样写入，此处只验展示层不改 risk dict）。"""
    from touchstone import render
    risk = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
    sf = {"parse_ok": True, "totals": {"files": 9, "added": 171, "deleted": 13},
          "sensitive_hits": []}
    body = render.render_report(risk, [], scope_facts=sf, review_reliable=False,
                                engine_status="llm_failed", ai_raw_count=0, added_lines=171)
    assert "[!CAUTION]" in body                               # CAUTION 告警出现
    assert "本轮 AI 评审不可信" in body
    assert "需人工评审" in body                                 # 不可信时改示待人工
    assert "无需人工介入" not in body                            # skip→"无需人工介入"不该出现（误导）
    assert risk["human_action"] == "skip"                     # 只改展示，不改机器数据


def test_unreliable_suspicious_empty_names_cause():
    """engine ok 但可疑空收敛：告警必须写明行数/建议数证据，而非泛泛'可能未实质产出'。"""
    from touchstone import render
    text = render.render_unreliable_callout("ok", ai_raw_count=0, added_lines=171)
    assert "[!CAUTION]" in text and "171" in text and "0 建议" in text


def test_reliable_review_keeps_normal_layout():
    """对照：可信时无 CAUTION，建议动作照常展示。"""
    from touchstone import render
    risk = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
    body = render.render_report(risk, [], review_reliable=True)
    assert "[!CAUTION]" not in body and "无需人工介入" in body   # skip 译为"无需人工介入"


# ---------------- pr-agent 评审意见：不可信时保留非降级告警内容 ----------------
def test_render_unreliable_preserves_non_degradation_banner():
    # 不可信时 det_warning/unverified_claims 不应被 CAUTION 告警整块覆盖丢弃；
    # 循环状态归状态行（①），告警归告警段（②）——v2 分离。
    risk = {"risk_band": "high", "human_action": "read+arbitrate",
            "verification_decision": "full_suite", "blast_radius": ["security_surface"]}
    alerts = ("⚠️ **契约解析告警**\n\n"
              "🟡 **2 条 waived/split 系 author 自证、机器未验证**")
    md = orchestrator.render_report(
        risk, [], alerts=alerts, review_reliable=False,
        loop_info=("continue", "待 author 逐项申报", ""),
        engine_status="llm_failed", ai_raw_count=0, added_lines=50)
    assert "[!CAUTION]" in md                       # CAUTION 告警出现
    assert "契约解析告警" in md                      # det_warning 保留
    assert "author 自证" in md                       # unverified_claims 保留
    assert "🔁 继续" in md                           # 循环状态在状态行


def test_render_unreliable_no_banner_still_has_caution():
    # 无告警时不可信仍输出 CAUTION，不崩
    risk = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
    md = orchestrator.render_report(risk, [], review_reliable=False,
                                    engine_status="llm_failed", ai_raw_count=0, added_lines=50)
    assert "[!CAUTION]" in md


def test_loop_unreliable_no_progress_does_not_escalate(rule_index):
    # PR #47 第2轮 bug 回归保护：评审不可信轮 reconcile 会 withhold 销项->销项率不升，
    # no_progress 旧逻辑会判"无推进"误升级。但 author 可能已改、只是评审不可信无法验证。
    # 不可信轮不应因 withhold 而 escalate，回落 continue 等可靠轮再判。
    prev = cl.from_findings([_finding("R-1")], round_no=1)     # 第1轮：1条 open
    # 第2轮不可信：R-1 本轮未命中但 review_reliable=False -> withhold，保持 open
    cur = cl.reconcile(prev, {}, [], round_no=2, review_reliable=False)
    assert cl.no_progress(prev, cur) is True                   # 销项率确未提升（0->0）
    dec, reason, _ = loop.loop_step([], rule_index, loop.LoopState(round=1),
                                    checklist_pair=(prev, cur), review_reliable=False)
    assert dec != "escalate"                                   # 不可信轮不因 withhold 升级
    assert "不可信" in reason or "continue" == dec


def test_loop_reliable_no_progress_still_escalates(rule_index):
    # 对照：可信轮 no_progress 仍 escalate（抓 author 只发评论不改代码的假修）
    prev = cl.from_findings([_finding("R-1")], round_no=1)
    cur = cl.reconcile(prev, {}, [_finding("R-1")], round_no=2)  # 仍命中、无申报、可信
    dec, _, _ = loop.loop_step([_finding("R-1")], rule_index, loop.LoopState(round=1),
                               checklist_pair=(prev, cur), review_reliable=True)
    assert dec == "escalate"


# ---------------- v2：状态行（循环+风险合一）的陈述行 + 触发因子纪律 ----------------
def test_situation_block_is_prose_not_table():
    """v2 状态行：标签+人话陈述（非表格）；枚举译中文；verification_decision 不入状态行（机器信号）。"""
    from touchstone import render
    risk = {"risk_band": "high", "human_action": "read+arbitrate",
            "verification_decision": "targeted_tests",
            "blast_radius": ["cross_module_contract", "security_surface"]}
    st = render.render_status_line(risk)
    assert "风险等级：高" in st and "需人工评审后合入" in st
    assert "触发因子：" in st and "跨模块契约变更" in st and "涉及安全面" in st
    assert "|" not in st                                     # 不再是表格
    assert "targeted_tests" not in st                        # 验证档不入状态行（移至参考信息）
    assert "read+arbitrate" not in st                        # 枚举名不外露


def test_situation_block_omits_trigger_line_when_no_factors():
    """去冗余：无触发因子（blast_radius 空）时状态行不显「触发因子：无」。有因子时照常显。"""
    from touchstone import render
    risk_none = {"risk_band": "low", "human_action": "skip",
                 "verification_decision": "cheap_only", "blast_radius": []}
    st = render.render_status_line(risk_none)
    assert "触发因子" not in st                       # 无因子 → 省行（不再显「触发因子：无」）
    assert "风险等级：低" in st
    # 对照：有因子 → 照常显
    st2 = render.render_status_line(dict(risk_none, blast_radius=["security_surface"]))
    assert "触发因子" in st2 and "涉及安全面" in st2


def test_facts_omit_sensitive_path_line_when_no_hit():
    """v2（观测意见 7）：修改范围删除（与 PR UI 统计重复）；无敏感路径命中时整行省略（不再写
    「敏感路径命中：无」）。简单 PR（无命中 + 无门禁）→ 整段静态检查省略（零噪声）。"""
    from touchstone import render
    sf_no_hit = {"parse_ok": True,
                 "totals": {"files": 1, "added": 2, "deleted": 0},
                 "sensitive_hits": []}
    facts = render.render_facts_v2(sf_no_hit)
    assert facts == ""                                   # 无敏感路径 + 无门禁 → 整段省略
    assert "修改范围" not in facts                       # 修改范围删除（与 PR UI 重复）
    assert "敏感路径命中：无" not in facts                # 旧措辞彻底消失
    # 对照：有命中 → 照常显（含规则名），仍无修改范围
    sf_hit = {"parse_ok": True, "totals": {"files": 1, "added": 1, "deleted": 0},
              "sensitive_hits": [{"rule": "SEC-PATH", "path": "auth/k.py"}]}
    facts_hit = render.render_facts_v2(sf_hit)
    assert "敏感路径命中（SEC-PATH）：`auth/k.py`" in facts_hit
    assert "修改范围" not in facts_hit                   # v2：修改范围在所有情形下都不显


def test_status_line_merges_loop_and_risk_into_one_line():
    """v2（观测意见 6）：循环决策 + 风险等级合成状态行同一 blockquote 行——替代旧版两行。
    断言：同一 `>` 行内同时含循环 emoji 与风险等级。"""
    from touchstone import render
    risk = {"risk_band": "mid", "human_action": "read",
            "verification_decision": "cheap_only", "blast_radius": []}
    body = render.render_report(risk, [], loop_info=("continue", "待 author 逐项申报", ""))
    lines = body.split("\n")
    status = [l for l in lines if l.startswith("> ") and "风险等级" in l]
    assert len(status) == 1                                # 唯一状态行
    assert "🔁 继续" in status[0] and "风险等级：中" in status[0]   # 循环+风险在同一行


def test_caution_stands_apart_from_status_line_when_unreliable():
    """v2 边界：不可信时 [!CAUTION] 在告警段（②），状态行（①，含循环+风险）在其前——
    CAUTION 不吞掉状态行（避免过度渲染），两段各自独立 blockquote。"""
    from touchstone import render
    risk = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
    md = render.render_report(
        risk, [], review_reliable=False,
        loop_info=("continue", "待 author 逐项申报", ""),
        engine_status="llm_failed", ai_raw_count=0, added_lines=50)
    lines = md.split("\n")
    caution_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "> [!CAUTION]")
    status_idx = next(i for i, ln in enumerate(lines) if "风险等级" in ln and ln.startswith("> "))
    assert status_idx < caution_idx                       # 状态行（①）在 CAUTION（②）前
    # CAUTION 块（连续 > 行）里不含风险等级——状态行已承载，不重复
    caution_lines = []
    for ln in lines[caution_idx:]:
        if not ln.startswith(">"):
            break
        caution_lines.append(ln)
    assert "风险等级" not in "\n".join(caution_lines)


def test_checklist_render_hides_ack_help_when_empty():
    """去冗余：空清单（0 发现即收敛，如 PR #70）无项可申报——「如何申报销项」折叠省去。
    v2：申报指引由 render_reference 据 has_checklist_items 开关决定显隐。marker 永远在。"""
    from touchstone import render
    # 空清单 → 无申报指引
    ref_empty = render.render_reference(has_checklist_items=False)
    assert "如何申报销项" not in ref_empty             # 空清单 → 省折叠
    # 对照：有项 → 照常显申报指引
    ref_has = render.render_reference(has_checklist_items=True)
    assert "如何申报销项" in ref_has
    # marker 永远在（机读状态不丢）
    assert "touchstone-checklist" in cl.render_marker({"round": 1, "items": [], "resolved_rate": 1.0})


def test_findings_checklist_merges_rule_and_ai_sources():
    """v2（观测意见 4）：确定性规则命中 + LLM 建议合为一段「评审发现与销项」——不再按来源
    拆成「静态检查·规则命中」+「AI 评审」两段。两类发现都在同一 - [ ] task list，由 agent
    元数据小字区分来源。"""
    from touchstone import render, checklist as cl
    findings = [
        {"rule_id": "DANGER-001", "severity": "error", "confidence": 1.0, "agent": "contract",
         "file": "a.py", "line": 1, "rationale": "r", "fix_direction": "d",
         "done_criteria": {"kind": "deterministic", "spec": {"recheck": "DANGER-001"}}},
        {"rule_id": "PRA-X", "severity": "warn", "confidence": 0.7, "agent": "pr-agent",
         "file": "b.py", "line": 2, "rationale": "r", "fix_direction": "d",
         "done_criteria": {"kind": "review", "spec": {"question": "q？"}}},
    ]
    c = cl.from_findings(findings)
    md = render.render_findings_checklist(findings, c)
    assert "### 评审发现与销项" in md                      # 合为一段（不再分「静态检查·规则命中」+「AI 评审」）
    assert "a.py:1" in md and "b.py:2" in md              # 两类发现同段
    assert "需人工复核：q？" in md                         # LLM 发现的达成判据
    assert "`DANGER-001`" in md and "`PRA-X`" in md       # agent 元数据小字区分来源


def test_findings_checklist_merges_direction_status_and_sig():
    """v2（观测意见 2+3+4）：方向当标题、sig 兼位置+锚点（不再单列位置/锚点行）；状态措辞统一；
    无「销项跟踪：…见上方」样板（详情已在同段）。"""
    from touchstone import render
    c = {"round": 3, "resolved_rate": 0.33, "items": [
        {"sig": "R@a.py:1", "status": "open", "direction": "收紧正则", "reasoning": "",
         "done_criteria": {"kind": "deterministic", "spec": {"recheck": "R"}}, "note": ""},
        {"sig": "R@b.py:2", "status": "done", "direction": "加 try/except", "reasoning": "",
         "done_criteria": {"kind": "review", "spec": {"question": "q？"}}, "note": "复核通过"},
        {"sig": "R@c.py:3", "status": "waived", "direction": "后补", "reasoning": "",
         "done_criteria": {}, "note": "下个 PR"}]}
    md = render.render_findings_checklist([], c)
    assert "**收紧正则**" in md                             # 方向当标题
    assert "`R@a.py:1`" in md                              # sig 在 checkbox 标题行（兼位置+锚点）
    assert "⬜ 待处理" in md and "✅ 已复核销项" in md       # 状态措辞统一
    assert "🟡 待人核准（author 豁免）" in md
    assert "销项跟踪：" not in md                          # 样板删除（观测意见 3）
    assert "位置：`" not in md                             # 不再单列位置行（sig 已兼）
    assert "锚点 `" not in md                              # 不再单列锚点行


def test_findings_checklist_resolved_item_without_direction_not_pending():
    """已销项项无方向时不显「（待补修复方向）」（PRA-REVIEW round-3 data-loss）。
    「待补」暗示待办，但已销项（done/waived/split）无需再补——方向是历史快照、本就可能未留存。
    open 项无方向仍显「待补」（提示 author 补）；已销项项改显「（已销项）」。"""
    from touchstone import render
    c = {"round": 2, "resolved_rate": 0.5, "items": [
        {"sig": "R@a.py:1", "status": "open", "direction": "", "reasoning": "",
         "done_criteria": {}, "note": ""},
        {"sig": "R@b.py:2", "status": "done", "direction": "", "reasoning": "",
         "done_criteria": {}, "note": "复核通过"}]}
    md = render.render_findings_checklist([], c)
    # open 项无方向 → 待补（提示补方向）
    assert "（待补修复方向）" in md
    # 已销项项无方向 → 不显「待补」（改显「已销项」，不误导）
    assert md.count("（待补修复方向）") == 1                  # 只有 open 项那一处
    assert "（已销项）" in md                                 # done 项用此占位


def test_status_line_resolved_rate_never_exceeds_100():
    """销项率兜底：异常大值也不溢出（护栏）。v2：销项率在状态行（render_status_line）。
    用非空 items 驱动（空清单不显示销项率——见 test_status_line_omits_rate_for_empty_checklist）。"""
    from touchstone import render
    st = render.render_status_line(
        {"risk_band": "low", "human_action": "skip", "verification_decision": "cheap_only", "blast_radius": []},
        checklist={"round": 1, "resolved_rate": 33, "items": [{"sig": "R:0", "status": "open"}]})  # 误传 33
    assert "销项率 100%" in st                              # min(100,...) 兜底


def test_status_line_omits_rate_for_empty_checklist():
    """空清单（items=[]）不显示销项率——_rate([])=1.0，若显示则「销项率 100%」对零项清单
    是噪声，与 v2 去冗余目标矛盾（PRA-GENERAL round-1）。真值守卫（非 `is not None`）：空列表
    是 falsy，跳过销项率行。"""
    from touchstone import render
    st = render.render_status_line(
        {"risk_band": "low", "human_action": "skip", "blast_radius": []},
        checklist={"round": 1, "resolved_rate": 1.0, "items": []})   # 空清单（_rate 空列表=1.0）
    assert "销项率" not in st


# ---------------- 变异体回归锁：waived 收敛门的轮次耗尽边界 ----------------
def test_loop_waived_claims_escalate_exactly_at_max_rounds(rule_index):
    """存活变异体回归锁（loop.py `nr >= max_rounds`）。

    场景：清单表面全销项，但唯一销项是 author 自证的 waived（未经机器核准）、
    且本轮发现清零。此时是否 escalate 严格取决于轮次是否耗尽：
      - nr == max_rounds       → escalate（轮次已耗尽，交人裁决）
      - nr == max_rounds - 1   → continue（还有一轮，点名待核准项）
    区分这两者的正是 `>=` 与 `>` 的边界：把 `>=` 变异成 `>` 会让恰好 == max_rounds
    的这一轮误判为 continue，等于凭空多给一轮、延后人工介入。此前无测试打到该
    边界，变异体存活；本测试锁死之。
    """
    prev = cl.from_findings([_finding("R-1")])
    sig = prev["items"][0]["sig"]
    # author 用 waived 单方申报销项（带理由方受理），机器未验证
    cur = cl.reconcile(prev, {sig: {"verb": "waived", "note": "测试夹具，非真实凭据"}}, [])
    assert cl.all_resolved(cur) and not cl.all_verified(cur)
    assert cl.has_unverified_claims(cur)

    mr = 9
    # nr == max_rounds：state.round = mr-1 → loop 内 nr = round+1 = mr
    dec_at, reason_at, _ = loop.loop_step(
        [], rule_index, loop.LoopState(round=mr - 1),
        max_rounds=mr, checklist_pair=(prev, cur))
    assert dec_at == "escalate", "轮次耗尽(nr==max_rounds)时 waived 未核准应 escalate 交人"
    assert "轮次耗尽" in reason_at

    # nr == max_rounds - 1：仍应 continue（尚有一轮）——守住边界另一侧
    dec_before, _, _ = loop.loop_step(
        [], rule_index, loop.LoopState(round=mr - 2),
        max_rounds=mr, checklist_pair=(prev, cur))
    assert dec_before == "continue", "未耗尽(nr==max_rounds-1)时应 continue 点名待核准项"

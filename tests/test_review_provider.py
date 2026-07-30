"""PR-Agent 评审路径骨架（评审提供器 + 归一 + 裁决映射）。纯函数、离线；
PR-Agent 真实端点为接缝，测试经 pr_ctx['pr_agent_output'] 注入原始输出。"""
import json

import pytest

from touchstone import review_provider as RP


# 一份贴近 PR-Agent improve+review 输出的样例
_RAW = {
    "code_suggestions": [
        {"relevant_file": "src/auth.py", "relevant_lines_start": 12, "relevant_lines_end": 14,
         "one_sentence_summary": "Validate token before use", "improved_code": "if not token: raise ...",
         "label": "security"},
        {"relevant_file": "src/util.py", "relevant_lines_start": 30, "relevant_lines_end": 30,
         "one_sentence_summary": "Rename variable for clarity", "improved_code": "user_count = ...",
         "label": "maintainability"},
        {"relevant_file": "src/calc.py", "relevant_lines_start": 7, "relevant_lines_end": 9,
         "one_sentence_summary": "Off-by-one in loop bound", "improved_code": "range(n+1)",
         "label": "possible bug"},
    ],
    "review": {
        "key_issues_to_review": [
            {"relevant_file": "src/calc.py", "start_line": 7, "end_line": 9,
             "issue_header": "Edge case", "issue_content": "n=0 not handled", "label": "possible issue"},
        ],
    },
}


# ---------------- 解析 ----------------
def test_parse_pr_agent_suggestions_and_review():
    items = RP.parse_pr_agent(_RAW)
    assert len(items) == 4
    sug = [i for i in items if i["kind"] == "suggestion"]
    rev = [i for i in items if i["kind"] == "review"]
    assert len(sug) == 3 and len(rev) == 1
    assert sug[0]["file"] == "src/auth.py" and sug[0]["line_start"] == 12 and sug[0]["label"] == "security"
    assert rev[0]["summary"] == "Edge case" and rev[0]["tool"] == "review"


def test_parse_empty_is_empty():
    assert RP.parse_pr_agent({}) == []
    assert RP.parse_pr_agent(None) == []


def test_parse_pr_agent_strips_trailing_newlines_from_fields():
    """pr-agent 返回的 relevant_file/summary/reason 带尾换行 → 显示污染（`file\\n:line` 换行、
    逐条发现子项间空行、判据「方向\\n」断行，PR #59 真实样例肉眼可见）。parse_pr_agent 在清源
    处 strip 首尾空白，两路（code_suggestions / key_issues）都覆盖。"""
    raw = {"code_suggestions": [{
        "relevant_file": "src/auth.py\n",
        "relevant_lines_start": 12,
        "one_sentence_summary": "Token 未校验即用\n",
        "suggestion_content": "if not token: raise\n",
    }]}
    items = RP.parse_pr_agent(raw)
    assert items[0]["file"] == "src/auth.py"            # 尾 \n 已剥
    assert items[0]["summary"] == "Token 未校验即用"
    assert items[0]["reason"] == "if not token: raise"
    # key_issues 路径同样剥
    raw2 = {"review": {"key_issues_to_review": [{
        "relevant_file": "src/util.py\n", "start_line": 30,
        "issue_header": "边界 off-by-one\n", "issue_content": "循环越界\n"}]}}
    it2 = RP.parse_pr_agent(raw2)[0]
    assert it2["file"] == "src/util.py" and it2["summary"] == "边界 off-by-one"
    # 贯通到 findings：file:line 不再换行、判据 question 不带尾 \n
    f = RP.normalize(items)[0]
    assert f["file"] == "src/auth.py" and "\n" not in f["file"]
    q = f["done_criteria"]["spec"]["question"]
    assert q.endswith("是否已按方向解决？")              # direction 尾 \n 已剥 → 不断行
    assert "\n" not in q


# ---------------- 归一 ----------------
def test_normalize_maps_label_to_category_and_agent():
    findings = RP.normalize(RP.parse_pr_agent(_RAW))
    cats = {f["file"]: f["category"] for f in findings}
    assert cats["src/auth.py"] == "security"
    assert cats["src/util.py"] == "convention"        # maintainability → convention
    # possible bug → correctness；possible issue → correctness_suspect（弱信号、不升 high）
    calc_cats = {f["category"] for f in findings if f["file"] == "src/calc.py"}
    assert calc_cats == {"correctness", "correctness_suspect"}
    # 顾问式：一律 warn，不产 block_candidate；agent 记来源
    assert all(f["severity"] == "warn" for f in findings)
    assert all(f["agent"].startswith("pr-agent:") for f in findings)
    assert all(f["rule_id"].startswith("PRA-") for f in findings)


def test_normalize_respects_discard():
    nmap = dict(RP._DEFAULT_NMAP, discard_labels=["maintainability"])
    findings = RP.normalize(RP.parse_pr_agent(_RAW), nmap)
    assert "src/util.py" not in {f["file"] for f in findings}   # 被丢弃
    assert len(findings) == 3


def test_normalize_unknown_label_falls_to_default_category():
    items = [{"kind": "suggestion", "file": "x.py", "line_start": 1, "label": "wat", "summary": "?"}]
    f = RP.normalize(items)[0]
    assert f["category"] == "convention"               # default_category


def test_normalize_label_case_insensitive():
    """标签→类别映射大小写无关——回归锁。

    pr-agent 的 label schema 明确「也接受其它相关标签」，LLM 会发 'Security'/'Possible Bug'
    等大写形式；nmap 键全小写，旧实现 l2c.get(label) 直接拿原始大小写查 → 大写标签落
    default_category='convention'：安全发现被降级、永不到 high（风险误路由）。删任一 .lower()
    即红（变异杀红）。"""
    items = [
        {"kind": "suggestion", "file": "a.py", "line_start": 1, "label": "Security", "summary": "s"},
        {"kind": "suggestion", "file": "b.py", "line_start": 2, "label": "Possible Bug", "summary": "s"},
        {"kind": "suggestion", "file": "c.py", "line_start": 3, "label": "security", "summary": "s"},  # 小写回归
    ]
    cats = [f["category"] for f in RP.normalize(items)]
    assert cats[0] == "security", f"大写 'Security' 须映射 security 非 convention（got {cats[0]!r}）"
    assert cats[1] == "correctness", f"'Possible Bug' 须映射 correctness（got {cats[1]!r}）"
    assert cats[2] == "security", f"小写 'security' 回归仍须 security（got {cats[2]!r}）"
    # 大写安全发现经 case-insensitive 映射后须真能升到 high（端到端证风险路由不再被 casing 误降）
    _, risk = RP.map_verdict(RP.normalize([items[0]]))
    assert risk["risk_band"] == "high"


def test_normalize_non_string_label_falls_to_default_not_crash():
    """输入侧与键侧同样防御：非字符串 label（上游解析出的数字等）直接 .lower() 会 AttributeError
    崩在 normalize 里。应像键侧 str(k).lower() 一样 str() 归一后落 default_category。"""
    # 数字 label（truthy、非 str）：旧实现 (7).lower() -> AttributeError
    out = RP.normalize([{"kind": "suggestion", "file": "a.py", "line_start": 1, "label": 7, "summary": "s"}])
    assert out[0]["category"] == "convention"            # 落默认，不崩
    # None label 同样安全
    out = RP.normalize([{"kind": "suggestion", "file": "a.py", "line_start": 1, "label": None, "summary": "s"}])
    assert out[0]["category"] == "convention"


def test_normalize_preserves_falsy_label_zero_not_swallowed():
    """round-2 finding PRA-GENERAL:review_provider.py:836「Avoid silently dropping falsy label values」：
    `str(label or "")` 会吞掉 falsy 但有效的 label——`0 or ""` 得 ""，于是 label=0 与缺失不可区分，
    rule_id 退回 kind 兜底（PRA-SUGGESTION）而非 PRA-0，与本 PR「防御非字符串 label」自相矛盾
    （上测试盖了 7，0 亦须正解）。修：只把 None 当空，0→"0" 经 str() 保留。"""
    out = RP.normalize([{"kind": "suggestion", "file": "a.py", "line_start": 1, "label": 0, "summary": "s"}])
    assert out[0]["category"] == "convention"            # 0 不在 nmap → 默认（语义正确）
    assert out[0]["rule_id"] == "PRA-0"                  # 0 被保留成 "0"，未被 or "" 吞成空→kind 兜底


def test_default_label_maps_precomputed_once():
    """round-3 finding PRA-GENERAL:review_provider.py:821「Precompute normalized label map once」：
    默认 nmap（模块级、不可变）的大小写归一+冲突校验原本每轮 normalize() 重算——冗余。改为 import 时
    预计算一次（_DEFAULT_L2C/_DEFAULT_DISCARD），既让坏默认配置在 import 即 fail-fast，又让默认路径
    per-call 成本只与 items 成比例。自定义 nmap 仍每次重建（不沿用预计算、不能假设传入 dict 不可变）。"""
    import pytest
    import touchstone.review_provider as rp
    # 预计算结果存在且正确（键全小写、大写键已归一命中）
    assert rp._DEFAULT_L2C, "默认标签映射应在 import 时预计算（非空）"
    assert rp._DEFAULT_L2C["security"] == "security"
    assert rp._DEFAULT_L2C["organization best practice"] == "convention"   # 大写键归一为小写
    assert rp._DEFAULT_DISCARD == set()                                    # discard_labels 默认空
    # 预计算 == 逐次重建（行为不变，只是不每轮重算）
    assert (rp._DEFAULT_L2C, rp._DEFAULT_DISCARD) == rp._build_label_maps(rp._DEFAULT_NMAP)
    # 自定义 nmap（有冲突）仍每次重建并 fail-loud——不沿用预计算
    bad = {"label_to_category": {"Security": "security", "security": "convention"}}
    with pytest.raises(ValueError, match="大小写归一后键冲突"):
        rp.normalize([{"label": "x"}], nmap=bad)


def test_normalize_raises_on_case_collision_in_nmap():
    """大小写归一把仅大小写不同的键合并；若映射到【不同】类别，后者静默覆盖前者（配置笔误把发现
    路由到错误类别=防静默故障）。对真冲突 fail-loud；同类别冗余键无害不报。默认 nmap 无冲突。"""
    import pytest
    bad = {"label_to_category": {"Security": "security", "security": "convention"},
           "default_category": "convention"}
    with pytest.raises(ValueError, match="大小写归一后键冲突"):
        RP.normalize([{"label": "x"}], nmap=bad)
    # 同类别冗余键无害（不报、保留首个）
    ok = {"label_to_category": {"Security": "security", "security": "security"},
          "default_category": "convention"}
    out = RP.normalize([{"label": "security", "summary": "s", "kind": "suggestion",
                         "file": "a", "line_start": 1}], nmap=ok)
    assert out[0]["category"] == "security"


# ---------------- 裁决映射 ----------------
def test_map_verdict_security_is_high():
    _, risk = RP.map_verdict(RP.normalize(RP.parse_pr_agent(_RAW)))
    assert risk["risk_band"] == "high"                 # 含 security/correctness
    assert "security_surface" in risk["blast_radius"]
    assert risk["verification_decision"] == "full_suite"   # 高 + 影响面严重(security_surface) → 最强一档


def test_map_verdict_only_convention_is_mid():
    items = [{"kind": "suggestion", "file": "a.py", "line_start": 1, "label": "typo", "summary": "x"}]
    _, risk = RP.map_verdict(RP.normalize(items))
    assert risk["risk_band"] == "mid" and risk["verification_decision"] == "cheap_only"


def test_map_verdict_empty_is_low():
    _, risk = RP.map_verdict([])
    assert risk["risk_band"] == "low" and risk["human_action"] == "skip"


def test_map_verdict_confidence_floor_filters():
    findings = [{"category": "security", "confidence": 0.2}]   # 低于 conf_min=0.5
    kept, risk = RP.map_verdict(findings)
    assert kept == [] and risk["risk_band"] == "low"


def test_map_verdict_possible_issue_alone_is_mid():
    # 弱信号 correctness_suspect 单独出现 → mid（不升 high）；这是 possible issue 调映射后的目标行为
    items = [{"kind": "review", "file": "a.py", "line_start": 1, "label": "possible issue", "summary": "maybe"}]
    findings = RP.normalize(items)
    assert findings[0]["category"] == "correctness_suspect"
    _, risk = RP.map_verdict(findings)
    assert risk["risk_band"] == "mid" and risk["verification_decision"] == "cheap_only"


def test_map_verdict_possible_bug_still_high():
    # 真缺陷信号照常升 high（只软化 possible issue，不动 possible bug/critical bug）
    items = [{"kind": "suggestion", "file": "a.py", "line_start": 1, "label": "possible bug", "summary": "off by one"}]
    _, risk = RP.map_verdict(RP.normalize(items))
    assert risk["risk_band"] == "high"


def test_map_verdict_high_categories_configurable():
    # high_categories 可配：把 correctness_suspect 纳入 → possible issue 也升 high
    nmap = dict(RP._DEFAULT_NMAP, high_categories=["security", "correctness", "correctness_suspect"])
    items = [{"kind": "review", "file": "a.py", "line_start": 1, "label": "possible issue", "summary": "m"}]
    _, risk = RP.map_verdict(RP.normalize(items, nmap), nmap)
    assert risk["risk_band"] == "high"


def test_map_verdict_contract_category_path():
    """contract 类发现 → blast 含 cross_module_contract。
    注意：contract 不在默认 high_categories → band=mid → cheap_only（当前行为）；
    高风险升级需配 high_categories 纳入 contract，或改 route() 逻辑。此处锁定现状。"""
    findings = [{"category": "contract", "confidence": 0.9, "rule_id": "CTR-001",
                 "agent": "touchstone-rules", "severity": "block_candidate"}]
    _, risk = RP.map_verdict(findings)
    assert "cross_module_contract" in risk["blast_radius"]
    assert risk["risk_band"] == "mid"          # 当前：contract 不在 high_categories
    assert risk["verification_decision"] == "cheap_only"   # 因 band != high


# ---------------- 评审提供器 fetch（注入 vs 子进程集成）----------------
def test_fetch_with_injected_output():
    items = RP.fetch({"pr_agent_output": _RAW})
    assert len(items) == 4


def test_build_pr_url_from_owner_repo_number():
    assert RP._build_pr_url({"owner": "o", "repo": "r", "number": 7}).endswith("/o/r/pull/7")
    assert RP._build_pr_url({"owner": "o", "repo": "r"}) == ""        # 缺 number → 空


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_invoke_endpoint_subprocess_success(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return _Proc(0, out=json.dumps(_RAW))
    monkeypatch.setattr(RP.subprocess, "run", fake_run)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")     # 隔离学习回路
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert len(items) == 4                                            # 子进程 JSON → 解析成 ReviewItem
    assert "--pr-url" in captured["args"] and "--mode" in captured["args"]


def test_invoke_endpoint_nonzero_raises(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(2, err="boom-detail"))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RuntimeError, match="boom-detail"):
        RP.fetch({"owner": "o", "repo": "r", "number": 3})


def test_invoke_endpoint_bad_json_raises(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out="not json"))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RuntimeError, match="JSON"):
        RP.fetch({"owner": "o", "repo": "r", "number": 3})


def test_invoke_endpoint_missing_runner_raises(monkeypatch):
    def boom(a, **k):
        raise FileNotFoundError("no such cmd")
    monkeypatch.setattr(RP.subprocess, "run", boom)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RuntimeError, match="pip install pr-agent"):
        RP.fetch({"owner": "o", "repo": "r", "number": 3})


def test_invoke_endpoint_no_pr_url_raises(monkeypatch):
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RuntimeError, match="PR URL"):
        RP.fetch({"sha": "s"})                                       # 无 pr_url / owner-repo-number


def test_invoke_endpoint_degraded_json_raises_typed(monkeypatch):
    # 适配器结构化降级：子进程退出 0 但 JSON 带 _degraded → 抛 ReviewEngineDegraded（带 degraded/reason）
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k:
                        _Proc(0, out=json.dumps({"_degraded": "llm_failed", "reason": "AuthError: 401"})))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"
    assert "401" in ei.value.reason


def test_engine_banner():
    from touchstone import orchestrator as orc
    assert "AI 评审未运行" in orc._engine_banner("no_engine")
    assert "LLM 调用失败" in orc._engine_banner("llm_failed")
    assert orc._engine_banner("ok") == ""


def test_review_pr_engine_status_on_degradation(monkeypatch):
    # fetch 抛 ReviewEngineDegraded → review_pr 仍返回确定性核对结果，但 engine_status 标降级
    from touchstone import orchestrator as orc

    def _degrade(pr, provider=None):
        raise RP.ReviewEngineDegraded("no_engine", "pr-agent 未安装")
    monkeypatch.setattr(RP, "fetch", _degrade)
    out = orc.review_pr({"diff": ""}, {}, {})
    assert out["engine_status"] == "no_engine"
    assert out["findings"] == []                       # 降级：无评审发现，仅确定性核对（空 diff→空）


def test_parse_diff_malformed_sets_warning():
    # C：unidiff 解析失败 → 返回空 + 置 _PARSE_WARNING（供 orchestrator 显式标注，防静默）
    from touchstone import contract_check as cc
    cc._PARSE_WARNING = None
    # hunk 内出现非法行首（=badline）→ unidiff 抛 UnidiffParseError
    files, added = cc.parse_diff("--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n=badline\n")
    assert files == set() and added == {}
    assert cc._PARSE_WARNING and "解析失败" in cc._PARSE_WARNING
    # 正常 diff 应回到无告警
    files, added = cc.parse_diff("--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+pass\n")
    assert cc._PARSE_WARNING is None


def test_review_pr_det_warning_on_bad_diff(monkeypatch):
    # C：坏 diff → review_pr 返回 det_warning；确定性核对空转不再被读成"干净"
    from touchstone import orchestrator as orc
    monkeypatch.setattr(RP, "fetch", lambda ctx, provider=None: [])
    out = orc.review_pr({"diff": "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n=badline\n"}, {}, {})
    assert out["det_warning"]                           # 非空告警
    assert out["findings"] == []                        # 坏 diff → 确定性核对 0 发现（但已显式标注）


def test_engine_banner_combines_det_warning():
    # post_results 把 det_warning 也拼进 banner——这里直接验 _engine_banner 与拼接逻辑的存在
    from touchstone import orchestrator as orc
    assert "AI 评审未运行" in orc._engine_banner("no_engine")
    assert orc._engine_banner("ok") == ""


def test_clean_review_trace_disambiguates_zero_findings():
    from touchstone import orchestrator as orc
    # 引擎正常 + 改动小 + 0 原始建议 → 合理（非空回）
    t = orc._clean_review_trace("ok", ai_raw_count=0, added_lines=3, n_changed=1)
    assert "已端到端运行" in t and "0 条原始建议" in t and "合理" in t
    # 引擎正常 + 改动大 + 0 原始建议 → 可疑，提示人工扫一眼
    t2 = orc._clean_review_trace("ok", ai_raw_count=0, added_lines=120, n_changed=8)
    assert "人工扫一眼" in t2
    # 引擎正常 + pr-agent 真有返回 → 不可疑（归一后 0 是被过滤）
    t3 = orc._clean_review_trace("ok", ai_raw_count=5, added_lines=120, n_changed=8)
    assert "5 条原始建议" in t3 and "人工扫一眼" not in t3
    # 降级时不输出溯源（由 _engine_banner 负责）
    assert orc._clean_review_trace("llm_failed", 0, 0, 0) == ""


def test_run_link_from_actions_env(monkeypatch):
    # 评审评论里贴的"完整 LLM 交互日志"链接，由 Actions env 构造；非 Actions 环境为空
    from touchstone import orchestrator as orc
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    assert orc._run_link() == "https://github.com/o/r/actions/runs/12345"
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert orc._run_link() == ""


def test_runner_imports_without_pr_agent():
    # 适配器模块本身可被导入、不在导入期触碰 pr-agent（pr-agent 只在 run() 内 import）
    from touchstone import pr_agent_runner as R
    assert R._read(None) is None
    assert callable(R.run) and callable(R.main)


def test_fetch_unknown_provider():
    with pytest.raises(ValueError):
        RP.fetch({"pr_agent_output": _RAW}, provider="nope")


# ============ 确定性影响面（不依赖 LLM 类别）·安全兜底回归 ============
def test_deterministic_blast_by_path():
    from touchstone import review_provider as rp
    assert "cross_module_contract" in rp.deterministic_blast(["db/migrations/0007_add.sql"])
    assert "cross_module_contract" in rp.deterministic_blast(["api/user.proto"])
    assert "security_surface" in rp.deterministic_blast(["svc/auth/login.py"])
    assert rp.deterministic_blast(["svc/pay/charge.py", "README.md"]) == []   # 普通路径不误报


def test_llm_missed_category_still_elevated_by_path():
    """评审侧【漏判】类别（category 不含 security/contract）时，
    改动却触及 migration/安全面 → 确定性 blast 仍把它抬到 high → full_suite。"""
    from touchstone import review_provider as rp
    findings = [{"rule_id": "PRA-STYLE", "category": "style", "severity": "warn", "confidence": 0.9}]
    # 不给 changed_files：沿用旧行为（低风险）
    _, risk0 = rp.map_verdict(list(findings))
    assert risk0["risk_band"] != "high"
    # 给出触及 schema 迁移的改动文件：即便 LLM 只报了 style，也被抬到 high + full_suite
    _, risk1 = rp.map_verdict(list(findings), changed_files=["db/migrations/0007_add.sql"])
    assert risk1["risk_band"] == "high"
    assert "cross_module_contract" in risk1["blast_radius"]
    assert risk1["verification_decision"] == "full_suite"


def test_to_rdjson_reviewdog_backend():
    """rdjson 导出：行内评论可交 reviewdog（成熟锚定后端），severity 正确映射。"""
    from touchstone import review_provider as rp
    d = rp.to_rdjson([{"rule_id": "SCOPE-001", "file": "m.sql", "line": 1,
                       "severity": "block_candidate", "rationale": "超出 scope"}])
    diag = d["diagnostics"][0]
    assert d["source"]["name"] == "touchstone"
    assert diag["severity"] == "ERROR" and diag["location"]["path"] == "m.sql"
    assert diag["code"]["value"] == "SCOPE-001"


def test_to_rdjson_shape():
    rd = RP.to_rdjson([{"rule_id": "SCOPE-001", "file": "a.sql", "line": 3,
                       "severity": "block_candidate", "rationale": "超出 scope"},
                      {"rule_id": "PRA-STYLE", "file": "b.py", "severity": "warn"}])
    d = rd["diagnostics"]
    assert rd["source"]["name"] == "touchstone" and len(d) == 2
    assert d[0]["severity"] == "ERROR" and d[0]["location"]["range"]["start"]["line"] == 3
    assert d[1]["severity"] == "WARNING" and d[1]["location"]["range"]["start"]["line"] == 1


def test_injection_disabled_switch(monkeypatch):
    from touchstone import review_provider as rp
    monkeypatch.setenv('TOUCHSTONE_EXPERIENCE_ENABLED', 'false')
    assert rp._experience_injection('.') == ''

def test_injection_skipped_in_pr_without_trusted_ref(monkeypatch, tmp_path):
    """PR 事件且未配受信 ref → 即便经验库真实存在也不注入（非空转验证：防投毒 fail-safe）。"""
    import importlib
    from touchstone import learning_loop
    from touchstone import review_provider as rp
    store = tmp_path / "exp.json"
    store.write_text('{"experiences": [{"id": "e:::T", "finding_type": "T", "kind": "emphasize",'
                     '"text": "FLAG-T", "status": "active", "updated_at": 1}]}', encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_STORE_PATH", str(store))
    from touchstone import experience_store
    importlib.reload(experience_store)   # STORE_PATH 在 experience_store 导入期求值
    importlib.reload(learning_loop)      # 门面再导出随后同步
    try:
        monkeypatch.setenv("TOUCHSTONE_EXPERIENCE_ENABLED", "true")
        monkeypatch.delenv("TOUCHSTONE_EXPERIENCE_REF", raising=False)
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        assert rp._experience_injection(".") == ""              # PR 无受信 ref：拒注入
        monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
        assert "FLAG-T" in rp._experience_injection(".")        # 非 PR：正常注入（证明非空转）
    finally:
        monkeypatch.delenv("TOUCHSTONE_STORE_PATH", raising=False)
        importlib.reload(experience_store); importlib.reload(learning_loop)


def test_experience_injection_passes_include_shadow_when_enabled(monkeypatch, tmp_path):
    """step4：TOUCHSTONE_SHADOW_INJECTION 开 → _experience_injection 透传 include_shadow=True，
    注入文本含 shadow 段（candidate 标灰 [shadow]）；默认关 → 只 active 段、无 shadow。"""
    import importlib
    from touchstone import experience_store, learning_loop
    from touchstone import review_provider as rp
    store = tmp_path / "exp.json"
    store.write_text(json.dumps({"experiences": [
        {"id": "e:::A", "finding_type": "A", "kind": "emphasize",
         "text": "FLAG-A", "status": "active", "updated_at": 1},
        {"id": "e:::S", "finding_type": "S", "kind": "emphasize",
         "text": "FLAG-S", "status": "candidate", "updated_at": 1, "source_prs": [101]},
    ]}), encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_STORE_PATH", str(store))
    importlib.reload(experience_store); importlib.reload(learning_loop)
    try:
        monkeypatch.setenv("TOUCHSTONE_EXPERIENCE_ENABLED", "true")
        monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")        # 非 PR：绕 EXPERIENCE_REF 闸
        monkeypatch.setenv("TOUCHSTONE_SHADOW_RATIO", "1.0")       # 全入选（免哈希不确定性）
        monkeypatch.setenv("TOUCHSTONE_SHADOW_MIN_EVIDENCE", "1")
        # 默认关 → 只 active 段
        monkeypatch.delenv("TOUCHSTONE_SHADOW_INJECTION", raising=False)
        out_off = rp._experience_injection(".")
        assert "FLAG-A" in out_off and "[shadow]" not in out_off
        assert "Shadow candidates" not in out_off
        # 开 → active + shadow 段（candidate 标灰）
        monkeypatch.setenv("TOUCHSTONE_SHADOW_INJECTION", "1")
        out_on = rp._experience_injection(".")
        assert "FLAG-A" in out_on and "FLAG-S" in out_on
        assert "Shadow candidates" in out_on and "[shadow]" in out_on
    finally:
        for k in ("TOUCHSTONE_STORE_PATH", "TOUCHSTONE_SHADOW_INJECTION",
                  "TOUCHSTONE_SHADOW_RATIO", "TOUCHSTONE_SHADOW_MIN_EVIDENCE"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(experience_store); importlib.reload(learning_loop)


def test_shadow_does_not_bypass_experience_ref_gate(monkeypatch, tmp_path):
    """铁律 5：shadow 开也不绕 EXPERIENCE_REF 防投毒闸——PR 事件 + 无受信 ref → 整体返回 ""
    （含 shadow candidate，证明 candidate 与 active 同走受信 ref，非空转）。"""
    import importlib
    from touchstone import experience_store, learning_loop
    from touchstone import review_provider as rp
    store = tmp_path / "exp.json"
    store.write_text(json.dumps({"experiences": [
        {"id": "e:::S", "finding_type": "S", "kind": "emphasize",
         "text": "FLAG-S", "status": "candidate", "updated_at": 1, "source_prs": [101]},
    ]}), encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_STORE_PATH", str(store))
    importlib.reload(experience_store); importlib.reload(learning_loop)
    try:
        monkeypatch.setenv("TOUCHSTONE_EXPERIENCE_ENABLED", "true")
        monkeypatch.setenv("TOUCHSTONE_SHADOW_INJECTION", "1")     # shadow 开
        monkeypatch.delenv("TOUCHSTONE_EXPERIENCE_REF", raising=False)
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")    # PR + 无 ref → 必拒
        assert rp._experience_injection(".") == ""                 # shadow 不绕闸
    finally:
        for k in ("TOUCHSTONE_STORE_PATH", "TOUCHSTONE_SHADOW_INJECTION"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("TOUCHSTONE_EXPERIENCE_REF", raising=False)
        importlib.reload(experience_store); importlib.reload(learning_loop)


def test_shadow_detection_failure_does_not_disable_active_injection(monkeypatch, tmp_path, capsys):
    """pr-agent #118 r1+r2：_shadow_injection_enabled() 抛异常 → 降级 include_shadow=False（只禁 shadow 段），
    不级联进外层 except 禁用整个经验注入——active 经验仍正常渲染（不返 ""）；且降级非静默（r2）——
    stderr 打 [warn] 含函数名/异常原因，运维可见（CLAUDE.md §3 诚实标 gap）。"""
    import importlib
    from touchstone import experience_store, learning_loop
    from touchstone import review_provider as rp
    store = tmp_path / "exp.json"
    store.write_text(json.dumps({"experiences": [
        {"id": "e:::A", "finding_type": "A", "kind": "emphasize",
         "text": "FLAG-A", "status": "active", "updated_at": 1},
    ]}), encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_STORE_PATH", str(store))
    importlib.reload(experience_store); importlib.reload(learning_loop)

    def _boom():
        raise RuntimeError("shadow detection exploded")
    try:
        monkeypatch.setenv("TOUCHSTONE_EXPERIENCE_ENABLED", "true")
        monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")        # 非 PR：绕 EXPERIENCE_REF 闸
        monkeypatch.setattr(learning_loop, "_shadow_injection_enabled", _boom)
        out = rp._experience_injection(".")
        assert "FLAG-A" in out               # active 注入未被禁（不级联返 ""）
        assert "[shadow]" not in out          # shadow 降级关
        # r2：降级非静默——stderr 打 [warn]（print(stderr) 走 capsys，非 logging）。锁定诊断不被静默移除。
        err = capsys.readouterr().err
        assert "[warn]" in err and "_shadow_injection_enabled" in err and "shadow detection exploded" in err
    finally:
        monkeypatch.delenv("TOUCHSTONE_STORE_PATH", raising=False)
        importlib.reload(experience_store); importlib.reload(learning_loop)


# ---------------- review_reliable：引擎健康度判据 ----------------
def test_review_reliable_ok_with_suggestions():
    assert RP.review_reliable("ok", ai_raw_count=3, added_lines=500) is True


def test_review_reliable_ok_small_diff_zero_suggestions():
    # 改动小（<阈值）且 0 建议 -> 合理，可靠
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=5) is True


def test_review_reliable_suspicious_empty_large_diff_zero_suggestions():
    # 改动不小（>=阈值）却 0 原始建议 -> 可疑空收敛（PR #44 真根因：diff 被裁空）-> 不可靠
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=25) is False
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=20) is False  # 边界


def test_review_reliable_engine_degraded():
    # 引擎降级各态 -> 不可靠（0 建议是缺审，非审完无问题）
    for st in ("no_engine", "provider_failed", "llm_failed", "skipped_large_diff"):
        assert RP.review_reliable(st, ai_raw_count=0, added_lines=5) is False


# ---------------- review_reliable engaged 逃生口（PR #51：审完无问题 ≠ 裁空/吞没）----------------
def test_review_reliable_engaged_clean_is_reliable():
    # glm 审完无问题：engine ok、改动不小、0 建议、但 engaged（多段实质性评审）-> 可靠。
    # 这是 PR #51 的真场景：干净 PR 被旧逻辑误判可疑空收敛。
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=61, engaged=True) is True
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=20, engaged=True) is True  # 边界


def test_review_reliable_not_engaged_large_still_suspicious():
    # 未 engaged（_rv 近乎空，如 diff 被裁空）+ 大改动 + 0 建议 -> 仍可疑（guard 不削弱）
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=61, engaged=False) is False


def test_review_reliable_engaged_default_false_preserves_old_behavior():
    # 不传 engaged（老调用/autonomy）-> 默认 False -> 大改动 0 建议仍可疑（向后兼容）
    assert RP.review_reliable("ok", ai_raw_count=0, added_lines=61) is False


def test_review_reliable_engaged_does_not_rescue_degraded():
    # 引擎降级时 engaged 无效——降级优先，0 建议是缺审不是审完
    assert RP.review_reliable("llm_failed", ai_raw_count=0, added_lines=61, engaged=True) is False
    assert RP.review_reliable("no_engine", ai_raw_count=0, added_lines=61, engaged=True) is False


def test_review_reliable_engaged_irrelevant_when_findings_present():
    # 有原始建议时本就可靠，engaged 无关
    assert RP.review_reliable("ok", ai_raw_count=2, added_lines=61, engaged=False) is True


def test_extract_engaged_reads_runner_signal():
    # runner 在 review 段写 _engaged（子进程路径）；无 _engaged 键时退化到 compute_engaged 现算（注入路径）。
    assert RP._extract_engaged({"review": {"_engaged": True, "key_issues_to_review": []}}) is True
    assert RP._extract_engaged({"review": {"_engaged": False}}) is False
    # 无 _engaged 键 -> 现算：仅空 key_issues、无其他段 -> 0 段 -> False
    assert RP._extract_engaged({"review": {"key_issues_to_review": []}}) is False
    assert RP._extract_engaged({"code_suggestions": []}) is False                  # 无 review 段
    assert RP._extract_engaged(None) is False                                      # 非 dict


def test_extract_engaged_truthy_nondict_review_is_false():
    # 闭环 PR #52 advisory（PRA-POSSIBLE_ISSUE）：review 为 truthy 非 dict（malformed/legacy
    # 字符串/列表/数）时，`data.get("review") or {}` 短路返回该非 dict 值，旧实现 .get("_engaged")
    # 会抛 AttributeError。守卫后须安全落到 False，且不抛。
    assert RP._extract_engaged({"review": "some string"}) is False
    assert RP._extract_engaged({"review": ["a", "b"]}) is False
    assert RP._extract_engaged({"review": 42}) is False
    # 正常 dict 仍工作（回归保护）
    assert RP._extract_engaged({"review": {"_engaged": True}}) is True


def test_compute_engaged_counts_nonempty_sections_excluding_key_issues():
    # 单一真源：compute_engaged 数 review 段里【排除 key_issues_to_review】后 >=2 个非空段。
    # pr_agent_runner 经 lazy import 复用本函数（防子进程内/外两套 engaged 逻辑漂移）。
    assert RP.compute_engaged({"review": {  # 3 段非空 -> True
        "estimated_effort_to_review": "2", "relevant_tests": "Yes", "security_concerns": "No",
        "key_issues_to_review": []}}) is True
    assert RP.compute_engaged({"review": {  # key_issues 非空但被排除，仅 1 段 -> False
        "estimated_effort_to_review": "2", "key_issues_to_review": [{"x": 1}]}}) is False
    assert RP.compute_engaged({"review": {"key_issues_to_review": []}}) is False  # 无其他段
    assert RP.compute_engaged({"review": {  # 空值段不计（""/None/[]/{}）
        "estimated_effort_to_review": "", "security_concerns": None, "relevant_tests": []}}) is False
    assert RP.compute_engaged({"review": "x"}) is False          # review 非 dict
    assert RP.compute_engaged({"code_suggestions": []}) is False  # 无 review 段
    assert RP.compute_engaged(None) is False                      # 非 dict


def test_extract_engaged_falls_back_to_compute_when_no_flag():
    # 离线注入路径（无 runner、无 _engaged 标志）——_extract_engaged 退化到 compute_engaged 现算，
    # 使端到端注入测试里 engaged 信号能流转（修复前：注入评审恒 engaged=False 的盲区）。
    assert RP._extract_engaged({"review": {  # 无 _engaged 键但多段非空 -> 现算 True
        "estimated_effort_to_review": "2", "relevant_tests": "Yes"}}) is True
    # 有 _engaged 键时优先读标志（不被现算覆盖）——子进程路径行为不变
    assert RP._extract_engaged({"review": {"_engaged": False,
        "estimated_effort_to_review": "2", "relevant_tests": "Yes"}}) is False


# ---------------- extract_review_excerpt：0 原始建议时贴 LLM 原始段打消"是否真审过"疑虑（PR #55）-----
def test_extract_review_excerpt_picks_nonempty_segments_excluding_key_issues():
    # 单一真源：抽 review 段里【非空】结构段，排除 key_issues_to_review（即"0 意见"本体）与内部标志。
    d = {"review": {
        "estimated_effort_to_review": "3",
        "relevant_tests": "Yes",
        "key_issues_to_review": [],          # 排除
        "security_concerns": "No",
        "_engaged": True,                     # 排除（内部标志）
        "_raw_excerpt": {},                   # 排除（内部标志）
        "empty_seg": "",                      # 排除（空值）
    }}
    assert RP.extract_review_excerpt(d) == {
        "estimated_effort_to_review": "3", "relevant_tests": "Yes", "security_concerns": "No"}
    assert RP.extract_review_excerpt({"review": {"key_issues_to_review": []}}) == {}   # 无非空段
    assert RP.extract_review_excerpt({"code_suggestions": []}) == {}                   # 无 review 段
    assert RP.extract_review_excerpt(None) == {}                                       # 非 dict


def test_extract_review_excerpt_truncates_and_singlelines():
    # 多行段值（如 security_concerns 段落）单行化 + 截断，快照可读且不撑爆横幅。
    long = "line1\nline2\nline3 " + "x" * 200
    ex = RP.extract_review_excerpt({"review": {"security_concerns": long}})
    assert ex["security_concerns"].endswith("…")
    assert len(ex["security_concerns"]) <= 161            # 截断后 <= max_chars(160) + 省略号
    assert "\n" not in ex["security_concerns"]            # 单行化
    # max_segments 封顶（保序取前 N 段）
    big = {f"seg_{i}": str(i) for i in range(20)}
    assert len(RP.extract_review_excerpt({"review": big}, max_segments=3)) == 3


def test_extract_review_excerpt_escapes_backticks_for_markdown_safety():
    # PR #57 评审意见：v 是 LLM 生成文本，security_concerns 等段会以反引号引用代码标识符（如 `eval()`）；
    # 奇数个反引号会让 _clean_review_trace 横幅里 `段名`: 值 的 inline-code span 失衡，腐蚀整段评论渲染。
    # 单一真源处把反引号归一化为单引号 → 快照 markdown-safe，所有消费方安全。
    d = {"review": {"security_concerns": "uses `eval()` for dynamic dispatch",
                    "code_feedback": "even count `a` plus `b` is fine but still normalized"}}
    ex = RP.extract_review_excerpt(d)
    assert "`" not in ex["security_concerns"]          # 反引号已归一化（奇数个 = 真正会失衡的场景）
    assert ex["security_concerns"] == "uses 'eval()' for dynamic dispatch"
    assert "`" not in ex["code_feedback"]              # 偶数个也一律归一化（统一契约，不数个数）
    # 渲染进横幅模板后，inline-code span 始终成对（值内再无反引号打开新 span）
    rendered = "\n".join(f"- `{k}`: {v}" for k, v in ex.items())
    assert rendered.count("`") % 2 == 0                # 每段恰好一对 `{k}` → 总数必为偶


def test_extract_excerpt_prefers_runner_flag_then_falls_back():
    # 镜像 _extract_engaged：子进程路径读 runner 写的 review._raw_excerpt；缺失（注入/老协议）则现算。
    flagged = {"review": {"_raw_excerpt": {"a": "1"}, "estimated_effort_to_review": "2"}}
    assert RP._extract_excerpt(flagged) == {"a": "1"}               # 标志优先，不被现算覆盖
    no_flag = {"review": {"estimated_effort_to_review": "2", "relevant_tests": "Yes"}}
    assert RP._extract_excerpt(no_flag) == {"estimated_effort_to_review": "2",
                                            "relevant_tests": "Yes"}  # 无标志 -> 现算
    assert RP._extract_excerpt({"review": {"_raw_excerpt": "not a dict"}}) == {}   # 非 dict 标志 -> {}
    assert RP._extract_excerpt(None) == {}                                         # 非 dict


def test_fetch_sets_raw_review_excerpt_meta_on_injection():
    # PRAgentProvider.fetch 出口统一设 raw_review_excerpt（覆盖注入+子进程两路径，镜像 review_engaged）。
    data = {"review": {"estimated_effort_to_review": "3", "relevant_tests": "Yes",
                       "key_issues_to_review": []}}
    items = RP.fetch({"pr_agent_output": data})
    assert items == []                                    # 无 key_issues/code_suggestions
    assert RP.invoke_meta()["raw_review_excerpt"] == {
        "estimated_effort_to_review": "3", "relevant_tests": "Yes"}


def test_clean_review_trace_appends_llm_excerpt_when_zero_raw():
    from touchstone import orchestrator as orc
    excerpt = {"estimated_effort_to_review": "3", "relevant_tests": "Yes", "security_concerns": "No"}
    # 0 原始建议（无实质意见）→ 贴 LLM 原始 review 段，打消"是否真审过"疑虑
    t = orc._clean_review_trace("ok", ai_raw_count=0, added_lines=120, n_changed=8,
                                raw_excerpt=excerpt)
    assert "LLM 原始评审" in t
    assert "`estimated_effort_to_review`: 3" in t
    assert "key_issues / code_suggestions 均空" in t
    # 有原始建议（ai_raw_count>0）→ 不贴 excerpt（"返回 N 条原始建议"已足）
    assert "LLM 原始评审" not in orc._clean_review_trace(
        "ok", ai_raw_count=5, added_lines=120, n_changed=8, raw_excerpt=excerpt)
    # excerpt 空 → 无内容可贴，不输出该块
    assert "LLM 原始评审" not in orc._clean_review_trace(
        "ok", ai_raw_count=0, added_lines=3, n_changed=1, raw_excerpt={})
    # 降级 → 溯源整体不输出（由 _engine_banner 负责）
    assert orc._clean_review_trace("llm_failed", 0, 0, 0, raw_excerpt=excerpt) == ""


def test_clean_review_trace_no_scope_dedup_and_line_broken():
    """横幅精简（B）+ 去冗余（C）：_clean_review_trace 不再含与「确定性事实」段重复的
    「改动：N 文件」统计行；改拆行（非全角空格连写的一长句）。溯源实质（head/detail/tail）保留。"""
    from touchstone import orchestrator as orc
    t = orc._clean_review_trace("ok", ai_raw_count=0, added_lines=120, n_changed=8)
    assert "已端到端运行" in t and "0 条原始建议" in t and "人工扫一眼" in t   # 溯源实质保留
    assert "改动：" not in t                             # 去掉与事实区重复的统计行（去冗余）
    assert "　" not in t                                 # 拆行，非全角空格连写


# ---------------- prediction_swallowed_failure：pr-agent 吞掉的 LLM 失败检测 ----------------
def test_prediction_swallowed_failure_empty_with_sig():
    # round-3 / #46 真场景：失败串在 stderr + 本轮 0 原始建议 -> 吞没失败
    data = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    stderr = "...WARNING Failed to generate prediction with openai/glm-5.2\nERROR Failed to generate...with any model"
    assert RP.prediction_swallowed_failure(data, stderr) is True


def test_prediction_swallowed_failure_with_real_issues_not_swallowed():
    # round-2 场景：improve 工具失败（stderr 有串）但 review 成功给了 key_issues -> 不算吞没
    data = {"code_suggestions": [], "review": {"key_issues_to_review": [{"relevant_file": "x.py"}]}}
    assert RP.prediction_swallowed_failure(data, "Failed to generate prediction") is False


def test_prediction_swallowed_failure_clean_success():
    # 正常成功：无失败串 -> 不算吞没
    data = {"code_suggestions": [{"a": 1}], "review": {"key_issues_to_review": []}}
    assert RP.prediction_swallowed_failure(data, "all good, no errors") is False


def test_invoke_endpoint_swallowed_failure_raises_llm_failed(monkeypatch):
    # pr-agent 退出码 0、无 _degraded、空 findings、stderr 含失败串 -> 抛 ReviewEngineDegraded("llm_failed")
    # （此前的 _degraded 字段检查漏过这种吞没；本检测是 review_reliable 主判据的来源）
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(
        0, out=json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}}),
        err="...Failed to generate prediction with openai/glm-5.2\nFailed to generate...any model"))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"
    assert "Failed to generate prediction" in ei.value.reason


def test_invoke_endpoint_partial_success_not_degraded(monkeypatch):
    # improve 工具失败（stderr 有串）但 review 给了 key_issues -> 不降级（仍拿到真实评审）
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(
        0, out=json.dumps({"code_suggestions": [],
                           "review": {"key_issues_to_review": [{"relevant_file": "x.py"}]}}),
        err="Failed to generate prediction with openai/glm-5.2"))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert len(items) == 1                                    # review 的 1 条意见照常返回


# ---------------- 子进程 stdout 噪音容错（litellm/pr-agent 延迟 print 污染；PR #49 no_engine 真根因）----------------
# runner（pr_agent_runner._emit_json）用 _JSON_BEGIN/_JSON_END 哨兵包裹 JSON；父进程 _extract_json
# 按哨兵提取、无哨兵则 raw_decode 兜底。模拟各种 litellm async 延迟 print 场景，确保不误判 no_engine。
def test_invoke_endpoint_json_with_trailing_litellm_noise(monkeypatch):
    # 〔本次 bug〕哨兵包裹的 JSON 后跟 litellm "Logging Details LiteLLM-Async Success Call"
    # （async 回调晚于 runner fd 级 dup2 重定向恢复才落盘）→ 父进程按哨兵提取，不 no_engine。
    noisy = (RP._JSON_BEGIN + json.dumps(_RAW) + RP._JSON_END +
             "\nLogging Details LiteLLM-Async Success Call, cache_hit=None")
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out=noisy))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 4


def test_invoke_endpoint_json_with_leading_noise(monkeypatch):
    # litellm 噪音在哨兵之前（回调早于 main 的 _emit_json）也能按哨兵提取。
    noisy = ("LiteLLM.Info: Give Feedback ...\n" + RP._JSON_BEGIN +
             json.dumps(_RAW) + RP._JSON_END)
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out=noisy))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 4


def test_invoke_endpoint_json_with_noise_both_sides(monkeypatch):
    noisy = ("preamble\n" + RP._JSON_BEGIN + json.dumps(_RAW) + RP._JSON_END + "\ntrailing")
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out=noisy))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 4


def test_invoke_endpoint_sentinel_absent_raw_decode_fallback(monkeypatch):
    # 无哨兵（老协议 runner / 哨兵缺失）：JSON 后跟噪音 → raw_decode 取首个对象兜底，
    # 不再 "Extra data" 崩成 no_engine（这正是 PR #49 修复前的失败模式）。
    noisy = json.dumps(_RAW) + "\nLogging Details LiteLLM-Async Success Call"
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out=noisy))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 4


def test_extract_json_unit():
    # 纯函数直接测 _extract_json 四分支：哨兵提取 / raw_decode 兜底 / 纯噪音抛 / 非 dict 抛。
    payload = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    raw = json.dumps(payload)
    assert RP._extract_json(RP._JSON_BEGIN + raw + RP._JSON_END + "trailing") == payload
    assert RP._extract_json("leading" + RP._JSON_BEGIN + raw + RP._JSON_END) == payload
    assert RP._extract_json(raw + " trailing noise") == payload          # raw_decode 兜底
    with pytest.raises(json.JSONDecodeError):
        RP._extract_json("not json at all")                              # 纯噪音无 JSON → 抛
    # 合法 JSON 但非 dict（int/list/null/str——如 litellm 噪音恰以数字或 '[' 开头）：非合法评审负载 → 抛，
    # 否则旧实现返回非 dict → _invoke_endpoint 当成功数据 → parse 空 → 假 engine_status=ok。
    for nondict in ("123", "[1, 2, 3]", "null", "\"str\""):
        with pytest.raises(json.JSONDecodeError):
            RP._extract_json(nondict)
    # 哨兵内包非 dict 同样抛（runner 本应总出 dict；这是契约守卫，非现实路径）
    with pytest.raises(json.JSONDecodeError):
        RP._extract_json(RP._JSON_BEGIN + "123" + RP._JSON_END)


def test_invoke_endpoint_nondict_json_raises_not_fake_ok(monkeypatch):
    # stdout 是合法 JSON 但【非 dict】（哨兵缺失的老/自定义 runner + litellm 噪音以数字/'[' 开头）。
    # 回归锁：旧 raw_decode 兜底返回 int/list → 不抛 JSONDecodeError → _invoke_endpoint 当成功数据返回
    # → parse_pr_agent 空 → 假 engine_status=ok。修复后：非 dict 抛 → ReviewEngineDegraded("no_engine")。
    for nondict in ("123", "[1, 2, 3]", "null", "\"oops\""):
        monkeypatch.setattr(RP.subprocess, "run", lambda a, _out=nondict, **kw: _Proc(0, out=_out))
        monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
        with pytest.raises(RP.ReviewEngineDegraded) as ei:
            RP.fetch({"owner": "o", "repo": "r", "number": 3})
        assert ei.value.degraded == "no_engine"


def test_swallowed_failure_ignores_litellm_success_log():
    # 〔举一反三·stderr 侧〕litellm 正常成功日志不含 _PRED_FAILURE_SIGS；即便 findings 空
    # （glm 真审了认为没问题）也不应误判吞没。锁死：成功日志不命中失败签名。
    data = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    assert RP.prediction_swallowed_failure(data, "Logging Details LiteLLM-Async Success Call") is False


# ---------------- 检测器盲区回归（对 pr-agent 0.37 源码核实的分层失败串）----------------
def test_swallowed_failure_detects_review_parse_layer():
    """review 工具的空 content 失败发生在 retry 圈外的解析层，stderr 不含
    'Failed to generate prediction'——旧单签名检测器在此漏检（小 PR 上
    added_lines 启发式也兜不住）。信号集合必须逐个能触发。"""
    from touchstone import review_provider as rp
    empty = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    for sig in ("Failed to parse AI prediction after fallbacks",
                "Failed to parse review data",
                "Failed to review PR: argument of type 'NoneType' is not iterable",
                "Failed to generate prediction with openai/glm-5.2"):
        assert rp.prediction_swallowed_failure(empty, f"WARNING ... {sig} ...") is True, sig


def test_swallowed_failure_still_requires_zero_output():
    """有真实评审产出时，即便 stderr 含失败串（如仅 improve 挂了），不算吞没。"""
    from touchstone import review_provider as rp
    data = {"code_suggestions": [], "review": {"key_issues_to_review": [{"issue_header": "x"}]}}
    assert rp.prediction_swallowed_failure(
        data, "Failed to parse review data") is False


def test_swallowed_failure_clean_stderr_negative():
    from touchstone import review_provider as rp
    empty = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    assert rp.prediction_swallowed_failure(empty, "all good, 0 suggestions") is False


# ---------------- 静默故障系统排查（2026-07-09）：部分降级与截断修复可见化 ----------------
def test_partial_tool_failure_improve_down_review_up():
    """S1：improve 挂、review 正常——整轮仍可信（不触发降级），但必须归因可见。"""
    from touchstone import review_provider as rp
    data = {"code_suggestions": [],
            "review": {"key_issues_to_review": [{"issue_header": "x"}]}}
    assert rp.partial_tool_failure(
        data, "ERROR Failed to generate code suggestions for PR, error: ...") == "improve"
    assert rp.partial_tool_failure(
        data, "[runner] improve produced no data（run() 内部已吞异常）") == "improve"


def test_partial_tool_failure_review_down_improve_up():
    from touchstone import review_provider as rp
    data = {"code_suggestions": [{"suggestion_content": "x"}],
            "review": {"key_issues_to_review": []}}
    assert rp.partial_tool_failure(data, "[runner] review prediction malformed（...）") == "review"
    assert rp.partial_tool_failure(data, "Failed to review PR: boom") == "review"


def test_partial_tool_failure_negative_cases():
    from touchstone import review_provider as rp
    both = {"code_suggestions": [{"s": 1}], "review": {"key_issues_to_review": [{"i": 1}]}}
    none_ = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    assert rp.partial_tool_failure(both, "Failed to review PR:") is None   # 两侧都有产出
    assert rp.partial_tool_failure(none_, "clean") is None                 # 无失败串
    # 两侧全空 + 失败串 -> 归 swallowed（整轮不可信），不算部分降级
    assert rp.partial_tool_failure(none_, "Failed to review PR:") is None


def test_runner_markers_included_in_swallowed_sigs():
    """runner 外化的三个工具级标记必须能触发整轮吞没检测（两侧全空时）。"""
    from touchstone import review_provider as rp
    empty = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    for sig in ("[runner] improve produced no data",
                "[runner] review produced empty prediction",
                "[runner] review prediction malformed"):
        assert rp.prediction_swallowed_failure(empty, sig) is True, sig


def test_invoke_meta_repaired_parses_counted(monkeypatch):
    """S3：截断/畸形被 try_fix_yaml 修复的次数经 meta 通道透出。
    fan-out 下 improve/review 各自独立解析；repaired_parse 信号只出现在【出问题的那一侧】子进程 stderr
    里（此处 improve 的 code_suggestions 解析被修了 2 次，review 侧干净）。mock 按工具分流，合并后
    计数=2（验证不是双子进程翻倍成 4）——锁住"合并 stderr 的计数=各子进程真实总和"。"""
    import json as _j
    import subprocess
    from touchstone import review_provider as rp
    imp_out = _j.dumps({"code_suggestions": [{"suggestion_content": "x", "relevant_file": "a.py",
                                              "language": "python", "existing_code": "",
                                              "improved_code": "", "one_sentence_summary": "s",
                                              "label": "possible bug"}],
                        "review": {"key_issues_to_review": []}})
    rev_out = _j.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}})
    err = ("WARNING Initial failure to parse AI prediction: bad yaml\n"
           "WARNING Initial failure to parse AI prediction: bad yaml again\n")

    def fake_run(args, **k):
        mode = args[args.index("--mode") + 1] if "--mode" in args else "improve+review"
        if mode == "improve":
            return subprocess.CompletedProcess(args, 0, stdout=imp_out, stderr=err)
        return subprocess.CompletedProcess(args, 0, stdout=rev_out, stderr="")
    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    items = rp.fetch({"pr_url": "https://github.com/o/r/pull/1"})
    assert items and rp.invoke_meta()["repaired_parses"] == 2
    assert rp.invoke_meta()["partial_tool_failure"] is None


# ---------------- caution 信息具体化（2026-07-14）：llm_failed 给具体原因，不贴误导性 stderr 尾部 ----------------
# 真场景：improve 流式超时挂、review 正常返回——stderr 尾部是 review 的 success 日志，真因
# （improve 的 `litellm.Timeout ... time taken=1189s`）被截在前面。caution 必须领头给具体原因。
def test_summarize_llm_failure_timeout_evidence():
    """PR#65 真场景：improve 超时，litellm 自记 timeout value=600 实测 time taken=1189s。
    caution 必须抽出这条（含"超时没在 600s 生效"铁证），而非埋进 success 日志尾部。
    stderr 按真实 run 29304939828 顺序复原（pr_code_suggestions run:189 的 improve 专用串在场）。"""
    stderr = (
        "Generating code suggestions for PR...\n"
        "WARNING Error during LLM inference: litellm.Timeout: APITimeoutError - Request timed out. "
        "Error_str: Request timed out. - timeout value=600.0, time taken=1189.44 seconds\n"
        "Failed to generate prediction with openai/glm-5.2\n"
        "Failed to generate code suggestions for PR, error: Failed to generate prediction with any model of ['openai/glm-5.2']\n"
        "Async Wrapper: Completed Call, calling async_success_handler ...\n")  # review 成功日志（尾部）
    tool, detail = RP.summarize_llm_failure(stderr)
    assert tool == "improve"
    assert "litellm.Timeout" in detail
    assert "time taken=1189.44 seconds" in detail
    assert "timeout value=600.0" in detail


def test_summarize_llm_failure_connection_error():
    """PR#66 真场景：improve Connection error（非超时）。stderr 按真实 run 29316278645 复原。"""
    stderr = ("Error during LLM inference: litellm.InternalServerError: InternalServerError: "
              "OpenAIException - Connection error.\n"
              "Failed to generate prediction with openai/glm-5.2\n"
              "Failed to generate code suggestions for PR, error: Failed to generate prediction with any model of ['openai/glm-5.2']")
    tool, detail = RP.summarize_llm_failure(stderr)
    assert tool == "improve"
    assert "Connection error" in detail


def test_summarize_llm_failure_review_tool():
    """归因到 review（另一侧）：review 工具自身挂时不应错记成 improve。"""
    stderr = ("Error during LLM inference: litellm.Timeout: APITimeoutError\n"
              "Failed to review PR: boom")
    tool, detail = RP.summarize_llm_failure(stderr)
    assert tool == "review"
    assert "litellm.Timeout" in detail


def test_summarize_llm_failure_no_error_line():
    """无 'Error during LLM inference' 行（仅 runner 外化标记）→ detail 空、tool 仍命中，不崩。"""
    tool, detail = RP.summarize_llm_failure("[runner] improve produced no data（run() 内部已吞异常）")
    assert tool == "improve"
    assert detail == ""


def test_summarize_llm_failure_unknown_tool():
    """无任何工具签名时 tool=None（纯噪音 stderr 不强行归因）。"""
    tool, detail = RP.summarize_llm_failure("Error during LLM inference: litellm.Timeout: boom")
    assert tool is None
    assert "litellm.Timeout" in detail


def test_failure_stderr_tail_skips_success_logs():
    """尾部优先取失败行，不取另一侧成功工具的 async_success_handler 日志。"""
    stderr = (
        "Error during LLM inference: litellm.Timeout: ... time taken=1189s\n"
        "Failed to generate prediction with openai/glm-5.2\n"
        "key_issues_to_review: []\n"
        "Async Wrapper: Completed Call, calling async_success_handler\n")
    tail = RP.failure_stderr_tail(stderr)
    assert "litellm.Timeout" in tail
    assert "Failed to generate prediction" in tail
    assert "async_success_handler" not in tail      # 成功日志被排除


def test_failure_stderr_tail_includes_runner_markers():
    """runner 外化标记行（[runner] ...）也属失败相关行，纳入尾部。"""
    stderr = ("[runner] improve produced no data（...）\nAsync Wrapper ... async_success_handler")
    tail = RP.failure_stderr_tail(stderr)
    assert "improve produced no data" in tail
    assert "async_success_handler" not in tail


def test_failure_stderr_tail_falls_back_when_no_failure_lines():
    """无失败行时回退原始尾部（不丢诊断）。"""
    tail = RP.failure_stderr_tail("nothing relevant here at all, just noise")
    assert "noise" in tail


def test_invoke_endpoint_swallowed_caution_surfaces_specific_error(monkeypatch):
    """端到端：llm_failed caution 的 reason 【领头】含具体 litellm 异常 + time taken/timeout，
    而非只贴另一侧 review 的 success 日志。这是'caution 该给具体原因'的回归锁。"""
    stderr = (
        "WARNING Error during LLM inference: litellm.Timeout: APITimeoutError - Request timed out. "
        "Error_str: Request timed out. - timeout value=600.0, time taken=1189.44 seconds\n"
        "Failed to generate prediction with openai/glm-5.2\n"
        "Failed to generate code suggestions for PR, error: Failed to generate prediction with any model of ['openai/glm-5.2']\n"
        "Async Wrapper: Completed Call, calling async_success_handler\n")
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(
        0, out=json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}}), err=stderr))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"
    reason = ei.value.reason
    assert "improve" in reason                       # 归因到工具
    assert "litellm.Timeout" in reason               # 具体异常
    assert "time taken=1189.44" in reason            # 时序证据（timeout 没在 600s 生效）
    # 具体原因在"吞成 0 建议"之前（领头），而非埋进尾部
    assert reason.index("litellm.Timeout") < reason.index("吞成")


def test_invoke_endpoint_swallowed_caution_excludes_success_log_noise(monkeypatch):
    """caution 的 reason 不含另一侧 review 的 success 日志（async_success_handler）——
    锁死修复目标：消除"一边说 llm_failed、一边贴 success 日志"的自相矛盾。"""
    stderr = (
        "Error during LLM inference: litellm.Timeout: APITimeoutError - time taken=1189s\n"
        "Failed to generate prediction with openai/glm-5.2\n"
        "Async Wrapper: Completed Call, calling async_success_handler\n")
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(
        0, out=json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}}), err=stderr))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert "async_success_handler" not in ei.value.reason


def test_summarize_llm_failure_dual_failure_aligns_detail_to_improve():
    """双失败（improve+review 都挂）：improve 先跑、review 后跑，errs=[improve 的错, review 的错]。
    tool 归因为 improve（_IMPROVE_FAIL_SIGS 先命中），detail 必须取 errs[0]（improve 的真因），
    而非 errs[-1]（review 的）——否则 caution 会"说 improve 挂、却贴 review 的异常"，
    自相矛盾、误导运维。锁死本轮修复（详情与归因工具对齐）。"""
    stderr = (
        "Generating code suggestions for PR...\n"
        "WARNING Error during LLM inference: litellm.Timeout: APITimeoutError - "
        "timeout value=600.0, time taken=1189.44 seconds\n"        # improve 的错（先跑 → errs[0]）
        "Failed to generate code suggestions for PR, error: boom\n"  # _IMPROVE_FAIL_SIGS
        "Generating review prediction...\n"
        "Error during LLM inference: litellm.InternalServerError: Connection error.\n"  # review 的错（后跑 → errs[-1]）
        "Failed to review PR: boom\n")                               # _REVIEW_FAIL_SIGS
    tool, detail = RP.summarize_llm_failure(stderr)
    assert tool == "improve"
    assert "litellm.Timeout" in detail               # improve 的真因
    assert "time taken=1189.44 seconds" in detail
    assert "Connection error" not in detail          # review 的错被排除（不串台）


def test_summarize_llm_failure_dual_failure_review_takes_last():
    """对偶：review 单侧失败（无 improve 签名）→ 取 errs[-1]。补全对齐矩阵的另一支。"""
    stderr = ("Error during LLM inference: litellm.Timeout: APITimeoutError\n"
              "Failed to review PR: boom")
    tool, detail = RP.summarize_llm_failure(stderr)
    assert tool == "review"
    assert "litellm.Timeout" in detail


def test_failure_stderr_tail_fallback_honors_limit():
    """回退路径（无失败行）也尊重 limit 参数，而非硬编码 -600。
    传超长纯噪音 stderr + 小 limit，断言返回长度 ≤ limit。锁死本轮修复（两分支用同一 limit）。"""
    noise = "x" * 2000   # 远超默认 limit=800
    assert len(RP.failure_stderr_tail(noise, limit=100)) <= 100   # limit=100 生效（旧代码会返回 600）
    assert len(RP.failure_stderr_tail(noise)) <= 800              # 默认 limit=800 生效


# ============================================================================
# fan-out：improve / review 两子进程并行（_collect_subprocess / _merge_results / 融合路径）
# 用户要求：测多进程的多种极限/边界场景——两成功合并、部分降级（一挂一成）、全挂各类型、
# 超时、单 mode、日志合并、fanout-off 回退、以及【真并行】（非串行）的铁证。
# ============================================================================
_SUG = {"relevant_file": "a.py", "relevant_lines_start": 1, "relevant_lines_end": 1,
        "one_sentence_summary": "validate token", "label": "security"}
_KI = {"relevant_file": "b.py", "start_line": 2, "end_line": 2,
       "issue_header": "Edge case", "issue_content": "n=0", "label": "possible issue"}
_IMP_OUT = {"code_suggestions": [_SUG], "review": {"key_issues_to_review": []}}
_REV_OUT = {"code_suggestions": [], "review": {"key_issues_to_review": [_KI]}}


def _mode_of(args):
    """从 subprocess.run 的 args 列表取 --mode 值（fan-out 时为 improve / review）。"""
    return args[args.index("--mode") + 1] if "--mode" in args else "improve+review"


def _fanout_mock(imp_proc, rev_proc, *, record=None):
    """造一个按 --mode 分流的 subprocess.run mock：improve 子进程返回 imp_proc，review 返回 rev_proc。
    record（可选）收集每次调用的 (mode, kwargs) 用于断言 env/调用次数。"""
    def fake(args, **k):
        mode = _mode_of(args)
        if record is not None:
            record.append((mode, k))
        return imp_proc if mode == "improve" else rev_proc
    return fake


# ---------------- _fanout_enabled ----------------
def test_fanout_enabled_default_for_improve_review(monkeypatch):
    monkeypatch.delenv("TOUCHSTONE_PRAGENT_FANOUT", raising=False)
    assert RP._fanout_enabled("improve+review") is True


def test_fanout_disabled_by_env(monkeypatch):
    for off in ("false", "0", "no", "off", "FALSE"):
        monkeypatch.setenv("TOUCHSTONE_PRAGENT_FANOUT", off)
        assert RP._fanout_enabled("improve+review") is False, off
    monkeypatch.setenv("TOUCHSTONE_PRAGENT_FANOUT", "true")
    assert RP._fanout_enabled("improve+review") is True


def test_fanout_disabled_for_single_mode():
    assert RP._fanout_enabled("improve") is False      # 无可并行对象
    assert RP._fanout_enabled("review") is False


# ---------------- _collect_subprocess：六种状态归一 ----------------
def test_collect_ok_parses_data(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out=json.dumps(_IMP_OUT), err="litellm noise"))
    r = RP._collect_subprocess(["x", "--mode", "improve"], "improve", 60)
    assert r.status == RP._OK and r.data == _IMP_OUT
    assert r.stderr == "litellm noise"               # 原始 stderr 保留（供诊断）


def test_collect_crashed_injects_marker_keeps_stderr(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(2, err="boom-detail"))
    r = RP._collect_subprocess(["x"], "improve", 60)
    assert r.status == RP._CRASHED
    assert "[runner] improve subprocess crashed" in r.stderr   # 归因标记（检测契约）
    assert "boom-detail" in r.stderr                            # 原始 stderr 原样保留
    assert "boom-detail" in r.reason


def test_collect_timeout_status(monkeypatch):
    import subprocess as sp
    def boom(a, **k):
        raise sp.TimeoutExpired(cmd=a, timeout=600)
    monkeypatch.setattr(RP.subprocess, "run", boom)
    r = RP._collect_subprocess(["x"], "review", 600)
    assert r.status == RP._TIMED_OUT and r.timeout == 600
    assert "[runner] review subprocess timed out" in r.stderr
    assert "llm_failed" not in r.stderr           # collect 只记原始失败；降级类型由 _aggregate_failure 定（不在此预烤）


def test_collect_catchall_exception_does_not_raise(monkeypatch):
    """subprocess.run 抛 Timeout/FileNotFound 之外的异常（PermissionError 等）→ 兑现「绝不抛」：
    归 _CRASHED（带异常类型），不击穿出去（fan-out 下会炸整条链路）。"""
    def boom(a, **k):
        raise PermissionError("exec format error")
    monkeypatch.setattr(RP.subprocess, "run", boom)
    r = RP._collect_subprocess(["x"], "improve", 60)   # 不抛
    assert r.status == RP._CRASHED
    assert "[runner] improve subprocess crashed" in r.stderr
    assert "PermissionError" in r.stderr and "PermissionError" in r.reason   # 异常类型可见


def test_collect_missing_status_has_install_hint(monkeypatch):
    def boom(a, **k):
        raise FileNotFoundError("no such cmd")
    monkeypatch.setattr(RP.subprocess, "run", boom)
    r = RP._collect_subprocess(["x"], "improve", 60)
    assert r.status == RP._MISSING
    assert "pip install pr-agent" in r.reason


def test_collect_bad_json_status(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out="not json"))
    r = RP._collect_subprocess(["x"], "improve", 60)
    assert r.status == RP._BAD_JSON
    assert "[runner] improve subprocess non-JSON output" in r.stderr
    assert "JSON" in r.reason


def test_collect_json_parse_non_decode_error_does_not_raise(monkeypatch):
    # PR#81 fix1：_collect_subprocess 兑现「绝不抛」契约。_extract_json 当前只抛 JSONDecodeError，
    # 但若未来改动引入其他异常（如 KeyError/TypeError），catch-all 须归 _BAD_JSON 而非击穿。
    # 模拟未来回归：让 _extract_json 抛非 JSONDecodeError → 断言不抛、归 _BAD_JSON、异常类型入诊断。
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out='{"code_suggestions": []}'))
    monkeypatch.setattr(RP, "_extract_json", lambda s: (_ for _ in ()).throw(ValueError("synthetic future regression")))
    r = RP._collect_subprocess(["x"], "review", 60)
    assert r.status == RP._BAD_JSON                       # 不抛，归 _BAD_JSON（输出解析不可用）
    assert "ValueError" in r.stderr and "synthetic future regression" in r.stderr
    assert "ValueError" in r.reason


def test_collect_degraded_status_carries_value(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k:
                        _Proc(0, out=json.dumps({"_degraded": "llm_failed", "reason": "AuthError: 401"})))
    r = RP._collect_subprocess(["x"], "review", 60)
    assert r.status == RP._DEGRADED and r.degraded == "llm_failed"
    assert r.reason == "AuthError: 401"


def test_collect_degraded_only_when_truthy(monkeypatch):
    # _degraded 为空串/None（边界：误带的假标志）→ 不当降级，按 _OK 处理
    for fake_deg in ("", None):
        monkeypatch.setattr(RP.subprocess, "run", lambda a, _d=fake_deg, **k:
                            _Proc(0, out=json.dumps({"_degraded": _d, "code_suggestions": [_SUG]})))
        r = RP._collect_subprocess(["x"], "improve", 60)
        assert r.status == RP._OK, fake_deg


def test_collect_passes_distinct_log_env(monkeypatch):
    """fan-out：每子进程经 env 收到独立 TOUCHSTONE_INTERACTION_LOG（防并发写覆盖）。"""
    seen = {}
    def fake(args, **k):
        seen[_mode_of(args)] = k.get("env", {}).get("TOUCHSTONE_INTERACTION_LOG")
        return _Proc(0, out=json.dumps(_IMP_OUT if _mode_of(args) == "improve" else _REV_OUT))
    monkeypatch.setattr(RP.subprocess, "run", fake)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    monkeypatch.setenv("TOUCHSTONE_INTERACTION_LOG", "/tmp/base.log")
    RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert seen["improve"].endswith(".improve") and seen["review"].endswith(".review")
    assert seen["improve"] != seen["review"]


# ---------------- _merge_results：纯函数边界矩阵 ----------------
def _res(mode, status, data=None, stderr="", reason="", degraded=None):
    return RP._SubResult(mode, status, data=data, stderr=stderr, reason=reason, degraded=degraded)


def test_merge_both_ok_combines_cs_from_improve_review_from_review():
    imp = _res("improve", RP._OK, _IMP_OUT)
    rev = _res("review", RP._OK, _REV_OUT)
    data, stderr, failure = RP._merge_results(imp, rev)
    assert failure is None
    assert data["code_suggestions"] == [_SUG]          # cs 取自 improve
    assert data["review"]["key_issues_to_review"] == [_KI]   # review 取自 review
    # 跨侧数据被忽略：improve 的 review 占位、review 的 cs 占位都不进合并
    assert len(data["code_suggestions"]) == 1 and len(data["review"]["key_issues_to_review"]) == 1


def test_merge_takes_correct_side_ignores_cross():
    # improve 恰好带了 review 数据、review 恰好带了 cs —— 合并必须各取【本侧】，不串台
    imp = _res("improve", RP._OK, {"code_suggestions": [_SUG], "review": {"key_issues_to_review": [_KI]}})
    rev = _res("review", RP._OK, {"code_suggestions": [_SUG], "review": {"key_issues_to_review": []}})
    data, _, failure = RP._merge_results(imp, rev)
    assert failure is None
    assert data["code_suggestions"] == [_SUG]                      # improve 的 cs
    assert data["review"]["key_issues_to_review"] == []            # review 的（空）review，非 improve 带过来的


def test_merge_same_subprocess_no_stderr_duplication():
    # fanout-off：imp is rev（同对象）→ stderr 不重复、data 原样
    single = _res("improve+review", RP._OK, _IMP_OUT, stderr="once\n")
    data, stderr, failure = RP._merge_results(single, single)
    assert failure is None and stderr == "once\n"      # 不是 "once\n\nonce"
    assert data is single.data                          # 同对象直接用


def test_merge_improve_not_run_single_mode():
    imp = _res("improve", RP._NOT_RUN)
    rev = _res("review", RP._OK, _REV_OUT)
    data, _, failure = RP._merge_results(imp, rev)
    assert failure is None                              # 只跑了一侧且成功
    assert data["code_suggestions"] == [] and data["review"]["key_issues_to_review"] == [_KI]


def test_merge_review_not_run_single_mode():
    imp = _res("improve", RP._OK, _IMP_OUT)
    rev = _res("review", RP._NOT_RUN)
    data, _, failure = RP._merge_results(imp, rev)
    assert failure is None
    assert data["code_suggestions"] == [_SUG] and data["review"] == {}


def test_merge_missing_is_fatal_no_engine():
    # 引擎没装：哪怕另一侧成功，整体仍 no_engine（救不了"装不上"）
    imp = _res("improve", RP._MISSING, reason="pip install pr-agent …")
    rev = _res("review", RP._OK, _REV_OUT)
    data, _, failure = RP._merge_results(imp, rev)
    assert failure == ("no_engine", "pip install pr-agent …")


def test_merge_both_crashed_no_engine():
    imp = _res("improve", RP._CRASHED, stderr="[runner] improve subprocess crashed（rc=2）\nboom",
               reason="improve rc=2")
    rev = _res("review", RP._CRASHED, reason="review rc=2")
    _, stderr, failure = RP._merge_results(imp, rev)
    assert failure[0] == "no_engine"
    assert "boom" in stderr                              # 原始诊断保留


def test_merge_both_timed_out_llm_failed():
    imp = _res("improve", RP._TIMED_OUT, reason="improve timeout")
    rev = _res("review", RP._TIMED_OUT, reason="review timeout")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure[0] == "llm_failed"                    # 超时=LLM 调用太慢，非引擎没装


def test_merge_both_bad_json_no_engine():
    imp = _res("improve", RP._BAD_JSON, reason="improve bad json")
    rev = _res("review", RP._BAD_JSON, reason="review bad json")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure[0] == "no_engine"


def test_merge_both_degraded_uses_value():
    imp = _res("improve", RP._DEGRADED, degraded="llm_failed", reason="401")
    rev = _res("review", RP._DEGRADED, degraded="llm_failed", reason="401")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure == ("llm_failed", "401\n401")


def test_merge_mixed_timeout_and_crash_prefers_llm_failed():
    imp = _res("improve", RP._TIMED_OUT, reason="imp timeout")
    rev = _res("review", RP._CRASHED, reason="rev crash")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure[0] == "llm_failed"                    # 有超时→就高归因 llm_failed


def test_merge_mixed_degraded_llm_failed_and_crash_prefers_llm_failed():
    imp = _res("improve", RP._DEGRADED, degraded="llm_failed", reason="401")
    rev = _res("review", RP._CRASHED, reason="rev crash")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure[0] == "llm_failed"


def test_merge_both_degraded_mixed_values_picks_llm_failed():
    # PR#81 fix2：两侧都 _degraded 但【值混合】（improve=no_engine、review=llm_failed）→ 就高归因 llm_failed。
    # 旧序（all(_DEGRADED) 分支先于 any(llm_failed) 分支）会落到 failed[0]=improve 的值=no_engine，漏掉就高。
    # improve 在 (imp, rev) 元组里居首即 failed[0]，故此用例恰好命中旧 bug 的取错侧。
    imp = _res("improve", RP._DEGRADED, degraded="no_engine", reason="improve 引擎缺失")
    rev = _res("review", RP._DEGRADED, degraded="llm_failed", reason="review 401")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure[0] == "llm_failed"                        # 就高：llm_failed 盖过 no_engine
    assert "improve 引擎缺失" in failure[1] and "review 401" in failure[1]   # 两侧 reason 都进汇总


def test_merge_both_degraded_all_no_engine_stays_no_engine():
    # fix2 回归锁：两侧都 _degraded 且无一 llm_failed → all 分支仍取 failed[0].degraded=no_engine
    # （调换分支顺序后这条路径不可被破坏）。
    imp = _res("improve", RP._DEGRADED, degraded="no_engine", reason="imp 缺失")
    rev = _res("review", RP._DEGRADED, degraded="no_engine", reason="rev 缺失")
    _, _, failure = RP._merge_results(imp, rev)
    assert failure[0] == "no_engine"


def test_merge_improve_failed_review_ok_is_partial_not_failure():
    # 部分降级：improve 挂、review 成 → 不抛（保留 review 发现），交下游 partial 元信息处理
    imp = _res("improve", RP._CRASHED, stderr="[runner] improve subprocess crashed（rc=2）")
    rev = _res("review", RP._OK, _REV_OUT)
    data, _, failure = RP._merge_results(imp, rev)
    assert failure is None                               # 不整轮降级
    assert data["review"]["key_issues_to_review"] == [_KI]   # review 发现照常在


def test_merge_improve_degraded_review_ok_is_partial():
    imp = _res("improve", RP._DEGRADED, degraded="llm_failed", reason="401")
    rev = _res("review", RP._OK, _REV_OUT)
    data, _, failure = RP._merge_results(imp, rev)
    assert failure is None


# ---------------- _status_partial_failure：精确归因 ----------------
def test_status_partial_failure_attributes():
    assert RP._status_partial_failure(_res("improve", RP._CRASHED), _res("review", RP._OK)) == "improve"
    assert RP._status_partial_failure(_res("improve", RP._OK), _res("review", RP._TIMED_OUT)) == "review"
    assert RP._status_partial_failure(_res("improve", RP._OK), _res("review", RP._OK)) is None
    assert RP._status_partial_failure(_res("improve", RP._CRASHED), _res("review", RP._CRASHED)) is None  # 双挂不归单侧
    single = _res("improve+review", RP._OK)
    assert RP._status_partial_failure(single, single) is None    # fanout-off：交 partial_tool_failure


# ---------------- 融合路径（fetch + 模式感知 mock）----------------
def test_fanout_both_success_merges_four_items(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(0, out=json.dumps({
                            "code_suggestions": _RAW["code_suggestions"], "review": {"key_issues_to_review": []}})),
                                     _Proc(0, out=json.dumps({
                            "code_suggestions": [], "review": _RAW["review"]}))))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 4   # 3 cs(improve) + 1 ki(review)


def test_fanout_invokes_two_subprocesses_improve_and_review(monkeypatch):
    record = []
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(0, out=json.dumps(_IMP_OUT)), _Proc(0, out=json.dumps(_REV_OUT)),
                                     record=record))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    RP.fetch({"owner": "o", "repo": "r", "number": 3})
    modes = sorted(m for m, _ in record)
    assert modes == ["improve", "review"]                # 确实起了两个子进程
    assert len(record) == 2


def test_fanout_improve_degraded_review_ok_returns_review_findings(monkeypatch):
    """核心行为变化：improve 挂（_degraded llm_failed）但 review 成 → 不整轮降级，返回 review 发现、
    标 partial=improve。旧单子进程下任一 _degraded 即整轮失败；fan-out 下部分降级更保真。"""
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(0, out=json.dumps({"_degraded": "llm_failed", "reason": "401"})),
                                     _Proc(0, out=json.dumps(_REV_OUT))))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert len(items) == 1 and items[0]["tool"] == "review"   # review 的意见照常返回
    assert RP.invoke_meta()["partial_tool_failure"] == "improve"


def test_fanout_review_degraded_improve_ok_returns_suggestions(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(0, out=json.dumps(_IMP_OUT)),
                                     _Proc(0, out=json.dumps({"_degraded": "llm_failed", "reason": "401"}))))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert len(items) == 1 and items[0]["tool"] == "improve"
    assert RP.invoke_meta()["partial_tool_failure"] == "review"


def test_fanout_improve_timeout_review_ok_partial(monkeypatch):
    import subprocess as sp
    def fake(args, **k):
        if _mode_of(args) == "improve":
            raise sp.TimeoutExpired(cmd=args, timeout=600)
        return _Proc(0, out=json.dumps(_REV_OUT))
    monkeypatch.setattr(RP.subprocess, "run", fake)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert len(items) == 1                                  # 超时侧丢弃，review 发现保留
    assert RP.invoke_meta()["partial_tool_failure"] == "improve"


def test_fanout_improve_crash_review_ok_partial(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(2, err="improve crashed"), _Proc(0, out=json.dumps(_REV_OUT))))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert len(items) == 1
    assert RP.invoke_meta()["partial_tool_failure"] == "improve"


def test_fanout_improve_crash_review_empty_not_swallowed(monkeypatch):
    # PR#81 fix3：fan-out 下 improve 硬失败（rc=2、stderr 带 pred-failure 串）、review 正常却空建议。
    # 合并后 data 正好空 + stderr 带失败串 → 旧 swallowed 兜底会误把整轮判 llm_failed、丢掉 review 的真发现。
    # fix3：_status_partial_failure 命中（=improve）→ 豁免 swallowed 检查；整轮保 partial、不降级、不抛。
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(
                            _Proc(2, err="Failed to generate prediction with openai/glm-5.2"),
                            _Proc(0, out=json.dumps({"code_suggestions": [],
                                                     "review": {"key_issues_to_review": []}}))))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    items = RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert items == []                                        # review 空建议=真没意见，不抛
    assert RP.invoke_meta()["partial_tool_failure"] == "improve"   # 失败仍可见，整轮可信


def test_fanout_improve_crash_review_swallowed_raises(monkeypatch):
    # fan-out 假收敛：improve 硬失败（rc=2）+ review【也吞没式失败】（rc=0、空建议、但 stderr 带 review
    # 失败串）。_status_partial_failure 只看子进程状态 → review 侧 _OK.failed=False → 命中 partial=improve。
    # 旧豁免（_partial_side 命中即整段跳过 swallowed 检查）会把这轮【两工具都挂】当可信空评审 → 假收敛。
    # 修：豁免前再查非失败侧(review)自身 stderr 是否带 _REVIEW_FAIL_SIGS → 带则不豁免、判 llm_failed。
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(
                            _Proc(2, err="Failed to generate prediction with openai/glm-5.2"),
                            _Proc(0, out=json.dumps({"code_suggestions": [],
                                                     "review": {"key_issues_to_review": []}}),
                                  err="Failed to review PR: Error during LLM inference: connection error")))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"


def test_fanout_review_crash_improve_swallowed_raises(monkeypatch):
    # 对称：review 硬失败（rc=2、带 review 子进程失败串）+ improve【也吞没式失败】（rc=0、空、stderr 带
    # improve 失败串）→ 同样两工具都挂，须判 llm_failed（豁免不应掩盖非失败侧的吞没式失败）。
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(
                            _Proc(0, out=json.dumps({"code_suggestions": [],
                                                     "review": {"key_issues_to_review": []}}),
                                  err="Failed to generate code suggestions for PR: Error during LLM inference: timeout"),
                            _Proc(2, err="Failed to review PR: boom")))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"


def test_fanout_both_degraded_raises(monkeypatch):
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(0, out=json.dumps({"_degraded": "llm_failed", "reason": "401"})),
                                     _Proc(0, out=json.dumps({"_degraded": "llm_failed", "reason": "401"}))))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"


def test_fanout_both_timeout_raises_llm_failed(monkeypatch):
    import subprocess as sp
    def fake(args, **k):
        raise sp.TimeoutExpired(cmd=args, timeout=600)
    monkeypatch.setattr(RP.subprocess, "run", fake)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    with pytest.raises(RP.ReviewEngineDegraded) as ei:
        RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert ei.value.degraded == "llm_failed"


def test_fanout_both_empty_clean_not_swallowed(monkeypatch):
    # 干净的小 PR：两侧都 0 原始建议、无失败串 → 不误判吞没，engine ok、0 发现
    monkeypatch.setattr(RP.subprocess, "run",
                        _fanout_mock(_Proc(0, out=json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}}),
                                            err="LiteLLM-Async Success Call"),
                                     _Proc(0, out=json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}}),
                                            err="LiteLLM-Async Success Call")))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    assert RP.fetch({"owner": "o", "repo": "r", "number": 3}) == []


def test_fanout_interaction_logs_merged_into_base(monkeypatch, tmp_path):
    """两子进程写各自交互日志（.improve/.review），跑完合并进 base 并删子文件——不丢任一侧、无并发覆盖。"""
    base_log = tmp_path / "ix.log"
    monkeypatch.setenv("TOUCHSTONE_INTERACTION_LOG", str(base_log))

    def fake(args, **k):
        mode = _mode_of(args)
        log_path = k.get("env", {}).get("TOUCHSTONE_INTERACTION_LOG")
        if log_path:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(f"[{mode}] request/response trace\n")
        return _Proc(0, out=json.dumps(_IMP_OUT if mode == "improve" else _REV_OUT))
    monkeypatch.setattr(RP.subprocess, "run", fake)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    RP.fetch({"owner": "o", "repo": "r", "number": 3})
    merged = base_log.read_text(encoding="utf-8")
    assert "[improve] request/response trace" in merged
    assert "[review] request/response trace" in merged
    assert not (tmp_path / "ix.log.improve").exists()      # 子文件已清理
    assert not (tmp_path / "ix.log.review").exists()


def test_fanout_disabled_runs_single_subprocess(monkeypatch):
    """TOUCHSTONE_PRAGENT_FANOUT=false → 回落单子进程（improve+review 合并），向后兼容旧行为。"""
    calls = []

    def fake(args, **k):
        calls.append(_mode_of(args))
        return _Proc(0, out=json.dumps(_RAW))              # 单子进程返回完整 improve+review
    monkeypatch.setattr(RP.subprocess, "run", fake)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    monkeypatch.setenv("TOUCHSTONE_PRAGENT_FANOUT", "false")
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 4
    assert calls == ["improve+review"]                     # 只调一次、合并 mode


def test_single_mode_improve_runs_one_subprocess(monkeypatch):
    """mode=improve（只跑建议侧）→ 单子进程、review 占位 _NOT_RUN。"""
    monkeypatch.setattr(RP.subprocess, "run", lambda a, **k: _Proc(0, out=json.dumps(_IMP_OUT)))
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    monkeypatch.setattr(RP, "_provider_mode", lambda ctx: "improve")   # 强制单一 mode
    assert len(RP.fetch({"owner": "o", "repo": "r", "number": 3})) == 1


def test_fanout_subprocesses_run_in_parallel(monkeypatch):
    """铁证：fan-out 真并行（两子进程并发执行），非串行——否则慢轮省时假设不成立。
    用并发计数器：两 subprocess.run 调用有重叠 → max_active==2（串行会是 1）。"""
    import threading
    import time as _time
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def fake(args, **k):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        _time.sleep(0.08)                                  # 放大重叠窗口，让两调用并发
        with lock:
            state["active"] -= 1
        out = _IMP_OUT if _mode_of(args) == "improve" else _REV_OUT
        return _Proc(0, out=json.dumps(out))
    monkeypatch.setattr(RP.subprocess, "run", fake)
    monkeypatch.setattr(RP, "_experience_injection", lambda d: "")
    RP.fetch({"owner": "o", "repo": "r", "number": 3})
    assert state["max_active"] == 2                        # 两子进程并发（串行=1 → 断言失败）


def test_load_nmap_corrupt_warns_missing_silent(tmp_path, capsys, monkeypatch):
    # P2-1：可选归一映射【缺失】= 常态静默；【损坏】= 回落默认但 stderr 可见（防分类漂移）
    monkeypatch.delenv("TOUCHSTONE_PRAGENT", raising=False)
    default = RP.load_nmap(str(tmp_path))
    assert "归一映射加载失败" not in capsys.readouterr().err
    d = tmp_path / ".touchstone"; d.mkdir()
    (d / "pr-agent.yaml").write_text(": :\n  - [", encoding="utf-8")
    got = RP.load_nmap(str(tmp_path))
    assert got == default
    assert "归一映射加载失败" in capsys.readouterr().err

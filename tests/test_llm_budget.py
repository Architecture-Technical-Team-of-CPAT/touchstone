"""LLM 预算收敛 + 大 diff 安全扫描回归测试。
核心：确定性核对（SEC-001）跑【全文 diff】，截断只施加在显示/LLM 侧——
大 PR 不能把泄漏的凭据藏在截断点之后绕过密钥门禁。"""
import os

from touchstone import llm_budget as LB


# ---------------- llm_budget 单一来源 ----------------
def test_context_tokens_from_env(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", "128000")
    assert LB.context_tokens() == 128000


def test_context_tokens_unknown_is_zero(monkeypatch):
    monkeypatch.delenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", raising=False)
    assert LB.context_tokens() == 0


def test_output_tokens_default_and_env(monkeypatch):
    monkeypatch.delenv("TOUCHSTONE_LLM_OUTPUT_TOKENS", raising=False)
    assert LB.output_tokens() == 4096
    monkeypatch.setenv("TOUCHSTONE_LLM_OUTPUT_TOKENS", "8192")
    assert LB.output_tokens() == 8192


def test_output_tokens_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_LLM_OUTPUT_TOKENS", "garbage")
    assert LB.output_tokens() == 4096


def test_est_tokens_positive_and_monotone():
    assert LB.est_tokens("") == 1                     # 空串给 1（避免 0 除）
    assert LB.est_tokens("a") > 0
    assert LB.est_tokens("a" * 1000) > LB.est_tokens("a")
    assert LB.est_tokens("hello world code") <= LB.est_tokens("hello world code " * 10)


def test_llm_diff_budget_derives_from_context(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", "128000")
    monkeypatch.setenv("TOUCHSTONE_LLM_OUTPUT_TOKENS", "4096")
    # 128000 - 2000(overhead) - 4096(output) = 121904
    assert LB.llm_diff_token_budget() == 121904


def test_llm_diff_budget_zero_when_context_unknown(monkeypatch):
    monkeypatch.delenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", raising=False)
    assert LB.llm_diff_token_budget() == 0            # 不声明 → 不主动截断


def test_truncate_to_tokens_respects_budget():
    big = "x" * 100000
    out = LB.truncate_to_tokens(big, 100)             # 截到约 100 token
    assert LB.est_tokens(out) <= 100
    assert out.endswith("... [diff truncated]") or len(out) < len(big)


def test_truncate_to_tokens_zero_means_no_truncation():
    assert LB.truncate_to_tokens("anything", 0) == "anything"


# ---------------- 大 diff：SEC-001 跑全文，密钥在尾部也抓得到（回归）----------------
def _rule_index():
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rules = yaml.safe_load(open(os.path.join(root, ".touchstone", "standards.yaml"), encoding="utf-8"))["rules"]
    return {r["id"]: r for r in rules}


def test_sec001_catches_secret_beyond_old_truncation_point(monkeypatch):
    """构造一个 > 原 DIFF_BUDGET(60K) 的大 diff，AKIA 密钥放在 60K 字符之后。
    全文扫描前：截断 → 漏检（本会话实测过）；全文扫描后：SEC-001 必须命中。
    这条测试锁住"安全扫描跑全文、不被大 diff 绕过"。"""
    from touchstone import contract_check as cc
    ridx = _rule_index()
    # 前面塞 65000 字符的普通改动（> 旧 60K 预算），末尾藏真密钥
    pad = "".join(f"diff --git a/p{i}.py b/p{i}.py\n--- a/p{i}.py\n+++ b/p{i}.py\n@@ -0,0 +1 @@\n+x{i}\n"
                  for i in range(4000))
    assert len(pad) > 60000
    diff = pad + 'diff --git a/secret.py b/secret.py\n--- a/secret.py\n+++ b/secret.py\n' \
                '@@ -0,0 +1 @@\n+K="AKIAABCDEFGHIJKLMNOP"\n'
    f = cc.check_contract_consistency(diff, {}, ridx)
    assert any(x["rule_id"] == "SEC-001" for x in f), "密钥在 60K 之后仍必须被 SEC-001 抓到（全文扫描）"


def test_render_summary_caps_findings_to_avoid_comment_overflow():
    """大 PR 产出大量发现 → 评审发现与销项段封顶列出，超出折叠，避免超 GitHub 65536 字符限。
    v2：封顶逻辑在 render_findings_checklist（合并段）。"""
    from touchstone import render, checklist as cl
    findings = [{"rule_id": f"R{i}", "agent": "pr-agent", "severity": "warn", "confidence": 0.5,
                 "file": "a.py", "line": i, "rationale": "x", "suggested_fix": "y"}
                for i in range(500)]
    c = cl.from_findings(findings)
    body = render.render_findings_checklist(findings, c)
    assert "另有" in body and "仅列前" in body
    assert len(body) < 65536                        # 评论体不超限
    assert body.count("- [ ]") <= LB.MAX_FINDINGS_IN_SUMMARY   # 列出的不超过封顶


# ---------------- 体量门禁（SIZE-001）----------------
def test_size_gate_blocks_large_diff(monkeypatch):
    """超过 TOUCHSTONE_MAX_DIFF_LINES → 不调 LLM，直接产 SIZE-001 block_candidate。"""
    from touchstone import orchestrator as orc, review_provider as rp
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "5")        # 只许 5 行
    # 10 行新增 → 超限
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1 @@\n+x{i}\n"
                   for i in range(10))
    pr = {"diff": diff, "pr_agent_output": {"SHOULD_NOT_BE_USED": True}}
    out = orc.review_pr(pr, {}, {})
    size = [f for f in out["findings"] if f.get("rule_id") == "SIZE-001"]
    assert size and size[0]["severity"] == "block_candidate"     # block 级
    assert out["engine_status"] == "skipped_large_diff"          # LLM 被跳过
    assert out["ai_raw_count"] == 0                              # 没调 pr-agent


def test_size_gate_allows_within_limit(monkeypatch):
    """未超限 → 正常调 LLM，不产 SIZE-001。"""
    from touchstone import orchestrator as orc
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "100")      # 上限 100，diff 只 2 行
    diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    pr = {"diff": diff, "pr_agent_output": {"code_suggestions": [], "review": {"key_issues_to_review": []}}}
    out = orc.review_pr(pr, {}, {})
    assert out["engine_status"] == "ok"
    assert not any(f.get("rule_id") == "SIZE-001" for f in out["findings"])


def test_size_gate_default_1000_under_limit(monkeypatch):
    """未设 TOUCHSTONE_MAX_DIFF_LINES → 默认上限 1000 行。200 行 < 1000 → 放行：正常调 LLM、不产 SIZE-001。"""
    from touchstone import orchestrator as orc
    monkeypatch.delenv("TOUCHSTONE_MAX_DIFF_LINES", raising=False)
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1 @@\n+x\n" for i in range(200))
    pr = {"diff": diff, "pr_agent_output": {"code_suggestions": [], "review": {"key_issues_to_review": []}}}
    out = orc.review_pr(pr, {}, {})
    assert out["engine_status"] == "ok"
    assert not any(f.get("rule_id") == "SIZE-001" for f in out["findings"])


def test_size_gate_default_1000_over_limit_blocks(monkeypatch):
    """未设 TOUCHSTONE_MAX_DIFF_LINES → 默认 1000 行。1200 行 > 1000 → SIZE-001 block + 跳过 LLM。"""
    from touchstone import orchestrator as orc
    monkeypatch.delenv("TOUCHSTONE_MAX_DIFF_LINES", raising=False)
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1 @@\n+x\n" for i in range(1200))
    pr = {"diff": diff, "pr_agent_output": {"SHOULD_NOT_BE_USED": True}}
    out = orc.review_pr(pr, {}, {})
    size = [f for f in out["findings"] if f.get("rule_id") == "SIZE-001"]
    assert size and size[0]["severity"] == "block_candidate"      # block 级
    assert out["engine_status"] == "skipped_large_diff"           # LLM 被跳过
    assert out["ai_raw_count"] == 0                               # 没调 pr-agent


def test_size_gate_boundary_is_strict_gt_against_configured_limit(monkeypatch):
    """锁 SIZE-001 门禁边界：以【TOUCHSTONE_MAX_DIFF_LINES 配置值】为界、严格大于（>，非 >=）。
    设上限 N，则正好 N 行 → 放行、N+1 行 → 拦截。

    用非默认值 N=100（非 1000）一并钉死两点，弥补既有测试只在 200/1200 远离边界的覆盖缺口：
      1. 严格 >：若误写成 >=，则 N 行（== 上限）会被拦截 → 第一处断言失败。
      2. 读 env 而非硬编码常量：若实现误把 1000 写死，则 N+1=101 行不会拦截（101 不超 1000）
         → 第二处断言失败。off-by-one 与"写死常量"是阈值门禁最易错的两点。
    """
    from touchstone import orchestrator as orc
    N = 100
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", str(N))

    def _diff(n):   # n 个干净 + 新增行（每行带 + 前缀，unidiff 严格计数）
        return ("diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
                f"@@ -0,0 +1,{n} @@\n" + "".join(f"+l{i}\n" for i in range(n)))

    # 正好 N 行（== 配置上限）→ 放行（严格 >：等于不算超）
    out = orc.review_pr(
        {"diff": _diff(N),
         "pr_agent_output": {"code_suggestions": [], "review": {"key_issues_to_review": []}}}, {}, {})
    assert out["engine_status"] == "ok"
    assert not any(f.get("rule_id") == "SIZE-001" for f in out["findings"])

    # N+1 行 → 拦截（SIZE-001 block + 跳过 LLM）
    out2 = orc.review_pr({"diff": _diff(N + 1), "pr_agent_output": {"SHOULD_NOT_BE_USED": True}}, {}, {})
    size = [f for f in out2["findings"] if f.get("rule_id") == "SIZE-001"]
    assert size and size[0]["severity"] == "block_candidate"        # block 级
    assert out2["engine_status"] == "skipped_large_diff"            # LLM 被跳过
    assert out2["ai_raw_count"] == 0                                # 没调 pr-agent

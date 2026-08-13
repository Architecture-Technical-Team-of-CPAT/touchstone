"""#1 真·链路回放（e2e）：用录制的 pr-agent 输出 + diff，离线重放整条
review_pr → post_results，断言【贴到 PR 的评论结构】稳定——把"集成是否还通"
变成可回归测试，而非靠手动开 PR 探。这是本会话最大教训的直接补救。"""
import json
import re

from touchstone import orchestrator as orc
from touchstone import review_provider as rp
from touchstone import checklist as cl


# ---- 录制的 golden 夹具：贴近真实 pr-agent improve+review 输出 + 一个含多类改动的 diff ----
GOLDEN_PR_AGENT = {
    "code_suggestions": [
        {"relevant_file": "src/auth.py", "relevant_lines_start": 12, "relevant_lines_end": 14,
         "one_sentence_summary": "Token 未校验即用", "improved_code": "if not token: raise",
         "label": "security"},
        {"relevant_file": "src/calc.py", "relevant_lines_start": 7, "relevant_lines_end": 9,
         "one_sentence_summary": "循环边界 off-by-one", "improved_code": "range(n+1)",
         "label": "possible bug"},
    ],
    "review": {"key_issues_to_review": [
        {"relevant_file": "src/util.py", "start_line": 30, "end_line": 30,
         "issue_header": "命名不清", "issue_content": "建议改名", "label": "enhancement"},
    ]},
}
GOLDEN_DIFF = (
    "diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n"
    "@@ -10,3 +10,4 @@\n def f():\n-    use(token)\n+    if token:\n+        use(token)\n"
    "diff --git a/src/calc.py b/src/calc.py\n--- a/src/calc.py\n+++ b/src/calc.py\n"
    "@@ -6,3 +6,3 @@\n-for i in range(n):\n+for i in range(n+1):\n")


def _golden_pr_ctx():
    return {"owner": "o", "repo": "r", "number": 42, "sha": "deadbeef",
            "token": "tok", "diff": GOLDEN_DIFF, "pr_agent_output": GOLDEN_PR_AGENT}


def test_e2e_replay_produces_stable_comment_structure(monkeypatch):
    """整链：fetch(注入)→normalize→map_verdict→review_pr→post_results，捕获贴出的评论，
    断言其结构（advisory 头/风险行/发现列表/marker 机读段）稳定。"""
    posted = {}
    def cap_gh(method, path, token, data=None, accept="application/vnd.github+json"):
        if method == "POST" and path.endswith("/comments"):
            posted["body"] = data["body"]
        return {}
    monkeypatch.setattr(orc, "gh", cap_gh)

    out = orc.review_pr(_golden_pr_ctx(), {}, {})
    findings, risk = out["findings"], out["risk"]
    orc.post_results("o", "r", 42, "deadbeef", "tok", risk, findings,
                     loop_info=("converged", "无可自改", "<!-- touchstone-loop: {} -->"),
                     change_class="high|code|security|security_surface", diff=GOLDEN_DIFF,
                     checklist=cl.from_findings(findings))

    body = posted["body"]
    # —— 人可读结构稳定 ——
    assert "Touchstone · AI Committer 代码检视" in body
    assert "风险等级" in body and "风险等级：高" in body   # 含 security → high，中文"高"
    assert "触发因子" in body                                # blast（原"影响面"→"触发因子"）
    # 发现逐条带关键字段
    for rid in ("PRA-",):                                  # pr-agent 来源前缀
        assert rid in body
    assert "src/auth.py" in body and "src/calc.py" in body
    # —— 机读 marker 可解析 + 字段齐 ——
    result = json.loads(re.search(r"<!-- touchstone-result: (.*?) -->", body, re.S).group(1))
    assert result["risk_band"] == "high"
    assert result["verification_decision"] in ("full_suite", "targeted_tests")
    assert isinstance(result["findings"], list) and len(result["findings"]) == len(findings)
    assert all({"rule_id", "agent", "severity"} <= set(f) for f in result["findings"])


def test_e2e_replay_findings_count_matches_raw(monkeypatch):
    """重放链路：pr-agent 原始条数 → 归一后条数一致（不被静默丢/加倍）。"""
    out = orc.review_pr(_golden_pr_ctx(), {}, {})
    # 2 code_suggestions + 1 key_issue = 3 ReviewItem；归一后仍 3（无 discard/conf 过滤命中）
    assert out["ai_raw_count"] == 3
    assert len(out["findings"]) == 3


def test_e2e_replay_zero_finding_path_shows_trace(monkeypatch, tmp_path):
    """0-发现路径：贴出的评论含溯源（不是裸"未发现"）——防静默故障的 e2e 锁。"""
    posted = {}
    monkeypatch.setattr(orc, "gh",
                        lambda m, p, t, data=None, **k: posted.update(body=data["body"]) if (m == "POST" and p.endswith("/comments")) else {})
    # 空 pr-agent 输出 + 空 diff → 0 发现 + 溯源
    pr = {"owner": "o", "repo": "r", "number": 1, "sha": "s", "token": "t",
          "diff": "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ +0,0 +1,3 @@\n+a\n+b\n+c\n",
          "pr_agent_output": {"code_suggestions": [], "review": {"key_issues_to_review": []}}}
    out = orc.review_pr(pr, {}, {})
    orc.post_results("o", "r", 1, "s", "t", out["risk"], out["findings"],
                     change_class="low|code|none|none",
                     engine_status="ok", ai_raw_count=0, added_lines=3, n_changed=1)
    assert "已端到端运行" in posted["body"] and "0 条原始建议" in posted["body"]


# ============================ #4 确定性层不变量 ============================
from touchstone import contract_check
from touchstone import stack_rules


def _rule_index():
    import yaml, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rules = yaml.safe_load(open(os.path.join(root, ".touchstone", "standards.yaml"), encoding="utf-8"))["rules"]
    return {r["id"]: r for r in rules}


def test_deterministic_layer_is_deterministic_and_llm_free(monkeypatch):
    """门禁核心承诺：同 diff → 同结论、逐位稳定、且与任何 LLM/env 无关。
    清空所有 LLM env、跑 N 次，断言结果完全一致。一旦有人把 LLM 引进确定性层即红。"""
    import os
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "OPENAI_API_KEY",
              "OPENAI_API_BASE", "TOUCHSTONE_FLAGSHIP_MODEL"):
        monkeypatch.delenv(k, raising=False)
    ridx = _rule_index()
    diff = (
        "diff --git a/db/migrations/0.sql b/db/migrations/0.sql\n--- a/db/migrations/0.sql\n"
        "+++ b/db/migrations/0.sql\n@@ +0,0 +1,1 @@\n+ALTER TABLE t;\n")
    runs = []
    for _ in range(5):
        cf = contract_check.check_contract_consistency(diff, {}, ridx)
        sf = stack_rules.check_stack_rules(diff, ridx)
        runs.append((json.dumps(cf, sort_keys=True, ensure_ascii=False),
                     json.dumps(sf, sort_keys=True, ensure_ascii=False)))
    assert len(set(runs)) == 1                      # 5 次完全一致


def test_deterministic_layer_independent_of_experience_env(monkeypatch):
    """确定性核对不应被经验库 env 影响（经验只调建议、不进门禁）。"""
    ridx = _rule_index()
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ +0,0 +1,1 @@\n+x=1\n"
    monkeypatch.delenv("TOUCHSTONE_EXPERIENCE_ENABLED", raising=False)
    a = contract_check.check_contract_consistency(diff, {}, ridx)
    monkeypatch.setenv("TOUCHSTONE_EXPERIENCE_ENABLED", "true")
    b = contract_check.check_contract_consistency(diff, {}, ridx)
    assert a == b

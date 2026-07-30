"""上游集成方报告(CI 耗时与可观测性)修复的回归：
空串 env 回落默认 / SIZE-001 门禁不被空串关闭 / ping 开关 / 交互日志时间戳 / metrics 耗时字段。"""
import importlib
import re


def _reload(mod):
    return importlib.reload(importlib.import_module("touchstone." + mod))


def test_empty_env_falls_back_to_default(monkeypatch):
    """vars.* 未创建时透传空串是 CI 常态：数值 env 空串必须回落默认，而非崩溃/静默清零。"""
    monkeypatch.setenv("TOUCHSTONE_MAX_ROUNDS", "")
    monkeypatch.setenv("GH_RETRY_MAX", "")
    monkeypatch.setenv("TOUCHSTONE_W_NOISE", "")
    try:
        assert _reload("loop").MAX_ROUNDS == 9
        assert _reload("ghclient").GH_RETRY_MAX == 5
        assert _reload("distill")._W_NOISE == 0.5
    finally:
        for k in ("TOUCHSTONE_MAX_ROUNDS", "GH_RETRY_MAX", "TOUCHSTONE_W_NOISE"):
            monkeypatch.delenv(k, raising=False)
        _reload("loop"); _reload("ghclient"); _reload("distill")


def test_max_diff_lines_empty_keeps_gate(monkeypatch):
    """上游报告问题三：空串此前经 `or 0` 静默关闭 SIZE-001 体量门禁。现空串→默认 1000，仅显式 0 关闭。"""
    from touchstone import orchestrator as O
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "")
    assert O._max_diff_lines() == 1000
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "0")
    assert O._max_diff_lines() == 0
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "250")
    assert O._max_diff_lines() == 250
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "1k")   # 评审意见：非数字（typo）→ 告警回落默认，门禁不失效、评审不崩
    assert O._max_diff_lines() == 1000
    monkeypatch.delenv("TOUCHSTONE_MAX_DIFF_LINES", raising=False)
    assert O._max_diff_lines() == 1000


def test_ping_switch(monkeypatch):
    """上游报告问题五：预检 ping 默认开（防静默故障）；确认端点健康的部署可显式关闭。"""
    from touchstone import pr_agent_runner as R
    monkeypatch.delenv("TOUCHSTONE_LLM_PING", raising=False)
    assert R._ping_enabled() is True
    monkeypatch.setenv("TOUCHSTONE_LLM_PING", "false")
    assert R._ping_enabled() is False
    monkeypatch.setenv("TOUCHSTONE_LLM_PING", "0")
    assert R._ping_enabled() is False
    # 评审意见：锁定大小写不敏感与 "no" 哨兵（防 .lower() 被误删后测试仍绿）
    for v in ("FALSE", "False", "No", "NO", " false ", "\tno"):   # 含空白变体：与全仓 env 解析同口径 strip
        monkeypatch.setenv("TOUCHSTONE_LLM_PING", v)
        assert R._ping_enabled() is False
    monkeypatch.setenv("TOUCHSTONE_LLM_PING", "true")
    assert R._ping_enabled() is True


def test_interaction_log_carries_stage_timestamps():
    """上游报告问题四：交互日志每条带 [+N.NNs] 相对耗时，阶段耗时可从产物直接读出。"""
    from touchstone import pr_agent_runner as R
    R._reset_trace()          # 评审意见：经统一入口重置，测试不持有内部结构细节
    R._ix("LLM 配置: …")
    assert re.match(r"^\[\+ *\d+\.\d{2}s\] LLM 配置", R._IX[-1])
    R._IX.clear(); R._IX_T0.clear()


def test_metrics_build_duration_fields():
    """上游报告问题四：metrics 增 t_* 耗时字段（仅收 t_ 前缀，四舍五入 2 位）。"""
    from touchstone import metrics
    rec = metrics.build({"number": 1}, "sha", {"risk_band": "low"}, [],
                        engine_status="ok", review_reliable=True, ai_raw_count=0,
                        loop_decision="converged", gate="success", unverified_claims=0,
                        change_class="low|code", added_lines=3,
                        durations={"t_review": 1.234, "t_total": 5.678, "skip_me": 9})
    assert rec["t_review"] == 1.23 and rec["t_total"] == 5.68
    assert "skip_me" not in rec

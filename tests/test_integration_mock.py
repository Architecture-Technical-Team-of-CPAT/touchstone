"""Mock 集成测试：打桩 GitHub HTTP / 网络 / 子进程 / LLM，真覆盖 main() 与 I/O 编排分支。
这些是离线无法真连的集成代码（GitHub API、子进程、LLM、CLI），用替身驱动其逻辑分支。"""
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)            # 使 `from touchstone import ...` 可解析（仅仓库根；
# 不再把 touchstone/、verify/ 子目录也插进 path——那会让"函数内平铺导入"这类地雷
# 在全量测试里被静默掩盖、单跑文件才炸，见 CHANGELOG 工程化加固第二轮）

from touchstone import ghclient as G          # noqa: E402
from touchstone import preflight as P         # noqa: E402
from touchstone import pr_agent_runner as R   # noqa: E402


# ============================ ghclient ============================
class _Resp:
    def __init__(self, status=200, text='{"ok":1}', headers=None, jsonv=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._j = {"ok": 1} if jsonv is None else jsonv

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_error(self.status_code)

    def json(self):
        return self._j


def requests_error(code):
    import requests
    return requests.HTTPError(str(code))


class _Sess:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_ghclient_make_session():
    assert G.make_session() is not None       # 构造 requests.Session（无网络）


def test_ghclient_request_json_and_diff():
    s = _Sess([_Resp(jsonv={"a": 1})])
    assert G.request("GET", "https://x", "tok", session=s) == {"a": 1}
    s = _Sess([_Resp(text="DIFFTEXT")])
    assert G.request("GET", "https://x", "tok",
                     accept="application/vnd.github.diff", session=s) == "DIFFTEXT"


def test_ghclient_403_retry_after(monkeypatch):
    monkeypatch.setattr(G.time, "sleep", lambda *_: None)
    s = _Sess([_Resp(status=403, headers={"Retry-After": "0"}), _Resp(jsonv={"ok": 2})])
    assert G.request("GET", "https://x", "tok", session=s) == {"ok": 2}
    assert len(s.calls) == 2                   # 二级限流 → 额外重试一次


# ============================ preflight ============================
def test_preflight_check_config_branches():
    env = {"GITHUB_TOKEN": "t", "LLM_BASE_URL": "u", "LLM_API_KEY": "k",
           "LLM_MODEL": "m", "LLM_TEST_MODEL": "m", "HTTPS_PROXY": "http://p"}
    names = {n for n, _, _ in P.check_config(env)}
    assert "model-diversity" in names         # LLM_MODEL == LLM_TEST_MODEL → 告警
    assert "proxy" in names
    rows2 = P.check_config({})
    assert any(n == "GITHUB_TOKEN" and not ok for n, ok, _ in rows2)   # 缺必需


def test_preflight_check_network(monkeypatch):
    monkeypatch.setattr(P, "_ping", lambda *a, **k: (True, "HTTP 200"))
    rows = P.check_network({"GITHUB_TOKEN": "t", "LLM_BASE_URL": "https://llm",
                            "LLM_MODEL": "m", "LLM_API_KEY": "k"})
    assert len(rows) == 3 and all(ok for _, ok, _ in rows)


def test_preflight_main_no_net(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["preflight", "--no-net"])
    for k in ("GITHUB_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("TOUCHSTONE_STANDARDS", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    P.main()                                   # 必需项就绪 + --no-net → 不 hard_fail
    assert "预检" in capsys.readouterr().out


def test_preflight_main_hardfail(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["preflight", "--no-net"])
    for k in ("GITHUB_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):           # 缺必需 → 退出码 1
        P.main()


# ============================ pr_agent_runner ============================
def test_pr_agent_read(tmp_path):
    assert R._read(None) is None
    p = tmp_path / "e.txt"
    p.write_text("hi", encoding="utf-8")
    assert R._read(str(p)) == "hi"


def test_pr_agent_run_import_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pr_agent", None)   # 不可导入 → 上报 no_engine（不抛、不非零退出）
    out = R.run("https://pr", "improve")
    assert out["_degraded"] == "no_engine"
    assert "pr-agent" in out["reason"]


def test_pr_agent_run_llm_failed(monkeypatch):
    # pr-agent 装了，但工具调用抛错（LLM 端点/鉴权/超时类）→ 上报 llm_failed（防静默故障）
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CrashingCS:
        def __init__(self, url):
            self.data = {}

        async def run(self):
            raise RuntimeError("LLM 401 unauthorized")
    cs_mod.PRCodeSuggestions = CrashingCS
    out = R.run("https://pr", "improve")
    assert out["_degraded"] == "llm_failed"
    assert "401" in out["reason"]


def test_pr_agent_run_llm_preflight_fails(monkeypatch):
    # LLM 预检 ping 失败（端点不可达/鉴权坏）→ 立即 llm_failed 带真实错误，不进 pr-agent
    _install_fake_pr_agent(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")

    def _boom(*a):
        raise RuntimeError("401 unauthorized")
    monkeypatch.setattr(R, "_ping_llm", _boom)
    out = R.run("https://pr", "improve")
    assert out["_degraded"] == "llm_failed"
    assert "探测失败" in out["reason"] and "401" in out["reason"]


def test_pr_agent_run_llm_config_incomplete(monkeypatch):
    # LLM 配置缺项（如没设 LLM_MODEL）→ llm_failed，明确告知缺哪个
    _install_fake_pr_agent(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    out = R.run("https://pr", "improve")
    assert out["_degraded"] == "llm_failed"
    assert "配置不全" in out["reason"]


def test_pr_agent_run_provider_failed(monkeypatch):
    # pr-agent 装了，但构造时取 PR/git provider 失败（pre-LLM）→ 上报 provider_failed，与 llm_failed 区分
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class NoProviderCS:
        def __init__(self, url):
            raise ValueError("Failed to get git provider for " + url)
    cs_mod.PRCodeSuggestions = NoProviderCS
    out = R.run("https://github.com/o/r/pull/1", "improve")
    assert out["_degraded"] == "provider_failed"
    assert "git provider" in out["reason"]


def test_pr_agent_run_maps_github_token(monkeypatch):
    # GITHUB_TOKEN → pr-agent settings.github.user_token；git_provider 显式 github
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_token")
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CS:
        def __init__(self, url):
            self.data = {"code_suggestions": []}

        async def run(self):
            return None
    cs_mod.PRCodeSuggestions = CS
    R.run("https://pr", "improve")
    assert settings.github.user_token == "ghs_test_token"
    assert settings.config.git_provider == "github"


def _install_fake_pr_agent(monkeypatch):
    algo_utils = types.ModuleType("pr_agent.algo.utils")
    algo_utils.load_yaml = lambda s, **k: {"review": {"key_issues_to_review": [{"x": 1}]}}
    cfg = types.ModuleType("pr_agent.config_loader")
    settings = types.SimpleNamespace(
        config=types.SimpleNamespace(
            publish_output=True, publish_output_progress=True,
            # 忠实复刻 pr_agent/settings/configuration.toml:34 默认值：
            #   max_model_tokens=32000（全局输入 cap）、custom_model_max_tokens=0（未声明）。
            # 让 fake 是 pr-agent 的忠实替身——既有上限测试才验得到 min 后的"有效上限"，
            # 而不是验一个不存在的默认（PR #49 真根因：第二闸 32000 把窗口压塌）。
            max_model_tokens=32000, custom_model_max_tokens=0),
        github=types.SimpleNamespace(user_token=""),
        pr_code_suggestions=types.SimpleNamespace(extra_instructions=""),
        pr_reviewer=types.SimpleNamespace(extra_instructions=""))
    cfg.get_settings = lambda: settings
    # 忠实复刻 pr_agent get_max_tokens（algo/utils.py:992-1013 契约）：
    #   base = MAX_TOKENS[model] 或 custom_model_max_tokens；若 max_model_tokens>0 取 min。
    # 把此契约固化进 fake——效果测试才能锁死"min 后的有效上限"，而非仅"runner 设了某字段"。
    # MAX_TOKENS 留空：touchstone 用自定义模型（glm-5.2 等不在 pr-agent 内置表），必走
    # custom 分支，与真实部署一致。
    algo_utils.MAX_TOKENS = {}

    def _get_max_tokens(model):
        s = cfg.get_settings().config
        if model in algo_utils.MAX_TOKENS:
            base = algo_utils.MAX_TOKENS[model]
        elif getattr(s, "custom_model_max_tokens", 0) > 0:
            base = s.custom_model_max_tokens
        else:
            raise Exception(
                f"Ensure {model} is defined in MAX_TOKENS or set config.custom_model_max_tokens")
        if getattr(s, "max_model_tokens", 0) and s.max_model_tokens > 0:
            base = min(s.max_model_tokens, base)
        return base
    algo_utils.get_max_tokens = _get_max_tokens
    cs_mod = types.ModuleType("pr_agent.tools.pr_code_suggestions")

    class PRCodeSuggestions:
        def __init__(self, url):
            self.data = {"code_suggestions": [{"s": 1}]}

        async def run(self):
            return None
    cs_mod.PRCodeSuggestions = PRCodeSuggestions
    rv_mod = types.ModuleType("pr_agent.tools.pr_reviewer")

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review:\n  key_issues_to_review: []"

        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    for name, mod in (("pr_agent", types.ModuleType("pr_agent")),
                      ("pr_agent.algo", types.ModuleType("pr_agent.algo")),
                      ("pr_agent.algo.utils", algo_utils),
                      ("pr_agent.config_loader", cfg),
                      ("pr_agent.tools", types.ModuleType("pr_agent.tools")),
                      ("pr_agent.tools.pr_code_suggestions", cs_mod),
                      ("pr_agent.tools.pr_reviewer", rv_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    return settings


def _stub_llm(monkeypatch):
    """让 runner 的 LLM 预检 ping 在离线测试里直接通过（设 LLM_* env + 打桩 _ping_llm 不真发请求）。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setattr(R, "_ping_llm", lambda *a: None)


def test_pr_agent_run_happy(monkeypatch):
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    out = R.run("https://pr", "improve+review", extra_instructions="be strict")
    assert out["code_suggestions"] == [{"s": 1}]
    assert out["review"]["key_issues_to_review"] == [{"x": 1}]
    assert out.get("_degraded") is None                     # 正常：无降级标记
    assert settings.config.publish_output is False          # 关键：不往 PR 发评论
    assert settings.pr_reviewer.extra_instructions == "be strict"


def test_pr_agent_main(monkeypatch, capsys):
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["pr_agent_runner", "--pr-url", "https://pr", "--mode", "improve"])
    R.main()
    assert "code_suggestions" in capsys.readouterr().out


def _install_fake_litellm(monkeypatch):
    """装一个可设属性的假 litellm 进 sys.modules，让 runner 的 `import litellm` 拿到它，
    从而可断言 runner 对 litellm.num_retries 等模块全局的写入（离线测试不引真 litellm）。"""
    mod = types.ModuleType("litellm")
    mod.num_retries = None
    mod.suppress_debug_info = False
    mod.set_verbose = False
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return mod


def _noop_cs(monkeypatch):
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CS:
        def __init__(self, url):
            self.data = {"code_suggestions": []}

        async def run(self):
            return None
    # 用 monkeypatch.setattr 而非直接赋值：保证测试后自动还原，即便 cs_mod 是真模块也不污染后续用例。
    monkeypatch.setattr(cs_mod, "PRCodeSuggestions", CS)


def test_pr_agent_run_neutralizes_litellm_num_retries_global(monkeypatch):
    # 【契约反转（勘误）】旧实现设 litellm.num_retries = max(1, env)。实证推翻其机制：该全局
    # 是【一次性】的——litellm 1.84 包装器首个失败即消费并重置 None（utils.py:1698），此后
    # openai client 回落默认 max_retries=2；且旧注释引用的 or-短路在 TTS 路径与 chat 无关。
    # 新契约：重试只在 tenacity 层（N 默认 0，快窗内抖动 N+1，见 test_llm_call_tuning）；
    # 此全局置 0（falsy → litellm 包装层不重试），client 内层由围栏逐调用注入 max_retries=0。
    # 本测试锁死"置 0"，防回归到会被 litellm 消费的正数。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    litellm_mod = _install_fake_litellm(monkeypatch)
    _noop_cs(monkeypatch)
    monkeypatch.delenv("TOUCHSTONE_LLM_NUM_RETRIES", raising=False)
    R.run("https://pr", "improve")
    assert litellm_mod.num_retries == 0


def test_pr_agent_run_tuning_failloud_when_handler_missing(monkeypatch, capsys):
    # fake pr_agent 树不含 algo.ai_handlers.litellm_ai_handler 子模块 → 调优安装失败必须
    # fail-loud 后继续（调优是收敛性优化，不把可用引擎搞挂；stderr 必须可见，防静默退化）。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    _install_fake_litellm(monkeypatch)
    _noop_cs(monkeypatch)
    out = R.run("https://pr", "improve")
    assert "LLM 调用调优未安装" in capsys.readouterr().err
    assert "_degraded" not in out


def test_pr_agent_run_sets_reflect_model(monkeypatch):
    # TOUCHSTONE_LLM_REFLECT_MODEL → config.model_reasoning（improve 自评专用），
    # 且【不是】fallback_models（后者保持清空）。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    _install_fake_litellm(monkeypatch)
    _noop_cs(monkeypatch)
    monkeypatch.setenv("TOUCHSTONE_LLM_REFLECT_MODEL", "glm-4.5-air")
    R.run("https://pr", "improve")
    import pr_agent.config_loader as cl
    s = cl.get_settings()
    assert s.config.model_reasoning == "openai/glm-4.5-air"
    assert s.config.fallback_models == []


def test_interaction_log_written_and_redacts_key(monkeypatch, tmp_path):
    # 完整 LLM 交互日志写入 artifact 文件，含 pr-agent 原始输出 + 配置轨迹；api_key 脱敏
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    logpath = tmp_path / "ix.log"
    monkeypatch.setenv("TOUCHSTONE_INTERACTION_LOG", str(logpath))
    out = R.run("https://pr", "improve+review", extra_instructions="x")
    R._write_interaction_log(out)
    txt = logpath.read_text(encoding="utf-8")
    assert "完整交互日志" in txt
    assert "code_suggestions" in txt          # 完整 pr-agent 输出
    assert "LLM 配置" in txt and "ping: 成功" in txt   # 交互轨迹
    assert "k" != txt  # 不写真实 api_key（_stub_llm 用了占位，但配置行只写'已设'）
    # 不设 env → 不写
    monkeypatch.delenv("TOUCHSTONE_INTERACTION_LOG", raising=False)
    R._write_interaction_log(out)   # 不抛、不写文件


# ============================ run.py（独立运行入口）============================
def _prep_repo_dir(tmp_path):
    import shutil
    (tmp_path / ".touchstone").mkdir()
    shutil.copy(os.path.join(ROOT, ".touchstone", "standards.yaml"),
                tmp_path / ".touchstone" / "standards.yaml")
    return tmp_path


_FAKE_REVIEW = {"findings": [{"rule_id": "R1", "confidence": 0.9, "file": "x.py",
                              "line": 1, "rationale": "r"}],
                "risk": {"risk_band": "low", "human_action": "skip",
                         "verification_decision": "cheap_only", "blast_radius": []}}


def test_run_main_dryrun(monkeypatch, tmp_path, capsys):
    from touchstone import run as RUN
    td = _prep_repo_dir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run", "--repo", "o/r", "--pr", "5", "--repo-dir", str(td)])
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(RUN.C, "gh", lambda *a, **k: {"head": {"sha": "abcdef123"}, "title": "T"})
    monkeypatch.setattr(RUN.C, "get_pr_diff", lambda *a, **k: "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+x\n")
    monkeypatch.setattr(RUN.C, "review_pr", lambda *a, **k: dict(_FAKE_REVIEW))
    RUN.main()
    assert "DRY-RUN" in capsys.readouterr().out


def test_run_main_post(monkeypatch, tmp_path):
    from touchstone import run as RUN
    td = _prep_repo_dir(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["run", "--repo", "o/r", "--pr", "5", "--repo-dir", str(td), "--post"])
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(RUN.C, "gh", lambda *a, **k: {"head": {"sha": "abcdef123"}, "title": "T"})
    monkeypatch.setattr(RUN.C, "get_pr_diff", lambda *a, **k: "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+x\n")
    monkeypatch.setattr(RUN.C, "review_pr", lambda *a, **k: dict(_FAKE_REVIEW))
    posted = {}
    monkeypatch.setattr(RUN.C, "post_results", lambda *a, **k: posted.setdefault("ok", True))
    RUN.main()
    assert posted.get("ok")


def test_run_checkout_success_and_fail(monkeypatch, tmp_path):
    from touchstone import run as RUN
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    d, created = RUN._checkout("o/r", "sha", "tok")
    assert created
    import shutil
    shutil.rmtree(d, ignore_errors=True)

    def boom(*a, **k):
        raise sp.CalledProcessError(1, a[0], stderr=b"nope")
    monkeypatch.setattr(sp, "run", boom)
    with pytest.raises(SystemExit):
        RUN._checkout("o/r", "sha", "tok")


def test_run_main_missing_token(monkeypatch, tmp_path):
    from touchstone import run as RUN
    monkeypatch.setattr(sys, "argv", ["run", "--repo", "o/r", "--pr", "5", "--repo-dir", str(tmp_path)])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        RUN.main()


# ============================ orchestrator ============================
from touchstone import orchestrator as ORC          # noqa: E402
from touchstone import review_provider as RP        # noqa: E402
from touchstone import govern as GOV                # noqa: E402

_RISK = {"risk_band": "low", "human_action": "skip",
         "verification_decision": "cheap_only", "blast_radius": []}


def test_render_summary_bands_and_findings():
    for band in ("high", "mid", "low"):
        r = dict(_RISK, risk_band=band, blast_radius=["schema"])
        assert "Touchstone" in ORC.render_summary(r, [])
    f = [{"rule_id": "R1", "confidence": 0.9, "agent": "pr-agent:review",
          "file": "x.py", "line": 3, "rationale": "r", "suggested_fix": "fix"}]
    assert "R1" in ORC.render_summary(_RISK, f)


def test_anchor_inline_in_diff_nearest_and_skip():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n"
    findings = [{"file": "x.py", "line": 2, "rule_id": "R", "rationale": "r"},   # 命中新增行 → 锚 2
                {"file": "x.py", "line": 99, "rule_id": "R2", "rationale": "r"}, # 越界 → 就近锚
                {"file": "y.py", "line": 1, "rule_id": "R3", "rationale": "r"}]  # 无新增行 → 跳过
    out = ORC.anchor_inline(findings, diff)
    lines = sorted(o["line"] for o in out)
    assert lines == [2, 3] and len(out) == 2


def test_ci_verdict_branches(monkeypatch):
    def mk(runs):
        return lambda *a, **k: {"check_runs": runs}
    monkeypatch.setattr(ORC, "gh", mk([{"name": "ci", "status": "completed", "conclusion": "success"}]))
    assert ORC.ci_verdict("o", "r", "s", "t") is True
    monkeypatch.setattr(ORC, "gh", mk([{"name": "ci", "status": "completed", "conclusion": "failure"}]))
    assert ORC.ci_verdict("o", "r", "s", "t") is False
    monkeypatch.setattr(ORC, "gh", mk([{"name": "ci", "status": "in_progress"}]))
    assert ORC.ci_verdict("o", "r", "s", "t") is None
    monkeypatch.setattr(ORC, "gh", mk([]))            # 仅 touchstone/无数据 → None
    assert ORC.ci_verdict("o", "r", "s", "t") is None


def test_post_results_calls_three_endpoints(monkeypatch):
    calls = []
    monkeypatch.setattr(ORC, "gh", lambda m, p, t, data=None, accept="": calls.append((m, p)) or {})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+a\n"
    f = [{"rule_id": "R1", "confidence": 0.9, "agent": "pr-agent:review",
          "file": "x.py", "line": 1, "rationale": "r", "suggested_fix": "fix"}]
    ORC.post_results("o", "r", 5, "sha", "tok", _RISK, f,
                     loop_info=("converged", "ok", "<!-- m -->"), change_class="low|code|none|none", diff=diff)
    paths = " ".join(p for _, p in calls)
    assert "/issues/5/comments" in paths and "/pulls/5/reviews" in paths and "/check-runs" in paths


def test_post_results_banner_shows_telemetry_status(monkeypatch):
    """遥测状态进报告横幅（防静默故障）：enabled(ok/failed) → 横幅含遥测行；disabled → 不含（免噪声）。
    失败原因须可见——可观测性子系统自身故障不许静默（同 alert/ironic-for-observability 约定）。"""
    captured = {}

    def fake_gh(method, path, token, data=None, accept=""):
        # 只抓摘要评论体（POST /issues/{n}/comments），其余 POST（review/check-run）忽略
        if method == "POST" and path.endswith("/comments") and isinstance(data, dict):
            captured["body"] = data.get("body", "")
        return {}

    monkeypatch.setattr(ORC, "gh", fake_gh)
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+a\n"
    f = [{"rule_id": "R1", "confidence": 0.9, "agent": "pr-agent:review",
          "file": "x.py", "line": 1, "rationale": "r", "suggested_fix": "fix"}]
    kw = dict(loop_info=("converged", "ok", "<!-- m -->"),
              change_class="low|code|none|none", diff=diff)
    # ok → 已上报
    ORC.post_results("o", "r", 5, "sha", "tok", _RISK, f, telemetry_status="ok", **kw)
    assert "遥测" in captured["body"] and "已上报" in captured["body"]
    # failed:<reason> → 上报失败 + 原因可见；"failed:" 前缀已剥（不重复）
    ORC.post_results("o", "r", 5, "sha", "tok", _RISK, f,
                     telemetry_status="failed: ConnectionError: boom", **kw)
    assert "遥测" in captured["body"] and "上报失败" in captured["body"]
    assert "boom" in captured["body"]            # 失败原因可见（防静默故障）
    assert "failed:" not in captured["body"]     # 前缀已剥
    # disabled（默认）→ 不含遥测行（免噪声）
    ORC.post_results("o", "r", 5, "sha", "tok", _RISK, f, **kw)
    assert "遥测" not in captured["body"]


def test_orchestrator_main_end_to_end(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": "abc123"},
                                                  "user": {"login": "alice"},
                                                  "author_association": "MEMBER"}}),
                     encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("TOUCHSTONE_SKIP_GATE", "1")        # 跳过总闸（checks 另测）
    monkeypatch.setenv("REPO_DIR", ROOT)
    monkeypatch.setattr(ORC, "STANDARDS_PATH", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    monkeypatch.setattr(ORC, "CONTRACT_PATH", os.path.join(tmp_path, "nope.yaml"))

    def fake_gh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        if path.endswith("/comments") and method == "GET":
            return []
        if "check-runs" in path and method == "GET":
            return {"check_runs": []}
        return {}
    monkeypatch.setattr(ORC, "gh", fake_gh)

    def _no_endpoint(pr, provider=None):                  # 触发 review_pr 的降级分支
        raise RuntimeError("PR-Agent 端点未配置")
    monkeypatch.setattr(RP, "fetch", _no_endpoint)
    ORC.main()

    out = json.loads((tmp_path / "touchstone-findings.json").read_text(encoding="utf-8"))
    assert out["pr"] == 7 and "risk" in out and out["gate"] is None
    # 作者出处持久化（autonomy 作者信任闸的输入；缺失即 fail-closed 不放行）
    assert out["author"] == {"login": "alice", "association": "MEMBER"}


def test_orchestrator_main_null_user_failclosed(monkeypatch, tmp_path):
    # PRA-POSSIBLE_ISSUE(#104 round-2)：事件 payload 的 "user": null（key 在、值空）时，
    # pr.get("user", {}) 只挡 key 缺失、挡不住 None → None.get("login") 崩 main()。
    # `or {}` 同时兜底两种情形；作者出处落 None → autonomy 作者闸 fail-closed 不放行。
    monkeypatch.chdir(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 9, "head": {"sha": "n9"},
                                                  "user": None,
                                                  "author_association": None}}),
                     encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("TOUCHSTONE_SKIP_GATE", "1")
    monkeypatch.setenv("REPO_DIR", ROOT)
    monkeypatch.setattr(ORC, "STANDARDS_PATH", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    monkeypatch.setattr(ORC, "CONTRACT_PATH", os.path.join(tmp_path, "nope.yaml"))

    def fake_gh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        if path.endswith("/comments") and method == "GET":
            return []
        if "check-runs" in path and method == "GET":
            return {"check_runs": []}
        return {}
    monkeypatch.setattr(ORC, "gh", fake_gh)

    def _no_endpoint(pr, provider=None):                  # 触发 review_pr 的降级分支
        raise RuntimeError("PR-Agent 端点未配置")
    monkeypatch.setattr(RP, "fetch", _no_endpoint)
    ORC.main()                                            # 旧实现在此抛 AttributeError

    out = json.loads((tmp_path / "touchstone-findings.json").read_text(encoding="utf-8"))
    # null user → login/association 落 None（fail-closed），不崩溃、不误信
    assert out["author"] == {"login": None, "association": None}


def _setup_main(monkeypatch, tmp_path, fake_gh_fn):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "event.json").write_text(
        json.dumps({"pull_request": {"number": 8, "head": {"sha": "sha8"}}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    monkeypatch.setenv("REPO_DIR", ROOT)
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setattr(ORC, "STANDARDS_PATH", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    monkeypatch.setattr(ORC, "CONTRACT_PATH", str(tmp_path / "nope.yaml"))
    monkeypatch.setattr(ORC, "gh", fake_gh_fn)
    monkeypatch.setattr(RP, "fetch", lambda pr, provider=None: [])


def test_main_rdjson_and_output(monkeypatch, tmp_path):
    def fgh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        return [] if (path.endswith("/comments") and method == "GET") else {}
    _setup_main(monkeypatch, tmp_path, fgh)
    monkeypatch.setenv("TOUCHSTONE_SKIP_GATE", "1")
    monkeypatch.setenv("TOUCHSTONE_RDJSON_PATH", str(tmp_path / "out.rdjson"))
    gho = tmp_path / "gho.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    ORC.main()
    assert (tmp_path / "out.rdjson").exists()
    assert "verification_decision=" in gho.read_text(encoding="utf-8")


def test_main_gate_path(monkeypatch, tmp_path):
    def fgh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        return [] if (path.endswith("/comments") and method == "GET") else {}
    _setup_main(monkeypatch, tmp_path, fgh)
    monkeypatch.delenv("TOUCHSTONE_SKIP_GATE", raising=False)   # 走 gate 路径
    monkeypatch.setattr(ORC.checks, "post_gate",
                        lambda pr, cfg, res: ("success", res))
    monkeypatch.setattr(ORC.checks, "run_checks", lambda cfg, pr: [])
    ORC.main()
    out = json.loads((tmp_path / "touchstone-findings.json").read_text(encoding="utf-8"))
    assert out["gate"] == "success"


def test_main_gate_crash_does_not_suppress_review(monkeypatch, tmp_path):
    """总闸块抛【非 RequestException】（如 checks.py 未来改动引入的编程错误 / 插件聚合异常）时，
    post_results 仍须执行、评审评论仍须投递。gate 块上移到 post_results 之前（PR #71）后，gate 的
    except 必须 catch 到 Exception——否则非网络异常向上冒泡、post_results 永不执行，评审评论被
    静默吞（PRA-REVIEW:orchestrator.py:473 行为回归）。锁既有契约：总闸崩溃不阻断评审交付。"""
    posted = {}

    def fgh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        if method == "POST" and path.endswith("/comments") and isinstance(data, dict):
            posted["body"] = data.get("body", "")
        return [] if (path.endswith("/comments") and method == "GET") else {}

    _setup_main(monkeypatch, tmp_path, fgh)
    monkeypatch.delenv("TOUCHSTONE_SKIP_GATE", raising=False)   # 走 gate 路径
    # run_checks 抛非网络异常（模拟 checks.py 编程错误）——作为 post_gate 的实参先求值，
    # gate 块的 except 必须兜住它，否则冒泡越过 post_results。
    def _boom(cfg, pr):
        raise RuntimeError("checks.py 内部错误（非网络）")
    monkeypatch.setattr(ORC.checks, "run_checks", _boom)
    ORC.main()                       # gate 崩溃被宽 except 吞，post_results 仍执行
    # 评审评论已投递（未被静默吞）+ 落盘完成（证明越过 gate 走到 main 尾部）
    assert posted.get("body"), "总闸崩溃不应静默吞掉评审评论"
    assert (tmp_path / "touchstone-findings.json").exists()
    out = json.loads((tmp_path / "touchstone-findings.json").read_text(encoding="utf-8"))
    assert out["gate"] is None       # gate 崩溃 → 保持 None（未算出）


def test_main_post_results_fail_swallowed(monkeypatch, tmp_path):
    # 摘要评论/内联/check-run POST 失败 → 走 warn/info 分支，main 不崩
    import requests
    def fgh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        if path.endswith("/comments") and method == "GET":
            return []
        if "check-runs" in path and method == "GET":
            return {"check_runs": []}
        raise requests.exceptions.HTTPError(f"403 forbidden: {path}")
    _setup_main(monkeypatch, tmp_path, fgh)
    monkeypatch.setenv("TOUCHSTONE_SKIP_GATE", "1")
    ORC.main()                       # 各 POST 失败被吞，落盘仍完成
    assert (tmp_path / "touchstone-findings.json").exists()


def test_main_telemetry_status_flows_into_report(monkeypatch, tmp_path):
    """main() 全链：遥测启用且 forward 返回 ok → 评审报告横幅含遥测行（防静默故障：状态不只进 stderr）。
    锁 gate+可观测性上移到 post_results 之前、_tel_res 贯通到横幅的重构——若顺序回退或 _tel_res 未传，
    此处断言失败。"""
    import touchstone.telemetry as _tel_mod
    posted = {}

    def fgh(method, path, token, data=None, accept="application/vnd.github+json"):
        if accept.endswith("diff"):
            return "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+x\n"
        if method == "POST" and path.endswith("/comments") and isinstance(data, dict):
            posted["body"] = data.get("body", "")
        return [] if (path.endswith("/comments") and method == "GET") else {}

    _setup_main(monkeypatch, tmp_path, fgh)
    monkeypatch.setenv("TOUCHSTONE_SKIP_GATE", "1")
    monkeypatch.setenv("TOUCHSTONE_TELEMETRY_ENDPOINT", "https://sink.example/api")
    monkeypatch.setattr(_tel_mod, "forward", lambda records, env, **kw: "ok")
    ORC.main()
    assert "遥测" in posted["body"] and "已上报" in posted["body"]


# ============================ govern.main ============================
def test_govern_main(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cal = {"aggregate": {"by_rule": {}}, "records": []}
    (tmp_path / "calibration.json").write_text(json.dumps(cal), encoding="utf-8")
    monkeypatch.setenv("CALIBRATION_JSON", str(tmp_path / "calibration.json"))
    monkeypatch.setenv("TOUCHSTONE_STANDARDS", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    monkeypatch.setattr(GOV, "detect_revert_shas", lambda *a, **k: set())   # 不打 git
    GOV.main()
    assert (tmp_path / "promotion-proposal.md").exists()
    assert (tmp_path / "autonomy-state.json").exists()


def test_govern_main_fail_closed_when_revert_scan_unavailable(monkeypatch, tmp_path):
    # detect_revert_shas 扫描失败(None) + 窗口内有自动放行 PR → govern.main 写出 tripped=True
    # （失败收敛，非谎报健康）。锁 main() 透传 revert_data_available 的接线。
    monkeypatch.chdir(tmp_path)
    cal = {"aggregate": {"by_rule": {}}, "records": [
        {"pr": 1, "merged": True, "merge_commit_sha": "abc12345", "auto_handled": True}]}
    (tmp_path / "calibration.json").write_text(json.dumps(cal), encoding="utf-8")
    monkeypatch.setenv("CALIBRATION_JSON", str(tmp_path / "calibration.json"))
    monkeypatch.setenv("TOUCHSTONE_STANDARDS", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    monkeypatch.setattr(GOV, "detect_revert_shas", lambda *a, **k: None)   # 扫描失败
    with pytest.raises(SystemExit) as ei:                                # tripped → main() sys.exit(2)
        GOV.main()
    assert ei.value.code == 2
    state = json.loads((tmp_path / "autonomy-state.json").read_text(encoding="utf-8"))
    assert state["tripped"] is True and state["revert_rate"] is None


def test_govern_main_survives_empty_yaml_and_null_json(monkeypatch, tmp_path):
    """PR #136（TEST-001）：空/注释-only YAML 与 null JSON 不能让 govern.main 崩。
    回归锁：yaml.safe_load 对空/注释-only 文件返回 None；json.load 对 ``null`` 字面返回 None。
    修复前 ``standards.get('rules', [])`` / ``cal.get('aggregate', {})`` 在 None 上 AttributeError；
    ``or {}`` 兜底后 main() 平稳落盘两份产物（无固化候选、熔断不跳）。"""
    monkeypatch.chdir(tmp_path)
    # null JSON：json.load → None；or {} → {}，cal.get 不再崩
    (tmp_path / "calibration.json").write_text("null", encoding="utf-8")
    monkeypatch.setenv("CALIBRATION_JSON", str(tmp_path / "calibration.json"))
    # 空（仅注释）YAML：yaml.safe_load → None；or {} → {}，standards.get 不再崩
    empty_std = tmp_path / "standards.yaml"
    empty_std.write_text("# 仅注释、零规则\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_STANDARDS", str(empty_std))
    monkeypatch.setattr(GOV, "detect_revert_shas", lambda *a, **k: set())   # 不打 git
    GOV.main()                                                              # 不抛 AttributeError
    proposal = (tmp_path / "promotion-proposal.md").read_text(encoding="utf-8")
    assert "无达阈值的固化候选" in proposal                                  # 空规则→无候选，落正常分支
    state = json.loads((tmp_path / "autonomy-state.json").read_text(encoding="utf-8"))
    assert state["tripped"] is False                                        # 无 records → 不熔断


def test_govern_main_survives_null_autonomy_prev(monkeypatch, tmp_path):
    """#136 review（govern.py:181）：autonomy-prev.json 内容为 ``null`` 时 json.load → None，
    原 ``json.load(f).get('approval_rate')`` 在 None 上 AttributeError（不在 (JSONDecodeError,KeyError) 捕获域）。
    ``(json.load(f) or {}).get(...)`` 兜底后 main() 平稳读出 prior=None。回归锁：去 or {} 即在 .get 上崩。"""
    monkeypatch.chdir(tmp_path)
    cal = {"aggregate": {"by_rule": {}}, "records": []}
    (tmp_path / "calibration.json").write_text(json.dumps(cal), encoding="utf-8")
    monkeypatch.setenv("CALIBRATION_JSON", str(tmp_path / "calibration.json"))
    monkeypatch.setenv("TOUCHSTONE_STANDARDS", os.path.join(ROOT, ".touchstone", "standards.yaml"))
    (tmp_path / "autonomy-prev.json").write_text("null", encoding="utf-8")   # null → json.load 返 None
    monkeypatch.setattr(GOV, "detect_revert_shas", lambda *a, **k: set())
    GOV.main()                                                               # 不抛 AttributeError
    assert (tmp_path / "autonomy-state.json").exists()


@pytest.mark.parametrize("module,fn,kind", [
    ("checks", "load_config", "dict"),
    ("contract_check", "load_scope_rules", "dict"),
    ("review_provider", "load_nmap", "dict"),
    ("gen_best_practices", "generate", "str"),
])
def test_yaml_loaders_empty_file_returns_safe_value_not_none(tmp_path, monkeypatch, module, fn, kind):
    """PR #136（TEST-001）：全仓裸 open()→with 扫描把 4 个 YAML 加载站的 ``or {}`` 搬进 with 块。
    不变量锁：空/注释-only YAML（safe_load → None）必须兜底成空字典，绝不把 None 透传到下游 ``.get``。
    任一站点误删 ``or {}`` 都会在此复现 ``AttributeError: 'NoneType' ... .get``。"""
    import importlib
    m = importlib.import_module(f"touchstone.{module}")
    ts = tmp_path / ".touchstone"; ts.mkdir()
    target = {
        "checks":              tmp_path / "checks.yaml",
        "contract_check":      ts / "scope-rules.yaml",
        "review_provider":     ts / "pr-agent.yaml",
        "gen_best_practices":  tmp_path / "standards.yaml",
    }[module]
    target.write_text("# 仅注释、零键\n", encoding="utf-8")                # safe_load → None
    # hermetic（#136 review #2）：清可能劫持默认路径的 env——review_provider.load_nmap 读 TOUCHSTONE_PRAGENT
    # 先于默认路径，CI/前序测试若设了它，本用例会读错文件；checks 则显式 setenv 覆盖到 tmp 目标。
    monkeypatch.delenv("TOUCHSTONE_PRAGENT", raising=False)
    if module == "checks":
        monkeypatch.setenv("TOUCHSTONE_CHECKS", str(target))
    if module == "gen_best_practices":
        out = getattr(m, fn)(str(target))
    else:
        out = getattr(m, fn)(str(tmp_path))
    assert out is not None, f"{module}.{fn} 对空 YAML 返回 None——``or {{}}`` 兜底丢了"
    assert isinstance(out, dict if kind == "dict" else str)


# ============================ calibrate ============================
from touchstone import calibrate as CAL            # noqa: E402
from touchstone import autonomy as AUT             # noqa: E402

_FINDING_MARKER = '<!-- touchstone-finding: {"rule_id": "R", "agent": "pr-agent:review"} -->'
_RESULT_MARKER = ("<!-- touchstone-result: " +
                  json.dumps({"risk_band": "low", "verification_decision": "cheap_only",
                              "change_class": None, "loop_decision": None, "findings": []}) + " -->")


def test_calibrate_pure_parsers():
    data = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        {"isResolved": True, "comments": {"nodes": [{"author": {"login": "github-actions[bot]"}, "body": _FINDING_MARKER}]}}]}}}}}
    threads = CAL.parse_review_threads(data)
    assert threads[0]["isResolved"] and threads[0]["comments"][0]["author"] == "github-actions[bot]"
    tf = CAL.thread_findings(threads)
    assert tf[0]["rule_id"] == "R" and tf[0]["resolved"] is True
    assert CAL._parse_result([_RESULT_MARKER], "bot")["risk_band"] == "low"
    assert CAL._human_verdict([{"user": {"login": "alice"}, "state": "APPROVED"},
                               {"user": {"login": "bot[bot]"}, "state": "CHANGES_REQUESTED"}], "bot") == "APPROVED"
    rec = CAL.record_calibration(5, {"findings": [{"rule_id": "R"}], "risk": {"risk_band": "high"}},
                                 {"state": "CHANGES_REQUESTED"})
    assert rec["agreement"] is True and rec["touchstone_band"] == "high"


def test_calibrate_gh_gql_and_fetch(monkeypatch):
    monkeypatch.setattr(CAL.ghclient, "request", lambda m, u, t, **k: {"m": m, "u": u})
    assert CAL.gh("/x", "tok")["m"] == "GET"
    assert CAL.gql("Q", {"v": 1}, "tok")["m"] == "POST"
    monkeypatch.setattr(CAL, "gql", lambda q, v, t: {"data": {"repository": {"pullRequest":
                        {"reviewThreads": {"nodes": []}}}}})
    assert CAL.fetch_review_threads("o", "r", 1, "t") == []


def test_calibrate_main(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    def fake_gh(path, token):
        if "/pulls?state=closed" in path:
            return [{"number": 1, "merged_at": "t", "merge_commit_sha": "sha"}]
        if "/issues/1/comments" in path:
            return [{"body": _RESULT_MARKER, "user": {"login": "github-actions[bot]"}}]
        if "/pulls/1/reviews" in path:
            return [{"user": {"login": "alice"}, "state": "APPROVED"}]
        return []
    monkeypatch.setattr(CAL, "gh", fake_gh)
    monkeypatch.setattr(CAL, "gh_paginate", fake_gh)
    monkeypatch.setattr(CAL, "fetch_review_threads", lambda *a: [])
    CAL.main()
    rep = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert rep["aggregate"]["total"] == 1 and (tmp_path / "calibration-report.md").exists()


def test_calibrate_main_excludes_author_self_resolve(monkeypatch, tmp_path, capsys):
    """calibrate.main() 须传 pr_author 给 thread_findings——作者自 resolve 自己 PR 的发现线程
    不算采纳，否则 finding_adoption 被污染、adoption_rate 虚高致 graduate/retire 误判。
    与 build_ground_truth 同一 bug 类。锁死调用点 pr_author 透传：删参数（变异）→
    finding_adoption[0].resolved 变 True → 本测红。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    body = ('<!-- touchstone-finding: '
            + json.dumps({'rule_id': 'PRA-X', 'agent': 'pr-agent:review'}) + ' -->')
    threads = [{'isResolved': True, 'resolved_by': 'author1',     # 作者 = 线程解决者
                'comments': [{'author': 'github-actions[bot]', 'body': body}]}]

    def fake_gh(path, token):
        if "/pulls?state=closed" in path:
            return [{"number": 1, "merged_at": "t", "merge_commit_sha": "sha",
                     "user": {"login": "author1"}}]
        if "/issues/1/comments" in path:
            return [{"body": _RESULT_MARKER, "user": {"login": "github-actions[bot]"}}]
        if "/pulls/1/reviews" in path:
            return [{"user": {"login": "alice"}, "state": "APPROVED"}]
        return []
    monkeypatch.setattr(CAL, "gh", fake_gh)
    monkeypatch.setattr(CAL, "gh_paginate", fake_gh)
    monkeypatch.setattr(CAL, "fetch_review_threads", lambda *a: threads)
    CAL.main()
    rep = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    fa = rep["records"][0]["finding_adoption"]
    assert fa and fa[0]["rule_id"] == "PRA-X"
    assert fa[0]["resolved"] is False     # 作者自 resolve → 不算采纳（传 pr_author 才成立）


# ============================ autonomy main / execute ============================
def test_autonomy_main_graduate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    recs = [{"merged": True, "change_class": "x", "loop_decision": "converged",
             "findings": [], "risk_band": "low", "merge_commit_sha": "s"} for _ in range(20)]
    (tmp_path / "calibration.json").write_text(json.dumps({"records": recs}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["autonomy", "--graduate"])
    AUT.main()
    grad = json.loads((tmp_path / "graduated-classes.json").read_text(encoding="utf-8"))
    assert "x" in grad["graduated_classes"]


# ============================ #90 round-2: OUTPUT_DIR / CALIBRATION_JSON 读写一致性 ============================
def test_calibrate_main_writes_to_calibration_json_override(monkeypatch, tmp_path):
    """#90 finding calibrate.py:328 — 设 CALIBRATION_JSON 时写方须写到该路径（与读方
    govern/autonomy 的 override_env 对齐）。旧代码漏 override_env → 写 OUTPUT_DIR/calibration.json
    而读方读 CALIBRATION_JSON，毕业判据建在错文件上。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    custom = tmp_path / "sub" / "my-cal.json"            # 非默认名+子目录，排除巧合
    monkeypatch.setenv("CALIBRATION_JSON", str(custom))

    def fake_gh(path, token):
        if "/pulls?state=closed" in path:
            return [{"number": 1, "merged_at": "t", "merge_commit_sha": "sha"}]
        if "/issues/1/comments" in path:
            return [{"body": _RESULT_MARKER, "user": {"login": "github-actions[bot]"}}]
        if "/pulls/1/reviews" in path:
            return [{"user": {"login": "alice"}, "state": "APPROVED"}]
        return []
    monkeypatch.setattr(CAL, "gh", fake_gh)
    monkeypatch.setattr(CAL, "gh_paginate", fake_gh)
    monkeypatch.setattr(CAL, "fetch_review_threads", lambda *a: [])
    CAL.main()
    assert custom.exists(), "calibrate 未写到 CALIBRATION_JSON 指定路径（写方漏 override_env）"
    assert not (tmp_path / "calibration.json").exists(), "写了默认名而非 CALIBRATION_JSON 覆盖"


def test_autonomy_graduate_reads_calibration_json_override(monkeypatch, tmp_path):
    """#90 finding autonomy.py:278 — 设 CALIBRATION_JSON 时读方须从该路径读（与写方 calibrate
    override_env 对齐）。CWD 放一份【空】calibration.json 作诱饵：读方若漏 override_env 会读到
    空记录→graduate 空；正确读 CALIBRATION_JSON 才拿到 20 条 x→graduate 出 x。"""
    monkeypatch.chdir(tmp_path)
    custom = tmp_path / "sub" / "my-cal.json"
    custom.parent.mkdir(parents=True)
    recs = [{"merged": True, "change_class": "x", "loop_decision": "converged",
             "findings": [], "risk_band": "low", "merge_commit_sha": "s"} for _ in range(20)]
    custom.write_text(json.dumps({"records": recs}), encoding="utf-8")
    monkeypatch.setenv("CALIBRATION_JSON", str(custom))
    (tmp_path / "calibration.json").write_text(json.dumps({"records": []}), encoding="utf-8")  # 诱饵
    monkeypatch.setattr(sys, "argv", ["autonomy", "--graduate"])
    AUT.main()
    grad = json.loads((tmp_path / "graduated-classes.json").read_text(encoding="utf-8"))
    assert "x" in grad["graduated_classes"], "autonomy 未从 CALIBRATION_JSON 读校准（读方漏 override_env）"


def test_autonomy_decision_reads_graduated_classes_from_output_dir(monkeypatch, tmp_path, capsys):
    """#90/#122 OUTPUT_DIR 统一漏掉的读点 autonomy.py:340 —— 决策模式读 graduated-classes.json 曾走
    【裸文件名】，而写方（autonomy.py:322 --graduate）走 artifact_path。设 TOUCHSTONE_OUTPUT_DIR 时
    写进隔离目录、读方却去 CWD 找 → 读不到 → graduated_classes 空 → class_graduated 永远 fail
    （autonomy 对该部署彻底失效，且是静默失效）。CWD 放【空】graduated-classes.json 作诱饵：读方若漏
    artifact_path 会读诱饵→class_graduated fail；正确读 OUTPUT_DIR 才拿到类→class_graduated pass。"""
    out = tmp_path / "iso"
    out.mkdir()
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", str(out))
    monkeypatch.chdir(tmp_path)                          # CWD 下放【空】诱饵（非 OUTPUT_DIR）
    (tmp_path / "graduated-classes.json").write_text(
        json.dumps({"graduated_classes": []}), encoding="utf-8")          # 诱饵：空
    (out / "graduated-classes.json").write_text(
        json.dumps({"graduated_classes": ["low|code|none|none"]}), encoding="utf-8")  # 真值在 OUTPUT_DIR
    co = {"gate": "success", "risk": {"risk_band": "low"}, "findings": [], "loop_decision": "converged",
          "change_class": "low|code|none|none", "review_reliable": True, "unverified_claims": 0,
          "added_lines": 10, "author": {"login": "alice", "association": "MEMBER"}, "pr": 1, "sha": "abc"}
    (out / "touchstone-findings.json").write_text(json.dumps(co), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["autonomy"])       # 决策模式（无 --inputs）→ 走 graduated-classes 读路径
    AUT.main()
    dec = json.loads(capsys.readouterr().out)
    assert "class_graduated" not in dec["failed"], \
        "决策模式未从 OUTPUT_DIR 读 graduated-classes（读方漏 artifact_path，读到 CWD 空诱饵→class_graduated fail）"


def test_check_verify_reads_result_from_output_dir(monkeypatch, tmp_path):
    """#90 finding checks.py:190 — _check_verify 须与写出方 verify_change.py 同路径（artifact_path）。
    设 TOUCHSTONE_OUTPUT_DIR 时 verify-result.json 落隔离目录；读方也须去那找，否则静默记
    'verify 未运行' 把 verify 结论漏出总闸。"""
    import json as _json
    from touchstone import checks
    out = tmp_path / "iso"
    out.mkdir()
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", str(out))
    monkeypatch.chdir(tmp_path)                          # CWD 下【故意不放】verify-result.json
    (out / "verify-result.json").write_text(
        _json.dumps({"passed": True, "spec_source": "human_curated"}), encoding="utf-8")
    passed, msg = checks._check_verify({}, {})
    assert passed is True, f"未从 OUTPUT_DIR 读到 verify 结果（读方漏 artifact_path）: {msg}"


def test_autonomy_main_decision_inputs(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    inp = {"risk": {"risk_band": "low"}, "findings": [], "loop_decision": "converged",
           "gate": "success", "autonomy_state": {"tripped": False},
           "graduated_classes": ["low|code|none|none"], "cls": "low|code|none|none"}
    (tmp_path / "in.json").write_text(json.dumps(inp), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["autonomy", "--inputs", str(tmp_path / "in.json")])
    AUT.main()
    assert "merge" in capsys.readouterr().out


def test_autonomy_main_noop(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["autonomy"])     # 无 touchstone-findings.json → no-op
    AUT.main()
    assert "no-op" in capsys.readouterr().out


class _UR:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_autonomy_execute_auto_merge(monkeypatch):
    seen = []

    def fake_req(method, url, token, data=None, **kw):
        seen.append(url)
        return {"merged": True}
    monkeypatch.setattr(AUT.ghclient, "request", fake_req)
    res = AUT.execute_auto_merge("o/r", 5, "sha", "tok")
    assert res == {"merged": True} and any("/merge" in u for u in seen)


def test_execute_auto_merge_posts_marker_comment(monkeypatch):
    """P0-5: execute_auto_merge 必须发 touchstone:auto_handled marker 评论
    （calibrate 据此重建自动放行归因；marker 丢了 → 熔断数据全错）。"""
    posts = []

    def fake_req(method, url, token, data=None, **kw):
        import json as _j
        posts.append((url, _j.dumps(data or {}, ensure_ascii=False)))
        return {"merged": True}
    monkeypatch.setattr(AUT.ghclient, "request", fake_req)
    AUT.execute_auto_merge("o/r", 5, "sha", "tok")
    comment_posts = [(u, b) for u, b in posts if "/issues/5/comments" in u]
    assert comment_posts, "未发 auto_handled marker 评论"
    assert "touchstone:auto_handled" in comment_posts[0][1]


# ============================ verify_change（mock LLM/runner，不碰子进程）========
from verify import verify_change as V          # noqa: E402


def test_verify_extract_code_and_interface(tmp_path):
    assert V._extract_code("```python\nx=1\n```") == "x=1"
    assert V._extract_code("bare code") == "bare code"
    (tmp_path / "m.py").write_text("def foo(a, b):\n    return a + b\n", encoding="utf-8")
    assert "foo" in str(V._extract_interface(str(tmp_path), ["m.py"]))


def test_verify_generate_spec_blind_tests(monkeypatch):
    monkeypatch.setattr(V, "_llm", lambda messages, **cfg: "```python\ndef test_x():\n    assert True is True\n```")
    cfg = {"base_url": "u", "api_key": "k", "model": "m"}
    ts = V.generate_spec_blind_tests(["adds two numbers"], "def add(a, b)", cfg)
    assert ts.source == "spec_blind" and "test_x" in ts.code and ts.author_model == "m"
    ts2 = V.generate_spec_blind_tests(["x"], "class Foo", cfg, framework="junit5")   # junit 分支
    assert ts2.code


def test_verify_select_runner_and_acceptance(monkeypatch, tmp_path):
    assert isinstance(V.select_runner(str(tmp_path), ["x.java"]), V.MavenRunner)
    assert isinstance(V.select_runner(str(tmp_path), ["x.py"]), V.PythonRunner)
    af = tmp_path / "acc.yaml"
    af.write_text("acceptance_criteria:\n  - does X\n", encoding="utf-8")
    monkeypatch.setenv("TOUCHSTONE_ACCEPTANCE", str(af))
    crit, src = V.resolve_acceptance_spec({}, str(tmp_path))
    assert src == "human_curated" and crit == ["does X"]
    monkeypatch.delenv("TOUCHSTONE_ACCEPTANCE", raising=False)
    crit2, src2 = V.resolve_acceptance_spec({"acceptance_criteria": ["a"]}, str(tmp_path / "none"))
    assert src2 == "author_proposed" and crit2 == ["a"]


def test_verify_regression_path_with_fake_runner(monkeypatch, tmp_path):
    class FakeRunner:
        lang = "python"

        def run_suite(self, d):
            return True, "ok"

        def changed_coverage(self, d, cf, ch):
            return 0.9

        def mutation(self, d, cf):
            return 0.8
    monkeypatch.setattr(V, "_worktree", lambda repo, ref: str(tmp_path))
    monkeypatch.setattr(V, "_rm_worktree", lambda *a, **k: None)
    monkeypatch.setattr(V, "_changed_lines", lambda repo, b, h: {"x.py": {1}})
    res = V._verify_regression(str(tmp_path), FakeRunner(), ["x.py"], "base", "head", "targeted_tests")
    assert res.passed and res.mode == "regression_only"


# ---------------- pr_agent_runner 健壮性：glm-5.2 修复不能静默回退 ----------------
def test_custom_max_tokens_uses_context_window_not_output(monkeypatch):
    # 【输入侧预算，非输出】custom_model_max_tokens 必须取 context_tokens（上下文窗口），
    # 不是 output_tokens。设 CONTEXT_TOKENS=200000、OUTPUT_TOKENS=4096 → 应取 200000。
    # 锁死此语义：若误用 output_tokens，会拿 4096 当窗口→diff 裁空→LLM 0 建议（PR #44 真根因）。
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    monkeypatch.setenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", "200000")
    monkeypatch.setenv("TOUCHSTONE_LLM_OUTPUT_TOKENS", "4096")
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CS:
        def __init__(self, url):
            self.data = {"code_suggestions": []}
        async def run(self):
            return None
    cs_mod.PRCodeSuggestions = CS
    R.run("https://pr", "improve")
    assert settings.config.custom_model_max_tokens == 200000   # 取上下文窗口，不是输出 4096
    assert settings.config.max_model_tokens == 200000          # 第二闸：否则被 pr-agent 默认 32000 min 掉，diff 被裁（本轮真根因）


def test_custom_max_tokens_context_unset_falls_back_to_128k(monkeypatch):
    # CONTEXT_TOKENS 未声明（0）→ 回退 128000（现代模型典型窗口），绝不回退 4096。
    # 4096 当窗口 = 改动 diff 被裁空。此回退值是"宁可让 LLM 看全 diff"的安全默认。
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    monkeypatch.delenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", raising=False)
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CS:
        def __init__(self, url):
            self.data = {"code_suggestions": []}
        async def run(self):
            return None
    cs_mod.PRCodeSuggestions = CS
    R.run("https://pr", "improve")
    assert settings.config.custom_model_max_tokens == 128000   # 回退 128k，不是 4096
    assert settings.config.max_model_tokens == 128000          # 第二闸同样回退，不留 pr-agent 默认 32000


def test_runner_emit_wraps_json_with_sentinels():
    # runner 的结构化输出用 _JSON_BEGIN/_JSON_END 哨兵包裹，父进程 _extract_json 按哨兵提取
    # （防 litellm/pr-agent 延迟 print 污染 stdout，PR #49 no_engine 真根因）。
    import io, json as _j
    out = {"code_suggestions": [{"s": 1}], "review": {"key_issues_to_review": []}}
    buf = io.StringIO()
    R._emit_json(out, buf)
    text = buf.getvalue()
    assert R._JSON_BEGIN in text and R._JSON_END in text
    inner = text.split(R._JSON_BEGIN, 1)[1].split(R._JSON_END, 1)[0]
    assert _j.loads(inner) == out


def test_custom_max_tokens_and_fallback_actually_set(monkeypatch):
    # 锁死：run 后 custom_model_max_tokens 已设（取 context_tokens）、fallback_models 已清空。
    # 这两项正是 glm-5.2 出真实意见的根因修复——若静默失败会回到"0 建议"。
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    monkeypatch.setenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", "3333")
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CS:
        def __init__(self, url):
            self.data = {"code_suggestions": []}
        async def run(self):
            return None
    cs_mod.PRCodeSuggestions = CS
    R.run("https://pr", "improve+review")
    assert settings.config.custom_model_max_tokens == 3333
    assert settings.config.fallback_models == []             # 清空，不再试不存在的 gpt-5.4-mini
    assert settings.config.model == "openai/m"               # provider 前缀就位
    assert settings.config.git_provider == "github"


def test_effective_max_tokens_reflects_full_context_window(monkeypatch):
    # 【验效果，非意图】runner 设完 config 后，pr-agent get_max_tokens(model) 必须返回完整
    # 上下文窗口——不只看 custom_model_max_tokens / max_model_tokens 各自被设了，而看两者
    # min 之后的【有效上限】。这正是 PR #49 的真根因：只设 custom、第二闸 max_model_tokens
    # 留 pr-agent 默认 32000，min 后有效上限塌成 32000 → 大 PR diff 被裁（run 29082805842）。
    # 若有人删掉 runner 里 `s.config.max_model_tokens = window` 那行，本测试即破。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    monkeypatch.setenv("TOUCHSTONE_LLM_CONTEXT_TOKENS", "200000")
    import pr_agent.tools.pr_code_suggestions as cs_mod

    class CS:
        def __init__(self, url):
            self.data = {"code_suggestions": []}
        async def run(self):
            return None
    cs_mod.PRCodeSuggestions = CS
    R.run("https://pr", "improve")
    import pr_agent.algo.utils as au
    # 有效上限 = min(max_model_tokens, custom) = min(200000, 200000) = 200000，非 32000
    assert au.get_max_tokens("openai/m") == 200000


def test_effective_cap_collapses_if_second_gate_at_default(monkeypatch):
    # 【灵敏度对照】模拟 PR #49 之前的 bug 形态：custom=200000 设了，但 max_model_tokens
    # 留 pr-agent 默认 32000（未被 runner 覆盖）→ get_max_tokens min 后塌成 32000。
    # 证明第二闸是 load-bearing，且上方正向测试对"漏设 max_model_tokens"这一类回归敏感。
    settings = _install_fake_pr_agent(monkeypatch)
    settings.config.custom_model_max_tokens = 200000
    settings.config.max_model_tokens = 32000        # pr-agent 默认，未被 runner 覆盖
    import pr_agent.algo.utils as au
    assert au.get_max_tokens("openai/m") == 32000   # 第二闸把窗口压回 32000


def test_runner_malformed_review_prediction_does_not_crash(monkeypatch):
    # pr-agent 评审意见：_rv 为 truthy 非 dict（malformed YAML 解析出的 string/list）时，
    # 旧实现 (_rv or {}).get 在非 dict 上抛 AttributeError 致 runner 崩溃。
    # 修复：检测到 malformed 后 _rv = {} 重置。本测试锁死不崩 + 返回空 key_issues。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    # load_yaml 返回 review 段是 string（malformed），触发 not isinstance(_rv, dict)
    algo_utils.load_yaml = lambda s, **k: {"review": "not-a-dict-malformed"}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review: garbage that parses to non-dict"
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    out = R.run("https://pr", "review")          # 不应抛 AttributeError
    assert out["review"]["key_issues_to_review"] == []   # malformed -> 空清单，不崩
    assert "_degraded" not in out                        # 非 _degraded（malformed 是可恢复的）


def test_runner_emits_engaged_for_substantive_review(monkeypatch):
    # glm 审完无问题：review 段有多段实质性内容（effort/security/relevant_tests）但 key_issues 空。
    # runner 应置 _engaged=True，让 review_reliable 把它认作"审完无问题"而非"裁空/吞没"（PR #51）。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    algo_utils.load_yaml = lambda s, **k: {"review": {
        "key_issues_to_review": [],
        "estimated_effort_to_review": "2",
        "relevant_tests": "Yes",
        "security_concerns": "No",
    }}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review:\n  key_issues_to_review: []"
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    out = R.run("https://pr", "review")
    assert out["review"]["key_issues_to_review"] == []
    assert out["review"]["_engaged"] is True          # 多段非空 -> 审完无问题


def test_runner_engaged_false_when_review_empty(monkeypatch):
    # _rv 近乎空（如 diff 被裁空、glm 无米下锅）-> _engaged=False，review_reliable 维持可疑判据。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    algo_utils.load_yaml = lambda s, **k: {"review": {"key_issues_to_review": []}}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review:\n  key_issues_to_review: []"
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    out = R.run("https://pr", "review")
    assert out["review"]["_engaged"] is False         # 仅空 key_issues，无其他段 -> 未 engaged


def test_runner_engaged_excludes_key_issues_from_count(monkeypatch):
    # 闭环 round-3 PRA-GENERAL:pr_agent_runner.py:271——engagement 计数须排除 key_issues_to_review
    # （注释承诺"key_issues 之外"，代码须一致）。key_issues 非空 + 仅 1 个其他段 → engaged=False。
    # 注：此场景 ai_raw_count>0 已使 review_reliable=True（走"有原始建议"路），engaged 值不影响
    # 可靠性——这里锁的是"计数排除 key_issues"这一行为契约（修复前 key_issues 被计入 → engaged=True）。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    algo_utils.load_yaml = lambda s, **k: {"review": {
        "key_issues_to_review": [{"x": 1}],   # 非空，但不应计入 engagement
        "estimated_effort_to_review": "2",     # 仅 1 个非 key_issues 段
    }}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review:\n  key_issues_to_review: []"
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    out = R.run("https://pr", "review")
    assert out["review"]["_engaged"] is False         # 排除 key_issues 后仅 1 段 < 2


def test_runner_emits_raw_excerpt_for_substantive_review(monkeypatch):
    # 0 原始建议（key_issues 空）时，runner 仍把 review 的非空结构段快照随 JSON 透出（_raw_excerpt），
    # 供报告横幅贴"LLM 原始评审"打消"是否真审过"疑虑（PR #55 评审意见）。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    algo_utils.load_yaml = lambda s, **k: {"review": {
        "key_issues_to_review": [],
        "estimated_effort_to_review": "2",
        "relevant_tests": "Yes",
        "security_concerns": "No",
    }}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review:\n  key_issues_to_review: []"
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    out = R.run("https://pr", "review")
    assert out["review"]["_raw_excerpt"] == {
        "estimated_effort_to_review": "2", "relevant_tests": "Yes", "security_concerns": "No"}
    assert "key_issues_to_review" not in out["review"]["_raw_excerpt"]   # "0 意见"本体不进快照


def test_runner_raw_excerpt_empty_when_review_empty(monkeypatch):
    # _rv 近乎空（diff 被裁空 / glm 无米下锅）→ 快照空；横幅无内容可贴，回退纯文本溯源。
    _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    algo_utils.load_yaml = lambda s, **k: {"review": {"key_issues_to_review": []}}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = "review:\n  key_issues_to_review: []"
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer
    out = R.run("https://pr", "review")
    assert out["review"]["_raw_excerpt"] == {}


def test_runner_warns_when_ticket_disable_fails(monkeypatch, caplog):
    # 闭环 round-3 PRA-GENERAL:pr_agent_runner.py:175——bare except:pass 改为告警（防静默故障）。
    # require_ticket_analysis_review 不可设（pr_reviewer 版本不符/只读）时，须落 _IX + 日志，不静默吞。
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    import pr_agent.algo.utils as algo_utils
    import pr_agent.tools.pr_reviewer as rv_mod
    algo_utils.load_yaml = lambda s, **k: {"review": {"key_issues_to_review": []}}

    class PRReviewer:
        def __init__(self, url):
            self.prediction = ""
        async def run(self):
            return None
    rv_mod.PRReviewer = PRReviewer

    class _ReadOnlyTicket:                       # require_ticket_analysis_review 只读 → 赋值抛 AttributeError
        @property
        def require_ticket_analysis_review(self):
            return True
    settings.pr_reviewer = _ReadOnlyTicket()      # runner 取同一 settings 对象（见 test_runner_disables_ticket_analysis）
    R._IX.clear()
    import logging
    with caplog.at_level(logging.WARNING, logger="touchstone.pr_agent"):
        R.run("https://pr", "review")             # 不应崩（except 兜住）
    assert "关 require_ticket_analysis_review 失败" in "\n".join(R._IX)   # 交互日志可见
    assert "关 require_ticket_analysis_review 失败" in caplog.text        # 日志可见（CI 直见/可采集）


def test_runner_disables_ticket_analysis(monkeypatch):
    # 关 pr-agent 工单合规分析（默认 true 致 fetch_sub_issues 每轮崩 + prompt 噪音，PR #51 排查）。
    settings = _install_fake_pr_agent(monkeypatch)
    _stub_llm(monkeypatch)
    R.run("https://pr", "improve")
    assert settings.pr_reviewer.require_ticket_analysis_review is False

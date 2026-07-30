#!/usr/bin/env python3
# ============================================================================
# touchstone/pr_agent_runner.py  ——  PR-Agent 子进程适配器
#
# 在【装了 pr-agent 的环境】里调 PR-Agent（improve / review），不往 PR 发评论，
# 把结果打成 review_provider.parse_pr_agent 期望的 JSON 打到 stdout：
#     {"code_suggestions": [...], "review": {"key_issues_to_review": [...]}}
#
# 设计要点：
#   • Touchstone 本体【不依赖】pr-agent；pr-agent 只装在跑本脚本的子进程环境里（依赖隔离）。
#   • LLM 配置经 LLM_* env 映射成 pr-agent/LiteLLM 认的键（见 run() docstring），不在本仓
#     workflow 里重复散落凭据；GITHUB_TOKEN 经 env 透传给 pr-agent 取 PR。本脚本不持有、不打印密钥。
#   • PR-Agent 是 pip 包、不是要部署的服务——这里就是 import 它、调用、拿结果。
#   • 【防静默故障】引擎层失败（pr-agent 没装 / LLM 调用失败）一律不抛、不非零退出，而是返回
#     {"_degraded": "no_engine"|"llm_failed", "reason": ...}；review_provider 转成
#     ReviewEngineDegraded，orchestrator 把它写进贴到 PR 的人可见评审说明（见 docs/touchstone-on-pr-agent.html）。
#
# 已对 pr-agent 0.37.0 验证下列 API（improve 结果在 self.data；review 需自行 load_yaml
# 解析 self.prediction；extra_instructions/publish_output 为真实配置键）。其它版本若有差异，
# 这是【你拥有的适配器】，按你的版本对齐即可。沙箱无凭据无法真跑，只验 API 表面。
#
# best_practices.md 不经本脚本注入：pr-agent 的本地 best_practices 是【文件式】——把
# gen_best_practices.py 生成的 best_practices.md 放到被审 PR 的仓库根，pr-agent 自动识别。
# 本脚本只负责 extra_instructions（学习回路 active 经验 / 主观强调）的注入。
# ============================================================================

import argparse
import time
import asyncio
import json
import os
import sys

from touchstone.logging_setup import get_logger

_log = get_logger("pr_agent")   # 诊断日志：默认 stderr，可经 TOUCHSTONE_LOG_* 接管

# review 解析用的 keys_fix_yaml，与 pr-agent PRReviewer._prepare_pr_review 一致
_REVIEW_KEYS_FIX = ["key_issues_to_review:", "relevant_file:", "relevant_line:", "suggestion:"]


def _read(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def _ping_enabled():
    """预检 ping 开关（上游报告问题五）：默认开（防静默故障的决定性观测点）；
    端点已确认健康、fan-out 下想省 improve/review 各一次往返的部署可置 false 关闭。"""
    return (os.environ.get("TOUCHSTONE_LLM_PING", "true") or "").strip().lower() not in ("0", "false", "no")


def _ping_llm(base, key, model):
    """直接探测 LLM 端点（1-token 请求），确认 base/key/model 可用。失败抛异常（带真实错误）。
    抽成函数便于测试 monkeypatch（离线测试不真发请求）。"""
    import openai
    c = openai.OpenAI(base_url=base, api_key=key, timeout=30)
    c.chat.completions.create(model=model,
                              messages=[{"role": "user", "content": "ping"}], max_tokens=1)


# 本次 run() 的交互轨迹（关键节点日志），main() 据此 + 返回结果写完整交互日志（artifact）。
# 单进程单线程，模块级即可。
_IX: list = []
_IX_T0: list = []      # run() 起点（monotonic）；上游报告问题四：交互日志自带阶段耗时


def _reset_trace():
    """重置交互轨迹与计时起点。run() 与测试都用它——评审意见：重置逻辑集中一处，
    测试不再直接改 _IX/_IX_T0 内部结构，run() 的重置演化时测试同步生效。"""
    _IX.clear()
    _IX_T0[:] = [time.monotonic()]


def _ix(msg):
    if _IX_T0:
        msg = f"[+{time.monotonic() - _IX_T0[0]:7.2f}s] {msg}"
    _IX.append(msg)


def _write_interaction_log(out):
    """把本次 LLM 交互的完整轨迹 + pr-agent 原始输出写到 TOUCHSTONE_INTERACTION_LOG（供 workflow
    上传为 artifact、评审评论里贴链接）。失败不影响主流程。"""
    path = os.environ.get("TOUCHSTONE_INTERACTION_LOG")
    if not path:
        return
    try:
        import json as _json
        parts = ["# PR-Agent / LLM 完整交互日志", "(api_key 已脱敏，不记录)"]
        parts += list(_IX)
        parts += ["", "---- 返回结果（pr-agent 完整输出 / 降级原因）----",
                  _json.dumps(out, ensure_ascii=False, indent=2)[:20000]]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
    except Exception as e:
        try:
            _log.warning("交互日志写入失败: %s", e)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LLM 调用调优：重试语义收敛 + glm 系列流式化。要点全部实证（183 runs / 96 PRs 全量分析
# + 缩尺受控实验），勿凭 litellm 文档或历史注释想当然：
#   • 真实失败两个种——hard timeout（PR#86 两次尝试 1221.2s/1515.22s 全超时）与长生成断连
#     （PR#81/#88/#83 降级轮，"glm 长生成路径负载下断连、短调用正常"）；全部发生在调用
#     600s+ 之后，轮内重试救回率 0/8；轮级转移 slow→slow 18:7 且变快间隔为小时级——慢是
#     端点×时段×规模的状态，不随重试消失。故默认不重试（N=0），唯一保留的是秒级快失败
#     （快窗内 TLS 瞬断/网关快速 5xx：成本低、真抖动概率高）的 +1 次。
#   • 重试只放 tenacity 层（每次重试是全新 litellm 调用，流式/日志语义干净）；litellm 包装层
#     与 openai client 内层一律 0——否则 client 默认 max_retries=2 在底下偷偷乘（缩尺实验：
#     一次工具调用全超时路径端点收到 7 个 HTTP 请求）。
#   • ai_timeout 是 httpx per-read 超时不是墙钟（滴流实验 4s/字节击穿 6s 超时 120s+）；
#     流式化让该语义恰好变正确：持续出字不误杀、真死等 600s 必杀。
# ---------------------------------------------------------------------------


def _llm_num_retries():
    """N：tenacity 层重试次数（secret env 控制）。默认 0 = 不重试；非法 env fail-loud 回 0。"""
    _raw = os.environ.get("TOUCHSTONE_LLM_NUM_RETRIES", "0")
    try:
        return max(0, int(_raw or 0))
    except (ValueError, TypeError):
        print(f"[runner] TOUCHSTONE_LLM_NUM_RETRIES 非法（{_raw!r}），回退默认 0", file=sys.stderr)
        return 0


def _retry_fast_window():
    """快窗（秒）。默认 120；0 = 关闭快失败加成（纯 N 次语义）；非法 env fail-loud 回默认。"""
    _raw = os.environ.get("TOUCHSTONE_LLM_RETRY_FAST_WINDOW", "120")
    try:
        return max(0, int(_raw or 0))
    except (ValueError, TypeError):
        print(f"[runner] TOUCHSTONE_LLM_RETRY_FAST_WINDOW 非法（{_raw!r}），回退默认 120", file=sys.stderr)
        return 120


def _is_jitter_error(exc):
    """抖动类判定（纯函数，供单测）：APIError 中排除超时（litellm.Timeout ⊂ openai.APITimeoutError，
    与长生成同源）与限流（沿用 pr-agent 原判定永不重试）。"""
    import openai
    return (isinstance(exc, openai.APIError)
            and not isinstance(exc, (openai.RateLimitError, openai.APITimeoutError)))


def _make_retry_policy(n_retries, fast_window):
    """tenacity 重试谓词工厂（纯函数，供单测）：
      允许重试次数 = N + (1 若 抖动类异常 且 失败发生在快窗内 else 0)；RateLimit 永不重试。
    attempt_number 语义：第 k 次尝试失败后 tenacity 以 attempt_number=k 询问；
    放行条件 k <= allowed → 实际重试次数恰为 allowed。"""
    def _policy(retry_state):
        outcome = getattr(retry_state, "outcome", None)
        exc = outcome.exception() if (outcome is not None and outcome.failed) else None
        if exc is None:
            return False
        import openai
        if isinstance(exc, openai.RateLimitError):
            return False
        allowed = n_retries
        if _is_jitter_error(exc) and retry_state.seconds_since_start < fast_window:
            allowed += 1
        return retry_state.attempt_number <= allowed
    return _policy


def _make_stop(n_retries):
    """tenacity stop 上限（纯函数）：最多 N+2 次尝试（覆盖快失败 N+1 次重试路径）。
    实际截断通常由 _make_retry_policy 先发生；本函数只是防御性天花板。"""
    def _stop(retry_state):
        return retry_state.attempt_number >= n_retries + 2
    return _stop


def _llm_thinking_extra_body():
    """思考模式开关（secret env TOUCHSTONE_LLM_THINKING）→ 注入请求体的 extra_body。
    值：disabled / enabled；未设/空 = 不注入（随端点默认）。方言为 GLM 系
    `{"thinking": {"type": ...}}`（对 litellm 1.84.0 实测 extra_body 原样落到请求 JSON 顶层）。
    背景：思考型端点默认开思考时，每次调用先烧数千 reasoning token 再出正文——大 diff
    improve 单调用 10min+ 的头号嫌疑；pr-agent 对 GLM 无任何思考控制路径（其 thinking
    参数仅 Claude extended thinking 专用），故由 runner 注入。优先在网关侧对本 key 默认
    关思考（治本）；网关不可改时用本开关。非法值 fail-loud 不注入（防静默改变端点行为）。"""
    _raw = os.environ.get("TOUCHSTONE_LLM_THINKING", "").strip().lower()
    if not _raw:
        return None
    if _raw in ("disabled", "enabled"):
        return {"thinking": {"type": _raw}}
    print(f"[runner] TOUCHSTONE_LLM_THINKING 非法（{_raw!r}，需 disabled/enabled），不注入", file=sys.stderr)
    return None


def _guard_acompletion(orig_acompletion, extra_body=None):
    """围栏（纯函数工厂，供单测）：逐调用注入 max_retries=0——openai client 内层不重试
    （否则默认 max_retries=2 在 tenacity 之下偷偷乘）；extra_body 非空时一并注入
    （思考开关等端点方言，主模型与自评模型统一生效）。setdefault：上游显式传参不覆盖。"""
    async def _guarded(*args, **kwargs):
        kwargs.setdefault("max_retries", 0)
        if extra_body and "extra_body" not in kwargs:
            kwargs["extra_body"] = extra_body
        return await orig_acompletion(*args, **kwargs)
    return _guarded


def _install_llm_call_tuning(model_override):
    """接线：围栏 + tenacity 谓词/上限 + glm 系列流式。调优是收敛性优化——任何一步失败
    fail-loud（stderr + 交互日志）后继续，不把可用引擎搞挂。"""
    try:
        from pr_agent.algo.ai_handlers import litellm_ai_handler as _lah
    except Exception as e:
        print(f"[runner] LLM 调用调优未安装（pr_agent 导入失败：{type(e).__name__}: {e}）", file=sys.stderr)
        _ix(f"LLM 调用调优未安装: {type(e).__name__}: {e}")
        return
    # 围栏：client 内层 0 重试 + 思考开关注入
    try:
        _thinking = _llm_thinking_extra_body()
        _lah.acompletion = _guard_acompletion(_lah.acompletion, extra_body=_thinking)
        _ix("围栏: openai client max_retries=0（每 litellm 调用恰 1 次 HTTP 尝试）"
            + (f"；thinking={_thinking['thinking']['type']}" if _thinking else "；thinking=端点默认"))
    except Exception as e:
        print(f"[runner] 围栏安装失败：{type(e).__name__}: {e}", file=sys.stderr)
        _ix(f"围栏安装失败: {type(e).__name__}: {e}")
    # tenacity 层：谓词 + 上限（控制器运行期可改，纯函数谓词/stop 均经概念验证）
    _n, _fw = _llm_num_retries(), _retry_fast_window()
    try:
        _pol = _lah.LiteLLMAIHandler.chat_completion.retry
        _pol.retry = _make_retry_policy(_n, _fw)
        _pol.stop = _make_stop(_n)
        _ix(f"重试策略: N={_n}（慢失败）/ N+1（{_fw}s 快窗内抖动）/ RateLimit 永不")
    except Exception as e:
        print(f"[runner] 重试策略安装失败，维持 pr-agent 默认（超时/断连也重试）："
              f"{type(e).__name__}: {e}", file=sys.stderr)
        _ix(f"重试策略安装失败: {type(e).__name__}: {e}")
    # glm 系列流式化：主模型 + 自评模型都入清单（自评模型同为 glm 长生成路径）
    if os.environ.get("TOUCHSTONE_LLM_STREAM", "true").lower() in ("1", "true", "yes"):
        try:
            from pr_agent.algo import STREAMING_REQUIRED_MODELS
            _models = [model_override, os.environ.get("TOUCHSTONE_LLM_REFLECT_MODEL", "").strip()]
            for _m in (f"openai/{m}" for m in _models if m):
                if _m not in STREAMING_REQUIRED_MODELS:
                    STREAMING_REQUIRED_MODELS.append(_m)   # handler 实例持同一 list 对象，append 即生效
            _ix(f"流式: {[m for m in _models if m]} 已入 STREAMING_REQUIRED_MODELS")
        except Exception as e:
            print(f"[runner] 流式启用失败，维持非流式：{type(e).__name__}: {e}", file=sys.stderr)
            _ix(f"流式启用失败: {type(e).__name__}: {e}")


def _patch_context_settings():
    """扩窗旋钮（issue #139 方案 A）：env 可调、纯函数可测。默认值取「守卫多在
    enclosing 函数头到命中行之间」的经验面：动态上下文上限 10→30、前置固定行 5→10、
    后置 1→3。回调消融：把三个 env 设回上游默认即得对照组。"""
    def _envi(name, default):
        # #140 R7：clamp 到非负——负的上下文行数会让 pr-agent 扩窗/补丁行计数下溢
        try:
            return max(0, int((os.environ.get(name) or "").strip() or default))
        except (ValueError, TypeError):
            return max(0, default)
    return {
        "allow_dynamic_context": True,
        "max_extra_lines_before_dynamic_context": _envi("TOUCHSTONE_DYNAMIC_CONTEXT_MAX", 30),
        "patch_extra_lines_before": _envi("TOUCHSTONE_PATCH_EXTRA_BEFORE", 10),
        "patch_extra_lines_after": _envi("TOUCHSTONE_PATCH_EXTRA_AFTER", 3),
    }


def run(pr_url, mode, extra_instructions=None):
    """调 PR-Agent（不发评论）→ 返回 dict 供 touchstone 解析。

    LLM 配置经 LLM_* env 映射成 PR-Agent/LiteLLM 认的键（方案 b，集中在本适配器，
    不把凭据重复散落到 workflow 明文 env）：
      LLM_API_KEY  → OPENAI_API_KEY（LiteLLM openai provider 在调用时读）
      LLM_BASE_URL → OPENAI_API_BASE
      LLM_MODEL    → get_settings().config.model = "openai/<model>"（LiteLLM provider 前缀，走 OpenAI 兼容端点）

    GitHub 凭据：从 GITHUB_TOKEN（workflow 透传、subprocess 继承）注入 pr-agent 的
    settings.github.user_token，并显式 git_provider="github"——否则 pr-agent 取不到 PR
    （GitProviderFactory 报 "Failed to get git provider"），连 LLM 都调不到。

    任何引擎层失败都【不抛异常、不非零退出】，而是返回带 `_degraded` 的 dict，由 review_provider
    转成 ReviewEngineDegraded、再由 orchestrator 写进人可见的评审说明（防静默故障）：
      _degraded="no_engine"      —— pr-agent 未安装 / 导入失败
      _degraded="provider_failed"—— pr-agent 已导入但取 PR/git provider 失败（凭据/网络，pre-LLM）
      _degraded="llm_failed"     —— PR 已取到、但 LLM 调用失败（端点/鉴权/超时/解析等）
    """
    # 先把 LLM_* 映射成 LiteLLM 认的 env（必须在 import/调用 pr-agent 前注入）
    _reset_trace()
    _ix(f"pr_url={pr_url} mode={mode} extra_instructions={len(extra_instructions or '')} 字符")
    if os.environ.get("LLM_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.environ["LLM_API_KEY"]
    if os.environ.get("LLM_BASE_URL") and not os.environ.get("OPENAI_API_BASE"):
        os.environ["OPENAI_API_BASE"] = os.environ["LLM_BASE_URL"]
    model_override = os.environ.get("LLM_MODEL")

    try:
        from pr_agent.algo.utils import load_yaml
        from pr_agent.config_loader import get_settings
        from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
        from pr_agent.tools.pr_reviewer import PRReviewer
    except ImportError as e:
        _ix(f"阶段=import 失败(no_engine): {e}")
        return {"_degraded": "no_engine",
                "reason": f"pr-agent 未安装：请在本子进程环境 `pip install pr-agent`。原始错误：{e}"}

    s = get_settings()
    s.config.publish_output = False           # 关键：不往 PR 发评论，只取结构化结果
    s.config.publish_output_progress = False
    # git_provider 经 env 可覆盖：默认 github（headless 运行避免 provider 自动探测失败）。
    # 设 TOUCHSTONE_GIT_PROVIDER=local → LocalGitProvider：审【本地分支】（HEAD vs --pr-url 给的
    # 目标分支），不经 GitHub、无需 token。用于本地端到端 / pre-push 自查。
    # 注意 local 模式 pr-agent 仅支持 review（不支持 improve，见 local_git_provider），故配 --mode review。
    s.config.git_provider = os.environ.get("TOUCHSTONE_GIT_PROVIDER", "github")
    gh_tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_USER_TOKEN")
    if gh_tok:
        s.github.user_token = gh_tok          # pr-agent 取 PR 需要 GitHub token
    if model_override:
        s.config.model = f"openai/{model_override}"   # LiteLLM：openai 前缀走 OpenAI 兼容端点
        # pr-agent 的 get_max_tokens(model) 要求模型在内置 MAX_TOKENS 表里，否则报
        # "Model ... is not defined in MAX_TOKENS ... no custom_model_max_tokens is set"
        # 直接判"Failed to generate prediction"（glm-5.2 等自定义模型不在表里——这是多日"0 建议"的真根因之一）。
        #
        # 【输入侧预算，非输出 max_tokens】pr-agent 0.37 的 get_max_tokens() 把此值当作
        # 【整个上下文窗口】用于内部 diff 裁剪预算（pr_processing.get_pr_diff 的 token 上限），
        # 而它实际向 LLM API 发起的 chat_completion【并不传 max_tokens】（litellm_ai_handler
        # 构造的 kwargs 无 max_tokens 字段，仅 extended_thinking 路径才设）——由端点用默认输出上限。
        # 故此值必须填【模型上下文窗口】(context_tokens)，绝不能填【输出】(output_tokens=默认4096)：
        # 填 4096 = 告诉 pr-agent"整个窗口只有 4096"→ 改动 diff 被裁成空 → LLM 拿到空 diff → 0 建议。
        # 这正是 PR #44（及 #42 收敛的运气成分）"LLM 没给意见"的真根因：output_tokens 语义用反。
        # 部署方用 secret TOUCHSTONE_LLM_CONTEXT_TOKENS 按模型卡声明上下文窗口；未声明时回退
        # 128000（现代模型典型窗口）而非 4096——宁可让 LLM 看全 diff，不可裁空。
        try:
            from touchstone.llm_budget import context_tokens
            ctx = context_tokens()
            window = ctx if ctx > 0 else 128000
            s.config.custom_model_max_tokens = window
            # 第二闸：pr-agent get_max_tokens（utils.py:992）末尾 min(max_model_tokens,
            # custom_model_max_tokens)。configuration.toml:34 默认 max_model_tokens=32000
            # （"防输入太长降智"全局 cap）。不覆盖则不管上面 custom 设多大都被 min 成 32000 →
            # 大 PR diff 被裁、review 看不全（run 29082805842：39518 token 裁到 32000）。
            # 与 custom 同设同一 window，让 diff 上限跟模型真实窗口走（部署方经 secret 钉值）。
            s.config.max_model_tokens = window
        except Exception:
            s.config.custom_model_max_tokens = 128000
            s.config.max_model_tokens = 128000
        # 清空 fallback_models：默认 fallback（gpt-5.4-mini 等）发到我们的 base 会返回"模型不存在"，徒增失败噪音。
        try:
            s.config.fallback_models = []
        except Exception as e:
            # 清不掉 → pr-agent 默认 fallback（gpt 系）会打到我们的 base 报"模型不存在"，
            # 徒增失败噪音与重试墙钟——不可静默。
            _log.warning("fallback_models 清空失败，默认 fallback 将产生失败噪音：%s: %s",
                         type(e).__name__, e)
        # improve 自评换模。注意概念区分：这是 config.model_reasoning（improve 生成建议后那次
        # mandatory self-reflection 打分调用【专用】的模型，pr_code_suggestions.py:409），
        # 【不是】fallback_models（那是主调用失败后 retry_with_fallback_models 的换模清单，
        # 上面已清空）。自评是浅任务无需大模型；指到 glm-4.5-air 可把 improve 健康路径
        # 耗时近乎减半。不设 = 沿用主模型（pr-agent 默认）。
        _reflect = os.environ.get("TOUCHSTONE_LLM_REFLECT_MODEL", "").strip()
        if _reflect:
            try:
                s.config.model_reasoning = f"openai/{_reflect}"
                _ix(f"improve 自评换模: openai/{_reflect}（model_reasoning，非 fallback_models）")
            except Exception as e:
                print(f"[pr-agent] 自评换模失败，沿用主模型：{type(e).__name__}: {e}", file=sys.stderr)
    # 单次 LLM 调用超时（pr-agent 默认 ai_timeout=120s，对 glm-5.2 等慢模型不够--实测 glm-5.2
    # 响应 360s+，120s 必超时，litellm 重试又超时，improve 工具两次全超时 -> 0 suggestions 假象）。
    # 经 TOUCHSTONE_LLM_CALL_TIMEOUT env 配置（秒），默认 600s（glm-5.2 实测 360-380s，留余量）。
    # 注意与 TOUCHSTONE_PRAGENT_TIMEOUT（整个子进程超时，需 > N次调用×ai_timeout）区分。
    try:
        s.config.ai_timeout = int((os.environ.get("TOUCHSTONE_LLM_CALL_TIMEOUT") or "").strip() or "600")
    except (ValueError, TypeError):
        s.config.ai_timeout = 600
    # 关 pr-agent 的工单合规分析（pr_reviewer.require_ticket_analysis_review，pr-agent
    # configuration.toml:86 默认 true）。它调 GitHub GraphQL fetch_sub_issues，对本仓 token/
    # 配置返回 None → github_provider.py:1243 response_json.get(...) 抛 AttributeError（每轮
    # review 必崩一次，虽被 pr-agent 捕获非致命，但是噪音 + 脆弱路径），且把 ticket 合规塞进
    # 评审 prompt、偏移 glm 对代码本身的注意力。touchstone 做代码评审门禁、不做工单合规——关掉。
    try:
        s.pr_reviewer.require_ticket_analysis_review = False
    except Exception as e:
        # 关失败要可见：pr_reviewer 不存在/属性不可设（pr-agent 版本不符）时，ticket 分析会继续
        # 每轮崩（fetch_sub_issues）+ 污染 prompt，无此告警则静默退化（防静默故障，同 review_provider
        # 哲学）。记交互日志 + 落 stderr（CI 日志直见，免下 artifact）。
        _ix(f"关 require_ticket_analysis_review 失败：{type(e).__name__}: {e}")
        _log.warning("关 require_ticket_analysis_review 失败，ticket 分析将继续：%s: %s",
                     type(e).__name__, e)
    if extra_instructions:
        s.pr_code_suggestions.extra_instructions = extra_instructions
        s.pr_reviewer.extra_instructions = extra_instructions
    # 扩窗（issue #139 方案 A）：守卫上下文缺失型误报的第一杠杆。上游默认
    # allow_dynamic_context=true / max_extra_lines_before_dynamic_context=10 /
    # patch_extra_lines_before=5 / after=1（0.39.0 configuration.toml:42-45）——
    # 守卫距命中行超过 10 行即被截掉。此处调大旋钮（env 可回调做消融对比）。
    for k, v in _patch_context_settings().items():
        try:
            setattr(s.config, k, v)
        except Exception as e:                    # 键不存在（版本不符）要可见，防静默退化
            _ix(f"扩窗配置 {k}={v} 设置失败：{type(e).__name__}: {e}")
    # 压一压 LiteLLM 的 stdout 噪音（"LiteLLM.Info / Give Feedback" 等 print），减少 stderr 干扰。
    # 需排查"LLM 到底被调了没"时设 TOUCHSTONE_LITELLM_VERBOSE=true。
    # 上游报告问题二：litellm 1.84 的 set_verbose 在【import 时】求值（litellm/__init__.py:91），
    # import 后再赋值是空操作且被废弃；LITELLM_LOG=DEBUG 亦无效（只设 handler level，logger 仍 NOTSET）。
    # 唯一有效方式是显式调 _turn_on_debug()。注意：DEBUG 走 stderr，会把 failure_stderr_tail 的
    # 600 字符窗口填满、把真实报错挤出——仅供临时诊断，确认后应关闭。
    try:
        import litellm
        litellm.suppress_debug_info = True
        if os.environ.get("TOUCHSTONE_LITELLM_VERBOSE", "").lower() in ("1", "true", "yes"):
            try:
                litellm._turn_on_debug()
                _ix("litellm DEBUG 已开启（_turn_on_debug；api_key 由 litellm SecretRedactionFilter 脱敏）")
            except Exception as _e:
                _log.warning("litellm._turn_on_debug 失败（诊断日志不可用）：%s: %s", type(_e).__name__, _e)
        # 【勘误 + 语义变更】此处曾设 litellm.num_retries = max(1, env)。实证推翻其注释的机制：
        # 该全局是【一次性】的——litellm 1.84 的异常包装器在首个失败消费后即重置为 None
        # （litellm/utils.py:1698），此后 openai client 回落默认 max_retries=2（3 次尝试/调用），
        # 压制从第二个失败起失效；且旧注释引用的 or-短路（litellm/main.py:6738）在 text-to-speech
        # 路径、与 chat 无关。现语义：重试【只】发生在 tenacity 层（_install_llm_call_tuning：
        # 慢失败 N 次、快窗内抖动 N+1 次，默认 N=0 不重试）；litellm 包装层与 openai client
        # 内层一律 0（下面置 0 中和全局 + 围栏逐调用注入 max_retries=0），层次单一、次数确定。
        litellm.num_retries = 0   # falsy → litellm 包装层不重试（utils.py 的 or 链落到 None）
    except Exception as e:
        # 本块失败 = litellm 全局未中和 + verbose 设置丢失：重试语义会静默回到
        # "litellm 一次性全局 + client 默认 2 重试"的旧世界——必须可见。
        _log.warning("litellm 全局配置块失败（重试语义可能回退）：%s: %s", type(e).__name__, e)
    _install_llm_call_tuning(model_override)

    # 【关键节点】LLM 配置日志 + 预检 ping：用同样的 base/key/model 直接发一个 1-token 请求，
    # 确认端点可达、凭据有效（成功会出现在 LLM 服务端请求日志里）。这是回答"LLM key 是否被调用"
    # 的决定性观测点——否则 pr-agent 可能在内部静默跳过 LLM、返回空建议，我们无从得知。
    _base = os.environ.get("OPENAI_API_BASE") or os.environ.get("LLM_BASE_URL")
    _key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    _ix(f"LLM 配置: model={model_override!r} base_url={_base!r} api_key={'已设' if _key else '缺失'}")
    _log.info("LLM 配置：model=%r base_url=%r api_key=%s",
              model_override, _base, "已设" if _key else "缺失")
    if not (_base and _key and model_override):
        _ix("阶段=LLM 配置不全 → llm_failed")
        return {"_degraded": "llm_failed",
                "reason": (f"LLM 配置不全：需 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 都设"
                           f"（model={model_override!r}, base={'有' if _base else '无'}, "
                           f"key={'有' if _key else '无'}）")}
    if not _ping_enabled():
        _ix("LLM 预检 ping: 跳过（TOUCHSTONE_LLM_PING=false；端点已确认健康的部署可关，省一次关键路径往返）")
        _log.info("LLM 预检 ping 跳过（TOUCHSTONE_LLM_PING=false）")
    else:
        try:
            _ping_llm(_base, _key, model_override)
            _ix("LLM 预检 ping: 成功（端点可达、凭据有效）")
            _log.info("LLM 预检 ping 成功（端点可达、凭据有效）")
        except Exception as e:
            _ix(f"LLM 预检 ping: 失败 → llm_failed ({type(e).__name__}: {e})")
            return {"_degraded": "llm_failed",
                    "reason": f"LLM 端点探测失败（{type(e).__name__}: {e}）—— base={_base} model={model_override}"}

    out = {"code_suggestions": [], "review": {"key_issues_to_review": []}}
    tools = set(mode.split("+"))
    _ix(f"阶段=pr-agent 工具执行: tools={sorted(tools)}")
    # pr-agent/LiteLLM 运行期会把 Info/调试信息 print 到 stdout，污染我们最后打印的 JSON
    # （曾导致 review_provider json.loads 失败、误判 no_engine）。这里在 fd 级把 stdout
    # 重定向到 stderr：库的任何 print（含 C 级写）都进 stderr（被 _invoke_endpoint 当诊断捕获），
    # 只有 main() 最后的 json.dump 走真正的 stdout。防 stdout 污染导致的静默故障。
    sys.stdout.flush()
    _saved_stdout_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        # 阶段一：构造 provider + 取 PR（pre-LLM）。构造时即拉取 PR diff，失败归 provider_failed。
        instances = {}
        try:
            if "improve" in tools:
                instances["cs"] = PRCodeSuggestions(pr_url)
            if "review" in tools:
                instances["rv"] = PRReviewer(pr_url)
            _ix("取 PR / 构造 provider: 成功")
        except Exception as e:
            _ix(f"取 PR / 构造 provider: 失败 → provider_failed ({type(e).__name__}: {e})")
            return {"_degraded": "provider_failed",
                    "reason": f"取 PR / git provider 失败（pre-LLM）：{type(e).__name__}: {e}"}
        # 阶段二：跑工具（LLM 调用）+ 解析。失败归 llm_failed。
        try:
            # 结构性事实（对 pr-agent 0.37 核实）：improve 与 review 的 run() 都在顶层全量
            # 捕获异常并只打日志——异常永远不会穿透到本 try，下面的 llm_failed 分支在自然
            # 情况下不可能触发。故工具级故障只能靠【专属 stderr 标记】外化，供
            # review_provider 的签名检测与部分降级诊断使用。标记串是检测契约的一部分，
            # 与 review_provider._PRED_FAILURE_SIGS / partial_tool_failure 联动，勿随意改写。
            if "cs" in instances:
                asyncio.run(instances["cs"].run())           # 结果落在 cs.data = {"code_suggestions": [...]}
                _cs_data = getattr(instances["cs"], "data", None)
                if not _cs_data:
                    print("[runner] improve produced no data（run() 内部已吞异常，真实错误见上文日志）",
                          file=sys.stderr)
                out["code_suggestions"] = (_cs_data or {}).get("code_suggestions") or []
            if "rv" in instances:
                asyncio.run(instances["rv"].run())           # rv.prediction 是原始 YAML 串；自行解析
                _pred = (instances["rv"].prediction or "").strip()
                if not _pred:
                    print("[runner] review produced empty prediction（LLM 空响应或早退，真实原因见上文日志）",
                          file=sys.stderr)
                data = load_yaml(_pred, keys_fix_yaml=_REVIEW_KEYS_FIX,
                                 first_key="review", last_key="security_concerns") or {}
                _rv = data.get("review")
                if _pred and not isinstance(_rv, dict):
                    # 有原文但修复解析后 review 段缺失/非 dict——形变输出（截断/答非所问），
                    # 旧实现 `or {}` 静默吞成空清单且 stderr 无任何失败串（盲区）。
                    print("[runner] review prediction malformed（review 段缺失或非 dict，原文见交互日志）",
                          file=sys.stderr)
                    _rv = {}   # sanitize：truthy 非 dict 时下方 .get 会抛 AttributeError 致 runner 崩溃（pr-agent 评审意见）
                out["review"]["key_issues_to_review"] = (_rv or {}).get("key_issues_to_review") or []
                # engagement：glm 是否给出实质性的多段评审结构。刻意【排除 key_issues_to_review】：
                # 它非空时 ai_raw_count>0 已使 review_reliable=True（走"有原始建议"路），engaged 只在
                # key_issues 为空的干净评审场景起作用——此时数 effort/security/relevant_tests 等段是否
                # >=2，区分"审完无问题"（engaged）与"diff 被裁空 / 响应被吞"（_rv 近乎空，not engaged）。
                # 复用 review_provider.compute_engaged（单一真源，防子进程内/外两套逻辑漂移）；
                # _rv_dict 是完整解析的 review 段（含 effort/security/relevant_tests 等），out["review"]
                # 此刻只有 key_issues_to_review——故以 {"review": _rv_dict} 喂入算。
                _rv_dict = _rv if isinstance(_rv, dict) else {}
                from touchstone.review_provider import compute_engaged, extract_review_excerpt
                out["review"]["_engaged"] = compute_engaged({"review": _rv_dict})
                # 原始反馈快照（effort/tests/security 等非空段）：0 原始建议时贴进报告横幅，打消
                # "是否真审过"疑虑（PR #55 评审意见）。跨子进程 JSON 边界透出，父进程 _extract_excerpt 读取。
                out["review"]["_raw_excerpt"] = extract_review_excerpt({"review": _rv_dict})
            _ix(f"工具执行完成: code_suggestions={len(out['code_suggestions'])} "
                f"key_issues={len(out['review']['key_issues_to_review'])}")
        except Exception as e:   # LLM 端点/鉴权/超时/解析失败等 —— 不静默吞掉，上报为 llm_failed
            _ix(f"工具执行: 失败 → llm_failed ({type(e).__name__}: {e})")
            return {"_degraded": "llm_failed", "reason": f"{type(e).__name__}: {e}"}
        return out
    finally:
        sys.stdout.flush()
        os.dup2(_saved_stdout_fd, 1)
        os.close(_saved_stdout_fd)


# 哨兵包裹 JSON：父进程（review_provider._extract_json）按哨兵精确提取 runner 的结构化输出，
# 隔离 litellm/pr-agent 延迟 print 到 stdout 的噪音（如 "Logging Details LiteLLM-Async Success Call"
# ——async 回调，晚于上方 fd 级 dup2 重定向恢复才落盘，dup2 拦不住）。哨兵是 fd 重定向之上更可靠的第二道。
_JSON_BEGIN = "\n<<<TOUCHSTONE_JSON_BEGIN>>>\n"
_JSON_END = "\n<<<TOUCHSTONE_JSON_END>>>\n"


def _emit_json(out, stream):
    """用哨兵包裹 JSON 写到 stream，供父进程按哨兵提取、隔离第三方 stdout 噪音。"""
    stream.write(_JSON_BEGIN)
    json.dump(out, stream, ensure_ascii=False)
    stream.write(_JSON_END)
    stream.flush()


def main():
    ap = argparse.ArgumentParser(prog="pr_agent_runner",
                                 description="调 PR-Agent（不发评论）并打印 JSON 供 touchstone 解析")
    ap.add_argument("--pr-url", required=True)
    ap.add_argument("--mode", default="improve+review", help="improve / review / improve+review")
    ap.add_argument("--extra-instructions-file")
    a = ap.parse_args()
    out = run(a.pr_url, a.mode, _read(a.extra_instructions_file))
    _write_interaction_log(out)   # 写完整 LLM 交互日志（TOUCHSTONE_INTERACTION_LOG，供 artifact + 评审链接）
    _emit_json(out, sys.stdout)   # 哨兵包裹，父进程按哨兵提取（防 litellm/pr-agent stdout 噪音）


if __name__ == "__main__":
    main()

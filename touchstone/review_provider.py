#!/usr/bin/env python3
# ============================================================================
# touchstone/review_provider.py
#   "Touchstone on PR-Agent" 集成设计（docs/touchstone-on-pr-agent.html）的
#   评审路径骨架：把评审/聚合复用开源 PR-Agent，本系统只做归一 + 裁决映射。
#
#   评审提供器 fetch(pr_ctx) -> [ReviewItem]   —— 封装 PR-Agent 调用（CLI/API、公网或自托管端点）
#   发现归一 normalize(items, nmap) -> [Finding] —— PR-Agent 输出映射成本系统 Finding
#   裁决映射 map_verdict(findings) -> (findings, risk) —— 出三类判定/风险等级（不做共识）
#
# 实现状态（诚实标注）：orchestrator.py 已直连本模块（自研委员会已退役）——评审主链走
#   review_provider.fetch → normalize → map_verdict。PR-Agent 的真实端点调用需在你的部署环境
#   实现 PRAgentProvider._invoke_endpoint（CLI 子进程或 HTTP）；本文件可离线测：parse / normalize
#   / map_verdict 全是纯函数；fetch 支持把 PR-Agent 原始输出经 pr_ctx['pr_agent_output'] 注入。
#   REVIEW_PROVIDER 目前仅支持 "pr-agent"（默认）；端点未配置时 orchestrator 降级为只跑确定性核对。
# ============================================================================

import re
import json
import os
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import yaml

# ---- PR-Agent label → 本系统 category 的默认映射（可被 .touchstone/pr-agent.yaml 覆盖）----
# PR-Agent improve 工具的 label 取值见其 $PRCodeSuggestions schema。
_DEFAULT_NMAP = {
    "label_to_category": {
        "security": "security",
        "critical bug": "correctness",
        "possible bug": "correctness",
        "possible issue": "correctness_suspect",   # 弱信号：值得看一眼但不当已知缺陷
        "performance": "convention",
        "enhancement": "convention",
        "best practice": "convention",
        "maintainability": "convention",
        "typo": "convention",
        "general": "convention",
        "Organization best practice": "convention",   # 违反我们注入的 standards
    },
    "default_category": "convention",
    "default_severity": "warn",          # 顾问式：PR-Agent 建议一律 advisory，不产 block_candidate
    "default_confidence": 0.7,
    "discard_labels": [],
    "conf_min": 0.5,
    "high_categories": ["security", "correctness"],   # 命中才升 high；correctness_suspect 不在内→落 mid
}


def _build_label_maps(nmap):
    """把 nmap 的 label_to_category / discard_labels 预归一为小写查表结构，并对大小写归一后的
    键冲突 fail-loud（防静默路由错误）。

    标签→类别映射大小写无关：pr-agent 的 label schema 明确「也接受其它相关标签」，LLM 实际会发
    大小写不一的形式（'Security'/'Possible Bug' 等）；nmap 键多为小写，若直接查表则大写标签落
    default_category='convention'——安全/正确性发现被错误降级、永不到 high（风险误路由，甚至被自动
    合并）。故键与输入双双归一为小写查表。大小写归一会把仅大小写不同的键合并——若它们映射到【不同】
    类别，后者静默覆盖前者（配置笔误把发现路由到错误类别=防静默故障），故对真冲突 fail-loud；
    同类别冗余键无害（保留首个）。"""
    l2c: dict[str, str] = {}
    for k, v in nmap.get("label_to_category", {}).items():
        lk = str(k).lower()
        prior = l2c.get(lk)
        if prior is not None and prior != v:
            raise ValueError(
                f"nmap.label_to_category 大小写归一后键冲突：'{k}' 与既有键（→ {prior}）映射到不同"
                f"类别（{v}）——会静默把发现路由到错误类别，请统一大小写或类别")
        if prior is None:
            l2c[lk] = v
    discard = {str(d).lower() for d in nmap.get("discard_labels", [])}
    return l2c, discard


# 默认 nmap 不可变且模块级——import 时预计算一次（round-3 finding PRA-GENERAL:821「Precompute
# normalized label map once」）：坏默认配置（键冲突）在此即 fail-fast 暴露，而非拖到首次评审才崩；
# normalize 默认路径直接复用、per-call 成本只与 items 成比例。自定义 nmap 仍每次重建（不能假设传入
# dict 不可变）。
_DEFAULT_L2C, _DEFAULT_DISCARD = _build_label_maps(_DEFAULT_NMAP)


class ReviewEngineDegraded(RuntimeError):
    """PR-Agent 评审引擎降级信号（防静默故障）。

    `degraded` ∈ {"no_engine", "llm_failed"}，由 `pr_agent_runner.run` 经 `_degraded` 字段上报、
    `_invoke_endpoint` 抛出；orchestrator 捕获后把对应说明写进贴到 PR 的人可见评审内容，
    而不是静默降级成"0 条发现"。子类 RuntimeError 以兼容既有宽泛捕获。"""
    def __init__(self, degraded, reason=""):
        super().__init__(f"{degraded}: {reason}")
        self.degraded = degraded
        self.reason = reason


# 可疑空收敛阈值（改动新增行 >= 此值 且 LLM 0 原始建议 -> 评审不可信）。
# 与 orchestrator._clean_review_trace 的 suspicious 判据同源；经 env 可调（如超大 PR 调参）。
_SUSPICIOUS_EMPTY_LINES = int((os.environ.get("TOUCHSTONE_SUSPICIOUS_EMPTY_LINES") or "").strip() or "20")


def review_reliable(engine_status, ai_raw_count, added_lines, engaged=False):
    """本轮 LLM 评审是否可作为 checklist 销项 / loop 收敛 / autonomy 自动放行的可靠证据。

    不可靠（返回 False）的情形--都意味着"本轮 0 建议不代表代码没问题"：
      1. 引擎降级：engine_status != "ok"（no_engine/provider_failed/llm_failed/skipped_large_diff）。
         pr-agent 没真跑或 LLM 调用失败 -> 0 建议是缺审，非审完无问题。
      2. 可疑空收敛：added_lines >= SUSPICIOUS_EMPTY_LINES 且 ai_raw_count == 0 且【未 engaged】。
         引擎虽 ok 但改动不小却 0 原始建议且 glm 没给出实质性评审结构--如 pr-agent 把 diff 裁空
         （PR #44 真根因：custom_model_max_tokens 语义用反致 4096 当窗口裁空 diff）：diff 空 →
         glm 无米下锅 → review 段近乎空（not engaged）。pr-agent 正常返回、engine_status="ok"，
         唯有此判据能抓住。

    engaged 逃生口（PR #51 排查）：engine_status="ok"、改动不小、0 key_issues/suggestions、
    但 glm 给出了实质性多段评审（effort/security/relevant_tests 等 ≥2 段非空）= 【审完无问题】，
    非"没审"。runner 在 review 段写 _engaged（见 _extract_engaged）。engaged 只放宽"可疑空收敛"，
    不救引擎降级（情形 1 仍优先）。engaged 默认 False → 老调用/老产物保持原行为（向后兼容）。

    返回 False 时：checklist 不予"复检未再命中"自动销项、loop 不收敛、autonomy 不自动放行
    --回落到人（ADVISORY 不拦人工合入）。是 PR #45 引擎根因修复之上的 defense-in-depth：
    引擎再坏或超大 PR 超 pr-agent token 预算被裁空时，不再假收敛放行未评审代码。"""
    if engine_status != "ok":
        return False
    if ai_raw_count > 0:
        return True                       # 有原始建议 → 引擎确审
    if added_lines < _SUSPICIOUS_EMPTY_LINES:
        return True                       # 小改动 0 建议合理
    return bool(engaged)                  # 改动不小却 0 建议：engaged=审完无问题；否则可疑（裁空/吞没）


# pr-agent 把 LLM 预测失败吞成 0 建议时，stderr 留下的可靠信号串。
# 见 retry_with_fallback_models（algo/pr_processing.py:326）的第一次吞 + run()（pr_code_suggestions.py:188）
# 的第二次吞：LLM 返回空 content -> 预测解析抛异常 -> WARNING "Failed to generate prediction" ->
# ERROR "Failed to generate prediction with any model" -> 退出码 0、JSON 无 _degraded、findings 全空。
# LLM 失败在 pr-agent 里因【工具而异】地出现在不同层，日志串不同（对 0.37 源码核实）：
#   improve：YAML 解析在 _get_prediction 内、位于 retry_with_fallback_models 圈内 ->
#            空 content 的解析失败会重抛，stderr 必含 "Failed to generate prediction"。
#   review ：retry 只包 _prepare_prediction（取原始文本）；解析在其后的 _prepare_pr_review
#            （retry 圈外）-> 空 content 走 load_yaml 失败路径，stderr 是
#            "Failed to parse AI prediction after fallbacks" / "Failed to parse review data"，
#            或 data=None 时 run() 顶层的 "Failed to review PR:"——【不含】generate prediction 串。
# 只认单一串会漏检"review 空响应 + improve 恰好 0 建议（小 PR 合法情形）"：SIG 缺席、
# engine_status 误判 ok，只剩 added_lines>=20 启发式兜底，小 PR 兜不住。故用信号集合。
# fan-out 子进程级硬失败标记（_collect_subprocess 在 crashed/timed out/non-JSON 时注入 stderr）。
# 带工具名(improve/review) → partial_tool_failure / summarize_llm_failure 能正确归因哪个工具挂了
# （两子进程都可能 emit 同款通用串；不带工具名会串台归因）。kind 串须与 _collect_subprocess 注入的
# 「[runner] {mode} subprocess {kind} …」前缀逐字一致——这里是检测契约的一部分。
_SUBPROC_FAIL_KINDS = ("crashed", "timed out", "non-JSON output")
_IMPROVE_SUBPROC_SIGS = tuple(f"[runner] improve subprocess {k}" for k in _SUBPROC_FAIL_KINDS)
_REVIEW_SUBPROC_SIGS = tuple(f"[runner] review subprocess {k}" for k in _SUBPROC_FAIL_KINDS)

_PRED_FAILURE_SIGS = (
    "Failed to generate prediction",              # improve/任何 retry 圈内失败（含超时/APIError）
    "Failed to parse AI prediction after fallbacks",   # review 解析层：修复兜底后仍无数据
    "Failed to parse review data",                # review 解析层：YAML 无 'review' 键
    "Failed to review PR:",                       # review run() 顶层吞没（如 data=None 的 TypeError）
    # runner 外化的工具级标记（run() 全吞使子进程异常通道失效，只能靠标记，见 pr_agent_runner）：
    "[runner] improve produced no data",
    "[runner] review produced empty prediction",
    "[runner] review prediction malformed",       # 形变输出：有原文但 review 段缺失/非 dict（旧盲区）
) + _IMPROVE_SUBPROC_SIGS + _REVIEW_SUBPROC_SIGS   # fan-out 子进程硬失败（crash/超时/坏JSON）——见 _collect_subprocess

# 工具专属的【顶层】失败串——用于部分降级归因（哪个工具挂了）。与 _PRED_FAILURE_SIGS 的
# 区别：SIGS 判"整轮是否吞没"（需两侧都空），这里判"单侧失败而另一侧仍有产出"。
_IMPROVE_FAIL_SIGS = ("Failed to generate code suggestions for PR",
                      "[runner] improve produced no data") + _IMPROVE_SUBPROC_SIGS
_REVIEW_FAIL_SIGS = ("Failed to review PR:", "Failed to parse review data",
                     "Failed to parse AI prediction after fallbacks",
                     "[runner] review produced empty prediction",
                     "[runner] review prediction malformed") + _REVIEW_SUBPROC_SIGS
_REPAIRED_PARSE_SIG = "Initial failure to parse AI prediction"   # 截断/畸形被 try_fix_yaml 修复的弱信号


def partial_tool_failure(data, stderr):
    """部分降级归因：一个工具失败而另一个仍有真实产出。整轮不判不可信
    （prediction_swallowed_failure 按设计放行——评审仍有效），但必须可见：
    improve 连挂数日而 review 正常时，建议侧信号长期缺失却无人察觉（本次排查盲区 S1）。
    返回 "improve" / "review" / None。纯函数，便于离线测试。"""
    err = stderr or ""
    cs = (data.get("code_suggestions") if isinstance(data, dict) else None) or []
    ki = (((data.get("review") or {}).get("key_issues_to_review"))
          if isinstance(data, dict) else None) or []
    if not cs and ki and any(sig in err for sig in _IMPROVE_FAIL_SIGS):
        return "improve"
    if not ki and cs and any(sig in err for sig in _REVIEW_FAIL_SIGS):
        return "review"
    return None


def prediction_swallowed_failure(data, stderr):
    """检测 pr-agent 把 LLM 预测失败静默吞成"0 建议成功"的情形（防假收敛的可靠主判据）。

    glm-5.2 间歇性返回空 content（choices 存在但 content 为空串）-> 预测解析抛异常 -> pr-agent
    retry 吞 + run() 再吞，返回空 data、退出码 0、无 _degraded 字段。此时 _invoke_endpoint 的
    _degraded 检查（仅查字段）漏过，engine_status 被当 "ok"，0 建议被当"审完无问题"-> 假收敛。

    stderr 里 pr-agent 记的失败串（_PRED_FAILURE_SIGS，按工具/层各异，见其定义处注释）是可靠
    信号。但 improve 工具失败而 review 工具成功给了意见时，stderr 仍含失败串（improve 失败）、
    本轮却有 key_issues -> 不算吞没（仍拿到真实评审）。
    故判据：任一失败串存在 且 本轮原始建议全空（code_suggestions 与 key_issues 都 0）。返回 True ->
    _invoke_endpoint 抛 ReviewEngineDegraded("llm_failed")，engine_status=llm_failed，
    review_reliable 主分支（engine_status!="ok"）触发，不再依赖"大改动+0建议"的启发式近似。

    data：pr-agent 返回的 JSON dict（{"code_suggestions":[...], "review":{"key_issues_to_review":[...]}}）。
    stderr：适配子进程的完整 stderr。纯函数，便于离线测试。"""
    if not any(sig in (stderr or "") for sig in _PRED_FAILURE_SIGS):
        return False
    cs = data.get("code_suggestions") if isinstance(data, dict) else None
    ki = ((data.get("review") or {}).get("key_issues_to_review")
          if isinstance(data, dict) else None)
    return not cs and not ki


def summarize_llm_failure(stderr):
    """从 pr-agent stderr 抽 LLM 失败的【具体原因】，供 llm_failed caution 领头（替代误导性的 stderr 尾部）。

    背景：llm_failed caution 原先只附 stderr[-600:]，但末尾常是【另一侧成功工具】的 success 日志——
    典型如 improve 挂、review 成：stderr 尾部是 review 的 "Async Wrapper ... async_success_handler"，
    把真因（improve 的 `litellm.Timeout ... time taken=1189s`）截在前面、运维看不到 → caution 一边说
    llm_failed、一边贴 success 日志，自相矛盾、零诊断价值。

    本函数抽：
      • 哪个工具挂了（improve / review，按 _IMPROVE_FAIL_SIGS / _REVIEW_FAIL_SIGS）；
      • `Error during LLM inference: <具体异常 … timeout value=X, time taken=Y>` 行——litellm
        超时 / 连接错误 / 限流 的真实原因，及"timeout 没在配置值生效"的时序证据。
        与归因工具对齐：improve 取首条（先跑）、review 取末条（后跑），双失败时不串台。
    返回 (tool, detail)：tool ∈ {"improve","review",None}，detail 为具体错误串（无则 ""）。纯函数。"""
    err = stderr or ""
    tool = None
    if any(sig in err for sig in _IMPROVE_FAIL_SIGS):
        tool = "improve"
    elif any(sig in err for sig in _REVIEW_FAIL_SIGS):
        tool = "review"
    errs = re.findall(r"Error during LLM inference: (.+)", err)
    if errs:
        # 与归因到的工具对齐：improve 先跑（错误在前→errs[0]），review 后跑（错误在后→errs[-1]）。
        # 双失败时若 tool=improve 却贴 errs[-1](review 的错误) 会自相矛盾——caution 说 improve 挂却报 review 的异常。
        # 无归因（tool=None）取最后一条作最佳猜测（保持原行为）。
        detail = (errs[0] if tool == "improve" else errs[-1]).strip()
    else:
        detail = ""
    return tool, detail


def failure_stderr_tail(stderr, limit=800):
    """stderr 中【失败相关】的行，替代误导性的原始尾部 [-600:]。

    原始尾部常落进另一侧成功工具的日志（见 summarize_llm_failure 背景）。这里只挑失败签名行
    （Error during LLM inference / Failed to generate|parse|review … / runner 外化标记），
    让 caution 贴的是真因而非 success 日志。无任何失败行时回退原始尾部（保留诊断、不丢）。纯函数。"""
    err = stderr or ""
    lines = [ln.strip() for ln in err.splitlines()
             if ("Error during LLM inference" in ln
                 or "Failed to generate" in ln
                 or "Failed to parse" in ln
                 or "Failed to review" in ln
                 or ln.lstrip().startswith("[runner]"))]
    if lines:
        return "\n".join(lines)[-limit:]
    return err.strip()[-limit:]


# 哨兵常量须与 pr_agent_runner._JSON_BEGIN/_JSON_END 字面一致（plumbing 协议）。
_JSON_BEGIN = "\n<<<TOUCHSTONE_JSON_BEGIN>>>\n"
_JSON_END = "\n<<<TOUCHSTONE_JSON_END>>>\n"


def _extract_json(stdout):
    """从 runner 子进程 stdout 提取结构化 JSON，容忍第三方库（litellm/pr-agent）延迟 print 的噪音。

    runner（pr_agent_runner._emit_json）用 _JSON_BEGIN/_JSON_END 哨兵包裹 JSON，本函数按哨兵精确提取；
    无哨兵（老协议/哨签缺失）时退化为 raw_decode 取首个 JSON 对象，容忍前后噪音。失败或解出的不是
    dict 则抛 json.JSONDecodeError（_invoke_endpoint 据此判 no_engine——纯噪音/非合法负载仍正确降级）。

    背景：litellm 1.84 async 成功回调会延迟把 "Logging Details LiteLLM-Async Success Call" 打到 stdout
    （晚于 runner 的 fd 级 dup2 重定向恢复），曾致 json.loads "Extra data" → 误判 no_engine（PR #49）。
    非 dict 守卫：raw_decode 兜底可返回任意合法 JSON 值（int/null/list/str——如 litellm 噪音恰以数字
    或 '[' 开头）；非 dict 不是合法评审负载，旧实现会把它当成功数据返回 → parse 空 → 假 engine_status=ok。"""
    m = re.search(re.escape(_JSON_BEGIN) + r"(.*?)" + re.escape(_JSON_END), stdout or "", re.S)
    obj = json.loads(m.group(1)) if m else json.JSONDecoder().raw_decode((stdout or "").lstrip())[0]
    if not isinstance(obj, dict):
        raise json.JSONDecodeError("提取到的 JSON 非 dict（非合法评审负载）", stdout or "", 0)
    return obj


# 非真评审内容的 review 段键：key_issues_to_review（"0 意见"本体）+ runner 注入的内部标志
# （_engaged/_raw_excerpt）。compute_engaged 计段、extract_review_excerpt 抽段都排除它们——
# 否则内部标志键（如 _engaged=True、非空 _raw_excerpt）会被当成"评审段"灌水 engaged 计数
# （test_silent_failure 锁：仅 _engaged=True 无真段 → 旧实现误判 engaged=True → 假 review_reliable）。
_NONCONTENT_REVIEW_KEYS = ("key_issues_to_review", "_engaged", "_raw_excerpt")
_EXCERPT_SKIP_KEYS = _NONCONTENT_REVIEW_KEYS   # 同义别名，保持 excerpt 侧引用名


def compute_engaged(data):
    """glm 是否给出实质性多段评审结构：review 段里【排除非内容键】后 >=2 个非空段。
    单一真源——pr_agent_runner 经 lazy import 复用本函数（防子进程内/外两套 engaged 逻辑漂移，
    见 memory「集成 mock 盲区」教训）。供离线注入路径（无 runner、无 _engaged 标志）按相同口径现算。"""
    if not isinstance(data, dict):
        return False
    review = data.get("review")
    review = review if isinstance(review, dict) else {}
    return sum(1 for k, v in review.items()
               if k not in _NONCONTENT_REVIEW_KEYS and v not in (None, "", [], {})) >= 2


def extract_review_excerpt(data, max_chars=160, max_segments=8):
    """从 pr-agent 原始输出抽取 review 的【非空】结构段（排除 key_issues_to_review 与内部标志），
    作"原始反馈"快照。单一真源——pr_agent_runner 经 lazy import 复用（防子进程内/外两套抽取漂移，
    同 compute_engaged）。

    用途：ai_raw_count==0（无 key_issues、无 code_suggestions）时把此快照贴进评审报告的 0-发现溯源
    横幅，打消"0 是否真审过"的疑虑——glm 审完无问题的干净评审仍会填 estimated_effort/relevant_tests/
    security_concerns 等结构段，这些段的存在即"真审了、只是无实质问题"的证据（PR #55 评审意见）。
    段值多为短串，也可能多行（如 security_concerns 段落）→ 单行化 + 截断 + 反引号归一化，保序，最多
    max_segments 段。返回 dict（段名→归一化单行值）；无内容返回 {}。纯函数，便于离线测试。
    返回值天然 markdown-safe：反引号已归一化为单引号——v 是 LLM 生成文本，security_concerns 等段常以
    反引号引用代码标识符（如 `eval()`），奇数个反引号会让消费方（_clean_review_trace 横幅的 `段名`: 值
    inline-code span、findings.json 审计）渲染失衡（PR #57 评审意见）。归一化放单一真源处，一处兜底全局安全。"""
    if not isinstance(data, dict):
        return {}
    review = data.get("review")
    review = review if isinstance(review, dict) else {}
    out = {}
    for k, v in review.items():
        if k in _EXCERPT_SKIP_KEYS:
            continue
        if v in (None, "", [], {}):
            continue
        s = str(v).replace("\r", " ").replace("\n", " ").replace("`", "'").strip()
        if not s:
            continue
        if len(s) > max_chars:
            s = s[:max_chars].rstrip() + "…"
        out[k] = s
        if len(out) >= max_segments:
            break
    return out


def _extract_engaged(data):
    """优先读 runner 写的 review._engaged 标志（子进程路径）；缺失（离线注入 / 老协议）则按
    compute_engaged 现算。非 dict / review 非 dict → False（保守：维持可疑空收敛判据）。"""
    if not isinstance(data, dict):
        return False
    review = data.get("review")
    if not isinstance(review, dict):
        # review 为 truthy 非 dict（malformed/legacy：字符串/列表/数）时，`... or {}` 会短路返回该非 dict 值，
        # 再 .get("_engaged") 抛 AttributeError。守卫之，使其安全落到 False（与 docstring 承诺一致）。
        return False
    if "_engaged" in review:
        return bool(review.get("_engaged"))
    return compute_engaged(data)


def _extract_excerpt(data):
    """优先读 runner 写的 review._raw_excerpt 标志（子进程路径，跨 JSON 边界透出）；缺失
    （离线注入 / 老协议 / 段全空）则按 extract_review_excerpt 现算。镜像 _extract_engaged 的
    两级回退，使端到端注入测试里"原始反馈"信号能流转。返回 dict（可能空）。"""
    if not isinstance(data, dict):
        return {}
    review = data.get("review")
    if isinstance(review, dict) and "_raw_excerpt" in review:
        val = review.get("_raw_excerpt")
        return val if isinstance(val, dict) else {}
    return extract_review_excerpt(data)


def load_nmap(repo_dir="."):
    """读 .touchstone/pr-agent.yaml 的 normalization 段（缺省用内置默认）。env TOUCHSTONE_PRAGENT 可覆盖路径。"""
    path = os.environ.get("TOUCHSTONE_PRAGENT", os.path.join(repo_dir, ".touchstone", "pr-agent.yaml"))
    nmap = dict(_DEFAULT_NMAP)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        norm = data.get("normalization", {})
        # 浅合并：用户配置覆盖默认（label 映射整体替换以避免歧义）
        for k in ("default_category", "default_severity", "default_confidence", "conf_min", "high_categories"):
            if k in norm:
                nmap[k] = norm[k]
        if "label_to_category" in norm:
            nmap["label_to_category"] = norm["label_to_category"]
        if "discard_labels" in norm:
            nmap["discard_labels"] = norm["discard_labels"]
    except FileNotFoundError:
        pass    # 静默豁免：可选配置，缺省用默认映射是常态，非故障
    except (OSError, yaml.YAMLError) as e:
        # 文件【在】但读不动/YAML 损坏 → 回落默认映射。必须可见：静默回落会让
        # finding 分类在配置损坏时不知不觉漂移（防静默故障）。
        print(f"[review_provider] 归一映射加载失败，回落默认: {e}", file=sys.stderr)
    return nmap


# ---- 解析 PR-Agent 原始输出 → ReviewItem -----------------------------------
# ReviewItem（dict）: {kind, file, line_start, line_end, summary, body, label, tool}
def _clean_str(v):
    """剥 pr-agent 字符串字段的尾换行/首尾空白——防显示污染：relevant_file/summary/reason
    带 \\n 尾时，会致 `file\\n:line` 换行、逐条发现子项间多余空行、达成判据「方向\\n」断行
    （PR #59 真实样例肉眼可见）。仅 strip 首尾，不动内部换行（保留多行 reasoning 语义）；
    非字符串原样返回（line_start 是 int、file 可能 None）。sig 归一化（PR #53 _norm_sig）
    只用于 ack 匹配，显示层在此一并清源。"""
    return v.strip() if isinstance(v, str) else v


def parse_pr_agent(raw):
    """把 PR-Agent improve（code_suggestions）与 review（key_issues_to_review）输出解析为 ReviewItem 列表。
    raw 形如 {'code_suggestions': [...], 'review': {'key_issues_to_review': [...], ...}}。
    字段名按 PR-Agent improve/review schema；不同版本字段略有差异，对接时以你的 PR-Agent 实际版本为准。
    防御：raw 来自子进程 JSON，形状不可信——顶层非 dict、条目非 dict 一律跳过而非抛异常
    （属性测试 test_parse_pr_agent_never_raises 的不变式：任意输入不崩）。"""
    items = []
    if not isinstance(raw, dict):
        return items
    for s in raw.get("code_suggestions", []) or []:
        if not isinstance(s, dict):
            continue
        items.append({
            "kind": "suggestion",
            "file": _clean_str(s.get("relevant_file")),
            "line_start": s.get("relevant_lines_start"),
            "line_end": s.get("relevant_lines_end"),
            "summary": _clean_str(s.get("one_sentence_summary") or s.get("suggestion_content")),
            "body": _clean_str(s.get("improved_code") or s.get("suggestion_content")),
            # reason 与 body 分开：body 可能是 improved_code（补丁），按评审意见 2 不得进
            # 模型来源发现的建议字段；reason 保留文字说明供 fix_reasoning。
            "reason": _clean_str(s.get("suggestion_content")),
            "label": (s.get("label") or "").strip(),
            "tool": "improve",
        })
    review = raw.get("review", {})
    review = review if isinstance(review, dict) else {}
    for k in review.get("key_issues_to_review", []) or []:
        if not isinstance(k, dict):
            continue
        items.append({
            "kind": "review",
            "file": _clean_str(k.get("relevant_file")),
            "line_start": k.get("start_line"),
            "line_end": k.get("end_line"),
            "summary": _clean_str(k.get("issue_header")),
            "body": _clean_str(k.get("issue_content")),
            "reason": _clean_str(k.get("issue_content")),
            "label": (k.get("label") or "review").strip(),
            "tool": "review",
        })
    return items


# ---- 子进程集成的辅助 --------------------------------------------------------
def _build_pr_url(pr_ctx):
    o, r, n = pr_ctx.get("owner"), pr_ctx.get("repo"), pr_ctx.get("number")
    host = os.environ.get("GITHUB_HOST", "github.com")
    return f"https://{host}/{o}/{r}/pull/{n}" if (o and r and n) else ""


def _load_provider_cfg(repo_dir):
    try:
        with open(os.path.join(repo_dir, ".touchstone", "pr-agent.yaml"), encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        return d.get("provider") or {}
    except OSError:
        return {}


def _provider_mode(pr_ctx):
    return (pr_ctx.get("mode") or os.environ.get("TOUCHSTONE_PRAGENT_MODE")
            or _load_provider_cfg(pr_ctx.get("repo_dir", ".")).get("mode") or "improve+review")


def _experience_injection(repo_dir):
    """学习回路的 active 经验 → PR-Agent extra_instructions（只读、可空、失败即空）。
    符合"评审路径只读经验库"的边界；经验只调建议、不进闸。
    TOUCHSTONE_EXPERIENCE_ENABLED=false 时整体关闭注入（默认开）。"""
    if os.environ.get("TOUCHSTONE_EXPERIENCE_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
        return ""
    # 纵深防御：PR 事件下未配受信 ref（TOUCHSTONE_EXPERIENCE_REF）则跳过注入——
    # 否则经验库会从可被本 PR 篡改的工作树读（投毒/提示注入）。工作流已配 ref 时无影响。
    if (os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
            and not os.environ.get("TOUCHSTONE_EXPERIENCE_REF")):
        import sys as _sys
        print("[warn] PR 评审未配置 TOUCHSTONE_EXPERIENCE_REF → 跳过经验注入（防经验库投毒）",
              file=_sys.stderr)
        return ""
    try:
        from touchstone import learning_loop
        # include_shadow 透传：env TOUCHSTONE_SHADOW_INJECTION 开时，active 段后追加 shadow candidate
        # 段（advisory only、文本标灰）。shadow 与 active 读同一 store、同一 EXPERIENCE_REF 防投毒闸——
        # 上方 L473-480 未配受信 ref 即整体返回 ""（含 shadow），candidate 也走受信 ref（铁律 5）。
        # 时序耦合：须与 orchestrator marker 归因（step3）读同一开关，否则 marker 说"注入了 shadow X"
        # 但此处未渲染 → with 臂归因失真；两处 env 默认关 = 字节级等价现状。
        # _shadow_injection_enabled() 独立求值 + 安全降级（pr-agent #118 r1）：它抛异常时降级 False
        # （只禁 shadow 段），不级联进外层 except 禁用整个经验注入含 active（与 step3 :504 同类隔离）。
        try:
            include_shadow = learning_loop._shadow_injection_enabled()
        except Exception as _e:
            # 诊断不静默（pr-agent #118 r2）：shadow 开关求值出错时仍降级 False（active 不受影响），
            # 但打 stderr 告警——运维能看到 shadow 段被关的原因（CLAUDE.md §3 诚实标 gap、不掩盖问题；
            # 与上方 L477-479 跳过注入告警同款 [warn] print 模式）。
            import sys as _sys
            print(f"[warn] _shadow_injection_enabled() 求值失败 → 降级 include_shadow=False"
                  f"（shadow 段关闭、active 注入不受影响）：{_e}", file=_sys.stderr)
            include_shadow = False
        return learning_loop.render_injection(
            learning_loop.load_store(),
            include_shadow=include_shadow,
        ) or ""
    except Exception:
        return ""


# ---- fan-out：improve / review 两个子进程并行 --------------------------------
# 把原「单子进程串行跑 improve+review」改成「两个独立子进程并行各跑一个工具」，把端到端 wall-clock
# 从 import+ping+improve+review 压到 import+ping+max(improve,review)（省 min(improve,review) 一侧）。
# 设计要点（见 memory touchstone-check-duration-profile）：
#   • 用子进程 fan-out 而非同进程 asyncio.gather——进程隔离零共享状态（get_settings 单例 / litellm
#     verbose / git_provider 在同进程并发下有竞争风险），安全远优于省那点导入开销。
#   • 两子进程 I/O-bound（等 LLM）、不抢 CPU-bound 的 import 算力，4 核 runner 上并行几乎无开销。
#   • 部分降级：一个工具挂了另一个仍有真实产出时，不整轮判失败——保留 OK 侧发现、标 partial 可见
#     （比旧「单子进程任一 _degraded 即整轮降级」更保真；review_reliable 因仍有 ai_raw_count 而可信）。
# 子进程状态机：_collect_subprocess 把一次 subprocess.run 的所有结局归一到 _SubResult.status。
_OK = "ok"                 # 退出 0、合法 JSON、无 _degraded
_DEGRADED = "degraded"     # 退出 0、合法 JSON、带 _degraded（pr-agent 没装 / LLM 调用失败）
_CRASHED = "crashed"       # 退出码非 0（适配器自身崩：venv 缺失 / bug）
_TIMED_OUT = "timed_out"   # subprocess.run 触发 TimeoutExpired（子进程已被 run 杀掉并 wait）
_BAD_JSON = "bad_json"     # 退出 0 但 stdout 非合法 JSON
_MISSING = "missing"       # FileNotFoundError（命令找不到 → pr-agent 没装）
_NOT_RUN = "not_run"       # 单一 mode（只 improve 或只 review）下另一工具的占位


class _SubResult:
    """一次子进程调用的归一化结果。_collect_subprocess 填充，_merge_results 消费。"""
    __slots__ = ("mode", "status", "data", "stderr", "reason", "degraded", "timeout")

    def __init__(self, mode, status, data=None, stderr="", reason="", degraded=None, timeout=None):
        self.mode = mode
        self.status = status
        self.data = data if data is not None else {}
        self.stderr = stderr or ""
        self.reason = reason or ""
        self.degraded = degraded
        self.timeout = timeout

    @property
    def failed(self):
        """是否硬失败（排除 _OK 与 _NOT_RUN 占位）。用于 _merge_results 的"所有已跑工具都挂"判定。"""
        return self.status not in (_OK, _NOT_RUN)


def _collect_subprocess(args, mode, timeout, log_path=None):
    """跑【一个】pr-agent 子进程并把所有结局归一到 _SubResult（绝不抛——失败也是数据）。
    硬失败(crashed/timed_out/bad_json)时把「[runner] {mode} subprocess {kind}」标记注入 stderr：
    partial_tool_failure / failure_stderr_tail 据此归因【哪个工具】挂了（两子进程可能 emit 同款通用
    litellm 串，不带工具名会串台）。kind 串须与 _SUBPROC_FAIL_KINDS 逐字一致（检测契约）。原始 stderr
    原样保留（litellm 真实异常 / boom-detail 等供 reason 与 caution）。log_path：fan-out 时给每子进程
    独立交互日志路径（并发写同一文件会互覆盖），None 则继承父进程 env。"""
    env = dict(os.environ)
    if log_path is not None:
        env["TOUCHSTONE_INTERACTION_LOG"] = log_path
    try:
        proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return _SubResult(mode, _TIMED_OUT,
                          stderr=f"[runner] {mode} subprocess timed out（{e.timeout}s）",
                          reason=f"{mode} 子进程超时（{e.timeout}s）——大 PR 或 LLM 端点慢；"
                                 f"可调 TOUCHSTONE_PRAGENT_TIMEOUT 或拆分 PR", timeout=e.timeout)
    except FileNotFoundError as e:
        return _SubResult(mode, _MISSING,
                          reason=f"找不到 PR-Agent 适配命令：请 `pip install pr-agent` 并确保 "
                                 f"touchstone.pr_agent_runner 可运行，或用 TOUCHSTONE_PRAGENT_CMD 指定。原始：{e}")
    except Exception as e:        # 兜底：兑现 docstring「绝不抛」承诺——subprocess.run 其他异常
                                  # （PermissionError/OSError/SubprocessError 等）也归 _CRASHED（下游
                                  # _aggregate_failure → no_engine），不击穿到 ThreadPoolExecutor future
                                  # 再炸整条评审链路（fan-out 下单子进程故障本就不该拖垮另一侧）。
        return _SubResult(mode, _CRASHED,
                          stderr=f"[runner] {mode} subprocess crashed（{type(e).__name__}: {e}）",
                          reason=f"{mode} 子进程异常（{type(e).__name__}: {e}）——"
                                 f"非超时、非命令缺失的执行故障。")
    rc = proc.returncode
    raw_err = proc.stderr or ""
    if rc != 0:
        return _SubResult(mode, _CRASHED,
                          stderr=f"[runner] {mode} subprocess crashed（rc={rc}）\n{raw_err}".rstrip(),
                          reason=f"{mode} 子进程非零退出（rc={rc}）。stderr 末尾：\n{raw_err.strip()[-600:]}")
    try:
        data = _extract_json(proc.stdout)
    except json.JSONDecodeError as e:
        return _SubResult(mode, _BAD_JSON,
                          stderr=f"[runner] {mode} subprocess non-JSON output（{e}）\n{raw_err}".rstrip(),
                          reason=f"{mode} 适配输出非合法 JSON：{e}；stdout 末尾：\n{(proc.stdout or '').strip()[-300:]}")
    except Exception as e:        # 兜底：兑现「绝不抛」——_extract_json 当前实现只抛 JSONDecodeError，
                                  # 此处防未来改动引入其他异常（同 subprocess.run 的 catch-all 一致）。
                                  # 归 _BAD_JSON（输出解析不可用），异常类型进 stderr/reason 供诊断。
        return _SubResult(mode, _BAD_JSON,
                          stderr=f"[runner] {mode} subprocess output parse crashed（{type(e).__name__}: {e}）\n{raw_err}".rstrip(),
                          reason=f"{mode} 输出解析异常（{type(e).__name__}: {e}）。")
    if isinstance(data, dict) and data.get("_degraded"):
        # _degraded 是结构化降级（pr-agent 没装 / LLM 调用失败）——data 自带信号，无需注入标记；
        # 部分降级归因由 _status_partial_failure 按状态精确给出（不靠 stderr 扫描，避免 _degraded 串不在
        # _IMPROVE/_REVIEW_FAIL_SIGS 里而漏归因）。
        return _SubResult(mode, _DEGRADED, data=data, stderr=raw_err,
                          reason=data.get("reason", ""), degraded=data["_degraded"])
    return _SubResult(mode, _OK, data=data, stderr=raw_err)


# fan-out 缺省 review 段（MISSING 占位 / 兜底）——与 pr_agent_runner 初始化的 out 同构。
_EMPTY_PAYLOAD = {"code_suggestions": [], "review": {"key_issues_to_review": []}}


def _merge_results(imp, rev):
    """合并 improve/review 两个 _SubResult。fan-out 时 imp/rev 是两个不同对象；fanout-off 单子进程时
    imp is rev（同一对象，避免 stderr 重复 / data 重包）。单一 mode 时未跑的那侧是 _NOT_RUN 占位。
    返回 (merged_data, merged_stderr, failure)：failure=None 表示无硬失败（ok / 部分降级，不抛，交由下游
    swallowed 检查与 partial 元信息处理）；failure=(degraded, reason) 表示须立即抛 ReviewEngineDegraded
    （引擎没装 / 所有已跑工具都硬失败）。纯函数（不碰 IO），便于离线测试多进程边界场景。"""
    # 致命：引擎没装（FileNotFoundError）——任一子进程 MISSING 即整体 no_engine（另一个跑了也救不了"装不上"）。
    for r in (imp, rev):
        if r.status == _MISSING:
            return dict(_EMPTY_PAYLOAD), r.stderr, ("no_engine", r.reason)
    same = imp is rev
    if same:
        data, stderr, ran = imp.data, imp.stderr, [imp]
    else:
        ran = [r for r in (imp, rev) if r.status != _NOT_RUN]
        cs = (imp.data.get("code_suggestions") if isinstance(imp.data, dict) else None) or []
        review = (rev.data.get("review") if isinstance(rev.data, dict) else None) or {}
        data = {"code_suggestions": cs, "review": review}
        # stderr 顺序 improve→review，与 summarize_llm_failure 的 errs[0](improve 先跑)/errs[-1](review 后跑) 归因一致。
        stderr = "\n".join(s for s in (imp.stderr, rev.stderr) if s)
    failed = [r for r in ran if r.failed]
    if ran and len(failed) == len(ran):
        deg, reason = _aggregate_failure(failed)
        return data, stderr, (deg, reason)
    return data, stderr, None


def _aggregate_failure(failed):
    """所有已跑工具都硬失败时，定整体降级类型 + 汇总 reason。优先级（就高归因，llm_failed > no_engine）：
      超时 → llm_failed（LLM 调用太慢，而非引擎没装）；
      任一 _degraded 且自带 llm_failed → llm_failed（**含「全是 _degraded 但值混合」的情况**——
          必须先于下方 all-分支判定，否则会落到 failed[0] 的值而漏掉就高）；
      全是 _degraded 且无一是 llm_failed → 取 failed[0].degraded（此时只剩 no_engine 之类）；
      其余(crashed/bad_json/无 degraded 的混合) → no_engine。"""
    if any(r.status == _TIMED_OUT for r in failed):
        deg = "llm_failed"
    elif any(r.status == _DEGRADED and r.degraded == "llm_failed" for r in failed):
        deg = "llm_failed"
    elif all(r.status == _DEGRADED for r in failed):
        deg = failed[0].degraded or "no_engine"
    else:
        deg = "no_engine"
    reason = "\n".join(r.reason for r in failed if r.reason)
    return deg, reason


def _status_partial_failure(imp, rev):
    """fan-out 部分降级归因（按子进程状态，精确知哪个挂了）。单子进程(imp is rev)返回 None——该路径的
    部分降级仍由 partial_tool_failure 扫描 stderr 负责（保持原行为）。返回 "improve" / "review" / None。"""
    if imp is rev:
        return None
    imp_failed, rev_failed = imp.failed, rev.failed
    if imp_failed and not rev_failed:
        return "improve"
    if rev_failed and not imp_failed:
        return "review"
    return None


def _fanout_enabled(base_mode):
    """fan-out 仅在 improve+review（两工具都跑）时有意义；单一 mode 无可并行对象。
    env TOUCHSTONE_PRAGENT_FANOUT=false 显式关闭（回落单子进程，便于对照/排障/复现旧行为）。"""
    if set(base_mode.split("+")) != {"improve", "review"}:
        return False
    return os.environ.get("TOUCHSTONE_PRAGENT_FANOUT", "true").lower() not in ("0", "false", "no", "off")


def _merge_interaction_logs(base, sub_paths):
    """fan-out：把各子进程的交互日志拼进 base（best-effort，失败静默——不挡评审）。两子进程并发写同一
    文件会互相覆盖，故 fan-out 时给每子进程独立路径，跑完按 improve→review 顺序合并回 base 再删子文件。"""
    if not base:
        return
    try:
        with open(base, "a", encoding="utf-8") as out:
            for p in sub_paths:
                try:
                    with open(p, encoding="utf-8") as fh:
                        chunk = fh.read()
                except OSError:
                    continue
                if chunk:
                    out.write(f"\n\n---- {os.path.basename(p)} 子进程交互日志 ----\n")
                    out.write(chunk)
        for p in sub_paths:
            try:
                os.unlink(p)
            except OSError:
                pass    # 静默豁免：分片临时文件清理是 best-effort，不影响已合并产物。
    except OSError:
        pass    # 静默豁免：合并流本身失败时产物缺失由消费方可见地报错，此处不重复。


# ---- 评审提供器：封装 PR-Agent 调用（子进程集成）----------------------------
class PRAgentProvider:
    """把 PR-Agent 抽象成一个可替换的"评审观察来源"。对上层只暴露 fetch(pr_ctx) -> [ReviewItem]。"""

    def fetch(self, pr_ctx):
        data = self._invoke(pr_ctx)
        # 注入路径（pr_agent_output）也按相同口径算 engaged——此前仅 _invoke_endpoint 设此 meta，
        # 离线/注入评审永远 engaged=False，使 engaged 信号无法在端到端测试里流转（盲区：PR#52 类
        # 「0意见→卡死」问题难以用注入式 e2e 锁住）。两条路径统一在 fetch 出口设，子进程路径里
        # _invoke_endpoint 不再重复设。
        _LAST_META["review_engaged"] = _extract_engaged(data)
        # 原始反馈快照：0 原始建议时贴进报告横幅打消"是否真审过"疑虑（见 extract_review_excerpt）。
        _LAST_META["raw_review_excerpt"] = _extract_excerpt(data)
        return parse_pr_agent(data)

    def _invoke(self, pr_ctx):
        # 注入点：测试/离线下经 pr_ctx['pr_agent_output'] 直接传入原始输出
        if "pr_agent_output" in (pr_ctx or {}):
            return pr_ctx["pr_agent_output"]
        return self._invoke_endpoint(pr_ctx)

    def _run_tools(self, cmd, pr_url, base_mode, tmp, timeout):
        """起子进程拿 (imp_res, rev_res)，所有结局经 _collect_subprocess 归一（绝不在此抛）。
        fan-out（默认）：improve/review 各一个子进程【并行】——ThreadPoolExecutor 起两线程各跑一个
        subprocess.run，communicate() 释放 GIL → 真并行；各自独立交互日志路径，跑完合并回 base。
        fanout-off / 单一 mode：单子进程；improve+review 时 imp is rev（同对象），单一 mode 时另一侧
        _NOT_RUN 占位（_merge_results 据状态正确处理）。"""
        extra_args = ["--extra-instructions-file", tmp.name] if tmp is not None else []
        base = cmd + ["--pr-url", pr_url] + extra_args
        if _fanout_enabled(base_mode):
            _ixlog = os.environ.get("TOUCHSTONE_INTERACTION_LOG")
            imp_log = f"{_ixlog}.improve" if _ixlog else None
            rev_log = f"{_ixlog}.review" if _ixlog else None
            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_imp = ex.submit(_collect_subprocess, base + ["--mode", "improve"], "improve", timeout, imp_log)
                fut_rev = ex.submit(_collect_subprocess, base + ["--mode", "review"], "review", timeout, rev_log)
                imp_res = fut_imp.result()
                rev_res = fut_rev.result()
            _merge_interaction_logs(_ixlog, [p for p in (imp_log, rev_log) if p])
            return imp_res, rev_res
        # 单子进程：base_mode（improve+review 合并 或 单一 mode）
        single = _collect_subprocess(base + ["--mode", base_mode], base_mode, timeout)
        tools = set(base_mode.split("+"))
        if tools == {"improve"}:
            return single, _SubResult("review", _NOT_RUN)   # 只跑 improve，review 占位
        if tools == {"review"}:
            return _SubResult("improve", _NOT_RUN), single   # 只跑 review，improve 占位
        return single, single   # improve+review 合并：imp is rev（同对象，_merge 走 same 分支不重复 stderr）

    def _invoke_endpoint(self, pr_ctx):
        """真集成（子进程）：起适配子进程 `python -m touchstone.pr_agent_runner`（env TOUCHSTONE_PRAGENT_CMD
        可覆盖），它在装了 pr-agent 的环境里调 PR-Agent（不发评论）、打印哨兵包裹的 JSON。
        fan-out（默认开，TOUCHSTONE_PRAGENT_FANOUT=false 关）：improve / review 各起一个子进程【并行】，
        把 wall-clock 从 import+ping+improve+review 压到 import+ping+max(improve,review)（省 min 侧）。
        两子进程 I/O-bound（等 LLM）不抢 CPU，进程隔离零共享状态（安全优于同进程 gather）。部分降级：一个
        工具挂了另一个仍有产出时不整轮判失败，保留 OK 侧发现、标 partial 可见。沙箱无凭据 → 子进程缺 key
        失败，故此处只能离线测 plumbing（测试 monkeypatch subprocess.run 注入每工具输出）。"""
        pr_url = pr_ctx.get("pr_url") or _build_pr_url(pr_ctx)
        if not pr_url:
            raise RuntimeError("无法确定 PR URL：pr_ctx 需含 pr_url 或 owner/repo/number")
        cmd = shlex.split(os.environ.get("TOUCHSTONE_PRAGENT_CMD", "python -m touchstone.pr_agent_runner"))
        base_mode = _provider_mode(pr_ctx)            # improve+review（默认）/ improve / review
        repo_dir = pr_ctx.get("repo_dir", ".")
        timeout = int((os.environ.get("TOUCHSTONE_PRAGENT_TIMEOUT") or "").strip() or "600")
        # best_practices.md 不经此传：pr-agent 的本地 best_practices 是文件式——放到被审仓库根即可。
        extra = pr_ctx.get("extra_instructions")
        if extra is None:
            extra = _experience_injection(repo_dir)   # 学习回路 active 经验 → extra_instructions（只读、可空）
        # 守卫上下文注入（issue #139 方案 B/C，与经验注入同边界：只调建议、失败即空）：
        #   B 生成侧——本次变更 hunk 的守卫摘要，降低「看 hunk 不看守卫」型误报产出；
        #   C 判后核销——上轮未销项的守卫事实（orchestrator 预取入 pr_ctx），守卫已覆盖的
        #     FP 本轮不再被重报 → 签名不再现 → reconcile 既有机制自动销项。
        try:
            from touchstone import guard_context as _gc_gate
            _guard_on = _gc_gate.enabled()               # 总开关单一判定（PR#140 R2 意见 3）
        except Exception:
            _guard_on = False
        if _guard_on:
            try:
                from touchstone import guard_context
                _gsegs = [guard_context.render_guard_digest(pr_ctx.get("diff") or "", repo_dir),
                          pr_ctx.get("guard_adjudication") or ""]
                _gtxt = "\n\n".join(x for x in _gsegs if x)
                if _gtxt:
                    extra = (extra + "\n\n" + _gtxt) if extra else _gtxt
            except Exception:
                pass    # 静默豁免：守卫注入是纯增强，失败即空段；抛出会拖垮评审链路（与经验注入同款边界）
        tmp = None
        try:
            if extra:
                tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
                tmp.write(extra)
                tmp.close()
            imp_res, rev_res = self._run_tools(cmd, pr_url, base_mode, tmp, timeout)
            data, stderr, failure = _merge_results(imp_res, rev_res)
            # 硬失败（引擎没装 / 所有已跑工具都挂）→ 整体降级。各子进程级结局（crash/超时/坏JSON/_degraded）
            # 已由 _collect_subprocess 归一到 _SubResult，_merge_results 汇总成 (degraded, reason)。
            if failure:
                raise ReviewEngineDegraded(*failure)
            # 部分降级归因（按子进程状态精确知哪个工具挂了）。提前算，既用于下方 swallowed 兜底的豁免，
            # 又复用进 _LAST_META.partial_tool_failure（DRY）。fanout-off（imp is rev）→ None。
            _partial_side = _status_partial_failure(imp_res, rev_res)
            # pr-agent 把 LLM 预测失败（返回空 content -> 解析抛异常）吞成 0 建议的第二道兜底：
            # 退出码 0、无 _degraded 时各子进程 _collect 漏过。合并后 stderr 的失败串 + 本轮 0 原始建议
            # → llm_failed（见 prediction_swallowed_failure）。这正是 PR #44/#46 "0 建议假收敛"真根因。
            # **但仅在非部分降级时适用**：fan-out 下若 improve 硬失败（退出码≠0、stderr 带失败串）而 review
            # 正常却空建议，data 合并后正好空 + stderr 带失败串 → 会误命中这条 swallowed 兜底、误把整轮判
            # llm_failed、丢掉 review 的真发现。故 _partial_side 命中时豁免：空是「失败侧没产出」的预期，
            # 不是「吞没式失败」；该失败已由 partial_tool_failure 标记可见，整轮仍可信、不降级。
            # ⚠ 豁免须收紧（防假收敛）：豁免只该盖住"失败侧的失败串"，不该盖住"非失败侧也吞没了自身失败"。
            # _status_partial_failure 只看子进程状态（_OK = rc=0 且合法 JSON 且无 _degraded），但它【不查
            # stderr 失败串】——pr-agent 把 LLM 失败吞成 rc=0/空建议时该侧也是 _OK.failed=False。故若非失败
            # 侧自身 stderr 带该工具失败串（与失败侧同源的 LLM 负载失败），实为【两工具都挂】的吞没式失败，
            # 整轮当可信空评审即假收敛（PR #44/#46 同类）。故 _partial_side 命中时，再查非失败侧自身 stderr：
            # 若也带该工具失败串 → 不豁免（落入 prediction_swallowed_failure，判 llm_failed）。
            if _partial_side == "improve":
                _ok_side, _ok_sigs = rev_res, _REVIEW_FAIL_SIGS
            elif _partial_side == "review":
                _ok_side, _ok_sigs = imp_res, _IMPROVE_FAIL_SIGS
            else:
                _ok_side, _ok_sigs = None, ()
            _ok_side_also_swallowed = _ok_side is not None and any(
                sig in (_ok_side.stderr or "") for sig in _ok_sigs)
            if (not _partial_side or _ok_side_also_swallowed) and prediction_swallowed_failure(data, stderr):
                # caution 领头给【具体原因】（哪个工具 + litellm 真实异常 + 时序），而非误导性的 stderr 尾部
                # （那常是另一侧成功工具的 success 日志，见 summarize_llm_failure）。
                _fail_tool, _fail_detail = summarize_llm_failure(stderr)
                _lead = f"{_fail_tool + ' 工具' if _fail_tool else '某工具'} LLM 调用失败"
                if _fail_detail:
                    _lead += f"：{_fail_detail}"
                raise ReviewEngineDegraded(
                    "llm_failed",
                    _lead + "——PR-Agent 把该失败吞成 0 建议（退出码 0、无 _degraded），"
                    "故 0 建议是 LLM 失败而非审完无问题。stderr 失败相关行：\n"
                    + failure_stderr_tail(stderr))
            # 诊断（防"0 建议但不知真假"的静默故障）：合并后计数 + 完整合并 stderr 打到 job 日志与交互日志
            # artifact，让人能区分"LLM 真没建议"与"返回了内容但 parse 没解析出来"，并定位真实错误。
            try:
                _cs = len((data.get("code_suggestions") or []))
                _ki = len(((data.get("review") or {}).get("key_issues_to_review") or []))
                _err_full = stderr.strip()
                print(f"[pr-agent] 原始返回：code_suggestions={_cs} key_issues={_ki} "
                      f"(合并 stderr {len(stderr)}B)", file=sys.stderr)
                if _err_full:
                    print(f"[pr-agent] stderr 完整：\n{_err_full[-6000:]}", file=sys.stderr)
                # 把完整合并 stderr 追加进交互日志 artifact（litellm 轨迹/真实 HTTP 错误）
                _ixlog = os.environ.get("TOUCHSTONE_INTERACTION_LOG")
                if _ixlog and _err_full:
                    with open(_ixlog, "a", encoding="utf-8") as _f:
                        _f.write("\n\n---- pr-agent 合并 stderr（litellm 轨迹 / 真实错误）----\n")
                        _f.write(_err_full)
            except Exception:
                pass
            # 非致命诊断元信息（部分降级/修复解析计数）——供 orchestrator 在报告中透明化：
            #   partial：单工具失败但另一侧仍有产出（整轮仍可信，不触发降级，但必须可见）。fan-out 下优先
            #            按子进程状态归因（_status_partial_failure，精确知哪个挂了），回落到 stderr 签名扫描
            #            （partial_tool_failure，兼容吞没式失败 / fanout-off 单子进程路径）。
            #   repaired：预测经 try_fix_yaml 修复解析的次数——输出截断（finish_reason=length）或轻度畸形的
            #             弱信号，条目可能被静默修丢（排查盲区 S3）。
            _LAST_META.update(
                partial_tool_failure=(_partial_side
                                      or partial_tool_failure(data, stderr)),
                repaired_parses=stderr.count(_REPAIRED_PARSE_SIG))
            # review_engaged 统一在 PRAgentProvider.fetch 出口经 _extract_engaged(data) 设置
            # （覆盖注入与子进程两路径）；此处不重复设。
            return data
        finally:
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass    # 静默豁免：临时文件清理 best-effort，主结果已在 try 内返回/抛出。


# 本次 invoke 的诊断元信息（部分降级/修复解析计数）。单次 CLI 进程内串行使用；
# fetch() 开头重置，_invoke_endpoint 填充，orchestrator 经 invoke_meta() 读取。
# 离线注入路径（pr_agent_output）不产生 stderr，保持默认值。
_LAST_META = {"partial_tool_failure": None, "repaired_parses": 0,
               "review_engaged": False, "raw_review_excerpt": None}


def invoke_meta():
    """最近一次 fetch 的诊断元信息（拷贝）。"""
    return dict(_LAST_META)


def fetch(pr_ctx, provider=None):
    """按 provider 取评审观察（默认 pr-agent；目前仅此一种，未知 provider 抛错）。
    orchestrator.review_pr 已直连本函数；REVIEW_PROVIDER 留作未来接入其它评审来源的开关。"""
    _LAST_META.update(partial_tool_failure=None, repaired_parses=0,
                       review_engaged=False, raw_review_excerpt=None)
    if callable(provider):                       # 注入观察源（自检/测试 seam）：直接返回原始 ReviewItem 列表，
        return list(provider(pr_ctx) or [])      # 不触发 PR-Agent 子进程 → 零网络。见 doctor.smoke_review。
    provider = provider or os.environ.get("REVIEW_PROVIDER", "pr-agent")
    if provider == "pr-agent":
        return PRAgentProvider().fetch(pr_ctx)
    raise ValueError(f"未知评审提供器: {provider}")


# ---- 发现归一：ReviewItem → 本系统 Finding ---------------------------------
def normalize(items, nmap=None):
    """把 PR-Agent 的 ReviewItem 按 nmap 映射成本系统 Finding（与 contract_check 同构，
    供下游裁决映射/总闸/校准直接复用）。agent 记来源（pr-agent:suggestion / pr-agent:review）。"""
    nmap = nmap or _DEFAULT_NMAP
    # 标签→类别的大小写归一 + 冲突 fail-loud 见 _build_label_maps（机制与理由集中在此）。默认 nmap 已
    # 在 import 时预计算（_DEFAULT_L2C/_DEFAULT_DISCARD）——复用、免每轮重算不可变映射；仅自定义
    # nmap 每次重建（不能假设传入 dict 不可变）。
    if nmap is _DEFAULT_NMAP:
        l2c, discard = _DEFAULT_L2C, _DEFAULT_DISCARD
    else:
        l2c, discard = _build_label_maps(nmap)
    findings = []
    for it in items or []:
        # 输入侧与键侧同样防御：非字符串 label（上游解析出的数字等）直接 .lower() 会 AttributeError，
        # 应落 default_category 而非崩（与键侧 str(k).lower() 对称）。
        # 显式 None 检查（round-2 finding）：`or ""` 会吞掉 falsy 但有效的 label——`0 or ""` 得 ""，
        # 于是 label=0 与缺失不可区分、rule_id 退回 kind 兜底，与本 PR「防御非字符串 label」自相矛盾
        # （测试盖了 7，0 反被错处理）。只把 None/缺失当空；其余（含 0/False）经 str() 保留原值。
        raw_label = it.get("label")
        label = "" if raw_label is None else str(raw_label)
        if label.lower() in discard:
            continue
        cat = l2c.get(label.lower(), nmap.get("default_category", "convention"))
        rid = "PRA-" + (label or it.get("kind", "review")).replace(" ", "_").upper()
        # 修订设计 §4.2（评审意见 2）：模型来源只给方向与依据，不给动手级指令。
        # suggestion 的 body 可能是 improved_code（补丁）——按设计降级为方向描述，不进任何建议字段；
        # deterministic_patch 仅确定性来源（confidence=1.0 规则命中）可填，模型来源禁填。
        direction = it.get("summary") or ""
        reasoning = it.get("reason") or ""
        if reasoning == direction:
            reasoning = ""            # 说明文字与方向重复时不复读
        # line_start 缺失时回退 line_end（借鉴 pr-agent 上游 #2510 评审定位精度）：
        # pr-agent review 类的 key_issues 偶不带 start_line 但带 end_line；此前 line=None
        # → 渲染 `file:None` + sig 含 `:None` 字面量。line_end 回退让 sig 有真实行号、
        # 跨轮 reconcile 更稳（行号稳定而非全部塌缩到 :None）。两层兜底：此处取值 +
        # render._location 渲染侧仍防 `:None`。
        # 用 `is not None` 而非 `or`：line_start=0 虽罕见（GitHub diff 行号 1-based），
        # 但 0 是 falsy，`or` 会错误跳过 0 回退到 line_end。与 render._location 的
        # `is not None` 检查保持一致（两侧同样的 None 判定逻辑）。
        ls = it.get("line_start")
        line = ls if ls is not None else it.get("line_end")
        findings.append({
            "rule_id": rid,
            "file": it.get("file"),
            "line": line,
            "category": cat,
            "severity": nmap.get("default_severity", "warn"),
            "confidence": nmap.get("default_confidence", 0.7),
            "rationale": it.get("summary") or it.get("body"),
            "fix_direction": direction,
            "fix_reasoning": reasoning,
            # 达成判据（评审意见 1）——诚实降级：model 来源在 normalize 层只有 one_sentence_summary
            # （=direction），不足以生成设计文档所要求的「一句可回答的具体复核问题」（示例：
            # 「新增的回滚路径是否覆盖了跨模块调用失败的分支？」）。此前用固定模板
            # 「{direction}」是否已按方向解决？套 direction 当占位 → 退化为止复读修复方向，
            # 零新信息。诚实降级：question 留空，渲染层退为「下一轮复检不再命中即销项」
            # （reconcile 实际机制：sig 不再现即自动销项，本就不靠判据文本）。确定性来源
            # （contract/probe）仍给真实机器可验判据（recheck rule_id / replay mutant）。
            "done_criteria": {"kind": "review", "spec": {"question": ""}},
            "suggested_fix": direction,   # 已废弃字段的过渡别名（=方向，不含补丁），供旧消费方
            "agent": "pr-agent:" + it.get("kind", "review"),
        })
    return findings


# ---- 裁决映射：Finding → 三类判定/风险等级（不做共识，PR-Agent 已去重排序）----
_HUMAN = {"high": "read+arbitrate", "mid": "read", "low": "skip"}


# 影响面严重因子：高风险 + 命中其一 → 升到 full_suite（最强一档，多跑变异）。
_SEVERE_BLAST = {"cross_module_contract", "security_surface"}

# 确定性影响面：直接从改动文件【路径】判定，不依赖 PR-Agent 给的 category。
# 目的：即便评审侧误判了类别（把该 high 的改动判成 low），命中这些路径的改动仍会被
# 强制抬到 high → full_suite，并触发（可选的）自动合并否决。这是「风险分流的安全性不能
# 全押在会误判的判断层」的确定性兜底（对应主设计 §5 该遗留项的缓解，此处落地）。
_DET_BLAST_PATTERNS = {
    "cross_module_contract": [
        r"(^|/)migrations?/", r"\.sql$", r"\.proto$", r"\.graphql$", r"\.avsc$", r"\.thrift$",
        r"(^|/)schema[./]", r"schema\.\w+$", r"openapi", r"swagger",
    ],
    "security_surface": [
        r"(^|/)(auth|oauth|iam|security|crypto|secrets?|credentials?)([/_.]|$)",
        r"(password|keystore|private[_-]?key)",
    ],
}


def deterministic_blast(changed_files):
    """从改动文件路径确定性推断影响面因子（不依赖 LLM 类别）。命中即强证据。"""
    files = [str(f).lower() for f in (changed_files or [])]
    out = []
    for factor, pats in _DET_BLAST_PATTERNS.items():
        if any(any(re.search(p, f) for p in pats) for f in files):
            out.append(factor)
    return out


def route(risk):
    """§4.2 风险分流（真实实现）：按 risk_band（高风险时再看影响面）决定人看不看、跑哪一档验证。
    三档充分性阶梯（§6.2）：
      低/中            → cheap_only（仅廉价信号）
      高               → targeted_tests（生成针对性验收测试 + 覆盖/哨兵）
      高 且 影响面严重 → full_suite（在 targeted 基础上再跑变异测试——最强、最贵的一档）
    map_verdict 在定级后调用本函数，填入 human_action / verification_decision。

    注：契约/安全类违例（contract/security category）由【确定性门禁】以 block_candidate
    severity 拦截，不依赖此处的验证档——验证(verify)只管 correctness（spec-blind 验收测试）。
    故契约变更落到 mid/cheap_only 不构成漏洞：它的拦截发生在门禁，不在 verify。"""
    band = risk.get("risk_band")
    if band == "high":
        severe = bool(_SEVERE_BLAST & set(risk.get("blast_radius") or []))
        vd = "full_suite" if severe else "targeted_tests"
    else:
        vd = "cheap_only"
    return {"human_action": _HUMAN.get(band, "read"), "verification_decision": vd}


def map_verdict(findings, nmap=None, changed_files=None, scope_facts=None):
    """把归一后的 Finding 按 category 映射到风险等级与验证预算决策（风险路由）。
    取代自研 aggregate 的"评审侧定级"，但去掉去重/共识——那由 PR-Agent 完成。
    返回 (过滤后 findings, RiskAssessment)，结构与主文档 RiskAssessment 一致。
    scope_facts（修订设计 §4.1，评审意见 7）：范围事实的 sensitive_hits 按仓级路径规则
    （.touchstone/scope-rules.yaml）确定性点亮影响面——推导顺序为路径规则命中（确定性）∪
    发现类别推导（模型，补充），模型漏报不再导致影响面漏判。"""
    nmap = nmap or _DEFAULT_NMAP
    conf_min = nmap.get("conf_min", 0.5)
    kept = sorted((f for f in (findings or []) if f.get("confidence", 0) >= conf_min),
                  key=lambda x: -x.get("confidence", 0))
    cats = {f.get("category") for f in kept}
    blast = []
    if "security" in cats:
        blast.append("security_surface")
    if "contract" in cats:                 # 一般来自 contract_check，而非 PR-Agent
        blast.append("cross_module_contract")
    if scope_facts and changed_files is None:
        changed_files = [f["path"] for f in scope_facts.get("changed_files", [])]
    det = set(deterministic_blast(changed_files))  # 确定性影响面（按路径，不信 LLM 类别）
    if scope_facts:                                # 范围事实的仓级规则命中（可配置的确定性影响面）
        det |= {h["rule"] for h in scope_facts.get("sensitive_hits", [])}
    blast = sorted(set(blast) | det)
    # 命中高危类别，或【确定性】命中严重影响面 → high。后者保证：即便评审侧漏判类别，
    # 触及 migration/schema/proto/安全面的改动也会被抬到 full_suite、并被自动合并否决拦下。
    high = bool(set(nmap.get("high_categories", ["security", "correctness"])) & cats) \
        or bool(_SEVERE_BLAST & set(det))
    band = "high" if high else ("mid" if kept else "low")
    risk = {"risk_band": band, "blast_radius": blast}
    risk.update(route(risk))          # §4.2 风险分流：人看不看 / 跑哪档验证
    return kept, risk


def to_rdjson(findings, source_name="touchstone"):
    """把发现转成 Reviewdog Diagnostic Format(rdjson)——成熟行内评论后端的接缝：
    reviewdog 处理行锚定长尾（过滤模式/位置修正），本系统不必自研。纯函数，供导出。"""
    sev = {"block_candidate": "ERROR", "warn": "WARNING"}
    return {"source": {"name": source_name},
            "diagnostics": [{
                "message": (f.get("rationale") or f.get("rule_id") or ""),
                "code": {"value": f.get("rule_id") or ""},
                "location": {"path": f.get("file") or "",
                             "range": {"start": {"line": int(f.get("line") or 1)}}},
                "severity": sev.get(f.get("severity"), "INFO"),
            } for f in (findings or [])]}

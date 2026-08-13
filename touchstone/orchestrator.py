#!/usr/bin/env python3
# ============================================================================
# touchstone/orchestrator.py  ——  Touchstone 主编排（评审复用 PR-Agent）
# ----------------------------------------------------------------------------
# 形态：advisory。只产出评分与发现、回贴到 PR，**绝不拦截合入**，与人工审核并行。
# 链路：load_standards/contract → get_pr_diff → review_provider.fetch(PR-Agent) → normalize
#        → map_verdict(按 category 定风险等级，不做共识) + contract_check(确定性契约一致性)
#        → 回贴(摘要 + 尽力内联 + 中性 check run) → 写 touchstone-findings.json
# 评审引擎复用开源 PR-Agent（见 docs/touchstone-on-pr-agent.html）；自研委员会已退役。
# touchstone 不再直接调 LLM——PR-Agent 自带端点配置；touchstone 只做归一/裁决/门禁/回贴。
# 依赖：GitHub 走 requests(ghclient)、diff 解析用 unidiff、配置 pyyaml。
#   GITHUB_API_URL  缺省 https://api.github.com；用 GitHub Enterprise 在此改。
# ============================================================================

import json
import os
import time
import re
import sys

import requests

import yaml

from touchstone import ghclient             # GitHub HTTP 客户端(requests)
from touchstone import checks                # 可插拔检查框架 + 总闸
from touchstone import loop                  # 反馈循环控制器
from touchstone import contract_check        # 提交契约一致性核对（确定性）
from touchstone import review_provider       # 评审提供器(复用 PR-Agent) + 发现归一 + 裁决映射
from touchstone import autonomy              # 变更分类计算（供自治经验层/auto_merge 重建）
from touchstone import stack_rules           # §4.1 栈专项确定性规则（machine_checkable 的 SPR/JAVA/CTR）
from touchstone import checklist as checklist_mod   # 收敛清单（修订设计 §4.3，评审意见 1、3）
from touchstone import lineage               # 轮次台账与同源检测（修订设计 §4.4，评审意见 10）
from touchstone.atomicio import atomic_write_json   # 状态文件原子写（决策输入不留半文件）
from touchstone.artifacts import artifact_path      # 统一产物路径（默认 CWD，可经 OUTPUT_DIR 隔离）
# 渲染层已拆至 touchstone/render.py（v2 六段版面填充；模块职责单一化）。此处再导出以保持
# 既有引用路径 orchestrator.render_* 兼容（测试与外部调用无需改动）。
# v2：render_facts/render_findings（v1 七段版面的两段渲染函数）已随版面合并移除——态势区→
# render_status_line、AI 评审+清单→render_findings_checklist、静态检查→render_facts_v2。
# 再导出仅保留仍存在的 render_report / render_summary。
from touchstone.render import (_load_template,  # noqa: F401
                               render_report, render_summary)

# --- 配置 ---------------------------------------------------------------------
STANDARDS_PATH = os.environ.get("TOUCHSTONE_STANDARDS", ".touchstone/standards.yaml")
CONTRACT_PATH  = os.environ.get("TOUCHSTONE_CONTRACT",  ".touchstone/pr.yaml")

# --- GitHub API（stdlib） -----------------------------------------------------
def gh(method, path, token, data=None, accept="application/vnd.github+json"):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return ghclient.request(method, base + path, token, data=data, accept=accept)


# --- 输入加载 -----------------------------------------------------------------
def load_yaml(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_pr_diff(owner, repo, number, token):
    """取 PR 全文 diff——确定性核对（SEC-001 等）必须覆盖全文，安全保证不随体量打折扣。
    LLM 侧的上下文限制由 pr-agent 自己管理（它取全文 PR + 用 custom_model_max_tokens 做
    max_tokens）；touchstone 的确定性核对（密钥扫描/契约/栈规则）是纯正则/AST，不进 LLM，
    不受 diff 体量影响。超大体量 PR 默认走 SIZE-001 体量门禁拆分（TOUCHSTONE_MAX_DIFF_LINES
    默认 1000 行；设 0 关闭、或调高/调低阈值）。"""
    return gh("GET", f"/repos/{owner}/{repo}/pulls/{number}", token,
              accept="application/vnd.github.v3.diff")


# --- 回贴 ---------------------------------------------------------------------
def anchor_inline(findings, diff):
    """把发现锚到 PR diff 的可评论行(RIGHT 侧新增行)。
    - 行恰在新增行上 → 直接锚。
    - 行不在新增行上(如指向被删代码/上下文外) → 就近锚到同文件最近新增行，注明原行。
    - 该文件无任何新增行(纯删除/重命名) → 不内联(靠摘要覆盖)。
    GitHub 要求内联评论落在 diff 内的可评论行，否则整条 review 被拒。"""
    _, added = contract_check.parse_diff(diff or "")

    def _fm(f):
        return ("<!-- touchstone-finding: "
                + json.dumps({"rule_id": f.get("rule_id"), "agent": f.get("agent")},
                             ensure_ascii=False) + " -->")
    out = []
    for f in findings:
        path, line = f.get("file"), f.get("line")
        if not path or not line:
            continue
        addl = sorted(n for n, _ in added.get(path, []))
        if not addl:                       # 文件无新增行 → 降级，只进摘要
            continue
        if line in addl:
            anchored, note = line, ""
        else:                              # 就近锚定，并注明原始行号
            anchored = min(addl, key=lambda n: abs(n - line))
            note = f"（原指 :{line}）"
        out.append({"path": path, "line": anchored, "side": "RIGHT",
                    "body": f"`{f['rule_id']}`{note} {f.get('rationale', '')}"
                            f"\n方向：{f.get('fix_direction') or f.get('suggested_fix', '')}\n{_fm(f)}"})
    return out


def ci_verdict(owner, repo, head_sha, token):
    """读 head 的 check-runs 总判定，供反馈循环判断 CI/verify 是否红。
    排除 touchstone 自身的 check（neutral·advisory，不参与）。
    返回 True=全绿/中性、False=有失败、None=仍有未完成或无数据（未知不强制 author 继续）。"""
    try:
        data = gh("GET", f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs", token)
    except requests.exceptions.RequestException:
        return None
    runs = [r for r in (data.get("check_runs") or [])
            if not str(r.get("name", "")).startswith("touchstone")]
    if not runs:
        return None
    if any(r.get("status") != "completed" for r in runs):
        return None                      # 还有未跑完 → 未知
    bad = {"failure", "timed_out", "cancelled", "action_required", "stale"}
    if any(r.get("conclusion") in bad for r in runs):
        return False
    return True


def _run_link():
    """构造本次 workflow run 的链接（Actions 自动注入的 env）。用于在评审评论里指向
    pr-agent-interaction artifact（完整 LLM 交互日志）。非 Actions 环境返回空。"""
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return ""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return ""
    return f"{server}/{repo}/actions/runs/{run_id}"


def _engine_banner(engine_status):
    """评审引擎降级的人可见说明（防静默故障）。贴在评审评论顶部 + check-run 标题里。"""
    if engine_status == "no_engine":
        return ("⚠️ **AI 评审未运行**：PR-Agent 未安装或不可用，本次评审**只含确定性契约与栈规则核对**，"
                "不含 LLM 代码评审。请确认 workflow 安装了 pr-agent（见 README「GitHub 集成」）。")
    if engine_status == "provider_failed":
        return ("⚠️ **AI 评审取 PR 失败**：PR-Agent 已启动但无法获取该 PR（git provider/凭据/网络），"
                "本次**只含确定性核对**。请检查 pr-agent 的 GitHub token（`GITHUB_TOKEN`）与 "
                "`git_provider` 配置。")
    if engine_status == "llm_failed":
        return ("⚠️ **AI 评审的 LLM 调用失败**：PR-Agent 已运行但 LLM 端点未成功响应，本次**只含确定性核对**。"
                "请检查 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 配置与端点可达性。")
    return ""


def _clean_review_trace(engine_status, ai_raw_count, added_lines, n_changed, raw_excerpt=None):
    """0 条发现时的溯源（防静默故障）：让人区分"LLM 真审了没问题"与"pr-agent 没真审/被过滤光"。
    仅在引擎正常（ok）且无降级时输出；降级由 _engine_banner 负责。

    ai_raw_count==0（无 key_issues、无 code_suggestions 的实质意见）时，附 LLM 真实返回的 review
    结构段快照（raw_excerpt，见 review_provider.extract_review_excerpt）——打消"0 是否真审过"的疑虑：
    glm 干净评审仍会填 estimated_effort/relevant_tests/security_concerns 等段，贴出来即可见"审过、
    只是没实质问题"，而非空回/被吞没（PR #55 评审意见）。"""
    if engine_status != "ok":
        return ""
    suspicious = added_lines >= 20 and ai_raw_count == 0   # 改动不小却 0 原始建议
    head = "🟢 **AI 评审已端到端运行**（PR-Agent + LLM 已调用，非模板空回）。"
    detail = f"PR-Agent 返回 **{ai_raw_count} 条原始建议**（归一后 0 条进入评审）；确定性契约/栈核对 0 命中。"
    tail = ("**改动不小却 0 建议——建议人工扫一眼**（LLM 可能未实质产出）。" if suspicious
            else "改动规模小，0 建议合理。")
    # 拆行（替代旧全角空格连写的一长句）——render_report 把横幅包成逐行 blockquote，更可扫读；
    # 去掉「改动：N 文件/N 行」行——与「确定性事实」段的"修改范围"重复（去冗余）。定性提示
    # （"改动不小却 0 建议"）保留，足够承载防静默故障信号。n_changed 不再进文本，保留入参以稳
    # 签名（调用方与测试按位置传 added_lines/n_changed）。
    trace = f"{head}\n{detail}\n{tail}"
    # 无实质意见时贴 LLM 原始 review 段，证明"审过"而非"空回"。raw_excerpt 已单行化+截断（extract_review_excerpt）。
    if ai_raw_count == 0 and raw_excerpt:
        segs = "\n".join(f"- `{k}`: {v}" for k, v in raw_excerpt.items())
        trace += (f"\n\n**LLM 原始评审**（glm 真实返回的 review 段，证明确实审了；"
                  f"key_issues / code_suggestions 均空 = 审完无实质问题）：\n{segs}")
    return trace


# 凭据脱敏：engine_detail（来自 ReviewEngineDegraded.reason / 过滤后 stderr）在降级场景被原样贴进
# 【公开 PR 评论】。litellm 详错 / verbose 轨迹可能夹带 Authorization 头或 api key（开
# TOUCHSTONE_LITELLM_VERBOSE 尤甚）；review_provider 仅抽取错误正文、不做脱敏。故在【呈现边界】
# 补这层防御——尽力而为，只抹凭据形子串，错误正文/traceback 保留可读（PRA-REVIEW 安全发现，PR #74）。
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.=]+"),          # Authorization: Bearer xxx
    re.compile(r"(?i)\bAuthorization\b['\"\s]*[:=]\s*['\"]?[A-Za-z0-9_\-\.=]+"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),                    # OpenAI / Anthropic 风格 key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),                # GitHub PAT（ghp_/ghs_/gho_/ghu_/ghr_）
    re.compile(r"(?i)(api[_-]?key|access[_-]?token)\b['\"\s]*[:=]\s*['\"]?[A-Za-z0-9_\-\.=]{16,}"),
)


def _redact_secrets(text):
    """抹掉凭据形子串（Bearer / Authorization / sk- / gh_ / api_key=…）→ ***REDACTED***。纯函数、可测。"""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


def _render_engine_detail(engine_status, engine_detail):
    """降级时把 engine_detail 渲染成「验证与日志」段里的原始错误块；engine 正常或无 detail → 空串。
    进公开 PR 评论前三连：①_redact_secrets 脱敏凭据 ②超 1500 字符加截断标记（不静默砍尾）
    ③四反引号围栏（raw error 自身含 ``` 不再撑破版面）。纯函数、可测（PRA-* PR #74）。"""
    if engine_status == "ok" or not engine_detail:
        return ""
    detail = _redact_secrets(engine_detail.strip())
    shown = detail[:1500] + (
        "\n[…]（已截断：原始错误超 1500 字符，完整内容见 `pr-agent-interaction.log`）"
        if len(detail) > 1500 else "")
    return (f"**评审引擎降级（`{engine_status}`）——原始错误：**\n\n"
            f"````\n{shown}\n````\n"
            "更完整的 litellm 调用轨迹 / 真实 HTTP 错误见交互日志 artifact `pr-agent-interaction.log`。")


def post_results(owner, repo, number, head_sha, token, risk, findings, loop_info=None,
                 change_class=None, diff=None, injected_types=None, injected_experience_ids=None,
                 shadow_types=None, shadow_experience_ids=None,
                 engine_status="ok", det_warning="", ai_raw_count=0, added_lines=0, n_changed=0,
                 scope_facts=None, checklist=None, rounds_left=None, ledger=None,
                 review_reliable=True,
                 llm_notes=None, raw_excerpt=None, unverified_claims=0, telemetry_status="disabled",
                 engine_detail=""):
    # (1) 摘要评论——总是成功；按 v2 六段版面模板组装：
    #     ①标题+状态行 ②告警 ③静态检查 ④评审发现与销项 ⑤参考信息 ⑥机器 marker
    # 评审不可信时，降级说明/0-发现溯源统一并入 render 层的 [!CAUTION] 置顶告警
    # （见 render.render_unreliable_callout；判定层的 review_reliable 信号在此接到呈现层）；
    # 可信时保持原逻辑。det_warning（确定性侧警告）与可信度无关，两种情形都保留。
    # v2：循环状态行不再并入 alerts——归 render_status_line（① 状态行），alerts 只载告警。
    alerts = "" if not review_reliable else _engine_banner(engine_status)
    if det_warning:
        alerts = (alerts + "\n\n" if alerts else "") + f"⚠️ **{det_warning}**"
    if review_reliable and not alerts and not findings:
        # 引擎正常且可信的 0 发现：附溯源，让人区分"LLM 真审了没问题"与"没真审"
        alerts = _clean_review_trace(engine_status, ai_raw_count, added_lines, n_changed,
                                     raw_excerpt=raw_excerpt)
    for note in (llm_notes or []):
        alerts = (alerts + "\n\n" if alerts else "") + note
    if unverified_claims:
        # author 自证销项点名——advisory 下提示人核准，autonomy 下已独立拦（no_unverified_claims 闸）
        alerts = (alerts + "\n\n" if alerts else "") + (
            f"🟡 **{unverified_claims} 条 waived/split 系 author 自证、机器未验证**："
            "这些豁免/拆分需人核准，不计入机器可验证收敛，也不触发自动放行。")
    # 遥测上报状态进告警（防静默故障：可观测性子系统自身状态对运维可见，不只进 stderr）。
    # 仅在启用遥测时显示——默认关（disabled）时不加行，免噪声。failed 时附原因，让人能定位。
    if telemetry_status and telemetry_status != "disabled":
        if telemetry_status == "ok":
            _tel_line = "📡 **遥测**：已上报本轮指标"
        else:
            _reason = (telemetry_status[len("failed:"):].strip()
                       if telemetry_status.startswith("failed:") else telemetry_status)
            _tel_line = f"📡 **遥测**：上报失败（不阻塞评审）— {_reason}"
        alerts = (alerts + "\n\n" if alerts else "") + _tel_line
    # 同源提示（轮次台账）：v2 统一到告警段（旧版在静态检查+清单两处重复呈现）。
    if ledger and ledger.get("lineage"):
        _entries = [e for e in ledger.get("lineage", []) if isinstance(e, dict) and "number" in e]
        if _entries:
            hist = "、".join(f"#{e['number']}（{e.get('rounds', '?')} 轮）" for e in _entries)
            _lin = (f"⚠️ 与已关闭的 {hist} 内容同源：历史已消耗 {ledger.get('rounds_spent', 0)} 轮，"
                    f"未销项 {len(ledger.get('inherited_open_items', []))} 条已并入本清单，"
                    f"剩余轮次按台账计。人工重置请打 `rounds-reset` label。")
            alerts = (alerts + "\n\n" if alerts else "") + _lin
    # 机器 marker 段：loop 状态 marker + checklist 权威状态 marker（机读，永远存在）。
    markers = []
    if loop_info:
        markers.append(loop_info[2])           # loop state marker（render_status_line 用 loop_info[0/1]）
    if checklist:
        markers.append(checklist_mod.render_marker(checklist))
    # 验证档（verification_decision）是机器路由信号——决定 CI 跑哪档验证，非给人的待办；降为行尾小字。
    _VD = {"cheap_only": "仅基础检查（不额外跑验证）",
           "targeted_tests": "针对性验收测试",
           "full_suite": "完整验证（针对性测试 + 变异测试）"}
    _vd = risk.get("verification_decision")
    run_link = _run_link()
    # 参考信息「验证与日志」<details>：链接为主、验证档降小字；LLM 失败时把具体可靠的
    # 原始错误（PR#68 做准的 reason）详列在此（CAUTION 只给精简指向）。
    _vblocks = []
    if run_link:
        _vd_note = (f" <sub>（本轮验证档：{_VD.get(_vd, _vd or '—')}）</sub>" if _vd else "")
        _vblocks.append(f"📄 完整验证运行与 LLM 交互日志：{run_link}{_vd_note}")
    _ed_block = _render_engine_detail(engine_status, engine_detail)
    if _ed_block:
        _vblocks.append(_ed_block)
    body = render_report(risk, findings, alerts=alerts, scope_facts=scope_facts,
                         checklist=checklist, rounds_left=rounds_left, loop_info=loop_info,
                         verification_blocks=_vblocks,
                         markers="\n".join(markers), gate_line="",
                         review_reliable=review_reliable, engine_status=engine_status,
                         ai_raw_count=ai_raw_count, added_lines=added_lines, engine_detail=engine_detail)
    # 机读 result marker（隐藏）——校准/自治经验从 API 重建数据的入口
    result_marker = "<!-- touchstone-result: " + json.dumps({
        "risk_band": risk["risk_band"],
        "verification_decision": risk["verification_decision"],
        "change_class": change_class,
        "loop_decision": (loop_info[0] if loop_info else None),
        "injected_types": injected_types,          # 本轮注入的经验类型（供 shadow A/B 分臂采集）
        "injected_experience_ids": injected_experience_ids,   # 本轮注入的经验【id】（单条归因/回退，见数据采集设计 取舍2）
        "shadow_types": shadow_types or [],              # 本轮 shadow 注入的 candidate 类型（破冷启动死锁，with 臂归因；SHADOW_INJECTION 开时非空；None→[] 稳定 list 类型）
        "shadow_experience_ids": shadow_experience_ids or [],   # 本轮 shadow 注入的 candidate【id】（单条归因/回退；None→[]）
        "findings": [{"rule_id": f.get("rule_id"), "agent": f.get("agent"),
                      "severity": f.get("severity")} for f in findings],
        "unverified_claims": unverified_claims,
    }, ensure_ascii=False) + " -->"
    body = body + "\n\n" + result_marker
    try:
        gh("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", token, {"body": body})
    except requests.exceptions.RequestException as e:
        print(f"[warn] 摘要评论失败: {e}", file=sys.stderr)
    # (2) 尽力内联评论（event=COMMENT，绝不 REQUEST_CHANGES）
    #     锚定到 diff 可评论行（删除行/超界行就近锚或降级）；每条附自识别隐藏标记
    if diff is not None:
        inline = anchor_inline(findings, diff)
    else:
        def _finding_marker(f):
            return ("<!-- touchstone-finding: "
                    + json.dumps({"rule_id": f.get("rule_id"), "agent": f.get("agent")},
                                 ensure_ascii=False) + " -->")
        inline = [{"path": f["file"], "line": f["line"], "side": "RIGHT",
                   "body": f"`{f['rule_id']}` {f.get('rationale','')}\n方向：{f.get('fix_direction') or f.get('suggested_fix','')}"
                           f"\n{_finding_marker(f)}"}
                  for f in findings if f.get("file") and f.get("line")]
    if inline:
        try:
            gh("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", token,
               {"event": "COMMENT", "comments": inline})
        except requests.exceptions.RequestException as e:
            print(f"[info] 内联评论降级(行不在 diff 内属正常): {e}", file=sys.stderr)
    # (3) 中性 check run（advisory，永不 failure）
    if head_sha:
        flag = "⚠️ 评审降级 · " if (engine_status != "ok" or det_warning) else ""
        try:
            gh("POST", f"/repos/{owner}/{repo}/check-runs", token, {
                "name": "touchstone", "head_sha": head_sha, "status": "completed",
                "conclusion": "neutral",
                "output": {"title": f"{flag}风险等级 {risk['risk_band']} · {len(findings)} 条发现",
                           "summary": body[:600]},
            })
        except requests.exceptions.RequestException as e:
            print(f"[info] check run 跳过: {e}", file=sys.stderr)


# --- main ---------------------------------------------------------------------
def _collect_injection():
    """取本轮要写入 result marker 的经验注入类型：active（生产路径）+ shadow（实验路径，env 开时）。
    与 review_provider._experience_injection 同源（只读经验库、失败即空）。
    active 是生产路径、shadow 是实验路径——shadow 取值用【独立内层】try/except 隔离：shadow 抛异常
    只丢弃 shadow、不 wipe 已成功取到的 active（pr-agent review #117 指出的失败隔离点）。四者皆
    失败即空（marker 写空）。shadow 仅 TOUCHSTONE_SHADOW_INJECTION 开时才取（默认关=字节级不变，
    需 step4 review_provider include_shadow 透传后才不归因失真——见 _shadow_injection_enabled）。"""
    injected_types, injected_experience_ids = [], []
    shadow_types, shadow_experience_ids = [], []
    try:
        from touchstone import learning_loop as _ll
        _store = _ll.load_store()
        injected_types = _ll.active_types(_store)
        injected_experience_ids = _ll.active_ids(_store)
        if _ll._shadow_injection_enabled():
            try:
                shadow_types = _ll.shadow_types(_store)
                shadow_experience_ids = _ll.shadow_ids(_store)
            except Exception:
                shadow_types, shadow_experience_ids = [], []
    except Exception:
        injected_types, injected_experience_ids = [], []
    return injected_types, injected_experience_ids, shadow_types, shadow_experience_ids


def _max_diff_lines():
    """SIZE-001 体量门禁阈值。空串（vars 未创建时 `${{ vars.X }}` 透传的常态）回落默认 1000，
    只有显式 "0" 才关闭——上游报告问题三：此前空串经 `or 0` 静默关闭门禁，超大 PR 直送 LLM 且无提示。"""
    raw = (os.environ.get("TOUCHSTONE_MAX_DIFF_LINES") or "").strip()
    if not raw:
        return 1000
    try:
        return int(raw)
    except ValueError:
        print(f"[warn] TOUCHSTONE_MAX_DIFF_LINES={raw!r} 非数字，回落默认 1000（SIZE-001 门禁保持生效）",
              file=sys.stderr)
        return 1000


def review_pr(pr, contract, standards, provider=None):
    """§4.1 主入口：复用 PR-Agent 评审 → 发现归一 → 提交契约核对 + 栈专项确定性规则 → 裁决映射。
    等价于 map_verdict( normalize(fetch(pr)) + check_contract_consistency(...) + check_stack_rules(...) )。
    pr：上下文 dict（owner/repo/number/sha/token/diff/standards 等）；返回 {findings, risk}。
    评审层只产建议与风险分流，不产准入（准入只由质量门禁/总闸决定）。"""
    nmap = review_provider.load_nmap(os.environ.get("REPO_DIR", "."))
    rules = standards.get("rules", []) if isinstance(standards, dict) else (standards or [])
    rule_index = {r["id"]: r for r in rules}
    diff = pr.get("diff", "")
    changed_files, added = contract_check.parse_diff(diff)
    added_lines = sum(len(v) for v in added.values())
    ai_raw_count = 0
    engaged = False         # glm 是否给出实质性多段评审（runner 经 _LAST_META 透出，见 review_provider）
    raw_excerpt = {}        # LLM 原始 review 段快照（0 原始建议时贴横幅，打消"是否真审过"疑虑）
    llm_notes = []          # LLM 侧非致命注记（部分降级/截断修复），进报告横幅
    engine_detail = ""      # 降级/失败时的具体原始错误（PR#68 做准的 reason）——留给渲染层
    max_lines = _max_diff_lines()
    size_findings = []
    if max_lines > 0 and added_lines > max_lines:
        engine_status = "skipped_large_diff"
        size_findings = [{
            "rule_id": "SIZE-001", "file": "", "line": 0,
            "category": "contract", "severity": "block_candidate",
            "confidence": 1.0,
            "rationale": f"PR 改动约 {added_lines} 行，超过单 PR 上限 {max_lines} 行。",
            "fix_direction": "请拆分为多个 PR，每个聚焦一个变更。",
            "fix_reasoning": "一次性提交大量代码增加评审难度与出错风险。",
            "done_criteria": {"kind": "deterministic", "spec": {"recheck": "SIZE-001"}},
            "suggested_fix": "请拆分为多个 PR，每个聚焦一个变更。",
            "agent": "contract-check",
        }]
        review_findings = []
    else:
        engine_status = "ok"
        try:
            raw_items = review_provider.fetch(pr, provider)
            ai_raw_count = len(raw_items)
            review_findings = review_provider.normalize(raw_items, nmap)
            _meta = review_provider.invoke_meta()
            engaged = _meta.get("review_engaged", False)   # review_reliable 据此区分"审完无问题"与"裁空/吞没"
            raw_excerpt = _meta.get("raw_review_excerpt") or {}  # 0 原始建议时贴横幅的 LLM 原始 review 段
            # 部分降级/修复解析：整轮仍可信（另一侧有真实产出/条目仍在），不触发降级，
            # 但必须在报告可见——improve 连挂数日而 review 正常时，建议侧信号长期缺失
            # 却无人察觉；截断修复则意味着条目可能被静默修丢（本次静默故障排查 S1/S3）。
            if _meta.get("partial_tool_failure") == "improve":
                llm_notes.append("⚠️ **本轮 improve 工具失败**：建议侧（code_suggestions）信号缺失，"
                                 "review 侧发现仍有效——非整轮不可信，真实错误见交互日志。")
            elif _meta.get("partial_tool_failure") == "review":
                llm_notes.append("⚠️ **本轮 review 工具失败**：key_issues 侧信号缺失，"
                                 "improve 侧建议仍有效——非整轮不可信，真实错误见交互日志。")
            if _meta.get("repaired_parses"):
                llm_notes.append(f"ℹ️ 本轮有 {_meta['repaired_parses']} 次 LLM 预测经修复解析"
                                 "（输出截断/畸形的弱信号，条目可能被修复丢弃），原文见交互日志。")
        except review_provider.ReviewEngineDegraded as e:
            engine_status = e.degraded
            engine_detail = e.reason or ""      # PR#68 做准的具体原因（工具 + litellm 真实异常 + 过滤后 stderr）
            print(f"[review_pr] 评审引擎降级（{e.degraded}）：{e.reason}", file=sys.stderr)
            review_findings = []
        except RuntimeError as e:
            engine_status = "no_engine"
            engine_detail = str(e)
            print(f"[review_pr] PR-Agent 不可用：{e}", file=sys.stderr)
            review_findings = []
    contract_findings = contract_check.check_contract_consistency(diff, contract or {}, rule_index)
    stack_findings = stack_rules.check_stack_rules(diff, rule_index)
    det_warning = contract_check._PARSE_WARNING or ""
    # 范围事实（修订设计 §4.1，评审意见 7）：确定性修改范围 + 仓级路径规则命中 + 内容指纹
    sf = contract_check.scope_facts(
        diff, contract_check.load_scope_rules(os.environ.get("REPO_DIR", ".")))
    findings, risk = review_provider.map_verdict(
        size_findings + review_findings + contract_findings + stack_findings, nmap,
        changed_files=changed_files, scope_facts=sf)
    return {"findings": findings, "risk": risk, "engine_status": engine_status,
            "det_warning": det_warning, "ai_raw_count": ai_raw_count,
            "added_lines": added_lines, "changed_files": changed_files,
            "scope_facts": sf, "llm_notes": llm_notes, "engaged": engaged,
            "raw_excerpt": raw_excerpt, "engine_detail": engine_detail}


def main():
    _t_main0 = time.monotonic()
    token = os.environ["GITHUB_TOKEN"]

    event = load_yaml(os.environ["GITHUB_EVENT_PATH"]) or {}
    pr = event.get("pull_request", {})
    number = pr.get("number")
    head_sha = pr.get("head", {}).get("sha")
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    if not number:
        sys.exit("非 PR 事件，跳过。")

    standards = load_yaml(STANDARDS_PATH)
    if not standards:
        sys.exit(f"未找到规范 {STANDARDS_PATH}")
    contract = load_yaml(CONTRACT_PATH)
    rule_index = {r["id"]: r for r in standards.get("rules", [])}

    diff = get_pr_diff(owner, repo, number, token)
    changed_files, _ = contract_check.parse_diff(diff)

    # 评审主链（§4.1）：PR-Agent 评审归一 + 契约核对 + 栈专项确定性规则 → 裁决映射
    # 守卫核销预取（issue #139 方案 C）：上轮权威清单的 open 项守卫事实须在评审【前】
    # 注入，故此处轻量早取一次评论解析 marker（与下方反馈循环的正式取用相互独立；
    # 双取代价一次 GET，换取不重排 main 流程）。失败即空——绝不阻塞评审链路。
    guard_adjudication = ""
    try:
        # import 同在 try 内（PR#140 R3 意见 3）：模块导入失败也走"失败即空"降级，
        # 不允许守卫增强层的 ImportError 崩掉 main()——与 attach 面的包裹口径一致。
        from touchstone import guard_context as _gc0
        if _gc0.enabled():
            _pre_comments = gh("GET", f"/repos/{owner}/{repo}/issues/{number}/comments", token)
            _pre_bodies = loop.trusted_bodies(
                _pre_comments if isinstance(_pre_comments, list) else [], None)
            _pre_cl = checklist_mod.parse_latest(_pre_bodies)
            if _pre_cl:
                guard_adjudication = _gc0.render_adjudication(
                    _pre_cl.get("items", []), os.environ.get("REPO_DIR", "."))
    except Exception:
        guard_adjudication = ""
    # repo_dir 显式透传（PR#140 R2 意见 1）：guard_adjudication 用 REPO_DIR 解析，而
    # review_provider 侧 digest 此前回退 pr_ctx.get("repo_dir",".")——REPO_DIR 非默认时
    # 两个守卫面解析目录不一致，digest 静默扫错目录返回空。单一来源，两面同目录。
    pr_ctx_review = {"owner": owner, "repo": repo, "number": number, "sha": head_sha,
                     "token": token, "diff": diff, "standards": standards,
                     "repo_dir": os.environ.get("REPO_DIR", "."),
                     "guard_adjudication": guard_adjudication}
    _t_rev0 = time.monotonic()
    _out = review_pr(pr_ctx_review, contract, standards)
    _t_review = round(time.monotonic() - _t_rev0, 2)
    findings, risk = _out["findings"], _out["risk"]
    engine_status = _out.get("engine_status", "ok")
    det_warning = _out.get("det_warning", "")
    ai_raw_count = _out.get("ai_raw_count", 0)
    llm_notes = _out.get("llm_notes") or []
    engine_detail = _out.get("engine_detail", "")   # 降级原始错误 → CAUTION 精简指向 + 验证与日志详列
    added_lines = _out.get("added_lines", 0)
    n_changed = len(_out.get("changed_files") or [])
    engaged = _out.get("engaged", False)
    raw_excerpt = _out.get("raw_excerpt") or {}
    # 本轮 LLM 评审是否可靠（engine_status + 可疑空收敛判据 + engaged 逃生口）。不可靠时
    # checklist 不予自动销项、loop 不收敛、autonomy 不自动放行--防"diff 被裁空/LLM 随机性"
    # 假收敛放行未评审代码。engaged 让"glm 审完无问题"的干净 PR 不再被误判可疑（PR #51）。
    reliable = review_provider.review_reliable(engine_status, ai_raw_count, added_lines, engaged=engaged)

    # 反馈循环：从历史评论 marker 取状态 → 决策 → 回贴附状态与新 marker。
    # 只信机器人自己发的评论（按发帖人过滤）——否则 author 可自己发伪造 marker 洗掉抗博弈闸。
    all_bodies = []          # 全量评论正文（含 author）——只用于解析 ack 申报（申报是输入信号）
    try:
        comments = gh("GET", f"/repos/{owner}/{repo}/issues/{number}/comments", token)
        comments = comments if isinstance(comments, list) else []
        all_bodies = [c.get("body", "") for c in comments]
        try:
            bot_login = (gh("GET", "/user", token) or {}).get("login")
        except requests.exceptions.RequestException:
            bot_login = None
        if not bot_login:
            # GET /user 未返回身份（默认 GITHUB_TOKEN 常见）——不降级：trusted_bodies 改按
            # [bot] 后缀过滤（github-actions[bot]），防伪造仍生效（人无法注册 [bot] 后缀）。
            print("[info] GET /user 未返回身份：loop marker 改按 [bot] 后缀过滤（防伪造仍生效）",
                  file=sys.stderr)
        bodies = loop.trusted_bodies(comments, bot_login)
    except requests.exceptions.RequestException:
        bodies = []
    state = loop.parse_latest_state(bodies)

    # 轮次台账（修订设计 §4.4，评审意见 10）：同源检测 + 历史继承。台账是增强，失败不阻塞。
    scope_facts = _out.get("scope_facts") or {}
    pr_labels = [l.get("name") for l in (pr.get("labels") or []) if isinstance(l, dict)]
    ledger = lineage.detect_lineage(
        scope_facts.get("fingerprint"), lambda m, p: gh(m, p, token),
        owner, repo, number, current_labels=pr_labels)

    # 收敛清单（修订设计 §4.3，评审意见 1、3）：上一轮权威清单（受信 marker）+ author 申报（ack，
    # 全量评论）→ 按达成判据复核销项 → 新一轮权威清单。首轮并入台账继承的历史未销项。
    prev_cl = checklist_mod.parse_latest(bodies)
    if prev_cl is None and ledger.get("inherited_open_items"):
        prev_cl = {"round": 0, "items": ledger["inherited_open_items"]}
    acks = checklist_mod.parse_acks(all_bodies)
    cur_cl = checklist_mod.reconcile(prev_cl, acks, findings, round_no=state.round + 1,
                                     review_reliable=reliable)
    try:                                       # 守卫事实附着（issue #139 方案 C）：只附着不判断，
        from touchstone import guard_context as _gc2   # 供人 waived 佐证 + 下一轮核销注入复用
        _gc2.attach_guard_facts(cur_cl, os.environ.get("REPO_DIR", "."))
    except Exception:
        pass    # 静默豁免：守卫附着是纯增强，失败即无守卫行；抛出会阻塞收敛清单主链
    checklist_mod.snapshot(cur_cl)          # 本轮快照写入文件（供可视化与校准回放）
    n_unverified = len(checklist_mod.unverified_claims(cur_cl))   # author 自证未核准销项数

    ci_pass = ci_verdict(owner, repo, head_sha, token)   # 供闭环：CI/verify 红则不收敛
    decision, reason, new_state = loop.loop_step(
        findings, rule_index, state, ci_passed=ci_pass,
        checklist_pair=(prev_cl, cur_cl), ledger=ledger, review_reliable=reliable)
    loop_info = (decision, reason, loop.render_marker(new_state))
    # v2：可见清单渲染由 render_report 内 render_findings_checklist 统一负责（合并 AI 评审 +
    # 清单）；此处只算 rounds_left 供状态行/台账用，不再预算 checklist_md。
    _rounds_left = loop.remaining_rounds(
        cur_cl.get("round", 0), ledger.get("rounds_left", loop.MAX_ROUNDS))

    # 变更分类（供自治经验层/auto_merge）：touchstone 侧此时已知 risk/findings/changed_files
    cls = autonomy.change_class(risk, findings, sorted(changed_files), rule_index)
    contract_clean = not any(f.get("agent") == "contract-check" for f in findings)

    # 本轮注入的经验类型（active 生产 + shadow 实验）由 _collect_injection 统一取：shadow 取值独立
    # try 隔离，失败不 wipe active（pr-agent review #117 隔离点）。详见 _collect_injection。
    injected_types, injected_experience_ids, shadow_types, shadow_experience_ids = _collect_injection()

    rd_path = os.environ.get("TOUCHSTONE_RDJSON_PATH")
    if rd_path:                       # 可选 reviewdog 后端：导出 RDFormat，行内投递交 reviewdog
        try:
            with open(rd_path, "w", encoding="utf-8") as _rf:
                json.dump(review_provider.to_rdjson(findings), _rf, ensure_ascii=False)
        except OSError as e:
            print(f"[warn] RDJSON 写出失败: {e}", file=sys.stderr)

    # 可插拔检查 → 对外发【一个】总闸状态（策略全在 .touchstone/checks.yaml）。
    # CI 中由独立 gate job 在(可选)verify 之后聚合并发布，此处置 TOUCHSTONE_SKIP_GATE 跳过自发、
    # 避免重复发；本地/dry-run（未设该环境变量）则就地计算并发布，行为不变。
    # 【顺序】gate 上移到 post_results 之前：可观测性块（metrics/告警/遥测）需 gate（_metrics.build
    # 的 gate= 参数），而遥测上报结果 _tel_res 要进评审报告横幅——故 gate + 可观测性须先于回贴评论
    # 跑完。gate 仍只在非 SKIP 时计算并外发，行为不变。
    gate = None
    if os.environ.get("TOUCHSTONE_SKIP_GATE", "").lower() not in ("1", "true", "yes", "on"):
        try:
            chk_cfg = checks.load_config(os.environ.get("REPO_DIR", "."))
            # 确定性发现 = contract-check（scope/test/dup/untested/sec）+ touchstone-rules（CTR/SPR/JAVA）。
            # 注意：之前这里误引了未定义的 contract_findings（NameError，仅因 gate 路径少被走到而隐藏）。
            det_findings = [f for f in findings
                            if f.get("agent") in ("contract-check", "touchstone-rules")]
            pr_ctx = {"owner": owner, "repo": repo, "sha": head_sha, "token": token,
                      "files": sorted(changed_files), "contract_findings": det_findings}
            gate, _ = checks.post_gate(pr_ctx, chk_cfg, checks.run_checks(chk_cfg, pr_ctx))
        except Exception as e:
            # 总闸是旁路增强（独立 check-run 状态），不是评审交付本身。catch 宽到 Exception：
            # gate 块上移到 post_results 之前（本 PR），若只 catch RequestException，则 checks.*
            # 抛任意非网络异常（如 checks.py 未来改动引入的编程错误、插件聚合异常）会向上冒泡、
            # post_results 永不执行——评审评论被静默吞掉。原本 gate 在 post_results 之后，崩溃也只是
            # 评论已发之后再炸 job；顺序上移后必须拓宽 except 才能守住「总闸崩溃不阻断评审交付」
            # 的既有契约（gate 维持 None，真因进 stderr，防静默故障）。
            print(f"[info] 总闸跳过（不阻断评审）: {type(e).__name__}: {e}", file=sys.stderr)

    # 运行指标（运维可观测性）：每轮追加一条扁平指标到事件流，供 CI 聚合成 dashboard/告警。
    # 与 findings.json（autonomy 决策用的完整状态）分开——本条只含可累加的健康数值。失败不阻塞。
    # 【顺序】整块上移到 post_results 之前：遥测 forward 的结果 _tel_res（ok/disabled/failed）要进
    # 评审报告横幅（防静默故障：可观测性子系统自身状态对运维可见，不只进 stderr），须在回贴评论前
    # 算出。附带好处——回贴评论失败时指标/遥测仍落盘（可观测性不依赖评论投递成功，契合防静默约定）。
    _tel_res = "disabled"   # 默认：未配端点 / 指标产出整体失败时保持（报告里不显示遥测行，免噪声）
    try:
        from touchstone import metrics as _metrics
        _meta = None
        try:
            _meta = review_provider.invoke_meta()
        except Exception as e:
            # meta 是 best-effort（partial_tool_failure/repaired_parses 计数）；取不到按 None，
            # 不阻断指标产出——但留痕，不让降级静默（防静默故障约定）。
            print(f"[info] metrics invoke_meta 取数失败（按 None 继续）: {e}", file=sys.stderr)
        _rec = _metrics.build(
            number, head_sha, risk, findings,
            engine_status=engine_status, review_reliable=reliable,
            ai_raw_count=ai_raw_count, loop_decision=decision, gate=gate,
            unverified_claims=n_unverified, change_class=cls,
            added_lines=added_lines, round_no=new_state.round, invoke_meta=_meta,
            durations={"t_review": _t_review,
                       "t_total": round(time.monotonic() - _t_main0, 2)})
        _metrics.emit(_rec)
        # 告警钩子（可观测性投递）：按 env 选通道，判定并投递到客户自己配置的渠道。
        # 总开关不开 → 无操作（只保留上面的 metrics artifact）。失败绝不冒泡——不拖垮评审 job；
        # 但留痕（防静默故障约定）：告警子系统自身故障不许静默（同 ironic-for-observability）。
        try:
            from touchstone import alert as _alert
            # 聚合取数单独兜底：load/summarize 挂掉（损坏/权限/未来改动）时按 None 继续，
            # 不能让聚合失败连带吞掉本轮单轮告警（silent_failure 等）——它们只依赖 _rec。
            try:
                _agg = _metrics.summarize(_metrics.load())
            except Exception as e:
                print(f"[info] alert 聚合取数失败（按 None 继续，单轮告警仍发）: {e}", file=sys.stderr)
                _agg = None
            _alert.run(_rec, _agg, dict(os.environ),
                       {"owner": owner, "repo": repo, "number": number,
                        "token": token, "run_url": _run_link()})
        except Exception as e:
            print(f"[warn] 告警投递失败（不阻塞评审）: {e}", file=sys.stderr)
        # 使用遥测（可选，默认关）：把本轮 metrics 记录上报到【配置指定】的中心汇聚点。
        # 未配 TOUCHSTONE_TELEMETRY_ENDPOINT → 无操作（不外发）。失败绝不冒泡——同 alert，
        # 可观测性子系统自身故障留痕（防静默故障约定），不拖垮评审 job。
        try:
            from touchstone import telemetry as _tel
            # forward 失败时【返回】"failed: ..."（内部已 catch、不抛）——下面的 except 只兜底
            # forward 自身抛异常的罕见情形。故须显式看返回值：返回失败串时留痕，否则 forward 的
            # 常见失败路径被静默吞（防静默故障：可观测性子系统自身故障不许静默，同 alert 约定）。
            _tel_res = _tel.forward([_rec], dict(os.environ), version=_rec.get("version", ""))
            if isinstance(_tel_res, str) and _tel_res.startswith("failed:"):
                print(f"[warn] 遥测上报失败（不阻塞评审）: {_tel_res}", file=sys.stderr)
        except Exception as e:
            # forward 自身抛异常（罕见）→ 显式置 failed: 串，让报告横幅也显示失败（防静默故障：
            # 异常路径同样可见），并 stderr 留痕。_tel_res 此前未赋值，此处兜底。
            _tel_res = f"failed: {e}"
            print(f"[warn] 遥测上报失败（不阻塞评审）: {e}", file=sys.stderr)
        # 评审健康度看板（可选，默认关）：每轮把健康度写成一个"活"的本仓 issue（重写 body，
        # 静默刷新——GitHub 不为 issue body 编辑发通知），仅显著事件追加评论。未配
        # TOUCHSTONE_METRICS_ISSUE=true → 无操作。失败绝不冒泡——同 alert/telemetry，
        # 可观测性子系统故障只留痕、不拖垮评审 job（run 内部已 catch，此处 except 仅兜底罕见抛出）。
        try:
            from touchstone import metrics_issue as _mi
            _mi_res = _mi.run(_rec, dict(os.environ),
                              {"owner": owner, "repo": repo, "number": number,
                               "token": token, "run_url": _run_link()})
            if isinstance(_mi_res, str) and _mi_res.startswith("failed:"):
                print(f"[warn] metrics-issue 看板更新失败（不阻塞评审）: {_mi_res}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] metrics-issue 钩子异常（不阻塞评审）: {type(e).__name__}: {e}", file=sys.stderr)
    except Exception as e:
        # 指标产出失败不阻塞评审主链——但绝不静默：可观测性子系统自身故障必须留痕
        # （同 learning_loop 2026-07-04 的防静默约定，ironic-for-observability 反模式）。
        # _tel_res 保持 "disabled"：指标整体失败时报告不显示遥测行（避免误导），真因在 stderr。
        print(f"[warn] 运行指标产出失败: {e}", file=sys.stderr)

    post_results(owner, repo, number, head_sha, token, risk, findings, loop_info, cls, diff,
                 injected_types=injected_types, injected_experience_ids=injected_experience_ids,
                 shadow_types=shadow_types, shadow_experience_ids=shadow_experience_ids,
                 engine_status=engine_status, det_warning=det_warning,
                 ai_raw_count=ai_raw_count, added_lines=added_lines, n_changed=n_changed,
                 scope_facts=scope_facts, checklist=cur_cl, rounds_left=_rounds_left,
                 ledger=ledger,
                 review_reliable=reliable, llm_notes=llm_notes,
                 raw_excerpt=raw_excerpt, unverified_claims=n_unverified,
                 telemetry_status=_tel_res, engine_detail=engine_detail)

    # 升级到人：打标签（best-effort）
    if decision == "escalate":
        try:
            gh("POST", f"/repos/{owner}/{repo}/issues/{number}/labels", token,
               {"labels": ["touchstone:needs-human"]})
        except requests.exceptions.RequestException as e:
            # needs-human 标签打不上 = 人工升级信号丢失——escalate 本身已定，标签只是
            # 传达渠道，失败必须可见（否则升级悄悄变没人接）。
            print(f"[warn] escalate 标签添加失败（人工升级信号未传达）: {e}", file=sys.stderr)

    # 校准 + 自治决策入口：落盘供下游 join / auto_merge 组装
    # 原子：这份 findings 是 verify join 与 autonomy auto_merge 的组装依据，进程被杀
    # 留下的半文件会让下游把残缺状态当真（见 atomicio 模块头注）。
    _findings_doc = {"pr": number, "sha": head_sha, "risk": risk, "findings": findings,
                     "changed_files": sorted(changed_files), "loop_decision": decision,
                     "contract_clean": contract_clean, "change_class": cls,
                     "gate": gate,
                     # 引擎健康度（供 autonomy 决策）：engine_status/ai_raw_count/added_lines +
                     # 预算 review_reliable。review_reliable=False 时 autonomy 不自动放行
                     # （防假收敛放行未评审代码，见 review_provider.review_reliable）。
                     "engine_status": engine_status, "ai_raw_count": ai_raw_count,
                     "added_lines": added_lines, "review_reliable": reliable,
                     "review_engaged": engaged,
                     # LLM 原始 review 段快照（0 原始建议时的"真审过"证据，见 _clean_review_trace）
                     "raw_review_excerpt": raw_excerpt,
                     # author 自证但未经人核准的销项数（waived/split）——autonomy 独立闸据此
                     # 拒放行（多层：即便 loop_decision 被虚报，本计数由 touchstone 侧写入）。
                     "unverified_claims": n_unverified,
                     # PR 作者出处（供 autonomy 作者信任闸）：login + GitHub author_association。
                     # 事件 payload 由 Actions 平台生成、作者不可伪造；产物缺/空此字段时
                     # autonomy 侧 fail-closed 不放行。`or {}` 同时兜底 key 缺失与 null 两种
                     # 情形（.get("user", {}) 只挡缺失、挡不住 "user": null 的 None.get() 崩溃）。
                     "author": {"login": (pr.get("user") or {}).get("login"),
                                "association": pr.get("author_association")}}
    atomic_write_json(artifact_path("touchstone-findings.json"), _findings_doc)

    # 风险分流的 job 输出：供下游 verify job 决定是否触发验证
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"verification_decision={risk['verification_decision']}\n")
            f.write(f"risk_band={risk['risk_band']}\n")
            f.write(f"loop_decision={decision}\n")

    print(f"[touchstone] 风险={risk['risk_band']} 发现={len(findings)} 条")


if __name__ == "__main__":
    main()

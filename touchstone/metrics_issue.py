#!/usr/bin/env python3
# ============================================================================
# touchstone/metrics_issue.py —— 评审健康度看板（常驻、每轮刷新的本仓 Issue）
# ----------------------------------------------------------------------------
# metrics 把每轮健康信号落成事件流；metrics_issue 在其之上做一件 metrics/alert/
# telemetry 都没覆盖的事：在【被评审的仓】开**一个**带 label 的 Issue，每轮把它
# 【重写】成一个"活"的看板（本轮快照 + 滚动趋势），让运维不必翻 artifact/日志就能
# 看评审可信率/静默故障/引擎降级/放行率的演进。
#
# 与现有三块的区别：
#   · metrics.py   —— 事件流落盘；但 touchstone-metrics.json 每 run 全新（workflow
#                     checkout base ref、无 download-at-start），跨 run 看不到趋势。
#   · alert.py     —— 反应式告警，只在阈值被打破时开/评论 touchstone-alert issue。
#   · telemetry.py —— 把数据 POST 到【外部】 collector，不留在本仓。
#   · 本模块       —— 常驻看板：每轮刷、留本仓、看趋势。
#
# 设计（混合模式）：
#   · 一个 issue / 一个仓，label 去重（复用 alert._open_or_update_issue 的 GET-by-label
#     + marker 命中骨架）。
#   · 每轮【重写 issue body】（PATCH）成看板。GitHub 默认不为 issue body 编辑发通知
#     （仅评论/@提及/状态变更通知）→ 静默刷新、不刷屏。
#   · 跨 run 滚动历史存在 body 的 HTML 注释 marker（沿用 <!-- touchstone-loop --> 先例），
#     bounded FIFO；趋势用现有 metrics.summarize(history) 重算，不重复造聚合。
#   · 仅【显著事件】（本轮收敛 / 引擎降级）追加评论（会通知），可配。
#
# 设计约束（与 alert/telemetry 一致）：
#   · 总开关 TOUCHSTONE_METRICS_ISSUE 不为 true → 无操作（默认关 = 零行为变化）。
#   · 数据最小化：看板与 marker 只用现有 metrics 字段，绝不含 diff/代码/凭据
#     （pr/sha 是本仓公开信息，可留）。
#   · 投递失败【绝不冒泡】：可观测性子系统故障只返回状态串、留 stderr，不拖垮评审 job。
#
# 已知限制（设计权衡，非 bug；运维见趋势缺口即此）：
#   · 并发丢更新：run() 对 issue body 做非原子 read-modify-write（GET→解析 marker→追加→PATCH）。
#     同仓多 PR 并发评审（CI 常态）时，两个 run 的读都发生在任一写之前 → 后 PATCH 覆盖前者，
#     偶尔丢一轮滚动历史（趋势精度略降）；但本轮快照始终最新、无崩溃/无损坏。可观测性本就
#     best-effort（失败绝不冒泡、看板近似），不值得上 ETag/If-Match 乐观锁——接受偶发丢一轮。
# ============================================================================

import json
from urllib.parse import quote

from touchstone import metrics as _metrics

ISSUE_LABEL = "touchstone-metrics"
_ISSUE_TITLE = "[touchstone] 评审健康度看板"
_DEFAULT_WINDOW = 50
_DEFAULT_COMMENT_EVENTS = "converged,degraded"

_OPEN = "<!-- touchstone-metrics-issue:"
_CLOSE = " -->"


# ---- GitHub 调用（默认实现走 ghclient 公开 client()；注入式测试用 gh_call 缝替换）---
def _default_gh(method, path, token, data=None):
    # 拷贝 alert._default_gh 的结构（走 ghclient 公开 client()，不伸手进私有 _base_url()），
    # 增 PATCH：看板 body 重写必须 PATCH /repos/.../issues/{n}。其余 method 立即抛
    # ValueError——不静默当 POST，防调用方误传 PUT/DELETE 被吞（同 alert 的显式白名单纪律）。
    from touchstone import ghclient
    c = ghclient.client(token)
    if method == "GET":
        return c.get(path)
    if method == "POST":
        return c.post(path, data or {})
    if method == "PATCH":
        return c.patch(path, data or {})
    raise ValueError(f"_default_gh 仅支持 GET/POST/PATCH，不支持 {method!r}")


# ---- marker 读写（沿用 loop.render_marker / parse_latest_state 的 HTML 注释先例）----
def _stamp_marker(history):
    """把滚动历史（record 列表）序列化进 HTML 注释 marker。"""
    return f"{_OPEN} {json.dumps(history, ensure_ascii=False)} {_CLOSE}"


def _parse_marker(body):
    """从 issue body 提取 marker 内的滚动历史；无 marker / 损坏 → []。

    取【首个】marker（find _OPEN）并用 stdlib JSONDecoder.raw_decode 从 _OPEN 之后起解析 JSON
    数组——它按 JSON 结构停在数组边界（字符串字面量内的 `-->`/`<!--` 不干扰），一步既扫又析，
    与 checklist.parse_latest（#99 canonical fix）同法。

    ⚠ OPEN 须用 find（首个）而非 rfind（末个）：记录字段是 author/record 可控内容，json.dumps
    不转义 `<`/`!`/`-`/`:`，故某字段含字面 _OPEN 串时，rfind(_OPEN) 会落在 JSON 内部那个 _OPEN
    上 → 切片为垃圾 → 解析失败 → 返 []（滚动历史静默丢失，同 #53/#99 marker-corruption 类）。
    真 marker 总在内容之前（每轮整段重写、stamp 在末尾），find 取首个即对。raw_decode 进一步保证：
    即便内容含 `-->`（json.dumps 亦不转义），它停在数组结构边界、不依赖首个 `-->`。"""
    if not body:
        return []
    i = body.find(_OPEN)
    if i == -1:
        return []
    # raw_decode 不跳前导空白、须指向 JSON 结构起点；marker 值恒为数组 → 取首个 '['。
    j = body.find("[", i + len(_OPEN))
    if j == -1:
        return []
    try:
        data, _end = json.JSONDecoder().raw_decode(body, j)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    return list(data) if isinstance(data, list) else []


# ---- 看板渲染（纯函数，无 IO；只用现有 metrics 字段，无 diff/代码/凭据）-------------
def _b(v):
    return "—" if v is None else str(v)


def _pct(v):
    return f"{v:.0%}" if isinstance(v, (int, float)) else "—"


def _table(rows):
    lines = ["| 指标 | 值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def _render_snapshot(record):
    reliable = record.get("review_reliable")
    rel_mark = "✅ 是" if reliable else "⚠️ 否（不可信）"
    return _table([
        ("引擎状态", _b(record.get("engine_status"))),
        ("评审可信", rel_mark),
        ("AI 原始建议", _b(record.get("ai_raw_count"))),
        ("发现(总/规则/AI)",
         f"{_b(record.get('findings_total'))} / {_b(record.get('findings_rule_based'))} / {_b(record.get('findings_ai'))}"),
        ("风险带", _b(record.get("risk_band"))),
        ("回路决策", _b(record.get("loop_decision"))),
        ("总闸", _b(record.get("gate"))),
        ("未核自证", _b(record.get("unverified_claims"))),
        ("变更类/新增行", f"{_b(record.get('change_class'))} / {_b(record.get('added_lines'))}"),
        ("单侧失败/修复解析",
         f"{_b(record.get('partial_tool_failure'))} / {_b(record.get('repaired_parses'))}"),
    ])


def _render_trend(agg):
    return _table([
        ("轮数", _b(agg.get("rounds"))),
        ("评审可信率", _pct(agg.get("review_reliable_rate"))),
        ("收敛率", _pct(agg.get("converged_rate"))),
        ("静默故障轮", _b(agg.get("silent_failure_rounds"))),
        # 单侧失败趋势：improve 连挂而 review 正常时，可信率/引擎分布都看着正常——这行让这类隐身
        # 降级（run-285 那种）在趋势上可见。imp/rev 分别计数（fan-out 下另一侧仍可信）。
        ("单侧失败轮(imp/rev)",
         f"{_b(agg.get('improve_degraded_rounds'))} / {_b(agg.get('review_degraded_rounds'))}"),
        ("被自证闸拦", _b(agg.get("blocked_by_unverified_claims"))),
        ("引擎分布", _b(agg.get("engine_status_dist"))),
    ])


def _render_dashboard(record, history, agg, ctx, label):
    """渲染整段看板 body（含末尾 marker）。纯函数。"""
    run_url = ctx.get("run_url")
    run_line = f"\n\n[查看本轮运行]({run_url})" if run_url else ""
    parts = [
        "# 📊 Touchstone 评审健康度看板",
        "",
        f"> 自动维护 · 每轮**静默刷新**（编辑 body 不发通知）· 仅显著事件追加评论。"
        f" 开关 `TOUCHSTONE_METRICS_ISSUE=true` · 标签 `{label}`。",
        "",
        f"## 本轮 · PR #{_b(record.get('pr'))} · sha {_b(record.get('sha'))} · round {_b(record.get('round'))}",
        "",
        _render_snapshot(record),
        run_line,
        "",
        f"## 滚动趋势 · 近 {_b(agg.get('rounds'))} 轮",
        "",
        _render_trend(agg),
        "",
        "---",
        "",
        "<!-- 历史数据（机读，勿手编） -->",
        _stamp_marker(history),
    ]
    return "\n".join(parts)


# ---- 显著事件（决定是否追加评论=会通知；body 重写本身不通知）------------------------
def _parse_events(raw):
    return {e.strip() for e in (raw or "").split(",") if e.strip()}


def _notable_events(record, events):
    """判定本轮触发的显著事件（与 alert.evaluate 的单轮口径对齐：converged / 引擎降级）。
    纯函数。返回 [事件描述]。alert 仍负责阈值聚合告警；本 sink 的评论只做单轮状态翻转 ping。"""
    out = []
    if "converged" in events and record.get("loop_decision") == "converged":
        out.append("本轮收敛（无可自改发现）")
    if "degraded" in events and record.get("engine_status") in ("no_engine", "provider_failed", "llm_failed"):
        out.append(f"引擎降级（{record.get('engine_status')}）")
    return out


def _maybe_comment(record, ctx, env, gh_call, number):
    notes = _notable_events(record, _parse_events(
        env.get("TOUCHSTONE_METRICS_ISSUE_COMMENT_EVENTS", _DEFAULT_COMMENT_EVENTS)))
    if not notes or not number:
        return False
    body = ("**显著事件**：" + "、".join(notes)
            + f"\n\nPR #{record.get('pr')} · sha {record.get('sha')} · round {record.get('round')}")
    run_url = ctx.get("run_url")
    if run_url:
        body += f"\n\n[查看本轮运行]({run_url})"
    gh_call("POST", f"/repos/{ctx['owner']}/{ctx['repo']}/issues/{number}/comments",
            ctx["token"], {"body": body})
    return True


# ---- issue 去重（按 label 找已开看板；body 含本 sink marker 即命中）------------------
def _find_issue(ctx, label, gh_call):
    """返回 (number, body) 或 (None, "")。按 label 列已开 issue，命中 marker 的那个即看板。"""
    owner, repo, token = ctx["owner"], ctx["repo"], ctx["token"]
    # label 来自 env（可含空格/特殊字符，如自定义 "my metrics"）；须 URL-encode，否则
    # 查询串 malformed → GET 失败被 run() 的 try/except 吞成 "failed:..." → 看板静默失败。
    found = gh_call("GET",
                    f"/repos/{owner}/{repo}/issues?state=open&labels={quote(label, safe='')}",
                    token) or []
    for i in found:
        body = i.get("body") or ""
        if _OPEN in body:
            return i.get("number"), body
    return None, ""


# ---- 编排 ----------------------------------------------------------------------
def run(record, env, ctx, *, gh_call=None):
    """每轮把评审健康度写进本仓的常驻看板 issue。

    env 关 → "disabled"。成功 → "ok"。GitHub 调用异常 → "failed: <Type>: <msg>"
    （绝不冒泡）。record 是 metrics.build(...) 的产出。"""
    if str(env.get("TOUCHSTONE_METRICS_ISSUE", "")).lower() != "true":
        return "disabled"
    gh_call = gh_call or _default_gh
    label = env.get("TOUCHSTONE_METRICS_ISSUE_LABEL", ISSUE_LABEL)
    try:
        window = int(env.get("TOUCHSTONE_METRICS_ISSUE_WINDOW", str(_DEFAULT_WINDOW)))
    except (TypeError, ValueError):
        window = _DEFAULT_WINDOW
    try:
        number, existing_body = _find_issue(ctx, label, gh_call)
        # 跨 run 滚动历史：从存量 marker 取出 → 追加本轮 → bounded FIFO。
        history = _parse_marker(existing_body)
        history.append(record)
        # window<=0 时 [-window:]==[:] 会保留全部（FIFO 失效）；0 是合法 int 不被上面的
        # except 捕获，须显式守：WINDOW=0 的字面义是「不留历史」→ 空。
        history = history[-window:] if window > 0 else []
        agg = _metrics.summarize(history)
        body = _render_dashboard(record, history, agg, ctx, label)
        if number is None:
            created = gh_call("POST", f"/repos/{ctx['owner']}/{ctx['repo']}/issues", ctx["token"],
                              {"title": _ISSUE_TITLE, "labels": [label], "body": body})
            number = (created or {}).get("number")
        else:
            gh_call("PATCH", f"/repos/{ctx['owner']}/{ctx['repo']}/issues/{number}",
                    ctx["token"], {"body": body})
        _maybe_comment(record, ctx, env, gh_call, number)
        return "ok"
    except Exception as e:                       # noqa: BLE001 —— 看板失败绝不拖垮评审
        msg = str(e).strip().replace("\n", " ")[:200]
        return f"failed: {type(e).__name__}: {msg}"

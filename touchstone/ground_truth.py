#!/usr/bin/env python3
# ============================================================================
# touchstone/ground_truth.py —— 真值集采集（人审裁决 → TF-GRPO 学习信号）
# ----------------------------------------------------------------------------
# 从 learning_loop 拆出（模块职责单一化，第三轮工程化加固）。本模块只管学习信号
# 从哪【来】：取最近已关闭 PR，人 resolve 的发现线程 → human_adopted（正例），
# 忽略的 → human_ignored（噪声负例）；PR 级 APPROVED/CHANGES_REQUESTED + 是否合入
# 一并记录。复用 calibrate 的 marker 解析与 GraphQL 线程采纳口径，不另建库。
# ============================================================================

import os
import sys

GT_WINDOW = int((os.environ.get("TOUCHSTONE_GT_WINDOW") or "").strip() or "30")   # 重建真值集回看的最近已关闭 PR 数
GT_DIFF_BUDGET = 8000                                            # 单 PR diff 截断字符预算（喂 TF-GRPO 的上下文）

# --- 盲区2 坏真值检测（B/C/D 信号 → trust_weight；env 默认全关 = 零行为变化）-----------
# 详见 docs/tfgrpo-self-evolution-design.html 盲区2。信号 A（系统性低组奖励）循环依赖 reward、
# 需持久化奖励历史，记为后置先决——本轮只做 B(LGTM-only)/C(低权重 reviewer)/D(极小 diff 却 resolved)。
TRUTH_QUALITY_DEFAULT = False        # 坏真值检测总开关（默认关：不算信号、weight 恒 1.0、不剔除）
TRUTH_PENALTY_DEFAULT = 0.34         # 每命中信号扣的权重（1→0.66、2→0.32）
TRUTH_HARD_DROP_DEFAULT = 3          # 命中信号数≥此 → weight=0 硬剔除（不进 distill/aggregate_ab）
TRUTH_LGTM_BODY_MAX_DEFAULT = 8      # approve-review body ≤此字数视作 shallow（信号 B）
TRUTH_TINY_DIFF_LINES_DEFAULT = 5    # added 行数 <此 且有 resolved 发现 → 信号 D

# GitHub author.association 低权重值（信号 C）：非成员/首次贡献/占位账号的采纳信号降权。
LOW_ASSOCIATIONS = {"NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN"}


def _truth_quality_enabled():
    """坏真值检测总开关（默认关）：TOUCHSTONE_TRUTH_QUALITY 真值时才算 B/C/D 信号、
    设 trust_weight、硬剔除。默认关 = reward 路径 weight 恒 1.0、零行为变化。"""
    val = os.environ.get("TOUCHSTONE_TRUTH_QUALITY")
    if val is None:
        return TRUTH_QUALITY_DEFAULT
    return val.lower() in ("1", "true", "yes", "on")

# --- 真值集采集：从 GitHub 人审裁决重建（喂 TF-GRPO 的学习信号）-----------------
#   「根据每次人工合入的好坏自己学习」的数据入口：取最近已关闭 PR，
#   把【人最终 resolve 了哪些发现线程】→ human_adopted（正例：该类发现值得挑）；
#   人忽略的 → human_ignored（噪声负例）。PR 级 APPROVED/CHANGES_REQUESTED + 是否合入
#   作为好坏信号一并记录。复用 calibrate 的 marker 解析与 GraphQL 线程采纳口径，不另建库。
def _gh_get(path, token, accept="application/vnd.github+json"):
    """GitHub REST GET（经 ghclient 连接池 + 退避）。accept 以 'diff' 结尾返回文本。"""
    from touchstone import ghclient
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return ghclient.request("GET", base + path, token, accept=accept)


def _stack_of(filenames):
    """从改动文件后缀粗判技术栈（仅用于经验按栈归类；不确定 → 空串=通用）。"""
    exts = {os.path.splitext(f)[1].lower() for f in (filenames or []) if f}
    if exts & {".java"}:                       return "java"
    if exts & {".py"}:                         return "python"
    if exts & {".go"}:                         return "go"
    if exts & {".ts", ".tsx", ".js", ".jsx"}:  return "typescript"
    return ""


def make_gt_entry(pr_number, repo, stack, summary, diff, touchstone_findings,
                  resolved_types, human_state, merged, injected_types=None,
                  shadow_types=None, *, trust_weight=1.0, truth_signals=None,
                  resolved_findings=None, human_waived=None):
    """纯函数：单个 PR → TF-GRPO 真值条目。
    human_adopted = 人 resolve 了线程的发现类型（正例：值得挑）；
    human_ignored = touchstone 挑了但人没采纳的（噪声负例）。
    raised_types = 本 PR touchstone 挑过的类型（A/B 分臂的 seen 基数）；
    injected_types = 本 PR 评审时【active 经验】注入了哪些类型（result marker；A/B with/without 依据）；
    shadow_types = 本 PR 评审时【shadow 注入】了哪些 candidate 类型（result marker；破冷启动死锁——
        让 candidate 未达 active 也能采 with 臂样本，graduate 零改动）。两者皆缺 → 该 PR 视作未注入（without 臂）。
    trust_weight = 坏真值检测（盲区2）给本条的 0–1 权重，默认 1.0（TOUCHSTONE_TRUTH_QUALITY 关时不传 →
        隐式 1.0，reward 路径字节级不变）；truth_signals = 命中的 B/C/D 信号（可观测、默认空）。
    resolved_findings（可选，差距1a）：带 file/line 的 resolved 发现列表 → 产 human_adopted_positions
    供 score_review 位置级部分信用。来源：calibrate.thread_findings 现已带 thread 锚定的 file/line
    （parse_review_threads 从 GraphQL reviewThread.path/line 解出），build_ground_truth 据此传 resolved 发现。
    缺失（无位置 / 线程无锚点）即无此字段，向后兼容。与 score_review schema 对齐。
    human_waived（可选，Phase 1）：本 PR【author waived 且人合入】的发现类型——确认噪声标签。
    是 human_ignored 的一个带信心子集（"人明确豁免并合入" vs "模糊忽略"）。来源：build_ground_truth 从
    受信清单 marker 提取 status=='waived' 的项、经 merge 闸采信（waived 是 author 自证，merge = 人背书）。
    Phase 1 仅采集+透传，不改 score_review/distill（默认字节零变化）；缺失即无此字段，向后兼容。
    与 _distill_via_llm 期望的 ground_truth schema 对齐（human_adopted_positions 喂 score_review）。"""
    adopted = sorted({t for t in (resolved_types or []) if t})
    ts_types = {(f.get("rule_id") or f.get("finding_type")) for f in (touchstone_findings or [])}
    ts_types = {t for t in ts_types if t}
    entry = {"pr_id": str(pr_number), "repo": repo or "", "stack": stack or "",
             "summary": summary or "", "diff": diff or "",
             "human_adopted": adopted,
             "human_ignored": sorted(ts_types - set(adopted)),
             "raised_types": sorted(ts_types),
             "injected_types": sorted({t for t in (injected_types or []) if t}),
             "shadow_types": sorted({t for t in (shadow_types or []) if t}),
             "human_state": human_state, "merged": bool(merged),
             "trust_weight": trust_weight, "truth_signals": dict(truth_signals or {})}
    if resolved_findings:                       # 位置信号（差距1a；缺失即无此字段，向后兼容）
        entry["human_adopted_positions"] = [
            {"finding_type": (f.get("rule_id") or f.get("finding_type")),
             "file": f.get("file"), "line": (f.get("line") or f.get("line_start"))}
            for f in resolved_findings
            if (f.get("rule_id") or f.get("finding_type"))]
    # merge 闸在【数据边界】强制（信任根③）：即便外部调用方绕过 _waived_types 直接传 human_waived，
    # 未合入的 PR 也不得带"确认噪声"标签——waived 是 author 自证，须有 merge（人背书）才采信。
    if human_waived and merged:
        entry["human_waived"] = sorted({t for t in human_waived if t})
    return entry


def aggregate_ab(ground_truth):
    """从真值集算 shadow A/B 的每类型采纳率分臂（graduate 用，无需外部 --ab-results 文件）。
    with 臂 = 该类型【被注入过经验（active 或 shadow 任一）】的那批 PR；without 臂 = 都未注入的那批。
    seen = 该类型被 touchstone 挑过的 PR 数；adopted = 其中人 resolve 了该类型的 PR 数。
    返回 graduate 期望的 {finding_type: {with_seen, with_adopted, without_seen, without_adopted}}。
    冷启动破局（step1+step2）：shadow_types 让 candidate 即使未达 active 也能进 with 臂采集——
    step1 加 shadow 注入基础设施（experience_store）、本 step 接通 with 臂判据（active|shadow 并集）；
    注入臂需 marker 记过注入类型（injected_types/shadow_types）才非空。graduate 零改动——仍走原
    ws≥20 且 lift≥0.10 判定，只是 with 臂样本来源拓宽了。"""
    arms = {}
    for pr in ground_truth or []:
        injected_or_shadow = ({t for t in (pr.get("injected_types") or []) if t}
                              | {t for t in (pr.get("shadow_types") or []) if t})
        raised = {t for t in (pr.get("raised_types") or []) if t}
        adopted = {t for t in (pr.get("human_adopted") or []) if t}
        for ftype in raised:
            a = arms.setdefault(ftype, {"with_seen": 0, "with_adopted": 0,
                                        "without_seen": 0, "without_adopted": 0})
            if ftype in injected_or_shadow:
                a["with_seen"] += 1
                if ftype in adopted:
                    a["with_adopted"] += 1
            else:
                a["without_seen"] += 1
                if ftype in adopted:
                    a["without_adopted"] += 1
    return arms


def _diff_added_lines(diff):
    """diff 文本里新增行数（以 '+' 开头、排除 '+++' 文件头）。纯函数，供信号 D。"""
    return sum(1 for ln in (diff or "").splitlines()
               if ln.startswith("+") and not ln.startswith("+++"))


def _truth_signals(reviews, findings_fa, diff, human_state, bot_login, *, diff_truncated=False):
    """盲区2 坏真值检测信号（B/C/D，纯函数）。findings_fa = calibrate.thread_findings 的输出
    （携带 resolver_association + resolved）。返回 {lgtm_only, low_weight_reviewer, tiny_diff_resolved}。
    B 委托 calibrate._lgtm_only（body_max 在此读 env 传入、默认 TRUTH_LGTM_BODY_MAX_DEFAULT——常量不再死）；
    C 看 resolved 发现的 resolver_association 是否低权重；
    D 看 added 行数是否极少却有 resolved 发现。两种 diff 不完整都【不】触发 D（pr-agent review #120 r2/r3）：
      • 空 diff（added=0）=取数失败——真 PR 至少 1 行 added；
      • 截断 diff（diff_truncated=True）=added 计数不可信——原始 >GT_DIFF_BUDGET 显然非 tiny，
        若 added 集中在截断点之后会少算把大 PR 看成 tiny。
    否则不完整 diff 叠 B/C 可能硬剔除有效真值（数据丢失）。
    信号 A 不在此（后置先决，见模块头注释）。"""
    from touchstone import calibrate as C
    resolved_fa = [f for f in (findings_fa or []) if f.get("resolved")]
    low_weight = any((f.get("resolver_association") or "") in LOW_ASSOCIATIONS for f in resolved_fa)
    tiny_lines = int((os.environ.get("TOUCHSTONE_TRUTH_TINY_DIFF_LINES") or "").strip() or str(TRUTH_TINY_DIFF_LINES_DEFAULT))
    added = _diff_added_lines(diff)
    tiny_diff = (not diff_truncated) and 0 < added < tiny_lines and bool(resolved_fa)
    body_max = int((os.environ.get("TOUCHSTONE_TRUTH_LGTM_BODY_MAX") or "").strip() or str(TRUTH_LGTM_BODY_MAX_DEFAULT))
    return {"lgtm_only": C._lgtm_only(reviews, human_state, bot_login, body_max),
            "low_weight_reviewer": low_weight,
            "tiny_diff_resolved": tiny_diff}


def _trust_weight(signals):
    """从命中信号算 trust_weight（0–1，纯函数）。每命中信号扣 penalty；命中数≥hard_drop→0（硬剔除）。
    默认 penalty=0.34 / hard_drop=3：1 信号→0.66、2→0.32、3+→0。"""
    active = sum(1 for v in (signals or {}).values() if v)
    penalty = float((os.environ.get("TOUCHSTONE_TRUTH_PENALTY") or "").strip() or str(TRUTH_PENALTY_DEFAULT))
    hard_drop = int((os.environ.get("TOUCHSTONE_TRUTH_HARD_DROP") or "").strip() or str(TRUTH_HARD_DROP_DEFAULT))
    if active >= hard_drop:
        return 0.0
    return round(max(0.0, 1.0 - penalty * active), 3)


def _waived_types(comments, bot_login, touchstone_findings, *, merged):
    """从（受信）评论里的权威清单 marker 提取【人合入】的 waived 发现类型（确认噪声标签，纯函数）。

    信任根（四锚）：
      ① 只取 bot 评论里的 touchstone-checklist marker——calibrate._trusted_bodies 过滤 + checklist.parse_latest，
         非 bot 发的假清单 marker 一律不认（防 author/攻击者伪造豁免污染 TF-GRPO 负例）；
      ② 用 marker 里 bot 复核后的 status=='waived'（reconcile 已要求带理由），不吃原始 touchstone-ack；
      ③ 仅当 PR 已合入才采信——waived 是 author 自证，merge = 人对豁免的隐式背书
         （autonomy 已保证 waived/split 不自动放行 → merged 即人放行，无自合并口子）；
      ④ 限定本 PR 真挑过的类型（raised_types）——waived 了一个没挑过的类型不产幻影标签。
    未合入 / 无清单 / 非受信 marker / 无 waived 项 → 空集（不产 human_waived 字段，向后兼容）。"""
    if not merged:
        return set()
    from touchstone import checklist as CK
    from touchstone import calibrate as C
    cl = CK.parse_latest(C._trusted_bodies(comments, bot_login))
    raised = {(f.get("rule_id") or f.get("finding_type"))
              for f in (touchstone_findings or [])}
    raised = {t for t in raised if t}
    out = set()
    for it in (cl or {}).get("items", []):
        if it.get("status") != "waived":
            continue
        sig = it.get("sig") or ""
        rtype = sig.split(":", 1)[0] if ":" in sig else sig    # sig = rule_id:file:line → rule_id
        if rtype in raised:
            out.add(rtype)
    return out


def build_ground_truth(owner, repo, token, *, window=GT_WINDOW, bot_login=None,
                       diff_budget=GT_DIFF_BUDGET, since_pr=None):
    """从 GitHub 重建 TF-GRPO 真值集（离线学习的数据入口，需 GITHUB_TOKEN）。
    复用 calibrate：touchstone 发现来自 <!-- touchstone-result: --> marker；
    人采纳来自该发现的评审线程被 resolved（GraphQL isResolved）；
    PR 级好坏来自人审 state(APPROVED/CHANGES_REQUESTED) + 是否合入。
    返回 [make_gt_entry ...]。任一 PR 取数失败仅跳过该 PR，不中断整体。

    since_pr（差距3b 增量水位，opt-in）：设为某 PR 编号时，number <= since_pr 的 PR 在取数前
    跳过（省 per-PR 的 comments/threads/reviews/diff/files 共 ~5 次 API 调用）。返回的条目仍按
    pr_id 降序混杂；调用方据返回条目的 max(pr_id) 推进水位。零信号的新 PR 会被取数但不产条目，
    不推进水位——这类 PR 下轮重取（廉价，仅 list 1 次 API；full-refresh 对账兜底漂移）。"""
    from touchstone import calibrate as C
    bot_login = bot_login or os.environ.get("TOUCHSTONE_BOT_LOGIN", "github-actions[bot]")
    prs = _gh_get(f"/repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc"
                  f"&per_page={window}", token) or []
    out = []
    for pr in prs:
        n = pr.get("number")
        if not n:
            continue
        if since_pr and n <= since_pr:
            continue                  # 增量水位：已处理的 PR 跳过取数（省 ~5 次 per-PR API 调用）
        try:
            comments = _gh_get(f"/repos/{owner}/{repo}/issues/{n}/comments?per_page=100", token) or []
            # 信任根①：result marker 只信受信作者（与 calibrate.py 既有调用点同口径）——此前传全部评论
            # body，非受信作者（author/任意 [bot] 账号）发的假 result marker 会伪造 raised_types/
            # injected_types 核心信号。改经 _trusted_bodies 过滤（_is_trusted_marker_author 已收紧）。
            result = C._parse_result(C._trusted_bodies(comments, bot_login), bot_login)
            if not result:
                continue                          # 未经过 touchstone 评审，无学习信号
            ts_findings = result.get("findings", []) or []
            # author waived + 人合入 → 确认噪声标签（Phase 1：仅采集透传，不改 reward/distill）。
            # 信任根见 _waived_types：只信 bot 清单 marker 的 status=='waived'、且经 merge 闸采信。
            # 未合入短路（PRA-GENERAL:ground_truth.py:230）：merged=False 时不进入 _waived_types 的
            # checklist/_trusted_bodies 解析——守卫前置更省、且不易因内部守卫误删而泄漏；与数据边界
            # merge 闸（make_gt_entry 的 `human_waived and merged`）双保险。
            merged = bool(pr.get("merged_at"))
            human_waived = (_waived_types(comments, bot_login, ts_findings, merged=True)
                            if merged else set())
            try:
                threads = C.parse_review_threads(
                    C.gql(C._GQL_THREADS, {"owner": owner, "repo": repo, "num": n}, token))
                # pr_author=作者 login：作者自 resolve 自己 PR 的发现线程不算人审采纳（否则伪造正例
                # 毒化 TF-GRPO 奖励——契约见 calibrate.thread_findings 的 pr_author 参数 +
                # test_author_self_resolve_not_counted_as_adoption）。build_ground_truth 曾漏传。
                fa = C.thread_findings(threads, bot_login,
                                       pr_author=(pr.get("user") or {}).get("login"))
            except Exception as e:
                print(f"[learning_loop] PR#{n} 评审线程解析失败（按无采纳记录处理）: {e}",
                      file=sys.stderr)
                fa = []
            resolved_types = {f.get("rule_id") for f in fa if f.get("resolved")}
            # 差距1a：带 file/line 的 resolved 发现（#131 review #2：过滤掉无 line 的——位置级奖励需行号；
            #   无 line 的仅进 resolved_types 做类型匹配，不进 positions，免产 line=null 的废位置）
            resolved_findings = [f for f in fa if f.get("resolved")
                                 and (f.get("line") is not None or f.get("line_start") is not None)]
            reviews = _gh_get(f"/repos/{owner}/{repo}/pulls/{n}/reviews?per_page=100", token) or []
            human_state = C._human_verdict(reviews, bot_login)
            diff_truncated = False
            try:
                diff = _gh_get(f"/repos/{owner}/{repo}/pulls/{n}", token,
                               accept="application/vnd.github.v3.diff")
                if len(diff) > diff_budget:
                    diff_truncated = True            # 截断后 added 计数不可信 → _truth_signals 据此不触发 D
                    diff = diff[:diff_budget] + "\n... [diff truncated]"
            except Exception as e:
                print(f"[learning_loop] PR#{n} diff 获取失败（以空 diff 继续）: {e}", file=sys.stderr)
                diff = ""
            files = [f.get("filename") for f in
                     (_gh_get(f"/repos/{owner}/{repo}/pulls/{n}/files?per_page=100", token) or [])]
            # 盲区2 坏真值检测：TOUCHSTONE_TRUTH_QUALITY 开时算 B/C/D 信号 + trust_weight；
            # weight==0（命中≥hard_drop 信号）→ 硬剔除，不 append（distill 与 aggregate_ab 都不再见它）。
            # 关时 signals=None/weight=1.0 → make_gt_entry 默认值，reward 路径字节级不变。
            if _truth_quality_enabled():
                signals = _truth_signals(reviews, fa, diff, human_state, bot_login,
                                         diff_truncated=diff_truncated)
                weight = _trust_weight(signals)
                if weight == 0.0:
                    active = sum(1 for v in signals.values() if v)
                    print(f"[learn] PR#{n} 坏真值硬剔除（命中 {active}/3 信号 → weight=0）：{signals}",
                          file=sys.stderr)
                    continue
            else:
                signals, weight = None, 1.0
            out.append(make_gt_entry(n, repo, _stack_of(files), pr.get("title", ""),
                                     diff, ts_findings, resolved_types, human_state,
                                     merged,
                                     injected_types=result.get("injected_types"),
                                     shadow_types=result.get("shadow_types"),
                                     trust_weight=weight, truth_signals=signals,
                                     resolved_findings=resolved_findings,
                                     human_waived=human_waived))
        except Exception as e:
            print(f"[learn] PR #{n} 取数失败，跳过：{e}", file=sys.stderr)
            continue
    return out


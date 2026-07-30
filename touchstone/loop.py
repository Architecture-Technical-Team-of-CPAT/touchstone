#!/usr/bin/env python3
# ============================================================================
# touchstone/loop.py  ——  反馈循环控制器 loop_step（设计 §4.3）
# ----------------------------------------------------------------------------
# 在 touchstone 侧(中立方,非 author)bound 住「author 改 → 重提 → 再审」的循环。
# 只管【可自改发现】(结构层 = 非正确性类 且 有 suggested_fix)；
#   正确性不在此解决——交 verify/人。
# converged ≠ correct：converged 仅表示无更多可自改发现，正确性由 verify job 正交把关。
# 状态持久化：存在 PR 评论的隐藏 marker 里，轮次由历史 marker 派生。
# 防篡改前提：只解析【机器人自己】发的评论（trusted_bodies 按发帖人过滤）——评论任何人都能发，
# 不过滤则 author 可伪造 marker（如同轮次+空 history）洗掉震荡/无推进闸。
# ============================================================================

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from touchstone import checklist as _checklist   # 单一签名构造源（_sig 委派 sig_of，保持同构）

try:
    MAX_ROUNDS = max(1, int((os.environ.get("TOUCHSTONE_MAX_ROUNDS") or "").strip() or "9"))
except (TypeError, ValueError):
    # 坏值（空串/非数字）→ 默认 9，绝不在 import 时崩整链。对齐 num_retries/max_lines 的 env 兜底
    # 风格——独此常量原本无兜底：env="" 或 "abc" 在导入时 ValueError → touchstone.loop import
    # 失败 → orchestrator 链崩（CI 里 vars 未设常被插成空串）。max(1,…) 顺带挡 0/负数。
    MAX_ROUNDS = 9
_OPEN = "<!-- touchstone-loop:"
_CLOSE = "-->"

# 正确性类 category（含 PR-Agent 源的 "correctness"）：不进自改循环，交 verify/人。
_CORRECTNESS_CATEGORIES = {"correctness", "correctness_suspect", "weak_test"}


@dataclass
class LoopState:
    round: int = 0
    history: list = field(default_factory=list)   # 每轮的可自改发现签名集（list[list[str]]）
    last_verdict: Optional[bool] = None           # 上轮 CI/verify 判定：True 绿 / False 红 / None 未知


def _sig(f):
    # 委派 checklist.sig_of：单一构造源，自动随其归一化（去 file/line 脏空白），保持两处同构。
    return _checklist.sig_of(f)


def remaining_rounds(cur_round, budget_left, max_rounds=MAX_ROUNDS):
    """本 PR 在 escalate 前还能再走几轮——供收敛清单头部「剩余轮次」展示。

    真实剩余 = min(总预算 − 当前轮, 台账继承额度 − 1)：
      - 总预算 − 当前轮：无同源历史时的自然剩余（9 轮制下第 4 轮剩 5）。
      - 台账继承额度 − 1：同源历史吃掉部分预算后的硬上限（loop_step 的
        ``max_rounds = min(MAX_ROUNDS, state.round + budget_left)``，state.round =
        cur_round − 1，故从 cur_round 起还可走 budget_left − 1 轮）。
    取两者 min 并夹 0——修复前 orchestrator 直接传静态 ``ledger_budget − 1``，与当前轮
    无关，致第 1..N 轮恒显同一值（如永远「剩余轮次 8」），误导「还剩很多轮」。
    """
    try:
        cur_round = int(cur_round or 0)
        budget_left = int(budget_left if budget_left is not None else max_rounds)
    except (TypeError, ValueError):
        return max(0, max_rounds - 1)
    return max(0, min(max_rounds - cur_round, budget_left - 1))


def author_actionable(findings, rule_index):
    """可由 author 自改的发现：非正确性类 且 有 fix_direction（改进方向）。
    正确性判定双路：① 在册规则的 class==correctness；② 发现自带 category 落在正确性集合
    （覆盖 PR-Agent 源的 PRA-* 发现——其 rule_id 不在 standards rule_index，单靠 ① 会漏网）。
    修订设计 §5（评审意见 2）：门槛从「有 suggested_fix（补丁/精确指令）」改为「有 fix_direction
    （方向）」——author 按方向结合自身上下文自行决定改法；suggested_fix 作过渡别名仍受理。"""
    out = []
    for f in findings:
        if rule_index.get(f.get("rule_id"), {}).get("class") == "correctness":
            continue                                   # 在册正确性规则
        if f.get("category") in _CORRECTNESS_CATEGORIES:
            continue                                   # PR-Agent 源 correctness（PRA-* 等）
        if not (f.get("fix_direction") or f.get("suggested_fix") or "").strip():
            continue
        out.append(f)
    return out


def loop_step(findings, rule_index, state, max_rounds=MAX_ROUNDS, ci_passed=None,
              checklist_pair=None, ledger=None, review_reliable=True):
    """返回 (decision, reason, new_state)。decision ∈ converged|continue|escalate。
    ci_passed：当前轮 CI/verify 判定（True 绿 / False 红 / None 未知）。
    发现清零但 CI 红时不收敛——发 continue 让 author 接着修构建/测试（仍受轮次上限约束）。
    安全性不依赖此项：converged 与否都不放行，自动合并另有独立的质量门禁（总闸）闸。

    修订设计 §5（评审意见 1、3、10）的可选增强（不传则行为与原版完全一致）：
      checklist_pair=(prev, cur)：收敛清单前后两轮。收敛定义升级为「清单全部销项（done|waived|split）
        且无可自改发现」；无推进判定升级为「清单销项无推进」（覆盖只发布评论不实际修改的假修）。
      ledger：轮次台账（RoundLedger）。轮次预算按 ledger['rounds_left'] 计——同内容重提
        继承历史消耗，刷不出新额度；余额 ≤0 直接升级人工。
      review_reliable：本轮 LLM 评审是否可信（见 review_provider.review_reliable）。False 时
        即便清单全销项/无可自改发现也不收敛--"0 发现"在 diff 被裁空/LLM 随机性下不可靠，
        回落 continue 待可靠轮复核。防假收敛放行未评审代码（PR #44 round-1 真根因兜底）。"""
    # 台账预算（评审意见 10）：同源重提继承历史轮次，本函数只看剩余额度。
    if ledger is not None:
        budget_left = int(ledger.get("rounds_left", max_rounds))
        if budget_left <= 0:
            nr0 = state.round + 1
            return ("escalate",
                    f"轮次台账余额为零（同源历史已耗 {ledger.get('rounds_spent', 0)} 轮）→ 交人；"
                    "如属正当重提需重置额度，请打 rounds-reset label",
                    LoopState(nr0, state.history, ci_passed))
        max_rounds = min(max_rounds, state.round + budget_left)

    acts = author_actionable(findings, rule_index)
    cur = sorted({_sig(f) for f in acts})
    prev_sets = [set(h) for h in state.history]
    nr = state.round + 1
    hist = (state.history + [cur])[-(max_rounds + 1):]   # 限长,避免 marker 膨胀

    # 清单语义（评审意见 1、3）：收敛与推进以清单销项为准。
    if checklist_pair is not None:
        from touchstone import checklist as _cl
        prev_cl, cur_cl = checklist_pair
        # 加固：收敛只认机器可验证销项（done）。存在 author 自证的 waived/split 时不收敛，
        # 回落 continue 并点名待人核准项——防 author 用 "waived: 随便写" 单方闭环触发自动放行。
        if _cl.all_resolved(cur_cl) and _cl.has_unverified_claims(cur_cl) and not cur:
            claims = _cl.unverified_claims(cur_cl)
            if nr >= max_rounds:
                return ("escalate",
                        f"清单表面全销项，但有 {len(claims)} 条 author 自证（waived/split）未经人核准，"
                        f"轮次耗尽 → 交人裁决", LoopState(nr, hist, ci_passed))
            return ("continue",
                    f"清单表面全销项，但有 {len(claims)} 条 waived/split 系 author 自证、机器未验证："
                    f"需人核准这些豁免/拆分后方可收敛（advisory 下人可径直合入）",
                    LoopState(nr, hist, ci_passed))
        if _cl.all_verified(cur_cl) and not cur:
            if ci_passed is False:
                if nr >= max_rounds:
                    return ("escalate", f"清单已销项但 CI/verify 持续为红，轮次耗尽（≥ {max_rounds}）→ 交人",
                            LoopState(nr, hist, ci_passed))
                return ("continue", "收敛清单已全部销项，但 CI/verify 为红：请修复构建/测试失败后再 push",
                        LoopState(nr, hist, ci_passed))
            if not review_reliable:
                # 本轮 LLM 评审不可信（引擎降级/可疑空收敛）-> "无可自改发现"不可靠（可能 diff
                # 被裁空/LLM 随机性未报），不予收敛。抓 PR #44 round-1 那类假收敛（首轮 diff 被
                # 裁空 -> 0 发现 -> 无清单项可 withhold -> 此处兜底）。回落 continue，人仍可合入。
                if nr >= max_rounds:
                    return ("escalate", "本轮 LLM 评审不可信且轮次耗尽 -> 交人",
                            LoopState(nr, hist, ci_passed))
                return ("continue", "本轮 LLM 评审不可信（引擎降级/可疑空收敛），不予收敛，待可靠轮复核",
                        LoopState(nr, hist, ci_passed))
            return ("converged", "收敛清单全部销项且无新增可自改发现（正确性另由 verify 把关）",
                    LoopState(nr, hist, ci_passed))
        if review_reliable and cur and _cl.no_progress(prev_cl, cur_cl):
            # 无推进闸仅在评审可信时触发：不可信轮 checklist.reconcile 会 withhold 依赖复检的销项
            # （done/自动销项），销项率自然不升--这是评审故障所致，非 author 未改。此时判"无推进"
            # 会把"已改但评审不可信无法验证"误判为假修升级。不可信轮回落 continue 等可靠轮再判。
            #
            # 且仅当本轮存在 author 可自改发现（cur 非空）时才触发：无推进的本意（见
            # checklist.no_progress 文档）是抓「author 只发评论不实际修改」的假修——只对 author
            # 可自改的发现成立。若本轮无可自改发现（cur 为空，典型=仅剩 correctness 这类不由
            # author ack 销项、归 verify/评审管的发现），「无推进（含假修）」是把 author 根本无法
            # 着手的事归咎于 author 的误判：回落 continue（轮次耗尽时走下方的「轮次耗尽」诚实
            # 升级），不甩假修锅。cur 已是 author_actionable 过滤后的本轮可自改签名集。
            return ("escalate", "无推进：清单销项率连续未提升且无 waived/split 申报（含假修）",
                    LoopState(nr, hist, ci_passed))
        if nr >= max_rounds:
            open_n = sum(1 for i in cur_cl.get("items", []) if i["status"] == "open")
            return ("escalate", f"轮次耗尽（≥ {max_rounds}）仍有 {open_n} 条未销项 → 交人",
                    LoopState(nr, hist, ci_passed))
        return ("continue",
                f"第 {nr} 轮，清单销项率 {int(cur_cl.get('resolved_rate', 0) * 100)}%，待 author 逐项申报",
                LoopState(nr, hist, ci_passed))

    # 收敛：无可自改发现。但若【已知 CI/verify 为红】则不算改完——提示 author 接着修。
    if not cur:
        if ci_passed is False:
            if nr >= max_rounds:
                return ("escalate",
                        f"发现已清但 CI/verify 持续为红，轮次耗尽（≥ {max_rounds}）→ 交人",
                        LoopState(nr, hist, ci_passed))
            return ("continue",
                    "委员会发现已清，但 CI/verify 为红：请修复构建/测试失败后再 push",
                    LoopState(nr, hist, ci_passed))
        if not review_reliable:
            # 本轮 LLM 评审不可信 -> "无可自改发现"不可靠，不予收敛（同 checklist 路径兜底）。
            if nr >= max_rounds:
                return "escalate", "本轮 LLM 评审不可信且轮次耗尽 -> 交人", LoopState(nr, hist, ci_passed)
            return "continue", "本轮 LLM 评审不可信（引擎降级/可疑空收敛），不予收敛，待可靠轮复核", LoopState(nr, hist, ci_passed)
        return "converged", "无可自改发现（正确性另由 verify 把关）", LoopState(nr, hist, ci_passed)

    # 震荡：发现集与历史某轮完全重复（改了又冒出同一组）
    if set(cur) in prev_sets:
        return "escalate", "震荡：发现集重复出现，自改未推进", LoopState(nr, hist, ci_passed)

    # 无推进：相比上一轮既没减少、也没解决任何既有发现（含假修 / 协商降级）
    if prev_sets:
        last = prev_sets[-1]
        if len(cur) >= len(last) and not (last - set(cur)):
            return "escalate", "无推进：既未减少也未解决既有发现（含假修/协商降级）", LoopState(nr, hist, ci_passed)

    # 轮次耗尽
    if nr >= max_rounds:
        return "escalate", f"轮次耗尽（≥ {max_rounds}）仍有可自改发现", LoopState(nr, hist, ci_passed)

    return "continue", f"第 {nr} 轮，待 author 自改 {len(cur)} 项", LoopState(nr, hist, ci_passed)


# --- 状态持久化（PR 评论隐藏 marker）----------------------------------------
def render_marker(state):
    payload = json.dumps({"round": state.round, "history": state.history,
                          "last_verdict": state.last_verdict}, ensure_ascii=False)
    return f"{_OPEN} {payload} {_CLOSE}"


def _is_bot_login(login):
    """是否是 bot 账号。GitHub 保留 `[bot]` 后缀给 bot（如 github-actions[bot]），
    人不能注册——故 login 以 [bot] 结尾即可靠判为 bot。"""
    return bool(login) and (login.endswith("[bot]") or login == "github-actions")


def trusted_bodies(comments, bot_login):
    """只保留【机器人自己】发的评论正文，供 parse_latest_state 使用。
    评论任何人都能发；不按发帖人过滤，author 就能伪造 loop marker（例如同轮次 + 空 history）
    洗掉震荡/无推进等抗博弈闸。

    bot_login 已知（GET /user 成功，如 PAT 部署）→ 精确按该 login 过滤。
    bot_login 未知（GET /user 失败，如默认 GITHUB_TOKEN）→ **不退回全量**，改按
    `[bot]` 后缀过滤：默认 GITHUB_TOKEN 发评论即 github-actions[bot]，仍能可靠区分
    bot 与人（人无法注册 [bot] 后缀），防伪造不降级。"""
    if bot_login:
        return [c.get("body", "") for c in (comments or [])
                if ((c.get("user") or {}).get("login")) == bot_login]
    return [c.get("body", "") for c in (comments or [])
            if _is_bot_login((c.get("user") or {}).get("login"))]


def parse_latest_state(comment_bodies):
    """从历史评论里取轮次最大的 loop marker；无则返回初始状态。"""
    latest = LoopState()
    for body in comment_bodies or []:
        i = body.rfind(_OPEN)
        if i == -1:
            continue
        j = body.find(_CLOSE, i)
        if j == -1:
            continue
        try:
            d = json.loads(body[i + len(_OPEN):j].strip())
            st = LoopState(int(d.get("round", 0)), list(d.get("history", [])),
                           d.get("last_verdict"))
            if st.round >= latest.round:
                latest = st
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return latest

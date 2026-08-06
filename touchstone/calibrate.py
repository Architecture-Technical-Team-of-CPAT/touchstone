#!/usr/bin/env python3
# ============================================================================
# touchstone/calibrate.py  ——  校准（设计 §4.3 record_calibration）
# ----------------------------------------------------------------------------
# 用人审真实裁决当免费 标准答案，衡量 touchstone 准不准。
# 不另建数据库——直接从 GitHub API 重建：
#   • touchstone 发现/风险：从其评论里的 <!-- touchstone-result: ... --> marker 解析
#   • 人审裁决：从 PR 的 review 状态(APPROVED/CHANGES_REQUESTED)与是否合入
# 最小可算的是【PR 级代理】吻合度（finding 级"人是否采纳某条"需线程解决状态=GraphQL，留作细化）：
#   • 风险等级 vs 人审决定（high 档应更多 CHANGES_REQUESTED = 校准良好）
#   • 某 agent/rule 命中的 PR 中，人最终要求改动的比例（命中多但该比例低 = 噪声专才）
# 北极星：touchstone 标了问题、人也确实想改 的吻合比例。
# ============================================================================

import json
import os
import re
import sys

from touchstone import ghclient            # GitHub HTTP 客户端(requests + 退避)
from touchstone.atomicio import atomic_write_json, atomic_write_text   # 状态文件原子写
from touchstone.artifacts import artifact_path
import requests

WINDOW = int((os.environ.get("CALIBRATE_WINDOW") or "").strip() or "50")   # 取最近 N 个已关闭 PR
NOISY_MIN_FIRES = 5          # agent/rule 命中达到此数才判定噪声
NOISY_CR_RATE = 0.2          # 命中 PR 的"人要求改动"比例低于此 → 噪声
NOISY_ADOPT_RATE = 0.2       # finding 级：命中条数多但被采纳(线程 resolved)比例低于此 → 噪声
_RESULT = re.compile(r"<!--\s*touchstone-result:\s*(\{.*?\})\s*-->", re.DOTALL)
_FINDING = re.compile(r"<!--\s*touchstone-finding:\s*(\{.*?\})\s*-->", re.DOTALL)


# --- GitHub REST（requests，见 ghclient；保持串行：二级限流惩罚并发）------------
def gh(path, token):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return ghclient.request("GET", base + path, token)


def gh_paginate(path, token):
    """翻页版 gh：自动跟 ?page=N&per_page=100 直到无更多（防 >100 条截断）。"""
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return ghclient.paginate(base + path, token)


# --- GitHub GraphQL：取 PR 评论线程的 isResolved（REST 不暴露线程解决状态）------
_GQL_THREADS = """
query($owner:String!,$repo:String!,$num:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$num){
      reviewThreads(first:100){
        nodes{ isResolved resolvedBy{login} path line comments(first:20){ nodes{ author{login} authorAssociation body } } }
      }
    }
  }
}"""


def gql(query, variables, token):
    base = os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
    return ghclient.request("POST", base, token,
                            data={"query": query, "variables": variables})


def parse_review_threads(data):
    """GraphQL 响应 → [{isResolved, comments:[{author, association, body}]}]。纯函数。
    association 取自评论节点的 authorAssociation（comment 顶层字段，非 author 子字段——
    GitHub GraphQL 的 Actor 类型无 association，authorAssociation 才合法）。"""
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    nodes = ((pr.get("reviewThreads") or {}).get("nodes")) or []
    out = []
    for t in nodes:
        comments = [{"author": ((c.get("author") or {}).get("login") or ""),
                     "association": c.get("authorAssociation") or "",
                     "body": c.get("body") or ""}
                    for c in (((t.get("comments") or {}).get("nodes")) or [])]
        out.append({"isResolved": bool(t.get("isResolved")),
                    "resolved_by": ((t.get("resolvedBy") or {}).get("login") or ""),
                    "path": t.get("path") or "",       # 线程锚定的文件（内联评审评论固有，差距1a 位置信号）
                    "line": t.get("line"),              # 线程锚定的行（outdated 线程可能为 null）
                    "comments": comments})
    return out


_DISMISS = re.compile(
    r"wont[\s-]*fix|won't[\s-]*fix|not\s+a\s+bug|by\s+design|false\s+positive|"
    r"not\s+applicable|out\s+of\s+scope|误报|无需修改|不必修改|不采纳|驳回|不在范围|不修",
    re.IGNORECASE)


# 一键过（LGTM-only）的口头禅：approve-review 的 body 仅含此类短语/emoji → shallow（盲区2 信号 B）。
_APPROVE_SHALLOW = re.compile(
    r"^(lgtm|lg2m|looks good(?: to me)?|approved|ship\s*it|sgtm|sounds good|"
    r"\+1|👍|好|没问题|可以|通过|赞同|同意)$",
    re.IGNORECASE)


def _thread_dismissed(comments):
    """线程里是否出现 wontfix/驳回 信号（粗启发式，宁可漏判也不把真采纳误判为驳回）。
    用于把"resolved 但实为 wontfix"从采纳里剔除——修正 N4a：isResolved 含 wontfix 解决。"""
    return any(_DISMISS.search(c.get("body") or "") for c in comments)


def thread_findings(threads, bot_login=None, pr_author=None):
    """把每条评论线程对回某条 touchstone 发现：线程内带 touchstone-finding 标记的评论
    → {rule_id, agent, resolved, dismissed}。
    resolved = 线程 isResolved 且未被 wontfix/驳回（N4a：resolved 含 wontfix 解决，
    那种不算采纳——否则会把人明确驳回的当正例，污染校准与 TF-GRPO 奖励）。"""
    out = []
    for t in threads:
        resolved = bool(t.get("isResolved"))
        if resolved and pr_author and t.get("resolved_by") == pr_author:
            resolved = False           # 作者自 resolve → 不作为采纳信号
        comments = t.get("comments") or []
        # resolver_association（盲区2 信号 C）：取线程末条【人类】评论的 association 当解决者身份——
        # resolved 线程的末条人类评论通常是解决者所留。排除 bot 尾评：bot（如 github-actions[bot]）
        # 的 association 常为 NONE（属 LOW_ASSOCIATIONS），若取它当 resolver 会误触发低权重信号。
        human_comments = [c for c in comments
                          if _is_human_reviewer(c.get("author") or "", bot_login)]
        resolver_comment = human_comments[-1] if human_comments else None
        resolver_assoc = (resolver_comment.get("association") or "") if resolver_comment else ""
        for c in comments:
            if not _is_trusted_marker_author(c.get("author") or "", bot_login):
                continue            # 信任根：只认 touchstone 自己发的 finding marker（防伪造）
            m = _FINDING.search(c.get("body") or "")
            if not m:
                continue
            try:
                meta = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            dismissed = _thread_dismissed(comments)
            out.append({"rule_id": meta.get("rule_id"), "agent": meta.get("agent"),
                        "resolved": resolved and not dismissed,
                        "dismissed": dismissed,
                        "resolver_association": resolver_assoc,
                        "file": t.get("path") or "",    # 差距1a：线程位置 → finding 位置（喂 score_review 位置级）
                        "line": t.get("line")})
            break                      # 一个线程只对一条发现
    return out


def fetch_review_threads(owner, repo, number, token):
    data = gql(_GQL_THREADS, {"owner": owner, "repo": repo, "num": number}, token)
    return parse_review_threads(data)


def _parse_result(comment_bodies, bot_login):
    """取最近一条 touchstone-result marker（touchstone 每轮都会贴）。"""
    for body in reversed(comment_bodies):
        m = _RESULT.search(body or "")
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _is_human_reviewer(login, bot_login):
    """是否非 bot 的人类评审者（排除 bot_login 身份与 [bot] 后缀，空 login 视作非人类）。
    _lgtm_only 用此过滤 approve-review（_human_verdict 保留原内联过滤、行为字节级不变）。"""
    return bool(login) and login != bot_login and not login.endswith("[bot]")


def _human_verdict(reviews, bot_login):
    """人审最终裁决：取最后一条非 bot 的决定性 review 状态。"""
    state = None
    for rv in reviews:
        login = (rv.get("user") or {}).get("login", "")
        if login == bot_login or login.endswith("[bot]"):
            continue
        s = rv.get("state")
        if s in ("APPROVED", "CHANGES_REQUESTED"):
            state = s
    return state


def _lgtm_only(reviews, human_state, bot_login, body_max):
    """一键过（LGTM-only，盲区2 信号 B）：PR 最终 APPROVED，但所有非 bot 的 approve-review
    都 shallow——body 空 / 极短(≤body_max 字) / 仅 LGTM 类口头禅。
    这种"采纳"信号弱（rubber-stamp），供坏真值检测降权。
    非 APPROVED（CHANGES_REQUESTED 或无裁决）不算一键过；无任何非 bot approve 时保守不命中。
    body_max 由调用方传入（ground_truth._truth_signals 读 env TOUCHSTONE_TRUTH_LGTM_BODY_MAX、
    默认 TRUTH_LGTM_BODY_MAX_DEFAULT）——本函数纯、无 env 耦合（pr-agent review #120 r2：
    原硬编码 "8" 读 env 致 TRUTH_LGTM_BODY_MAX_DEFAULT 成死代码、改常量则 _lgtm_only 静默漂移）。"""
    if human_state != "APPROVED":
        return False
    approves = [rv for rv in (reviews or [])
                if rv.get("state") == "APPROVED"
                and _is_human_reviewer((rv.get("user") or {}).get("login", ""), bot_login)]
    if not approves:
        return False

    def _shallow(rv):
        b = (rv.get("body") or "").strip()
        return not b or len(b) <= body_max or bool(_APPROVE_SHALLOW.match(b))

    return all(_shallow(rv) for rv in approves)


def _is_trusted_marker_author(login, bot_login):
    """marker 信任根：只认 touchstone 自己发的评论里的 marker。
    防 PR author/任意评论者发假 <!-- touchstone-result/finding/auto_handled --> marker 伪造
    校准/学习/熔断数据——这是整个自学习闭环的信任根。

    bot_login 已知（GET /user 成功，如 PAT 部署）→ 精确按该 login 过滤（与 loop.trusted_bodies
    同口径）。此前【即便 bot_login 已知】也接受任意 [bot] 后缀账号（dependabot[bot]、renovate[bot]…）
    的 marker——系统级口子（result/finding marker 决定 adopted/raised_types 核心信号），本 PR 收紧。
    bot_login 未知（GET /user 失败，如默认 GITHUB_TOKEN）→ 退回 [bot] 后缀兜底（PR #27：
    GitHub 保留 [bot] 给 bot 账号、人无法注册，仍防人伪造；与 loop.trusted_bodies 降级路径逐字一致）。"""
    if not login:
        return False
    if bot_login:
        return login == bot_login
    return login.endswith("[bot]") or login == "github-actions"


def _trusted_bodies(comments, bot_login):
    """只取 trusted 作者的评论 body（供 _parse_result / auto_handled 等 marker 解析）。"""
    return [c.get("body", "") for c in comments
            if _is_trusted_marker_author((c.get("user") or {}).get("login", ""), bot_login)]


# --- 纯聚合（可测）-----------------------------------------------------------
def _norm_record(r):
    """归一化两种 CalibrationRecord 形状：main() 经 record_calibration 构造的（touchstone_band/
    touchstone_findings/human_verdict）与历史 inline 形状（risk_band/findings/human_state）。"""
    return {
        "pr": r.get("pr"),
        "risk_band": r.get("risk_band", r.get("touchstone_band")),
        "findings": r.get("findings", r.get("touchstone_findings", [])),
        "human_state": r.get("human_state", r.get("human_verdict")),
        "finding_adoption": r.get("finding_adoption", []),
        "merged": r.get("merged"),
        "merge_commit_sha": r.get("merge_commit_sha"),
        "auto_handled": r.get("auto_handled"),
    }


def record_calibration(pr, touchstone_output, human_verdict):
    """§4.3（薄封装）：把【单个 PR】的 touchstone 输出与人审裁决组装成一条 CalibrationRecord
    （成员见设计 §3.5：touchstone_findings / touchstone_band / human_verdict / human_flagged / agreement）。
    批量校准不另建库，而是从 GitHub API 重建多条 record 后交 aggregate()（见 main()）。"""
    if isinstance(touchstone_output, dict):
        findings = touchstone_output.get("findings", []) or []
        band = (touchstone_output.get("risk") or {}).get("risk_band")
    else:
        findings, band = (touchstone_output or []), None
    if isinstance(human_verdict, dict):
        hv = human_verdict.get("state") or human_verdict.get("verdict")
        flagged = human_verdict.get("flagged", []) or []
    else:
        hv, flagged = human_verdict, []
    touchstone_flagged = bool(findings) or band in ("mid", "high")
    human_changes = str(hv).upper() in ("CHANGES_REQUESTED", "CHANGES")
    return {"pr": pr, "touchstone_findings": findings, "touchstone_band": band,
            "human_verdict": hv, "human_flagged": flagged,
            "agreement": touchstone_flagged == human_changes}


def aggregate(records):
    """records: [{risk_band, findings:[{rule_id,agent}], human_state, merged}]（经 _norm_record
    也接受 record_calibration 的 touchstone_* / human_verdict 形状）。"""
    records = [_norm_record(r) for r in records]
    def cr(rs):                       # 人"要求改动"比例（CHANGES_REQUESTED）
        n = [r for r in rs if r.get("human_state")]
        return (sum(r["human_state"] == "CHANGES_REQUESTED" for r in n) / len(n)) if n else None

    out = {"total": len(records), "by_risk": {}, "by_agent": {}, "by_rule": {}, "noisy": []}
    with_find = [r for r in records if r.get("findings")]
    out["prs_with_findings"] = len(with_find)
    out["overall_changes_requested_rate"] = cr(records)
    # 风险等级校准
    for band in ("high", "mid", "low"):
        rs = [r for r in records if r.get("risk_band") == band]
        out["by_risk"][band] = {"count": len(rs), "changes_requested_rate": cr(rs)}
    # 按 agent / rule：命中计数 + 命中 PR 的人改动比例
    def by_key(keyfn):
        acc = {}
        for r in records:
            seen = set()
            for f in r.get("findings", []):
                k = keyfn(f)
                if k and k not in seen:
                    seen.add(k)
                    acc.setdefault(k, []).append(r)
        return {k: {"fires": len(rs), "changes_requested_rate": cr(rs)} for k, rs in acc.items()}

    out["by_agent"] = by_key(lambda f: f.get("agent"))
    out["by_rule"] = by_key(lambda f: f.get("rule_id"))
    # 噪声判定：命中多但人改动比例低
    for kind, d in (("agent", out["by_agent"]), ("rule", out["by_rule"])):
        for k, v in d.items():
            rate = v["changes_requested_rate"]
            if v["fires"] >= NOISY_MIN_FIRES and rate is not None and rate < NOISY_CR_RATE:
                out["noisy"].append({"kind": kind, "key": k, "fires": v["fires"],
                                     "changes_requested_rate": round(rate, 2)})
    # finding 级采纳率（GraphQL 线程 isResolved）——比 PR 级更细，直接供固化/噪声判定使用
    def fa_by(keyfn):
        acc = {}
        for r in records:
            for fa in r.get("finding_adoption", []):
                k = keyfn(fa)
                if not k:
                    continue
                a = acc.setdefault(k, {"seen": 0, "adopted": 0})
                a["seen"] += 1
                a["adopted"] += 1 if fa.get("resolved") else 0
        return acc

    for kind, d, acc in (("agent", out["by_agent"], fa_by(lambda f: f.get("agent"))),
                         ("rule", out["by_rule"], fa_by(lambda f: f.get("rule_id")))):
        for k, a in acc.items():
            slot = d.setdefault(k, {"fires": 0, "changes_requested_rate": None})
            slot["findings_seen"] = a["seen"]
            slot["adopted"] = a["adopted"]
            slot["adoption_rate"] = round(a["adopted"] / a["seen"], 2) if a["seen"] else None
            if a["seen"] >= NOISY_MIN_FIRES and slot["adoption_rate"] is not None \
                    and slot["adoption_rate"] < NOISY_ADOPT_RATE:
                out["noisy"].append({"kind": kind, "key": k, "level": "finding",
                                     "findings_seen": a["seen"], "adoption_rate": slot["adoption_rate"]})
    return out


def render_report(agg):
    L = [f"# 校准报告（最近 {agg['total']} 个已关闭 PR）", ""]
    cr = agg["overall_changes_requested_rate"]
    L.append(f"含发现的 PR：{agg['prs_with_findings']}/{agg['total']}　"
             f"整体人要求改动比例：{cr if cr is None else round(cr,2)}")
    L.append("\n## 风险等级校准（high 应明显高于 low）")
    for b in ("high", "mid", "low"):
        v = agg["by_risk"][b]
        L.append(f"- {b}: n={v['count']} 人改动比例={v['changes_requested_rate']}")
    L.append("\n## 按 agent（命中数 · 人改动比例 · finding 级采纳率）")
    for k, v in sorted(agg["by_agent"].items(), key=lambda x: -x[1]["fires"]):
        L.append(f"- {k}: fires={v['fires']} cr={v['changes_requested_rate']} "
                 f"adopt={v.get('adoption_rate')}({v.get('adopted','-')}/{v.get('findings_seen','-')})")
    L.append("\n## 按 rule（命中数 · 人改动比例 · finding 级采纳率）")
    for k, v in sorted(agg["by_rule"].items(), key=lambda x: -x[1]["fires"]):
        L.append(f"- {k}: fires={v['fires']} cr={v['changes_requested_rate']} "
                 f"adopt={v.get('adoption_rate')}({v.get('adopted','-')}/{v.get('findings_seen','-')})")
    if agg["noisy"]:
        L.append("\n## ⚠ 疑似噪声（命中多但很少被采纳 → 考虑收紧/退役）")
        for n in agg["noisy"]:
            if n.get("level") == "finding":
                L.append(f"- [{n['kind']}·finding] {n['key']}: seen={n['findings_seen']} "
                         f"adopt={n['adoption_rate']}")
            else:
                L.append(f"- [{n['kind']}] {n['key']}: fires={n['fires']} cr={n['changes_requested_rate']}")
    else:
        L.append("\n（未发现达阈值的噪声 agent/rule）")
    return "\n".join(L)


def main():
    token = os.environ["GITHUB_TOKEN"]
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    bot = os.environ.get("TOUCHSTONE_BOT_LOGIN", "github-actions[bot]")
    prs = gh(f"/repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc"
             f"&per_page={WINDOW}", token)
    records = []
    for pr in prs:
        n = pr["number"]
        comments = gh_paginate(f"/repos/{owner}/{repo}/issues/{n}/comments", token)
        result = _parse_result(_trusted_bodies(comments, bot), bot)
        if not result:
            continue                      # 该 PR 没经过 touchstone，跳过
        # 真实自动放行标记（autonomy.execute_auto_merge 发布的隐藏 marker）；只信 bot 发的（防伪造）
        auto_handled = any("touchstone:auto_handled" in b for b in _trusted_bodies(comments, bot))
        reviews = gh_paginate(f"/repos/{owner}/{repo}/pulls/{n}/reviews", token)
        try:
            # pr_author=作者 login：作者自 resolve 自己 PR 的发现线程不算采纳（与 build_ground_truth
            # 同一契约），否则 finding_adoption 被污染、adoption_rate 虚高致 graduate/retire 误判。
            fa = thread_findings(fetch_review_threads(owner, repo, n, token), bot,
                                 pr_author=(pr.get("user") or {}).get("login"))
        except (requests.exceptions.RequestException, KeyError, ValueError) as e:
            print(f"[warn] PR #{n} 线程采纳取用失败: {e}", file=sys.stderr)
            fa = []
        # 经 record_calibration 构造（设计 §3.5 的 CalibrationRecord 形状），再追加重建期才有的字段。
        # aggregate 经 _norm_record 同时消费此形状与历史 inline 形状。
        hv = _human_verdict(reviews, bot)
        rec = record_calibration(n, {"findings": result.get("findings", []),
                                     "risk": {"risk_band": result.get("risk_band")}}, hv)
        rec.update({"finding_adoption": fa, "merged": bool(pr.get("merged_at")),
                    "merge_commit_sha": pr.get("merge_commit_sha"), "auto_handled": auto_handled})
        records.append(rec)
    agg = aggregate(records)
    report = render_report(agg)
    print(report)
    # atomic_write_text：自建 OUTPUT_DIR 父目录（设隔离目录时不 FileNotFoundError）+ 原子落盘
    atomic_write_text(artifact_path("calibration-report.md"), report)
    # 原子：calibration.json 喂 autonomy graduate 与 govern 固化判据，半文件会污染毕业类
    # override_env="CALIBRATION_JSON" 与读方（govern.py + autonomy.py）对齐：设了 CALIBRATION_JSON
    # 时读写都走它，不致写 OUTPUT_DIR/calibration.json 而读方读 CALIBRATION_JSON（#90 round-1 finding calibrate.py:328）
    atomic_write_json(artifact_path("calibration.json", override_env="CALIBRATION_JSON"),
                      {"aggregate": agg, "records": records})


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# ============================================================================
# touchstone/autonomy.py  ——  自动放行达标路径（设计 §4.7）
# ----------------------------------------------------------------------------
# 可选路径（默认关）：开启后才替人点合并。第一原则：授权自动放行的【不是委员会的
# 意见，而是汇总状态检查（质量门禁）的通过】。多闸串联、默认全关、经验门控、熔断保障。
#   decide_auto_merge：质量门禁绿 + 委员会无拦截 + 反馈循环收敛 + 未熔断 + 类已达标 + 总开关
#   经验层：change_class 签名 + build_experience + graduate_classes（与 §4.4 固化同构）
#   安全自举：影子(shadow) → 毕业(graduate) → 实放(live)
#   execute_auto_merge：调 merge API 并打 auto_handled marker 供校准归因
# 默认 AUTONOMY_ENABLED 关 → 放行 0 个。
# ============================================================================

import json
import os
import sys

from touchstone import ghclient   # GitHub HTTP 统一入口（连接池+退避）
from touchstone import review_provider  # review_reliable（引擎健康度判据）
from touchstone.atomicio import atomic_write_json
from touchstone.artifacts import artifact_path


def _envbool(k):
    return os.environ.get(k, "").lower() in ("1", "true", "yes", "on")


AUTONOMY_ENABLED = _envbool("AUTONOMY_ENABLED")       # 总开关，默认关
AUTONOMY_SHADOW = _envbool("AUTONOMY_SHADOW")         # 影子模式，默认关
GRAD_MIN_SAMPLES = int((os.environ.get("GRAD_MIN_SAMPLES") or "").strip() or "20")
GRAD_MAX_BAD_RATE = float((os.environ.get("GRAD_MAX_BAD_RATE") or "").strip() or "0.05")


# --- 变更分类签名（经验在此粒度累积/毕业）------------------------------------
def file_profile(changed_files):
    kinds = set()
    for f in changed_files or []:
        base = os.path.basename(f)
        if f.endswith(".md") or "/docs/" in f or f.startswith("docs/"):
            kinds.add("doc")
        elif "test" in base.lower() or "/test/" in f or "/tests/" in f or "src/test/" in f:
            kinds.add("test")
        else:
            kinds.add("code")
    if not kinds:
        return "empty"
    if kinds == {"doc"}:
        return "docs_only"
    if kinds == {"test"}:
        return "test_only"
    if kinds == {"code"}:
        return "code"
    return "mixed"


def change_class(risk, findings, changed_files, rule_index=None):
    rule_index = rule_index or {}
    cats = sorted({(f.get("category") or rule_index.get(f.get("rule_id"), {}).get("category"))
                   for f in (findings or [])} - {None})
    blast = ",".join(risk.get("blast_radius") or []) or "none"
    return f"{risk.get('risk_band')}|{file_profile(changed_files)}|{','.join(cats) or 'none'}|{blast}"


# --- 经验层：累积与毕业（与 §4.4 promote_to_gate 同构）----------------------
def build_experience(merge_records):
    """merge_records: [{change_class, auto_eligible, reverted, hotfixed, human_override}]
    仅 auto_eligible(本会被自动放行)的样本计入；坏结局=被 revert 或 hotfix。"""
    acc = {}
    for m in merge_records or []:
        if not m.get("auto_eligible"):
            continue
        c = acc.setdefault(m.get("change_class"), {"samples": 0, "bad": 0, "overrides": 0})
        c["samples"] += 1
        if m.get("reverted") or m.get("hotfixed"):
            c["bad"] += 1
        if m.get("human_override"):
            c["overrides"] += 1
    for v in acc.values():
        v["bad_rate"] = round(v["bad"] / v["samples"], 3) if v["samples"] else 0.0
    return acc


def graduate_classes(experience, min_samples=None, max_bad_rate=None):
    min_samples = GRAD_MIN_SAMPLES if min_samples is None else min_samples
    max_bad_rate = GRAD_MAX_BAD_RATE if max_bad_rate is None else max_bad_rate
    return {c for c, v in (experience or {}).items()
            if v["samples"] >= min_samples and v["bad_rate"] <= max_bad_rate}


# --- 自动放行判据（可选路径，默认关）------------------------------------------
def author_trusted(login, association, trusted_associations=None, allowlist=None):
    """作者信任闸判定（纯函数，供单测）——decide_auto_merge 第 9 闸的输入。
    背景：其余 8 闸对 PR 作者身份不敏感，陌生 fork 作者与内部成员待遇相同；而 verify 的
    "绿"由执行 PR 代码的 job 产出（攻击者可影响，见 SECURITY.md 信任边界）——自动放行
    必须叠一道与 PR 内容无关、作者不可自证的身份闸。
    规则（fail-closed）：
      • allowlist（login 白名单，大小写不敏感）命中 → 信任（与关联闸取并集）；
      • 否则 association ∈ trusted_associations（默认 OWNER/MEMBER/COLLABORATOR，
        GitHub author_association 语义：均需仓库/组织侧授予，作者无法单方获得；
        CONTRIBUTOR/FIRST_TIME_CONTRIBUTOR/NONE 可由任意人达成，不信）；
      • login/association 缺失或未知 → False（产物没带作者信息就不放行）。"""
    if trusted_associations is None:
        trusted_associations = {
            a.strip().upper()
            for a in os.environ.get("TOUCHSTONE_AUTONOMY_TRUSTED_ASSOCIATIONS",
                                    "OWNER,MEMBER,COLLABORATOR").split(",") if a.strip()}
    if allowlist is None:
        allowlist = {
            a.strip().lower()
            for a in os.environ.get("TOUCHSTONE_AUTONOMY_AUTHOR_ALLOWLIST", "").split(",")
            if a.strip()}
    if login and str(login).strip().lower() in allowlist:
        return True
    return bool(login) and bool(association) and str(association).strip().upper() in trusted_associations


def decide_auto_merge(risk, findings, loop_decision, gate,
                      autonomy_state, graduated_classes, cls,
                      enabled=None, shadow=None, base_fresh=None, review_reliable=True,
                      unverified_claims=0, author_trusted=False):
    enabled = AUTONOMY_ENABLED if enabled is None else enabled
    shadow = AUTONOMY_SHADOW if shadow is None else shadow
    # 阻断否决（不再是委员会）：high 风险档或任一幸存 block_candidate 发现 → 否决（能拦、不能批）
    veto = (risk.get("risk_band") == "high") or \
           any(f.get("severity") == "block_candidate" for f in (findings or []))
    checks = {
        # 准入只看总闸：契约/确定性规则/(可选)verify 都已折进汇总状态检查，autonomy 不再自行判定质量门禁
        "quality_gate": gate == "success",
        "no_blocking_veto": not veto,
        "loop_converged": loop_decision == "converged",
        "not_tripped": not (autonomy_state or {}).get("tripped"),
        "class_graduated": cls in (graduated_classes or set()),
        # 第七道闸·基线新鲜度（bors「not rocket science」规则）：CI 绿是对旧 main 算的，
        # 直接合可能引入语义冲突（两个 PR 各自绿、合在一起坏）。base_fresh=False（已确认
        # 基线过期）→ 拒绝放行、先带上最新 main 重跑；None（未评估，如纯离线决策/测试）→ 不拦。
        "base_fresh": base_fresh is not False,
        # 引擎健康度：本轮 LLM 评审不可信（引擎降级/可疑空收敛）-> 收敛不可信，不自动放行。
        # 防"diff 被裁空/LLM 随机性"假收敛被当"低风险+收敛"自动合入未评审代码。
        "review_reliable": review_reliable,
        # 独立校验（多层）：清单里有 author 自证但未经人核准的 waived/split 时，绝不自动放行。
        # loop 侧已因此不给 converged（第一道），本闸不信 loop_decision 单点、独立再拦一道——
        # 防 author 虚报 result marker 的 loop_decision=converged 跳过 loop 门。
        "no_unverified_claims": (unverified_claims or 0) == 0,
        # 第 9 闸：作者信任（见 author_trusted docstring）。默认 False = fail-closed：
        # 调用方漏传即按"不信任"拒放行（PRA-SECURITY——安全闸默认须兜底关，不能乐观开）。
        # 生产唯一调用方 main → build_decision_inputs 从 findings 产物 fail-closed 计算
        # 后显式传入；测试 happy-path 须显式传 True。（review_reliable 的 True 默认是既有
        # 保留项，不在本 PR 范围。）
        "author_trusted": author_trusted,
    }
    base = {"checks": checks, "change_class": cls,
            "failed": [k for k, v in checks.items() if not v]}
    if not enabled:
        return {"merge": False, "mode": "disabled",
                "reason": "AUTONOMY_ENABLED 关（默认）→ 回落到人", **base}
    if base["failed"]:
        return {"merge": False, "mode": "shadow" if shadow else "live",
                "reason": "未过闸：" + ",".join(base["failed"]) + " → 回落到人", **base}
    if shadow:
        return {"merge": False, "mode": "shadow", "would_merge": True,
                "reason": "影子模式：各闸通过，本会自动放行（未执行，记证据）", **base}
    return {"merge": True, "mode": "live", "reason": "各闸通过 → 自动放行", **base}


# --- 基线新鲜度（merge skew 防护）--------------------------------------------
def is_base_fresh(pr_data, base_branch_head_sha):
    """纯判定：PR 的 base sha 是否就是 base 分支当前 head（即 CI 结论是对最新基线算的）。"""
    pr_base = ((pr_data or {}).get("base") or {}).get("sha")
    return bool(pr_base) and bool(base_branch_head_sha) and pr_base == base_branch_head_sha


def check_base_fresh(repo, pr_number, token, api_url=None, update_if_behind=True):
    """取 PR 与 base 分支现状判基线新鲜度；过期且 update_if_behind 时调 GitHub
    update-branch API 把最新 base 合进 PR 分支（触发 CI 重跑），本轮返回 False 等下轮再判。
    任一 API 失败 → 返回 None（未评估，不据此拦；对应闸的 fail-open 仅限『评不了』，评出过期必拦）。"""
    api = (api_url or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip("/")
    # 经 ghclient（连接池 + urllib3.Retry 退避 + Retry-After）——自动合并链路最需要健壮性，
    # 此前手写 urllib 无任何重试，恰是"ghclient 唯一入口"承诺的最后一块缺口。
    try:
        prd = ghclient.request("GET", api + f"/repos/{repo}/pulls/{pr_number}", token)
        base_ref = ((prd.get("base") or {}).get("ref")) or "main"
        head = ghclient.request("GET", api + f"/repos/{repo}/commits/{base_ref}", token).get("sha")
    except Exception as e:
        print(f"[autonomy] 基线新鲜度评估失败（不据此拦）: {e}", file=sys.stderr)
        return None
    if is_base_fresh(prd, head):
        return True
    if update_if_behind:
        try:
            ghclient.request("PUT", api + f"/repos/{repo}/pulls/{pr_number}/update-branch",
                             token, data={})
            print("[autonomy] 基线过期：已请求 update-branch，等 CI 重绿后下轮再判", file=sys.stderr)
        except Exception as e:
            print(f"[autonomy] update-branch 失败（仍拒放行）: {e}", file=sys.stderr)
    return False


# --- 合并入队（merge queue / auto-merge 原生通道）------------------------------
def _gql_post(url, token, payload):
    return ghclient.request("POST", url, token, data=payload)


def enqueue_auto_merge(repo, pr_number, token, api_url=None, merge_method="SQUASH"):
    """AUTONOMY_MERGE_MODE=queue：不自己调 merge API，改走 GitHub 原生
    enablePullRequestAutoMerge（分支保护开 merge queue 时即入队）——排队/批测/跳车由
    平台承担，不自建 bors。先查 PR node id，再发 mutation。"""
    api = (api_url or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip("/")
    gql = api[:-3] + "graphql" if api.endswith("/v3") else api + "/graphql"
    owner, name = repo.split("/", 1)
    def _post(payload):
        return _gql_post(gql, token, payload)
    q = _post({"query": "query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n)"
                        "{pullRequest(number:$p){id}}}",
               "variables": {"o": owner, "n": name, "p": int(pr_number)}})
    node = (((q.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("id")
    if not node:
        raise RuntimeError(f"取 PR node id 失败: {q.get('errors')}")
    m = _post({"query": "mutation($id:ID!,$m:PullRequestMergeMethod!){"
                        "enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:$m})"
                        "{pullRequest{autoMergeRequest{enabledAt}}}}",
               "variables": {"id": node, "m": merge_method}})
    if m.get("errors"):
        raise RuntimeError(f"入队失败: {m['errors']}")
    return m



# --- 执行（merge API 集成点；打 auto_handled marker 供校准归因）----------------
def execute_auto_merge(repo, pr_number, sha, token, api_url=None, merge_method="squash"):
    api = (api_url or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip("/")

    def _req(method, path, payload):
        return ghclient.request(method, api + path, token, data=payload)

    merged = _req("PUT", f"/repos/{repo}/pulls/{pr_number}/merge",
                  {"sha": sha, "merge_method": merge_method})
    marker = json.dumps({"auto_handled": True, "sha": sha})
    _req("POST", f"/repos/{repo}/issues/{pr_number}/comments",
         {"body": f"<!-- touchstone:auto_handled {marker} -->\n"
                  "Touchstone 自动放行：质量门禁通过 + 变更分类已达标 + 各闸通过。"})
    return merged


# --- Actions 闭环：组装决策输入 / 从历史重建经验与达标类 ----------------------
def build_decision_inputs(touchstone_out, autonomy_state, graduated_classes):
    """把 touchstone-findings.json（含总闸结论 gate）+ 熔断态 + 达标类 → decide_auto_merge 入参。纯函数。"""
    reliable = touchstone_out.get("review_reliable")
    if reliable is None:
        # 旧产物无 review_reliable 字段 -> 从 engine_status/ai_raw_count/added_lines 重算（向后兼容）
        reliable = review_provider.review_reliable(
            touchstone_out.get("engine_status", "ok"),
            touchstone_out.get("ai_raw_count", 0),
            touchstone_out.get("added_lines", 0))
    return {
        "risk": touchstone_out.get("risk", {}),
        "findings": touchstone_out.get("findings", []),
        "loop_decision": touchstone_out.get("loop_decision"),
        "gate": touchstone_out.get("gate"),
        "autonomy_state": autonomy_state,
        "graduated_classes": list(graduated_classes or []),
        "cls": touchstone_out.get("change_class"),
        "review_reliable": reliable,
        "unverified_claims": touchstone_out.get("unverified_claims", 0),
        # 作者信任：orchestrator 把事件里的 user.login/author_association 写进产物
        # （"author" 字段）。产物缺该字段（旧产物/非 GH 环境）→ author_trusted(None,None)
        # = False：没有作者出处就不自动放行（fail-closed，与 no_unverified_claims 同哲学）。
        "author_trusted": author_trusted(
            (touchstone_out.get("author") or {}).get("login"),
            (touchstone_out.get("author") or {}).get("association")),
    }


def reconstruct_auto_eligible(record):
    """从历史 marker/记录重建【委员会侧】放行资格（质量门禁/熔断在放行时另判）：
    非否决(非 high 档且无 block_candidate) ∧ 闭环收敛 ∧ 契约净。"""
    findings = record.get("findings", [])
    veto = (record.get("risk_band") == "high") or \
           any(f.get("severity") == "block_candidate" for f in findings)
    contract_clean = not any(f.get("agent") == "contract-check" for f in findings)
    return (not veto) and record.get("loop_decision") == "converged" and contract_clean


def experience_record(record, reverted=False, hotfixed=False):
    return {"change_class": record.get("change_class"),
            "auto_eligible": reconstruct_auto_eligible(record),
            "reverted": reverted, "hotfixed": hotfixed}


def graduate_from_calibration(records, reverted_shas=None):
    """校准记录(含 change_class/loop_decision/findings/merge_commit_sha) + revert 集
    → 经验库 → 达标变更分类集合。影子累积：只数 auto_eligible 的人合并样本。"""
    reverted_shas = set(reverted_shas or [])
    recs = [experience_record(r, reverted=(r.get("merge_commit_sha") in reverted_shas))
            for r in records if r.get("merged")]
    return graduate_classes(build_experience(recs))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="touchstone.autonomy")
    ap.add_argument("--inputs", help="显式决策输入 JSON（测试/手动用）")
    ap.add_argument("--execute", action="store_true", help="merge=true 时真执行")
    ap.add_argument("--graduate", action="store_true",
                    help="从 calibration.json 重建经验、写 graduated-classes.json")
    args = ap.parse_args()

    # 模式一：发布达标变更分类（govern 定时任务用）
    if args.graduate:
        # override_env="CALIBRATION_JSON" 与写方（calibrate.py）+ 另一读方（govern.py）对齐，
        # 否则设了 CALIBRATION_JSON 时读 OUTPUT_DIR/calibration.json 而写 CALIBRATION_JSON→graduate
        # 模式拿不到校准数据（#90 round-1 finding autonomy.py:278）
        cal = _load(artifact_path("calibration.json", override_env="CALIBRATION_JSON")) or {}
        grad = sorted(graduate_from_calibration(cal.get("records", [])))
        # 原子：这份毕业类清单直接决定哪些 change_class 可被自动放行，半文件不可接受
        atomic_write_json(artifact_path("graduated-classes.json"), {"graduated_classes": grad})
        print(f"[autonomy] 达标变更分类 {len(grad)}：{grad}")
        return

    # 模式二：决策（+ 可选执行）
    if args.inputs and os.path.exists(args.inputs):
        d = _load(args.inputs) or {}
        cls = d.get("cls") or change_class(d.get("risk", {}), d.get("findings", []),
                                           d.get("changed_files", []), d.get("rule_index", {}))
        repo, pr, sha = d.get("repo"), d.get("pr"), d.get("sha")
    else:
        co = _load(artifact_path("touchstone-findings.json"))
        if not co:
            print("[autonomy] 无 touchstone 产物；no-op（默认不放行）")
            return
        d = build_decision_inputs(
            co,
            {"tripped": os.environ.get("AUTONOMY_TRIPPED") == "true"},
            # 须与写方（line 322 --graduate 的 artifact_path）同路径：设 TOUCHSTONE_OUTPUT_DIR 时
            # 写进隔离目录、读方却去 CWD → 读不到 → graduated_classes 空 → class_graduated 永远 fail
            # （autonomy 对该部署彻底失效）。#90/#122 OUTPUT_DIR 统一漏掉的读点（与 calibration.json/
            # touchstone-findings.json 读法对齐）。
            (_load(artifact_path("graduated-classes.json")) or {}).get("graduated_classes", []))
        cls = d["cls"]
        repo = os.environ.get("GITHUB_REPOSITORY")
        pr, sha = co.get("pr"), co.get("sha")

    base_fresh = None
    if args.execute and repo and pr and os.environ.get("GITHUB_TOKEN"):
        base_fresh = check_base_fresh(repo, pr, os.environ["GITHUB_TOKEN"])
    dec = decide_auto_merge(d.get("risk", {}), d.get("findings", []), d.get("loop_decision"),
                            d.get("gate"), d.get("autonomy_state"),
                            set(d.get("graduated_classes", [])), cls,
                            base_fresh=base_fresh,
                            review_reliable=d.get("review_reliable", True),
                            unverified_claims=d.get("unverified_claims", 0),
                            author_trusted=d.get("author_trusted", False))
    print(json.dumps(dec, ensure_ascii=False))
    if dec["merge"] and args.execute and repo and pr and sha:
        if os.environ.get("AUTONOMY_MERGE_MODE", "direct") == "queue":
            enqueue_auto_merge(repo, pr, os.environ["GITHUB_TOKEN"])
            print("[autonomy] 已入 merge queue（排队/批测/合并由平台执行）")
        else:
            execute_auto_merge(repo, pr, sha, os.environ["GITHUB_TOKEN"])
            print("[autonomy] 已自动放行（auto_handled）")


if __name__ == "__main__":
    main()

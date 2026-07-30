#!/usr/bin/env python3
# ============================================================================
# touchstone/learning_loop.py  ——  自进化评审学习回路（Phase 2）
#   设计：docs/learning-loop-design.html
#
# 一条【无训练、无权重、离线周期】的回路：把"人最终采纳/忽略了什么"蒸馏成自然语言
# 经验，回注 PR-Agent 的 extra_instructions —— 让评审随真实使用自我改进。
#
#   奖励来源 = calibrate.aggregate(records)  （复用；by_rule/by_agent 的 fires/adoption_rate）
#   经验进化 = 计数式蒸馏（distill_candidates，无需大模型）；或 TF-GRPO 语义优势蒸馏
#              （_distill_via_llm，已实现；取自 arXiv 2510.08191，生产需一个参数冻结的旗舰模型端点）
#   蒸馏器可插拔 = distill(ctx, name) 按名分发；register_distiller 注册自有实现（不必改本文件）；
#                  _distill_via_llm 的 rollout/score/distill_advantage 三步亦可注入替换。
#   门控/退役 = graduate（shadow A/B 达标）+ retire（govern 式，前提不再成立即退役）
#   注入     = render_injection(active 经验) → PR-Agent extra_instructions
#
# 两条铁律（来自设计中对坑的应对）：
#   ① 评审与学习解耦：评审路径只【读】经验库；学习是离线 cron，挂了不影响评审（用上一版经验）。
#   ② 经验只调"建议"、绝不进"合入闸"：只对 PR-Agent 源的发现(PRA-*/pr-agent:*)产经验；
#      确定性 contract_check 不受经验影响、永不进经验库（作固定基准，坑 2b）。
#   ③ 新经验默认不注入：先入 candidate 池，经 shadow A/B 达标才转 active（坑 3）。
# ============================================================================

import json
import os
import sys

# ============================================================================
# 第三轮工程化加固：本模块按职责三分——
#   experience_store.py  经验的【状态】（存取/生命周期/注入渲染）
#   distill.py           经验怎么【产生】（计数式 + TF-GRPO，可插拔）
#   ground_truth.py      学习信号从哪【来】（人审裁决重建真值集）
# 本文件保留 CLI/main 编排，并再导出全部名字——既有引用路径
# （orchestrator._ll.* / review_provider / 测试 / seed 脚本）零改动兼容。
# ============================================================================
from touchstone.atomicio import atomic_write_json
from touchstone.experience_store import (  # noqa: F401
    SUPPRESS_ADOPT_MAX, EMPHASIZE_ADOPT_MIN,
    GRADUATE_MIN_SAMPLES, GRADUATE_MIN_LIFT, RETIRE_ADOPT_MAX, STORE_PATH,
    SHADOW_INJECTION_DEFAULT,
    SHADOW_RATIO_DEFAULT, SHADOW_MAX_PER_REVIEW_DEFAULT, SHADOW_MIN_EVIDENCE_DEFAULT,
    BOOTSTRAP_SEED_DEFAULT, BOOTSTRAP_MIN_FIRES_DEFAULT, BOOTSTRAP_MIN_ADOPT_DEFAULT,
    _read_store_text, load_store, save_store, _is_review_type, _exp_id,
    _protected_types, seed_experience, merge_candidates, graduate, retire,
    disable, _resolve_conflicts, _evidence_strength, render_injection, active_types, active_ids,
    _shadow_hash, _shadow_env_params, _shadow_injection_enabled,
    shadow_candidates, shadow_types, shadow_ids,
    _bootstrap_enabled, bootstrap_from_calibrate,
    TAXONOMY_ENFORCE_DEFAULT, _normalize_type, coerce_type, known_types,
    RETIRE_NEGATIVE_LIFT_DEFAULT, retire_on_negative_lift)
from touchstone.distill import (  # noqa: F401
    DISTILL_MIN_FIRES,
    TFGRPO_GROUP_SIZE, _W_NOISE, _W_MISS,
    distill_candidates, _finding_types, score_review, _extract_json, _llm_json,
    rollout_reviews, distill_semantic_advantage, _flagship_llm, _distill_via_llm,
    _counting_distiller, _tfgrpo_distiller, _DISTILLERS, register_distiller,
    distill, _flagship_configured,
    _rollout_cache_key, _load_cache, _save_cache, _Budget, _looks_injected,
    _env_rollout_cache, _env_int_opt,
    _positional_reward_enabled, _is_positional_signal, _position_credit, _score_positional)
from touchstone.ground_truth import (  # noqa: F401
    GT_WINDOW, GT_DIFF_BUDGET, _gh_get, _stack_of, aggregate_ab,
    make_gt_entry, build_ground_truth,
    # 盲区2 坏真值检测（B/C/D 信号 → trust_weight；env 默认全关 = 零行为变化）
    TRUTH_QUALITY_DEFAULT, TRUTH_PENALTY_DEFAULT, TRUTH_HARD_DROP_DEFAULT,
    TRUTH_LGTM_BODY_MAX_DEFAULT, TRUTH_TINY_DIFF_LINES_DEFAULT, LOW_ASSOCIATIONS,
    _truth_quality_enabled, _diff_added_lines, _truth_signals, _trust_weight)
from touchstone.calibrate import (  # noqa: F401
    _APPROVE_SHALLOW, _is_human_reviewer, _lgtm_only)

def _pragent_label_types(path):
    """读 pr-agent.yaml 的 normalization.label_to_category 键 → PRA-* 类型集
    （复用 review_provider.normalize 的 "PRA-"+label.replace(" ","_").upper() 映射，两端不漂移）。
    文件缺/解析失败 → 空集（不阻塞；taxonomy 仍含已 active 类型 + env 扩展）。"""
    try:
        import yaml
    except ImportError:                       # yaml 缺：单独 catch，避免下面 except 引用未绑定的 yaml.YAMLError 反抛 NameError
        return set()
    try:
        with open(path, encoding="utf-8") as f:            # with 上下文：及时关闭句柄（非 CPython 也不锁文件）
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return set()
    if not isinstance(data, dict):     # 非 dict（None/list/标量）→ 视作空，防 data.get 崩
        data = {}
    labels = (((data.get("normalization") or {}).get("label_to_category")) or {}).keys()
    return {"PRA-" + str(k).replace(" ", "_").upper() for k in labels}


def _lift_summary(ab_results):
    """从 ab 数据算 lift 分布（c2）：正/负/样本不足 各多少类型——让"经验净效果"从'事后追问'变'可见'。
    与 retire_on_negative_lift 同源判据（样本门槛镜像 GRADUATE_MIN_SAMPLES）。纯函数。"""
    pos = neg = insuf = 0
    for ab in (ab_results or {}).values():
        if not isinstance(ab, dict):               # null 值条目（JSON ab:null）→ 跳过，防 ab.get 崩
            continue
        ws, wa = ab.get("with_seen", 0), ab.get("with_adopted", 0)
        os_, oa = ab.get("without_seen", 0), ab.get("without_adopted", 0)
        if ws < GRADUATE_MIN_SAMPLES or os_ < GRADUATE_MIN_SAMPLES:
            insuf += 1
            continue
        lift = (wa / ws) - (oa / os_)
        pos += 1 if lift > 0 else 0
        neg += 1 if lift < 0 else 0
    return {"positive_lift": pos, "negative_lift": neg, "insufficient_samples": insuf}


def _resolve_taxonomy(store):
    """决定本轮 merge_candidates 用不用 taxonomy 白名单（c1）。
    TOUCHSTONE_TAXONOMY_ENFORCE 真值时：白名单 = pr-agent.yaml label 集 ∪ 已 active 类型 ∪ env 扩展；
    否则返回 None = 不启用（默认关 = 字节级零行为变化）。"""
    val = os.environ.get("TOUCHSTONE_TAXONOMY_ENFORCE")
    enabled = (TAXONOMY_ENFORCE_DEFAULT if val is None
               else val.lower() in ("1", "true", "yes", "on"))
    if not enabled:
        return None
    yaml_path = os.environ.get(
        "TOUCHSTONE_PRAGENT_YAML",
        os.path.join(os.environ.get("REPO_DIR", "."), ".touchstone", "pr-agent.yaml"))
    return known_types(store, extra=_pragent_label_types(yaml_path))


def _parse_cli(argv):
    import argparse
    p = argparse.ArgumentParser(prog="touchstone.learning_loop",
        description="离线自进化学习回路：人审裁决 → 蒸馏候选经验 → 达标激活/退役 → 落盘。")
    p.add_argument("--store", help=f"经验库路径（默认 {STORE_PATH}）")
    p.add_argument("--ground-truth", dest="ground_truth",
                   help="TF-GRPO 真值集 JSON 路径（配合 --build-ground-truth 写入；存在则读）")
    p.add_argument("--calib-agg", dest="calib_agg",
                   help="calibrate 聚合结果 JSON（计数式蒸馏 + 退役用；支持 calibration.json 外层）")
    p.add_argument("--ab-results", dest="ab_results", help="shadow A/B 结果 JSON（candidate→active 门控用）")
    p.add_argument("--output", help="学习报告输出路径")
    p.add_argument("--build-ground-truth", dest="build_ground_truth", action="store_true",
                   help="从 GitHub 人审裁决重建真值集（需 GITHUB_TOKEN / GITHUB_REPOSITORY）")
    p.add_argument("--window", type=int, default=GT_WINDOW, help="重建真值集时回看的最近已关闭 PR 数")
    p.add_argument("--distiller", help="蒸馏器名(counting/tfgrpo/自定义)；缺省自动：有真值集+旗舰端点→tfgrpo")
    return p.parse_args(argv)


def main(argv=None):
    """离线 cron 入口：读经验库 →(按需重建真值集 / 读 calib_agg)→ 蒸馏 → 并入候选 →
    达标激活 / 退役 → 落盘 + 学习报告 + changed 输出。
    被测试/库直接调用(argv=None)时走环境变量，保持既有行为；以 -m/脚本带 CLI 参数运行时解析参数。"""
    if argv is not None:                       # CLI 路径（learn.yml 走这里）
        a = _parse_cli(argv)
        store_path = a.store or STORE_PATH
        gt_path = a.ground_truth
        agg_path = a.calib_agg or os.environ.get("TOUCHSTONE_CALIB_AGG")
        ab_path = a.ab_results or os.environ.get("TOUCHSTONE_AB_RESULTS")
        out_path = a.output
        build_gt = a.build_ground_truth
        window = a.window
        distiller = a.distiller
    else:                                      # 环境变量路径（库/测试）
        store_path = STORE_PATH
        agg_path = os.environ.get("TOUCHSTONE_CALIB_AGG")
        gt_path = os.environ.get("TOUCHSTONE_TFGRPO_GROUNDTRUTH")
        ab_path = os.environ.get("TOUCHSTONE_AB_RESULTS")
        out_path = os.environ.get("TOUCHSTONE_LEARNING_REPORT")
        build_gt = os.environ.get("TOUCHSTONE_BUILD_GROUND_TRUTH", "").lower() in ("1", "true", "yes")
        window = GT_WINDOW
        distiller = None

    report = {"steps": [], "distiller": None, "candidates": 0, "graduated": [],
              "retired": [], "active": 0, "total": 0, "ground_truth": 0}
    store = load_store(store_path)
    before = {(e.get("id"), e.get("status"), e.get("text")) for e in store.get("experiences", [])}

    # ① 真值集：按需从 GitHub 人审裁决重建（"人工合入好坏" → TF-GRPO 学习信号）
    ground_truth = None
    if build_gt:
        token = os.environ.get("GITHUB_TOKEN")
        repo_full = os.environ.get("GITHUB_REPOSITORY") or ""
        if token and "/" in repo_full:
            owner, repo_name = repo_full.split("/", 1)
            try:
                ground_truth = build_ground_truth(owner, repo_name, token, window=window)
                if gt_path:
                    os.makedirs(os.path.dirname(gt_path) or ".", exist_ok=True)
                    # 原子写：真值喂校准（决策相邻态），崩溃留半文件会让下轮校准
                    # 读到损坏 JSON——与决策态同纪律走 atomicio。
                    atomic_write_json(gt_path, ground_truth)
                report["steps"].append(f"build_ground_truth: 重建 {len(ground_truth)} 条真值")
            except Exception as e:
                report["steps"].append(f"build_ground_truth 失败: {e}")
        else:
            report["steps"].append("build_ground_truth 跳过：缺 GITHUB_TOKEN/GITHUB_REPOSITORY")
    if ground_truth is None and gt_path and os.path.exists(gt_path):
        try:
            with open(gt_path, encoding="utf-8") as f:
                ground_truth = json.load(f)
        except (OSError, json.JSONDecodeError):
            ground_truth = None

    # 真值集下限门控（TOUCHSTONE_GROUND_TRUTH_MIN）：不足则不跑 TF-GRPO，回退计数式
    gt_min = int((os.environ.get("TOUCHSTONE_GROUND_TRUTH_MIN") or "").strip() or "0")
    if ground_truth and gt_min and len(ground_truth) < gt_min:
        report["steps"].append(f"真值集 {len(ground_truth)} < 下限 {gt_min}，TF-GRPO 跳过")
        ground_truth = None
    report["ground_truth"] = len(ground_truth or [])

    # ② calibrate 聚合（计数式蒸馏的奖励 + 退役的前提信号）
    agg = None
    if agg_path and os.path.exists(agg_path):
        try:
            with open(agg_path, encoding="utf-8") as f:
                raw = json.load(f)
            agg = raw.get("aggregate", raw) if isinstance(raw, dict) else raw   # 兼容 calibration.json
        except (OSError, json.JSONDecodeError):
            agg = None

    # ③ 蒸馏：有真值集 + 旗舰端点 → TF-GRPO（语义优势）；否则计数式
    name = distiller or os.environ.get("TOUCHSTONE_DISTILLER")
    ctx = {"calib_agg": agg or {}, "ground_truth": ground_truth,
           "store": store, "repo": os.environ.get("REPO_DIR", ""),
           "stack": os.environ.get("TOUCHSTONE_STACK", "")}
    if not name:
        name = "tfgrpo" if (ground_truth and _flagship_configured()) else "counting"
    try:
        cands = distill(ctx, name)
    except RuntimeError as e:                  # 旗舰端点未配置等 → 回退计数式
        report["steps"].append(f"distill({name}) 失败：{e}（回退 counting）")
        cands = distill(ctx, "counting")
        name = "counting"
    report["distiller"] = name
    report["candidates"] = len(cands)
    # ③.5 bootstrap seed（merge 前，冷启动辅助路径 c，env 开时）：高采纳全新 type 直接 seed active
    # emphasize——让全新 type 立即有首个 active 撑 aggregate_ab with 臂，与 shadow 注入(a) 互补。
    # 必须在 merge_candidates【前】：distill 已把 adoption>=0.80 的 type 产成 candidate，若 bootstrap
    # 在 merge 后跑，其 existing 检查会命中刚并入的 candidate 而跳过 → 永不触发。放 merge 前：bootstrap
    # active 先入 store，随后 merge 的同 id candidate 经 update 分支补 evidence 但不降级 active
    # （merge_candidates 不降级 active/retired）。默认关=零行为变化。
    bootstrapped = bootstrap_from_calibrate(agg or {}, store,
                                            repo=os.environ.get("REPO_DIR", ""),
                                            stack=os.environ.get("TOUCHSTONE_STACK", ""))
    if bootstrapped:
        report["steps"].append(f"bootstrap_from_calibrate: 高采纳 type 直接 seed active："
                               f"{len(bootstrapped)} 条 {bootstrapped}")

    merge_candidates(store, cands, taxonomy=_resolve_taxonomy(store))

    # ④ candidate → active（shadow A/B 达标）
    ab = None
    if ab_path and os.path.exists(ab_path):
        try:
            with open(ab_path, encoding="utf-8") as f:
                ab = json.load(f)
        except (OSError, json.JSONDecodeError):
            ab = None
    if ab is None and ground_truth:
        ab = aggregate_ab(ground_truth)            # 按每 PR 的 injected_types 切 with/without 两臂
        report["steps"].append(f"aggregate_ab: 从 {len(ground_truth)} 条真值切 A/B（注入臂需积累才有效）")
    if ab:
        grad = graduate(store, ab)
        report["graduated"] = grad
        report["steps"].append(f"graduate 达标转 active：{len(grad)} 条 {grad}")
        # c2：差分回滚——注入反降采纳率的 active 经验退役（与 graduate 对称），让坏经验不必
        # 等跌破 retire 绝对门槛才下线。lift 摘要让"经验净效果"可见（多少正/负 lift）。
        neg = retire_on_negative_lift(store, ab)
        if neg:
            report["steps"].append(f"retire_on_negative_lift 注入反降退役：{len(neg)} 条 {neg}")
        report["lift_summary"] = _lift_summary(ab)
    else:
        report["steps"].append("graduate 跳过（无 A/B 数据；自动达标需积累样本）")

    # ⑤ active → retired（前提不再成立）
    if agg:
        retired = retire(store, agg)
        report["retired"] = retired
        if retired:
            report["steps"].append(f"retire 退役：{len(retired)} 条 {retired}")

    save_store(store, store_path)
    report["active"] = sum(1 for e in store["experiences"] if e["status"] == "active")
    report["total"] = len(store["experiences"])

    # ⑥ 学习报告 + changed 输出（供 workflow 决定是否提交经验库）
    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    after = {(e.get("id"), e.get("status"), e.get("text")) for e in store.get("experiences", [])}
    changed = "true" if before != after else "false"
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"changed={changed}\n")
    print(f"[learn] distiller={name} 候选={report['candidates']} "
          f"真值={report['ground_truth']} active={report['active']}/{report['total']} changed={changed}")
    for s in report["steps"]:
        print(f"[learn] {s}")
    return report


if __name__ == "__main__":
    main(sys.argv[1:])

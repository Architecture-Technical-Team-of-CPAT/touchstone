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
    _protected_types, seed_experience, merge_candidates, canonicalize_store, graduate, retire,
    disable, _resolve_conflicts, _evidence_strength, render_injection, active_types, active_ids,
    _shadow_hash, _shadow_env_params, _shadow_injection_enabled,
    shadow_candidates, shadow_types, shadow_ids,
    _bootstrap_enabled, bootstrap_from_calibrate,
    TAXONOMY_ENFORCE_DEFAULT, _normalize_type, _canonical_type, coerce_type, known_types,
    RETIRE_NEGATIVE_LIFT_DEFAULT, retire_on_negative_lift,
    CONVERGE_N_STABLE_DEFAULT, CONVERGE_LIFT_DRIFT_DEFAULT, CONVERGENCE_ENABLED_DEFAULT,
    update_convergence, converged_types,
    TREND_MAX_HISTORY_DEFAULT, AUTO_ROLLBACK_M_DEFAULT, DIFFERENTIAL_METRICS_DEFAULT,
    append_lift_history, retire_on_lift_decline,
    _differential_enabled, _auto_rollback_m, _trend_max_history, _is_declining)
from touchstone.distill import (  # noqa: F401
    DISTILL_MIN_FIRES,
    TFGRPO_GROUP_SIZE, _W_NOISE, _W_MISS,
    distill_candidates, _finding_types, score_review, _extract_json, _llm_json,
    rollout_reviews, distill_semantic_advantage, _flagship_llm, _distill_via_llm,
    _counting_distiller, _tfgrpo_distiller, _DISTILLERS, register_distiller,
    distill, _flagship_configured,
    _rollout_cache_key, _load_cache, _save_cache, _Budget, _looks_injected,
    _env_rollout_cache, _env_int_opt,
    _positional_reward_enabled, _is_positional_signal, _position_credit, _score_positional,
    DISTILL_MIN_SOURCE_PRS_DEFAULT, _distill_min_source_prs, _distill_max_reward_var,
    _pvariance, _filter_by_consistency)
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
    # learn.yml 用 ${{ vars.TOUCHSTONE_TAXONOMY_ENFORCE }} 透传：vars. 未设时 GHA 求值为空串、传入
    # "TOUCHSTONE_TAXONOMY_ENFORCE="（present-but-empty）而非不设。把空串归一到 None（=未设），
    # 让下方 val is None 分支一致生效，避免 "空/未设 → None" 的注释语义与实际路径漂移。
    if val is not None and not val.strip():
        val = None
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
    p.add_argument("--watermark", dest="watermark",
                   help="增量水位 JSON 路径（差距3b；配合 --build-ground-truth，记录上次处理到的 PR 编号）")
    p.add_argument("--trend", dest="trend",
                   help="差分时序 JSON 路径（差距3b；adoption-trend.json，记录 per-type lift 跨轮趋势）")
    p.add_argument("--distiller", help="蒸馏器名(counting/tfgrpo/自定义)；缺省自动：有真值集+旗舰端点→tfgrpo")
    return p.parse_args(argv)


# --- 差距3b：增量水位（opt-in，默认关 = 零行为变化）------------------------------
# learn.yml 每周一全量重建 GT_WINDOW=30 个 PR；增量水位记录上次处理到的 PR 编号，下轮只取数
# number>水位 的新 PR（省 per-PR ~5 次 API 调用）。周期性全量对账（FULL_REFRESH_EVERY）兜底
# 漂移：旧 PR 新增评审信号、或信号-less PR 滞留的误差，每 N 轮一次全量重建消化掉。
# 水位文件随经验库一起 git 提交（learn.yml 已 commit data/*.json）→ 下轮 checkout 即可用，
# 与 save_store 同纪律（save_store 成功后才写水位——失败不推进，下轮重取，幂等）。
INCREMENTAL_DEFAULT   = "false"     # vars/未设 → off（全量=现状）
FULL_REFRESH_EVERY_DFLT = 4         # 每 N 轮强制全量对账一次


def _incremental_enabled():
    """TOUCHSTONE_INCREMENTAL 真值时开增量水位（默认关 = 全量重建=现状）。"""
    return os.environ.get("TOUCHSTONE_INCREMENTAL", INCREMENTAL_DEFAULT).lower() in ("1", "true", "yes", "on")


def _full_refresh_every():
    """TOUCHSTONE_FULL_REFRESH_EVERY：每 N 轮全量对账一次（默认 4）；非正 → 永不全量（纯增量）。"""
    try:
        n = int((os.environ.get("TOUCHSTONE_FULL_REFRESH_EVERY") or "").strip() or str(FULL_REFRESH_EVERY_DFLT))
    except ValueError:
        n = FULL_REFRESH_EVERY_DFLT
    return n if n > 0 else 0


def _read_watermark(path):
    """读水位文件 → {"watermark": int, "round": int} 或 None（缺/损坏→None=首轮全量）。"""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    wm = d.get("watermark")
    rnd = d.get("round", 0)
    try:                                    # 防脏值（字符串/null）→ int 或视为缺失
        wm = int(wm) if wm is not None else None
        rnd = int(rnd)
    except (TypeError, ValueError):
        wm, rnd = None, 0
    return {"watermark": wm, "round": rnd} if wm is not None else {"watermark": None, "round": rnd}


def _write_watermark(path, watermark, rnd):
    """原子写水位（与 atomic_write_json 同纪律——崩溃留半文件会让下轮读到损坏 JSON）。"""
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_json(path, {"watermark": int(watermark or 0), "round": int(rnd)})


def _pr_id_int(entry):
    """安全提取 ground_truth 条目的 pr_id 为 int。

    PRA round-2（learning_loop.py:400 "Silent Exception Risk"）：旧裸表达式
    `int(e["pr_id"])` 依赖外层 `str(e.get("pr_id","")).isdigit()` 守卫，但守卫与
    取值分两处、且 `e["pr_id"]` 用下标——若 pr_id 缺失会 KeyError、非数字非空串
    （负数 "-1" 等）会 ValueError。提取为纯函数：缺失/非数字→None，调用方过滤。
    兼容 pr_id 为 int 型（100）或 str 型（"100"）——str() 归一再判 isdigit，无 AttributeError。"""
    v = entry.get("pr_id")
    if v is None:
        return None
    s = str(v).strip()
    return int(s) if s.isdigit() else None


def _decide_since_pr(wm_state, *, force_full=False, full_every=None):
    """纯函数：据水位状态决定本轮 since_pr（None=全量；正整数=增量只取 number>since_pr）。
    返回 (since_pr, mode)：
      - mode="first"        首轮（无水位）→ 全量重建，建立基线。
      - mode="force_full"   FORCE_REBUILD / 手动触发 → 全量。
      - mode="periodic_full" round % full_every == 0 → 周期性全量对账（兜底漂移）。
      - mode="incremental"  otherwise → 增量，since_pr=水位。
    full_every<=0 视作"永不全量对账"（纯增量，mode 不会是 periodic_full）。"""
    full_every = full_every if full_every is not None else _full_refresh_every()
    rnd = (wm_state or {}).get("round", 0)
    wm = (wm_state or {}).get("watermark")
    if force_full:
        return None, "force_full"
    if wm is None:
        return None, "first"
    # PRA round-3（learning_loop.py:233 "rnd>0 gate"）：去掉 `rnd > 0` 前置——bootstrap 修复后首轮
    # 写 round=1（round=0 不再持久化），该门控原本排除的 round=0 场景已不可达；保留它只会让
    # 外部构造的 round=0 文件错走增量。rnd%full_every==0（含 rnd=0）→ periodic_full（安全全量）。
    if full_every > 0 and rnd % full_every == 0:
        return None, "periodic_full"
    return wm, "incremental"


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
        wm_path = a.watermark
        trend_path = a.trend
    else:                                      # 环境变量路径（库/测试）
        store_path = STORE_PATH
        agg_path = os.environ.get("TOUCHSTONE_CALIB_AGG")
        gt_path = os.environ.get("TOUCHSTONE_TFGRPO_GROUNDTRUTH")
        ab_path = os.environ.get("TOUCHSTONE_AB_RESULTS")
        out_path = os.environ.get("TOUCHSTONE_LEARNING_REPORT")
        build_gt = os.environ.get("TOUCHSTONE_BUILD_GROUND_TRUTH", "").lower() in ("1", "true", "yes")
        window = GT_WINDOW
        distiller = None
        wm_path = os.environ.get("TOUCHSTONE_WATERMARK_PATH")
        trend_path = os.environ.get("TOUCHSTONE_TREND_PATH")

    report = {"steps": [], "distiller": None, "candidates": 0, "graduated": [],
              "retired": [], "active": 0, "total": 0, "ground_truth": 0}
    store = load_store(store_path)
    before = {(e.get("id"), e.get("status"), e.get("text")) for e in store.get("experiences", [])}

    # ① 真值集：按需从 GitHub 人审裁决重建（"人工合入好坏" → TF-GRPO 学习信号）
    ground_truth = None
    # 差距3b 增量水位（opt-in，默认关=全量=现状）：开时读水位、只取数 number>水位 的新 PR；
    # 周期性全量对账（round % FULL_REFRESH_EVERY == 0）或 FORCE_REBUILD 时回全量，消化漂移。
    # wm_active 与 wm_state 分离：wm_state 在首轮（水位文件未建）为 None，但 wm_active 仍真——
    # PRA round-3：旧写块门控 `wm_state is not None` 让首轮跳过写水位 → 文件永不创建 → 增量特性
    # 永不激活（每轮都 first 模式）。wm_active 独立判增量是否启用，首轮也能 bootstrap 写出水位。
    wm_active = bool(wm_path) and build_gt and _incremental_enabled()
    wm_state = _read_watermark(wm_path) if wm_active else None
    since_pr = None
    if wm_state is not None:
        force_full = os.environ.get("FORCE_REBUILD", "").lower() in ("1", "true", "yes")
        since_pr, mode = _decide_since_pr(wm_state, force_full=force_full, full_every=_full_refresh_every())
        if mode == "incremental":
            report["steps"].append(f"build_ground_truth 增量模式：since_pr={since_pr}（round={wm_state.get('round', 0)}）")
        else:
            report["steps"].append(f"build_ground_truth 全量模式（{mode}）")
    elif wm_active:
        report["steps"].append("build_ground_truth 全量模式（first：水位未建，本轮 bootstrap）")
    # 差距3b 差分时序（opt-in，默认关）：读历史 trend，本轮 append 后判趋势回滚（下方 ab 就绪后）。
    trend = None
    if _differential_enabled() and trend_path:
        try:
            with open(trend_path, encoding="utf-8") as f:
                trend = json.load(f)
            if not isinstance(trend, dict):
                print(f"[learn] 警告：trend 文件非 dict（{type(trend).__name__}），重置为 {{}}——"
                      f"历史时序丢失，请检查 {trend_path}", file=sys.stderr)
                trend = {}
        except json.JSONDecodeError:
            # PRA round-4（learning_loop.py:275）：损坏的 trend 文件静默重置会丢全部历史时序，无信号。
            # 发声提醒运维调查（数据丢失可见），仍重置为 {} 以不阻断本轮。
            print(f"[learn] 警告：trend 文件 JSON 损坏，重置为 {{}}——历史时序丢失，请检查 {trend_path}",
                  file=sys.stderr)
            trend = {}
        except OSError:
            trend = {}                                # 文件不存在（首轮）——正常，不警示
    if build_gt:
        token = os.environ.get("GITHUB_TOKEN")
        repo_full = os.environ.get("GITHUB_REPOSITORY") or ""
        if token and "/" in repo_full:
            owner, repo_name = repo_full.split("/", 1)
            try:
                ground_truth = build_ground_truth(owner, repo_name, token, window=window, since_pr=since_pr)
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
    # 差距3a：收敛检测（默认关）开时，已 stable 的 type 不再蒸馏（active 已稳定，省候选产出/rollout）。
    skip = converged_types(store)
    if skip:
        report["steps"].append(f"converged_types: 跳过 {len(skip)} 个稳定 type 的蒸馏：{sorted(skip)}")
    name = distiller or os.environ.get("TOUCHSTONE_DISTILLER")
    ctx = {"calib_agg": agg or {}, "ground_truth": ground_truth,
           "store": store, "repo": os.environ.get("REPO_DIR", ""),
           "stack": os.environ.get("TOUCHSTONE_STACK", ""),
           "skip_types": skip}
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

    # ③.6 归一化存量：合并大小写/分隔符变体造成的重复条目（PRA-CONSISTENCY 与 PRA-consistency 等）。
    # merge_candidates 已对【新进】候选规范化；本步清【存量】历史重复——TF-GRPO 的 LLM 自由产 finding_type
    # 时曾把同一规律裂成多条。幂等，已干净则无副作用。不丢弃任何条目。（条目数减少=有合并；rename 不减数）
    _n_before = len(store.get("experiences", []))
    canonicalize_store(store)
    if len(store.get("experiences", [])) < _n_before:
        report["steps"].append(
            f"canonicalize_store: 合并 {_n_before - len(store['experiences'])} 条重复 finding_type 变体")

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

    # ⑤.5 差距3a：收敛检测——据本轮 ab lift 更新 active 经验的 stable 状态（默认关=零行为变化）。
    # 放 graduate/retire 之后：用最终 active 集合的 lift 判收敛；新 active（刚 graduate）从 0 计起。
    if ab:
        newly_stable = update_convergence(store, ab)
        if newly_stable:
            report["steps"].append(f"update_convergence: 新标 stable {len(newly_stable)} type：{sorted(newly_stable)}")

    # ⑤.6 差距3b 差分时序 + 趋势回滚（默认关=零行为变化）。先 append 本轮 lift 到时序，再据趋势退役。
    # 放收敛检测后、retire 后：用最终 lift 时序判"持续恶化"；与 retire_on_negative_lift（静态阈值）互补。
    if trend is not None and ab:
        append_lift_history(trend, ab)
        declined = retire_on_lift_decline(store, trend)
        if declined:
            report["steps"].append(f"retire_on_lift_decline 趋势退役：{len(declined)} 条 {declined}")

    save_store(store, store_path)
    report["active"] = sum(1 for e in store["experiences"] if e["status"] == "active")
    report["total"] = len(store["experiences"])

    # 差距3b：save_store 成功后才推进水位（同 atomic 纪律——失败不推进，下轮重取，幂等）。
    # PRA round-1：新水位 = max(本轮条目 pr_id, 旧水位)——永不回退（含全量轮 since_pr=None）。
    # PRA round-2：门控 `ground_truth is not None`（列表存在即推进 round，空也前进，防空轮死循环）。
    # PRA round-3（learning_loop.py:273/233 "bootstrap"）：旧门控 `wm_state is not None` 在首轮
    #   （水位文件未建）为假 → 写块跳过 → 文件永不创建 → 增量永不激活。改门控为 `wm_active`
    #   （= wm_path 设 + build_gt + 增量开），首轮 wm_state=None 也 bootstrap：old_wm/round 取 0。
    if wm_active and ground_truth is not None:
        old_wm = (wm_state or {}).get("watermark") or 0          # 首轮 wm_state=None → old_wm=0
        old_round = (wm_state or {}).get("round", 0)              # 首轮 → old_round=0
        new_round = old_round + 1                                 # round 始终推进（驱动周期性全量调度）
        new_wm = old_wm                                           # 默认保持旧水位
        if ground_truth:                                          # 非空才算新水位
            # _pr_id_int 安全提取（缺失/非数字→None，过滤；兼容 int/str 型 pr_id）
            pids = [p for e in ground_truth if (p := _pr_id_int(e)) is not None]
            # PRA round-3（learning_loop.py:416 "Missing Minimum-Sample Guard"）：真值非空但
            # pids 为空（所有 pr_id 异常）→ 水位静默停滞在 old_wm。发声提醒（API schema 变更等）。
            if not pids:
                print(f"[learn] 警告：{len(ground_truth)} 条真值均无有效 pr_id，"
                      f"水位停滞在 {old_wm}（检查 ground_truth 的 pr_id 字段）", file=sys.stderr)
            new_wm = max(new_wm, max(pids, default=0))
        _write_watermark(wm_path, new_wm, new_round)
        report["steps"].append(f"learn_watermark: 推进至 pr={new_wm}（round {old_round}→{new_round}）")

    # 差距3b：save_store 成功后才持久化时序（同 atomic 纪律——失败不写半文件，下轮从旧 trend 继续）。
    if trend is not None and trend_path:
        os.makedirs(os.path.dirname(trend_path) or ".", exist_ok=True)
        atomic_write_json(trend_path, trend)

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

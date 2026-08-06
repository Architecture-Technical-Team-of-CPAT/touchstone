#!/usr/bin/env python3
# ============================================================================
# touchstone/distill.py —— 经验蒸馏（计数式 + TF-GRPO 语义优势，可插拔分发）
# ----------------------------------------------------------------------------
# 从 learning_loop 拆出（模块职责单一化，第三轮工程化加固）。本模块只管经验怎么
# 【产生】：计数式蒸馏（distill_candidates，无需大模型）；TF-GRPO 语义优势蒸馏
# （_distill_via_llm，arXiv 2510.08191：分组 rollout → 组内奖励对比 → LLM 蒸馏差异，
#  需参数冻结的旗舰模型端点）；distill(ctx, name) 按名分发 + register_distiller 注册自有实现。
# 产出一律是 status=candidate 的候选——激活/退役等生命周期在 experience_store.py。
# ============================================================================

import hashlib
import json
import os
import re
import sys
import tempfile
import time

from touchstone.experience_store import (_exp_id, _is_review_type,           # noqa: F401
                                         _protected_types, render_injection,
                                         SUPPRESS_ADOPT_MAX, EMPHASIZE_ADOPT_MIN)
# 采纳率阈值的单一事实来源在 experience_store（入池与退役是同一对判据的镜像）；此处引用。

# --- 阈值 ---------------------------------------------------------------------
DISTILL_MIN_FIRES   = 8      # 命中样本下限，才考虑蒸馏成候选经验

# 差距2a：跨 PR 一致性（opt-in，默认关 = 零行为变化）
#   仅 1 PR 高 reward 的 candidate 是"运气"非"能力"——入池前要求 source_prs ≥ K 且 reward_var 小。
#   默认 K=1（=不限=现状）、max_var 未设（=不检查）；开启需 vars 显式设更紧值。
DISTILL_MIN_SOURCE_PRS_DEFAULT = 1     # 1 = 不限（每条 candidate 至少来自 1 PR，恒满足）


def _distill_min_source_prs():
    """TOUCHSTONE_DISTILL_MIN_SOURCE_PRS：candidate 至少来自 N 个 PR 才入池（默认 1=不限=现状）。"""
    try:
        # int(float(...)) 兜底 "2.0" 等合法 float 串——int("2.0") 抛 ValueError 会静默退默认值
        # （PRA round-4 distill.py:34），让 TOUCHSTONE_DISTILL_MIN_SOURCE_PRS=2.0 悄悄变 1 难排查。
        n = int(float((os.environ.get("TOUCHSTONE_DISTILL_MIN_SOURCE_PRS") or "").strip()
               or str(DISTILL_MIN_SOURCE_PRS_DEFAULT)))
    except (ValueError, TypeError):
        n = DISTILL_MIN_SOURCE_PRS_DEFAULT
    return n if n > 0 else DISTILL_MIN_SOURCE_PRS_DEFAULT


def _distill_max_reward_var():
    """TOUCHSTONE_DISTILL_MAX_REWARD_VAR：跨 PR reward 方差上限（默认未设=不检查）。
    非负 float 启用；空/非法/负 → None（不检查）。设计推荐 0.15。"""
    v = (os.environ.get("TOUCHSTONE_DISTILL_MAX_REWARD_VAR") or "").strip()
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return f if f >= 0 else None


def _pvariance(values):
    """总体方差（纯函数）。n<2 返回 0.0——单点方差定义性为 0（恒 ≤ max_var → pass），并非"无意义"：
    单 PR 候选的样本量把关由 min_source_prs 闸负责（正交职责），方差闸只度量既有 ≥2 PR 的一致性
    （PRA round-4 distill.py:57）。避免顶层 import statistics（仅此处用）。"""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return sum((x - mean) ** 2 for x in values) / n

# --- 蒸馏：calibrate 奖励 → 候选经验（训练-free 计数式）--------------------------
def distill_candidates(calib_agg, repo="", stack="", skip_types=None):
    """从 calibrate.aggregate 的 by_rule 统计蒸馏候选经验（无 LLM、无权重）。
    低采纳→suppress（别挑）、高采纳→emphasize（该挑）。只对 PR-Agent 源类型；确定性 contract 类型被跳过。
    更丰富的 TF-GRPO 语义优势蒸馏见 _distill_via_llm（已实现，需旗舰模型端点）。
    skip_types（差距3a）：已收敛稳定的 type 集合，跳过其候选产出（active 已稳定，不必反复改写）。"""
    now = int(time.time())
    protected = _protected_types()
    skip_types = skip_types or set()
    out = []
    for ftype, v in (calib_agg.get("by_rule") or {}).items():
        if not _is_review_type(ftype):
            continue                      # 确定性 contract 类型不进经验（固定基准）
        if ftype in skip_types:
            continue                      # 收敛稳定：跳过（省候选产出；active 不变）
        fires = v.get("fires", 0)
        adopt = v.get("adoption_rate")
        if adopt is None:
            adopt = v.get("changes_requested_rate")
        if fires < DISTILL_MIN_FIRES or adopt is None:
            continue
        if adopt <= SUPPRESS_ADOPT_MAX:
            if ftype in protected:
                continue                      # 红线：受保护类型永不 suppress
            kind, text = "suppress", (f"Deprioritize {ftype}-type suggestions in this repo; "
                                      f"historically dismissed (adoption {adopt:.0%} over {fires}).")
        elif adopt >= EMPHASIZE_ADOPT_MIN:
            kind, text = "emphasize", (f"Emphasize {ftype}-type suggestions in this repo; "
                                       f"historically valued (adoption {adopt:.0%} over {fires}).")
        else:
            continue
        out.append({"id": _exp_id(ftype, kind, repo, stack), "repo": repo, "stack": stack,
                    "finding_type": ftype, "kind": kind, "text": text,
                    "evidence": {"fires": fires, "adoption": round(adopt, 2)},
                    "status": "candidate", "source": "counting", "locked": False,
                    "source_prs": [], "created_at": now, "updated_at": now})
    return out


# --- TF-GRPO：分组 rollout + 组内语义优势 → 候选经验 -----------------------------
#   取自 Training-Free GRPO（arXiv 2510.08191）：策略（PR-Agent 旗舰模型）冻结不动，
#   用“组内相对语义优势”取代数值优势/梯度，把经验积累成注入提示词的 token prior。
#   落到 PR 评审：对历史已合 PR（带人审裁决的最小真值集）分组生成评审、离线打分、
#   旗舰模型内省高分 vs 低分 → 候选经验。无梯度、无权重。
TFGRPO_GROUP_SIZE = int((os.environ.get("TOUCHSTONE_TFGRPO_G") or "").strip() or "4")
_W_NOISE = float((os.environ.get("TOUCHSTONE_W_NOISE") or "").strip() or "0.5")   # 噪声（人忽略却挑了）扣分权重，人可调
_W_MISS  = float((os.environ.get("TOUCHSTONE_W_MISS") or "").strip() or "0.25")   # 漏报（人采纳却没挑）扣分权重，人可调


def _finding_types(review):
    return {(f.get("finding_type") or f.get("rule_id")) for f in (review or [])
            if (f.get("finding_type") or f.get("rule_id"))}


def score_review(review, human_adopted, *, w_noise=None, w_miss=None):
    """② 按人审真值给一份评审离线打分（纯函数、不需大模型，复用 calibrate 的命中/噪声口径）。
    review: 一次 rollout 的发现列表（每个含 finding_type）。
    human_adopted: 人最终采纳的发现——【类型集合】[str]（既有口径）或【位置信号】[{finding_type,file,line}]
    （差距1a opt-in，经 _distill_via_llm 按 TOUCHSTONE_POSITIONAL_REWARD 选择）。
    奖励 = 命中(真阳) − w_noise·噪声(假阳) − w_miss·漏报。权重缺省取 _W_NOISE/_W_MISS（env 可配、人可调）。"""
    w_noise = _W_NOISE if w_noise is None else w_noise
    w_miss = _W_MISS if w_miss is None else w_miss
    if _is_positional_signal(human_adopted):
        return _score_positional(review, human_adopted, w_noise, w_miss)
    adopted = set(human_adopted or [])
    seen = _finding_types(review)
    hits = len(seen & adopted)
    noise = len(seen - adopted)
    miss = len(adopted - seen)
    return hits - w_noise * noise - w_miss * miss


def _env_num(parse, name, default):
    """env 数值解析：空串或非法值（如 'abc'）→ default，防 import 期 int()/float() 崩（#132 review）。"""
    raw = (os.environ.get(name) or "").strip()
    try:
        return parse(raw) if raw else default
    except (TypeError, ValueError):
        return default


# --- 位置级奖励（差距1a，opt-in 默认关）-----------------------------------------
# 类型集合匹配把"同类型、不同位置"全算 1.0 命中——位置级改为 (type,file,行邻近) 部分信用，奖励更细、
# 方差更低。注意【数据依赖】：位置信号 human_adopted_positions 由 make_gt_entry 从 resolved findings
# （带 file/line）产；calibrate.thread_findings 现已带线程锚定的 file/line（parse_review_threads 从
# GraphQL reviewThread.path/line 解出），build_ground_truth 据此把 resolved findings 传 make_gt_entry——
# 故真值侧已有真位置数据。本评分器离线可测；生产真正生效还需开 TOUCHSTONE_POSITIONAL_REWARD（仍 opt-in 默认关）。
# 解析用 _env_num（#132 防 malformed：空串/非法值回落默认、不崩）——1a 激活的数据管道与 #132 健壮解析并存。
_POS_LINE_WINDOW = _env_num(int, "TOUCHSTONE_POS_LINE_WINDOW", 10)
_POS_PARTIAL_SAMEFILE = _env_num(float, "TOUCHSTONE_POS_PARTIAL_SAMEFILE", 0.5)   # 同 file 行距远
_POS_PARTIAL_NOFILE = _env_num(float, "TOUCHSTONE_POS_PARTIAL_NOFILE", 0.5)       # 无 file 可比


def _positional_reward_enabled():
    """差距1a 总开关（默认关）：真值时 _distill_via_llm 把 human_adopted_positions 喂 score_review。
    关 → 类型集合匹配（reward 字节级不变）。"""
    return os.environ.get("TOUCHSTONE_POSITIONAL_REWARD", "").lower() in ("1", "true", "yes", "on")


def _is_positional_signal(human_adopted):
    """human_adopted 是位置信号（dict 列表）还是类型集（str 列表）？看首元素类型。"""
    seq = human_adopted or []
    return bool(seq) and isinstance(seq[0], dict)


def _position_credit(finding, positions):
    """review 项 vs 同 type 的 adopted 位置列表 → 最高部分信用 [0,1]。
    同 type 同 file 行距≤_POS_LINE_WINDOW → 1.0（命中同一处）；同 type 同 file 行距远 → _POS_PARTIAL_SAMEFILE；
    同 type 无 file 可比 → _POS_PARTIAL_NOFILE。部分信用权重为未经验证的初值（差距1a+4b 需真实数据校准）。"""
    ftype = finding.get("finding_type") or finding.get("rule_id")
    file = finding.get("file")
    line = finding.get("line") or finding.get("line_start")
    best = 0.0
    for p in positions:
        if ftype and (p.get("finding_type") or p.get("rule_id")) != ftype:
            continue
        pf, pl = p.get("file"), p.get("line")
        if file and pf:
            if file != pf:
                continue                       # 不同文件：此位置不算（同 type 别处可能命中）
            if line and pl:
                try:
                    best = max(best, 1.0 if abs(int(line) - int(pl)) <= _POS_LINE_WINDOW
                               else _POS_PARTIAL_SAMEFILE)
                except (TypeError, ValueError):
                    best = max(best, _POS_PARTIAL_SAMEFILE)
            else:
                best = max(best, _POS_PARTIAL_SAMEFILE)
        else:
            best = max(best, _POS_PARTIAL_NOFILE)
    return best


def _score_positional(review, positions, w_noise, w_miss):
    """位置级奖励：hits=各 review 项的最高位置信用之和；noise=review 里 type 不在 adopted 的项数；
    miss=adopted 里无任何 review 项同 type 的项数。"""
    adopted_by_type = {}
    for p in positions or []:
        ft = p.get("finding_type") or p.get("rule_id")
        if ft:
            adopted_by_type.setdefault(ft, []).append(p)
    adopted_types = set(adopted_by_type)
    hits, seen_types, noise = 0.0, set(), 0
    for f in review or []:
        ft = f.get("finding_type") or f.get("rule_id")
        if not ft:
            continue
        seen_types.add(ft)
        if ft in adopted_by_type:
            hits += _position_credit(f, adopted_by_type[ft])
        else:
            noise += 1
    miss = len(adopted_types - seen_types)
    return hits - w_noise * noise - w_miss * miss


def _extract_json(text, default):
    """从 LLM 文本里抽取 JSON（容忍 ```json``` 包裹与前后说明）；失败返回 default。"""
    if not text:
        return default
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = raw.find(opener), raw.rfind(closer)
        if 0 <= i < j:
            try:
                return json.loads(raw[i:j + 1])
            except json.JSONDecodeError:
                pass    # 静默豁免：候选切片解析失败 → 继续尝试下一策略，
                        # 回退链走完仍失败时由调用方统一报错。
    return default


def _llm_json(llm, messages, default):
    """调用注入的 llm(messages)->str 并抽 JSON；任何失败都回退 default（鲁棒，离线可注入假 llm）。"""
    try:
        return _extract_json(llm(messages), default)
    except Exception as e:
        # 回退 default 是刻意设计（离线可注入假 llm），但静默会让"LLM 全程没调通"不可见——留痕
        print(f"[learning_loop] LLM 调用失败，回退默认值: {e}", file=sys.stderr)
        return default


def rollout_reviews(pr, experience_text, llm, group_size=TFGRPO_GROUP_SIZE, *, max_workers=None):
    """① 在当前经验库 E（experience_text）下，让冻结旗舰模型对一个历史 PR 生成 group_size 份评审。
    每份是发现列表 [{finding_type, file?, note?}]。llm(messages)->str 由调用方注入
    （生产=参数冻结的旗舰模型端点；测试=确定性假 llm）；变体序号入提示以促组内多样性。
    max_workers>1：G 份变体并行（I/O-bound、互独立、顺序由 ex.map 保留 → 与串行结果一致、更省墙钟）；
    默认 None=串行（确定性、字节级不变）。"""
    sys_p = ("You are a senior code reviewer. Given a PR and the repo's learned review experience, "
             "list the review findings you would raise. Respond ONLY as a JSON array of objects "
             '{"finding_type": "PRA-...", "file": "...", "note": "..."}.')
    user_tmpl = ("# Repo experience (advisory)\n{exp}\n\n"
                 "# PR\nid={pid} repo={repo} stack={stack}\n{summary}\n\n# Diff\n{diff}\n\n"
                 "(variant {variant}: explore a distinct angle)")

    def _one(variant):
        user = user_tmpl.format(exp=experience_text or "(none)", pid=pr.get("pr_id"),
                                repo=pr.get("repo"), stack=pr.get("stack"),
                                summary=pr.get("summary", ""), diff=pr.get("diff", ""),
                                variant=variant)
        rv = _llm_json(llm, [{"role": "system", "content": sys_p},
                             {"role": "user", "content": user}], default=[])
        return rv if isinstance(rv, list) else []

    variants = list(range(group_size))
    if max_workers and max_workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            raw = list(ex.map(_one, variants))     # ex.map 保序 → 与串行结果一致
    else:
        raw = [_one(v) for v in variants]
    return raw


# --- 差距2b：结构化经验模板 + 注入式过滤（opt-in 默认关）-------------------------
# 设计文档 §3.2：TOUCHSTONE_EXP_INJECTION_FILTER 开 → distill_semantic_advantage 改要求 LLM
# 输出 {finding_type, kind, condition, action}，condition+action 渲染成 "When <c>, <a> (PRA-X)"
# 存进 text；condition/action 含 prompt 注入/越权模式 → 丢弃 + stderr（复用 review_provider
# EXPERIENCE_REF 防投毒纪律）。关（默认）→ 仅自由 text（reward 路径字节级不变）。
# 注入式/越权指令模式（词边界正则：避免 "react as"/"filesystem:" 这类合法文本误伤）。
_EXP_INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\b[\s\S]{0,20}\b(?:previous|above)\b"   # 指令覆盖类
    r"|\b(?:system|new\s+instructions)\s*:"                                 # 角色/分段（\b 不误伤 filesystem:）
    r"|\b(?:you\s+are\s+now|approve\s+all|act\s+as|override\s+previous)\b",  # 直接越权（\b 不误伤 react as）
    re.IGNORECASE)
_EXP_MAX_TEXT_LEN = _env_num(int, "TOUCHSTONE_EXP_MAX_TEXT_LEN", 240)   # #138 review：防 malformed 值导入期崩（同 #132 _POS_* 套路）


def _exp_injection_filter_enabled():
    """差距2b 总开关（默认关）：开 → 结构化 {condition,action} 模板 + 注入式过滤。"""
    return os.environ.get("TOUCHSTONE_EXP_INJECTION_FILTER", "").lower() in ("1", "true", "yes", "on")


def _looks_injected(*texts):
    """任一段文本命中 prompt 注入/越权指令模式 → 真（词边界，避免误伤 'react as'/'filesystem:'）。"""
    return bool(_EXP_INJECTION_RE.search(" ".join((t or "") for t in texts)))


def _render_structured_text(condition, action, ftype):
    """结构化 condition+action → 注入用经验文本（设计文档 §3.2 口径）。"""
    return f"When {condition.strip()}, {action.strip()} ({ftype})"


def distill_semantic_advantage(pr, group, llm, repo="", stack=""):
    """③ 组内相对语义优势：把一组带分数的评审交旗舰模型内省——高分挑对了什么、低分挑偏/漏了什么——
    按 仓·栈·发现类型 提炼候选经验。返回与 distill_candidates 同 schema 的 Experience(candidate)；
    只保留 PR-Agent 源类型（确定性 contract 类型永不进经验，坑 2b）。

    差距2b（opt-in，TOUCHSTONE_EXP_INJECTION_FILTER 默认关）：开 → prompt 改要求 LLM 输出
    {finding_type, kind, condition, action}，text 由 condition+action 渲染（"When <c>, <a> (PRA-X)"），
    并校验非空 + 祈使味 + 拒注入式；关 → 仅自由 text（行为不变）。"""
    rewards = group["rewards"]
    if len(rewards) < 2 or len({round(r, 6) for r in rewards}) < 2:
        return []                                  # 退化组：组内奖励无差异，对比无意义（I4）
    # strict=True：outputs 与 rewards 同长是 rollout 构造不变式，违反应显式暴露而非静默截断
    ranked = sorted(zip(group["outputs"], rewards, strict=True), key=lambda x: -x[1])
    payload = {"pr_id": pr.get("pr_id"),
               "reviews_by_reward": [{"reward": round(rw, 2), "review": rv} for rv, rw in ranked]}
    structured = _exp_injection_filter_enabled()                 # 差距2b 总开关
    if structured:
        sys_p = ("Compare the higher-reward reviews against the lower-reward ones for this PR and "
                 "distill repo-specific review experience: which finding_type to EMPHASIZE (humans "
                 "act on) and which to SUPPRESS (humans dismiss). Respond ONLY as a JSON array of "
                 '{"finding_type": "PRA-...", "kind": "emphasize|suppress", '
                 '"condition": "<when this happens>", "action": "<one imperative sentence>"}.')
    else:
        sys_p = ("Compare the higher-reward reviews against the lower-reward ones for this PR and "
                 "distill repo-specific review experience: which finding_type to EMPHASIZE (humans "
                 "act on) and which to SUPPRESS (humans dismiss). Respond ONLY as a JSON array of "
                 '{"finding_type": "PRA-...", "kind": "emphasize|suppress", "text": "<one imperative sentence>"}.')
    user = f"# PR\n{pr.get('summary', '')}\n\n# Group\n{json.dumps(payload, ensure_ascii=False)}"
    items = _llm_json(llm, [{"role": "system", "content": sys_p},
                            {"role": "user", "content": user}], default=[])
    now, out = int(time.time()), []
    protected = _protected_types()
    for it in items if isinstance(items, list) else []:
        ftype = (it or {}).get("finding_type", "")
        kind = (it or {}).get("kind")
        if not ftype or kind not in ("emphasize", "suppress"):
            continue
        if not _is_review_type(ftype):
            continue                          # 确定性类型不进经验（固定基准，坑 2b）
        if kind == "suppress" and ftype in protected:
            continue                          # 红线：受保护类型永不 suppress
        if structured:                        # 差距2b：结构化 + 校验 + 注入过滤
            condition = (it.get("condition") or "").strip()
            action = (it.get("action") or "").strip()
            if not condition or not action or "?" in action:    # 非空 + 祈使味（问句非祈使）
                continue
            if _looks_injected(condition, action):              # 注入式/越权 → 丢弃 + stderr
                print(f"[learn] 差距2b：丢弃疑似注入式经验文本（ftype={ftype}, kind={kind}）",
                      file=sys.stderr)
                continue
            # 渲染前按字段截断（#131 review #1）：防超长 condition/action 把 "When …, … (ftype)"
            # 模板从中间切断；原渲染后整体截断会切掉 (ftype) 尾或 action 半句。两字段均分预算扣模板开销。
            _field_max = max(16, (_EXP_MAX_TEXT_LEN - len(ftype) - 10) // 2)
            condition = condition[:_field_max]
            action = action[:_field_max]
            text = _render_structured_text(condition, action, ftype)
            if len(text) > _EXP_MAX_TEXT_LEN:
                text = text[:_EXP_MAX_TEXT_LEN]                 # 兜底（pre-truncate 后通常不再触发）
        else:
            text = (it.get("text") or "").strip()
            if not text:
                continue
        out.append({"id": _exp_id(ftype, kind, repo, stack), "repo": repo, "stack": stack,
                    "finding_type": ftype, "kind": kind, "text": text,
                    "evidence": {"tfgrpo": True, "group_rewards": [round(x, 2) for x in rewards],
                                 "pr": pr.get("pr_id")},
                    "status": "candidate", "source": "tfgrpo", "locked": False,
                    "source_prs": [pr.get("pr_id")] if pr.get("pr_id") else [],
                    "created_at": now, "updated_at": now})
    return out


def _flagship_llm():
    """默认旗舰模型调用器 llm(messages)->str（openai SDK，参数冻结）。仅真实运行时构造；
    缺 env / 缺 openai 时清晰报错。测试一律注入假 llm，不走此处。"""
    base_url = os.environ.get("LLM_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("TOUCHSTONE_FLAGSHIP_MODEL") or os.environ.get("LLM_MODEL")
    if not (base_url and api_key and model):
        raise RuntimeError("TF-GRPO 需要旗舰模型端点：设置 LLM_BASE_URL / LLM_API_KEY / TOUCHSTONE_FLAGSHIP_MODEL")
    import openai
    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=120)

    def _call(messages):
        resp = client.chat.completions.create(model=model, messages=messages, temperature=0.7)
        return resp.choices[0].message.content or ""
    return _call


# --- c3：rollout 缓存 + 预算 + 并发（docs/tfgrpo-productionization-design.html 差距 4a/3a）---------
#   缓存：策略冻结，输入（pr_id + 经验库 E + 旗舰模型 + G）相同 → rollout 意图可复现 → 命中即复用，
#         砍掉"每周 cron 全量重跑"对未变 PR 的重复采样。跨 epoch：E 变了 → key 变 → 自然 miss（正确）。
#   预算：max_llm_calls 限单次 run 的旗舰调用量，超限跳过剩余 PR（不静默——skipped_prs 留痕）。
#   三者默认全关（cache=None / max_llm_calls=None / max_workers=None）→ 字节级零行为变化。
def _rollout_cache_key(pr, experience_text, group_size, *, rollout_tag="default"):
    """稳定键：同 PR + 同经验库 E + 同组大小 + 同旗舰模型 + 同 PR 内容(summary/diff) + 同 rollout 实现 → 复用 rollout。
    summary/diff 入键：PR 标题改 / GT_DIFF_BUDGET 变更截断 → key 变 → 不返回 stale rollout。
    rollout_tag 入键：换 rollout 实现（自定义 distiller）→ tag 变 → 不复用旧实现产的结果（默认 "default"）。"""
    model = os.environ.get("TOUCHSTONE_FLAGSHIP_MODEL") or os.environ.get("LLM_MODEL") or ""
    h = hashlib.sha256()
    h.update(str(pr.get("pr_id", "")).encode("utf-8"))
    h.update(b"\x1f")
    h.update((experience_text or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update(str(group_size).encode("utf-8"))
    h.update(b"\x1f")
    h.update(model.encode("utf-8"))
    h.update(b"\x1f")
    h.update((pr.get("summary") or "").encode("utf-8"))   # PR 内容入键（防 stale）
    h.update(b"\x1f")
    h.update((pr.get("diff") or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update((rollout_tag or "default").encode("utf-8"))  # rollout 实现身份入键（防跨实现 stale）
    return h.hexdigest()


def _load_cache(cache):
    """cache 入参归一：None→None（不缓存）；dict→原样用；str 路径→加载 JSON dict（缺/坏→空 dict）。"""
    if cache is None or isinstance(cache, dict):
        return cache
    if isinstance(cache, str):
        try:
            with open(cache, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}
    return None


def _save_cache(cache_obj, cache_arg):
    """仅当 cache_arg 是路径时落盘（dict 入参由调用方持有、不写回）。失败留痕不阻塞（防静默约定）。
    唯一临时文件（mkstemp）：并发/重入同路径不撞 `.tmp`；TypeError 也 catch——json.dump 遇不可序列化值
    不再击穿"失败留痕不阻塞"契约。"""
    if not isinstance(cache_arg, str) or not isinstance(cache_obj, dict):
        return
    tmp = None
    try:
        d = os.path.dirname(cache_arg) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(cache_arg) + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache_obj, f, ensure_ascii=False)
        os.replace(tmp, cache_arg)            # 原子替换：崩溃不留半文件（同 atomicio 纪律）
        tmp = None                            # 已 replace，勿清理
    except (OSError, TypeError) as e:
        print(f"[distill] rollout 缓存写盘失败（不阻塞）: {e}", file=sys.stderr)
    finally:
        if tmp and os.path.exists(tmp):       # 异常时清理残留临时文件
            try: os.unlink(tmp)
            except OSError: pass


class _Budget:
    """LLM 调用预算计数器。max_calls=None → 不限（默认）。has(n)=还能不能再花 n；use(n)=记账。"""

    def __init__(self, max_calls):
        self.max = max_calls
        self.used = 0
        self.skipped_prs = []

    def has(self, n):
        return self.max is None or self.used + n <= self.max

    def use(self, n):
        self.used += n

    @property
    def exhausted(self):
        return self.max is not None and self.used >= self.max


def _distill_via_llm(ground_truth, store, llm=None, *, group_size=TFGRPO_GROUP_SIZE,
                     epochs=1, repo="", stack="",
                     rollout=None, score=None, distill_advantage=None,
                     cache=None, max_llm_calls=None, max_workers=None, skip_types=None,
                     min_source_prs=None, max_reward_var=None):
    """TF-GRPO 入口（实现）。机制设计见 docs/learning-loop-design.html §3。
    ground_truth: 最小真值集 [{pr_id, repo, stack, summary, diff, human_adopted:[finding_type]}]
                  —— 历史已合 PR + 人审裁决（生产由 calibrate 从 GitHub 重建）。
    store: 当前经验库（用其 active 经验 render 成 E 来 condition rollout）。
    llm:   注入的 llm(messages)->str；缺省用 _flagship_llm()（参数冻结的旗舰模型端点）。
    多轮(epochs)对每个 PR：① rollout G 份评审 → ② 按人审真值离线打分 → ③ 旗舰模型内省出组内语义优势
    → 候选经验。策略全程冻结、无梯度无权重。返回候选经验（caller 再 merge_candidates → graduate 门控）。
    可注入替换其中任一步（默认用内置）：
      rollout(pr, E_text, llm, group_size) -> [review]
      score(review, human_adopted) -> float
      distill_advantage(pr, group, llm, repo, stack) -> [Experience(candidate)]
    c3 成本控制（默认全关 = 零行为变化）：
      cache(dict|路径)：rollout 结果按 (pr_id+E+模型+G+summary/diff+rollout_tag) 缓存复用，砍重复采样；
                        中途失败也落盘（try/finally）；唯一临时文件（mkstemp）防并发撞名。
      max_llm_calls(int)：单次 run 旗舰调用预算（rollout 生成 + distill 内省各计入），超限跳过剩余 PR
                         （不静默，记 budget.skipped_prs）；
      max_workers(int)：仅对内置 rollout_reviews 生效（注入自定义 rollout 时自行管理并发）。"""
    llm = llm or _flagship_llm()
    rollout_is_default = rollout is None
    rollout = rollout or rollout_reviews
    rollout_tag = ("default" if rollout_is_default
                   else f"{rollout.__module__}:{rollout.__qualname__}")  # 缓存 key 的实现身份
    score = score or score_review
    distill_advantage = distill_advantage or distill_semantic_advantage
    base_active = [e for e in (store or {}).get("experiences", []) if e.get("status") == "active"]
    cache_obj = _load_cache(cache)
    budget = _Budget(max_llm_calls)
    acc = {}
    # 差距2a：跨 PR 一致性——按 candidate id 记每个【unique pr_id】的组均 reward（瞬态，不进 store）。
    # 用 {pr_id: reward} 去重：同一 PR 多 epoch 的 rollout 只算一个跨 PR 样本（design 要"跨 PR 一致"，
    # 非"跨 epoch 一致"）。下方 return 前据此过滤运气型 outlier。
    reward_hist = {}
    try:                                    # 评审 item 3：循环中途失败也落盘已采缓存（finally）
        for _ in range(max(1, epochs)):
            # 每轮用「已有 active + 本轮已蒸出候选」重渲染 E，下一轮在更新后的 E 上 rollout（I2）
            cond = {"experiences": base_active + [dict(c, status="active") for c in acc.values()]}
            experience_text = render_injection(cond)
            for pr in ground_truth or []:
                # 差距3a 收敛跳过：本 PR 所有 raised_types 都已 stable → 跳过 rollout+内省（省 G+1 次旗舰
                # 调用）。混合 type（部分未收敛）仍照常处理——不能因一个稳定 type 牺牲同 PR 其他 type 信号。
                pr_types = {t for t in (pr.get("raised_types") or []) if t}
                if skip_types and pr_types and pr_types <= skip_types:
                    continue
                key = (_rollout_cache_key(pr, experience_text, group_size, rollout_tag=rollout_tag)
                       if cache_obj is not None else None)
                if cache_obj is not None and key in cache_obj:
                    reviews = cache_obj[key]                       # 缓存命中：复用，不重复采样
                else:
                    if not budget.has(group_size):
                        budget.skipped_prs.append(pr.get("pr_id"))   # 预算耗尽：跳过（不静默）
                        continue
                    if rollout_is_default and max_workers:
                        reviews = rollout(pr, experience_text, llm, group_size, max_workers=max_workers)
                    else:
                        reviews = rollout(pr, experience_text, llm, group_size)
                    budget.use(group_size)
                    if cache_obj is not None:
                        cache_obj[key] = reviews
                # 盲区2：reward 乘真值条目的 trust_weight（坏真值检测给的 0–1 权重）。GT 条目无该字段
                # （Step1 前 / TOUCHSTONE_TRUTH_QUALITY 默认关）→ 默认 1.0，reward 字节级不变。
                # 组内每条 review 共享同 PR 的 weight → 相对优势仅被等比缩放、符号不变；坏真值条目的
                # reward magnitude 向 0 收缩，抑制其蒸出的经验。weight=0 的条目已在 build_ground_truth 硬剔除。
                # 防御外部 JSON 异常（pr-agent review #121）：显式 null（key 在、值 None）会 TypeError 崩整批
                # → coalesce None→1.0；越界值（负/>1）会翻转符号或放大 reward，破坏"只缩不放、符号不变"契约
                # → clamp [0,1]。GT 由本仓 make_gt_entry 产时恒为合法 [0,1] float，此仅兜底手改/外部 JSON。
                weight = pr.get("trust_weight", 1.0)
                weight = 1.0 if weight is None else min(1.0, max(0.0, weight))
                # 差距1a（opt-in）：开 TOUCHSTONE_POSITIONAL_REWARD 且本 PR 有位置信号 → 喂位置给 score_review
                # 走部分信用；否则类型集合（既有口径）。位置缺/开关关 → 回落类型集（字节级不变）。
                human_signal = (pr.get("human_adopted_positions")
                                if (_positional_reward_enabled() and pr.get("human_adopted_positions"))
                                else pr.get("human_adopted"))
                rewards = [score(o, human_signal) * weight for o in reviews]
                group = {"outputs": reviews, "rewards": rewards}
                # distill 内省也耗 1 次旗舰调用——计入预算；否则缓存命中时 rollout 0 预算却仍逐 PR
                # 触发 distill，预算失真（可显示 0 用量而数百次调用）。预算耗尽则跳过内省。
                if budget.has(1):
                    budget.use(1)
                    distilled = distill_advantage(pr, group, llm,
                                                  pr.get("repo", repo), pr.get("stack", stack))
                else:
                    distilled = []
                for c in distilled:
                    prev = acc.get(c["id"])
                    if prev:
                        prev["source_prs"] = sorted(set(prev["source_prs"]) | set(c["source_prs"]))
                        prev["updated_at"] = c["updated_at"]
                    else:
                        acc[c["id"]] = c
                    # 差距2a：记录本 PR 对该 candidate 的组均 reward（按 pr_id 去重）
                    if rewards:
                        pid = pr.get("pr_id")
                        # PRA round-5：pr_id 缺失时 str(None)="None" 会把多个无 id 的 PR 奖励
                        # 合并到同一 key，污染方差。跳过缺失 pr_id 的记录（不污染 reward_hist）。
                        if pid is not None:
                            rh = reward_hist.setdefault(c["id"], {})
                            rh[str(pid)] = round(sum(rewards) / len(rewards), 4)
            if budget.exhausted:
                break                              # 预算耗尽：不再开下一 epoch
    finally:
        if budget.skipped_prs:
            print(f"[distill] LLM 预算耗尽，跳过 {len(budget.skipped_prs)} 个 PR："
                  f"{budget.skipped_prs}（调 TOUCHSTONE_ROLLOUT_BUDGET / 增量水位）", file=sys.stderr)
        _save_cache(cache_obj, cache)              # 中途失败也落盘（评审 item 3）
    # 差距2a 跨 PR 一致性过滤（默认 min_source_prs=1、max_var=None → 不过滤=现状）：
    #   ① source_prs 数 < min_source_prs → 丢（仅 1 PR 的运气型 outlier）。
    #   ② 跨 PR reward 方差 > max_reward_var → 丢（跨 PR 不一致：某 type 仅个别 PR 高 reward）。
    return _filter_by_consistency(acc, reward_hist, min_source_prs, max_reward_var)


def _filter_by_consistency(acc, reward_hist, min_source_prs, max_reward_var):
    """差距2a：按跨 PR 一致性过滤蒸馏候选。纯函数。
    min_source_prs<=1 且 max_reward_var is None → 不过滤（默认=零行为变化）。"""
    # 两闸 None 均回退各自 env reader（对称）：PRA round-4 distill.py:595——旧 min_source_prs=None
    # 回退常量 DEFAULT（忽略 env），而 max_reward_var=None 回退 env reader，不对称致直接调用
    # _distill_via_llm(min_source_prs=None) 时 env 覆盖被静默忽略。生产 _tfgrpo_distiller 已显式
    # 解析两 env 传入不受影响；env 未设时 _distill_min_source_prs() 返回 DEFAULT(1)，零行为变化。
    min_sp = _distill_min_source_prs() if min_source_prs is None else min_source_prs
    max_var = max_reward_var if max_reward_var is not None else _distill_max_reward_var()
    if min_sp <= 1 and max_var is None:
        return list(acc.values())             # 默认关：不过滤
    kept, dropped = [], []
    for cid, c in acc.items():
        rh = reward_hist.get(cid, {})
        n_prs = len(rh) or len(c.get("source_prs") or [])   # rh 优先（按 pr_id 去重）；fallback source_prs
        if min_sp > 1 and n_prs < min_sp:
            dropped.append((cid, f"<{min_sp} PRs ({n_prs})")); continue
        # PRA round-1/2（distill.py:583/594 "单 PR 绕过方差"）：单 PR 候选 pvariance≡0（单点
        # 无方差），自然 ≤ max_var 必留——不是"绕过"，是方差对单点无信息量。其"证据是否充足"
        # 由上面 min_source_prs 闸管（正交职责：min_sp=样本量门槛、max_var=既有样本一致性门槛）。
        # 两闸可组合覆盖全部意图（无缺失功能）：
        #   min_sp=1 + var=None  → 不过滤（默认）
        #   min_sp=1 + var=0.1   → 留单 PR，多 PR 按方差过滤
        #   min_sp=2 + var=None  → 丢单 PR（样本量门槛）
        #   min_sp=2 + var=0.1   → 丢单 PR + 多 PR 按方差过滤
        # 评审所谓"variance-only 且丢单 PR"= 第 4 行（min_sp=2），可达。
        # PRA round-5：方差闸要求既有 reward 数据——rh 空时 _pvariance([])=0.0 恒 ≤ max_var 会
        # 静默放行无证据候选（rewards 未录 / score 返空 / pr_id 缺失被跳过）。fail-closed：
        # max_var 启用且 rh 空时丢弃（无可校验一致性的数据）。默认 max_var=None 不入此支，零行为变化。
        if max_var is not None and not rh:
            dropped.append((cid, "no reward history for variance check")); continue
        if max_var is not None and _pvariance(list(rh.values())) > max_var:
            dropped.append((cid, f"reward_var>{max_var}")); continue
        kept.append(c)
    if dropped:
        print(f"[distill] 差距2a 一致性过滤：丢弃 {len(dropped)} 条运气/不一致 candidate："
              f"{dropped}（调 TOUCHSTONE_DISTILL_MIN_SOURCE_PRS / _MAX_REWARD_VAR）", file=sys.stderr)
    return kept


# --- 蒸馏器分发：按名选实现 + 注册自定义（照搬 review_provider 的分发风格）---------------
#   蒸馏上下文 ctx（统一入参，各实现按需取用）：{calib_agg, ground_truth, store, llm, repo, stack}
def _counting_distiller(ctx):
    return distill_candidates(ctx.get("calib_agg") or {}, ctx.get("repo", ""), ctx.get("stack", ""),
                             skip_types=ctx.get("skip_types"))


def _env_rollout_cache():
    """TOUCHSTONE_ROLLOUT_CACHE：未设=None（不缓存）；'memory'=进程内 dict（每次 run 新建）；
    其它字符串=文件缓存路径（跨 run 持久，cron 间复用）。生产路径经此把 c3 缓存接进 run。"""
    v = os.environ.get("TOUCHSTONE_ROLLOUT_CACHE", "").strip()
    if not v:
        return None
    return {} if v.lower() == "memory" else v


def _env_int_opt(name):
    """env→正整数 or None（未设/非正→None=不限）。"""
    v = os.environ.get(name, "").strip()
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


def _tfgrpo_distiller(ctx):
    # c3 成本控制在生产 run-path 接通：ctx 显式传入优先，否则读 env（learn.yml 设 env 即生效）。
    cache = ctx.get("cache", _env_rollout_cache())
    budget = ctx.get("max_llm_calls", _env_int_opt("TOUCHSTONE_ROLLOUT_BUDGET"))
    workers = ctx.get("max_workers", _env_int_opt("TOUCHSTONE_ROLLOUT_WORKERS"))
    # 差距2a 跨 PR 一致性：ctx 显式传入优先，否则读 env（默认 min=1/var=None=不过滤=现状）。
    min_sp = ctx.get("min_source_prs", _distill_min_source_prs())
    max_var = ctx.get("max_reward_var", _distill_max_reward_var())
    return _distill_via_llm(ctx.get("ground_truth") or [], ctx.get("store") or {"experiences": []},
                            ctx.get("llm"), repo=ctx.get("repo", ""), stack=ctx.get("stack", ""),
                            cache=cache, max_llm_calls=budget, max_workers=workers,
                            skip_types=ctx.get("skip_types"),
                            min_source_prs=min_sp, max_reward_var=max_var)


_DISTILLERS = {"counting": _counting_distiller, "tfgrpo": _tfgrpo_distiller}


def register_distiller(name, fn):
    """注册自定义蒸馏器 fn(ctx)->[Experience]。外部 `import learning_loop` 后调用即可，不必改本文件；
    随后用 env TOUCHSTONE_DISTILLER=name 或 distill(ctx, name) 选用。"""
    _DISTILLERS[name] = fn


def distill(ctx, name=None):
    """按名分发到蒸馏器，返回候选经验（与 distill_candidates 同 schema，交 merge_candidates → graduate）。
    name 缺省取 env TOUCHSTONE_DISTILLER；再缺省：有真值集→tfgrpo，否则 counting。
    内置 counting / tfgrpo；自定义实现经 register_distiller 注册后即可按名选用。"""
    name = name or os.environ.get("TOUCHSTONE_DISTILLER") or ("tfgrpo" if ctx.get("ground_truth") else "counting")
    fn = _DISTILLERS.get(name)
    if not fn:
        raise ValueError(f"未知蒸馏器: {name!r}（已注册: {sorted(_DISTILLERS)}）")
    return fn(ctx)


def _flagship_configured():
    """旗舰模型端点是否就绪（TF-GRPO 生成/内省用）。缺则自动回退计数式蒸馏。"""
    return bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY")
                and (os.environ.get("TOUCHSTONE_FLAGSHIP_MODEL") or os.environ.get("LLM_MODEL")))


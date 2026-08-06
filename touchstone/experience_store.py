#!/usr/bin/env python3
# ============================================================================
# touchstone/experience_store.py —— 经验库（存取 + 生命周期 + 注入渲染）
# ----------------------------------------------------------------------------
# 从 learning_loop 拆出（模块职责单一化，第三轮工程化加固）。本模块只管经验的
# 【状态】：JSON 存取（含从受信 ref 读取防投毒）、seed/merge 入池、
# graduate（shadow A/B 达标 candidate→active）、retire/disable（前提不再成立即退役）、
# render_injection（active 经验 → PR-Agent extra_instructions）。
# 经验怎么【产生】在 distill.py；学习信号从哪【来】在 ground_truth.py；
# learning_loop.py 保留 CLI/main 编排并再导出全部名字（既有引用路径兼容）。
# 铁律不变：经验只调"建议"、绝不进"合入闸"；确定性 contract 类型永不进经验库。
# ============================================================================

import hashlib
import json
import os
import time

from touchstone.atomicio import atomic_write_json

# --- 阈值（保守：宁可慢些演进，不轻易注入/退役）---------------------------------
SUPPRESS_ADOPT_MAX  = 0.20   # 采纳率低于此 → "别挑"（suppress）；蒸馏入池与退役镜像判据共用
EMPHASIZE_ADOPT_MIN = 0.80   # 采纳率高于此 → "该挑"（emphasize）；蒸馏入池与退役镜像判据共用
GRADUATE_MIN_SAMPLES = 20     # shadow A/B 两臂各需的样本下限
GRADUATE_MIN_LIFT   = 0.10   # 注入臂采纳率 - 不注入臂 ≥ 此 → 候选达标转 active
RETIRE_ADOPT_MAX    = 0.15   # active 经验对应类型采纳率跌破此（且复发）→ 退役（govern 式）
RETIRE_MIN_FIRES    = 8      # 退役判据的样本下限（与 distill.DISTILL_MIN_FIRES 同值同理：
                             # 样本不足不轻举妄动——蒸馏侧不入池，退役侧不退役）

# --- shadow 注入（冷启动破死锁：candidate 先 shadow 注入采集 A/B with 臂；env 默认全关）---------
# 详见 docs/tfgrpo-self-evolution-design.html §2。本组 env 默认值=现状不变：render_injection
# 默认 include_shadow=False、shadow_candidates 的 ratio/max/min_evidence 有保守默认。
SHADOW_INJECTION_DEFAULT      = False # shadow 注入总开关（默认关=字节级零行为变化；开需配 EXPERIENCE_REF）
SHADOW_RATIO_DEFAULT          = 0.5   # candidate 被选中 shadow 注入的长期比例（0-1，基于 id 稳定哈希）
SHADOW_MAX_PER_REVIEW_DEFAULT = 3     # 单轮评审最多注入多少条 shadow candidate（限制爆炸面）
SHADOW_MIN_EVIDENCE_DEFAULT   = 1     # candidate 至少 N 条 source_prs 才入选（初筛防孤证）

# --- bootstrap seed（冷启动破死锁辅助路径 c：高采纳 type 直接 seed active 撑 with 臂；env 默认关）---
# 与 shadow 注入(a)互补：(a) 让 candidate 采 with 臂数据逐步 graduate；(c) 让【全新 type】立即有
# 首个 active（进 active_types → with 臂非空），加速冷启动。门槛高于蒸馏入池（MIN_FIRES 15>8、
# MIN_ADOPT 0.85>0.80）抑制冷启动期小样本偶然高采纳。只产 emphasize（高采纳=该挑）、locked=False
# （让 retire 能管，与人手 seed 的 locked=True 区分）、source="bootstrap"。
BOOTSTRAP_SEED_DEFAULT       = False  # bootstrap 总开关（默认关=零行为变化；开需 calib_agg 有数据）
BOOTSTRAP_MIN_FIRES_DEFAULT  = 15      # type 至少 N fires 才 bootstrap（高于 DISTILL_MIN_FIRES=8）
BOOTSTRAP_MIN_ADOPT_DEFAULT  = 0.85    # type 采纳率 ≥ 此才 bootstrap（高于 EMPHASIZE_ADOPT_MIN=0.80）

STORE_PATH = (os.environ.get("TOUCHSTONE_STORE_PATH")
             or os.environ.get("TOUCHSTONE_EXPERIENCE") or ".touchstone/experience.json")

# --- 经验库（JSON 产物，非服务）-------------------------------------------------
# experience: {id, repo, stack, finding_type, kind(suppress/emphasize),
#              text, evidence{fires,adoption,shadow_fires?,graduated_via?},
#              status(candidate/shadow/active/retired),
#              source(human/tfgrpo/counting/bootstrap?), locked(bool: 人锁定→回路不得改写/退役),
#              source_prs[], created_at, updated_at}
#   status=shadow：采 A/B with 臂数据的【注入角色】，由 shadow_candidates 从 candidate 池按 id 稳定
#   哈希+ratio 临时选中（非持久状态——candidate 经 shadow 采数达标后仍 candidate→active，
#   graduate 零改动）；当前作语义占位、无写入路径。evidence.shadow_fires（采数 PR 数）/
#   graduated_via（"ab"|"bootstrap"）为采数/达标流程的回写字段（向后兼容，缺失即默认）。
def _read_store_text(path):
    ref = os.environ.get("TOUCHSTONE_EXPERIENCE_REF")
    if ref:
        import subprocess
        r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_store(path=None):
    path = path or STORE_PATH
    try:
        text = _read_store_text(path)
        if not text:
            return {"experiences": []}
        store = json.loads(text)
        # 防静默故障（A3-F3）：经验库唯一合法顶层结构是 dict 且 experiences 为 list。存档若是合法
        # JSON 但形状不对（顶层 list/标量，或 experiences 非 list——旧格式/损坏/手改），json.loads 照样
        # 成功并原样返回，下游 render_injection / seed_experience 的 store.get(...) 与迭代会
        # AttributeError/TypeError 崩整个学习回路注入。在唯一加载边界 fail-safe：形状不对即视为损坏、
        # 回落安全默认，不抛、不崩、不把坏数据静默传下去。
        if not isinstance(store, dict) or not isinstance(store.get("experiences"), list):
            return {"experiences": []}
        return store
    except (OSError, json.JSONDecodeError):
        return {"experiences": []}


def save_store(store, path=None):
    path = path or STORE_PATH
    atomic_write_json(path, store)        # 原子：喂经验回路的库不留半文件
    return store


def _is_review_type(finding_type):
    """只有 PR-Agent 源的发现类型才进经验库；确定性 contract_check（SCOPE/TEST/DUP/CTR…）是固定基准，永不进。"""
    return finding_type.startswith("PRA-") or finding_type.startswith("pr-agent")


def _exp_id(finding_type, kind, repo="", stack=""):
    # 经验唯一键含 仓·栈：多仓部署下 A 仓与 B 仓的同类型经验不互相覆盖（I1）
    return f"{kind}:{repo}:{stack}:{finding_type}"


def _protected_types():
    """人立的红线：这些 finding_type 永不许被学习回路 suppress（哪怕历史上人总忽略）。
    来自 env TOUCHSTONE_PROTECTED_TYPES（逗号分隔），如 PRA-SECURITY,PRA-POSSIBLE_BUG。"""
    return {t.strip() for t in os.environ.get("TOUCHSTONE_PROTECTED_TYPES", "").split(",") if t.strip()}


# --- 分类法白名单（c1：防 LLM 幻觉 finding_type 污染经验库；env 默认关 = 零行为变化）---
# 详见 docs/tfgrpo-productionization-design.html 差距 1b。rollout_reviews / distill_semantic_advantage
# 让旗舰模型自由产 "finding_type":"PRA-..."，唯一过滤是 _is_review_type（只查前缀）——LLM 可幻觉出
# PRA-FAKE 之类进经验库、再以 [shadow] 注入污染后续 rollout 的 E。白名单在【入池唯一闸】merge_candidates
# 兜底：未知类型经软映射后仍不中 → 丢弃 + 留痕（fail-closed，不静默）。
TAXONOMY_ENFORCE_DEFAULT = False


def _normalize_type(ftype):
    """finding_type 归一化用于软匹配：大写、分隔符统一为 '-'。
    'PRA-Spring_Tx' / 'pra spring tx' / 'PRA_SPRING_TX' → 'PRA-SPRING-TX'。纯函数。"""
    return (str(ftype or "").upper()
            .replace("_", "-").replace(" ", "-").replace("/", "-")).strip("-")


def coerce_type(ftype, known):
    """把 LLM 产出的 finding_type 校验/软映射到白名单已知类型。
    known=None → 白名单未配置，照原样返回（不校验）；非 None 时：精确命中 → 原值；
    归一化后命中 → 白名单里的规范形（修大小写/分隔符漂移）；都不中 → None（未知，调用方丢弃）。纯函数。"""
    if known is None:
        return ftype
    if ftype in known:
        return ftype
    norm = _normalize_type(ftype)
    if not norm:
        return None
    for k in known:
        if _normalize_type(k) == norm:
            return k
    return None


def _canonical_type(ftype):
    """finding_type 的【生态规范形】：'PRA-' 前缀保留 + 其后部分大写、分隔符统一为下划线。
    与 review_provider.normalize 的 rid = 'PRA-' + label.replace(' ','_').upper() 一致——保证 store 里的
    finding_type 与 calibrate by_rule 键 / aggregate_ab 键 / 注入 marker（injected_types、shadow_types）
    【同形】，使 graduate/retire/ground_truth 的按类型查找不因大小写或分隔符漂移失配。

    与 _normalize_type 的区别：_normalize_type 把【所有】分隔符（含 'PRA-' 前缀那个连字符）折成连字符，
    仅供 coerce_type 的对称软匹配用（两端同函数即可，分隔符字符无所谓）；本函数保留 'PRA-' 前缀连字符、
    只归一化其后部分，产出的是要【写进 store】的规范形。纯函数。

    分隔符折叠是【有意】的：rest 部分大小写、分隔符字符（-/_/空格/斜杠）与连续个数都折成单个下划线。
    这些差异是 LLM 产 finding_type 时的样式噪声（'PRA-A-B'/'PRA-A_B'/'PRA-A--B' 同一规律），不是 PRA
    分类法里区分语义的分隔——故折叠不损失信息。真正不同的类型不会撞：分隔符的【有无】仍被区分
    （'PRA-AB' ≠ 'PRA-A_B'），不同 token 亦然（'PRA-FOO' ≠ 'PRA-BAR'）。见
    test_canonical_type_folds_separator_variants_not_distinct_types。

    例：'PRA-consistency'/'PRA-CONSISTENCY' → 'PRA-CONSISTENCY'；
        'PRA-COVERAGE-GAP'/'PRA-COVERAGE_GAP'/'PRA-coverage gap' → 'PRA-COVERAGE_GAP'。"""
    s = str(ftype or "").strip()
    if not s:
        return ""
    prefix, sep, rest = s.partition("-")
    if sep and prefix.upper() == "PRA":
        rest_u = rest.upper().replace(" ", "_").replace("-", "_").replace("/", "_")
        rest_u = "_".join(p for p in rest_u.split("_") if p)   # 折叠连续下划线 + 去首尾
        return f"PRA-{rest_u}" if rest_u else "PRA"
    # 非 'PRA-' 形（'pr-agent-*' 等罕见，或无连字符）：保守地只大写 + 折叠空格/斜杠，不改命名空间连字符
    whole = s.upper().replace(" ", "_").replace("/", "_")
    return "_".join(p for p in whole.split("_") if p)


def _canonicalize_candidate(c):
    """把候选的 finding_type 规范化为生态形并同步重算 id（id 含 finding_type）。不丢弃——仅归一化。
    已规范或 finding_type 为空 → 原样返回。纯函数（返回新 dict，不改输入）。"""
    ft = _canonical_type(c.get("finding_type", ""))
    if ft and ft != c.get("finding_type"):
        return dict(c, finding_type=ft,
                    id=_exp_id(ft, c.get("kind", ""), c.get("repo", ""), c.get("stack", "")))
    return c


def known_types(store, extra=()):
    """经验库的有效 finding_type 白名单（taxonomy）。
    = 已 active 的类型 ∪ extra（调用方传入，如 pr-agent.yaml 的 label 集）∪ env TOUCHSTONE_TAXONOMY_TYPES。
    纯函数（不含 I/O——pr-agent.yaml 的解析由调用方做后经 extra 传入，保持本模块可离线单测）。"""
    types = set(extra or [])
    types |= {e.get("finding_type") for e in (store or {}).get("experiences", [])
              if e.get("status") == "active" and e.get("finding_type")}
    types |= {t.strip() for t in os.environ.get("TOUCHSTONE_TAXONOMY_TYPES", "").split(",") if t.strip()}
    return types


def seed_experience(store, finding_type, kind, text, *, repo="", stack="",
                    status="active", locked=True, source="human"):
    """写一条经验当种子。默认 source=human（人手写，权威：直接 active 且 locked，学习回路不得
    静默改写或退役）。source=bootstrap（自动 seed：高采纳 type 直接 active 撑 with 臂，冷启动辅助
    路径 c；locked=False 让 retire 能管，与人手 locked=True 区分）。传 locked=False 可交回路管理。
    用于冷启动、注入团队领域知识与红线。"""
    if kind not in ("emphasize", "suppress"):
        raise ValueError("kind 必须是 emphasize 或 suppress")
    if source not in ("human", "bootstrap"):
        raise ValueError("source 必须是 human 或 bootstrap")
    now = int(time.time())
    exp = {"id": _exp_id(finding_type, kind, repo, stack), "repo": repo, "stack": stack,
           "finding_type": finding_type, "kind": kind, "text": text.strip(),
           "evidence": {"seeded": True}, "status": status, "source": source,
           "locked": bool(locked), "source_prs": [], "created_at": now, "updated_at": now}
    idx = {e["id"]: e for e in store.get("experiences", [])}
    if exp["id"] in idx:
        idx[exp["id"]].update({k: exp[k] for k in ("text", "status", "source", "locked", "updated_at")})
        return idx[exp["id"]]
    store.setdefault("experiences", []).append(exp)
    return exp


def merge_candidates(store, candidates, *, taxonomy=None):
    """把候选并入经验库的 candidate 池：同 id 已存在则更新证据（不降级 active/retired 的状态）。
    taxonomy（集合，默认 None=不启用 = 零行为变化）：非空时，finding_type 不在白名单的候选经
    coerce_type 软映射；仍未知 → 丢弃并 stderr 留痕（fail-closed 防 LLM 幻觉类型污染经验库；不静默）。

    总是先做 _canonicalize_candidate（大小写/分隔符→生态规范形，合并重复变体，不丢弃任何候选）——
    即便 taxonomy 关。防 TF-GRPO 的 LLM 自由产 finding_type（'PRA-COVERAGE-GAP' vs 'PRA-COVERAGE_GAP'
    vs 'PRA-consistency'）把同一规律裂成多条、互相稀释证据。"""
    idx = {e["id"]: e for e in store.get("experiences", [])}
    dropped = []
    for c in candidates:
        c = _canonicalize_candidate(c)              # 先规范化 finding_type/id（合并重复变体，不丢弃）
        if taxonomy is not None:
            ft = coerce_type(c.get("finding_type", ""), taxonomy)
            if ft is None:
                dropped.append(c.get("finding_type"))
                continue                      # 未知类型：丢弃（fail-closed），不入池
            if ft != c.get("finding_type"):
                # 软映射修正了类型 → 同步重算 id（id 含 finding_type），否则错位成新条目
                c = dict(c, finding_type=ft,
                         id=_exp_id(ft, c.get("kind", ""), c.get("repo", ""), c.get("stack", "")))
        if c["id"] in idx:
            e = idx[c["id"]]
            if e.get("locked") or e.get("source") == "human":
                continue                      # 人锁定/手写的经验，回路不得静默改写
            e["evidence"] = c["evidence"]
            e["text"] = c["text"]
            e["updated_at"] = c["updated_at"]
        else:
            store.setdefault("experiences", []).append(c)
            idx[c["id"]] = c
    if dropped:
        import sys as _sys
        print(f"[experience_store] taxonomy 白名单丢弃 {len(dropped)} 个未知 finding_type："
              f"{sorted(set(d for d in dropped if d))}（TOUCHSTONE_TAXONOMY_ENFORCE 开时生效）",
              file=_sys.stderr)
    return store


def _is_number(x):
    """数值判定（int/float，排除 bool——True/False 既是 int 子类又不该当计数/时间戳）。
    evidence 合并（fires/adoption）与时间戳合并（created_at/updated_at）的健壮过滤共用：手改/损坏
    的库可能出现非数值（字符串/None），直接 sum/min/max 会 TypeError。纯函数。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _merge_evidence(evidences):
    """合并一组同 canonical 类型兄弟条目的 evidence，避免静默丢失累积信号（fires/group_rewards 等）。
    evidences[0] 为代表条目的 evidence——非合并键（pr / ab_lift 等）取它的值、序优先。
    fires（数值，排除 bool）求和；adoption 是比率、不能直接求和——优先按 fires 加权平均（与求和后的
    fires 自洽，不偏向代表单方面）；若无 fires 权重可用却仍有 adoption 载体（如某兄弟只带 adoption
    不带 fires——evidence={"adoption":0.5}），退化为等权平均以【不静默丢弃】adoption 信号；group_rewards
    拼接去重、布尔标志（tfgrpo/seeded）取或。纯函数。"""
    evs = [e for e in evidences if isinstance(e, dict)]
    if not evs:
        return {}
    merged = dict(evs[0])                                  # 代表的键作底（pr/ab_lift 等保留）
    fires_pairs = [(e["fires"], e.get("adoption")) for e in evs if _is_number(e.get("fires"))]
    if fires_pairs:
        merged["fires"] = sum(f for f, _ in fires_pairs)
    # adoption 合并（比率，不直接求和）：优先 fires 加权平均；无 fires 权重时退化等权平均，绝不静默丢载体。
    # weighted = 同时带 fires+adoption 的兄弟（可按 fires 加权）；all_adopt = 全部 adoption 载体（含无 fires 的）。
    weighted = [(f, a) for f, a in fires_pairs if _is_number(a)]
    all_adopt = [a for a in (e.get("adoption") for e in evs) if _is_number(a)]
    tot = sum(f for f, _ in weighted)
    if weighted and tot > 0:
        merged["adoption"] = sum(f * a for f, a in weighted) / tot
    elif all_adopt:                        # 无 fires 权重但有 adoption 载体：等权平均，不丢信号（防 :251 重开）
        merged["adoption"] = sum(all_adopt) / len(all_adopt)
    rewards = []
    for e in evs:
        for r in (e.get("group_rewards") or []):
            if r not in rewards:
                rewards.append(r)
    if rewards:
        merged["group_rewards"] = rewards
    if any(e.get("tfgrpo") for e in evs):
        merged["tfgrpo"] = True
    if any(e.get("seeded") for e in evs):
        merged["seeded"] = True
    return merged


def _union_list(group, key):
    """跨 group 的兄弟条目并集某个 list 字段，保序去重——O(1) 查找（辅助 set）；元素不可哈希时回退线性。
    group = [(i, e), ...]。缺失或非 list 字段自然跳过（产出空 list）。纯函数。"""
    out, seen = [], set()
    for _, g in group:
        for item in (g.get(key) or []):
            try:
                if item in seen:
                    continue
                seen.add(item)
            except TypeError:                    # 不可哈希（dict/list 元素）→ 回退线性扫描保去重
                if item in out:
                    continue
            out.append(item)
    return out


def canonicalize_store(store):
    """把经验库里【非 locked、非 human】条目的 finding_type 规范化为生态形，并合并由此产生的重复变体。
    幂等：再跑一次无副作用（已规范条目不变、无重复可合）。locked / source='human' 的权威条目原样不动；
    finding_type 规范后为空（罕见）也不动。**不丢弃任何条目**——仅归一化 + 合并。

    合并策略（同一 canonical id 的多条 → 一条）：status 取优先级最高者（active > shadow > retired >
    candidate，不回退已推进的状态）；source_prs 取并集；evidence 合并（fires 求和、group_rewards 拼接、
    tfgrpo/seeded 取或，其余键取代表值）；text 取代表；时间取 created_at=min、updated_at=max。代表选择
    的 ties（同 status 且同 source_prs 数）用原始下标作最终 tie-breaker——跨 run/测试稳定可复现，
    不依赖输入条目次序。顺序保持首次出现位置（合并到首条处，后续重复删去）。

    典型场景：run #6 的 191 条里 LLM 把同一规律裂成 'PRA-CONSISTENCY' 与 'PRA-consistency'、
    'PRA-COVERAGE-GAP' 与 'PRA-COVERAGE_GAP'——本函数在下一次 learn.yml 运行时把它们并成单条，
    让证据不再互相稀释、graduate/retire 的按类型判定不再错位。"""
    exps = store.get("experiences", [])

    def _key(e):
        return _exp_id(_canonical_type(e.get("finding_type", "")),
                       e.get("kind", ""), e.get("repo", ""), e.get("stack", ""))

    def _touchable(e):
        # locked / human / 规范后为空 → 不动（权威或无法规范）
        return not (e.get("locked") or e.get("source") == "human"
                    or not _canonical_type(e.get("finding_type", "")))

    # 按 canonical id 分组，保留 (原始下标, 条目)：下标用作 _rep 最终稳定 tie-breaker
    groups = {}
    for i, e in enumerate(exps):
        if not _touchable(e):
            continue
        groups.setdefault(_key(e), []).append((i, e))

    _rank = {"active": 0, "shadow": 1, "retired": 2, "candidate": 3}

    def _rep(group):
        # 代表 = status 优先级高 > source_prs 多 > 原始下标小（稳定，跨 run/测试可复现）
        return min(group, key=lambda ie: (_rank.get(ie[1].get("status"), 9),
                                          -(len(ie[1].get("source_prs") or [])),
                                          ie[0]))

    # locked / human 权威条目按原样保留，其 id 被"占用"——非权威条目规范化时不得并入这些 id，
    # 否则会把一条非权威变体重命名成与权威条目同 id（破坏 id 唯一性）。见 test_canonicalize_store_*
    reserved = {e.get("id") for e in exps if not _touchable(e)}

    out = []
    seen = set()
    merged_n = renamed_n = 0
    for e in exps:
        if not _touchable(e):
            out.append(e)
            continue
        key = _key(e)
        group = groups[key]
        if key in reserved:
            # canonical id 被某 locked/human 条目占用：不规范化、不合并，本条保留原 id/finding_type（防撞）
            out.append(e)
            continue
        if len(group) > 1:
            if key in seen:
                continue                       # 后续重复：已并到首个位置，跳过
            seen.add(key)
            _ri, rep_entry = _rep(group)
            rep = dict(rep_entry)
            rep["finding_type"] = _canonical_type(rep.get("finding_type", ""))
            rep["id"] = key
            # source_prs 并集保序去重：辅助 set 做 O(1) 查找，避免大库（千条×百 PR）退化成 O(N·M)。
            rep["source_prs"] = _union_list(group, "source_prs")
            # evidence 合并（fires 求和、group_rewards 拼接、标志或）——代表序优先，不丢兄弟累积信号
            rep["evidence"] = _merge_evidence(
                [rep_entry.get("evidence")]
                + [g.get("evidence") for _, g in group if g is not rep_entry])
            # 时间合并：created_at=最早、updated_at=最晚。仅取数值时间戳——手改/损坏的库可能存非数值
            # （字符串/None），直接 min/max 会 TypeError；缺失或非数值的自然跳过，全空回落 default=0。
            created = [g.get("created_at") for _, g in group if _is_number(g.get("created_at"))]
            updated = [g.get("updated_at") for _, g in group if _is_number(g.get("updated_at"))]
            rep["created_at"] = min(created, default=0)
            rep["updated_at"] = max(updated, default=0)
            # 通用防御：跨【所有兄弟】扫顶层 list 字段（不只 rep 的键——否则 sibling-only 列表字段被静默丢）
            # 并集保序。schema 今日仅 source_prs（已上方处理），故今日为 no-op；防未来新增累积型列表字段。
            _list_keys = {k for _, g in group for k, v in g.items()
                          if isinstance(v, list) and k != "source_prs"}
            for k in _list_keys:
                rep[k] = _union_list(group, k)
            # 防御性保留最强保护标志：_touchable 已把 locked / source='human' 挡在 groups 之外（故组内
            # 必无此类条目、本段今日是 no-op），但若日后 _touchable 回归把它们误放进组，合并结果仍须继承
            # locked=True / source='human'——权威条目不得被静默抹掉保护。（review round-2 销项）
            if any(g.get("locked") for _, g in group):
                rep["locked"] = True
            if any(g.get("source") == "human" for _, g in group):
                rep["source"] = "human"
            out.append(rep)
            merged_n += len(group) - 1
        else:
            ft = _canonical_type(e.get("finding_type", ""))
            if ft != e.get("finding_type") or key != e.get("id"):
                out.append(dict(e, finding_type=ft, id=key))
                renamed_n += 1
            else:
                out.append(e)
    store["experiences"] = out
    if merged_n or renamed_n:
        import sys as _sys
        print(f"[experience_store] canonicalize_store：合并 {merged_n} 条重复变体、"
              f"规范 {renamed_n} 条 finding_type", file=_sys.stderr)
    return store


# --- 门控：candidate → active（shadow A/B 达标）---------------------------------
def graduate(store, ab_results):
    """shadow A/B：对最近 PR 比较"注入该经验 vs 不注入"的采纳率，lift 达标且样本足 → 转 active。
    ab_results: {finding_type: {with_seen, with_adopted, without_seen, without_adopted}}。
    A/B 的真实跑批（需真实 PR + PR-Agent）在你的环境做；本函数只做达标【判定】。"""
    graduated = []
    for e in store.get("experiences", []):
        if e["status"] != "candidate":
            continue
        ab = ab_results.get(e["finding_type"])
        if not ab:
            continue
        ws, wa = ab.get("with_seen", 0), ab.get("with_adopted", 0)
        os_, oa = ab.get("without_seen", 0), ab.get("without_adopted", 0)
        if ws < GRADUATE_MIN_SAMPLES or os_ < GRADUATE_MIN_SAMPLES:
            continue
        lift = (wa / ws) - (oa / os_)
        if lift >= GRADUATE_MIN_LIFT:
            e["status"] = "active"
            if not isinstance(e.get("evidence"), dict):   # evidence 可为 None（JSON evidence:null）→ 建空 dict 再写
                e["evidence"] = {}
            e["evidence"]["ab_lift"] = round(lift, 2)
            e["updated_at"] = int(time.time())
            graduated.append(e["id"])
    return graduated


# --- 退役：active → retired（govern 式，前提不再成立）---------------------------
def retire(store, calib_agg):
    """active 经验的前提若不再成立则退役（沿 govern 思路）：
      suppress（"这类是噪声"）—— 若该类型采纳率回升到 emphasize 阈值以上 → 前提不再成立，退役；
      emphasize（"这类有价值"）—— 若该类型采纳率跌破 RETIRE_ADOPT_MAX → 前提不再成立，退役。"""
    by_rule = calib_agg.get("by_rule") or {}
    retired = []
    for e in store.get("experiences", []):
        if e["status"] != "active":
            continue
        if e.get("locked"):
            continue                          # 人锁定的经验不自动退役
        v = by_rule.get(e["finding_type"])
        if not v or v.get("fires", 0) < RETIRE_MIN_FIRES:
            continue
        adopt = v.get("adoption_rate")
        if adopt is None:
            adopt = v.get("changes_requested_rate")
        if adopt is None:
            continue
        gone = (e["kind"] == "suppress" and adopt >= EMPHASIZE_ADOPT_MIN) or \
               (e["kind"] == "emphasize" and adopt <= RETIRE_ADOPT_MAX)
        if gone:
            e["status"] = "retired"
            e["updated_at"] = int(time.time())
            retired.append(e["id"])
    return retired


# --- c2：差分回滚——active 经验若"注入反降采纳率"则退役（与 graduate 对称）-------------
# 回答 docs/tfgrpo-productionization-design.html 差距 3b："经验在帮还是在害"。graduate 只看
# 正向 lift≥+0.10 转 active；坏经验原本要等跌破 retire 的 RETIRE_ADOPT_MAX(0.15) 绝对门槛
# 才下线——相对差分让"注入后比不注入还差"的经验更快退役。样本门槛镜像 graduate（≥20）防小样本误杀。
RETIRE_NEGATIVE_LIFT_DEFAULT = -0.05   # 注入臂采纳率比不注入臂低 5pp 即回滚


def retire_on_negative_lift(store, ab_results, *, min_samples=None, threshold=None):
    """active 经验若'注入该经验 vs 不注入'的采纳率 lift ≤ threshold（默认 -0.05）→ 退役。
    与 graduate 对称：graduate 看 lift≥+0.10 转 active；本函数看 lift≤负阈值 退役。
    ab_results 同 graduate：{finding_type: {with_seen, with_adopted, without_seen, without_adopted}}。
    样本不足（<min_samples）不轻动；locked/人手 seed 不动。返回退役的 id 列表。"""
    if min_samples is None:
        min_samples = GRADUATE_MIN_SAMPLES
    if threshold is None:
        threshold = float(os.environ.get("TOUCHSTONE_RETIRE_NEGATIVE_LIFT",
                                         RETIRE_NEGATIVE_LIFT_DEFAULT))
    retired = []
    for e in store.get("experiences", []):
        if e.get("status") != "active" or e.get("locked") or e.get("source") == "human":
            continue
        ab = (ab_results or {}).get(e.get("finding_type"))
        if not ab:
            continue
        ws, wa = ab.get("with_seen", 0), ab.get("with_adopted", 0)
        os_, oa = ab.get("without_seen", 0), ab.get("without_adopted", 0)
        if ws < min_samples or os_ < min_samples:
            continue
        lift = (wa / ws) - (oa / os_)
        if lift <= threshold:
            e["status"] = "retired"
            if not isinstance(e.get("evidence"), dict):   # evidence 可为 None（JSON evidence:null）→ 建空 dict 再写
                e["evidence"] = {}
            e["evidence"]["ab_lift"] = round(lift, 2)
            e["updated_at"] = int(time.time())
            retired.append(e["id"])
    return retired


# --- 差距3a：收敛检测（opt-in，默认关 = 零行为变化）------------------------------
# 某 active 经验连续 N_STABLE 轮 text 不变 + ab_lift 变化 < LIFT_DRIFT → 标 convergence_state=
# "stable"，下轮蒸馏跳过该 type（已稳定，不必反复改写）；新增信号（text 变 / lift 漂移）打破稳定。
# 状态记在经验条目 convergence 字段（随 store 持久化），不另起文件。
#   convergence = {"stable_rounds": int, "last_text_hash": str, "last_lift": float|None,
#                  "state": "stable"|None}
# 与 retire_on_negative_lift 同源 lift（ab_results 逐字同 schema），但只读不改 status——
# 收敛是"跳过蒸馏"的优化，不是生命周期变更（retire/disable 仍照常触发）。
CONVERGE_N_STABLE_DEFAULT   = 3        # 连续 N 轮稳定 → 标 stable
CONVERGE_LIFT_DRIFT_DEFAULT = 0.05     # lift 变化阈值（绝对值）
CONVERGENCE_ENABLED_DEFAULT = "false"  # vars/未设 → off（全量蒸馏=现状）


def _convergence_enabled():
    return os.environ.get("TOUCHSTONE_CONVERGENCE", CONVERGENCE_ENABLED_DEFAULT).lower() in (
        "1", "true", "yes", "on")


def _converge_n_stable():
    try:
        n = int((os.environ.get("TOUCHSTONE_CONVERGE_N_STABLE") or "").strip()
                or str(CONVERGE_N_STABLE_DEFAULT))
    except ValueError:
        n = CONVERGE_N_STABLE_DEFAULT
    return n if n > 0 else CONVERGE_N_STABLE_DEFAULT


def _converge_lift_drift():
    try:
        d = float((os.environ.get("TOUCHSTONE_CONVERGE_LIFT_DRIFT") or "").strip()
                  or str(CONVERGE_LIFT_DRIFT_DEFAULT))
    except ValueError:
        d = CONVERGE_LIFT_DRIFT_DEFAULT
    return d if d >= 0 else CONVERGE_LIFT_DRIFT_DEFAULT


def _lift_from_ab(ab):
    """从单 type 的 ab dict 算 lift = with_rate - without_rate；样本不足/缺 → None。"""
    if not isinstance(ab, dict):
        return None
    ws, wa = ab.get("with_seen", 0), ab.get("with_adopted", 0)
    os_, oa = ab.get("without_seen", 0), ab.get("without_adopted", 0)
    if ws < GRADUATE_MIN_SAMPLES or os_ < GRADUATE_MIN_SAMPLES:
        return None
    return (wa / ws) - (oa / os_)


def update_convergence(store, ab_results):
    """逐 active 经验更新收敛状态（差距3a）。text 哈希不变 + lift 可算且漂移 < 阈值 → stable_rounds+1；
    否则归零。stable_rounds 达 N_STABLE → state="stable"。lift 不可算（样本不足）时只看 text：text 变
    则归零，text 不变则维持（不 +1，也不归零——样本不足不该单方面确认收敛，但也不因无数据惩罚）。
    返回本轮新标 stable 的 type 集（可观测）。纯增量改 convergence 字段，不动 status/text/evidence。"""
    if not _convergence_enabled():
        return set()
    n_stable = _converge_n_stable()
    drift = _converge_lift_drift()
    ab_results = ab_results or {}
    newly_stable = set()
    for e in store.get("experiences", []):
        if e.get("status") != "active":
            continue                                  # 非 active 不跟踪（candidate 还在变、retired 已下线）
        ftype = e.get("finding_type")
        text_hash = hashlib.sha256((e.get("text") or "").encode("utf-8")).hexdigest()
        lift = _lift_from_ab(ab_results.get(ftype))
        conv = e.get("convergence")
        if not isinstance(conv, dict):
            conv = {}
        prev_hash = conv.get("last_text_hash")
        prev_lift = conv.get("last_lift")
        text_same = prev_hash is not None and prev_hash == text_hash
        if text_same and lift is not None and prev_lift is not None:
            # text 不变 + 双方 lift 可得 → 比 drift
            if abs(lift - prev_lift) < drift:
                conv["stable_rounds"] = int(conv.get("stable_rounds", 0)) + 1  # 稳 → +1
            else:
                conv["stable_rounds"] = 0                                      # lift 漂移 → 归零
        elif text_same:
            # PRA round-4（experience_store.py:589）：text 不变但本轮或上轮 lift 缺（样本不足 /
            # 从不足中恢复）→ 维持，不奖不罚。旧实现此分支（prev_lift None）落 else 归零，惩罚了
            # 临时样本不足的 baseline 臂。现与"本轮 lift None"同处置：hold。
            conv["stable_rounds"] = int(conv.get("stable_rounds", 0))
        else:
            conv["stable_rounds"] = 0                                          # text 变 → 归零
        conv["last_text_hash"] = text_hash
        conv["last_lift"] = round(lift, 4) if lift is not None else None
        was_stable = conv.get("state") == "stable"
        conv["state"] = "stable" if conv["stable_rounds"] >= n_stable else None
        e["convergence"] = conv
        if conv["state"] == "stable" and not was_stable:
            newly_stable.add(ftype)
    return newly_stable


def converged_types(store):
    """返回【所有 active 经验均已 stable】的 finding_type 集（distill skip_types 用）。
    PRA round-3（experience_store.py:606 "Lossy Normalization"）：旧实现"≥1 条 stable 即收入"
    会让同 type 下仍有非 stable 兄弟经验时整 type 被跳过——丢失其候选蒸馏（两条不同 text 的
    active 经验共享 finding_type 时，一条 stable 就 suppress 了另一条的演化）。改为"该 type 的
    所有 active 经验均 stable"才收入：保守不跳过任何仍在演化的 type。未开收敛检测时返回空集。"""
    if not _convergence_enabled():
        return set()
    by_type = {}                              # ftype -> list[bool]（每条 active 是否 stable）
    for e in store.get("experiences", []):
        if e.get("status") != "active" or not e.get("finding_type"):
            continue
        conv = e.get("convergence") if isinstance(e.get("convergence"), dict) else {}
        by_type.setdefault(e["finding_type"], []).append(conv.get("state") == "stable")
    return {ftype for ftype, sts in by_type.items() if all(sts)}


# --- 差距3b：差分时序持久化 + 趋势回滚（opt-in，默认关 = 零行为变化）------------------
# retire_on_negative_lift 是【静态阈值闸】（lift ≤ -0.05 退役）；本节补【趋势闸】与【时序可观测】：
#   ① append_lift_history：把本轮 per-type lift 追加到时序文件（adoption-trend.json），回答"经验在帮
#      还是在害"的时序变化（_lift_summary 只给本轮快照，看不出趋势）。
#   ② retire_on_lift_decline：active 经验的 type 时序连续 M_DECLINE 轮下降 → 提前退役（不必等
#      lift 跌破 -0.05）。与静态闸互补：趋势闸在 lift 仍正但持续恶化时下线，防"慢性毒药"经验。
# 时序文件随经验库 git 提交（learn.yml commit data/*.json），跨轮持久；lift=None（样本不足）的类型
# 不进时序（无法判趋势，不污染）。趋势判据复用收敛漂移阈值（每步降幅 > drift，防噪声抖动误触发）。
TREND_MAX_HISTORY_DEFAULT   = 20      # 每 type 保留的最大时序条数（防无界增长）
AUTO_ROLLBACK_M_DEFAULT     = 2       # 连续 M 轮下降 → 趋势退役（0 = 关闭，仅静态闸）
DIFFERENTIAL_METRICS_DEFAULT = "false"  # vars/未设 → off（不记时序=现状）


def _differential_enabled():
    """TOUCHSTONE_DIFFERENTIAL_METRICS 真值时开时序持久化+趋势回滚（默认关）。"""
    return os.environ.get("TOUCHSTONE_DIFFERENTIAL_METRICS", DIFFERENTIAL_METRICS_DEFAULT).lower() in (
        "1", "true", "yes", "on")


def _auto_rollback_m():
    """TOUCHSTONE_AUTO_ROLLBACK_M：连续 M 轮下降触发趋势退役（默认 2）；0/非正 → 关闭趋势闸。"""
    try:
        m = int((os.environ.get("TOUCHSTONE_AUTO_ROLLBACK_M") or "").strip()
                or str(AUTO_ROLLBACK_M_DEFAULT))
    except ValueError:
        m = AUTO_ROLLBACK_M_DEFAULT
    return m if m > 0 else 0


def _trend_max_history():
    try:
        n = int((os.environ.get("TOUCHSTONE_TREND_MAX_HISTORY") or "").strip()
                or str(TREND_MAX_HISTORY_DEFAULT))
    except ValueError:
        n = TREND_MAX_HISTORY_DEFAULT
    return n if n > 0 else TREND_MAX_HISTORY_DEFAULT


def append_lift_history(trend, ab_results, *, ts=None, max_history=None):
    """纯函数：把本轮 ab 的 per-type lift 追加到时序（trend = {type: [entries]}，原地改 + 返回）。
    每条 = {ts, lift, with_seen, with_adopted, without_seen, without_adopted}；cap 到 max_history 条/type
    （FIFO 丢旧）。lift=None（样本不足 < GRADUATE_MIN_SAMPLES）时仍记录条目（lift=null + 计数）——
    PRA round-2（experience_store.py:659）：旧实现 `if lift is None: continue` 让样本不足的类型完全
    不可见，运维无法区分"样本不足"与"type 缺失"。现保留条目，_is_declining 对 tail 含 None 返回
    False（不轻动），故 null 条目不污染趋势判定、只增可见性。复用 _lift_from_ab（同源 lift 口径）。"""
    if max_history is None:
        max_history = _trend_max_history()
    ts = ts if ts is not None else int(time.time())
    if not isinstance(trend, dict):
        trend = {}
    for ftype, ab in (ab_results or {}).items():
        if not isinstance(ab, dict):
            continue
        lift = _lift_from_ab(ab)
        entry = {"ts": ts, "lift": round(lift, 4) if lift is not None else None,
                 "with_seen": ab.get("with_seen", 0), "with_adopted": ab.get("with_adopted", 0),
                 "without_seen": ab.get("without_seen", 0), "without_adopted": ab.get("without_adopted", 0)}
        series = trend.get(ftype) or []
        series.append(entry)
        # max_history<=0 显式视为"不限"（PRA round-5 experience_store.py:680）：0 因 Python 切片
        # `[-0:]≡[0:]` 本就保留全部，但负值 `[-(-1):]=[1:]` 会误丢首条——边界无定义致运维误配静默
        # 丢数据。统一：<=0 不封顶（与 0 的现状一致，并修复负值 bug）。
        if max_history > 0 and len(series) > max_history:
            series = series[-max_history:]
        trend[ftype] = series
    return trend


def _is_declining(series, m, drift):
    """纯函数：series 最后 m+1 条 lift 是否连续 m 步下降（每步降幅 > drift，防噪声抖动）。
    数据不足（< m+1 条）/ 含 None / m 非正 → False（不轻动）。
    PRA round-8（experience_store.py:None "non-positive m"）：m<=0 时 range(m)=range(0或负)=[]
    → all([])=True（空真值）——语义错误（"0 步下降"应为 False 而非 True）。retire_on_lift_decline
    的 m_decline<=0 守卫（:716）在生产中阻此路径，但函数应自洽。显式 m<=0 → False。"""
    if m <= 0 or not series or len(series) < m + 1:
        return False
    tail = [s.get("lift") for s in series[-(m + 1):]]
    if any(l is None for l in tail):
        return False
    return all(tail[i] - tail[i + 1] > drift for i in range(m))


def retire_on_lift_decline(store, trend, *, m_decline=None, drift=None):
    """趋势回滚（差距3b）：active 经验的 type 时序连续 m_decline 轮下降 → 退役。
    与 retire_on_negative_lift（静态阈值）互补——趋势闸在 lift 仍正但持续恶化时提前下线。
    样本不足/时序不足（< m+1 条）不动；locked/人手 seed 不动。返回退役 id 列表。
    留 evidence.rollback_reason='auto_rollback_lift_decline' + lift_trace 供人复核。"""
    if m_decline is None:
        m_decline = _auto_rollback_m()
    if m_decline <= 0:
        return []
    if drift is None:
        drift = _converge_lift_drift()
    retired = []
    for e in store.get("experiences", []):
        if e.get("status") != "active" or e.get("locked") or e.get("source") == "human":
            continue
        ftype = e.get("finding_type")
        series = (trend or {}).get(ftype) or []
        if not _is_declining(series, m_decline, drift):
            continue
        e["status"] = "retired"
        if not isinstance(e.get("evidence"), dict):
            e["evidence"] = {}
        e["evidence"]["rollback_reason"] = "auto_rollback_lift_decline"
        # PRA round-3（experience_store.py:720）：旧 `s.get("lift", 0)` 把缺失/None 的 lift 掩成 0，
        # 误导 lift_trace（如 [0.3,0.2,None] 显示成 [0.3,0.2,0.0]）。且 round(None) 会 TypeError。
        # 忠实记录：None→None（供人复核见真实数据），非 None 才 round。
        e["evidence"]["lift_trace"] = [
            round(s["lift"], 4) if s.get("lift") is not None else None
            for s in series[-(m_decline + 1):]]
        e["updated_at"] = int(time.time())
        retired.append(e["id"])
    return retired


def disable(store, exp_id):
    """人工单条停用（→retired），可回退。每条经验留来源/证据，便于抽检与回退。"""
    for e in store.get("experiences", []):
        if e["id"] == exp_id:
            e["status"] = "retired"
            e["updated_at"] = int(time.time())
            return True
    return False


# --- 注入：active 经验 → PR-Agent extra_instructions（只建议、不进闸）-------------
def _evidence_strength(e):
    """证据强度元组：(source_prs 数, evidence.fires, updated_at)——多 PR 反复见证 > 命中样本数 > 新旧。
    用于冲突消解（c4-2c）：原仅按 updated_at 排序，会把'单 PR 偶然蒸出但新'误判为比'多 PR 反复
    验证但旧'更可信。现改为证据强度优先（主排序键已变 = 行为变化）；仅当证据强度完全相等时才退回
    updated_at 作末位 tiebreak（此 tiebreak 与旧逻辑一致）。"""
    ev = e.get("evidence") or {}
    return (len(e.get("source_prs") or []), ev.get("fires") or 0, e.get("updated_at") or 0)


def _resolve_conflicts(active):
    """同一 仓·栈·发现类型 不能既 emphasize 又 suppress：保留证据强度最高的一条（I3 / c4-2c）。"""
    by = {}
    for e in active:
        k = (e.get("repo", ""), e.get("stack", ""), e.get("finding_type"))
        if k not in by or _evidence_strength(e) >= _evidence_strength(by[k]):
            by[k] = e
    keep = {id(v) for v in by.values()}
    return [e for e in active if id(e) in keep]


def render_injection(store, *, include_shadow=False):
    """把 active 经验渲染成注入 PR-Agent 的 extra_instructions 文本。

    include_shadow=False（默认，现状不变）：仅 active；candidate/retired 不注入。
    include_shadow=True（冷启动破死锁，需 env 显式开启 + 受信 ref 防投毒同等约束——见
    review_provider._experience_injection）：active 段后追加 shadow 段，从 candidate 池确定性
    抽样（shadow_candidates）、每条前缀 [shadow] 标灰（采数期、advisory only、未达门槛）。
    shadow 候选只影响 PR-Agent 建议、不进 contract_check/verify/总闸——铁律不变。
    输出纯指令文本——只影响 PR-Agent 的建议，不触碰确定性 contract_check / 总闸（评审与合入闸的边界）。"""
    active = _resolve_conflicts([e for e in store.get("experiences", []) if e["status"] == "active"])
    if not active and not include_shadow:
        return ""
    lines = []
    if active:
        lines.append("# Learned review experience (repo-specific, advisory only — do not gate merges):")
        for e in active:
            lines.append(f"- {e['text']}")
    if include_shadow:
        shadow = shadow_candidates(store, **_shadow_env_params())
        if shadow:
            lines.append("# Shadow candidates (exploratory, advisory only — gathering A/B data, not yet validated):")
            for e in shadow:
                lines.append(f"- [shadow] {e['text']}")
    return "\n".join(lines)


def active_types(store):
    """当前 active 经验的 finding_type 列表——即本轮评审会被注入（render_injection）的类型。
    供 orchestrator 写入 result marker，为未来 shadow A/B 采纳率分臂采集留接口。"""
    return [e.get("finding_type") for e in (store or {}).get("experiences", [])
            if e.get("status") == "active" and e.get("finding_type")]


def active_ids(store):
    """当前 active 经验的 id 列表——供 orchestrator 写入 result marker 的 injected_experience_ids，
    使坏经验可【单条】归因与回退（类型级的 active_types 只能归因到类型，见数据采集设计 取舍 2）。"""
    return [e.get("id") for e in (store or {}).get("experiences", [])
            if e.get("status") == "active" and e.get("id")]


# --- shadow 注入：candidate 池 → 采 A/B with 臂数据的隔离标灰注入（冷启动破死锁）-------------
# 详见 docs/tfgrpo-self-evolution-design.html §2。本组函数只【选】+【渲染】不【激活】：
# graduate 零改动（candidate→active 仍走原 A/B 达标判定），仅拓宽数据采集侧的注入口子。
# 除数用 2**32 而非 (2**32-1)：前 8 hex 位最大 0xFFFFFFFF=(2**32-1)，除以 2**32 保证商严格
# < 1.0（半开区间 [0,1)），使 ratio=1.0 能真正全选——除以 (2**32-1) 会让 hash=0xFFFFFFFF 时商=1.0，
# 被 `>=ratio` 错误排除（off-by-one 边界 bug，pr-agent 第 2 轮指出）。
_SHADOW_HASH_SCALE = float(2**32)


def _shadow_hash(exp_id):
    """经验 id → [0,1) 的稳定哈希。用 hashlib（非内置 hash()）：后者随 PYTHONHASHSEED 抖动，
    同一 PR 多轮评审会注入不同 shadow 集，污染 A/B 归因（with 臂样本无法稳定归属该 type）。"""
    return int(hashlib.sha256(exp_id.encode("utf-8")).hexdigest()[:8], 16) / _SHADOW_HASH_SCALE


def _shadow_env_params():
    """从 env 读 shadow 注入三参数（render_injection/shadow_types/shadow_ids 统一来源，保证
    本轮渲染的 shadow 段与 marker 归因的 shadow_types/shadow_ids 取的是同一批候选）。"""
    return {
        "ratio": float((os.environ.get("TOUCHSTONE_SHADOW_RATIO") or "").strip() or str(SHADOW_RATIO_DEFAULT)),
        "max_per_review": int((os.environ.get("TOUCHSTONE_SHADOW_MAX_PER_REVIEW") or "").strip() or str(SHADOW_MAX_PER_REVIEW_DEFAULT)),
        "min_evidence": int((os.environ.get("TOUCHSTONE_SHADOW_MIN_EVIDENCE") or "").strip() or str(SHADOW_MIN_EVIDENCE_DEFAULT)),
    }


def _shadow_injection_enabled():
    """shadow 注入总开关（默认关）：TOUCHSTONE_SHADOW_INJECTION 真值时才启用 shadow 注入的
    【归因】（orchestrator marker 写 shadow_types/shadow_experience_ids）与【渲染】
    （review_provider render_injection(include_shadow=True)，step4 接通）。

    默认关 = 现状字节级不变：orchestrator 写空 shadow_*、render_injection include_shadow=False。
    启用前提：还需配 TOUCHSTONE_EXPERIENCE_REF（PR 事件下防经验库投毒，见
    review_provider._experience_injection 的纵深防御）——ref 未配时 review_provider 整个跳过
    经验注入（active+shadow 都不渲染），此时单开本开关会致 marker 归因与实际渲染不一致，故
    【未接通 step4 渲染前勿开本开关】（开了 shadow_types 会写但 PR-Agent 没收到 → with 臂归因失真）。

    orchestrator 与 review_provider 必须读【同一】本开关，保证「marker 归因的 shadow_types」与
    「实际渲染的 shadow 段」取同一批候选（shadow_types/shadow_ids 与 render_injection 同源走
    shadow_candidates，见 _shadow_env_params 注释）。"""
    val = os.environ.get("TOUCHSTONE_SHADOW_INJECTION")
    if val is None:
        return SHADOW_INJECTION_DEFAULT
    return val.lower() in ("1", "true", "yes", "on")


def _bootstrap_enabled():
    """bootstrap seed 总开关（默认关）：TOUCHSTONE_BOOTSTRAP_SEED 真值时才从 calib_agg 高采纳 type
    自动 seed active emphasize（冷启动辅助路径 c）。默认关 = 零行为变化（无 calib_agg 时也无产出）。"""
    val = os.environ.get("TOUCHSTONE_BOOTSTRAP_SEED")
    if val is None:
        return BOOTSTRAP_SEED_DEFAULT
    return val.lower() in ("1", "true", "yes", "on")


def _bootstrap_text(finding_type):
    """bootstrap 自动产经验的注入文本（通用——无 LLM 生成具体建议，仅标记该 type 高采纳、该挑）。"""
    return (f"Reviewers frequently adopt {finding_type}-type findings in this repo — "
            f"prioritize surfacing similar findings.")


def bootstrap_from_calibrate(calib_agg, store, repo="", stack=""):
    """从 calib_agg 的高采纳 type 产 active emphasize（冷启动破死锁辅助路径 c）。

    让【全新 type】立即有首个 active（进 active_types → aggregate_ab with 臂非空），加速冷启动——
    与 shadow 注入(a)互补：(a) 让 candidate 采 with 臂数据逐步 graduate；(c) 让全新 type 越过
    "从未注入"的鸡生蛋起点。门槛高于蒸馏入池（MIN_FIRES/MIN_ADOPT）抑制小样本偶然高采纳。

    安全纪律（对齐 docs/tfgrpo-self-evolution-design.html §2）：
    - 只产 emphasize（高采纳=该挑），永不产 suppress（suppress 风险更高，留人手 seed）。
    - 跳过 protected_types（人立红线，bootstrap 即使高采纳也不碰）。
    - 跳过非 review type（确定性锚 SCOPE/TEST 等永不进经验，坑 2b）。
    - 只对该 type【尚无任何 emphasize 经验】的全新 type 产——避免与已入池 candidate 同 id 冲突
      （seed_experience 的 update 分支会把 candidate 直接提成 active，绕过 graduate，违反坑 3 门控）。
    - source="bootstrap" + locked=False（让 retire 能管，与人手 locked=True 区分）。

    返回新产出的经验 id 列表。"""
    if not _bootstrap_enabled():
        return []
    by_rule = (calib_agg or {}).get("by_rule") or {}
    protected = _protected_types()
    min_fires = int((os.environ.get("TOUCHSTONE_BOOTSTRAP_MIN_FIRES") or "").strip() or str(BOOTSTRAP_MIN_FIRES_DEFAULT))
    min_adopt = float((os.environ.get("TOUCHSTONE_BOOTSTRAP_MIN_ADOPT") or "").strip() or str(BOOTSTRAP_MIN_ADOPT_DEFAULT))
    existing = {_exp_id(e.get("finding_type"), "emphasize", repo, stack)
                for e in (store or {}).get("experiences", []) if e.get("kind") == "emphasize"}
    produced = []
    for ftype, stats in by_rule.items():
        if not _is_review_type(ftype):
            continue
        if ftype in protected:
            continue
        fires = (stats or {}).get("fires", 0) or 0
        adopt = (stats or {}).get("adoption_rate")
        if fires < min_fires or adopt is None or adopt < min_adopt:
            continue
        eid = _exp_id(ftype, "emphasize", repo, stack)
        if eid in existing:
            continue
        seed_experience(store, ftype, "emphasize", _bootstrap_text(ftype),
                        repo=repo, stack=stack, status="active", locked=False, source="bootstrap")
        produced.append(eid)
    return produced


def shadow_candidates(store, *, ratio, max_per_review, min_evidence):
    """从 candidate 池里挑出本轮要 shadow 注入的候选（采 A/B with 臂数据，破冷启动死锁）。

    入选条件：status=="candidate" 且 source_prs 数 >= min_evidence（初筛防孤证）。
    安全闸：protected_types（人立的红线类型）的 suppress 永不 shadow 注入——红线类型即使
    历史上人总忽略，也不让学习回路在采数期碰；protected 的 emphasize 不受此限（该挑的仍采数）。
    抽样：每个 candidate 独立确定性判定（_shadow_hash(id) < ratio）——ratio 控长期入选比例；
    max_per_review 截单轮爆炸面（负数 clamp 到 0——避免 selected[:负数] 返尾部元素的语义 bug）。
    判定稳定（哈希基于 id）使同 candidate 跨轮归因一致。

    本函数只【选】不【注入】：渲染由 render_injection(include_shadow=True) 做；graduate 零改动。"""
    protected = _protected_types()
    selected = []
    for e in store.get("experiences", []):
        if e.get("status") != "candidate":
            continue
        if e.get("kind") == "suppress" and e.get("finding_type") in protected:
            continue
        if len(e.get("source_prs") or []) < min_evidence:
            continue
        if _shadow_hash(e["id"]) >= ratio:
            continue
        selected.append(e)
    selected.sort(key=lambda e: _shadow_hash(e["id"]))
    return selected[:max(0, max_per_review)]


def shadow_types(store):
    """本轮会被 shadow 注入的 candidate 的 finding_type 列表——供 orchestrator 写入 result
    marker 的 shadow_types 字段，使 aggregate_ab 的 with 臂能归因到 shadow 注入的 type
    （破冷启动死锁的数据采集侧）。与 active_types 对称：active_types 归因 active 注入、
    shadow_types 归因 shadow 注入。参数取自 TOUCHSTONE_SHADOW_* env（与 render_injection 同源）。"""
    return [e.get("finding_type") for e in shadow_candidates(store or {}, **_shadow_env_params())
            if e.get("finding_type")]


def shadow_ids(store):
    """本轮会被 shadow 注入的 candidate 的 id 列表——供 orchestrator 写入 result marker 的
    shadow_experience_ids 字段（与 active_ids 对称：坏 shadow 经验可单条归因与回退）。
    参数取自 TOUCHSTONE_SHADOW_* env（与 render_injection 同源）。"""
    return [e.get("id") for e in shadow_candidates(store or {}, **_shadow_env_params()) if e.get("id")]


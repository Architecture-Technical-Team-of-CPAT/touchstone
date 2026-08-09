# touchstone/seed_loader.py —— 消费方仓 .touchstone/seeds.yaml → 评审注入文本
# ----------------------------------------------------------------------------
# 与引擎经验库（data/experience_store.json，TF-GRPO 学的）互补：
#   • 引擎经验库：所有消费方共享的通用经验（需 learn.yml + TF-GRPO + graduate）
#   • 消费方 seeds.yaml：该消费方私有的团队规范（直接注入、不走 graduate、无需基础设施）
#
# 设计要点：
#   • 路径约定 .touchstone/seeds.yaml（与 pr.yaml/standards.yaml/pr-agent.yaml 同目录）
#   • 评审时 review_provider._experience_injection 读 → 追加到 extra_instructions
#   • 只走 TOUCHSTONE_EXPERIENCE_ENABLED 总闸；不走 EXPERIENCE_REF 防投毒闸
#     （seeds.yaml 是仓内配置，与 pr-agent.yaml 同级，受合并权限保护，非跨仓引用）
#   • 失败优雅降级：文件缺失/解析失败/格式不对 → 返回 ""，不阻塞评审
#   • kind=emphasize（多盯紧）/ suppress（少挑）；text 写英文（PR-Agent 提示词英文环境）
"""
从消费方仓的 ``.touchstone/seeds.yaml`` 加载手写规范种子 → 评审注入文本。

yaml 格式（列表，每项一条规范）::

    - finding_type: PRA-ERROR-SWALLOW
      kind: emphasize            # emphasize=多盯紧 / suppress=少挑
      stack: python              # 可选，技术栈过滤（空=所有栈）
      text: Flag empty catch blocks; they swallow errors silently.

    - finding_type: PRA-NIT
      kind: suppress
      text: Don't flag formatting nits; the linter handles them.

纯函数、无副作用、无外部依赖（仅 stdlib + yaml）。失败优雅降级返回 ""。
"""

import os
import sys

SEEDS_REL = os.path.join(".touchstone", "seeds.yaml")   # 与 pr.yaml/standards.yaml 同目录


def load_seed_injection(repo_dir=".", stack=None):
    """读 ``repo_dir/.touchstone/seeds.yaml`` → 返回注入文本（供 PR-Agent extra_instructions）。

    repo_dir：消费方仓的 checkout 根（评审时 orchestrator 透传 REPO_DIR）。
    stack：可选技术栈过滤（如 "python"）。契约：**非 str 的 stack（int/list/None/空串）
    一律当作"不过滤"（fail-open，注入全部）**——advisory 规范 fail-open 比 str() 强转
    丢全部更安全；非 str 入参另打 [warn] 辅助排查 caller bug。
    无文件 / 解析失败 / 空列表 → 返回 ""（优雅降级，不阻塞评审）。
    格式不对的条目（kind 非 emphasize/suppress、缺 finding_type/text）逐条跳过、不整体失败。

    已知 gap（诚实标注）：主评审路径 ``_experience_injection`` 不持有当前 PR 的技术栈上下文，
    故调用时不传 stack → 带 ``stack`` 字段的种子也会注入（不按栈过滤）。多数团队规范是通用的
    （不标 stack），不受影响；按栈过滤的种子需由显式持有栈上下文的调用方自行调本函数。
    """
    # abspath 归一化（round-2 review）：防 repo_dir 带尾斜杠/相对分量致 join 行为意外。
    abs_dir = os.path.abspath(repo_dir or ".")
    if not os.path.isdir(abs_dir):               # round-5 review：repo_dir 非目录（如误传文件路径）→ 显式告警 + 返空
        print(f"[warn] seeds.yaml：repo_dir 不是目录（{abs_dir}）→ 跳过种子注入", file=sys.stderr)
        return ""
    path = os.path.join(abs_dir, SEEDS_REL)
    if not os.path.isfile(path):
        return ""
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            seeds = yaml.safe_load(f)
    except Exception as e:
        print(f"[warn] seeds.yaml 解析失败（跳过种子注入）：{e}", file=sys.stderr)
        return ""
    if not seeds:
        return ""
    if not isinstance(seeds, list):
        print(f"[warn] seeds.yaml 顶层应是列表（每项一条规范），实际 {type(seeds).__name__} → 跳过",
              file=sys.stderr)
        return ""
    parts = []
    # 长度封顶（防御纵深）：seeds.yaml 在 PR 评审时从 repo_dir 读，text/finding_type 直接进
    # PR-Agent 提示词。封顶限注入面（防整段文档被塞入），与 threat-model 文档（seeds.yaml.example）
    # 一道把"PR-head 配置影响评审提示词"这个既有向量（pr-agent.yaml/standards.yaml 同款）圈在
    # 可控范围。评审输出本身 advisory-only（无合入权），进一步限影响。
    MAX_TEXT = 500
    MAX_TYPE = 80
    # round-3 review：非 str stack（int/list 等）→ 当作"不过滤"（与 None 同），而非 str() 强转
    # （"123" 永不匹配 → 静默丢全部种子）。caller 传错类型时 fail-open（注入全部）比 fail-closed
    # （丢全部团队规范）更安全——团队规范本身是 advisory。round-4 review：非 str 另打 [warn]
    # 辅助排查 caller bug（fail-open 但不静默）。
    if isinstance(stack, str) and stack.strip():
        stack_l = stack.strip().lower()
    elif stack is None or (isinstance(stack, str) and not stack.strip()):
        stack_l = None                        # None / 空串 = 合法"不过滤"
    else:
        print(f"[warn] load_seed_injection 收到非 str stack（{type(stack).__name__}）"
              f"→ 当作不过滤（fail-open）；可能是 caller bug", file=sys.stderr)
        stack_l = None
    for s in seeds:
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind") or "").strip().lower()
        ftype = str(s.get("finding_type") or "").strip()[:MAX_TYPE]
        text = str(s.get("text") or "").strip()[:MAX_TEXT]
        if kind not in ("emphasize", "suppress") or not ftype or not text:
            continue                          # 格式不对：逐条跳过、不整体失败
        if stack_l is not None:
            raw_stack = s.get("stack")
            # round-2 review：stack 非 str（list/dict）时 str() 产 "['python']" 永不匹配 →
            # 静默丢种子。要求 stack 必须是 str（或 None/留空=全栈）；非 str 视为格式不对跳过。
            if raw_stack is not None and not isinstance(raw_stack, str):
                continue
            s_stack = str(raw_stack or "").strip().lower()
            if s_stack and s_stack != stack_l:
                continue                      # 种子标了栈且不匹配 → 跳过
        verb = "Prioritize surfacing" if kind == "emphasize" else "Do not raise"
        parts.append(f"- [{ftype}] {verb}: {text}")
    if not parts:
        return ""
    header = "## Team seed rules (from .touchstone/seeds.yaml)"
    return header + "\n" + "\n".join(parts)

#!/usr/bin/env python3
# ============================================================================
# touchstone/gen_best_practices.py
#   从 .touchstone/standards.yaml 生成 PR-Agent 的 best_practices.md。
#
# 规范一处定义、分别供两个消费者（见 docs/touchstone-on-pr-agent.html §4 规范注入）：
#   • machine_checkable=true  的规则 → 由 touchstone-rules / contract_check 确定性处理（不进本文件）。
#   • machine_checkable=false 的规则（主观判断）→ 注入 PR-Agent 的 best_practices.md，
#     违反时 PR-Agent 产出一条标 “Organization best practice” 的建议（归一为 convention）。
#
# 用法：
#   python touchstone/gen_best_practices.py                       # 打印到 stdout
#   python touchstone/gen_best_practices.py --out .touchstone/best_practices.md
#   python touchstone/gen_best_practices.py --standards PATH --org "Your Org"
#
# 输出采用 PR-Agent 推荐的 pattern-based 结构（非裸 bullet），按 applies_to 分语言分组。
# 不要手改生成结果——改 standards.yaml 后重新生成（CI 可加校验：生成结果应与提交版一致）。
# ============================================================================

import argparse
import sys

import yaml


def select_subjective(standards):
    """取主观规则（machine_checkable 非真）。可机检规则留给确定性检查，不进 best_practices。"""
    return [r for r in standards.get("rules", []) if not r.get("machine_checkable", False)]


def _scope_label(applies_to):
    if applies_to in ("*", None):
        return "all languages"
    if isinstance(applies_to, list):
        return ", ".join(applies_to)
    return str(applies_to)


def _oneline(s):
    """author_guidance / detect_hint 里含换行，压成一行，便于 pattern 条目。"""
    return " ".join((s or "").split())


def render_best_practices(rules, org="Touchstone"):
    """渲染为 PR-Agent best_practices.md（pattern-based）。按 applies_to 分组（通用 / 各语言）。"""
    out = [
        f"# {org} best practices",
        "",
        "<!-- 本文件由 touchstone/gen_best_practices.py 从 .touchstone/standards.yaml 生成；"
        "请勿手改——改 standards.yaml 后重新生成。 -->",
        "<!-- 仅含主观规则（machine_checkable=false）；可机检规则由 touchstone-rules / contract_check 确定性处理。 -->",
        "",
        "若 PR 代码违反下列任一 pattern，请生成一条建议（将被标为 “Organization best practice”）。"
        "聚焦项目特有判断，不重复通用且 AI 已知的常识。",
        "",
    ]
    universal = [r for r in rules if r.get("applies_to") in ("*", None)]
    scoped = [r for r in rules if r.get("applies_to") not in ("*", None)]
    n = 0

    def emit(subset):
        nonlocal n
        for r in subset:
            n += 1
            out.append(f"Pattern {n} ({r['id']}, applies: {_scope_label(r.get('applies_to'))}): "
                       f"{_oneline(r.get('description'))}")
            if r.get("rationale"):
                out.append(f"- Why: {_oneline(r['rationale'])}")
            if r.get("author_guidance"):
                out.append(f"- Do: {_oneline(r['author_guidance'])}")
            out.append("")

    if universal:
        out += ["## 通用 (all languages)", ""]
        emit(universal)
    if scoped:
        groups = {}
        for r in scoped:
            groups.setdefault(_scope_label(r.get("applies_to")), []).append(r)
        for scope, subset in sorted(groups.items()):
            out += [f"## {scope}", ""]
            emit(subset)
    return "\n".join(out).rstrip() + "\n"


def generate(standards_path, org="Touchstone"):
    with open(standards_path, encoding="utf-8") as f:
        standards = yaml.safe_load(f) or {}
    return render_best_practices(select_subjective(standards), org)


def main():
    ap = argparse.ArgumentParser(prog="gen_best_practices",
                                 description="从 standards.yaml 生成 PR-Agent best_practices.md")
    ap.add_argument("--standards", default=".touchstone/standards.yaml")
    ap.add_argument("--out", default="-", help="输出路径；'-' 表示 stdout（默认）")
    ap.add_argument("--org", default="Touchstone", help="标签前缀（Organization best practice 的 org 名）")
    args = ap.parse_args()
    md = generate(args.standards, args.org)
    if args.out == "-":
        sys.stdout.write(md)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[gen] 写出 {args.out}（{md.count(chr(10))} 行）", file=sys.stderr)


if __name__ == "__main__":
    main()

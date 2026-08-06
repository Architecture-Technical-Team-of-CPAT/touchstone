#!/usr/bin/env python3
# ============================================================================
# touchstone/metrics.py —— 运行指标（运维可观测性）
# ----------------------------------------------------------------------------
# 目的：把"LLM 是否真在工作、评审是否可信、门禁在放行还是拦截"从"靠人事后追问"
# （历史上 PR #44/#47/#48 的静默故障都是人去问"为什么没意见"才挖出来的）变成
# 【每轮主动产出的结构化指标】，供 CI 汇总成 dashboard / 触发告警。
#
# 与 touchstone-findings.json 的区别：findings.json 是给 autonomy 决策用的【本轮完整状态】；
# 本模块产出的 touchstone-metrics.json 是给【运维聚合】用的扁平数值——字段稳定、可跨轮累加、
# 便于 jq/Prometheus/表格直接消费，不含大对象（findings 详情、diff 等）。
#
# 单行 JSON（每轮一条）追加到 metrics 文件，天然是可 tail、可 grep、可聚合的事件流。
# ============================================================================

import json
import sys
import time

from touchstone import __version__
from touchstone.artifacts import artifact_path, ensure_output_dir

# metrics 文件名 + 显式覆盖 env（兼容既有部署 TOUCHSTONE_METRICS_PATH）。路径在 emit/load 内
# 【调用时】解析（与 PR 其它模块一致）——避免 import 后改 TOUCHSTONE_OUTPUT_DIR 致模块级缓存陈旧。
_METRICS_NAME = "touchstone-metrics.json"
_METRICS_OVERRIDE_ENV = "TOUCHSTONE_METRICS_PATH"


def _metrics_path():
    """调用时解析 metrics 路径（TOUCHSTONE_METRICS_PATH 覆盖优先，否则 OUTPUT_DIR/name）。
    pr-agent review #122 r1：原 ``METRICS_PATH`` 模块级求值，import 后改 OUTPUT_DIR 致缓存陈旧、
    metrics 写错位置；改调用时解析与 PR 其它模块对齐。"""
    return artifact_path(_METRICS_NAME, override_env=_METRICS_OVERRIDE_ENV)


def build(pr, sha, risk, findings, *, engine_status, review_reliable,
          ai_raw_count, loop_decision, gate, unverified_claims,
          change_class, added_lines, round_no=None, invoke_meta=None, durations=None):
    """把一轮评审的关键健康信号压成一条扁平指标记录（可 JSON 序列化的纯 dict）。
    invoke_meta：review_provider.invoke_meta()（部分降级/截断修复计数），可选。"""
    meta = invoke_meta or {}
    dur = {k: round(float(v), 2) for k, v in (durations or {}).items()
           if k.startswith("t_")}          # 阶段耗时（上游报告问题四：无耗时数据则无法定位时间去向）
    rule_hits = sum(1 for f in findings if f.get("agent") != "pr-agent")
    ai_hits = sum(1 for f in findings if f.get("agent") == "pr-agent")
    return {
        **dur,
        "ts": int(time.time()),
        "version": __version__,
        "pr": pr,
        "sha": (sha or "")[:12],
        "round": round_no,
        # —— LLM 健康度（静默故障可观测性的核心）——
        "engine_status": engine_status,          # ok / no_engine / provider_failed / llm_failed / skipped_large_diff
        "review_reliable": bool(review_reliable),  # False = 本轮评审不可信（不该被当绿灯）
        "ai_raw_count": ai_raw_count,            # LLM 原始建议数（0 + reliable=False 即静默故障）
        "partial_tool_failure": meta.get("partial_tool_failure"),  # improve/review 单侧失败
        "repaired_parses": meta.get("repaired_parses", 0),         # 截断/畸形被修复解析次数
        # —— 评审产出 ——
        "findings_total": len(findings),
        "findings_rule_based": rule_hits,        # 确定性命中（DANGER/SEC/契约/栈）
        "findings_ai": ai_hits,                  # AI 建议
        # —— 门禁决策 ——
        "risk_band": risk.get("risk_band"),
        "loop_decision": loop_decision,          # converged / continue / escalate ...
        "gate": gate,                            # 总闸状态
        "unverified_claims": unverified_claims,  # author 自证待人核准数（>0 阻止自动放行）
        "change_class": change_class,
        "added_lines": added_lines,
    }


def emit(record, path=None):
    """把一条指标记录以单行 JSON 追加到 metrics 文件（事件流；失败不阻塞主流程）。

    捕获三类失败并全部吞成 False（契约：失败不阻塞主流程）：
    ``OSError``（磁盘/权限）、``TypeError``/``ValueError``（record 含不可 JSON 序列化
    对象——如 invoke_meta 带回非预期类型时 ``json.dumps`` 抛出）。旧版只接 OSError，
    序列化错会穿出去违背契约。留 stderr 痕迹而非静默返 False——可观测性子系统自身
    绝不静默故障（同 learning_loop 2026-07-04 的防静默约定）。"""
    p = path or _metrics_path()
    try:
        ensure_output_dir(p)   # append 是非原子写：OUTPUT_DIR 指向不存在目录时先建父目录（#90）
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError) as e:
        print(f"[warn] metrics.emit 失败: {e}", file=sys.stderr)
        return False


# ---- 聚合（供 doctor / dashboard / 告警消费）--------------------------------
def load(path=None):
    """读取 metrics 事件流为 record 列表（文件不存在返回 []）。"""
    p = path or _metrics_path()
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue        # 跳过损坏行，不让单条坏记录拖垮聚合
    except FileNotFoundError:
        pass    # 静默豁免：遥测文件不存在 = 未开遥测的常态，非故障
    except OSError as e:
        # 文件在但读不动（权限/损坏）→ 聚合会静默缺数据，必须可见。
        print(f"[metrics] 遥测文件读取失败，本次聚合缺数据: {e}", file=sys.stderr)
    return out


def summarize(records):
    """把事件流聚合成运维关心的比率——评审可信率、静默故障率、被拒率、放行率。
    这些比率就是告警阈值的输入（如可信率 < 0.8 应报警）。

    无论 records 是否为空都返回**同一套 schema**（空记录给零值默认）——下游
    监控脚本/告警直接 index rate 字段，若空时只回 ``{"rounds": 0}`` 会 KeyError。
    """
    n = len(records)
    reliable = sum(1 for r in records if r.get("review_reliable"))
    # 【静默故障】= 引擎自报 ok（看着正常）但本轮被判定不可信（false-convergence 守则抓到的）。
    # 注意：llm_failed / provider_failed / no_engine 是引擎【已检测到】的故障（大声报错），
    # 不是静默——把它们算进 silent 会虚高静默指标、误导运维。故这里只数 engine_status=='ok'。
    silent = sum(1 for r in records
                 if not r.get("review_reliable") and r.get("engine_status") == "ok")
    converged = sum(1 for r in records if r.get("loop_decision") == "converged")
    blocked_by_claims = sum(1 for r in records if (r.get("unverified_claims") or 0) > 0)
    engine_dist = {}
    for r in records:
        k = r.get("engine_status", "unknown")
        engine_dist[k] = engine_dist.get(k, 0) + 1
    # 单侧失败轮数（fan-out 下 improve/review 之一挂、另一侧仍可信）：
    # 这类 partial 失败既不算 silent（engine_status=='ok'）也不进 engine_status_dist 的 llm_failed
    # （那是"全部已跑工具都挂"）——没有专属计数时，"improve 连挂数日、review 正常"会在聚合趋势里隐身
    # （review_reliable_rate 仍高、engine_status_dist 显示 ok），正是 run-285 那种盲区。故单列计数。
    improve_degraded = sum(1 for r in records if r.get("partial_tool_failure") == "improve")
    review_degraded = sum(1 for r in records if r.get("partial_tool_failure") == "review")
    return {
        "rounds": n,
        "review_reliable_rate": round(reliable / n, 3) if n else 0.0,   # 越高越好；< 0.8 值得排查
        "silent_failure_rounds": silent,                     # 疑似静默故障轮数
        "converged_rate": round(converged / n, 3) if n else 0.0,
        "blocked_by_unverified_claims": blocked_by_claims,   # 被 author 自证闸拦下的轮数
        "engine_status_dist": engine_dist,                   # 引擎状态分布（诊断入口）
        "improve_degraded_rounds": improve_degraded,         # improve 单侧失败轮（review 仍可信）
        "review_degraded_rounds": review_degraded,           # review 单侧失败轮（improve 仍可信）
    }


if __name__ == "__main__":       # python -m touchstone.metrics [metrics.json] → 打印聚合摘要
    import sys
    recs = load(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(summarize(recs), ensure_ascii=False, indent=2))

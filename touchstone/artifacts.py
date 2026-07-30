#!/usr/bin/env python3
# ============================================================================
# touchstone/artifacts.py —— 统一产物路径解析
# ----------------------------------------------------------------------------
# 问题：产物文件（touchstone-findings.json / calibration.json / verify-result.json
# / metrics / 各类 .md 报告）此前散落在 7 个模块，各自硬编码相对文件名直接写 CWD，且
# 已出现约定分裂（metrics.py 用 TOUCHSTONE_METRICS_PATH、govern.py 用 CALIBRATION_JSON
# 各自为政）。后果：
#   ① 作为可安装工具在客户机器跑，产物落在调用者当前目录（可能是任意工作目录）；
#   ② 同机并发评审多个 PR 时，同名产物互相覆盖（findings/calibration 无 PR 隔离）。
#
# 方案：单一环境变量 TOUCHSTONE_OUTPUT_DIR 作为产物根目录。
#   • 默认 "."（CWD）——与旧行为完全一致，现有 touchstone.yml 不设此变量、
#     upload-artifact 仍从 workspace 根找文件，GitHub Actions 路径【零破坏】。
#   • 本地/可安装/并发场景显式设 TOUCHSTONE_OUTPUT_DIR（如 /tmp/ts-pr-123）即获得
#     隔离，多 PR 各写各的目录，不再覆盖。
#
# 关键不变量：同一份产物的【读方与写方必须走同一解析】。findings 由 orchestrator 写、
# 由 checks/autonomy 读，分处不同 job 但共享 workspace——两侧都经 artifact_path()
# 解析同一 OUTPUT_DIR，才能在跨 job 传递中对齐。故所有读写点统一改走本模块。
#
# 兼容既有细粒度覆盖：metrics/calibration/govern 原有的单文件 env（TOUCHSTONE_METRICS_PATH
# 等）作为【高优先级显式覆盖】保留——若设了绝对/相对具体路径，尊重之，不再拼 OUTPUT_DIR；
# 未设才回落到 OUTPUT_DIR/<默认名>。这样既统一了默认约定，又不破坏已依赖旧 env 的部署。
# ============================================================================

import os


def output_dir():
    """产物根目录。默认 CWD（与旧行为一致），TOUCHSTONE_OUTPUT_DIR 可改。"""
    return os.environ.get("TOUCHSTONE_OUTPUT_DIR", ".")


def artifact_path(name, override_env=None):
    """解析产物 name 的落盘路径。
    override_env：可选的单文件覆盖 env 名（兼容既有 TOUCHSTONE_METRICS_PATH 等）。
      设了该 env → 直接用其值（绝对或相对 CWD，调用方自负），不拼 OUTPUT_DIR；
      未设 → OUTPUT_DIR/name。
    仅拼路径，不建目录（写入侧的 atomicio.atomic_write_* 会自建父目录）。"""
    if override_env:
        explicit = os.environ.get(override_env)
        if explicit:
            return explicit
    d = output_dir()
    if d in ("", "."):
        return name            # 保持相对文件名原样（与旧 open("x.json") 字节级一致）
    return os.path.join(d, name)


def ensure_output_dir(path):
    """确保 ``path`` 的父目录存在。供【非原子写】（append / 第三方流式写）在 OUTPUT_DIR
    指向不存在目录时不 ``FileNotFoundError``——这正是 OUTPUT_DIR feature「设目录隔离」的
    核心用例。原子写（``atomicio.atomic_write_*``）已自建父目录，无需调用本函数。

    path 为已解析路径（``artifact_path()`` 的返回值亦可）。目录已存在是 no-op（exist_ok）；
    裸文件名（无父目录，如默认 OUTPUT_DIR="." 返回的 "x.json"）安全跳过。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

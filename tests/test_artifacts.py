#!/usr/bin/env python3
# ============================================================================
# tests/test_artifacts.py —— 产物路径统一解析行为锁
# ----------------------------------------------------------------------------
# 锁：① 默认（不设 OUTPUT_DIR）返回裸文件名——与旧 open("x.json") 字节级一致，保证
# GitHub Actions 路径零破坏；② 设 OUTPUT_DIR 后拼进该目录（并发隔离）；③ override_env
# 显式覆盖优先（兼容既有 TOUCHSTONE_METRICS_PATH/CALIBRATION_JSON）；④ 读写配对一致
# （orchestrator 写、checks/autonomy 读同一份 findings，必须解析到同一路径）。
# ============================================================================

import os

from touchstone.artifacts import artifact_path, ensure_output_dir, output_dir


def test_default_is_cwd_bare_name(monkeypatch):
    monkeypatch.delenv("TOUCHSTONE_OUTPUT_DIR", raising=False)
    # 默认必须返回裸文件名（不是 "./x"），与历史 open("touchstone-findings.json") 完全一致
    assert artifact_path("touchstone-findings.json") == "touchstone-findings.json"
    assert output_dir() == "."


def test_explicit_dot_still_bare(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", ".")
    assert artifact_path("calibration.json") == "calibration.json"


def test_output_dir_joined(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", "/tmp/ts-pr-123")
    assert artifact_path("touchstone-findings.json") == "/tmp/ts-pr-123/touchstone-findings.json"


def test_override_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", "/tmp/ignored")
    monkeypatch.setenv("TOUCHSTONE_METRICS_PATH", "/var/custom/m.json")
    # override_env 设了具体路径 → 用它，不拼 OUTPUT_DIR
    assert artifact_path("touchstone-metrics.json",
                         override_env="TOUCHSTONE_METRICS_PATH") == "/var/custom/m.json"


def test_override_env_unset_falls_back_to_output_dir(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", "/tmp/out")
    monkeypatch.delenv("CALIBRATION_JSON", raising=False)
    assert artifact_path("calibration.json",
                         override_env="CALIBRATION_JSON") == "/tmp/out/calibration.json"


def test_read_write_pairing_same_path(monkeypatch):
    """orchestrator 写 / checks 读同一份 findings：给定同一 OUTPUT_DIR，两侧解析必须一致。"""
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", "/tmp/ts-pr-77")
    write_side = artifact_path("touchstone-findings.json")   # orchestrator/checks 写
    read_side = artifact_path("touchstone-findings.json")    # checks/autonomy 读
    assert write_side == read_side == "/tmp/ts-pr-77/touchstone-findings.json"


def test_isolation_two_prs_distinct_paths(monkeypatch):
    """并发两个 PR 各设不同 OUTPUT_DIR → 产物路径互不覆盖（本补丁要解决的核心问题）。"""
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", "/tmp/pr-1")
    p1 = artifact_path("touchstone-findings.json")
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", "/tmp/pr-2")
    p2 = artifact_path("touchstone-findings.json")
    assert p1 != p2


def test_end_to_end_write_read_isolation(monkeypatch, tmp_path):
    """写-读闭环隔离：设 OUTPUT_DIR 到隔离目录，atomic_write_json 落该目录，
    artifact_path 解析回同一路径读回——证明产物不落 CWD、跨读写点对齐。"""
    from touchstone.atomicio import atomic_write_json
    import json
    out = tmp_path / "pr-999"
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", str(out))
    # 写方（如 orchestrator/checks）
    atomic_write_json(artifact_path("touchstone-findings.json"), {"pr": 999, "gate": "success"})
    # 产物落在隔离目录、不在 CWD
    assert (out / "touchstone-findings.json").exists()
    assert not (tmp_path / "touchstone-findings.json").exists()
    # 读方（如 autonomy/checks）解析到同一路径读回
    doc = json.load(open(artifact_path("touchstone-findings.json"), encoding="utf-8"))
    assert doc == {"pr": 999, "gate": "success"}


# ============ round-2 闭环锁：OUTPUT_DIR 指向不存在目录时非原子写不崩 ============
def test_ensure_output_dir_creates_missing_nested_parent(tmp_path):
    """ensure_output_dir 建 path 的父目录（供 append 等非原子写在 OUTPUT_DIR 指向
    不存在目录时不 FileNotFoundError——feature「设 OUTPUT_DIR 隔离」核心用例）。
    已存在的目录是 no-op；裸文件名（无父目录）安全跳过。"""
    target = str(tmp_path / "a" / "b" / "c" / "out.json")
    assert not os.path.exists(os.path.dirname(target))
    ensure_output_dir(target)
    assert os.path.isdir(os.path.dirname(target))
    ensure_output_dir(target)            # 二次调用（已存在）no-op，不抛
    ensure_output_dir("bare.json")       # 裸文件名（dirname="")安全跳过


def test_metrics_append_creates_nonexistent_output_dir(tmp_path, monkeypatch):
    """metrics.emit 是非原子 append（事件流追加，不能走 atomic_write 的自建父目录）。
    设 OUTPUT_DIR 指向不存在目录时，emit 须先 ensure_output_dir 建目录，否则
    open(p,'a') FileNotFoundError——返回 False、丢指标（#90 finding metrics.py:72）。
    变异（去掉 ensure_output_dir）：open 抛 OSError 被吞成 False → 断言 is True 杀红。"""
    import touchstone.metrics as M
    out = tmp_path / "isolated"          # 不存在
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", str(out))
    p = artifact_path("touchstone-metrics.json")
    assert M.emit({"ts": 1, "pr": "x"}, path=p) is True
    assert (out / "touchstone-metrics.json").exists()

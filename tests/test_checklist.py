# ============================================================================
# tests/test_checklist.py —— checklist 快照的原子写与旁路语义（商用审计 P2-3）
# ============================================================================
def test_snapshot_atomic_and_failure_returns_none(tmp_path, monkeypatch):
    # P2-3：快照走 atomicio（半文件毁校准回放）；失败仍返回 None（旁路不阻塞主链）
    import json as _json
    import touchstone.checklist as CL
    p = tmp_path / "checklist-round-1.json"
    cl = {"round": 1, "items": []}
    assert CL.snapshot(cl, str(p)) == str(p)
    assert _json.loads(p.read_text(encoding="utf-8")) == cl      # 落盘完整可解析
    def _boom(path, obj):
        raise OSError("disk full")
    monkeypatch.setattr(CL, "atomic_write_json", _boom)
    assert CL.snapshot(cl, str(tmp_path / "x.json")) is None


def test_norm_sig_strips_legacy_none_line_segment():
    """legacy 迁移（PRA-REVIEW round-3）：v2 前 sig_of 在 line=None 时产 `rule:file:None`，
    v2 改为省略行段（`rule:file`）。_norm_sig 中心化剥除 `:None` 行段后缀——使旧 marker/ack
    的 `:None` sig 与新 sig 在 reconcile 时可匹配（否则旧项 orphan、永远销不掉）。

    安全边界：只剥三段格式（count(':')>=2）的 `:None` 后缀（即行段=None）；不误伤 file=None
    的 `rule:None`（只一段冒号、不剥，保留 file 段标记）。"""
    import touchstone.checklist as CL
    assert CL._norm_sig("R:a.py:None") == "R:a.py"             # legacy 行段 None → 剥除
    assert CL._norm_sig("R:a.py:None\n") == "R:a.py"           # 脏空白 + legacy None 一起处理
    assert CL._norm_sig("R:a.py:5") == "R:a.py:5"             # 正常行号不动
    assert CL._norm_sig("R:a.py") == "R:a.py"                 # v2 干净 sig 不动
    assert CL._norm_sig("R:None") == "R:None"                 # file=None（一段冒号）不剥——保留 file 标记
    assert CL._norm_sig("") == ""                             # 空串安全

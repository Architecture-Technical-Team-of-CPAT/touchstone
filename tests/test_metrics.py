# ============================================================================
# tests/test_metrics.py —— 运行指标（运维可观测性，商用化 P0-3.2）
# ============================================================================
import json

from touchstone import metrics as M


def _rec(reliable=True, engine="ok", ai=2, decision="continue", claims=0):
    risk = {"risk_band": "high"}
    findings = ([{"agent": "contract"}] + [{"agent": "pr-agent"}] * ai) if ai else []
    return M.build(42, "deadbeef1234", risk, findings,
                   engine_status=engine, review_reliable=reliable, ai_raw_count=ai,
                   loop_decision=decision, gate="2/3", unverified_claims=claims,
                   change_class="code", added_lines=100)


def test_build_flat_serializable():
    r = _rec()
    json.dumps(r)                                    # 必须可序列化
    assert r["review_reliable"] is True and r["engine_status"] == "ok"
    assert r["findings_rule_based"] == 1 and r["findings_ai"] == 2
    assert r["sha"] == "deadbeef1234" and "version" in r


def test_emit_and_load_roundtrip(tmp_path):
    p = tmp_path / "m.json"
    assert M.emit(_rec(), path=str(p)) and M.emit(_rec(reliable=False), path=str(p))
    recs = M.load(str(p))
    assert len(recs) == 2


def test_metrics_path_resolved_lazily_post_import(monkeypatch, tmp_path):
    """metrics 路径须在 emit/load【调用时】解析——import 后再设 TOUCHSTONE_OUTPUT_DIR 须生效，
    不能用模块级缓存。pr-agent review #122 r1：原 ``METRICS_PATH`` 模块级求值，import 后改 env
    致缓存陈旧、metrics 写错位置；改调用时解析与 PR 其它模块对齐。锁死：import 完再设 OUTPUT_DIR，
    emit（不传 path）落到新目录、load（不传 path）读得回。"""
    out = tmp_path / "late-dir"                       # import 时还不存在、env 也未设
    monkeypatch.delenv("TOUCHSTONE_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("TOUCHSTONE_METRICS_PATH", raising=False)
    # M 已在模块顶 import（早于下面 setenv）——若用模块级缓存，此刻 _metrics_path() 应是 import 时的值
    monkeypatch.setenv("TOUCHSTONE_OUTPUT_DIR", str(out))
    assert M.emit(_rec()) is True                     # 调用时解析 → 落 import 后才设的 OUTPUT_DIR
    written = out / "touchstone-metrics.json"
    assert written.exists()
    recs = M.load()                                   # load 同样调用时解析、读得回
    assert len(recs) == 1 and recs[0]["pr"] == 42


def test_emit_catches_serialization_error(tmp_path, capsys):
    """record 含不可 JSON 序列化对象时，emit 必须【吞成 False 不抛】——契约承诺
    '失败不阻塞主流程'。旧版只接 OSError，json.dumps 的 TypeError/ValueError 会穿出去
    违背契约。锁死：返 False、stderr 有留痕（防静默故障）、不写半行坏数据。"""
    p = tmp_path / "m.json"
    bad = {"ts": 0, "oops": {1, 2, 3}}            # set 不可 JSON 序列化 → TypeError
    assert M.emit(bad, path=str(p)) is False       # 不抛、返 False
    assert "metrics.emit" in capsys.readouterr().err   # 留痕，不静默
    assert p.read_text(encoding="utf-8") == ""      # dumps 失败先于 write，未落坏数据


def test_load_skips_corrupt_lines(tmp_path):
    p = tmp_path / "m.json"
    p.write_text('{"ok":1}\n{bad json\n{"ok":2}\n', encoding="utf-8")
    assert len(M.load(str(p))) == 2                  # 坏行跳过，不拖垮聚合


def test_summarize_rates():
    recs = [_rec(reliable=True, decision="converged"),         # 收敛轮（engine ok）
            _rec(reliable=False, engine="ok", ai=0),           # 静默故障：engine 报 ok 却不可信
            _rec(reliable=True, claims=1)]                     # 被自证闸拦（engine ok）
    s = M.summarize(recs)
    assert s["rounds"] == 3
    assert s["review_reliable_rate"] == round(2 / 3, 3)
    assert s["silent_failure_rounds"] == 1
    assert s["blocked_by_unverified_claims"] == 1
    assert s["engine_status_dist"] == {"ok": 3}


def test_summarize_detected_failure_is_not_silent():
    """llm_failed / provider_failed 是引擎【已检测到】的故障，不算静默——只有
    engine_status=='ok' 却 review_reliable=False 才算（false-convergence 守则抓的）。
    锁死 silent 计数不把这些大声报错的状态计入，避免虚高静默指标误导运维。"""
    recs = [_rec(reliable=False, engine="llm_failed", ai=0),
            _rec(reliable=False, engine="provider_failed", ai=0)]
    s = M.summarize(recs)
    assert s["silent_failure_rounds"] == 0
    assert s["review_reliable_rate"] == 0.0
    assert s["engine_status_dist"]["llm_failed"] == 1
    assert s["engine_status_dist"]["provider_failed"] == 1


def test_summarize_empty():
    """空记录也必须返回完整 schema（零值默认）——下游监控/告警直接 index rate 字段，
    若空时只回 {"rounds":0} 会 KeyError。"""
    s = M.summarize([])
    assert s["rounds"] == 0
    assert s["review_reliable_rate"] == 0.0
    assert s["silent_failure_rounds"] == 0
    assert s["converged_rate"] == 0.0
    assert s["blocked_by_unverified_claims"] == 0
    assert s["engine_status_dist"] == {}


def test_build_carries_round_no():
    """round_no 必须透传到 record['round']。orchestrator 曾因 `round_no=(loop_info and None)`
    笔误（loop_info 是 tuple、恒返回 None）致该字段恒为 null，可观测性失真——此测试锁死
    build 不丢 round_no（修复见 orchestrator.py metrics.emit 调用处）。"""
    r = M.build(42, "deadbeef1234", {"risk_band": "high"}, [],
                engine_status="ok", review_reliable=True, ai_raw_count=0,
                loop_decision="converged", gate="2/3", unverified_claims=0,
                change_class="code", added_lines=10, round_no=7)
    assert r["round"] == 7


def test_telemetry_read_oserror_warns_missing_silent(tmp_path, capsys):
    # P2-1：遥测文件【缺失】= 未开遥测常态静默；【在但读不动】= 聚合缺数据必须可见
    import touchstone.metrics as M
    assert M.load(str(tmp_path / "nope.jsonl")) == []
    assert "遥测文件读取失败" not in capsys.readouterr().err
    unreadable = tmp_path / "dir-as-file.jsonl"
    unreadable.mkdir()                                   # 目录冒充文件 → IsADirectoryError ⊂ OSError
    assert M.load(str(unreadable)) == []
    assert "遥测文件读取失败" in capsys.readouterr().err

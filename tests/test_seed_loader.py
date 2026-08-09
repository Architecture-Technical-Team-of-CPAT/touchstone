# tests/test_seed_loader.py —— .touchstone/seeds.yaml 加载器纯函数契约
import os
import textwrap

from touchstone import seed_loader


def _seed_path(repo_dir):
    return os.path.join(repo_dir, ".touchstone", "seeds.yaml")


def _write(repo_dir, body):
    p = _seed_path(repo_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


def test_no_file_returns_empty(tmp_path):
    assert seed_loader.load_seed_injection(str(tmp_path)) == ""


def test_empty_file_returns_empty(tmp_path):
    _write(str(tmp_path), "")
    assert seed_loader.load_seed_injection(str(tmp_path)) == ""


def test_empty_list_returns_empty(tmp_path):
    _write(str(tmp_path), "[]")
    assert seed_loader.load_seed_injection(str(tmp_path)) == ""


def test_valid_emphasize_and_suppress(tmp_path):
    _write(str(tmp_path), """
        - finding_type: PRA-ERROR-SWALLOW
          kind: emphasize
          text: Flag empty catch blocks.
        - finding_type: PRA-NIT
          kind: suppress
          text: Skip formatting nits.
    """)
    out = seed_loader.load_seed_injection(str(tmp_path))
    assert "Team seed rules" in out
    assert "[PRA-ERROR-SWALLOW] Prioritize surfacing: Flag empty catch blocks." in out
    assert "[PRA-NIT] Do not raise: Skip formatting nits." in out


def test_stack_filter_seed_with_matching_stack_included(tmp_path):
    _write(str(tmp_path), """
        - finding_type: PRA-X
          kind: emphasize
          stack: python
          text: py only
    """)
    out = seed_loader.load_seed_injection(str(tmp_path), stack="python")
    assert "PRA-X" in out


def test_stack_filter_seed_with_nonmatching_stack_excluded(tmp_path):
    _write(str(tmp_path), """
        - finding_type: PRA-X
          kind: emphasize
          stack: python
          text: py only
    """)
    out = seed_loader.load_seed_injection(str(tmp_path), stack="go")
    assert out == ""                                # 唯一一条被栈过滤掉 → 空


def test_stack_no_field_applies_to_all_stacks(tmp_path):
    """没标 stack 的种子对所有栈生效（通用规范）。"""
    _write(str(tmp_path), """
        - finding_type: PRA-UNIVERSAL
          kind: emphasize
          text: applies everywhere
    """)
    for st in ("python", "go", "rust", "anything"):
        assert "PRA-UNIVERSAL" in seed_loader.load_seed_injection(str(tmp_path), stack=st)


def test_malformed_yaml_returns_empty(tmp_path):
    _write(str(tmp_path), "not: valid: yaml: [\n")
    assert seed_loader.load_seed_injection(str(tmp_path)) == ""


def test_top_level_dict_returns_empty(tmp_path):
    _write(str(tmp_path), """
        finding_type: PRA-X
        kind: emphasize
        text: wrong shape
    """)
    assert seed_loader.load_seed_injection(str(tmp_path)) == ""


def test_bad_items_skipped_individually_good_kept(tmp_path):
    """格式不对的条目逐条跳过、不整体失败；合法条目保留。"""
    _write(str(tmp_path), """
        - finding_type: PRA-GOOD
          kind: emphasize
          text: keep
        - finding_type: PRA-BAD
          kind: weird
          text: bad kind
        - text: no finding_type
        - finding_type: PRA-NOTEXT
          kind: emphasize
        - "plain string item"
        - 42
    """)
    out = seed_loader.load_seed_injection(str(tmp_path))
    assert "PRA-GOOD" in out
    assert "PRA-BAD" not in out
    assert "PRA-NOTEXT" not in out
    assert "plain string" not in out
    # 只剩一条合法
    lines = [l for l in out.splitlines() if l.startswith("- [")]
    assert len(lines) == 1


def test_kind_case_insensitive(tmp_path):
    _write(str(tmp_path), """
        - finding_type: PRA-A
          kind: EMPHASIZE
          text: upper
        - finding_type: PRA-B
          kind: Suppress
          text: mixed
    """)
    out = seed_loader.load_seed_injection(str(tmp_path))
    assert "PRA-A" in out and "Prioritize surfacing" in out
    assert "PRA-B" in out and "Do not raise" in out


def test_text_and_finding_type_required(tmp_path):
    """缺 text 或 finding_type 的条目跳过（空 text 也算缺）。"""
    _write(str(tmp_path), """
        - finding_type: PRA-A
          kind: emphasize
          text: "  "
        - kind: emphasize
          text: no ftype
    """)
    assert seed_loader.load_seed_injection(str(tmp_path)) == ""


def test_stack_non_string_does_not_crash(tmp_path, capsys):
    """stack 传非 str（int 等）不抛 AttributeError——round-1 review 防御；
    round-3 review：非 str 当作"不过滤"（fail-open，注入全部）；round-4 review：另打 [warn] 辅助排查。"""
    _write(str(tmp_path), """
        - finding_type: PRA-X
          kind: emphasize
          stack: python
          text: keep
    """)
    # 合法"不过滤"（None / 空串）→ 无 warn
    seed_loader.load_seed_injection(str(tmp_path), stack=None)
    assert "[warn]" not in capsys.readouterr().err
    seed_loader.load_seed_injection(str(tmp_path), stack="")
    assert "[warn]" not in capsys.readouterr().err
    # 合法 str → 无 warn
    seed_loader.load_seed_injection(str(tmp_path), stack="python")
    assert "[warn]" not in capsys.readouterr().err
    # 非 str（int/list/对象）→ fail-open 注入 + [warn] 标记 caller bug
    for bad in (123, ["python"], object()):
        out = seed_loader.load_seed_injection(str(tmp_path), stack=bad)
        assert "PRA-X" in out                          # fail-open：种子照注入
        err = capsys.readouterr().err
        assert "[warn]" in err and "非 str" in err     # 但打 warn 辅助排查


def test_text_length_capped(tmp_path):
    """text 字段长度封顶（限 prompt 注入面）——超长被截断到 MAX_TEXT。"""
    long_text = "x" * 1000
    _write(str(tmp_path), f"""
        - finding_type: PRA-LONG
          kind: emphasize
          text: {long_text}
    """)
    out = seed_loader.load_seed_injection(str(tmp_path))
    # 截断到 500（MAX_TEXT）；不出现完整 1000 字
    assert "PRA-LONG" in out
    assert "x" * 500 in out
    assert "x" * 501 not in out


def test_finding_type_length_capped(tmp_path):
    """finding_type 也封顶（round-2 review：限注入面一致）——超长截断到 MAX_TYPE=80 整体长度。"""
    long_type = "PRA-" + "Y" * 200      # 远超 MAX_TYPE=80
    _write(str(tmp_path), f"""
        - finding_type: {long_type}
          kind: emphasize
          text: keep
    """)
    out = seed_loader.load_seed_injection(str(tmp_path))
    # 截断后 ftype 整体 80 字符 = "PRA-"(4) + "Y"*76
    import re
    m = re.search(r"\[(PRA-Y+)\]", out)
    assert m and len(m.group(1)) == 80   # 整体 80
    assert "Y" * 77 not in out           # 不超过 cap（去掉 PRA- 前缀后 76 个 Y）


def test_repo_dir_normalized(tmp_path):
    """repo_dir 带尾斜杠或 .. 分量也能正确定位（round-2 review：abspath 归一化）。"""
    _write(str(tmp_path), """
        - finding_type: PRA-X
          kind: emphasize
          text: keep
    """)
    # 尾斜杠不影响定位
    assert "PRA-X" in seed_loader.load_seed_injection(str(tmp_path) + "/")
    # 含 .. 的路径归一化后定位到同一文件（父目录 + subdir/..）
    parent = os.path.dirname(str(tmp_path))
    rel = os.path.join(parent, os.path.basename(str(tmp_path)), "..", os.path.basename(str(tmp_path)))
    assert "PRA-X" in seed_loader.load_seed_injection(rel)


def test_stack_non_string_yaml_value_skipped(tmp_path):
    """YAML 里 stack 是 list/dict（非 str）→ str() 产 "['python']" 永不匹配；round-2 review
    要求跳过这类格式不对的条目，而非静默丢。"""
    _write(str(tmp_path), """
        - finding_type: PRA-GOOD
          kind: emphasize
          text: keep this
        - finding_type: PRA-LIST-STACK
          kind: emphasize
          stack: ["python"]
          text: list stack should be skipped
        - finding_type: PRA-DICT-STACK
          kind: emphasize
          stack: {lang: python}
          text: dict stack should be skipped
    """)
    out = seed_loader.load_seed_injection(str(tmp_path), stack="python")
    assert "PRA-GOOD" in out
    assert "PRA-LIST-STACK" not in out
    assert "PRA-DICT-STACK" not in out



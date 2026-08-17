"""skill 文档与实现不漂移：SKILL.md 里的示例与语义必须被真实解析器认可。
SKILL.md 是销项协议的权威文档（agent 照它办事）；它说谎 = agent 按错误协议申报。
本测试把文档里的 ack 示例喂给 parse_acks/reconcile，断言文档描述的行为成立。"""
import os
import re

from touchstone import checklist as ck


def _skill_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "skills", "touchstone-ack", "SKILL.md")


def _skill_text():
    return open(_skill_path(), encoding="utf-8").read()


def test_skill_file_exists_with_frontmatter():
    m = re.match(r"^---\n(.*?)\n---\n", _skill_text(), re.S)
    assert m, "SKILL.md 缺 frontmatter（name/description）"
    assert "name: touchstone-ack" in m.group(1)
    assert "description:" in m.group(1)


def test_skill_ack_examples_parse_with_real_parser():
    """文档示例 ack 块必须被 _ACK_BLOCK/_ACK_LINE 真实解析（动词 done/waived/split、
    note 非空）——示例与解析器漂移时此处红，文档即失去权威性。"""
    text = _skill_text()
    # 文档里示例在四反引号围栏内嵌三反引号块；取所有 touchstone-ack 围栏块
    blocks = re.findall(r"```touchstone-ack\s*\n(.*?)```", text, re.S)
    assert blocks, "SKILL.md 未包含 touchstone-ack 示例块"
    acks = ck.parse_acks(["```touchstone-ack\n" + blocks[0] + "```"])
    assert any(v["verb"] == "done" for v in acks.values()), "示例须含 done"
    assert any(v["verb"] == "waived" for v in acks.values()), "示例须含 waived"
    assert any(v["verb"] == "split" for v in acks.values()), "示例须含 split"
    for sig, v in acks.items():
        assert v["note"], f"{sig} 的示例须带 note（waived 无理由不受理）"


def test_skill_semantics_match_implementation():
    """文档语义表与 VERIFIED/CLAIMED 集合一致（防文档宣称与销项判据加固脱节）。"""
    text = _skill_text()
    assert "done" in ck.VERIFIED
    assert ck.CLAIMED == {"waived", "split"}
    assert "`done`" in text and "`waived`" in text and "`split`" in text
    # 文档必须写明 done 的复检语义（下轮签名不再命中才落 done）
    assert "复检" in text
    # 反博弈语义必须写明（全 waived 不触发收敛）
    assert "不触发收敛" in text or "不阻塞收敛" in text


def test_skill_inline_comments_not_counted_is_stated():
    """核心事实（PR 级评论计数、行内线程不计数）必须出现在文档开头。"""
    head = _skill_text()[:1500]
    assert "行内" in head and "PR 级评论" in head

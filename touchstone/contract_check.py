#!/usr/bin/env python3
# ============================================================================
# touchstone/contract_check.py  ——  提交契约一致性核对（设计 §4.1）
# ----------------------------------------------------------------------------
# 落实"索引而非凭据"：契约的每个声明都被【独立核对】，对不上即视为发现。
# 全确定性（无 LLM、置信=1.0），产出的发现并入委员会发现池后一同 aggregate。
# 三项核对：
#   scope 越界          → diff 改到 scope 外的文件        → SCOPE-001
#   claimed tests 不实  → 声称加测试但 diff 无测试文件改动 → TEST-001
#   reused 不实         → 声称复用但新增代码无对应引用     → DUP-001（疑似重复造轮子）
# ============================================================================

import fnmatch
import os
import re
import sys

from unidiff import PatchSet


_PARSE_WARNING = None   # 最近一次 parse_diff 的告警：diff 解析失败时置位，orchestrator 读取后写进评审（防静默故障）。
# 单线程评审路径下安全；解析成功会清空。


def parse_diff(diff_text):
    """返回 (changed_files:set[str], added_lines:dict[file -> list[(lineno, text)]])。
    用成熟库 unidiff 解析，稳妥处理重命名/合并/边界等长尾（替代早期手写状态机）。
    解析失败时返回空、并把原因写进模块级 _PARSE_WARNING——调用方（orchestrator）据此在评审里
    显式标注"确定性核对未生效"，而不是让 0 条发现被读成"干净"（防静默故障）。"""
    global _PARSE_WARNING
    files, added = set(), {}
    try:
        patch = PatchSet(diff_text or "")
    except Exception as e:      # 含 UnidiffParseError；unidiff 对部分畸形输入会抛库内
        # UnboundLocalError 等非契约异常——同样按解析失败处理，防评审主链被打断。
        _PARSE_WARNING = f"diff 解析失败（{e}）：确定性契约/栈核对未生效，本次仅含 AI 评审（若有）"
        return files, added
    _PARSE_WARNING = None
    for pf in patch:
        if pf.is_removed_file:               # 整文件删除(+++ /dev/null) 不计入变更文件
            continue
        path = pf.path
        files.add(path)
        for hunk in pf:
            for line in hunk:
                if line.is_added:
                    added.setdefault(path, []).append(
                        (line.target_line_no, (line.value or "").rstrip("\n")))
    return files, added


def _finding(rule_id, file, line, category, rationale, fix, rule_index,
             confidence=1.0, severity=None):
    rule = rule_index.get(rule_id, {})
    # 显式 severity 优先，否则取规则 severity；被 govern 固化(enforced)的一律升为 block_candidate
    sev = severity or rule.get("severity", "warn")
    if rule.get("enforced"):
        sev = "block_candidate"
    return {
        "rule_id": rule_id, "file": file, "line": line, "category": category,
        "severity": sev,
        "confidence": confidence,        # 确定性核对默认 1.0；纯启发式可下调
        "rationale": rationale, "agent": "contract-check",
        # 修订设计 §4.2（评审意见 1、2）：方向+依据+达成判据。
        # 确定性来源的 fix 文本本就是方向性描述；依据即 rationale 指回的规则事实。
        "fix_direction": fix,
        "fix_reasoning": rationale,
        # 确定性判据：规则复检——下一轮该 rule_id 不再命中即销项（机器可复核）。
        "done_criteria": {"kind": "deterministic", "spec": {"recheck": rule_id}},
        "suggested_fix": fix,            # 已废弃字段的过渡别名（=fix_direction，不含补丁），供旧消费方
    }


def _match_any(path, globs):
    return any(fnmatch.fnmatch(path, g) for g in globs)


def check_scope(files, scope, rule_index):
    # 跳过模板占位符（以 '<' 开头，如未填的 pr.yaml 里 "<path/glob…>"）——
    # 与 check_tests 同构：占位符不算真实声明，否则会对每个文件刷假阳性 SCOPE-001。
    scope = [s for s in (scope or []) if s and not str(s).startswith("<")]
    if not scope:
        return []
    return [_finding("SCOPE-001", f, 0, "scope_creep",
                     "改动文件不在提交契约声明的 scope 内（疑似越界）",
                     f"将 {f} 的改动拆到独立 PR，或在 scope 中显式声明", rule_index)
            for f in sorted(files) if not _match_any(f, scope)]


def check_tests(files, tests_added, rule_index):
    claimed = [t for t in (tests_added or []) if t and not str(t).startswith("<")]
    if not claimed:
        return []
    test_files = {f for f in files if "test" in f.lower() or "spec" in f.lower()}
    if not test_files:
        return [_finding("TEST-001", claimed[0], 0, "weak_test",
                         "提交契约声称新增测试，但 diff 中无测试文件改动（申报不实）",
                         "实际补充有意义断言的测试，或如实更新 tests_added", rule_index)]
    return []


def _identifiers(text):
    """从代码文本抽出标识符集合：完整点链 + 叶子名。供精确成员匹配（非子串）。"""
    out = set()
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_.]+", text):
        out.add(t)
        out.add(t.split(".")[-1])
    return out


def check_reuse(added, reused, rule_index):
    claims = [c for c in (reused or []) if c and not str(c).startswith("<")]
    if not claims:
        return []
    # 精确成员匹配：避免子串误碰（如声明 get_profile 被 get_profile_v2 假性命中）。
    # 残留：分词不剥注释/字符串，名字出现在注释里仍算命中——属 advisory 误差，跨语言精确剥离不划算。
    present = _identifiers("\n".join(t for lines in added.values() for _, t in lines))
    out = []
    for claim in claims:
        toks = re.findall(r"[A-Za-z_][A-Za-z0-9_.]+", str(claim))
        names = {tok for t in toks for tok in (t, t.split(".")[-1])}
        if names and not (names & present):
            out.append(_finding("DUP-001", "(diff)", 0, "duplication",
                                 f"提交契约声称复用「{claim}」，但新增代码中未见对应引用"
                                 "（申报不实/疑似重复造轮子）",
                                 "确认确实复用既有能力并在代码中引用，或如实更新 reused_components",
                                 rule_index))
    return out


_CODE_EXT = {".java", ".kt", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".scala"}


def _is_test(p):
    return "test" in p.lower() or "spec" in p.lower()


def check_untested_code(files, rule_index):
    """纯 diff 事实检查（不依赖 manifest）：改动含代码文件但 diff 中无任何测试文件。
    陈述事实(置信高)、严重度仅 warn——是否真需要测试由委员会/人判。"""
    code = [f for f in sorted(files)
            if os.path.splitext(f)[1] in _CODE_EXT and not _is_test(f)]
    tests = [f for f in files if _is_test(f)]
    if code and not tests:
        return [_finding("TEST-001", code[0], 0, "weak_test",
                         "改动包含代码文件，但 diff 中无任何测试文件改动（疑似缺测试）",
                         "为本次改动补充有意义断言的测试；若确无需测试请在 PR 说明缘由",
                         rule_index, confidence=0.9, severity="warn")]
    return []


# --- SEC-001：硬编码密钥/凭据（离线、确定性正则扫描）----------------------------
# 规则集【冻结】：只作离线兜底，不再新增模式——沿此路线扩张终点是维护一个更差的
# gitleaks。完整密钥扫描请经 checks.yaml 的 relay 检查挂 gitleaks/semgrep（见主设计 §4.7）。
# 高精度特征串（已知格式的云/Git 凭据 + PEM 私钥头 + 通用凭据赋值）。
# SEC-002（SQL/命令注入）是污点分析、需外部 SAST 数据流，不在此——经 checks.yaml relay 接入。
_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"aws[_-]?secret[_-]?(?:access[_-]?key)?\s*[:=]\s*['\"][A-Za-z0-9/+=]{40}['\"]", re.I),
     "AWS secret access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{59,}"), "GitHub fine-grained PAT"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "PEM 私钥"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9]{40,}\b"), "OpenAI API key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{40,}\b"), "OpenAI legacy key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bsk_live_[A-Za-z0-9]{24}\b"), "Stripe secret key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "JWT token"),
    (re.compile(r"(?:api[_-]?key|secret|token|passwd|password|pwd)\b\s*[:=]\s*['\"]"
                r"([A-Za-z0-9_\-+/=]{16,})['\"]", re.I), "硬编码凭据赋值"),
]
# 占位符/示例值：命中则跳过，压低误报（确定性扫描宁可漏不误拦）。
_PLACEHOLDER = re.compile(
    r"(example|sample|changeme|changed?|placeholder|todo|xxxx|<[^>]+>|your[_-]?\w*"
    r"|_here\b|redacted|test|dummy|fake|replace[_-]?me)", re.I)


def check_secrets(added, rule_index):
    """SEC-001：扫新增行里的硬编码密钥/凭据（确定性、离线）。仅当 SEC-001 在册且 machine_checkable 时生效。
    产 finding(agent=contract-check, category=security, severity 取自规则=block_candidate) → 自动进确定性门禁。"""
    rule = rule_index.get("SEC-001", {})
    if not rule.get("machine_checkable"):
        return []
    out = []
    for path, lines in added.items():
        if _is_test(path):
            continue            # 测试文件里的密钥是故意夹具，不据此阻断（真实泄密仍由外部 SAST 兜底）
        for lineno, text in lines:
            for pat, label in _SECRET_PATTERNS:
                m = pat.search(text)
                if not m:
                    continue
                matched = m.group(1) if m.groups() else m.group(0)
                if _PLACEHOLDER.search(matched):       # 示例/占位值 → 跳过
                    continue
                out.append(_finding("SEC-001", path, lineno, "security",
                                    f"疑似硬编码凭据（{label}）—— 安全红线，凭据应走密钥管理",
                                    "将凭据移至环境变量/密钥管理服务；代码中勿入硬编码凭据",
                                    rule_index))
                break                                   # 一行最多报一条
    return out


# --- DANGER-001：危险代码构造（eval/exec/shell 注入/os.system/pickle）--------------
# 与 SEC-001 同构（正则扫新增行、跳测试文件、category=security），但：
#   • 不走 _PLACEHOLDER 过滤——这些是【构造本身】危险，非"值像凭据"，没有占位符概念；
#   • severity=warn（规则侧）、confidence=0.9——构造有少量合法用途，不一刀切 block，只升 high+full_suite；
#   • 定位是 SEC-002（污点注入，需 SAST 数据流）的廉价前置兜底：构造出现即风险，不等数据流证实。
# 已知小误报：注释/docstring 里这些构造的【调用形】字面也会命中（同 SEC-001 不区分注释）——warn 级，人复核即消。
# 不扫 os.exec*（进程替换，非任意 Python 求值）、yaml.load（需判 Loader 参数，正则不可靠）——留 SEC-002/SAST。
# 注意：本块的标签/注释刻意不写裸调用字面（如 eval 后跟括号），否则扫描器会 FP 自身源码；
# 规则的权威字面清单见 .touchstone/standards.yaml 的 DANGER-001 描述。
_DANGER_PATTERNS = [
    (re.compile(r"\beval\s*\("), "eval 求值，任意代码执行"),
    (re.compile(r"\bexec\s*\("), "exec 执行，任意代码执行"),
    (re.compile(r"\bshell\s*=\s*True\b"), "subprocess 启用 shell，注入面"),
    (re.compile(r"\bos\.system\s*\("), "os.system 调用，命令注入"),
    (re.compile(r"\bpickle\.loads?\s*\("), "pickle 反序列化，RCE"),
]


def check_danger_patterns(added, rule_index):
    """DANGER-001：扫新增行里的危险代码构造（确定性、离线）。与 check_secrets 同构：仅当 DANGER-001
    在册且 machine_checkable 时生效；跳过测试文件（与 SEC-001 一致——测试夹具里的 eval 不进生产）。
    产 finding(agent=contract-check, category=security, severity 取自规则=warn, confidence=0.9)
    → security 类别进 map_verdict 升 high+full_suite：LLM 空回/误判时仍兜住，关上"非 java 坏代码全押 LLM"窗口。"""
    rule = rule_index.get("DANGER-001", {})
    if not rule.get("machine_checkable"):
        return []
    out = []
    for path, lines in added.items():
        if _is_test(path):
            continue            # 测试文件里的危险构造是夹具/演示，不据此抬风险（生产代码仍扫）
        for lineno, text in lines:
            for pat, label in _DANGER_PATTERNS:
                if pat.search(text):
                    out.append(_finding("DANGER-001", path, lineno, "security",
                                        f"危险代码构造（{label}）—— 构造存在即高风险，"
                                        "即便参数看似可信也应改用安全替代",
                                        "用安全替代（ast.literal_eval/subprocess 列表参数/json 替 pickle）"
                                        "；确需使用须在 PR 说明可信边界",
                                        rule_index, confidence=0.9))
                    break                               # 一行最多报一条
    return out


# 本引擎支持的 pr.yaml schema 版本。与 touchstone 软件版本（__version__，SemVer）正交：
# schema_version 只在 pr.yaml 字段结构/语义 breaking change 时 bump（如改必填字段、改字段
# 含义），由 schema_version_warning 在运行时核对——未知版本告警但不阻断（向前兼容）。
SCHEMA_VERSION = "0.1"


def schema_version_warning(contract):
    """pr.yaml 的 schema_version 与引擎支持版本不匹配时的告警串（无问题返回 None）。

    schema_version 标记 pr.yaml 的字段结构版本（非软件版本）。touchstone 软件升 release
    不改 schema（0.1.0→0.2.2 一直是 0.1）；只有 pr.yaml 字段定义 breaking change 时才 bump。
    本函数让这个解耦关系在运行时显式：
      - 缺失 schema_version → 默认按当前 schema 处理（向前兼容，旧 yaml 无此字段）
      - == SCHEMA_VERSION → 无事
      - != SCHEMA_VERSION → 告警：可能字段不兼容，提示升级 TOUCHSTONE_ENGINE_REF
    不进 findings（schema 不匹配不是 author 能修的契约违规，而是 engine/配置版本错配；
    进 checklist 会污染 author 的销项工作）。调用方（run.py）据此打 stderr 告警。"""
    sv = (contract or {}).get("schema_version")
    if sv is None:
        return None                    # 未声明→默认按当前 schema 处理（向前兼容）
    if sv != SCHEMA_VERSION:
        return (f"pr.yaml schema_version={sv!r} 超出本引擎支持的 {SCHEMA_VERSION!r}，"
                f"字段可能不兼容——升级 TOUCHSTONE_ENGINE_REF 到匹配此 schema 的 touchstone "
                f"版本，或在 pr.yaml 里改回 schema_version: {SCHEMA_VERSION!r}")
    return None


def check_contract_consistency(diff_text, contract, rule_index):
    """确定性核对契约三项声明（需 manifest）+ 无 manifest 也能跑的纯 diff 事实检查 + SEC-001 密钥扫描
    + DANGER-001 危险代码构造扫描。"""
    contract = contract or {}
    files, added = parse_diff(diff_text)
    return (check_scope(files, contract.get("scope"), rule_index)
            + check_tests(files, contract.get("tests_added"), rule_index)
            + check_reuse(added, contract.get("reused_components"), rule_index)
            + check_untested_code(files, rule_index)
            + check_secrets(added, rule_index)
            + check_danger_patterns(added, rule_index))


# ============================================================================
# 范围事实 ScopeFacts（修订设计 §4.1，评审意见 7）
# ----------------------------------------------------------------------------
# 由确定性工具直接从 diff 计算出的修改范围事实：不依赖任何声明、不经过任何模型。
# 是继「硬门禁结果」「经验知识库」之后的第三个客观锚：
#   - changed_files / totals → 报告确定性事实区的修改范围概览（给人第一眼）
#   - sensitive_hits         → 按 .touchstone/scope-rules.yaml 路径规则点亮影响面（不信 LLM 类别）
#   - fingerprint            → 内容指纹，供轮次台账（lineage）同源比对（评审意见 10）
# 解析失败沿用 _PARSE_WARNING 防静默故障约定：parse_ok=False 时下游必须显式标注。
# ============================================================================

import hashlib

# 内置路径规则缺省值：与 review_provider._DET_BLAST_PATTERNS 语义对齐（正则）。
# 仓库可用 .touchstone/scope-rules.yaml 覆盖/扩展（human_curated，与 acceptance.yaml 同级待遇）。
_DEFAULT_SCOPE_RULES = {
    "cross_module_contract": [
        r"(^|/)migrations?/", r"\.sql$", r"\.proto$", r"\.graphql$", r"\.avsc$", r"\.thrift$",
        r"(^|/)schema[./]", r"schema\.\w+$", r"openapi", r"swagger",
    ],
    "security_surface": [
        r"(^|/)(auth|oauth|iam|security|crypto|secrets?|credentials?)([/_.]|$)",
        r"(password|keystore|private[_-]?key)",
    ],
}


def load_scope_rules(repo_dir="."):
    """读 .touchstone/scope-rules.yaml（factor -> [正则] 映射；缺省用内置默认）。
    用户配置按 factor 整体替换（避免与默认合并产生歧义），未提及的 factor 保留默认。"""
    import yaml
    path = os.path.join(repo_dir, ".touchstone", "scope-rules.yaml")
    rules = {k: list(v) for k, v in _DEFAULT_SCOPE_RULES.items()}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for factor, pats in (data.get("factors") or {}).items():
            if isinstance(pats, list) and pats:
                rules[str(factor)] = [str(p) for p in pats]
    except FileNotFoundError:
        pass    # 静默豁免：可选配置，缺省用内置默认是常态（见 docstring），非故障
    except (OSError, yaml.YAMLError) as e:
        # 文件【在】但读不动/YAML 损坏 → 回落内置默认。必须可见：静默回落会让
        # blast radius 分级在配置损坏时不知不觉变粗（防静默故障）。
        print(f"[contract_check] scope-rules 加载失败，回落内置默认: {e}", file=sys.stderr)
    return rules


def _fileset_hash(paths):
    return hashlib.sha256("\n".join(sorted(paths)).encode("utf-8")).hexdigest()[:16]


def scope_facts(diff_text, scope_rules=None):
    """纯规则产出范围事实（ScopeFacts，修订设计 §4.1）。不调用任何模型。

    返回 dict：
      changed_files[]  每项 {path, added, deleted, hunks:[[start, added, deleted],…]}
      sensitive_hits[] 每项 {path, rule}：命中哪条路径规则（factor 名）
      totals           {files, added, deleted}
      fingerprint      {fileset:[…], shape:{path:[added,deleted]}, fileset_hash}
                       fileset/shape 保留原始值供台账做相似度比对（哈希无法比对部分相似）
      parse_ok / parse_warning  防静默故障：解析失败时下游必须显式标注「确定性核对未生效」
    """
    global _PARSE_WARNING
    rules = scope_rules or _DEFAULT_SCOPE_RULES
    out = {"changed_files": [], "sensitive_hits": [], "totals": {"files": 0, "added": 0, "deleted": 0},
           "fingerprint": {"fileset": [], "shape": {}, "fileset_hash": ""},
           "parse_ok": True, "parse_warning": ""}
    try:
        patch = PatchSet(diff_text or "")
    except Exception as e:      # 含 UnidiffParseError；unidiff 对部分畸形输入会抛库内
        # UnboundLocalError 等非契约异常——同样按解析失败处理（防静默故障约定不变）。
        out["parse_ok"] = False
        out["parse_warning"] = f"diff 解析失败（{e}）：范围事实未生效"
        return out
    # 按 path 聚合：unidiff 会把 `diff --git` 头解析成一个零 hunk 的幻影条目，
    # 与随后的 ---/+++ 条目同路径——不聚合会把同一文件计成两条。
    by_path = {}
    for pf in patch:
        entry = by_path.setdefault(pf.path, {"path": pf.path, "added": 0, "deleted": 0, "hunks": []})
        for hunk in pf:
            h_add = sum(1 for l in hunk if l.is_added)
            h_del = sum(1 for l in hunk if l.is_removed)
            entry["added"] += h_add
            entry["deleted"] += h_del
            entry["hunks"].append([hunk.target_start or hunk.source_start or 0, h_add, h_del])
    for entry in by_path.values():
        out["changed_files"].append(entry)
        out["totals"]["added"] += entry["added"]
        out["totals"]["deleted"] += entry["deleted"]
        low = entry["path"].lower()
        for factor, pats in rules.items():
            if any(re.search(p, low) for p in pats):
                out["sensitive_hits"].append({"path": entry["path"], "rule": factor})
    out["totals"]["files"] = len(out["changed_files"])
    fileset = sorted(f["path"] for f in out["changed_files"])
    out["fingerprint"] = {
        "fileset": fileset,
        "shape": {f["path"]: [f["added"], f["deleted"]] for f in out["changed_files"]},
        "fileset_hash": _fileset_hash(fileset),
    }
    return out

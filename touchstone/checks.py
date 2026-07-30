#!/usr/bin/env python3
# ============================================================================
# touchstone/checks.py —— 可插拔"必须通过"检查框架
# ----------------------------------------------------------------------------
# 设计要点：
#   • 策略全在 .touchstone/checks.yaml（挂哪些检查、哪几个 required、阈值）——不散在
#     GitHub 设置里；改"哪个必须绿"只改这个文件。
#   • 对外只发【一个】总闸状态(默认 touchstone/gate)：当且仅当所有 required 且启用
#     的检查都通过时为 success。GitHub 那边只需一次性要求这一个状态即可（人点合并场景）。
#   • 三种插件：
#       builtin —— 进程内函数（如 touchstone 自带确定性规则、verify 深检）
#       relay   —— 读某个【已有】GitHub check-run 的结论（工具在自己的 CI 里跑过）
#       service —— POST PR 上下文到一个 HTTP 服务，拿回结果（未来自建服务的挂点）
#   • Touchstone 不发明关卡：质量保障来自现成工具/未来服务，这里只提供挂载与汇总。
# ============================================================================

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml

from touchstone import ghclient
from touchstone.atomicio import atomic_write_json
from touchstone.artifacts import artifact_path

DEFAULT_GATE = "touchstone/gate"
_RELAY_OK = {"success", "neutral", "skipped"}
_BUILTINS: dict = {}  # name -> fn(pr_ctx, cfg) -> (passed: bool|None, summary: str)
# service 类检查慢（HTTP POST 到外部服务）、彼此独立、且不抢 GitHub token 限流 → 并行跑。
# 并发上限避免一堆 service 同时打爆外部端点；builtin（瞬时）/relay（吃 token）仍串行。
_MAX_SERVICE_WORKERS = 8


def builtin(name):
    """注册一个内置检查插件。fn 返回 (passed, summary)；passed=None 表示中性/跳过。"""
    def deco(fn):
        _BUILTINS[name] = fn
        return fn
    return deco


class CheckResult:
    def __init__(self, name, passed, summary="", required=False):
        self.name = name
        self.passed = passed          # True 通过 / False 失败 / None 中性·跳过·未知
        self.summary = summary
        self.required = required


# ---- 配置 -------------------------------------------------------------------
def load_config(repo_dir):
    path = os.environ.get("TOUCHSTONE_CHECKS",
                          os.path.join(repo_dir, ".touchstone", "checks.yaml"))
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}                       # 未配置：合法空策略（不挡）
    except (yaml.YAMLError, OSError) as e:
        # 文件存在但解析失败/不可读（坏 YAML、权限拒、路径指向目录、损坏符号链接等）= 配置坏了：
        # 不能当成"空策略"静默放行（防静默故障）。标 _config_error，post_gate 据此 fail-closed 并在
        # 总闸 summary 显式报警。旧 bare `except OSError: data={}` 把这类静默降级成空策略→gate success，
        # 与 YAMLError 的 fail-closed 处理自相矛盾。FileNotFoundError 在前先捕获，不会落到这里。
        data = {"_config_error": f"checks.yaml 不可读或解析失败（{e}）——按 fail-closed 处理，请修正配置"}
    data.setdefault("gate", {}).setdefault("status_name", DEFAULT_GATE)
    data.setdefault("checks", [])
    return data


# ---- 各类插件运行器 ---------------------------------------------------------
def _gh(pr, method, path):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return ghclient.request(method, base + path, pr["token"])


def _run_relay(pr, cfg):
    """读某个已有 check-run 的结论（工具在自己的 CI 跑过，这里只转达）。"""
    src = cfg.get("source_check")
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    data = ghclient.paginate_check_runs(
        base + f"/repos/{pr['owner']}/{pr['repo']}/commits/{pr['sha']}/check-runs", pr["token"])
    runs = [r for r in (data.get("check_runs") or []) if r.get("name") == src]
    if not runs:
        return None, f"未找到检查 {src}"
    if any(r.get("status") != "completed" for r in runs):
        return None, f"{src} 未完成"
    # required 的接力检查 fail-closed：只有 success 算过——否则 author 让源 CI 跳过
    # （[skip ci]/路径过滤/条件）即可绿总闸，自动合并下会放行未经验证的代码。
    # 非 required 保持宽松（neutral/skipped 视为过，兼容既有流水线）；
    # 个别 required 检查确需放宽时，在 checks.yaml 里对该检查设 allow_skipped: true。
    ok_set = {"success"} if (cfg.get("required") and not cfg.get("allow_skipped")) else _RELAY_OK
    bad = [r for r in runs if r.get("conclusion") not in ok_set]
    return (not bad), f"{src}=" + ",".join(r.get("conclusion") or "?" for r in runs)


def _truthy(v):
    """把 service 返回的 passed 字段归一为布尔（fail-closed）。

    service（外部 HTTP 服务 / shell 脚本）常把布尔写成字符串——`bool('false') == True` 会把
    「失败」误判为「通过」（required service 假放行总闸）。字符串按真值白名单 {'true','1','yes','on'}
    （大小写无关）判；其余类型走 `bool()`（与旧行为一致：bool/int/None 不变）。非白名单字符串
    （'ok' / 'passed' 等畸形值）→ False：门禁对模糊输入 fail-closed，不凭 lenient truthiness 放行。"""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v)


def _run_service(pr, cfg):
    """POST PR 上下文到一个 HTTP 服务（未来自建质量服务的挂点）。"""
    r = requests.post(cfg["url"], json={
        "owner": pr["owner"], "repo": pr["repo"], "sha": pr["sha"],
        "files": pr.get("files", [])}, timeout=cfg.get("timeout", 60))
    r.raise_for_status()
    d = r.json()
    return _truthy(d.get("passed")), str(d.get("summary", ""))


def _run_builtin(pr, cfg):
    fn = _BUILTINS.get(cfg.get("plugin", cfg.get("name")))
    if fn is None:
        return None, f"未注册的内置插件 {cfg.get('plugin', cfg.get('name'))}"
    return fn(pr, cfg)


_RUNNERS = {"builtin": _run_builtin, "relay": _run_relay, "service": _run_service}


# ---- 编排：跑检查 → 汇总总闸 → 发一个状态 -----------------------------------
def _run_one(pr, cfg):
    """跑单个 check 配置 → CheckResult。
    插件隔离：runner 抛任何异常都记中性（passed=None），不拖垮总闸计算。抽出来是为了让
    service 类能在线程池里并行复用同一段隔离逻辑（单检查失败不波及其余）。"""
    name = cfg.get("name", "?")
    required = bool(cfg.get("required", False))
    runner = _RUNNERS.get(cfg.get("type", "builtin"))
    if runner is None:
        return CheckResult(name, None, f"未知插件类型 {cfg.get('type')}", required)
    try:
        passed, summary = runner(pr, cfg)
    except Exception as e:        # 插件隔离：单个插件失败不拖垮总闸计算，记为中性
        passed, summary = None, f"插件异常: {e}"
    return CheckResult(name, passed, summary, required)


def run_checks(config, pr):
    """跑所有启用的检查 → 按 checks.yaml 的【配置顺序】返回 CheckResult 列表。
    service 类（慢、打外部服务、彼此独立、不抢 GitHub token 限流）并行；builtin（瞬时）
    /relay（吃 token 限流）保持串行。结果一律按配置顺序回填（非执行顺序），故 post_gate 的
    summary 行序与 aggregate_gate 的判定与旧串行实现完全一致——并行只压墙钟、不改可观测行为。"""
    cfgs = [c for c in config.get("checks", []) if c.get("enabled", True)]
    by_idx: dict[int, CheckResult] = {}
    service_idx: list[int] = []
    for i, cfg in enumerate(cfgs):
        if cfg.get("type") == "service":
            service_idx.append(i)            # 慢 + 打外部服务 → 攒一批并行
        else:
            by_idx[i] = _run_one(pr, cfg)    # builtin 瞬时 / relay 吃 token 限流 → 串行
    if service_idx:
        max_workers = min(len(service_idx), _MAX_SERVICE_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_to_idx = {ex.submit(_run_one, pr, cfgs[i]): i for i in service_idx}
            for fut in as_completed(fut_to_idx):
                by_idx[fut_to_idx[fut]] = fut.result()   # _run_one 内已 catch 全部异常 → 不抛
    return [by_idx[i] for i in range(len(cfgs))]          # 配置顺序回填


def aggregate_gate(results):
    """总闸：所有【required】检查都必须 passed=True；任一 required 非通过 → 总闸 failure。
    非 required 的结果只作信息展示，不影响总闸。无 required 检查 → success（空策略不挡）。"""
    required = [r for r in results if r.required]
    if any(r.passed is not True for r in required):
        return "failure"
    return "success"


def post_gate(pr, config, results):
    """把汇总后的总闸发成【一个】GitHub check-run；明细列在 summary 里。"""
    name = config["gate"]["status_name"]
    mark = {True: "✓", False: "✗", None: "–"}
    lines = [f"{mark[r.passed]} {r.name}{'（必须）' if r.required else ''}: {r.summary}"
             for r in results]
    # 配置解析失败 → fail-closed（不能静默当空策略放行），并在 summary 顶部报警（防静默故障）
    cfg_err = config.get("_config_error")
    if cfg_err:
        gate = "failure"
        lines.insert(0, f"⚠️ {cfg_err}")
    else:
        gate = aggregate_gate(results)
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    ghclient.request("POST", base + f"/repos/{pr['owner']}/{pr['repo']}/check-runs",
                     pr["token"], data={
                         "name": name, "head_sha": pr["sha"], "status": "completed",
                         "conclusion": gate,
                         "output": {"title": f"Touchstone 总闸：{gate}",
                                    "summary": "\n".join(lines) or "（无启用的检查）"}})
    return gate, results


# ---- 内置插件：touchstone 自带的确定性规则 -----------------------------------
@builtin("touchstone-rules")
def _check_touchstone_rules(pr, cfg):
    """通过 = 确定性检查（contract-check + touchstone-rules）无拦截级发现。
    拦截级 = severity == block_candidate（含被 enforce 固化升级的）或 category == contract。
    severity 由各检查器按规则 severity 计算：block_candidate 规则立即拦截，warn 规则仅 enforced 后拦截。"""
    findings = pr.get("contract_findings") or []
    block = [f for f in findings
             if f.get("severity") == "block_candidate" or f.get("category") == "contract"]
    if block:
        ids = ",".join(sorted({f.get("rule_id", "?") for f in block}))
        return False, f"确定性规则拦截：{ids}"
    return True, f"{len(findings)} 条建议、无拦截级"


# ---- 内置插件：verify 正确性深检（默认关；算力够时在 checks.yaml 里开）----------
@builtin("verify")
def _check_verify(pr, cfg):
    """折入 verify 深检结果：verify_change 作为独立/按需 job 跑、写 verify-result.json，
    本插件只把它的结论折进总闸。未跑则记中性（不挡）。"""
    import json
    # 与写出方（verify_change.py:449 artifact_path("verify-result.json")）对齐：设了
    # TOUCHSTONE_OUTPUT_DIR 时读写都落隔离目录；旧硬编码 "verify-result.json" 只在 CWD 找，
    # OUTPUT_DIR 非空时读方找不到结果文件、静默记 "verify 未运行" 把 verify 结论漏出总闸
    # （#90 round-1 finding checks.py:190）。artifact_path 在 OUTPUT_DIR="." 时原样返回文件名，
    # 默认场景字节级不变；result_file 为绝对路径时 os.path.join 仍尊重之。
    path = artifact_path(cfg.get("result_file", "verify-result.json"))
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, "verify 未运行（无结果文件）"
    if not isinstance(d, dict):
        # verify-result.json 攻击者可影响（零密 job 跑 PR 代码产出）：非对象 JSON
        # （数组/标量/字符串）会让 d.get 崩插件——按"未产出有效结论"中性 + 明示处理
        # （fail-closed：required 时总闸 fail），与本函数 _truthy 的畸形值纪律一致。
        return None, "verify 结果非对象（疑篡改/格式错），按未通过处理"
    # 可信绿：author 自报规格(author_proposed)的绿不构成正确性认证，不算通过（此规则由 autonomy.floor 搬来）
    # passed 走 _truthy 而非 bool()：verify-result.json 由执行 PR 代码的零密 job 产出，内容
    # 攻击者可影响（见 SECURITY.md 信任边界）——畸形字符串（"ok"/"passed" 等，bool() 恒真）
    # 必须 fail-closed 判 False，与本文件 _run_service 的 _truthy 纪律一致。
    passed = _truthy(d.get("passed")) and d.get("spec_source") != "author_proposed"
    return passed, f"verify passed={d.get('passed')} spec={d.get('spec_source')}"


# ---- CLI：独立发总闸（CI 的 gate job 在 touchstone(+ 可选 verify) 之后聚合并发布）----
def main():
    """读 touchstone-findings.json（+ 若 verify 跑过则有 verify-result.json）→ 跑检查
    → 发对外那【一个】总闸 → 把最终结论写回 touchstone-findings.json（供 autonomy 读到含 verify 的总闸）。
    产物路径经 artifact_path 解析：默认 CWD（Action 场景），设 TOUCHSTONE_OUTPUT_DIR 时落隔离目录。"""
    import json
    try:
        with open(artifact_path("touchstone-findings.json"), encoding="utf-8") as f:
            co = json.load(f)
    except (OSError, ValueError):
        # findings 缺失 = touchstone job 没产出结果（崩溃/被取消/artifact 下载失败）。
        # 不能静默 no-op：否则 PR 要么看起来"没事"，要么 required 总闸凭空消失且无说明。
        # 用 workflow 透传的 head sha 发一个明确的 failure check-run 说明情况（防静默故障）。
        owner, _, name = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
        sha = os.environ.get("TOUCHSTONE_HEAD_SHA") or os.environ.get("GITHUB_SHA")
        msg = ("评审流水线未产出结果（touchstone-findings.json 缺失）——"
               "touchstone job 失败/被取消或 artifact 下载失败，总闸无法计算。请重跑或查看 touchstone job 日志。")
        print(f"[gate] {msg}")
        if sha and os.environ.get("GITHUB_TOKEN"):
            try:
                base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
                gate_name = load_config(os.environ.get("REPO_DIR", "."))["gate"]["status_name"]
                ghclient.request("POST", base + f"/repos/{owner}/{name}/check-runs",
                                 os.environ["GITHUB_TOKEN"], data={
                                     "name": gate_name, "head_sha": sha, "status": "completed",
                                     "conclusion": "failure",
                                     "output": {"title": "Touchstone 总闸：评审流水线未产出结果",
                                                "summary": "⚠️ " + msg}})
            except Exception as e:
                print(f"[gate] 无法发布'未产出结果' check-run: {e}", file=sys.stderr)
        return
    owner, _, name = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
    findings = co.get("findings", [])
    pr = {"owner": owner, "repo": name, "sha": co.get("sha"),
          "token": os.environ.get("GITHUB_TOKEN", ""),
          "files": co.get("changed_files", []),
          # 确定性发现 = contract-check（含 SEC-001 密钥）+ touchstone-rules（CTR/SPR/JAVA）
          "contract_findings": [f for f in findings
                                if f.get("agent") in ("contract-check", "touchstone-rules")]}
    cfg = load_config(os.environ.get("REPO_DIR", "."))
    gate, _ = post_gate(pr, cfg, run_checks(cfg, pr))
    co["gate"] = gate
    # 原子：这份含总闸结论的 findings 是 autonomy decide_auto_merge 的直接入参，半文件不可接受
    atomic_write_json(artifact_path("touchstone-findings.json"), co)
    print(f"[gate] 总闸={gate}（已写回 touchstone-findings.json）")


if __name__ == "__main__":
    main()

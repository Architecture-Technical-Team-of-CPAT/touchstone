# Touchstone 部署指南（客户版）

> 面向对象：在**你自己的仓库**上把 Touchstone 装起来、稳定跑起来的部署/运维人员，不要求了解内部实现。
> 与 `RUNBOOK.md` 的区别：RUNBOOK 是作者在本仓自测/dogfooding 的操作手册；本文是**客户环境从零到上线**的落地路径。
> 适用版本：**v0.1.0**。升级前请对照本仓 `CHANGELOG.md`。

Touchstone 是一个 **PR 评审门禁**：对每个 PR 跑评审（复用 PR-Agent 产出建议）+ 确定性契约核对，给出风险分流与 advisory；准入由质量门禁/总闸决定。它**不替代**你的人评审，而是把可机检的问题前置拦住、把评审信号沉淀成指标。

---

## 1. 前提

- **Python 3.11+**，能 `pip install`。
- 一个 **GitHub 仓库**（github.com 或企业版 GHE 均可）。
- 一个 **OpenAI 兼容的 LLM 端点**（供 PR-Agent 评审用；端点在 `.touchstone/pr-agent.yaml` 配置，不经环境变量）。
- 若你的机器经代理访问外网：先配好 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`（代理没配好会 407 或静默挂起，见 §7）。

## 2. 安装

推荐用 `constraints.txt` 锁定一组**已验证可跑**的具体版本，保证客户环境与团队一致、可复现：

```bash
git clone <你的 touchstone 仓库地址>
cd touchstone
git checkout v0.1.0
pip install -e . -c constraints.txt
touchstone --version        # 应打印 touchstone 0.1.0
```

`pyproject.toml` 声明的是**兼容范围**（如 `openai>=1.30,<3`）；`constraints.txt` 锁的是**具体版本**。升级依赖的纪律见 §9。

## 3. 必配 / 建议配的环境变量

Touchstone 有几个「不设就撞默认坑」的配置——**装完先跑 §4 的 `doctor`，它会逐项告诉你哪些没配、后果是什么**，不必死记下表。

| 变量 | 必需 | 说明 |
|---|---|---|
| `GITHUB_TOKEN` | **是** | 取 PR、回贴评论/check。CI 里用 `secrets.GITHUB_TOKEN`。 |
| `TOUCHSTONE_LLM_CONTEXT_TOKENS` | 强烈建议 | LLM **输入侧上下文窗口**，按模型卡填。过小 → 大 PR 的 diff 被裁空致 0 建议（PR#44 类）或被端点超窗拒绝（PR#47 类）；不设回退 32768。 |
| `TOUCHSTONE_PRAGENT_TIMEOUT` | 建议 | PR-Agent 子进程超时（秒）。慢模型（如 glm）单轮 360s+，建议 ≥ 360，CI 里常设 3600（PR#48 类）。 |
| `GITHUB_API_URL` / `GITHUB_GRAPHQL_URL` | GHE 才需 | 企业版 GitHub 的 API 在 `https://<host>/api/v3`、GraphQL 在 `/api/graphql`。 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_TEST_MODEL` | 仅 verify 用 | **评审本身不需要**（评审走 PR-Agent）。仅在启用可选的 verify（独立验收测试）时配；`LLM_TEST_MODEL` 应与评审模型**异源**，避免同源盲点。 |

仓内配置放在 `.touchstone/`：`standards.yaml`（评审规范/规则）、`pr.yaml`（提交契约）、`pr-agent.yaml`（PR-Agent 端点）、`scope-rules.yaml`、`checks.yaml`。把 `.touchstone/` 提交进你的仓库即可被 checkout 读取。

## 4. 部署前自检（**上线门**）

装完、配完，先跑健康度自检。这一步把「配置齐 + 端点通 + 评审引擎真能跑通产出裁决」一次性验掉，用**退出码**告诉你能不能上线：

```bash
touchstone doctor              # 全检：配置 + 连通 + 一次自检评审，红绿报告
touchstone doctor --no-net     # 离线场景：只检配置 + 自检评审
touchstone doctor --json       # 机器可读（接 CI 门 / 运维聚合）
```

读法：`✓` 通过、`⚠` 警告（可降级/可事后补，不拦门）、`✗` 阻断（必须修）。**退出 0 = 可上线；退出 1 = 有阻断项。** 其中「自检评审」阶段跑的是离线确定性裁决链（证明引擎能产出裁决）；LLM 端点能不能连由「连通性」阶段单独报——两块都绿才算真健康。

（`python -m touchstone.preflight` 是更轻的子集，只到连通性；`doctor` 是它的超集，多跑一次自检评审。）

## 5. 首次试跑（dry-run → 回贴）

```bash
# 只读，不动 PR：打印评审结果，确认无误
touchstone --repo <owner/name> --pr <PR编号>

# 确认后回贴 advisory 评论/check
touchstone --repo <owner/name> --pr <PR编号> --post
```

对不属于你的外部 PR，默认 dry-run 不回贴；`--post` 才动它。

## 6. 接入 CI（GitHub Actions）

仓库已带 `.github/workflows/touchstone.yml`，链路为：**touchstone（评审+风险，不拦截）→（按需）verify_plan → verify_execute（执行 PR 代码，零凭据）→ gate（统一发那一个总闸）→（可选）auto_merge**。

把 §3 的变量配到仓库 **Secrets / Variables**：`GITHUB_TOKEN` 自动注入；`TOUCHSTONE_LLM_CONTEXT_TOKENS`、`TOUCHSTONE_LLM_OUTPUT_TOKENS` 等按需设为 secret。

安全要点（默认已如此，勿改松）：workflow 顶层 `permissions: contents: read`；执行不可信 PR 代码的 `verify_execute` job **零凭据**、权限降到 `contents:read`——PR 代码既无 secret 可窃，拿到 token 也伪造不了总闸。详见 `SECURITY.md`。

## 7. 运维可观测

每轮评审产出结构化指标，CI 上传为 artifact `touchstone-metrics.json`（可信率、静默故障轮数、放行率、引擎状态分布、自证拦截数）。聚合查看：

```bash
python -m touchstone.metrics touchstone-metrics.json
```

这是把「LLM 静默故障靠人追问才发现」变成「主动可见、可告警」的抓手——建议把可信率、被拒率接你的监控/告警。

### 告警（可选，默认关）

在 metrics 之上，Touchstone 可把关键信号**主动投递**到你自己的渠道。**默认不外呼**（只保留 metrics artifact）；要开启设 `TOUCHSTONE_ALERT_ENABLED=true`。目标全由你配、进你自己的渠道，**不回传任何第三方**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TOUCHSTONE_ALERT_ENABLED` | 关 | 总开关；非 `true` 则不外呼 |
| `TOUCHSTONE_ALERT_CHANNELS` | `github-issue,github-pr-comment` | 通道集。GitHub 原生复用同一 `GITHUB_TOKEN`、**不出外网**，内网/断网首选 |
| `TOUCHSTONE_ALERT_WEBHOOK` | — | 设了则加 webhook 通道，POST 告警 JSON（企业微信/钉钉/自建；该 URL 须从 runner 可达） |
| `TOUCHSTONE_ALERT_RELIABLE_MIN` | 0.8 | 评审可信率低于此 → 告警 |
| `TOUCHSTONE_ALERT_SILENT_MAX` | 0 | 静默故障轮数超过此 → 告警 |

触发规则：单轮高危（静默故障 / 引擎降级 / author 自证待核准）→ 贴对应 **PR 评论**；滚动聚合（可信率过低 / 持续静默故障）→ 开或更新一个带 `touchstone-alert` label 的**跟踪 Issue**（去重防刷屏）。告警投递失败**不影响评审**（可观测性不当门禁）。纯内网客户走 GitHub 原生即可，公网 webhook 连不通就别配。

### 使用遥测（可选，默认关）

可把每轮 metrics 记录**上报到一个你指定的中心汇聚点**（"touchstone 统一的地方"），供跨部署观察健康趋势。**默认不上报**——不配 `TOUCHSTONE_TELEMETRY_ENDPOINT` 就一个字节都不外发。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TOUCHSTONE_TELEMETRY_ENDPOINT` | — | 汇聚点 URL。**不配=禁用**。可指向厂商中心 collector，**也可指向你自己的内网聚合点** |
| `TOUCHSTONE_TELEMETRY_TOKEN` | — | 可选 bearer（collector 鉴权） |
| `TOUCHSTONE_TELEMETRY_DEPLOYMENT_ID` | `unset` | 部署标识（站点/租户），供中心按部署分桶 |
| `TOUCHSTONE_TELEMETRY_ANONYMIZE` | 关 | `true` → 连 PR 号/sha 也抹掉，只留健康数值 |

**透明声明——发什么 / 绝不发什么**：上报的只是 metrics 扁平记录（`engine_status`、`review_reliable`、可信率、裁决、风险档、发现计数等健康/裁决**数值**）。**绝不外发代码、diff、PR 正文、凭据、评审内容**（这些本就不在 metrics 记录里，且有字段白名单兜底）。开 `ANONYMIZE` 后连 repo/PR 标识都不发，可"只共享健康趋势、不暴露在审什么"。上报失败不影响评审。**断网/涉密环境不配端点即完全禁用**，或把端点指向内网自有聚合点。

## 8. 排障（常见坑）

先跑 `touchstone doctor`——下面这些它大多会直接点出来。

| 症状 | 多半原因 | 处置 |
|---|---|---|
| LLM 给 0 建议、diff 像被清空 | `TOUCHSTONE_LLM_CONTEXT_TOKENS` 过小，diff 被裁空（PR#44 类） | 按模型卡调大；核对是否误填成了输出上限 |
| 大 PR 被端点 400 拒 | 上下文窗口设得比模型真实窗口大 / 输入超窗（PR#47 类） | 按模型真实窗口设 context tokens |
| 本轮评审「不可信」/ 子进程超时 | 慢模型 + `TOUCHSTONE_PRAGENT_TIMEOUT` 偏小（PR#48 类） | 调大超时（≥360，慢模型 3600） |
| 连接 407 / 静默挂起 | 经代理访问外网但代理未配好 | 配 `HTTP(S)_PROXY`/`NO_PROXY` |
| GHE 上 API 404 | 用了公有 github 的 API 地址 | 设 `GITHUB_API_URL`/`GITHUB_GRAPHQL_URL` 为你的 GHE 地址 |

## 9. 版本与升级

- 部署锁在 tag（首个公开发布 **v0.1.0**）+ `constraints.txt`，客户可引用、可复现。
- 升级依赖的纪律：**改 `pyproject.toml` 范围 → 重装 → 跑全测试 → 刷新 `constraints.txt`**，四步缺一不可。
- 报漏洞 / 安全响应流程见 `SECURITY.md`。

## 变更历史

| 日期 | 变更内容 | 原因 |
|---|---|---|
| 2026-07-13 | 首版客户部署指南 | 补齐"交给不懂内情的客户也能稳定跑"的部署路径（区别于 RUNBOOK 作者自测视角），配套 `touchstone doctor` 上线门 |

## 部署效率与踩坑（来自集成方实测的上游报告，2026-07）

一个集成仓的 36 次运行实测：中位 351s → 修复后 117s。差距全部来自配置与上游缺陷，不是 LLM 固有成本。按性价比排序：

1. **缓存必须预热**。GitHub 2026-06-26 起 `pull_request_target` 等低信任触发器对默认分支缓存【只读】——评审工作流里 save 永远失败且被 `continue-on-error` 静默（每轮白重建 venv ~57s）。本仓已改为：`touchstone.yml` 只 restore，`warm-pragent-cache.yml` 在可信触发器（push 到 main / 每周 schedule / 手动）下写缓存。集成方两个工作流都要拿；checkout 到子目录时**两处 `hashFiles` 路径要同步改**，否则该键段静默变空。
2. **`TOUCHSTONE_LLM_THINKING` 与 `TOUCHSTONE_LLM_REFLECT_MODEL` 是必配项，不是可选调优**。集成方数据：耗时与 diff 行数几乎无关（r=0.288）、与建议条数无关（r=0.065），只随问题难度——reasoning token 是大头。同 PR 实测 thinking 未生效 213s → disabled 31s；整轮 393s → 125s。逐项挑 env 透传（而非整体复制 workflow）时极易漏掉 REFLECT_MODEL。
3. **数值 env 空串一律回落默认**（`${{ vars.X }}` 未创建时透传空串是常态）。此前 `TOUCHSTONE_MAX_DIFF_LINES` 空串会静默关闭 SIZE-001 体量门禁（超大 PR 直送 LLM 且无提示）；现空串=默认 1000，仅显式 `0` 关闭。全仓数值 env 已统一此语义。
4. **耗时可观测**：交互日志每条自带 `[+N.NNs]` 相对时间；`touchstone-metrics.json` 增 `t_review` / `t_total`。定位"时间花在哪"不再依赖 GHA 行时间戳。
5. **诊断开关**：`TOUCHSTONE_LITELLM_VERBOSE=true` 现在真正生效（显式调 `litellm._turn_on_debug()`；litellm 1.84 的 `set_verbose` 在 import 时求值，事后赋值是空操作）。注意 DEBUG 走 stderr、会把失败归因的 600 字符 stderr 窗口挤满——仅临时诊断，用完关闭。`TOUCHSTONE_LLM_PING=false` 可关预检 ping（默认开；fan-out 下省 improve/review 各一次往返，仅在端点已确认健康时关）。
6. **Python ≥ 3.13 硬下限**：pr-agent 0.39 在 <3.13 与锁文件冲突（`ResolutionImpossible`），且被安装步的 `continue-on-error` 静默成"AI 评审未运行"。锁文件头部已标注。

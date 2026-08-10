# Changelog

本文件记录 Touchstone 的发布版本。设计的逐版迭代历史见 `docs/touchstone-design.html` 的变更历史。
版本遵循语义化版本（SemVer）。版本号单一来源在 `touchstone/__init__.py` 的 `__version__`。

## [未发布]

（下个版本的新变更记于此。）

## [0.2.6] — 2026-08-09（`<details>` 渲染修复 + summary 露关键信息预览）

本版本同步上游 AKDI-SE/touchstone v0.2.6：修复评审报告里 `<details>` 折叠区「点击展开」无反应的回归（自 v0.2.2 #158 长依据折叠起即存在），并把折叠 summary 从无信息标签升级为首句预览。

### `<details>` 折叠渲染修复（上游 #168，修 #167）

- **去空行修「点击展开」无反应**：`<details>` 是 CommonMark type-6 HTML block，遇空行即终止。此前 summary 与 body 间、body 与 `</details>` 间各有一空行 → `<details>` 在首个空行处被截断成孤立开标签，body 变成始终可见的松散段落、`</details>` 变孤立闭标签，表现即「点击展开」点了没反应。去空行让整段留在同一 HTML block 内。此 bug 自 v0.2.2（#158 长依据折叠）起影响所有评审的长依据折叠。

### summary 露首句预览（关键信息可见）

- **`<details>` summary 从「依据（N 字，点击展开）」升级为首句预览**：summary 不再是无信息标签，而是露核心论断，author 扫清单时不展开也能判断依据是否值得细读。
- **句末判定**：CJK 全角（。！？）无条件算句末；ASCII 半角（`.!?`）仅在后接空白/串尾时算——否则版本号（`0.2.3`）、小数、缩写（`e.g.`）、域名里的 `.` 会被误判。截断处加 `…`。已知局限：ASCII 句号直连 CJK（无空格）与缩写（e.g./i.e.）不被精确识别——属轻量句切分的固有边角，全文始终在 body 内不影响判读。
- **body 折叠空白防内部空行**：body 嵌入前 `re.sub(r"\s+", " ", reasoning).strip()` 折叠成单行——内容一字不丢。
- **两条硬约束均满足**：(a) 非动态获取——summary 与 body 均静态嵌入 markdown；(b) 不影响 API 取全量 review 意见——全文始终在 details body 内，`<!-- touchstone-checklist -->` 结构化标记不受影响。

## [0.2.5] — 2026-08-06（adopted 信号放宽 + 消费方团队规范种子）

本版本汇集两项学习回路与评审注入的改进：放宽 adopted 信号采集口径以破 graduate 死锁；新增消费方仓的团队手写规范种子机制。

### adopted 信号放宽（破 graduate 死锁）

- **ack `done` + merged → human_adopted**（#165）：此前 `build_ground_truth` 只认 thread-resolved 发现为 `human_adopted`——touchstone 工作流用 ```touchstone-ack``` 不用 thread resolve，致 29 条真值里 `human_adopted` 恒空、graduate 阈值（20+20 臂）数学上不可达。现新增 `_ack_done_types`（与 `_waived_types` 对称）：清单 marker `status=="done"`（机器复检过 sig 消失）+ 人合入 → 计为 adopted 强信号。配套 `GT_WINDOW` 默认 30→200 覆盖全仓历史。`if merged` 守卫 + try/except 隔离解析异常（per-PR 故障不拖整批）。

### 消费方团队规范种子（`.touchstone/seeds.yaml`）

- **无需学习回路基础设施的团队规范注入**（#166）：任何消费方仓可在 `.touchstone/seeds.yaml`（样例见 `seeds.yaml.example`）写团队手写规范，评审时**直接注入** PR-Agent 提示词——无需 learn.yml、无需经验库、无需 TF-GRPO、无需旗舰模型。与引擎经验库（TF-GRPO 学的、跨仓共享）分层共存：引擎库走 `TOUCHSTONE_EXPERIENCE_REF` 防投毒闸；seeds.yaml 是仓内配置（与 pr-agent.yaml 同级、受合并权限保护）不走该闸；两路同受 `TOUCHSTONE_EXPERIENCE_ENABLED` 总闸。`emphasize`（多盯紧）/ `suppress`（少挑）两 kind，可选 `stack` 技术栈过滤。防御纵深：text 500 字 + finding_type 80 字封顶、非 str stack fail-open + [warn]、repo_dir isdir 校验、解析失败优雅降级。诚实标注威胁模型（与 pr-agent.yaml 同款 PR-head 配置向量）与栈过滤 gap（主评审路径不持有栈上下文）。

## [0.2.3] — 2026-08-06（配置/版本管理体验：schema_version 告警 + latest 自动跟版）

本版本汇集两项配置与版本管理体验改进，让 pr.yaml 的 `schema_version` 与 touchstone 软件版本的解耦关系在运行时显式，并支持下游零摩擦跟 patch 版本。

### 配置/版本管理体验

- **schema_version 运行时兼容性告警**（#161）：此前 pr.yaml 的 `schema_version` 字段在代码里完全未被读取。现 `contract_check.SCHEMA_VERSION` 常量 + `schema_version_warning()` 在 `run.py` 启动时核对——缺失（旧 yaml 向前兼容）或匹配静默；不匹配 stderr 告警提示升级 `TOUCHSTONE_ENGINE_REF` 或改回已知 schema。不进 findings/checklist（配置错配非 author 能修的契约违规）。`.touchstone/pr.yaml` 模板加字段语义注释。
- **示例 workflow 支持 `TOUCHSTONE_ENGINE_REF=latest`**（#162）：下游（如 doushuaigong）此前锁具体 tag，每次升级手动改。README 示例改为 env + `gh release view` resolve step——`latest` 自动解析最新 release tag、具体 tag / `main` 原样透传。三选一（`latest` / `v0.2.x` / `main`）行为与适用场景在表格中文档化。配合 schema_version 告警：`latest` 若拉到改了 pr.yaml 字段的 breaking 版本，引擎运行时 stderr 告警兜底。

## [0.2.2] — 2026-08-06（评审呈现层改进：去冗余 + 定位精度 + 折叠 + 诚实降级）

本版本汇集三项评审呈现层改进，借鉴 pr-agent 上游 #2510 的单条评审写作风格。全部为呈现层变更（无 layer-contract 变更、无 API 破坏），对应 `render._finding_entry` 单条发现的渲染。

### 评审呈现层改进（借鉴 pr-agent 上游 #2510 写作风格）

- **单条发现去字段冗余**（#157）：`修复方向` 与 `rationale` 同文时不再复读（标题已含一句话问题，方向同文即纯噪声）；依据字段早有同等去重守卫，此处补齐对称。
- **定位精度**（#157）：行号缺失时渲染只显文件名（不再 `file:None`）。两层兜底——`review_provider.normalize` 做 `line_start→line_end` 回退（`is not None` 判定，0 不当缺失），`render._location` 是渲染侧最终兜底。sig 带真实行号让跨轮 reconcile 更稳（行号稳定而非塌缩到 `:None`）。
- **长依据折叠**（#158）：`fix_reasoning` 超 200 字符折叠进 `<details>`，summary 行露字数，author 一眼扫清单只看标题+方向，需要细节再展开。短依据平铺（快速判读信号）。
- **done_criteria 诚实降级**（#159）：model 来源（pr-agent）在 normalize 层给不出设计所要求的「一句可回答的具体复核问题」，此前用模板 `「{direction}」是否已按方向解决？` 复读修复方向。现 `question` 留空，渲染退为「下一轮复检不再命中即销项」（如实描述 reconcile 实际机制）。确定性来源（contract/probe）不受影响，仍给真实机器可验判据。

## [0.2.1] — 2026-08-06（TF-GRPO 生产化全部差距 + Probe + 运维加固）

本版本汇集 TF-GRPO 离线自演化生产化的全部 4 个差距、Probe 测试有效性探针新模块、以及集成方 CI 耗时上游报告驱动的运维加固。4 个 TF-GRPO 差距遵循同一纪律：**env-default-off = 字节级零行为变化**，纯函数 + 离线可测，无 layer-contract 变更。

### TF-GRPO 生产化差距（3a + 1a + 3b + 2a，皆 opt-in、默认零行为变化）

- **差距 3a 收敛检测 + 增量水位**（#151）：连续 `N_STABLE` 轮 text+lift 不变的 active 经验标 `stable`，下轮蒸馏跳过该 type（省 LLM 调用）；增量水位（`TOUCHSTONE_INCREMENTAL`）只取 number>水位的新 PR，每 `FULL_REFRESH_EVERY` 轮强制全量对账兜底漂移。
- **差距 1a 位置级奖励 env 接通**（#152）：thread_findings 带 file/line → `build_ground_truth` 产 resolved_findings → `make_gt_entry` 产 `human_adopted_positions` → `score_review` 走位置级部分信用。机制 + 数据管线 + 测试就位，本 env 接通生产 run-path（`TOUCHSTONE_POSITIONAL_REWARD`）。
- **差距 3b 差分时序 + 趋势回滚**（#153，merge `4cdfd42`）：开 `TOUCHSTONE_DIFFERENTIAL_METRICS=true` 后每轮 per-type lift 追加到 `adoption-trend.json`（时序可观测）；active 经验 lift 连续 `AUTO_ROLLBACK_M` 轮下降 → 趋势退役（与 `retire_on_negative_lift` 静态阈值互补，提前下线慢性恶化经验）。
- **差距 2a 跨 PR 一致性过滤**（#154，merge `3e83d3c`）：candidate 入池前要求来自 ≥`MIN_SOURCE_PRS` 个 PR 且跨 PR reward 方差 ≤`MAX_REWARD_VAR`（防"仅 1 PR 高 reward"的运气型 outlier 污染经验库）。默认 MIN=1（不限）、MAX_VAR 空（不检查）= 不过滤。

### TF-GRPO 闭环 + 经验库改进（本版本同步收口）

- **TF-GRPO 闭环合上**（#143）：开 shadow 注入 + 接通 taxonomy 清洗开关（`TOUCHSTONE_TAXONOMY_ENFORCE`）。
- **finding_type 归一化**（#144）：合并大小写/分隔符变体（不丢弃），稳定 tiebreak + 兄弟 evidence 合并。
- **waived 噪声标签采集**（#146）：采集 waived+merged 为确认噪声标签（Phase 1，零行为变化）。
- **marker 信任根收紧**（#147）：`bot_login` 已知时精确匹配（系统级安全加固）。
- **聚合单侧失败轮数**（#148）：run-285 盲区——improve 挂而 review 仍可信时不再隐身。
- **CI 耗时优化**（#149）：内联 gate + pip 下载缓存 + 文档化 verify-result.json 缺失一致性。

### Probe：测试有效性探针（新模块）

补上 Touchstone 缺失的「测试相对于行为」审查层——回答「『测试全绿』本身可信吗」。对标 dev-loop 变异探针实践与 AKDI fail-open 实证（lint 零文件检查恒 exit 0、冒烟 11/63 skip 计通过）。设计见 `docs/touchstone-probe-design.md`（随 docs/probe-design 分支独立 PR 交付，SIZE-001 拆分）。

- **L0 断言普查（静态·`census`）**：不跑测试，静态揪四类假测试——skip 计通过 / 零断言 / 恒真弱断言（assert True、assertIsNotNone）/ 吞异常。
- **L1 变异探针（动态·`plan`+`run`）**：锚定 ScopeFacts 增量函数，按（分支数 × 低断言密度）选点，套最小高价值算子集（CMP/BOOL/CONST/RET/EXC，纯 stdlib `ast`，零第三方），预算内注入→定向跑测→恢复源码→五态判决。
- **抗自身 fail-open**：每轮追加哨兵变异体、最先执行，哨兵未被击杀即判本轮 `invalid` 并作废全部判决（never silent）；`plan_empty` 显式汇报「没做事」不给假绿灯；`ProbeRunReport` 强制全计数。
- **接现有闭环**：`to_findings` 把 survived 转标准 Finding（`done_criteria` = replay 击杀该变异体，机器可验证），并入 checklist 四态收敛 + ack；`mutant_id` 为内容指纹，接 lineage 跨轮记账；`replay` 下一轮定向复验，击杀即闭环。
- 边界：增量抽样非全库、不替代测试运行器、不自动改测试；等价变异体走 ack 人裁；Go 引擎/全库基线棘轮留待 M2。
- 新增 `touchstone/probe.py`（~350 行）+ 13 测试（静态快测 + 真跑 pytest 的 run/replay 端到端）。

### 运维加固（集成方 CI 耗时上游报告，中位 351s→117s 的经验固化）

- **缓存写入（问题一，影响所有模板集成方）**：GitHub 2026-06-26 起 `pull_request_target` 对默认分支缓存只读，save 永久失败且被 `continue-on-error` 静默为黄警告（"针对瞬态故障的容错在故障变永久后成为静默器"）。`touchstone.yml` 改 restore-only、权限降 `actions: read`；新增 `warm-pragent-cache.yml` 在可信触发器预热（省 ~52s/轮）。缓存键补 `runner.arch`（venv 含编译产物，跨架构命中不可用）；注释标注子目录 checkout 时 `hashFiles` 需改路径。
- **数值 env 空串静默失效/崩溃（问题三 + 全仓排查）**：`TOUCHSTONE_MAX_DIFF_LINES` 空串此前经 `or 0` 静默关闭 SIZE-001 体量门禁；另有多处裸 `int(env)`/`float(env)` 空串直接 ValueError 崩 job。全仓 14 个文件统一为"空串回落默认"（`_max_diff_lines()` 等），仅显式 `0` 才是关闭。
- **`TOUCHSTONE_LITELLM_VERBOSE` 空操作（问题二）**：litellm 1.84 的 `set_verbose` 在 import 时求值、事后赋值无效且被废弃。改为显式调 `litellm._turn_on_debug()`；模板默认置 `false` 并注明 stderr 窗口污染（DEBUG 会把 `failure_stderr_tail` 的真实报错挤出）。
- **耗时可观测性（问题四）**：`_ix` 交互日志每条带 `[+N.NNs]` 相对时间戳；`metrics.build` 增 `durations`（`t_review`/`t_total` 进 `touchstone-metrics.json`，支撑耗时告警）。
- **预检 ping 开关（问题五）**：`TOUCHSTONE_LLM_PING=false` 可关（默认开，保留防静默故障的观测点）。
- **Python ≥3.13 下限（问题六）**：`pragent-constraints.txt` 头部显式标注；DEPLOYMENT 新增「部署效率与踩坑」节，把 `TOUCHSTONE_LLM_THINKING`/`REFLECT_MODEL` 升为必配项（集成方数据：213s→31s）。
- 测试 +5（空串回落 / SIZE 门禁 / ping 开关 / 日志时间戳 / metrics 耗时字段）。

## [0.1.0] — 2026-07-18（首个公开发布）

首个公开发布版本。版本号单一来源 `touchstone/__init__.py`（`__version__ = "0.1.0"`），遵循 SemVer。本版本汇集公开发布前的全部内部迭代成果——PR-Agent 驱动的评审主链（发现归一 / 风险分流 / 回贴）、确定性门禁（契约核对 + 栈专项规则）、独立验证 verify（默认关）、渐进自治 autonomy（默认关）、校准与离线学习回路（含 TF-GRPO），以及面向客户的运维成熟度（健康度自检 `doctor`、运行指标 `metrics`、告警钩子 `alert`、使用遥测 `telemetry`）、依赖锁与安全政策。

> **版本历史说明**：本仓在首个公开发布前曾以 `v0.1.0` / `v0.2.0` / `v0.2.1` / `[1.0.0]` 等标记记录内部迭代，但**均未在 GitHub 发布过 tag/release**。现以 `0.1.0` 作为首个公开发布版本号重新整理版本历史，旧的内部版本标记已并入下方时间线（不再作为独立发布版本）。

**评审报告改版（report-pr66-improvements-v2）**：
- **重分层「确定性 vs LLM」两视图**：③「确定性事实」→「静态检查」（修改范围/敏感路径/门禁/同源 + **确定性规则命中逐条**，不经 LLM）与 ④「评审发现」→「AI 评审」（**仅** pr-agent 的 LLM 发现）并列同级 H3。规则命中与 AI 建议各自独立 `MAX_FINDINGS_IN_SUMMARY` 上限；逐条发现渲染抽 `_finding_entry` 共用。
- **「收敛清单」→「待解决问题清单」并瘦身**：每条只留 状态/方向/位置/销项备注，依据与达成判据移到上方评审段（顶部加一行销项跟踪说明）；机读 JSON marker 字段不变 → ack/reconcile 机制不受影响。
- **品牌行** `Touchstone · ADVISORY` → `Touchstone · AI Committer 代码检视`（templates/review_report.md 唯一 H2）。
- **降级原始错误贯通**：`review_pr` 捕获 `ReviewEngineDegraded.reason`/`RuntimeError` 入新字段 `engine_detail` → main → post_results → 「验证与日志」段详列原始错误（截 1500 字符、指向交互日志 artifact）；置顶 `[!CAUTION]` 精简为两行（失败环节 + 指向验证与日志），不再塞原始 dump。

**商用化 P1（运维成熟度，第二批）**：
- **健康度自检 `touchstone doctor`**：新增 `touchstone/doctor.py`——在 preflight（配置+连通）之上补上"评审引擎现在真能跑通产出裁决吗"这一步（引入**自检评审/smoke review** 概念：合成 PR 在进程内跑 `review_pr`、注入空观察源走确定性裁决链、零网络、断言产出合法裁决）。三阶段汇成红绿表（`✓/⚠/✗`）+ **单一退出码**表达能否上线（0=可上线，1=有阻断项）；支持 `--no-net`、`--json`（运维聚合/CI 门）。`touchstone doctor`/`touchstone preflight` 子命令分派接入（`touchstone --repo … --pr …` 原状不变）。
- **依赖模块增强**：`review_provider.fetch` 增加**可调用注入口**（callable provider），供自检/测试短路 PR-Agent 子进程；`preflight.check_standards` 从 `main` 抽出供复用。
- **客户版部署指南**：新增 `docs/DEPLOYMENT.md`——从零到上线的落地路径（前提/安装/必配项/部署前自检/CI 接入/可观测/排障/升级纪律），区别于 `RUNBOOK.md` 的作者自测视角。
- **变异测试基线**：跑完红线四模块（contract_check/stack_rules/loop/checklist）的 mutmut 全量（2049 变异，原始击杀率 57.1%）+ 靶向审计（6/6 行为关键变异全抓），写入 `docs/mutation-baseline.md`。过程中揪出并修复一处真实测试缺口——`loop_step` 非清单路径"无推进升级"未被守住（author 可只加不减拖轮），补 `test_loop_escalate_on_no_progress_legacy` 锁死；并刷新靶向审计过期锚点（`_extract_json` 迁移）。
- **使用遥测（预留 sink，默认关）**：新增 `touchstone/telemetry.py`——可把每轮 metrics 记录上报到【配置指定】的中心汇聚点，供跨部署观察 touchstone 健康趋势。护栏（面向政企/内网客户）：默认关（不配 `TOUCHSTONE_TELEMETRY_ENDPOINT` 则一字节不外发）、端点是配置无硬编码 URL（可指厂商或客户内网聚合点）、字段白名单数据最小化（绝不外发 diff/代码/PR 正文/凭据）、`ANONYMIZE` 可抹掉 pr/sha 标识、失败绝不冒泡、上报通道带与 `alert.py` 一致的 SSRF 防护（scheme 白名单 + 不跟随重定向）。12 条测试全离线（含验证 diff/token 等被白名单挡掉 + SSRF scheme/重定向拦截）。
- **告警钩子**：新增 `touchstone/alert.py`——在 metrics 之上把关键信号主动投递到【客户自己配置】的渠道。单轮高危（静默故障/引擎降级/author 自证待核准）贴 PR 评论；滚动聚合（可信率过低/持续静默故障）开或更新带 `touchstone-alert` label 的跟踪 Issue（去重防刷屏）；`TOUCHSTONE_ALERT_WEBHOOK` 可选走 webhook。总开关 `TOUCHSTONE_ALERT_ENABLED` 默认关（不外呼、只保留 artifact），投递失败绝不冒泡（不拖垮评审 job），无任何硬编码外部 URL。判定为纯函数、投递可注入，17 条测试全离线。
- **故障排查 runbook**：新增 `docs/incident-runbook.md`——把散在 CHANGELOG 的踩坑（裁空 PR#44 / 超窗 PR#47 / 超时 PR#48 / LLM 静默故障 / author 欺骗面 / 变异测试缓存假失败）集中为「症状→诊断→处置」运维手册，接上 doctor/metrics/交互日志诊断抓手。
- 新增 14 条 doctor/seam 测试；ruff/mypy 全绿。

**商用化 P0 加固（首批）**：
- **版本发布纪律**：版本号统一到单一来源 `touchstone/__init__.py`（修复此前 `__init__` 与 pyproject 版本不一致），pyproject 动态读取；新增 `--version` CLI。
- **依赖锁定**：主依赖加上界（`pyyaml>=6.0,<7` / `requests>=2.31,<3` / `unidiff>=0.7,<1` / `openai>=1.30,<3`，防上游破坏性大版本静默破坏客户环境）；新增 `constraints.txt` 锁一组已验证版本供客户复现。
- **安全政策**：新增 `SECURITY.md`——私密漏洞披露渠道、响应 SLA、以及本系统五条关键安全边界（author 不可自证闭环 / 不可信不得伪装通过 / 凭据隔离 / 确定性核对不打折 / 权威状态只信机器人）。
- **配置强校验**：preflight 增补"不设就撞坑"的关键配置校验——`TOUCHSTONE_LLM_CONTEXT_TOKENS` 未按模型卡设置的警告（PR #47 被拒根因类）、过小值检测（PR #44 裁空类）、VERIFY_ENABLED 缺凭据、PRAGENT 超时过小（PR #48 慢模型类）。部署前一键暴露隐患。
- **运行指标（可观测性）**：新增 `touchstone/metrics.py`——每轮评审产出扁平指标事件流（评审可信率 / 静默故障轮数 / 放行率 / 引擎状态分布 / author 自证拦截数），workflow 上传 `touchstone-metrics.json` artifact，`python -m touchstone.metrics` 聚合。把历史上"靠人追问才发现"的 LLM 静默故障变为主动可见、可告警。5 条测试。

以下为 0.1.0 汇集的各轮开发明细（时间倒序）：

### 2026-07-09（author 自证销项的校验缺口加固）

问题分析："author 能否通过写 ack 答复、在不修改/假修改下闭环 touchstone 意见列表"——答案是**能**，且直通自动放行。已修复。

**问题链路**：收敛清单的 `waived`/`split` 申报只校验"note 非空"、不验真伪，却计入 `RESOLVED` → 拉高 resolved_rate → `all_resolved` → loop `converged` → autonomy `loop_converged` 闸 → 自动放行。author 遂可**不改一行代码**发 `SEC-001:x.yaml:7: waived: 这是测试夹具` 单方闭环任意意见。advisory 下 waived 标了"🟡 待人核准"但仅是视觉提示、无强制；自动放行模式下没有任何闸检查"是否存在未核准的 author 自证"。（对比：`done` 有机器复检兜底——签名本轮仍命中则拒，不受影响。）

**加固（双闸 + 呈现）**：
- **销项分级**：`VERIFIED = {done}`（touchstone 侧机器确认签名复检不再命中）vs `CLAIMED = {waived, split}`（author 自证、机器不可核实）。`RESOLVED` 仍是三者之并（供 resolved_rate 展示与 no_progress 判定），但新增 `all_verified()` / `has_unverified_claims()` / `unverified_claims()`。
- **收敛门**：loop 的收敛判据从 `all_resolved` 改为 `all_verified`；存在 CLAIMED 时不给 `converged`，回落 `continue` 并点名待人核准项（advisory 下人仍可径直合入）。
- **autonomy 独立闸（多层校验）**：新增 `no_unverified_claims`——不信任 `loop_decision` 单点（result marker 理论上可被 author 虚报），`unverified_claims` 计数由 touchstone 侧写入 findings.json/result marker，即便 loop_decision 被虚报成 converged 也独立再拦一道。
- **呈现**：报告横幅点名"N 条 waived/split 系 author 自证、机器未验证"；waived/split 的 note 加"（待人核准，机器未验证）"前缀，note 内容无论塞什么都改不了 status。
- 6 条对抗测试 + 端到端复现（author waived 不改代码 → loop continue + autonomy 双闸 failed）。526 → **532** 测试全绿。

### 2026-07-09（LLM 静默故障系统排查：部分降级与截断修复可见化）

对"LLM 出问题但评审意见不体现"做全链路问题排查（pr-agent 0.37 两工具内部 → runner 出口 → provider 解析 → 判定/呈现），在既有机制（_degraded 结构化上报 / stdout fd 级隔离 / 分层失败签名 / review_reliable 判定+CAUTION 呈现 / 0-发现溯源）之外，识别并修复三个新盲区，另确认两类残余风险及其缓解：

- **结构性事实（本次排查的钥匙）**：improve 与 review 的 `run()` 都在 pr-agent 顶层全量捕获异常——异常永远穿不透到 runner 的 try，`_degraded: llm_failed` 分支在自然情况下不可能触发。工具级故障只能靠 stderr 外化。据此 runner 新增三个**工具级专属标记**（improve produced no data / review produced empty prediction / review prediction malformed），并入吞没检测签名集合。
- **S1 部分降级不可见（新堵）**：improve 连挂而 review 正常时，吞没检测按设计放行（评审仍有效）、engine ok、可信——建议侧信号可以缺失数日无人察觉。新增 `partial_tool_failure()` 工具级归因（专属顶层失败串 + 单侧空另一侧有产出），经新开的 invoke_meta 通道透出，报告横幅明示"本轮 improve/review 工具失败,该侧信号缺失"。
- **S2 review 形变输出（新堵）**：review 段存在但非 dict（截断/答非所问被 try_fix_yaml 修出畸形），旧 runner `or {}` 静默吞成空清单且 stderr 无任何失败串——签名检测与启发式全部漏过。现 runner 显式打标记，进签名集合。
- **S3 截断修复静默丢条目（新堵，透明化）**：LLM 输出截断（finish_reason=length）或轻度畸形时 try_fix_yaml 能"修好"但可能修丢条目，全程无失败串。现统计 stderr 中 "Initial failure to parse AI prediction" 次数经 meta 透出，报告注记"本轮有 N 次预测经修复解析（条目可能被修复丢弃）"。不改判定，只让人知道。
- **残余 R1（不可消除，已有缓解）**：LLM 合法返回空建议（格式正确、内容为空判断）与"真审完没问题"不可区分——缓解为 0-发现溯源行 + added_lines 启发式 + 交互日志全量留痕。**残余 R2**：self-reflect 阶段模型劣化把全部建议打成低分被阈值过滤、或端点被换成弱模型仍返回 200——属质量漂移非故障，缓解为校准回路的采纳率监控（TF-GRPO 奖励侧可见）；可选增强是 preflight 加最小提示词回环校验（留待团队决策）。
- 6 条回归测试；review_pr 返回契约增加 llm_notes 键。521 → **526** 测试全绿。

### 2026-07-09（检测器盲区：review 工具解析层失败漏检）

对"LLM 反馈为空"根因链做独立代码级复核（pr-agent 0.37 源码 + 本仓提交历史 + prompt token 实测），确认 #45/#46/5c1129c 的诊断大方向成立，并修正一处不精确——它构成现行检测器的真实盲区：

- **复核结论 A（#45 语义用反）**：成立且证据更硬。b30d6fc 引入 llm_budget 时把 1ff7c67 原本正确的 8192 改成了 `output_tokens()`（默认 4096）——这是一次回归而非初始设计错。定量：pr-agent 的 `custom_model_max_tokens` 语义 = 上下文窗口（内置 MAX_TOKENS 表同义替代，get_pr_diff 以「该值 − 1000~1500 buffer」为 prompt 总预算）；review 工具 prompt 自重 ≈3.6–4.8K tokens，**零 diff 也超 4096−1500=2596 的预算**，任意大小 PR 的 diff 必被裁空（improve 自重 ≈2.3K，余量仅 ~300 token，正常 patch 同样放不下）。裁空是确定性的——解释了故障的普遍性而非偶发性。
- **复核结论 B（空响应被吞，检测器可靠）**：一半成立。improve 的 YAML 解析在 `_get_prediction` 内、位于 retry 圈内，空 content 的解析失败重抛，stderr 必含 "Failed to generate prediction"——旧检测器覆盖 ✓。**review 的解析在 retry 圈外**（retry 只包取原始文本的 `_prepare_prediction`，解析在其后的 `_prepare_pr_review`）：空 content 走 "Failed to parse AI prediction after fallbacks" / "Failed to parse review data" / run() 顶层 "Failed to review PR:" 路径，**不含**上述签名。旧单签名检测器在"review 空响应 + improve 恰好 0 建议（小 PR 合法情形）"下漏检，engine_status 误判 ok，仅剩 added_lines≥20 启发式兜底——小 PR 兜不住。修复：`_PRED_FAILURE_SIG` 扩展为分层信号集合 `_PRED_FAILURE_SIGS`（4 串，逐层注明来源），判据其余不变（仍需本轮零建议共同成立，improve 单独失败而 review 有产出不误报）。3 条盲区回归测试。
- **复核结论 C（ai_timeout）**：成立，litellm_ai_handler 确将 `config.ai_timeout` 透传 acompletion。

### 2026-07-09（不可信评审的呈现层接入）

PR #44/#46 暴露的最后一块拼图：`review_reliable` 信号已接判定层（#46：不销项/不收敛/不放行），但**呈现层缺位**——不可信轮的报告仍只在横幅里低调提示"改动不小却 0 建议——建议人工扫一眼"，态势表照常显示由 0 发现推得的 LOW·可跳过/skip，评审失败反而以最低风险示人。本轮接入：

- **[!CAUTION] 置顶告警**：review_reliable=False 时以 GitHub 原生红色警示框置于 H2 正下方，首句即"**本轮 AI 评审不可信 —— 0 发现 ≠ 审过没问题**"；写明原因（engine_status 精确映射 no_engine/provider_failed/llm_failed，或裁空启发式并给出行数/建议数证据）、后果（不销项/不收敛/不放行）、出路（人工评审或修复后重触发，指向交互日志）。告警**替代**常规降级/溯源横幅（同一信息不两处重复），循环状态行保留其后。
- **态势表不采信**：建议动作改示 `人工评审`（原建议不采信）、风险等级注明"仅确定性信号"。只改展示——result marker 里的机器数据原样写入，校准与台账重建不受影响。
- 铁律写入模板头注，`test_unreliable_review_renders_caution_and_distrusts_action` 等三条回归测试锁死；设计文档变更历史记阶段十。515 → **518** 测试全绿。

### 2026-07-04（评审报告易读性改版）

七段版面**语义与信息不减**，呈现按易读性重排（版面变更=设计变更：模板头注、设计文档 §4.8 七段表与变更历史已同步）。排版铁律：

- **层级修复（核心问题）**：旧版全文只有收敛清单是 H3 标题，其余段落全是加粗行——GitHub 渲染后 H3 远大于加粗正文，收敛清单看起来像整条评论的总标题，与之**语义并列**的"确定性事实"等段反像其下级。现在：全文唯一 H2（品牌 + ADVISORY 定位声明），确定性事实/评审发现/收敛清单/验证与日志四段一律 H3——并列段落并列层级。
- **一眼态势**：风险等级/建议动作/验证建议/影响面从"全角空格挤一行"改为 Markdown 表格。
- **关键信息前置**：逐条发现改编号列表，「`file:line` — 问题」打头；rule_id/severity/置信/来源是审计信息，降级为行尾 `<sub>` 小字。
- **降噪**：状态横幅（循环/降级/溯源）统一 blockquote 与正文区隔；"完整 LLM 交互日志"去实现细节括注（原"（pr-agent 原始输出 / LLM 配置 / ping）"）；每轮重复的申报方式样板折叠进 `<details>`；收敛清单标题去品牌前缀（品牌只在 H2 出现一次）。
- 新增 `test_report_layout_invariants` 把排版铁律固化为回归测试。488 → **489** 测试，既有断言零改动全绿。

### 2026-07-04（工程化加固·第四轮：工具链收尾）

- **mypy 渐进接入**：pyproject 增加 `[tool.mypy]`（默认宽松：ignore_missing_imports，暂不开 check_untyped_defs——首测其在本仓 dict 密集风格下产生 71 处推断噪音，等核心结构补 TypedDict 后逐模块收紧）。默认模式抓到 4 处真问题并修复，其中 1 处是**类型契约与语义不符**：`VerificationResult.passed` 声明 `bool` 但语义上 None=无法判定（unsupported/漂移兜底），修正为 `Optional[bool]` 并注明三值语义。CI lint job 增加 mypy 步骤。
- **mutmut 扩围并修通**：变异测试范围从 contract_check 一个文件扩至全部确定性裁决模块（+stack_rules/loop/checklist——红线契约的裁决代码，变异测试对其价值最高）。同时修通此前"mutmut 3.x 与本仓测试集成需单独配置"的遗留：setup.cfg 改多行列表语法 + `also_copy` 带上沙箱缺的 `.touchstone/` 规则文件与完整包目录。已实测端到端可跑（4 模块共 1798 个变异体）；全量跑一遍并建立击杀率基线留作团队任务。
- **verify_change CLI 函数化 + 补测**：`__main__` 裸块（100+ 行零覆盖）重构为可测的 `main(argv)`（learning_loop 同款模式，退出码 0/1/2 语义不变）；新增 `tests/test_cli_paths.py`（plan 落盘/execute 读回/产物缺失退 2/verify 不过退 1/GitHub 回贴、autonomy --graduate 与 no-op 路径）。verify_change 覆盖率 73%→95%，总覆盖率 90%→**92%**，CI 门槛 85→**88**。
- **杂项**：`push-to-github.sh`（一次性引导脚本）挪至 `scripts/`；测试 481→**488**。

### 2026-07-04（工程化加固·第三轮：模块拆分）

两个巨型模块按职责拆分，全部既有引用路径经门面再导出零改动兼容。测试 481 全绿、逐文件独立通过、ruff 清零、覆盖率 90% 不变。

- **learning_loop（723 行）三分**：`experience_store.py`（经验的**状态**：JSON 存取含受信 ref 防投毒、seed/merge、graduate/retire/disable、render_injection）+ `distill.py`（经验怎么**产生**：计数式 + TF-GRPO 语义优势 + 可插拔分发）+ `ground_truth.py`（学习信号从哪**来**：人审裁决重建真值集）；learning_loop.py 保留 CLI/main 编排并再导出全部名字。拆分中理顺一处阈值语义：入池与退役是同一对采纳率判据的镜像，SUPPRESS/EMPHASIZE 单一事实来源归 experience_store，distill 引用；retire 的样本下限独立为 RETIRE_MIN_FIRES（与 DISTILL_MIN_FIRES 同值同理但语义独立）。
- **verify runner 层拆分**：`verify/runners.py` 承接 PythonRunner/MavenRunner/select_runner 及全部执行/覆盖/变异落地（pytest/coverage/AST 变异/JaCoCo/PIT）；verify_change（861→471 行）只留裁决编排（plan/execute、判过条件、充分性阶梯、diff 改动行解析）。新语言 runner（Go/TS/…）在 runners.py 挂 select_runner 即可——"换语言只需替换 LANG RUNNER"从头注承诺变成正式扩展点。verify 运行方式统一为 `python -m verify.verify_change`（workflow/RUNBOOK/测试同步）。
- **测试迁移**：monkeypatch 需打在实现所在模块才能影响内部调用——涉及 runner 内部的 patch 目标从 verify_change 迁至 runners（17 处），learning_loop 的 STORE_PATH reload / _gh_get patch 迁至 experience_store / ground_truth（既有 import_hygiene 守卫自动覆盖四个新模块）。

### 2026-07-04（工程化加固·第二轮）

第一轮的两个"留作后续"项落地（lint 工具链、渲染层拆分），过程中又抓到并根治一类**被双重掩盖的运行期地雷**。测试 478 → **481**，ruff 全绿。

- **函数内平铺导入地雷（5 处）**：第一轮的包化改造只覆盖了顶层导入，函数体内还残留 5 处 sibling 平铺导入（orchestrator/loop/pr_agent_runner/review_provider 各 1-2 处）——移除 sys.path hack 后这些分支一执行必然 ModuleNotFoundError。它们此前不炸的原因有两层掩盖：①相关分支缺测试覆盖；②`test_integration_mock.py` 把 `touchstone/` 子目录插进了 sys.path，使全量测试里平铺名恰好可解析（单跑其他文件才炸）。本轮全部改为包导入、清除 path 污染，并新增 `tests/test_import_hygiene.py` 三条结构性守卫：静态扫描禁止 sibling 平铺导入（函数内也逃不掉）、禁止测试污染 sys.path、render 再导出兼容性。全部测试文件现可**逐个独立通过**（消除顺序依赖）。
- **渲染层拆分**：`_load_template`/`render_facts`/`render_findings`/`render_report`/`render_summary` 从 orchestrator（592 行）拆至新模块 `touchstone/render.py`；orchestrator 保留再导出，既有 `orchestrator.render_*` 引用路径与测试零改动兼容。上述地雷之一（render_findings 内的 `from llm_budget import`）随拆分根治为顶层包导入。
- **verify 执行环境的 token 落盘缺口（第一轮遗留的过度承诺，自查修复）**：verify_plan/verify_execute 未写 job 级 permissions，继承了 workflow 级 `checks: write`；且 actions/checkout 默认 `persist-credentials: true` 会把 GITHUB_TOKEN 写进 `.git/config`——verify_execute 里执行的 PR 代码读 `.git/config` 即可拿到足以**伪造 touchstone/gate 总闸**的 token（该缺口在拆分前的单 verify job 就存在，"GITHUB_TOKEN 已去掉"只去了 env 未去凭据落盘）。现两 job 权限降为 `contents: read` + checkout `persist-credentials: false`："执行环境零凭据"承诺至此才真正成立。
- **ruff 工具链（克制配置）**：pyproject 增加 `[tool.ruff]`——只选真缺陷规则（F/E7/E9/B/PLE），显式豁免 E701/E702/E731（单行紧凑写法是本仓刻意风格，不做格式化重排以免噪音淹没语义变更）。首跑 31 处命中，修复其中真缺陷：3 处死导入（含 orchestrator 拆分后彻底不用的 `re`）、3 处 `raise ... from e` 补异常因果链（排障时可见原始异常）、1 处 `zip(strict=True)` 把 rollout 同长不变式显式化、2 处无占位 f-string、测试侧重复导入/死变量各 1。CI 新增 lint job。

### 2026-07-04（工程化加固）

外部代码评审驱动的一轮工程卫生与安全边界修复。测试 109 → **478**，覆盖率 52% → **90%**（verify 0% → 81%）。

- **测试资产找回（P0）**：恢复 4ac2aaf 误删的 17 个测试文件（test_verify/test_learning_loop/test_autonomy/test_review_provider/test_checks/test_ghclient/属性测试等）——「命门」verify_change 与「差异化核心」learning_loop 此前处于零测试状态。恢复的属性测试当即抓到一个真回归并已修复：`parse_pr_agent` 对非 dict 输入崩溃（历史提交 9febc2e 声称加过的 isinstance 守卫实际不在代码里，现补齐顶层与条目两级形状守卫）。
- **verify 凭据隔离（P0，设计 §6.6 落地）**：`verify_change` 拆分为 `plan_verification`（持 LLM 凭据，只读接口 + 生成验收测试，**绝不执行 PR 代码**）与 `execute_verification`（真正执行 PR 代码，**不需要任何凭据**）；CLI 增加 `--phase plan|execute|all`，plan 产物 `acceptance-tests.json` 经 artifact 传递。workflow 的 verify job 相应拆为 verify_plan（持密不执行）/ verify_execute（执行零 secret）两个 job——恶意 PR 在执行环境中再无凭据可窃取。原单进程用法（`--phase all`）保留给可信环境，行为不变。新增 `tests/test_verify_phases.py` 固化三条不变式（plan 不执行代码 / plan 落盘回读与单进程判决等价 / execute 不接触凭据）。
- **自测 CI（P0）**：新增 `.github/workflows/ci.yml`——此前 5 个 workflow 没有一个跑本仓自己的 pytest。普通 pull_request 事件（无 secrets）+ Python 3.10/3.13 矩阵 + 覆盖率门槛（pyproject `fail_under = 85`，门槛对自己生效）。
- **打包与导入（P1）**：新增 `pyproject.toml`（`pip install -e .` 可装，`touchstone` CLI 入口）；`verify/` 包化；移除全部模块内 `sys.path.insert` hack，包内 sibling 导入统一为 `from touchstone import x`；运行方式统一为 `python -m touchstone.<module>`（workflow/README/RUNBOOK 已同步），`requirements.txt` 降级为指向 pyproject 的薄引用。
- **仓库卫生（P1）**：移除入库的构建产物——`mutants/`（mutmut 变异快照，约占仓库三分之一体量，其中还残留着已删测试的陈旧副本）与 `.coverage`；`.gitignore` 补全（coverage/pytest/hypothesis/mutants/egg-info/venv/运行产物）。
- **ghclient「唯一入口」承诺兑现（P1）**：`autonomy` 的 5 处裸 urllib 调用（check_base_fresh / update-branch / GraphQL 入队 / merge 执行 / marker 评论）全部迁至 ghclient——自动合并链路此前无任何重试与 Retry-After 处理；orchestrator 清理 urllib 残留 except 与死导入。
- **可排障性（P2）**：learning_loop 三处静默吞异常（LLM 调用回退 / 评审线程解析 / diff 取数）补 stderr 留痕；`gitcode_check` 的 `GITCODE_DIFF_CMD` 执行不再启用 shell（改 shlex.split，管道需求需显式 `bash -c` 包裹，让 shell 语义成为明示选择）。

### 2026-07-04

2026-06-25 技术方案评审已采纳意见（1–7、10）的落地实现（意见 8、9、11 明确不采纳）。修订设计与数据结构-流程锚定矩阵见 `docs/touchstone-design-revision.html`。

- **范围事实 ScopeFacts（意见 7）**：`contract_check.scope_facts()`——确定性修改范围（每文件增删/hunk 结构）+ 仓级路径规则命中（新增 `.touchstone/scope-rules.yaml`，human_curated）+ 内容指纹。`map_verdict` 接收 scope_facts：影响面推导 = 路径规则命中（确定性）∪ 类别推导（模型补充），敏感路径命中但模型零发现时影响面照样点亮；评审报告新增「确定性事实区」呈现机器实测修改范围。
- **Finding 方向化（意见 1、2）**：模型来源只给 `fix_direction`（方向）+ `fix_reasoning`（依据），PR-Agent 的 improved_code 补丁在归一时降级、不再进任何建议字段；`deterministic_patch` 通道仅确定性来源保留。每条发现附 `done_criteria` 达成判据（deterministic=规则复检 / review=定向复核问题）。`loop.author_actionable` 门槛改为「有 fix_direction」（suggested_fix 作过渡别名仍受理）。
- **收敛清单（意见 3）**：新模块 `touchstone/checklist.py`——逐项销项清单（open/done/waived/split 状态机），置顶评论 task list（人可读）+ 隐藏 JSON marker（权威状态，沿用 trusted_bodies 防篡改）双载体，每轮快照写入 `checklist-round-N.json`。author 经 ```touchstone-ack``` 代码块申报；申报是输入信号，评审方按达成判据复核后才销项（done 需复检不再命中，waived 需理由，split 需链接）。`loop_step` 清单语义：收敛=清单全部销项且无新增可自改发现；无推进=销项率连续为零且无 waived/split 申报（覆盖假修）。
- **轮次台账（意见 10）**：新模块 `touchstone/lineage.py`——记账主体从 PR 号改为内容指纹（文件集 Jaccard≥0.8 且 hunk 结构相似≥0.6 双阈值）。同源的「关旧开新」继承历史轮次消耗与未销项清单（从关闭 PR 的机器人评论重建，不新增存储；已合入的关闭不入台账），余额为零直接升级人工；`rounds-reset` label 人工授权重置。author 伪造历史 marker 不被采信（[bot] 过滤）。
- **版面模板（意见 4）**：评审报告七段版面抽出为 `touchstone/templates/review_report.md`（一等设计资产，代码只填充不定义版面）：①声明与风险横幅 ②总结 ③确定性事实 ④逐条发现（定位·方向·依据·达成判据）⑤收敛清单 ⑥验证结果 ⑦机器 marker。
- **加固**：`parse_diff`/`scope_facts` 对 unidiff 在畸形输入上抛出的库内异常（UnboundLocalError 等）按解析失败处理并显式标注（防静默故障约定不变，此前会打断评审主链）。
- **主设计文档回灌**：修订内容合并入 `docs/touchstone-design.html` 正文——§2.1 四个新概念、§3.2 Finding 字段改造、新增 §3.9 ScopeFacts / §3.10 ConvergenceChecklist / §3.11 RoundLedger / **§3.12 数据结构-流程锚定矩阵**（含内生控制变量单列，新增结构须同步矩阵行）、§4.8 反馈质量与收敛机制接口 + 七段版面定义、§5 一致性校验补记已解决冲突与有意接受的遗留项、§7 变更历史「阶段八」。
- **全流程可视化** `docs/touchstone-visualization.html`（自包含离线，无外部依赖）：8 节点可交互流程图 + 两轮切换，每节点展示真实中间状态（ScopeFacts / 归一前后 Finding / RiskAssessment / RoundLedger / 两轮清单 / loop marker / 七段报告实际正文 / ack 申报与解析）+ 内生控制变量当次取值。数据由真实模块逐环节执行采集（PR-Agent 输出与关闭 PR 检索为注入桩），页面自带验收标准声明。
- **真实数据回放发现并修复一处缺陷**：台账继承的种子清单（round=0）曾使同源新 PR 的第 1 轮被误判「无推进」直接升级（author 尚未获得修改机会）——`checklist.no_progress` 增加第 0 轮闸 + 回归测试。这正是意见 6「用真实数据核对中间状态」的预期收益。
- 测试 444 → **469**（+25：`tests/test_revision_items.py` 覆盖范围事实/字段改造/清单状态机与复核/台账同源与伪造防御/版面七段/种子清单回归），全绿、离线。

### 2026-07-03

v0.2.1 之后的积累：基准仓收敛到 **AKDI-SE/touchstone** main（PR #16 合入），并补齐文档与代码的一致性。

- **新增模块** `touchstone/gitcode_check.py`（GitCode 平台适配的可插拔检查闸）。生产代码 3840 → **4445 行**（17 模块）。
- **TF-GRPO 生产化差距**：`docs/learning-loop-design.html` 新增 §3.6，列出论文实现（~185 行）与生产落地之间的差距（奖励质量 / 蒸馏质量 / 收敛性 / 规模 / ground-truth 清理）——明确为建议性、与 `VERIFY_ENABLED`/`AUTONOMY_ENABLED` 无关。
- **workflow 加固**：`learn.yml` 的 `TOUCHSTONE_EXPERIENCE_REF`（经验库从受信任引用读取，防工作树投毒）+ 4 条回归测试。
- **文档对齐（本次）**：README / index / slides / 4+1 的「生产代码行数 / 测试用例数 / 工作流条数 / 功能区行数」全部更新到当前真实值（**4445 行 / 276 用例 / 14 测试文件 / 5 条 workflow**）；补回遗漏的 `gitcode_check` 模块；`gitcode-sync-todo.md` 的基准仓从 1587 改为 AKDI-SE。
- 测试 268 → **276**（+8：经验引用受信读取、TF-GRPO 生产化回归等），全绿、离线、无新增运行时依赖。

### 2026-07-02（公开发布前·架构审查后的安全加固）

架构审查后的安全加固与文档对齐（不改冻结契约字段、marker 仅追加、测试只增不削）。

- **确定性影响面兜底（P0，最关键）**：`map_verdict` 除按 category 定级外，新增 `review_provider.deterministic_blast`——直接从改动文件【路径】判定影响面（migration/`*.sql`/`*.proto`/schema → cross_module_contract；auth/crypto/secrets 等路径 → security_surface），与评审侧结果保守取并；命中严重影响面即【无视 LLM 类别】抬到 high → full_suite，并触发（可选的）自动合并否决。此前 blast 仅由 PR-Agent 给的 category 推导，评审侧漏判类别时高危改动会被误走 cheap_only、自动合并下仅凭 CI 绿放行——本条把主设计 §5 承诺的「确定性兜底」真正落地。
- **经验 provenance 到 id 级（P1）**：result marker 追加 `injected_experience_ids`（`learning_loop.active_ids`），使坏经验可【单条】归因与回退（此前仅 `injected_types` 类型级，见数据采集设计 取舍 2）。
- **文档对齐**：4+1 / index / slides 的「生产代码行数」「测试用例数」更新到当前值（3840 行 / 254 用例）；主设计 §5 该遗留项改为「已落地」。
- **loop marker 防伪造（P0）**：loop 状态此前从 PR 的【全部】评论解析——评论任何人都能发，author 可伪造 marker（同轮次+空 history）洗掉震荡/无推进等抗博弈闸。现只解析机器人自己发的评论（`loop.trusted_bodies` 按发帖人过滤，orchestrator 经 `GET /user` 确认身份；无法确认时降级全量并告警）。
- **required 接力检查 fail-closed（P0）**：`_run_relay` 此前把 skipped/neutral 一律算过——author 用 [skip ci]/路径过滤让源 CI 跳过即可绿总闸，自动合并下会放行未经验证的代码。现 required 的 relay 只认 success；非 required 保持宽松（兼容既有流水线）；确需放宽对该检查设 `allow_skipped: true`。
- **第七道闸·基线新鲜度（P0，对照 bors/merge queue）**：`decide_auto_merge` 新增 `base_fresh` 闸——CI 绿是对旧 main 算的就不自动合（两个各自绿的 PR 合在一起可能语义冲突，即 merge skew；`sha` 参数只防 head 再 push、不防基线过期）。live 执行前 `check_base_fresh` 比对 PR base sha 与 base 分支当前 head；过期则调 GitHub update-branch 带上最新 main、CI 重绿后下轮再判；评估失败仅记 None 不误拦，评出过期必拦。长期演进建议改用 GitHub 原生 merge queue（见主设计 §2.6），不自建合并执行器。
- **SEC-\* 规则冻结（P1）**：内置 SEC-001 只作离线兜底、不再新增模式——完整密钥扫描经 checks.yaml 的 relay 挂 gitleaks/semgrep（主设计 §4.7 已加示例行）。
- **成熟工具接缝三件（P1/P2）**：① 变异测试可经 `TOUCHSTONE_MUTATION_CMD` 换用 mutmut/cosmic-ray（外部命令，stdout 末尾数字作击杀率，失败回退内置 AST 变异）；② `AUTONOMY_MERGE_MODE=queue` 经 GraphQL enablePullRequestAutoMerge 走 GitHub 原生 merge queue/auto-merge（不自建合并执行器，direct 保留兜底）；③ 设 `TOUCHSTONE_RDJSON_PATH` 导出 Reviewdog rdjson，行内评论锚定长尾可交 reviewdog。
- **TF-GRPO 加固重施（P0，专项复检）**：审查发现 I1–I4 加固未曾合入 main，而新自学习代码把多仓真值采集接通后，I1（经验 id 不含仓·栈，多仓同类型互相覆盖）已成实际缺陷。现重施于新基线：`_exp_id` 含 `kind:repo:stack:finding_type`（I1）；`_distill_via_llm` 每轮用已蒸出候选重渲染注入 E（I2，真 multi-epoch）；`render_injection` 前 `_resolve_conflicts` 消解同 仓·栈·类型 的 emphasize/suppress 矛盾（I3）；`distill_semantic_advantage` 退化组（组内奖励无差异）跳过、并对【整组】带分对比归纳替代 top-2/bottom-2（I4，贴合论文、降小组取样方差）。
- 测试 251 → 268（+17：确定性 blast 按路径 / 评审漏判仍被路径抬级 / active_ids / 伪造 marker 过滤 / required-relay skipped 拒过 ×2 / base_fresh 闸 / is_base_fresh 纯判定 / 变异输出解析 / 外部变异命令 / rdjson 导出），全绿、离线。

### 2026-06-25（公开发布前·确定性红线门禁生效）

审查后修复：让「确定性红线门禁」真正生效，并接通若干悬空的安全机制（均不改冻结契约字段、marker 仅追加、测试只增不削）。

- **门禁生效（P0）**：`stack_rules`/`contract_check` 的 severity 改为取自规则（不再硬编码 warn），`enforced` 固化标志接入运行时——block_candidate 规则（CTR-001/SPR-TX-001/JAVA-EQ-001）立即阻断，warn 规则经固化后阻断。门禁输入纳入 `touchstone-rules` 发现（此前仅 contract-check，且 orchestrator 误引未定义变量）。内置 **SEC-001 离线密钥扫描器**（高精度正则 + 占位符过滤）；SEC-002（注入）仍标注为外部 SAST。SEC-001 **豁免测试文件**（密钥夹具是故意的，不据此阻断——兑现「宁可漏不误拦」；本条由 Touchstone 审自身 PR 时抓到）。
- **安全机制接通（P1）**：`loop` 按 category 排除 correctness（修 PR-Agent 源 PRA-* 漏网）；熔断改读真实 `auto_handled` marker（不再用低风险代理，hotfix 检测留作未来）；学习回路 `graduate` 接入 `main()`、result marker 追加 `injected_types`（candidate→active 自动达标需积累 A/B 数据，此前由人写 seed 驱动）。
- **配置/开箱（P2）**：`checks.yaml` 的 unit-tests 默认非必填（修开箱总闸恒红）；`preflight` 把 LLM_* 降为可选（评审走 PR-Agent，仅 verify 需要）；`run.py` clone 支持 GHE；`select_runner` 非 Python/Java 返回 None（不再误生成 pytest）；`calibrate.aggregate` 别名容错 + `main()` 经 `record_calibration` 构造记录。
- **清理（P3）**：删 `_SEVERE_BLAST` 死项、修 `review_provider` 过时 docstring、文档对齐。
- **契约检查精度**：`check_scope` 跳过 `<...>` 占位符 scope（未填的 pr.yaml 模板）——不再对每条 PR 刷假阳性 SCOPE-001（与 SEC-001 豁免测试文件同类；亦由审自身 PR 时 bot 报的 23 条 SCOPE-001 触发）。
- 测试 228 → 245（+17 锁定行为），全绿、离线。
- **dogfooding 验证**：PR #2 用 Touchstone 审自身——初版被总闸判 failure（SEC-001 误拦测试夹具），定位修复后判 success（见 RUNBOOK §8）。门禁拦下「看着对、实则误拦」、逼出正确修复的能力，在本仓自己身上得到证实。

### 2026-06-23（公开发布前·项目首个可用版本）

首个版本。

- **评审主链**:复用 PR-Agent,做发现归一、风险分流、回贴(顾问式,默认不阻断)。
- **确定性门禁**:契约一致性核对 + 栈专项规则(机器可检,命中即阻断),聚合为单一总闸 `touchstone/gate`。
- **独立验证 verify(默认关)**:异模型盲测 + 改前/改后对比 + 充分性阶梯(覆盖/变异);Python 与 Java 双 runner(参考级)。
- **渐进自治 autonomy(默认关)**:仅对校准达标的变更类放行,熔断保障;自主边界 = 验证边界。
- **校准与离线学习**:与人审吻合度/噪声;经验蒸馏含计数式与 **TF-GRPO**(arXiv 2510.08191——策略冻结 + 组内语义优势蒸馏经验当 token prior;经注入 llm,离线假-llm 测试覆盖,生产需旗舰模型端点)。人类输入:`seed_experience` 手写种子、红线 `TOUCHSTONE_PROTECTED_TYPES`(受保护类型永不 suppress)、`locked`(人锁定经验不被回路改写/退役)、奖励权重可配；附 examples/seed_experiences.py（10 条手写种子案例，可直接跑）。
- **GitHub 集成**:三条工作流(touchstone / calibrate / govern)。
- 生产代码约 3427 行 / 17 模块;228 个离线测试全绿(无需 LLM / 网络 / 外部服务);行覆盖率 83%。

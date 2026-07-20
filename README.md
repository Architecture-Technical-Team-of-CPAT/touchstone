# Touchstone

**给 AI 时代的代码合入装一把试金石。** Touchstone 在 GitHub PR 上做评审,默认只给建议;是否准予合入,交给一道客观、可复现、可审计的质量门禁,而不是 AI 的判断。

## 它是什么 / 它不是什么

**它是**:一个挂在 PR 上的评审与门禁系统。它把现成的 AI 评审(复用 [PR-Agent](https://github.com/qodo-ai/pr-agent))接进来,补上 PR-Agent 没有的那部分——发现归一、风险分流、确定性契约核对、栈专项规则、单一总闸,以及可选的独立验证与渐进自治。

**它不是**:一个"让 AI 替你点合并"的工具。自动合并是**可选、默认关闭**的能力;打开后,它的放行边界也被牢牢限制在"机器能验证的范围"之内。默认形态下,合入由人点,Touchstone 只提供建议与一道硬门禁。

## 为什么需要它 —— 核心理念

AI 写的代码越来越多,但 AI 评审有两个绕不开的弱点:

1. **似是而非的错误。** 大模型会给出读起来很合理、实则错误的判断。这类错误恰恰最难靠"再看一眼"发现。
2. **同源盲点。** 用来评审的模型,往往和写这段代码的模型是同一类。写错的地方,复审多半也看不出来——人审之所以可靠,部分正因为人和 AI 不同源。

结论很简单:**判断可以来自 AI,但"准予合入"这个决定不能押在判断上。** Touchstone 把准入权交给客观、可复现、可审计的机制——测试是否真的通过、确定性规则是否被触犯——而不是任何一段自然语言意见。

一条贯穿全系统的红线:**自主边界 = 验证边界。** Touchstone 能自动做到哪一步,取决于它能客观验证到哪一步;验证不到的,就只建议、由人决定。即使 AI 判错,只要它不进入前两类(正确性、红线契约)的准入裁决,它的错误也越不过质量门禁。

## 三类判定

Touchstone 把一个 PR 上要回答的问题分成三类,各用各的依据:

| 判定 | 问的是 | 靠什么定 | 能否阻断合入 |
|---|---|---|---|
| **正确性** | 改动真的对吗 | 机器:独立生成验收测试并真实执行(verify) | 能(开启 verify 时) |
| **红线契约** | 是否触犯硬性约定 | 机器:确定性规则与契约核对(无 LLM) | 能 |
| **质量** | 写得好不好 | Touchstone 的判断(复用 PR-Agent) | **不能,仅建议** |

只有前两类(可被机器客观裁定的)才有资格阻断合入;第三类质量判断永远只是建议。

## 一个 PR 会发生什么(默认形态)

1. PR 打开或更新,`touchstone.yml` 触发。
2. Touchstone 调用 PR-Agent 做评审,把它的输出**归一**成内部统一的 `Finding`,并据此做**风险分流**(`RiskAssessment`:风险档 + 影响面 → 是否需要验证、需要哪一档)。
3. 跑**确定性核对**:契约一致性(`contract_check`)与栈专项规则(`stack_rules`),这些不依赖 LLM,命中即为红线。
4. 把评审建议与发现**回贴**到 PR(advisory)。
5. 所有"必须通过才能合"的检查折进**单一总闸** `touchstone/gate`;它绿,才满足分支保护。是否点合并,由人决定。

开启可选能力后,在第 3 步与第 5 步之间会多出独立验证(verify),在第 5 步之后可由 autonomy 在达标类上替人点合并——两者默认都关。

## 默认形态与可选能力

- **评审(默认开)** —— 顾问式,只产建议与发现,不阻断。
- **确定性门禁(默认开)** —— 契约与栈规则 + SEC-001 密钥扫描 + DANGER-001 危险代码构造(`eval`/`exec`/`os.system`/`pickle`、subprocess 启用 shell)扫描,机器可检。`severity=block_candidate` 的规则(CTR-001/SPR-TX-001/JAVA-EQ-001/SEC-001/DANGER-001)命中即阻断;`warn` 规则经校准固化(`enforced`)后升级为阻断。SEC-002(注入)依赖外部 SAST。
- **独立验证 verify(默认关)** —— 用**异于评审的模型**、盲于实现地生成验收测试,在 git worktree 上对改动前/改动后两版分别执行,要求"改后通过 ∧ 改前失败 ∧ 覆盖/变异达标 ∧ 回归绿"才判正确。是 Touchstone 的核心,分量也最重。
- **渐进自治 autonomy(默认关)** —— 仅对经校准证明"放行靠谱"的变更类,才开自动合并,且有熔断保障;自动放行另有**作者信任闸**(仅 OWNER/MEMBER/COLLABORATOR 或显式白名单,fork 陌生作者不进自动合并,fail-closed)。自主边界严格等于验证边界。
- **学习回路 learning_loop(离线,Touchstone 的差异化核心)** —— 评审引擎复用的是开源 PR-Agent,所以真正属于 Touchstone 的创造,是这条让评审越用越准的回路:统计"人最终采纳了哪些发现、忽略了哪些",把规律写成自然语言经验,加进给 PR-Agent 的提示词里。它分两档:当前实际跑的是**计数式做法**(不训练模型、不改权重,只统计采纳率,已实现);更强的 **TF-GRPO**(取自 arXiv 2510.08191)**也已实现、离线可测**(机制见 `docs/learning-loop-design.html` 第 3 节;生产需一个参数固定的旗舰模型端点)。整条回路都离线跑、和评审分开(它出问题不影响评审);经验只用来调建议,绝不参与合入判定;新经验还要先用真实 PR 做 shadow A/B 对照,达标了才正式启用。

无论开关如何,所有检查都**聚合成一道总闸**对外暴露,分支保护只认这一个状态。

## 复用而非重造

Touchstone **不自己实现通用代码评审**——那部分复用成熟的 PR-Agent(跑在独立 venv,经子进程调用,不进本仓依赖)。Touchstone 只做 PR-Agent 没有的事:把不同来源的评审**归一**、把意见**映射**成风险档、确定性的**契约/栈规则**核对、**单一总闸**、**独立验证**、**自治**、以及**校准与学习**。其中**让评审越用越准的学习回路(TF-GRPO)是 Touchstone 最有差异化价值的一块**——评审引擎本身是复用的,自我改进才是 Touchstone 自己的创造(机制设计见 `docs/learning-loop-design.html` 第 3 节)。PR-Agent 没装或 LLM 没调通时,评审降级为只跑契约核对 + 栈规则,**并在 PR 评审里显式标注**(防静默故障,见下文「GitHub 集成」)。

## 快速开始

```bash
# 1. 依赖
pip install -e .                        # 依赖范围见 pyproject.toml
# 客户环境复现一致版本：pip install -e . -c constraints.txt
python -m touchstone.run --version      # 查看版本

# 2. 部署前自检——上线门(配置 + 连通 + 一次自检评审,红绿报告 + 退出码)
touchstone doctor                       # 退出 0=可上线；1=有阻断项。--no-net 离线；--json 机读
python -m touchstone.preflight          # 更轻的子集：只到连通性，不跑自检评审

# 3. 对任意 PR 跑一次评审(默认 dry-run,只打印不回贴)
python -m touchstone.run --repo owner/name --pr 314

# 4. 真回贴评论/check
python -m touchstone.run --repo owner/name --pr 314 --post
```

可选参数:`--repo-dir`(给定已 checkout 的 PR head,跳过自动 clone)、`--standards`(指定 `standards.yaml` 路径)。

> 所有测试与确定性核对**离线可跑**,无需 LLM、网络或外部服务。只有 PR-Agent 评审与可选的 verify 才需要 LLM 端点。

## 配置(`.touchstone/`,随仓库版本化)

| 文件 | 作用 |
|---|---|
| `standards.yaml` | **单一事实源规范**:同一份既喂 author 生成端,也喂评审端,两端不漂移 |
| `pr.yaml` | 提交契约模板:author 每个 PR 按此生成 `SubmissionContract` |
| `checks.yaml` | 可插拔检查闸配置:哪些检查折进总闸、哪些必须通过 |
| `pr-agent.yaml` | PR-Agent 原始输出 → 内部 `Finding` 的归一映射 |
| `best_practices.md` | 主观规则库,作为喂 PR-Agent 评审侧的 prompt 素材 |
| `acceptance.yaml.example` | 人核准验收规格样例(verify 用,可选) |

## GitHub 集成

### 快速部署到你的仓库（3 分钟）

不需要 fork 或 clone Touchstone 代码——在你的仓库创建一个 workflow，checkout Touchstone 的 release 版本、装依赖、跑 `orchestrator.py` 即可。

**Step 1**：在你的仓库创建 `.github/workflows/touchstone.yml`：

```yaml
name: Touchstone Review
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write
  checks: write

jobs:
  touchstone:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.base.ref }}
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -e .
      - name: Set up PR-Agent
        run: |
          python -m venv .pragent-venv
          .pragent-venv/bin/pip install -U pip
          .pragent-venv/bin/pip install pr-agent
      - name: Checkout Touchstone
        uses: actions/checkout@v4
        with:
          repository: Architecture-Technical-Team-of-CPAT/touchstone
          ref: v0.1.0                      # 锁定版本
          path: .touchstone-src
      - name: Run review
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_MODEL: ${{ secrets.LLM_MODEL }}
          TOUCHSTONE_PRAGENT_CMD: ".pragent-venv/bin/python -m touchstone.pr_agent_runner"
          TOUCHSTONE_SKIP_GATE: "true"
        run: |
          pip install -e .touchstone-src
          cd .touchstone-src && python -m touchstone.orchestrator
```

你的仓库不需要任何 Touchstone 代码——版本由 `ref: v1` 锁定。

**Step 2**：配置 Secrets（Settings → Secrets and variables → Actions → New repository secret）:

| Secret 名 | 必填 | 说明 | 示例 |
|---|---|---|---|
| `LLM_BASE_URL` | ✅ | LLM 的 OpenAI 兼容端点 | `https://open.bigmodel.cn/api/coding/paas/v4` |
| `LLM_API_KEY` | ✅ | LLM 端点的 API key | `your-key-here` |
| `LLM_MODEL` | ✅ | 评审用的模型名 | `glm-5.2` |
| `TOUCHSTONE_LLM_CONTEXT_TOKENS` | 推荐 | 模型上下文窗口（token）。2000 行 diff 约需 64K（含 prompt 开销 + 输出预留）；GLM-5.2 支持 128K | `131072` |
| `TOUCHSTONE_LLM_OUTPUT_TOKENS` | 推荐 | 模型最大输出（token）。2000 行 PR 的建议产出上限 ~7.5K，8192 覆盖不截断 | `8192` |
| `TOUCHSTONE_LLM_REFLECT_MODEL` | 可选 | improve 自评（self-reflection 打分，第二次 LLM 调用）专用的小模型；不设则沿用主模型。`touchstone.yml` 已默认 `glm-4.5-air`（自评是浅任务，小模型即可，improve 健康路径耗时近乎减半） | `glm-4.5-air` |
| `TOUCHSTONE_LLM_THINKING` | 可选 | 思考模式开关：`disabled`/`enabled`，逐调用注入请求体 `{"thinking":{"type":...}}`（GLM 方言）。思考型端点默认开思考时每调用先烧数千 reasoning token（大 diff 单调用 10min+ 的头号成因）；优先在网关侧对 key 默认关（治本），网关不可改时配 `disabled`。`touchstone.yml` 仅透传此 secret、不预置默认值，故未设=随端点默认（思考型端点须显式配 `disabled` 才关） | `disabled` |

> `GITHUB_TOKEN` 由 GitHub Actions 自动提供，无需手动配。

**Step 3**：配置 Variables（Settings → Secrets and variables → Actions → Variables tab）:

| Variable 名 | 默认 | 说明 |
|---|---|---|
| `TOUCHSTONE_MAX_DIFF_LINES` | `1000` | 单 PR 行数上限，超限不调 LLM 直接 block 并提示拆分。设 `0` 关闭；建议 `500`–`2000` |

**Step 4**：分支保护（Settings → Branches → Branch protection rules）:
- 把 `touchstone/gate` 设为 **Required status check**——确定性检查不过就拦。

**Step 5**：验证——开一个测试 PR，应看到：
- PR 评论里出现 **Touchstone · AI Committer 代码检视** 评审（含「AI 评审」+「静态检查」分段）。
- check `touchstone/gate` 为 success（无 block 级发现时）。
- 若 LLM 未配通，评论顶部会出现 `⚠️ AI 评审...` 横幅（不静默）。

### 五条工作流

- `touchstone.yml` —— PR 触发:评审 + 高风险时的 verify → 回贴 + 汇总成总闸。
- `calibrate.yml` —— 定时:从已合 PR 重建"与人审吻合度 / 噪声"报告。
- `govern.yml` —— 定时:把复发的发现固化为硬门禁、按 revert/hotfix 信号做熔断校准。
- `learn.yml` —— 定时:离线学习回路(计数式蒸馏 + TF-GRPO),产出经验库并经 PR 合入。
- `seed.yml` —— 手动/定时:从手写种子案例初始化或补充经验库。

### AI 评审引擎（PR-Agent + LLM）

评审引擎是开源的 **PR-Agent**(Apache-2.0,pip 包),装在**独立 venv**、不进本仓依赖,经子进程适配器 `pr_agent_runner.py` 调用。`touchstone.yml` 已含一步把 pr-agent 装进 `.pragent-venv`,并用 `TOUCHSTONE_PRAGENT_CMD` 指过去。要让 LLM 评审生效,配好上面的 `LLM_*` secrets 即可。

pr-agent **取 PR** 用 workflow 自带的 `GITHUB_TOKEN`——**无需额外配置**。其它(GitLab/Bitbucket 等)git provider 未适配,本仓面向 GitHub。

**LLM 调用调优**:`pr_agent_runner` 在子进程内对 LiteLLM 做了四件事——① 主模型与自评模型(`TOUCHSTONE_LLM_REFLECT_MODEL`,默认 `glm-4.5-air`)双双启用**流式**,把单次调用的墙钟超时语义校准为「真死等必杀、持续出字不误杀」;② tenacity 重试层数由 `TOUCHSTONE_LLM_NUM_RETRIES` 控制(默认 0=不重试——基于全量 run 实证:真实失败全发生在 600s+ 后、轮内重试救回率 0);③ 仅对秒级抖动类失败(快速 5xx/瞬断)在快窗(`TOUCHSTONE_LLM_RETRY_FAST_WINDOW`,默认 120s,0=纯单次)内自动 N+1;④ 经 `TOUCHSTONE_LLM_THINKING` 可逐调用下发思考模式开关(pr-agent 对 GLM 无思考控制路径,由 runner 的 acompletion 围栏注入)。流式有 `TOUCHSTONE_LLM_STREAM` 回退开关(默认 `true`)。四者都在子进程内自洽,不进本仓依赖。

**反静默故障**:若 pr-agent 没装好、取 PR 失败、或 LLM 端点没调通,Touchstone **不会**静默降级成"0 条发现"——它会把降级说明写在贴到 PR 的评审评论顶部、并反映在 check 标题里:

- `⚠️ AI 评审未运行` —— pr-agent 未安装/不可用,本次只含确定性契约与栈规则核对;
- `⚠️ AI 评审取 PR 失败` —— pr-agent 已启动但取不到该 PR(git provider/凭据/网络),检查 `GITHUB_TOKEN`;
- `⚠️ AI 评审的 LLM 调用失败` —— pr-agent 已跑但 LLM 未成功响应,请检查 `LLM_*` 配置;
- `🚫 PR 体量超限` —— 超 `TOUCHSTONE_MAX_DIFF_LINES`,不调 LLM、直接 block 提示拆分。

这样人一眼就能看出"这次到底有没有 AI 评审",不会被空评审误导。

## 仓库结构

```
.
├── .touchstone/                # 仓内策略(随仓库版本化,离线生效)
│   ├── standards.yaml          # 单一事实源规范(喂 author 与评审两端)
│   ├── pr.yaml                 # 提交契约模板
│   ├── checks.yaml             # 可插拔检查闸配置
│   ├── pr-agent.yaml           # PR-Agent 输出 → Finding 归一映射
│   ├── best_practices.md       # 主观规则库(评审 prompt 素材)
│   └── acceptance.yaml.example # 人核准验收规格样例(verify 用)
├── touchstone/                 # 评审判断 + 门禁/集成 + 闭环/治理 + 可观测 + 入口
│   ├── orchestrator.py         # 主编排:评审归一 → 风险分流 → 回贴(advisory)
│   ├── review_provider.py      # 评审来源适配(PR-Agent / 优雅降级)
│   ├── pr_agent_runner.py      # PR-Agent 调用(独立 venv 子进程)+ LLM 调用调优(重试/流式/自评换模)
│   ├── render.py               # 评审报告渲染(分段 + rdjson)
│   ├── stack_rules.py          # 栈专项确定性规则(DI/事务/equals/异常/日志/路径契约)
│   ├── contract_check.py       # 确定性契约一致性核对(无 LLM)
│   ├── gen_best_practices.py   # 主观规则 → PR-Agent prompt 素材
│   ├── checklist.py            # 收敛清单:逐项销项 + author ack 申报(done/waived/split)
│   ├── loop.py                 # 反馈循环 loop_step(有界、防震荡、可升级)
│   ├── calibrate.py            # 影子校准:与人审吻合度 / 噪声
│   ├── learning_loop.py        # 离线学习:从校准记录蒸馏候选
│   ├── distill.py              # 经验蒸馏器(可插拔:TF-GRPO + 计数式)
│   ├── experience_store.py     # 经验库读写(原子写 + 受信 marker 信任根)
│   ├── ground_truth.py         # 校准真值集
│   ├── lineage.py              # 轮次台账:同源重提检测 + 历史欠账继承
│   ├── llm_budget.py           # LLM token 预算(上下文/输出窗口)
│   ├── logging_setup.py        # 统一日志配置
│   ├── checks.py               # 可插拔检查闸聚合 → 单一 touchstone/gate
│   ├── govern.py               # 治理:固化提案(发现→硬门禁)+ 熔断
│   ├── autonomy.py             # 渐进开放自动合并(可选、默认关)
│   ├── metrics.py              # 评审度量(可信率/静默故障/放行率)
│   ├── metrics_issue.py        # (可选)度量看板:常驻 issue 每轮刷新趋势
│   ├── alert.py                # 阈值告警(可信率跌破/静默故障超额)
│   ├── telemetry.py            # (可选)外部 telemetry 转发
│   ├── ghclient.py             # GitHub REST/GraphQL 客户端(连接池 + 退避)
│   ├── gitcode_check.py        # GitCode 平台适配检查(可插拔检查闸)
│   ├── preflight.py            # 起步自检(配置 + 连通)
│   ├── doctor.py               # 上线自检(配置 + 连通 + 一次自检评审)
│   └── run.py                  # 独立入口:python -m touchstone.run --pr N
├── verify/
│   ├── verify_change.py        # 质量门禁核心:独立验收测试 + 改前/改后对比 + 充分性阶梯
│   └── runners.py              # Python(pytest+coverage)/Java(Maven+JaCoCo+PIT) runner + 外部变异接缝(防伪)
├── tests/                      # 818 个离线用例 / 37 个文件(无需 LLM / 网络 / 外部服务)
└── .github/workflows/          # touchstone.yml · calibrate.yml · govern.yml · learn.yml · seed.yml
```

生产代码约 8350 行 / 31 个模块;测试 818 个用例 / 37 个文件,全绿、离线;行覆盖率 93%(核心逻辑模块 90–100%;GitHub API / 子进程 / LLM / CLI 等集成层经 mock 覆盖)。

## 状态与边界(诚实交代)

- **评审 + 确定性门禁**:已实现、可跑。
- **verify(独立验证)**:参考级实现,**默认关、尚未规模化实跑**。Python(pytest + coverage)与 Java(Maven + JaCoCo + PIT)双 runner;Python 侧变异为自写 AST,生产应换 mutmut/cosmic-ray;需要一个异于评审的 LLM 端点(离线测试以桩覆盖)。
- **autonomy(自治)**:默认关,需要足够的校准数据证明某变更类"放行靠谱"后才逐步开放。
- **学习回路 / TF-GRPO**:两档都已实现、离线可跑——计数式蒸馏,以及核心的 **TF-GRPO**(策略冻结 + 组内语义优势把经验蒸馏成注入提示词的 token prior,取自 arXiv 2510.08191,机制见 `docs/learning-loop-design.html` 第 3 节)。TF-GRPO 经注入的 `llm` 调用旗舰模型,离线用假 llm 覆盖测试;生产需配置一个参数固定的旗舰模型端点(`LLM_BASE_URL`/`LLM_API_KEY`/`TOUCHSTONE_FLAGSHIP_MODEL`)与一份历史 PR 真值集。出于稳健,新经验先 shadow A/B 达标才注入、且只影响建议不碰合入。因为经验是人能读写的自然语言,**人能直接读写它学到的东西**:手写种子(`seed_experience`)、审校候选、立红线(受保护类型永不 suppress、`locked` 经验不被回路改写/退役)、调奖励权重——见 `docs/learning-loop-design.html` 第 6 节。蒸馏器**可插拔**:`register_distiller` 注册自有实现、env `TOUCHSTONE_DISTILLER` 按名切换,`_distill_via_llm` 的 rollout/score/distill 三步也可单独注入——整体或局部换成你们自己的实现都行。
- 还有一些预留的可替换实现(比如内网 embedding、不同语言的测试 runner),默认都不启用,确认依赖就绪后再接入。

## 设计文档

- `docs/DEPLOYMENT.md` —— 客户版部署指南(从零到上线，区别于 RUNBOOK 的作者自测视角)
- `docs/incident-runbook.md` —— 运维故障排查手册(症状→诊断→处置，接 doctor/metrics 诊断)
- `docs/touchstone-design.html` —— 详细设计(自包含离线 HTML,含内联 SVG)
- `docs/touchstone-arch-4plus1.html` —— 4+1 架构视图
- `docs/touchstone-index.html` —— 模块与交付物索引
- `docs/touchstone-slides.html` —— 评审用 slides
- `docs/touchstone-on-pr-agent.html` —— 与 PR-Agent 的复用边界
- `docs/learning-loop-design.html` —— 学习回路设计

## 名称由来

试金石是古人辨真金与愚人金的器物:不听成色的说辞,把东西在石上一划,真假立现。它也指"评判事物的标准"。这两层意思正是本系统的立身之本——**对似是而非的判断,不信表象,只认那道客观的标尺。**

## 运维与安全

- **版本**：遵循 SemVer，版本号单一来源在 `touchstone/__init__.py`。首个公开发布 v0.1.0，历史见 `CHANGELOG.md`。
- **可观测性**：每轮评审产出 `touchstone-metrics.json`（评审可信率、静默故障、放行率、引擎状态分布）。聚合：`python -m touchstone.metrics touchstone-metrics.json`。用于把 LLM 静默故障从"事后追问"变为"主动可见"。
- **部署前自检**：`python -m touchstone.preflight` 校验配置完整性与连通性，含"不设就撞坑"的关键项（如 `TOUCHSTONE_LLM_CONTEXT_TOKENS` 未按模型卡设置的警告）。
- **安全**：漏洞披露流程与本系统的安全边界见 `SECURITY.md`。请勿通过公开 issue 报告安全问题。

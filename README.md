# GEO 采样 Skill / GEO Sampling Skill

支持 Answerbit 真实只读分析，也提供独立的合成数据试跑。使用 Python 3.9+ 和 Python 内置的 SQLite（SQLite 3.30+）。

## 生成运营看板

准备当前飞书题库导出，安装 `requirements.txt`。在获批的本机环境设置 `ANSWERBIT_BRAND_ID`。运行时隐藏输入只覆盖 Kalodata 的只读 Key，或在当前运行环境配置 `ANSWERBIT_API_KEY`。

Agent 启动真实取数前应告知所需 Key 和预计耗时，安全配置完成前暂停。约 1,000–3,000 条记录可先估 3–8 分钟，限流与采样窗口会影响时间。

```sh
python3 dashboard.py refresh /path/to/question-bank.xlsx .trial/dashboard.sqlite .trial/GEO运营看板.html --excel .trial/GEO采样明细.xlsx
```

默认交付同批简短报告、`GEO运营看板.html` 和 `GEO采样明细.xlsx`。报告写在对话内，按核心数据表、一段结论与判断、最多三个优先行动项组织。核心表包含「提及率」，可同时采用 Answerbit 平台快照，并标明各自来源。飞书会话支持附件且获准交付时，两个文件作为当前用户可下载的产物。HTML 在浏览器里离线筛选和对照。Excel 可按题号、日期、市场、平台、分组和标签筛选，并有单独的异常表。两份文件保留必要的回查字段，含原文的 SQLite 留在获批本机。

再次刷新同一输出位置时，会自动与上一次成功 HTML 的整批数据对比。平台快照优先，缺项时使用 Skill 自算指标。首次运行注明无历史数据，题库、题目范围、分析口径或窗口长度不同会提示可比限制。失败尝试不更新上次基线。

只对账飞书题库和 Answerbit 返回题目的交集。正常和异常明细均带已确认的飞书题号。题库外记录单列数量，正式批次照常交付。题库内有候选但题号无法确认时，保留上一批并生成 `.question-check.xlsx`。

在本机执行 `python3 answerbit.py query .trial/dashboard.sqlite` 可只读查询同批指标和异常。查具体回答时，执行 `python3 local_answers.py .trial/dashboard.sqlite .trial/GEO运营看板.html` 并打开返回的本机地址。在「非明确推荐记录」点击「查看原文」，可定位题号、题文和 Answerbit 回答。保持查询进程运行。这两种查询都无需 Answerbit Key。

默认读取北京时间最近七个自然日（含当天）的全范围，也可附 `--begin` 与 `--end`。平台原生快照有独立范围与读取时间。刷新失败保留最后成功数据并显示失败阶段。使用 `snapshot` 代替 `refresh` 可从已保存的兼容 SQLite 离线生成；附 `--fetch-platform` 时仅补拉 Answerbit 原生快照。

看板按总览、平台快照、品牌表现、官网引用、回答表现、运营行动、对照分析和指标说明分页。「指标说明」可离线查看定义、分母和限制。同一窗口切页时保留主筛选和对照条件。平台快照页使用独立的全范围口径。

每次正式 `refresh` 都重算首轮推荐，并在同一次读取中自动复检该批未知。要补做历史快照的推荐复检，用 `review` 代替 `refresh`，沿用该快照和看板的路径。历史复检只重读首轮未知任务，可用 `--defer-unavailable` 暂缓上轮未读到的旧任务。「回答表现」页的非明确推荐表支持市场、平台、日期、分组、标签、推荐判断和待确认原因筛选。刷新时只给能唯一匹配飞书题号并通过校验的有效回答保存原文。原文和对应题文仅保存在本机 SQLite，复检结果单独绑定该批快照。包含原文的库和本机查询入口仅供当前运行者使用，留在获批的本地目录。旧库缺少原文时需要重新刷新。

市场、平台、分组和标签支持多选，空选代表全部。市场、平台和分组在同维度内取并集，跨维度取交集；标签可切换匹配全部或任一。打开「对照分析」后，当前范围沿用主筛选，对照范围单独设置日期和各维度。页面先展示三个关键指标的差异，详细指标可展开，两组有共同的单一竞品集合才显示份额差值。Answerbit 原生区可切换品牌提及率、平均排名和曝光效果分数，查看对应趋势和平台竞品排行。运营行动清单随主筛选更新，每项保留代表题和回查定位。

## 试跑

本节仅为合成测试流程；真实使用按上文直接刷新看板。预检和发布由 GEO 负责人操作，测试运营端运行同步和查询。

```sh
python3 archive.py prepare examples/demo-batch.json .trial/draft.sqlite
python3 archive.py publish .trial/draft.sqlite .trial/shared
python3 archive.py sync .trial/shared .trial/operator.sqlite
python3 archive.py query .trial/operator.sqlite
python3 -m unittest -v
```

预检成功后先看输出，再执行发布。每个批次使用独立的草稿路径。预检报错时停止，不发布该批。发布将快照写入共享目录，再更新测试批次索引 `latest-demo.json`。同步失败返回非零，并报告保留的本地批次（如有）。只读查询返回分组和各指标的分子、分母，`null` 表示不适用。参见[指标定义](skill/geo-sampling/references/metric-definitions.md)。

## 输入

参照 [合成批次](examples/demo-batch.json)。`batch` 固定批次编号、题库版本和带时区的采样时间窗（左闭右开）。`synthetic` 和 `retrieval_complete` 都必须为 `true`。前者隔离测试数据，后者表示输入已取全，仍可能存在平台漏采。独立的 `plan` 定义实际启用的题目、语言、平台和轮次。回答记录须有来源记录 ID、原题文、采集时间和状态。正式引用是 URL 数组，`[]` 代表明确无引用，缺字段或 `null` 代表未知。

工具只将白名单字段写入快照。不保存题文、回答、原始引用 URL 或密钥。真实接入前还需验证 Answerbit 的只读品牌范围、分页、实际启用题单和引用字段，确认公司批准的共享位置。合成测试不调用外部服务。

## 运行 Skill 时拉取 Answerbit

真实读取入口与合成试跑隔离。先在获批的本地运行环境安装 `requirements.txt`，准备飞书《一期题库最终版_全部国家》的 XLSX 导出，并配置 `ANSWERBIT_BRAND_ID`。每位运行者必须自行提供一个仅覆盖目标品牌的 Answerbit 只读 Key。终端支持隐藏输入，Agent 等非交互运行需在启动环境配置当前用户自己的 `ANSWERBIT_API_KEY`。没有 Key 时停止真实取数。请在 Answerbit 创建时选择只读和品牌范围，客户端的查询成功不能证明凭证没有其他权限。

```sh
python3 answerbit.py refresh /path/to/question-bank.xlsx .trial/answerbit.sqlite --begin YYYY-MM-DD --end YYYY-MM-DD
```

已归档快照可由有本地文件权限的维护人员用 `python3 answerbit.py query .trial/answerbit.sqlite` 离线核对。该命令不连接 Answerbit，也不是 Skill 的真实取数入口。

刷新和查询均可附加 `--group-id` 及一个或多个 `--tag-id`，按分组和所有所选标签的交集计算。摘要分别列出 Answerbit 分组及标签，标签保留 ID、名称和类型。飞书题库分类另存。多标签题在总体只计一次，不能把标签组相加。标签字段缺失或冲突时列异常；旧快照要重新刷新才能使用这些筛选。

刷新使用官方的回答列表和详情查询，仅读取既有记录。列表分页须与接口总数闭合；已完成分析的匹配记录逐条取详情。用各市场页的「编号」「问题内容」「分类名称」对账，审阅页和说明页不参与。题文仅做 Unicode 规范化及空白归一。同一句题文对应多个市场时，只接受唯一可区分的市场标签。采样网络地区不用于推断题号所属市场。未匹配、歧义和引用缺失单列。Excel 导出变更后重新配置本地文件路径或替换批准的本地副本。

仅运行本节的命令行指标入口时，本地库保存题号、市场、分类、采样日期、任务 ID、返回题文、点名状态与指标判定。匹配异常可用同批已确认的提问 ID 和相同题文补齐题号，有冲突时仅列候选。旧批未保存候选时需重新取数。题库文件的 SHA-256 作为版本标识，该命令行入口在计算后丢弃回答原文。没有独立的实际启用计划时，覆盖率不可计算。无可用记录、分页不全或详情核对失败均保留上一份本地库。

`answerbit-observed/3` 新增市场竞品对比、首次位置、推荐初判、中立推荐题单和平台情感对账。用独立的快照路径刷新。历史快照仍可查询原指标，新字段提示「需刷新」。详见[指标定义](skill/geo-sampling/references/metric-definitions.md)。

参考 [Answerbit OpenAPI 接入](https://answerbit.qq.com/zh-CN/docs/external/4-openapi-getting-started) 和 [回答查询](https://answerbit.qq.com/zh-CN/docs/external/4-openapi-getting-started/answers)。

默认「提及率」只使用返回题文未点名 Kalodata 及审核别名的有效回答，分子扣除明确负面。点名题单独展示；首页包含判断、优先行动和可展开的数据状态。历史快照未保存完整返回题文或题目范围时，会注明回算来源和对比限制。

## 运营场景和公开范围

看板新增按发现度、数据引用、对比、品牌健康划分的场景表，随筛选重算；见[场景定义](skill/geo-sampling/references/analysis-scenarios.md)。标签序号不等于接口 ID，点名状态仍按实际题文判断。

内部飞书题库链接通过 `GEO_QUESTION_BANK_URL` 在本机配置，未配置时不显示链接。不要提交真实题库、HTML 看板、Excel、原文库、日志或凭证；HTML 和 Excel 也包含内部提问与统计。团队和品牌 ID 是既有授权保留的资源标识，不是访问凭证。安装时需保留完整仓库目录；单独复制 `skill/geo-sampling` 不包含 Python 运行工具。

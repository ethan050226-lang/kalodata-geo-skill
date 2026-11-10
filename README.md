# GEO 采样 Skill / GEO Sampling Skill

支持 Answerbit 真实只读分析，也提供独立的合成数据试跑。使用 Python 3.9+ 和 Python 内置的 SQLite（SQLite 3.30+）。

## 试跑

在仓库目录运行。预检和发布由 GEO 负责人操作。运营只运行同步和查询。

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

本地库保存题号、市场、分类、采样日期、任务 ID 与指标判定。中立推荐仅额外保留对应提问，供运营回查。题库文件的 SHA-256 作为版本标识，回答原文在计算后丢弃。没有独立的实际启用计划时，覆盖率不可计算。无可用记录、分页不全或详情核对失败均保留上一份本地库。

`answerbit-observed/3` 新增市场竞品对比、首次位置、推荐初判、中立推荐题单和平台情感对账。用独立的快照路径刷新。历史快照仍可查询原指标，新字段提示「需刷新」。详见[指标定义](skill/geo-sampling/references/metric-definitions.md)。

参考 [Answerbit OpenAPI 接入](https://answerbit.qq.com/zh-CN/docs/external/4-openapi-getting-started) 和 [回答查询](https://answerbit.qq.com/zh-CN/docs/external/4-openapi-getting-started/answers)。

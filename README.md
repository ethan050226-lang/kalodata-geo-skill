# Kalodata GEO 归档工具

首版仅用于合成数据试跑。使用 Python 3.9+ 和 Python 内置的 SQLite（SQLite 3.30+）。

## 试跑

在仓库目录运行。预检和发布由 GEO 负责人操作。运营只运行同步和查询。

```sh
python3 archive.py prepare examples/demo-batch.json .trial/draft.sqlite
python3 archive.py publish .trial/draft.sqlite .trial/shared
python3 archive.py sync .trial/shared .trial/operator.sqlite
python3 archive.py query .trial/operator.sqlite
python3 -m unittest -v
```

预检成功后先看输出，再执行发布。每个批次使用独立的草稿路径。预检报错时停止，不发布该批。发布将快照写入共享目录，再更新测试批次索引 `latest-demo.json`。同步失败返回非零，并报告保留的本地批次（如有）。只读查询返回分组和各指标的分子、分母，`null` 表示不适用。参见[指标定义](skill/kalodata-geo-baseline/references/metric-definitions.md)。

## 输入

参照 [合成批次](examples/demo-batch.json)。`batch` 固定批次编号、题库版本和带时区的采样时间窗（左闭右开）。`synthetic` 和 `retrieval_complete` 都必须为 `true`。前者隔离测试数据，后者表示输入已取全，仍可能存在平台漏采。独立的 `plan` 定义实际启用的题目、语言、平台和轮次。回答记录须有来源记录 ID、原题文、采集时间和状态。正式引用是 URL 数组，`[]` 代表明确无引用，缺字段或 `null` 代表未知。

工具只将白名单字段写入快照。不保存题文、回答、原始引用 URL 或密钥。真实接入前还需验证 Answerbit 的只读品牌范围、分页、实际启用题单和引用字段，确认公司批准的共享位置。合成测试不调用外部服务。

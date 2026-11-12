# GEO 采样 Skill / GEO Sampling Skill

只读分析 Answerbit 已有回答，匹配飞书题号，生成可追溯的看板与简报。运行要求 Python 3.9+、SQLite 3.30+及requirements.txt依赖。安装保留完整仓库；单独复制Skill目录不包含Python工具。

## 用户运行

Agent先确认运营国家（可多国），引导安全配置品牌级只读Key并说明预计耗时，两项齐备前不真实取数或发布。本次已明确的无需重复问，历史国家配置须确认沿用。交付数据仅含确认国家，HTML内嵌数据与附件也须裁剪；不能裁剪则停止共享。国家自报不替代访问授权。

询问是否需要定时、频率、时间及接收位置。推荐并默认每周一北京时间上午11:30，汇总上一周周一至周日；用户确认后才设置。已有订阅更新原任务，不擅自改成默认时间。详见[定时推送](skill/geo-sampling/references/scheduled-delivery.md)。定时依赖运行Agent已有的持久调度、数据、发布和消息能力；缺项保存待接入，不能称已启用。用户禁止CLI时，不运行本仓库命令行工具。

## 管理员私有配置

- Answerbit：运行者安全配置ANSWERBIT_API_KEY，或交互终端隐藏输入。品牌范围用ANSWERBIT_BRAND_ID，或私有.trial/answerbit-resources.json；可用ANSWERBIT_RESOURCE_CONFIG改路径。结构为`{"brand_id":"你的品牌ID","team_id":"你的团队ID"}`，只传brand_id查询。Key格式通过后才读资源文件；查询成功不证明Key没有写权限，需在平台核实只读及品牌范围。
- 在线题库：GEO_QUESTION_BANK_URL；配置拥有知识库及电子表格只读权限的官方lark-cli，必要时用LARK_CLI指定路径。面向核查的题库链接可用GEO_QUESTION_BANK_URL；未配置则隐藏，不提交内部地址。
- 飞书交付：独立授权及私有GEO_FEISHU_CONFIG，配置指定位置与获准接收者，按[飞书交付](skill/geo-sampling/references/feishu-preview.md)验收。

不提交密钥、资源ID、私人配置、真实题库、原文、日志、数据库、Excel或真实HTML。Answerbit与飞书凭证分开，原文SQLite仅留获批本机。

## 运行命令

以下仅供获准使用命令行的环境；国家交付裁剪仍由Agent落实，工具取回品牌全量不代表可以全量共享。

```sh
python3 geo_run.py /path/to/question-bank.xlsx .trial/dashboard.sqlite .trial/GEO运营看板.html --excel .trial/GEO采样明细.xlsx
```

默认最近七个北京时间自然日（含当天），可加`--begin YYYY-MM-DD --end YYYY-MM-DD`；定时默认前七个完整自然日，与手动窗口不同。refresh在Key就绪后先更新题库缓存，再取数；完整读取必需国家页，检查题号、版本和分页。失败保留上一批。未配置在线地址时用本地Excel并标明来源；`--offline-bank`明确用缓存，`--feishu-bank <URL>`临时指定在线题库。

| 目的 | 入口 |
|---|---|
| 仅验证在线题库，无需Answerbit Key | `python3 feishu_bank.py .trial/在线题库.xlsx` |
| 仅本地看板 | `python3 dashboard.py refresh <题库.xlsx> <库.sqlite> <看板.html>` |
| 离线重绘，不更换历史题库 | dashboard.py同参数，snapshot代替refresh；`--fetch-platform`仅补读平台快照 |
| 历史推荐未知复检 | dashboard.py同参数，review代替refresh；`--defer-unavailable`仅在明确暂缓旧任务时使用 |
| 命令行指标刷新 | `python3 answerbit.py refresh <题库.xlsx> <库.sqlite> --begin YYYY-MM-DD --end YYYY-MM-DD` |
| 查询同批指标与异常 | `python3 answerbit.py query .trial/dashboard.sqlite`，可离线、无需Key |
| 本机原文查看 | `python3 local_answers.py .trial/dashboard.sqlite .trial/GEO运营看板.html`，保持进程运行后点查看原文 |

answerbit.py支持单个`--group-id`和可重复`--tag-id`，按交集计算；多分组用看板。平台分组、标签及飞书分类独立；总体按唯一任务计数。正常与异常明细须匹配已确认题号，无候选记录单列不入指标；有候选但歧义时输出.question-check.xlsx并停止发布。

## 交付和规范

默认交付已验收飞书链接及同批简报：核心数据表、一段结论、最多三条证据行动。Excel可本地生成，默认不上传。原文通过同批本机库查看；飞书共享版仅保留题文、题号、日期、平台和分组定位，工作台入口不是原文直达。

geo_run.py核对远端版本：reused复用已验收链接；pending_acceptance仍待实际预览和权限验收。证据绑定精确版本后，用`python3 feishu_publish.py <看板.html> <库.sqlite> --accept-proof <证据.json>`验收入账为published，才交付新链接。失败保留上一成功结果，不发布半批；尚无固定链接切换，多人发布使用统一串行环境。

- [Skill执行规程](skill/geo-sampling/SKILL.md)：启动、国家范围、流程及报告。
- [指标定义](skill/geo-sampling/references/metric-definitions.md)：公式、分母、缺失与可比条件。
- [运营场景](skill/geo-sampling/references/analysis-scenarios.md)：按实际分组/标签解读。
- [看板展示与图表](skill/geo-sampling/references/dashboard-design.md)：布局、交互和选图。
- [飞书交付](skill/geo-sampling/references/feishu-preview.md)：版本校验、分享、验收及失败处理。

## 合成试跑与验证

合成归档与真实读取隔离，输入参照examples/demo-batch.json。batch含版本、带时区窗口，synthetic及retrieval_complete均须为true；plan提供启用题目、语言、平台、轮次。引用空数组表示明确无引用，缺字段/null表示未知。合成快照只写白名单，不保存题文、原文、引用URL或Key。

```sh
python3 archive.py prepare examples/demo-batch.json .trial/draft.sqlite
python3 archive.py publish .trial/draft.sqlite .trial/shared
python3 archive.py sync .trial/shared .trial/operator.sqlite
python3 archive.py query .trial/operator.sqlite
```

先查看预检再发布；草稿使用独立路径。同步失败保留本地批次，null表示不适用。

回归检查：`python3 -m unittest discover -p 'test_*.py'`；手算及模板：`node test_scenarios.cjs`；可选浏览器检查：`node test_dashboard_ui.cjs <本地看板.html>`，可设置PLAYWRIGHT_MODULE、CHROME_PATH和SCREENSHOT_DIR。该验证使用独立浏览器，真实看板和截图保持私有。测试不能代替飞书真实运营账号验收。

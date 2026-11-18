# GEO 采样 Skill

只读分析 Answerbit 已有回答，与飞书题库核对后，按获准国家生成可交互的飞书看板和同批简报。支持国家及模型见[国家与模型配置](skill/geo-sampling/references/country-models.md)。

运行环境需要 Python 3.9+、SQLite 3.30+ 及 requirements.txt 依赖。安装时保留完整仓库；仅复制 Skill 目录缺少执行工具。

Agent 从[执行入口](skill/geo-sampling/SKILL.md#执行入口)区分查看、刷新、补发及订阅。统一 Agent 负责接收需求，中央环境负责执行。跨订阅合并、实际投递和持久调度由宿主接入，接入条件见[集中执行与国家推送](skill/geo-sampling/references/scheduled-delivery.md)。

## 管理员私有配置

题库连接先执行[自动检查、复用和开通指引](skill/geo-sampling/references/feishu-connection.md)。优先复用公司现有获批连接，默认应用身份；已有获批用户连接可在私有运行环境设置`GEO_FEISHU_BANK_IDENTITY=user`，不再被固定切回bot。该设置仅控制题库读取，与发布身份分开。程序不会自动登录、切换账号或申请权限；缺项由Agent按实际原因引导，一次实际全量读取通过后继续完整刷新。

- Answerbit：运行者安全配置ANSWERBIT_API_KEY，或交互终端隐藏输入。品牌范围用ANSWERBIT_BRAND_ID，或私有.trial/answerbit-resources.json；可用ANSWERBIT_RESOURCE_CONFIG改路径。结构见[空白私有配置样例](examples/private-resources.example.json)，填写brand_id、team_id和question_bank_url后存入被忽略的私有目录；不要修改或提交已填值的公开样例，只传brand_id查询。Key格式通过后才读资源文件；查询成功不证明Key没有写权限，需在平台核实只读及品牌范围。
- 在线题库：先读取GEO_QUESTION_BANK_URL，未设置时在Key就绪后读取同一私有资源文件的question_bank_url；复用拥有知识库及电子表格只读权限的官方lark-cli，必要时用LARK_CLI指定路径。题库保持只读。面向核查的题库链接可用GEO_QUESTION_BANK_URL；未配置则隐藏，不提交内部地址。
- 权限审批：任何需企业管理员审批的权限，先按[权限申请前置报告](skill/geo-sampling/SKILL.md#权限申请前置报告)向用户说明具体权限、用途、资源范围、缺项与替代方式；用户决定前不发起申请、扩权或为权限生效发布应用版本。复用既有权限不等于已验证实时题库可读，仍需实际读取验收。
- 题库只读授权由用户提供：缺少访问权时，将指定飞书题库以“可阅读”权限共享给当前Agent或其已配置应用，或完成对应只读授权。Agent负责连接配置、实读验收及后续复用；文件访问权和应用读取权限分别核验，需要企业管理员审批时先向用户报告，不以文件分享成功冒充接通。品牌信息先查私有配置，Excel读取组件由Agent处理。缺项按[中文提示规则](skill/geo-sampling/references/feishu-connection.md#失败时按原因处理)说明问题、处理人及下一步，不要求用户提供变量名、权限代码或程序库。
- 飞书交付：独立授权及私有GEO_FEISHU_CONFIG，配置指定位置、获准管理员和sharing_scope（默认admins_only；用户确认后可用tenant_readable公司内只读），按[飞书交付](skill/geo-sampling/references/feishu-preview.md)验收。identity可省略或留空，默认bot，既有user配置保持不变；它只影响发布，不阻止本地生成。

不提交密钥、资源ID、私人配置、真实题库、原文、日志、数据库、Excel或真实HTML。Answerbit与飞书凭证分开，原文SQLite仅留获批本机。

## 运行命令

完整运行使用以下入口，不停留在--check预检。工具可以在获批本机读取品牌全量，再生成独立的国家副本；分享前仍须核对接收权限。

```sh
python3 geo_run.py /path/to/question-bank.xlsx .trial/dashboard.sqlite .trial/GEO运营看板.html --market US --excel .trial/GEO采样明细.xlsx
```

默认最近七个北京时间自然日（含当天），可加`--begin YYYY-MM-DD --end YYYY-MM-DD`；定时默认前七个完整自然日，与手动窗口不同。refresh在Key就绪后先从飞书读取并校验真实题库，再更新缓存和取数；完整读取必需国家页，检查题号、版本和分页。失败保留上一批。真实取数前必须实际读取飞书题库，不能只检查本机配置；在线读取或校验失败就停止，不请求Answerbit。`--offline-bank`不能绕过真实取数前置核对；仅纯离线snapshot可使用本地题库，`--feishu-bank <URL>`可临时指定获批在线题库。

| 目的 | 入口 |
|---|---|
| 仅验证在线题库，无需Answerbit Key | `python3 feishu_bank.py .trial/在线题库.xlsx` |
| 仅本地国家看板 | 上方同一条 geo_run.py 命令加 `--local-only`，保留 `--market` 和同批分析 |
| 离线重绘，不更换历史题库 | dashboard.py同参数，snapshot代替refresh；`--fetch-platform`仅补读平台快照 |
| 历史推荐未知复检 | dashboard.py同参数，review代替refresh；`--defer-unavailable`仅在明确暂缓旧任务时使用 |
| 命令行指标刷新 | `python3 answerbit.py refresh <题库.xlsx> <库.sqlite> --begin YYYY-MM-DD --end YYYY-MM-DD` |
| 查询同批指标与异常 | `python3 answerbit.py query .trial/dashboard.sqlite`，可离线、无需Key |
| 本机原文查看 | `python3 local_answers.py .trial/dashboard.sqlite .trial/GEO运营看板.html`，保持进程运行后点查看原文 |

answerbit.py支持单个`--group-id`和可重复`--tag-id`，按交集计算；多分组用看板。平台分组、标签及飞书分类独立；总体按唯一任务计数。正常与异常明细须匹配已确认题号，无候选记录单列不入指标；有候选但歧义时输出.question-check.xlsx并停止发布。

## 对账与结果字段

题文仅做Unicode规范化和空白归一，不按语义近似强配题号。同一句题文对应多个国家时，须用唯一可区分的市场标签确认；采样网络地区不能当目标市场。可用同批已确认提问ID与相同题文补齐异常题号，冲突时保留候选核查。题库导出文件SHA-256作为本地版本标识；在线版本另保留修订号和内容校验值。

读取返回摘要时，官网链接用`website_links`，分组/标签拆分用`website_links_by_group`和`website_links_by_tag`；内容指标用`content`，范围拆分用`dimensions`；Answerbit分组和标签分别用`answerbit_groups`及`answerbit_tags`。若`content_status=需刷新`或缺相应字段，刷新到独立快照，保留旧批可查；不得将缺字段解释为零。

注意工具差异：dashboard.py流程会将通过校验、唯一匹配的题文和回答原文保存在本地SQLite；单独answerbit.py指标入口计算后丢弃回答原文，其库不能冒充原文查询库。新旧快照不可据此覆盖或混用。

## 发布连接检查

先按[发布权限清单](skill/geo-sampling/references/feishu-preview.md#一次列清发布权限)核对实际应用/身份及已有连接。常规发布使用私有中央 `registry.json` 和已知文件标识，不扫描飞书目录、不要求 `space:document:retrieve`。首次接入或记录缺失时，管理员先核对历史并初始化/恢复记录；不能将未知历史当空目录。随后只读检查成员及分享能力：

```sh
python3 feishu_publish.py <已有看板.html> <已有库.sqlite> --check-access
```

此命令只探测读取能力。快捷下载报错、保留权限更新及回读操作见[已有权限单次发布](skill/geo-sampling/references/feishu-preview.md#已有权限单次发布)。

初始化入口为 `python3 feishu_publish.py <计划看板.html> <计划库.sqlite> --market <获准国家> --initialize-registry <私有历史核查证明.json>`，多国重复 `--market`。首次可使用尚未生成的计划文件路径，不取真实数据；证明和旧回执导入要求见[中央记录与版本去重](skill/geo-sampling/references/feishu-preview.md#中央记录与版本去重)。已有看板时可省略国家参数，沿用看板已隔离范围。仅有旧 HTML 而无完整验收回执时不自动导入成功；记录损坏、已知链接不可达或上传结果不明均停止，要求管理员核查，不自动申请目录权限或重新取数。请备份整个私有状态目录，统一使用单一串行发布者。

## 运行结果与续跑

`--check` 只核对本地配置。缺发布配置时仍可完成已获准的本地分析；结果中的 `blockers` 指明发布缺项，有 `local.analysis_context` 时先完成同批核心发现。具体状态与下一步见[飞书交付](skill/geo-sampling/SKILL.md#飞书交付)。

严格发布候选通过真实预览和权限检查后，用 `python3 feishu_publish.py <看板.html> <库.sqlite> --accept-proof <证据.json>` 登记。手动更新指定文件按单次发布规程独立留证。发布状态按国家集合隔离；中断后须核对本地提交及远端状态，保留上一成功结果。失败时不重新取数解决发布问题。

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

真实品牌、团队、题库地址仅在获批私有环境配置；GitHub只分发空白样例和读取逻辑。其他运行环境须由管理员安全配置，不能通过公开仓库自动获得本机真实值。Team ID仅作管理定位，不额外传给当前回答接口。

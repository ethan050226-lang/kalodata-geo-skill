# GEO 采样 Skill / GEO Sampling Skill

只读分析 Answerbit 已有回答，匹配飞书题号，生成可追溯的看板与简报。运行要求 Python 3.9+、SQLite 3.30+及requirements.txt依赖。安装保留完整仓库；单独复制Skill目录不包含Python工具。

## 用户运行

Agent先确认运营国家（可多国），引导安全配置品牌级只读Key并说明预计耗时，两项齐备前不真实取数或发布。本次已明确的无需重复问，历史国家配置须确认沿用。交付数据仅含确认国家，HTML内嵌数据与附件也须裁剪；不能裁剪则停止共享。国家自报不替代访问授权。geo_run.py要求显式传入`--market`（可重复）；内部生成裁剪后的SQLite，再重算HTML、Excel和简报。单国交付不嵌入品牌全量平台快照，无法归属国家的异常另注明未纳入。

询问是否需要定时、频率、时间及接收位置。推荐并默认每周一北京时间上午11:30，汇总上一周周一至周日；用户确认后才设置。已有订阅更新原任务，不擅自改成默认时间。详见[定时推送](skill/geo-sampling/references/scheduled-delivery.md)。定时依赖运行Agent已有的持久调度、数据、发布和消息能力；缺项保存待接入，不能称已启用。本Skill允许使用官方lark-cli和仓库Python工具执行；新增权限仍须按下文报告。

## 管理员私有配置

题库连接先执行[自动检查、复用和开通指引](skill/geo-sampling/references/feishu-connection.md)。优先复用公司现有获批连接，默认应用身份；已有获批用户连接可在私有运行环境设置`GEO_FEISHU_BANK_IDENTITY=user`，不再被固定切回bot。该设置仅控制题库读取，与发布身份分开。程序不会自动登录、切换账号或申请权限；缺项由Agent按实际原因引导，一次实际全量读取通过后继续完整刷新。

原文质量筛查已接入每次真实刷新：无效和同题同模型明显缩水的回答标为“数据异常”，不进入Skill自算指标的有效样本；异常原因、编号和对照记录在看板“回答表现”和Excel“回答质量”中查看，本地原文保留。筛查阈值与限制见[指标定义](skill/geo-sampling/references/metric-definitions.md#回答原文质量检查)，平台原生快照仍按平台口径。

- Answerbit：运行者安全配置ANSWERBIT_API_KEY，或交互终端隐藏输入。品牌范围用ANSWERBIT_BRAND_ID，或私有.trial/answerbit-resources.json；可用ANSWERBIT_RESOURCE_CONFIG改路径。结构见[空白私有配置样例](examples/private-resources.example.json)，填写brand_id、team_id和question_bank_url后存入被忽略的私有目录；不要修改或提交已填值的公开样例，只传brand_id查询。Key格式通过后才读资源文件；查询成功不证明Key没有写权限，需在平台核实只读及品牌范围。
- 在线题库：先读取GEO_QUESTION_BANK_URL，未设置时在Key就绪后读取同一私有资源文件的question_bank_url；复用拥有知识库及电子表格只读权限的官方lark-cli，必要时用LARK_CLI指定路径。题库保持只读。面向核查的题库链接可用GEO_QUESTION_BANK_URL；未配置则隐藏，不提交内部地址。
- 权限审批：任何需企业管理员审批的权限，先按[权限申请前置报告](skill/geo-sampling/SKILL.md#权限申请前置报告)向用户说明具体权限、用途、资源范围、缺项与替代方式；用户决定前不发起申请、扩权或为权限生效发布应用版本。复用既有权限不等于已验证实时题库可读，仍需实际读取验收。
- 题库只读授权由用户提供：缺少访问权时，将指定飞书题库以“可阅读”权限共享给当前Agent或其已配置应用，或完成对应只读授权。Agent负责连接配置、实读验收及后续复用；文件访问权和应用读取权限分别核验，需要企业管理员审批时先向用户报告，不以文件分享成功冒充接通。品牌信息先查私有配置，Excel读取组件由Agent处理。缺项按[中文提示规则](skill/geo-sampling/SKILL.md#一次性题库接入与缺项提示)说明问题、处理人及下一步，不要求用户提供变量名、权限代码或程序库。
- 飞书交付：独立授权及私有GEO_FEISHU_CONFIG，配置指定位置与获准接收者，按[飞书交付](skill/geo-sampling/references/feishu-preview.md)验收。identity可省略或留空，默认bot，既有user配置保持不变；它只影响发布，不阻止本地生成。

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
| 仅本地看板 | `python3 dashboard.py refresh <题库.xlsx> <库.sqlite> <看板.html>` |
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

## 配置不足时的行为

`--check`只检查本机前置条件，不请求平台，也不代替完整运行或每次取数前对飞书题库的实际核对；国家和Key就绪后才读私有品牌及发布配置，输出仅显示缺项名称，不回显实际值。blockers只列真实取数前置缺项，publication_warnings单列发布提示，不能据发布提示停止本地分析。`--local-only`只生成本地结果，不调用飞书发布。

未配置飞书发布、上传失败或验收失败时，完整本地分析返回`local_ready_pending_publication`，交付已校验HTML及同批简报，不能称已上传或交付飞书链接。已验收飞书入口保持原样。缺在线题库或在线核对失败时，即使本地有Excel也停止真实取数；已有快照仍可纯离线查看。缺Key、品牌资源或有效题号仍停止真实分析，不能猜值。已核实的参数可由运行者在获批私有环境配置，无须在聊天发送。

## 交付和规范

默认交付已验收飞书链接及同批简报：核心数据表、一段结论、最多三条证据行动。Excel可本地生成，默认不上传。原文通过同批本机库查看；飞书共享版仅保留题文、题号、日期、平台和分组定位，工作台入口不是原文直达。

geo_run.py核对远端版本：reused复用已验收链接；pending_acceptance仍待实际预览和权限验收。证据绑定精确版本后，用`python3 feishu_publish.py <看板.html> <库.sqlite> --accept-proof <证据.json>`验收入账为published，才交付新链接。发布状态按国家集合隔离，避免串用他国上次入口。本地文件提交中断会回滚，未完成的提交标记阻止发布；进程意外中止后须重跑核对，不声称多文件写入具备系统级事务。失败保留上一成功结果，不发布半批；尚无固定链接切换，多人发布使用统一串行环境。

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

真实品牌、团队、题库地址仅在获批私有环境配置；GitHub只分发空白样例和读取逻辑。其他运行环境须由管理员安全配置，不能通过公开仓库自动获得本机真实值。Team ID仅作管理定位，不额外传给当前回答接口。

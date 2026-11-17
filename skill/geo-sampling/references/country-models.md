# 国家与模型配置

运行读取同目录的 [country-models.json](country-models.json)，并与本批最新飞书题库的明确模型配置核对；冲突时停止相关正式统计，不自动修改题库。国家依据唯一匹配的工作表及题号，不依据题文地名、旧区域标签或采样 IP。

| 国家 | 本轮纳入模型 | 状态 |
| --- | --- | --- |
| 英国、美国、德国、法国 | ChatGPT、Gemini、Google AIO、Perplexity、Google AI Mode | 已确认 |
| 泰国、越南、印尼、马来西亚 | ChatGPT、Gemini、Google AI Mode、Google AIO | 已确认；Claude 暂缓 |
| 中国 | 豆包、通义千问、DeepSeek、腾讯元宝、文心一言、Kimi、ChatGPT | 已确认 |
| 日本、韩国 | ChatGPT、Gemini、Google AI Mode、Google AIO | 2026-11-17 已确认完整名单，替代旧冲突名单；Claude 暂缓 |

## 读取与统计

- 题库可选使用国家页「采样模型」列，或「国家模型配置」页的「国家」「平台标识」列；未提供时使用有来源的已确认配置。不将历史回答中出现过的模型自动认定为获准模型。
- 完整国家交付入口在 Answerbit 回答读取前核对配置、映射和来源，输出 `sampling_plan`；仅核对已有回答，不创建计费采样任务，不修改模型启用、频次或地区。其他国家不自动继承配置。
- Claude 本轮不采样、不分析；保留历史名单及暂缓来源，不因其缺少标识阻断其他模型。
- Google AIO、Google AI Mode、Gemini 独立计数。AIO 的已核实标识为 `google_ai_overviews`；普通 Google、`google_aio` 不能自动代替它。新环境或服务标识变化须只读核对。
- AIO 当前未采样仍保留配置和筛选入口；没有有效回答显示「暂无数据」，比例不适用，不能填 0%。零条仅证明当前窗口无有效回答，不能直接断言平台未跑；任务启用、频次和采样覆盖仍需另行核实。
- 范围外和未知模型从国家派生库、指标、图表、题单、行动及 Excel 有效回答中排除；在「国家模型排除」保留题号、题文、平台、日期和原因。排除审计属于本批范围，不冒充页面当前筛选范围。
- 母批保留于获批本地；派生库同步清理关联侧表及删除页。共享 HTML 不含回答原文或本机查询地址，不能仅预选国家却仍内嵌全量数据。
- 配置校验值进入版本核对与历史对比；配置不同时并列说明，不计算误导性变化。定时任务保持暂停，安装更新不代表真实飞书发布或各国采样已验收。

## 平台标识

| 名称 | Answerbit `platform` |
| --- | --- |
| ChatGPT / GPT | `chatgpt` |
| Gemini | `gemini` |
| Perplexity | `perplexity` |
| Google AI Mode | `google_ai_mode` |
| Google AIO / AI Overviews | `google_ai_overviews` |
| 豆包 | `doubao` |
| 通义千问 | `qianwen` |
| DeepSeek | `deepseek` |
| 腾讯元宝 | `yuanbao` |
| 文心一言 | `yiyan` |
| Kimi | `kimi` |
| Claude | 本轮暂缓，无需补标识 |

标识须来自服务返回及已审核证据；私人证据、连接信息和账号配置不随公开配置提交。

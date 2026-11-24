# 增强版 GEO 看板

增强版以已经准备并对账的私有批次为输入，提供国家和平台筛选、同批对照、每日趋势、行动线索、点名分组、任务原文与口碑关键词定位。平台返回关键词和本地提取关键词独立呈现。历史周期仍须通过题库、范围、指标及采样完成度校验。

```sh
python3 enhanced_dashboard.py /path/to/prepared-private-batch.json /path/to/enhanced-dashboard.html
```

输入也可以是已有增强 HTML 快照。输入须含当前看板数据、`answer_details.records`、`answer_details.record_count` 和已对账的 `preview` 元数据。回答明细使用任务 ID 与题号定位，按维度与看板有效样本逐组核对。输出保留输入的原文、已有关键词来源及指标判定。这个入口只渲染已有数据，无需平台账号或凭据。

增强版 HTML 包含整批回答全文，应按原文批准范围管理。常规飞书共享版仍由既有发布流程生成。GitHub 源码只保存空的数据占位符，真实 HTML、题库、数据库和私有配置留在获准交付位置。

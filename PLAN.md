# PLAN — ClinicalTrials.gov Query-to-Visualization Agent

截止：Tue Nov 10, 2026, 3:00 PM ET
标注说明：文中写“(待确认)”的地方，是需要对照真实返回结果核实后再定的内容。
对照文档：`take-home-assignment.md`（下文用 “作业 §X” 引用对应章节）。

---

## 0. 核心设计原则（README 第一条设计决策）

LLM 只负责“理解问题 → 生成受约束的查询计划（QueryPlan）”，不碰任何数字。
取数、过滤、聚合、计数、生成图表 spec、生成 citations 全部由确定性代码完成。

好处：
- 图里每个数字都能追溯到具体 NCT 记录，citations 是聚合过程的副产品（作业 §5 bonus）
- 幻觉只可能出现在“理解错问题”这一步，而这一步有 schema 约束 + 业务规则校验 + 重试 + 降级兜底（作业 §7.2）
- 所有问题类型走同一条管线：新增问题类型 = 注册一个新维度 / 新图类型，不写 one-off 分支（作业 §4 “single coherent approach”、§7.4 “without one-off hacks”）

作业要求 → 本计划对应位置：

| 作业要求 | 对应 |
|---|---|
| §1.1 理解问题 | §11 Planner |
| §1.2 从 ClinicalTrials.gov 取数 | §9 API |
| §1.3 判断是否需要可视化、选什么图 | §5 `answer_mode` + §7 选择规则 |
| §1.4 输出 visualization spec | §8 Response |
| §3.1 文档化 request schema | §4 + `GET /schema` + README |
| §3.2 文档化 response schema，前端不用猜 | §8（按 type 的判别联合）+ README |
| §4 多种图类型，含有意义的网络图 | §7、§8 network_graph |
| §5 deep citations | §8 Citation |
| §6 code / README / 3–5 个真实 example | §13、§15 |
| §8 AI 工具使用、如何验证正确性 | §12、§15 第 10 条 |

---

## 1. Pipeline

```
Request
  │
  ▼
[1] Request 校验 (Pydantic)
  │
  ▼
[2] Planner (OpenAI structured output) ──► QueryPlan
  │        ▲
  │        └── 业务规则校验失败：带错误信息重试 1 次；仍失败 → 规则降级 plan + notes
  ▼
[3] Plan Validator（业务规则）+ 合并用户显式字段（显式字段优先）+ 范围兜底（无过滤时加默认范围）
  │
  ├── answer_mode = unsupported ──► 结构化拒答（visualization = null + reason）
  ▼
[4] Fetcher：/studies 分页、字段裁剪、上限、缓存；超上限时切换“分桶计数”策略
  │
  ▼
[5] Normalizer：phase / 日期 / 缺失值 / 药名归一化
  │
  ▼
[6] Aggregator：通用 group_by / cross_tab / co_occurrence / bin / point 引擎（同时收集 citations）
  │
  ▼
[7] Spec Builder：visualization {type, title, encoding, data} + meta
  │
  ▼
Response
```

---

## 2. 技术栈

- Python 3.11+
- FastAPI（`POST /visualize`，`GET /health`，`GET /schema` 返回 request/response JSON Schema）
- Pydantic v2（request / QueryPlan / response 全部建模，自动导出 JSON Schema 进 README）
- httpx（调用 API，带超时、重试、限流）
- LLM：OpenAI（公司提供），Structured Outputs（`json_schema` + `strict: true`），SDK 用 `client.beta.chat.completions.parse(response_format=QueryPlanLLM)`
  - 默认模型 `gpt-4.1`（非推理模型，延迟低，结构化输出稳定），`OPENAI_MODEL` 环境变量可切换
  - Planner 评估时对比 `gpt-4.1` / `gpt-4.1-mini` / `gpt-5.4-mini`，结果写进 README
  - `planner/llm_client.py` 做薄适配层：`generate_plan(messages) -> QueryPlanLLM`，换模型不动 planner 逻辑
- pytest + 录制的真实 API JSON fixture
- 简单文件缓存（按请求参数 hash），开发时省调用、跑 example 时结果可复现

---

## 3. 目录结构

```
take_home2/
  app/
    main.py              # FastAPI 入口
    config.py            # 环境变量：OPENAI_API_KEY、OPENAI_MODEL、MAX_RECORDS 等
    schemas/
      request.py         # VisualizeRequest
      plan.py            # QueryPlanLLM（strict）+ QueryPlan（内部）
      response.py        # VisualizeResponse，按 visualization.type 的判别联合
    planner/
      llm_client.py      # OpenAI 适配层（structured output、超时、重试）
      llm_planner.py     # 调 LLM 产出 QueryPlan
      prompts.py
      fallback.py        # 规则降级 plan
    validation/
      plan_rules.py      # 业务规则校验
    ctgov/
      client.py          # API 客户端（分页、重试、限流、缓存）
      query_builder.py   # QueryPlan.filters → API 参数
      count_strategy.py  # 超上限时的分桶 countTotal 计数
    data/
      normalize.py       # phase / 日期 / 药名归一化
      dimensions.py      # 维度注册表（核心扩展点）
    aggregate/
      engine.py          # group_by / cross_tab / co_occurrence / bin / point
      citations.py
    viz/
      builder.py         # 聚合结果 → visualization spec
  scripts/
    record_fixtures.py   # 拉真实 API 返回存进 tests/fixtures
    run_examples.py      # 生成 examples/
  tests/
    fixtures/            # 录制的真实 API 返回
    test_normalize.py
    test_aggregate.py
    test_plan_rules.py
    test_end_to_end.py   # mock LLM + fixture
  examples/
    01_time_series.json  # request + 真实 response
    ...
  README.md
  requirements.txt
  .env.example
```

---

## 4. Request Schema（作业 §3.1）

| 字段 | 类型 | 必填 | 校验 |
|---|---|---|---|
| `query` | string | 是 | 1–500 字符，去首尾空白 |
| `drug_name` | string \| string[] | 否 | 数组长度 ≤ 4；传数组表示对比（Drug A vs Drug B） |
| `condition` | string \| string[] | 否 | 同上 |
| `sponsor` | string | 否 | 匹配 lead sponsor（`query.lead`） |
| `country` | string | 否 | |
| `trial_phase` | enum[] | 否 | `EARLY_PHASE1, PHASE1, PHASE2, PHASE3, PHASE4, NA`（与 `/studies/enums` 一致） |
| `status` | enum[] | 否 | `RECRUITING, NOT_YET_RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED, TERMINATED, ...`（与 `/studies/enums` 一致） |
| `start_year` | int | 否 | 1990–当前年份+5（允许预计开始日期） |
| `end_year` | int | 否 | ≥ start_year |
| `viz_type` | enum | 否 | 用户强制指定图类型时覆盖 LLM 选择（仍要过业务规则） |
| `max_records` | int | 否 | 默认 10000，上限 20000 |
| `top_n` | int | 否 | 默认 15，范围 1–50，用于截断类别过多的图 |

规则：显式结构化字段 > LLM 从 query 中解析出的值。两者冲突时以显式字段为准，并写进 `meta.notes`。
校验失败返回 422 + 统一错误结构（见 §8）。

---

## 5. QueryPlan Schema（LLM 输出，内部结构）

```json
{
  "answer_mode": "visualization | single_value | unsupported",
  "unsupported_reason": null,
  "intent": "trend | distribution | comparison | geographic | network | correlation",
  "cohorts": [
    { "label": "Pembrolizumab", "filters": { "intervention": "Pembrolizumab", "condition": null, "sponsor": null } }
  ],
  "shared_filters": {
    "condition": null, "intervention": null, "sponsor": null, "country": null,
    "status": ["RECRUITING"], "phase": null, "start_year": 2015, "end_year": null
  },
  "group_by": "start_year",
  "split_by": null,
  "measure": null,
  "network": null,
  "viz_type": "time_series",
  "top_n": 15,
  "interpretation": "Count of trials per start year for Pembrolizumab since 2015"
}
```

- `answer_mode`（对应作业 §1.3 “判断是否需要可视化”）：
  - `visualization`：正常出图
  - `single_value`：问题本质是一个数（“现在有多少招募中的 X 试验？”）→ 返回 `type = "metric"` 的单值卡片，仍带 citations 和 meta
  - `unsupported`：与临床试验无关，或 API 字段无法回答（例如“这个药效果好不好”）→ `visualization = null`，`meta.notes` 给出原因和可回答的改写建议
- `measure`：数值维度（histogram 用一个，scatter 用 `{x, y}` 两个）
- **OpenAI strict 模式约束**（Pydantic 建模时就要满足）：
  - 不能有自由 dict：`filters` / `shared_filters` 是固定字段的对象（如上）
  - 所有字段都 required，可选字段写成 `X | None`，不用默认值
  - `additionalProperties: false`（Pydantic 用 `model_config = ConfigDict(extra="forbid")`）
  - LLM 输出用的 `QueryPlanLLM` 和内部 `QueryPlan` 分开：前者满足 strict，后者带默认值和派生字段
- `group_by` / `split_by` / `measure` / `network.*` 只能取维度注册表里的 enum 值
- `cohorts` 长度 > 1 即为对比查询 → grouped_bar_chart，series = cohort
- `network`：`{ "source_dim": "lead_sponsor", "target_dim": "intervention_name" }`；两者相同即同类共现网络（drug ↔ drug）

### Plan 业务规则（validation/plan_rules.py）
- `time_series` 要求 `group_by` 是 temporal 维度
- `bar_chart` / `grouped_bar_chart` 要求 categorical 维度
- `network_graph` 要求 `network` 非空，且两端都是 categorical 维度，`group_by` 为空
- `histogram` 要求 `measure` 是 numeric 维度
- `scatter_plot` 要求 `measure.x`、`measure.y` 都是 numeric 维度
- cohorts 最多 4 个
- **范围兜底**：没有任何实体过滤（drug / condition / sponsor / country）时不拉全库（约 50 万条）。自动加默认范围：`study_type = INTERVENTIONAL` + 最近 5 年开始的试验（drug ↔ drug 网络再加 `intervention_type = DRUG`），写进 `meta.notes` 和 `filters_applied`

---

## 6. 维度注册表（data/dimensions.py，整个系统的扩展点）

每个维度定义：`name`、`kind`（categorical / temporal / numeric）、`api_fields`（`fields=` 用的 piece name）、`extractor(study) -> list[Extracted(value, raw_value, field_path)]`、`multi_valued`、`normalizer`。

| 维度 | API 字段路径（已用 NCT03540771 真实 JSON 核实，除标注外） | 多值 | 类型 |
|---|---|---|---|
| `phase` | `protocolSection.designModule.phases` → 派生单值标签（见 §10） | 否 | categorical |
| `start_year` | `protocolSection.statusModule.startDateStruct.date` | 否 | temporal |
| `overall_status` | `protocolSection.statusModule.overallStatus` | 否 | categorical |
| `study_type` | `protocolSection.designModule.studyType` | 否 | categorical |
| `lead_sponsor` | `protocolSection.sponsorCollaboratorsModule.leadSponsor.name` | 否 | categorical |
| `sponsor_class` | `protocolSection.sponsorCollaboratorsModule.leadSponsor.class` | 否 | categorical |
| `intervention_type` | `protocolSection.armsInterventionsModule.interventions[].type` | 是 | categorical |
| `intervention_name` | `protocolSection.armsInterventionsModule.interventions[].name`（原始名，用于 citation） | 是 | categorical |
| `drug` | `derivedSection.interventionBrowseModule.meshes[].term`（已核实，MeSH 归一化名；network 和药物分布默认用这个） | 是 | categorical |
| `condition` | `protocolSection.conditionsModule.conditions` | 是 | categorical |
| `country` | `protocolSection.contactsLocationsModule.locations[].country` | 是 | categorical |
| `site` | `protocolSection.contactsLocationsModule.locations[].facility`（已核实） | 是 | categorical |
| `investigator` | `protocolSection.contactsLocationsModule.overallOfficials[].{name, affiliation, role}`（已核实） | 是 | categorical |
| `enrollment` | `protocolSection.designModule.enrollmentInfo.count` | 否 | numeric |
| `duration_months` | 派生：`completionDateStruct.date − startDateStruct.date` | 否 | numeric |
| `site_count` | 派生：`len(locations)` | 否 | numeric |

补充：
- `startDateStruct` / `completionDateStruct` 都有 `type`（`ACTUAL | ESTIMATED`），保留下来供 notes 和 time_series 标注用
- `locations[].status`（站点招募状态）已核实（NCT06927648：同一试验内站点有 RECRUITING / NOT_YET_RECRUITING 混合），piece name `LocationStatus`
- 网络图里 `site`、`investigator` 两个维度对应作业 §4 提到的 “investigators, sites”
- 真实数据里的坑（2026-11-09 实测）：
  - 药企试验的站点名常是匿名的 “Research Site” → `site` 维度过滤掉这类占位名，notes 说明
  - 药企试验的 overallOfficials 常是 “Medical Director”、“Pfizer CT.gov Call Center” 这类占位 → `investigator` 维度只取 `role = PRINCIPAL_INVESTIGATOR / STUDY_CHAIR` 且名字不在占位黑名单里的
  - 干预 MeSH 词把 “MK3475”、“Pembrolizumab KEYTRUDA®” 都归成 `pembrolizumab`，placebo 和 best supportive care 不会出现；但会混进非药物词（Radiotherapy、Specimen Handling 等），有的试验 MeSH 为空

多值维度的计数口径：每个 trial 对每个不同的值只算 1 次（去重），所以各桶之和可能大于 trial 总数；在 `meta.units` 和 `meta.notes` 里说明。`phase` 是单值，各桶之和 = trial 总数。

---

## 7. 问题类型 → 引擎操作 → 图类型（覆盖作业 Appendix 全部示例）

| 问题类型 | 示例（Appendix 原句或同类） | 引擎操作 | viz type |
|---|---|---|---|
| 时间趋势 | “How has the number of trials for [drug] changed per year since 2015?” | group_by(start_year)，补齐空年份 | `time_series` |
| 时间趋势 | “How many trials started each year for [condition]?” | 同上 | `time_series` |
| 分布 | “How are [condition] trials distributed across phases?” | group_by(phase) | `bar_chart` |
| 分布 | “What are the most common intervention types for [drug/condition] trials?” | group_by(intervention_type) + top_n | `bar_chart` |
| 分布（数值） | 入组人数分布 | bin(enrollment) | `histogram` |
| 对比 | “Compare phases for trials involving Drug A vs Drug B.” | 每个 cohort 各自 group_by(phase)，合并为 series | `grouped_bar_chart` |
| 对比 | “Compare sponsor categories across two conditions.” | cohort = 两个 condition，group_by(sponsor_class) | `grouped_bar_chart` |
| 地理 | “Which countries have the most recruiting trials for [condition]?” | group_by(country) + 招募状态过滤 + top_n | `bar_chart`（`meta.geo_hint = "country"`） |
| 网络 | “Show a network of sponsors ↔ drugs for [condition] trials.” | co_occurrence(lead_sponsor, drug) | `network_graph` |
| 网络（同类） | “Which drugs frequently co-occur in combination studies?” | co_occurrence(drug, drug)，只算 ≥2 个药物的 trial | `network_graph` |
| 网络（扩展） | condition ↔ drug、sponsor ↔ site、investigator ↔ sponsor | 同一个 co_occurrence，换维度 | `network_graph` |
| 相关 | enrollment vs 持续时间 | point(enrollment, duration_months) | `scatter_plot` |
| 单值 | “How many recruiting trials are there for X?” | countTotal | `metric` |

选图规则（写进 prompt，也写进 README）：时间维度 → time_series；两个实体之间的关系 → network_graph；多个 cohort → grouped_bar_chart；单个 numeric → histogram；两个 numeric → scatter_plot；问一个数 → metric；其他 categorical → bar_chart。

---

## 8. Response Schema（作业 §3.2，Pydantic 判别联合，`visualization.type` 为判别字段）

```json
{
  "visualization": {
    "type": "bar_chart | grouped_bar_chart | time_series | histogram | scatter_plot | network_graph | metric",
    "title": "string",
    "encoding": { },
    "data": [ ]
  },
  "meta": {
    "answer_mode": "visualization | single_value | unsupported",
    "query_interpretation": "string",
    "filters_applied": { },
    "plan": { },
    "units": { "y": "trials" },
    "sort": { "field": "trial_count", "order": "desc" },
    "time_granularity": "year",
    "grouping": { "group_by": "phase", "split_by": null, "multi_valued": false },
    "record_counts": { "total_matching": 1234, "fetched": 1234, "truncated": false, "count_method": "full_scan | bucket_count" },
    "notes": [ "string" ],
    "source": { "name": "clinicaltrials.gov", "api": "https://clinicaltrials.gov/api/v2", "api_version": "2.0.5", "data_timestamp": "ISO-8601", "retrieved_at": "ISO-8601" }
  }
}
```

`unsupported` 时 `visualization = null`。错误统一结构：`{ "error": { "code": "VALIDATION_ERROR | UPSTREAM_ERROR | PLANNER_ERROR", "message": "...", "details": [...] } }`。

### 各图类型的 encoding + data 约定

**bar_chart**
```json
"encoding": { "x": {"field": "category", "type": "nominal", "label": "Phase"}, "y": {"field": "trial_count", "type": "quantitative", "label": "Trials"} },
"data": [ { "category": "Phase 2", "trial_count": 78, "citation_count": 78, "citations": [ ] } ]
```
数据行用固定 key（`category`、`series`、`trial_count`、`year`、`bin_start` …），每种图的 data 结构是静态的，前端不用按维度猜 key；维度名称和轴标题放在 `encoding.*.label`。

**grouped_bar_chart**：encoding 多一个 `"series": {"field": "cohort", "type": "nominal"}`，每行 `{ "phase", "cohort", "trial_count", "citation_count", "citations" }`。

**time_series**：`x = {"field": "year", "type": "temporal"}`，按年升序，空年份补 0；每行额外带 `estimated_count`（开始日期是 ESTIMATED 的 trial 数），前端可以把未来年份画成虚线。

**histogram**：`encoding = { "x": {"field": "bin", "bin_start": "bin_start", "bin_end": "bin_end", "label": "Enrollment"}, "y": {"field": "count"} }`；每行 `{ "bin_start", "bin_end", "count", "citation_count", "citations" }`，左闭右开；`meta.binning` 写分箱方法（默认对数分箱，enrollment 长尾）。

**scatter_plot**：`encoding = { "x": {"field": "x", "label": "Enrollment"}, "y": {"field": "y", "label": "Duration (months)"} }`；每行一个 trial `{ "nct_id", "x", "y", "label", "citations" }`；缺任一值的 trial 不画，数量写进 notes。

**metric**：`encoding = { "value": {"field": "value"} }`，`data = [ { "value": 2318, "label": "Recruiting lung cancer trials", "citations": [ ] } ]`。

**network_graph**
```json
"encoding": {
  "nodes": { "id": "id", "label": "label", "group": "type", "size": "trial_count" },
  "edges": { "source": "source", "target": "target", "weight": "weight", "directed": false }
},
"data": {
  "nodes": [ { "id": "lead_sponsor:merck sharp & dohme llc", "label": "Merck Sharp & Dohme LLC", "type": "lead_sponsor", "trial_count": 40, "degree": 12, "citations": [ ] } ],
  "edges": [ { "source": "lead_sponsor:merck sharp & dohme llc", "target": "intervention_name:pembrolizumab", "weight": 35, "citation_count": 35, "citations": [ ] } ]
}
```
- 边权 = 同时出现两端实体的 trial 数（去重）
- 规模控制：先过滤边权 ≥ `min_weight`（默认 2），再按加权度数保留 top N 个节点（默认 30）；被剪掉的节点数和边数写进 notes
- 节点 id 用 `维度:归一化值`，保证不同维度同名不冲突

### Citation 结构（作业 §5）
```json
{
  "nct_id": "NCT01234567",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"PHASE2\", \"PHASE3\"]",
  "title": "<briefTitle 原文>",
  "url": "https://clinicaltrials.gov/study/NCT01234567"
}
```
- `excerpt` 是 API 返回的该字段原始值（归一化之前），原样序列化，直接支撑这个 datum（作业要求 “exact text excerpt … or a specific field/value”）
- `title` 只作上下文展示，不作为证据
- 网络边的 citation 带两条 excerpt（两端实体各一条）
- 每个 datum 最多带 5 条 citation，另给 `citation_count` 表示总数

---

## 9. ClinicalTrials.gov API 使用（已对照 OpenAPI spec `https://clinicaltrials.gov/api/oas/v2` 和真实请求核实，API v2.0.5）

数据源：只用官方 Data API v2（作业 §2：https://clinicaltrials.gov/data-api/api），base URL `https://clinicaltrials.gov/api/v2`，不需要 key。

- 主 endpoint：`GET /studies`；辅助：`/studies/enums`（测试里校验本地 enum 与 API 一致）、`/version`（`apiVersion`、`dataTimestamp` 写进 `meta.source`）
- 检索：`query.cond`、`query.intr`（药物用 `AREA[InterventionName]X OR AREA[InterventionMeshTerm]X`，避免概念扩展误召回，见 §10）、`query.lead`（只匹配 lead sponsor）、`query.spons`（sponsor + collaborator）、`query.locn`、`query.term`
- 过滤：
  - `filter.overallStatus=RECRUITING,COMPLETED`（逗号或竖线分隔）
  - `filter.advanced` Essie 语法，已实测：`AREA[Phase]PHASE2`、`AREA[StartDate]RANGE[2015-01-01,MAX]`、`AND` / `OR` / 括号
- 字段裁剪：`fields=NCTId|BriefTitle|Phase|StartDate|...`（piece name，返回仍是嵌套 JSON），只取当前 plan 用到的维度
- 分页：`pageSize` 最大 1000（超出会被压到 1000），默认 10；`nextPageToken` → `pageToken`，后续页必须带相同的其他参数；`countTotal=true` 只在第一页返回 `totalCount`
- 排序：默认不排序 → 截断时拿到的是“任意子集”，不是“最新 N 条”
- **没有聚合 / facet 能力**：`/studies` 不返回分组计数；`/stats/field/values` 只统计全库，不能加过滤条件 → 所有分组计数都在客户端算
- 准确计数策略（`meta.record_counts.count_method`）：
  - `full_scan`（默认）：先 `countTotal`，总数 ≤ `max_records` 就全量翻页拉取（每页 1000 条）
  - `bucket_count`：总数超上限且维度是低基数的（phase / status / year / study_type / sponsor_class），每个桶单独发 `countTotal=true&pageSize=5` 请求拿精确数（pageSize=0 会被当成默认 10），这 5 条正好用作 citations
  - 高基数维度（country / sponsor / drug / network）超上限时只用前 `max_records` 条，`truncated = true`，notes 写明“排名基于样本”
- 健壮性：超时、429/5xx 指数退避重试（最多 3 次）；速率限制文档没写明，但实测约 10 个请求 / 5 秒就会返回 429 → 客户端串行请求 + 全局限速（约 1 req/s）+ 遵守 `Retry-After`。这也意味着 bucket_count 每个桶一个请求很贵，只用在桶数 ≤ 15 的维度上，缓存必须开

开工第一步：`scripts/record_fixtures.py` 拉 3–4 个真实请求存进 `tests/fixtures/`（含一个招募中、多站点的试验，确认 `locations[].status` 和 `overallOfficials`）。

---

## 10. 真实数据处理（normalize.py，作业 §7.1 “sensible handling of real-world API data”）

- **Phase**（单值派生标签，各桶之和 = trial 总数）：
  - `["PHASE1"]` → “Phase 1”；`["EARLY_PHASE1"]` → “Early Phase 1”
  - `["PHASE1","PHASE2"]` → “Phase 1/2”；`["PHASE2","PHASE3"]` → “Phase 2/3”
  - `["NA"]` → “Not Applicable”（多为器械 / 行为干预）；缺失（多为观察性研究）→ “Unknown”
  - 输出顺序固定：Early Phase 1 → Phase 1 → Phase 1/2 → Phase 2 → Phase 2/3 → Phase 3 → Phase 4 → Not Applicable → Unknown
  - 用户按 phase 过滤时，“Phase 2” 也匹配 Phase 1/2 和 2/3 的试验（API 层面就是这样），写进 notes
- **日期**：兼容 `YYYY`、`YYYY-MM`、`YYYY-MM-DD`；无法解析 → Unknown 桶，不丢弃；保留 ACTUAL / ESTIMATED
- **药名**：默认用 `drug` 维度（干预 MeSH 词），同义名自动合并（已实测 MK3475 / Keytruda → pembrolizumab）。在此基础上：
  - 只保留 `interventions[].type ∈ {DRUG, BIOLOGICAL}` 的试验对应的 MeSH 词，再用小黑名单去掉非药物词（Radiotherapy、Specimen Handling、Drug Therapy 等）
  - MeSH 为空的试验退回 `interventions[].name`：小写、去括号内容和剂量、过滤 placebo / standard of care
  - citation 的 excerpt 同时给 MeSH 词和原始干预名，两者都是 API 原文
- **国家**：用 API 的 country 字段；同一 trial 多站点同国家去重。“招募中”问题优先用站点级 `locations[].status = RECRUITING` 的国家；字段不存在时退回试验级 `overallStatus`，notes 写明口径（实测：招募中试验 100% 有站点状态，已结束的试验大多没有，所以这个退回逻辑只在少数情况触发）
- **字段覆盖率实测**（每个 fixture 前 100 条）：phase 为空 6–24%（多为观察性研究）；干预 MeSH 在按药查询时约 90%，按疾病查询时只有 50–67% → 原始药名退回逻辑是必需的，不是可选的；overallOfficials 60–78%
- **Sponsor**：保留原名，不做模糊合并（写进 limitations）
- **默认药物检索会误召回**（实测）：`query.intr=pembrolizumab` 会做概念扩展，抽查的 5 条“干预里没有该药”的试验，完整记录里任何字段都没出现 pembrolizumab / Keytruda / MK-3475（例如 NCT03719105 是淋巴瘤化疗方案）。2015 年至今的计数对比：
  - 默认 `query.intr=pembrolizumab`：2938
  - `AREA[InterventionName]pembrolizumab`：2532（已自带同义名匹配，手动加 MK-3475 / Keytruda 结果不变）
  - `AREA[InterventionMeshTerm]pembrolizumab`：2526
  - **两者 OR：2599 ← 采用**
  - 用严格写法重录 fixture 后，100/100 条都真的含该药；其中 96% 能被 `drug` 维度识别出来，剩下 4% 是 MeSH 漏标了组合方案名里的药（如 “CBA-1535+Pembrolizumab”）。这只影响网络图和药物排名里的边；对比查询的 cohort 计数由 API 检索决定，不受影响
  - 结论：药物过滤一律用 `query.intr=AREA[InterventionName]X OR AREA[InterventionMeshTerm]X`，默认写法约 12% 的结果是误召回。README 的 design decisions 写这一条（含上面的对比数字）
  - 疾病检索 `query.cond` 的扩展（如 breast cancer ↔ breast neoplasms）是想要的行为，保持默认，但 README 里说明口径
- **对比查询的重叠**：同时用 Drug A 和 Drug B 的 trial 在两个 cohort 里各算一次，重叠数写进 notes
- **缺失值**：一律进 “Unknown”，在 data 里保留，前端可以选择隐藏

---

## 11. LLM Planner 设计（作业 §7.2）

- 用 OpenAI Structured Outputs（strict）强制输出 `QueryPlanLLM`，所有维度和图类型都是 enum。strict 模式保证 JSON 结构合法，所以重试主要针对业务规则校验失败（§5 的规则），以及 `refusal` 和超时
- `temperature=0`，保证同一 query 的 plan 可复现（gpt-5 系列推理模型不支持，改用 `reasoning_effort="low"`）
- LLM 调用结果按 (model, prompt hash) 缓存，跑 example 和评估时省额度
- System prompt 里给出：维度列表及含义、§7 的选图规则、`answer_mode` 判断规则、7–8 个 few-shot（覆盖 Appendix 每一类 + 一个 single_value + 一个 unsupported）
- LLM 只填过滤条件里的“实体字符串”（如药名）和结构选择，不输出任何数字结果
- 校验失败 → 把错误信息回传重试 1 次 → 还失败就用规则降级（关键词匹配出 intent，默认 bar_chart by phase），`meta.notes` 标明降级
- 实体存在性检查：取数结果为 0 时返回空 data + notes 建议（例如检查拼写），不编造
- 用户传了 `viz_type` 时跳过 LLM 的选图，但仍过业务规则；冲突时返回 422 并说明原因
- `meta.plan` 原样返回最终 plan，前端和评审都能看到“系统是怎么理解这个问题的”

---

## 12. 测试与验证（README 的 “how you validated correctness”，作业 §8）

- 单元测试：normalize 的边界情况（phase 组合、日期格式、药名清洗）、aggregator 计数（用小 fixture 手算预期）、多值去重口径、plan 规则、网络剪枝
- 端到端：mock LLM 返回固定 plan + fixture 数据，断言 response 能通过 Pydantic response schema 校验
- 交叉核对：挑 2 个 example，把总数和分桶数跟 ClinicalTrials.gov 网站上的搜索结果对比，写进 README
- 一致性检查：phase 分布各桶之和 = `total_matching`；`bucket_count` 和 `full_scan` 在小查询上结果一致（测试里断言）
- Planner 评估：准备 12–15 条问题（含刁钻写法、single_value、unsupported），记录 plan 是否正确，三个模型的对比结果表放 README

---

## 13. Example Runs（作业 §6.3 要 3–5 个，计划做 5 个，覆盖 5 种不同的图）

1. 时间趋势：“How has the number of trials for Pembrolizumab changed per year since 2015?” → time_series
2. 分布：“How are Alzheimer's disease trials distributed across phases?” → bar_chart
3. 对比：“Compare phases for trials involving Pembrolizumab vs Nivolumab.” → grouped_bar_chart
4. 地理：“Which countries have the most recruiting trials for breast cancer?” → bar_chart (top 15)
5. 网络：“Show a network of sponsors ↔ drugs for melanoma trials.” → network_graph

有时间再加（不计入 5 个）：drug ↔ drug 联合用药网络、enrollment histogram、一个 unsupported 示例。

每个存成 `examples/0X_*.json`（request + 完整 response），由 `scripts/run_examples.py` 调真实服务生成，保证是真实输出。

---

## 14. 时间表（今天 Mon Nov 9 下午开始）

| 时段 | 任务 | 完成标准 |
|---|---|---|
| Mon 晚 第 1 小时 | 拉 fixture、核对剩余 (待确认) 字段；用公司 key 跑通一次 OpenAI structured output | 第 6、9 节待确认项全部确认，LLM 调用通 |
| Mon 晚 2–3 小时 | schemas（request / plan / response）+ dimensions + normalize | 单元测试通过 |
| Mon 晚 2 小时 | ctgov client（分页、缓存、重试、限流）+ aggregator（group_by / cross_tab）+ citations | 手写 plan 能产出带 citation 的 bar / time_series / grouped bar |
| Mon 深夜 | 睡觉（至少 6 小时） | |
| Tue 早 2 小时 | LLM planner + 校验 + 重试 + 降级 + answer_mode | 5 个 example query 都能产出正确 plan |
| Tue 上午 1.5 小时 | network（co_occurrence + 剪枝）+ bucket_count 策略 | network example 跑通 |
| Tue 上午 1 小时 | FastAPI 接口 + 端到端测试 + run_examples；README 同时开写 | examples/ 生成完毕 |
| Tue 中午 1.5 小时 | README 收尾 | 覆盖第 15 节所有项 |
| Tue 13:30–14:15 | 从干净环境按 README 重装跑一遍、打 zip | |
| Tue 14:15 前 | 提交（预留 45 分钟缓冲） | |

---

## 15. README 大纲（作业 §6.2 + §8）

1. Overview（一段话 + pipeline 图）
2. Quick start（install / `.env`（`OPENAI_API_KEY`、`OPENAI_MODEL`）/ start / curl 示例）
3. Request schema（表格 + 校验规则 + `GET /schema`）
4. Response schema（通用结构 + 每种图的 encoding/data 约定 + citation 结构 + 错误结构），写到前端照着就能实现 renderer
5. Supported query types & viz types（第 7 节的表 + 选图规则）
6. Key design decisions & tradeoffs
   - LLM 只做计划、不碰数字
   - 维度注册表作为单一扩展点
   - 判断是否需要可视化（answer_mode）
   - 多值维度的计数口径、phase 单值化
   - 全量拉取 vs 分桶计数（准确性 vs 延迟），API 没有聚合能力
   - citations 用原始字段值作为 excerpt，每个 datum 截断 5 条
   - 无过滤时的默认范围兜底
7. Data handling（第 10 节）
8. Testing & validation（第 12 节，含网站交叉核对结果和 planner 评估表）
9. Limitations & future work
   - sponsor 实体消歧（同一公司多种写法不合并）；药名已用 MeSH 合并，进一步可接 RxNorm
   - `drug` 维度的已知漏识别（实测样本约 2–4%，只影响网络图和药物排名，不影响 cohort 计数）：
     - （已解决）干预类型登记成 DEVICE / OTHER 的药物方案（如 NCT02704156 “Cyberknife plus Pembrolizumab and Trametinib”）：拆开名字后作为“待定值”，只有能对上同一批数据里由药物类干预确认过的药名才保留（`aggregate.engine.extract_batch`），器械和手术名不会混进来。README 的 design decisions 可以写这一条
     - MeSH 有值但不全时，不再看原始名（如 NCT04507906 “Nivolumab Combined with Anlotinib” 只标了 anlotinib）。把原始名和 MeSH 合并会让同义名重新变成多个节点，所以没做；改进方向是用 MeSH 同义词表把原始名映射到 MeSH 再合并
   - 超大结果集：用官方全量数据下载离线建索引，替代逐页拉取
   - 高基数维度超上限时排名基于样本
   - 地图类可视化
   - 多轮对话 / 追问澄清
10. AI tools usage（用了哪些工具、哪些部分自己设计、哪些是生成后修改的、怎么验证的）

---

## 16. 砍需求顺序（时间不够时从上往下砍）

1. scatter_plot（连同 `duration_months`、`site_count`）
2. histogram
3. 前端 demo（作业里本来就是 optional）
4. `site` / `investigator` 维度
5. Planner 评估缩到 5 条、只测默认模型
6. bucket_count 策略（退化为截断 + notes 说明）
7. drug ↔ drug 同类网络（保留 sponsor ↔ drug）

不能砍：LLM 校验与降级、answer_mode、network_graph（评分明确加分）、citations（bonus，但和架构天然契合，成本低）、README 的 schema 文档、真实 example 输出。

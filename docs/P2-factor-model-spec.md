# P2 规格（Review Draft）：多因子研究、影子验证与个股决策

> 状态：待 DeepSeek 反向审查；未经晋级不得影响持仓操作
>
> 前置：P1 的投资策略配置、市场环境、组合风险、快照版本已经上线并稳定
>
> 决策风险：只读研究，不自动下单
>
> 目标：建立一套能证明、能复盘、能降级的个股信号体系，而不是给现有建议堆叠更多看似专业的指标。

---

## 一、问题与核心原则

当前 `/analysis` 已使用真实持仓、实时价格、技术指标、组合集中度和一致性校验，但仍存在局限：

- 技术面权重较高，缺少估值、质量、低波动和预期变化。
- 候选池评分是人工规则，没有经过横截面IC和样本外检验。
- DeepSeek仍可产生原始评级和价格目标，存在随机性与虚假精度风险。
- 不同因子周期不一致，不能简单相加后直接映射买入或卖出。

本阶段遵守以下硬原则：

1. **先研究，后晋级**：未经历史验证和影子运行的模型只能展示，不能改变操作建议。
2. **因子模型不等于Alpha**：只有通过样本外超额收益、成本和稳定性检验后，才可以标记为 `validated_alpha`。
3. **LLM没有裁决权**：确定性引擎决定信号、目标仓位和最终评级；DeepSeek只解释事实、提出反例并生成中文摘要。
4. **缺失数据不放大剩余因子**：使用中性填充、覆盖率和置信度惩罚，不把缺失权重重新分配给短周期噪声。
5. **时间可得性优先**：财报、分析师预期和行业数据必须按当时已发布日期使用，避免未来数据泄漏。
6. **市场分开标准化**：美股与港股分别构建可比股票池，必要时进行行业中性化，不能把不同制度和估值分布直接混排。
7. **20%目标不进入个股得分**：组合目标只用于事后目标进度，不用于抬高候选分数或目标价。

### 非目标

- 不根据单日资金流决定买卖。
- 不把分析师目标价直接当作系统目标价。
- 不在P2阶段使用黑箱机器学习预测价格。
- 不自动下单，不因模型得分高而突破P1风险约束。

---

## 二、模型生命周期与裁决架构

### 2.1 生命周期

每个模型版本必须处于以下状态之一：

```text
research -> shadow -> advisory -> retired
```

- `research`：离线研究，只输出因子结果和验证报告。
- `shadow`：在线使用真实数据生成影子评级，不影响用户看到的操作。
- `advisory`：通过晋级门槛后，可参与确定性决策。
- `retired`：数据漂移、表现恶化或接口变化后停用。

状态由版本化注册表控制：

```json
{
  "model_version": "multifactor-v1.0.0",
  "status": "shadow",
  "promoted_at": null,
  "validation_report_id": "factor-validation-v1",
  "allowed_markets": ["US", "HK"],
  "decision_weight": 0,
  "rollback_reason": null
}
```

### 2.2 最终裁决顺序

```text
Longbridge 数据快照
  -> 因子特征与数据质量
  -> 已晋级信号模型
  -> P1 市场与组合风险约束
  -> 目标仓位区间与最终评级
  -> DeepSeek 中文解释
  -> 确定性文字/价格/方向一致性校验
  -> 决策日志与5/20/60日结果
```

DeepSeek输出不得包含独立最终评级。若保留 `llm_suggested_rating` 供研究，只能写入审计字段，永远不能覆盖 `final_rating`。

---

## 三、研究股票池与数据快照

### 3.1 股票池

- US：优先使用 `.SPX.US` 成分股，按成交额、上市时间和数据覆盖筛选。
- HK：优先使用 HSI.HK / HSTECH 可用成分股，按成交额和数据覆盖筛选。
- 现有固定候选池继续作为接口失败时的“研究白名单”，但不能用于证明横截面Alpha。
- 当前持仓即使不在研究池中也计算可用因子，但 `universe_rank` 标记为不可比。
- 排除停牌、成交极低、历史不足252日、异常复权和缺少关键标识的股票。

### 3.2 时间点快照

每日保存：

```json
{
  "snapshot_date": "2026-07-31",
  "symbol": "NVDA.US",
  "market": "US",
  "price_as_of": "2026-07-31T20:00:00Z",
  "fundamental_period": "2026Q1",
  "fundamental_published_at": "...",
  "consensus_as_of": "...",
  "factor_raw": {},
  "factor_normalized": {},
  "data_quality": {},
  "source": "longbridge"
}
```

要求：

- 只能使用 `published_at <= snapshot_date` 的财报和预期数据。
- 保存原始字段，不只保存最终分数，以便接口定义变化后重算。
- 日线统一复权方式；模型版本记录 `adjustment=forward` 或其他明确口径。
- 财报、估值、预期和价格分别记录 `as_of`，不能用一个总时间掩盖数据陈旧。
- FC临时文件不能作为长期存储；正式环境使用OSS、TableStore或数据库。

---

## 四、候选因子模型 v1

P2第一版使用五个中低频核心因子；资金流降为辅助覆盖，不作为核心15%权重。

| 因子 | 初始研究权重 | 建议持有周期 | 原始特征 |
|---|---:|---|---|
| 动量/趋势 | 25% | 1—3个月 | 20/60/120日收益、MA50/MA200、距52周高点 |
| 价值 | 20% | 6—18个月 | 行业PE/PB/PS排名、FCF收益率；负PE单独处理 |
| 质量 | 25% | 6—24个月 | ROE、毛利率/净利率稳定性、FCF、资产负债质量 |
| 低波动/回撤 | 15% | 1—6个月 | 60/252日波动率、下行波动、252日最大回撤 |
| 预期修正 | 15% | 1—6个月 | EPS/收入一致预期变化、评级变化、目标价变化与分歧度 |

这些是**待验证的研究权重**，不是上线权重。晋级前必须比较等权、IC权重和稳定性约束权重。

### 4.1 动量/趋势

- 计算20/60/120日总收益，避免只用一个窗口。
- MA关系只作为趋势状态，不重复当成三个独立高权重因子。
- 处理财报跳空：保留原始动量，同时输出剔除最近5日的中期动量供研究。
- 横截面极值按1%/99% winsorize；样本太小时使用5%/95%。

### 4.2 价值

- Longbridge `valuation-rank` 是每日行业 `rank/total`，不是股票自身五年PE分位；字段命名必须为 `industry_rank_percentile`。
- 股票自身历史估值分位只有在获得实际PE/PB历史值后才能计算，不能由行业排名冒充。
- 负PE不能自动视为极贵或极便宜；PE缺失时使用PB、PS、FCF收益率等可用指标并降低覆盖率。
- 至少分别对US/HK标准化；行业样本足够时做行业中性排名。

### 4.3 质量

不同指标必须分别定义，不能用“ROE>15% × 四项”：

- 盈利能力：TTM ROE/ROIC及三年稳定性。
- 利润质量：经营现金流/净利润、自由现金流为正的持续性。
- 经营质量：毛利率或净利率趋势及波动。
- 资产负债：净负债、利息覆盖或适合该行业的风险指标。

行业适配：

- 银行/保险不使用普通企业资产负债率评分，改用PB、ROE、资本与资产质量可得指标。
- 能源、原材料和航运不机械反转PE；使用中周期利润、FCF和资产负债情况，并标注周期敏感。
- 地产使用净资产折价、现金流和杠杆指标。
- 数据不足时标记 `sector_adapter_status=partial`，不得伪造统一可比性。

### 4.4 低波动/回撤

- 使用60日和252日年化波动率、下行波动率与最大回撤。
- 低波动因子需与杠杆ETF、上市时间和停牌数据结合，避免把缺少交易误判为低风险。
- 该因子用于风险调整，不意味着低波动股票必然上涨。

### 4.5 预期修正

- 优先使用EPS/收入一致预期的变化率、上调/下调方向和分析师分歧度。
- 目标价上涨空间只能作为一项输入，必须同时记录目标价更新时间和覆盖机构数。
- `quote` 不提供普通股票IV；期权IV必须来自期权报价，且没有活跃期权时不可用。
- 只使用“当前目标价高于现价20%”会受到价格下跌和预期陈旧影响，禁止直接打满分。

### 4.6 资金流辅助项

- `capital` 默认快照提供大/中/小单流入流出，`--flow` 提供当日分钟净流入。
- 在没有连续日频持久化前，资金流只展示 `flow_overlay`，不得进入核心因子分。
- 连续保存至少60个交易日并完成IC/成本验证后，才允许申请成为正式因子。
- 即使晋级，单日资金流对综合信号的绝对修正不得超过预设上限，并接受成交额归一化。

---

## 五、标准化、缺失数据与综合分

### 5.1 标准化

在每个调仓日、每个市场内：

1. 清洗无效值与异常复权。
2. 对原始因子winsorize。
3. 转为横截面Z-score或百分位。
4. 行业样本充足时做行业中性化。
5. 统一方向：值越大代表预期相对表现越强。

输出同时保留 `raw_value`、`z_score`、`percentile`、`universe_size` 和 `sector_sample_size`。

### 5.2 缺失规则

- 缺失子指标：使用该市场/行业中位数作为中性值，记录缺失。
- 缺失整个因子：因子贡献为0，不把权重分给其他因子。
- `data_coverage = Σ available_factor_weight`。
- `data_coverage < 0.70`：最高只能 `neutral`，不能输出正向晋级信号。
- 价值或质量均缺失：即使技术面强，也不能输出高置信度中长期加仓信号。
- 数据陈旧超过各自SLA时视为部分缺失。

### 5.3 综合信号

```text
raw_composite_z = Σ(research_weight_i × factor_z_i)
signal_percentile = percentile(raw_composite_z within market universe)
confidence
  = data_coverage
  × model_validation_quality
  × freshness_score
  × universe_comparability
```

模型输出是相对信号，不直接等同于 Buy/Sell：

```json
{
  "signal": "positive | neutral | negative",
  "signal_percentile": 82,
  "confidence": 0.74,
  "horizon": "1-3m",
  "data_coverage": 0.85
}
```

阈值由样本外验证确定并版本化，禁止在 spec 中先拍脑袋固定 `8=Buy`。

---

## 六、因子验证与晋级门槛

### 6.1 研究设计

- US与HK分别验证，不合并收益分布。
- 使用滚动月度或双周调仓；测试1M、3M、6M前瞻收益和因子衰减。
- 使用Spearman Rank IC；因子值使用当时可得数据。
- 构建五分位或十分位组合，检查收益是否大致单调。
- 同时报告等权、多空和只做多Top组相对基准表现。
- 计入佣金、平台费、印花税（适用市场）、买卖价差和最小交易单位造成的偏差。
- 报告换手率、容量与成交额约束。

### 6.2 样本外与walk-forward

- 时间顺序切分，不允许随机打乱。
- 每个窗口只用过去数据选择权重和阈值，再在下一窗口验证。
- 至少报告：IS/OOS IC、IR、Sharpe、Calmar、最大回撤、换手率、净超额收益和退化比例。
- 参数搜索结果必须展示热力图或邻域稳定性；只有单一点表现好视为过拟合。

### 6.3 从 research 晋级 shadow

必须同时满足：

- 数据契约和时间点测试通过，无已知未来数据泄漏。
- OOS平均IC为正，且不是由单一阶段或少数股票贡献。
- Top组相对基准的净超额收益为正，计入保守交易成本后仍成立。
- 分层收益具有合理单调性，或清楚说明非线性并锁定规则。
- OOS表现没有相对IS发生不可解释的严重退化。
- 最大回撤和换手率在策略配置允许范围内。

不在规格中预设“IC>0.03必过”等唯一门槛；验证报告必须同时给置信区间、样本数和经济显著性，由版本审批记录决定。

### 6.4 从 shadow 晋级 advisory

- 在线影子运行至少60个交易日，并积累至少30个独立可评估信号；取更晚者。
- 5/20/60日方向命中率、相对基准收益、建议翻转率和置信度校准没有显著偏离离线结果。
- 因子输入的缺失率、延迟和接口错误率满足SLA。
- DeepSeek叙述与确定性结论冲突率低于设定阈值，且所有冲突均被校验器阻断。
- 通过人工抽查，不存在周期股、负PE、复权、停牌和跨市场代码映射错误。

### 6.5 自动降级与退役

以下任一条件触发 `advisory -> shadow`：

- 连续评估窗口IC转负或净超额收益显著恶化。
- 数据缺失率、延迟或schema变化超过SLA。
- OOS回撤超过验证报告容许范围。
- 建议翻转率异常上升。
- 实现版本与验证版本不一致。

---

## 七、从因子信号到持仓操作

### 7.1 先得到目标仓位，不直接映射评级

因子模型先提供信号目标，再由P1风险层审批：

```text
signal_target_weight
  = policy_base_weight
  × signal_strength
  × confidence
  × market_risk_multiplier

desired_weight
  = min(
      signal_target_weight,
      single_position_cap,
      same_company_remaining_cap,
      sector_remaining_cap,
      portfolio_risk_budget_cap
    )
```

若用户未配置 `policy_base_weight` 或目标区间，只输出研究信号，不推导具体仓位。

### 7.2 持仓评级

评级由当前权重相对 `desired_weight` 与容忍带确定：

- `Overweight`：正向信号有效，当前权重低于目标下沿，且所有风险约束允许加仓。
- `Hold`：当前权重位于容忍带，信号置信度不足，或模型仍处于shadow。
- `Underweight`：当前权重高于目标上沿，或负向信号达到已验证阈值。
- `Sell`：只在投资逻辑失效、风险硬约束或已验证强负向信号同时满足明确退出规则时使用。
- `Buy`：主要用于未持有候选；持仓详情优先使用加仓/持有/减仓语义。

任何因子得分都不能绕过：现金下限、集中度、组合风险预算、数据覆盖率和市场状态约束。

### 7.3 价格计划

- 入场区间基于可复现的技术位置，如MA20/MA50、近期支撑和ATR，不由DeepSeek自由生成。
- 止损价基于结构失效或ATR风险距离，并受单笔风险预算限制；不能固定为现价90%。
- 目标价优先来自已验证的风险收益框架、阻力位或最新一致预期区间；证据不足时允许 `price_target=null`，不制造精确数字。
- 返回 `reward_risk_ratio` 和每个价格的计算依据。

---

## 八、DeepSeek 协作协议

### 8.1 输入

DeepSeek只能接收已经验证和标注时间的数据：

- 最终确定性评级和允许动作范围。
- 因子原始值、标准化值、贡献、覆盖率和历史有效性。
- P1市场状态、组合风险、绑定约束和策略配置。
- 最新持仓、成本、原币价格和历史决策。
- 明确提供的新闻、财报或预期，不得自行假设。

### 8.2 输出

```json
{
  "executive_summary": "简体中文",
  "bull_case": [],
  "bear_case": [],
  "counterarguments": [],
  "invalidation_conditions": [],
  "data_limitations": [],
  "change_explanation": "简体中文"
}
```

不再要求DeepSeek输出最终 `rating`、目标仓位或自由格式价格。最终响应中的这些字段来自确定性决策引擎。

### 8.3 反例审查

DeepSeek的重点任务不是迎合模型，而是检查：

- 正向结论是否被单一因子主导。
- 质量与估值是否因周期、会计口径或一次性项目失真。
- 资金流、新闻和预期是否已经过期。
- 同公司、ETF重叠、币种和组合集中是否抵消个股吸引力。
- 本次建议变化是否有真正的新证据。

### 8.4 最终校验

- 文本包含与最终评级相反的动作词时，返回校验失败并使用规则模板。
- DeepSeek引用未提供的数据时删除该证据并降低置信度。
- 没有新数据、模型版本或策略配置变化时，不允许仅因LLM文本变化改变方向。

---

## 九、API 契约

### 9.1 `GET /factor-analysis?symbol=NVDA.US`

返回研究与影子结果，不要求该股票必须是持仓：

```json
{
  "symbol": "NVDA.US",
  "model_version": "multifactor-v1.0.0",
  "model_status": "shadow",
  "snapshot_date": "2026-07-31",
  "universe": {"market":"US", "name":".SPX.US", "size":480},
  "factors": {
    "momentum": {"raw":{}, "z_score":0.8, "percentile":78, "contribution":0},
    "value": {"raw":{}, "z_score":-0.2, "percentile":42, "contribution":0},
    "quality": {"raw":{}, "z_score":1.1, "percentile":86, "contribution":0},
    "low_volatility": {"raw":{}, "z_score":-0.5, "percentile":31, "contribution":0},
    "expectation_revision": {"raw":{}, "z_score":0.4, "percentile":65, "contribution":0}
  },
  "composite": {
    "signal":"positive",
    "signal_percentile":82,
    "confidence":0.74,
    "data_coverage":0.85,
    "decision_weight":0
  },
  "flow_overlay": {"available":true, "score_effect":0},
  "quality": {"missing_fields":[], "stale_fields":[], "warnings":[]},
  "source":"longbridge",
  "fetched_at":"2026-08-01T04:00:00Z"
}
```

`model_status=shadow` 时所有 `contribution` 和 `decision_weight` 必须为0。

### 9.2 `/analysis` 增强响应

```json
{
  "rating":"Hold",
  "rating_source":"deterministic_policy_engine",
  "desired_weight":null,
  "binding_constraint":"policy_not_configured",
  "factor_analysis":{},
  "market_regime":{},
  "portfolio_risk":{},
  "narrative":{},
  "audit": {
    "data_snapshot_id":"...",
    "factor_model_version":"multifactor-v1.0.0",
    "factor_model_status":"shadow",
    "risk_model_version":"portfolio-risk-v1",
    "policy_version":1,
    "consistency":"pass"
  }
}
```

---

## 十、前端

### 10.1 个股详情

- 使用条形图而非雷达图作为默认展示，便于比较原始分、市场百分位、贡献和缺失状态。
- 明确显示 `研究中 / 影子运行 / 已验证参与决策`。
- 显示数据覆盖率、更新时间、可比股票池和绑定风险约束。
- shadow模型显示“仅供观察，未影响当前持仓建议”。
- 展示因子贡献时避免把0—100分伪装为上涨概率。

### 10.2 潜力个股发现

- 严格排除当前持仓和同公司跨市场标的。
- 先经过流动性、数据覆盖、趋势和风险准入，再按已验证综合信号排名。
- 加入“对现有组合的边际风险贡献”和相关性，不能只奖励缺失行业。
- 推荐卡片展示：信号百分位、置信度、数据覆盖、建议观察区间、组合互补性和模型状态。
- 模型仍为shadow时使用“研究候选”，不显示确定性买入评级。

---

## 十一、测试、监控与成功指标

### 11.1 单元测试

- winsorize、Z-score、百分位、行业中性和小样本退化。
- 负PE、零PB、财报缺失、停牌、复权异常和新股。
- 缺失因子不重新分配权重，覆盖率和置信度正确下降。
- US/HK分开标准化，禁止跨市场直接排名。
- 周期行业适配，禁止统一PE反转。
- shadow模型的 `decision_weight` 永远为0。
- LLM文字与确定性动作冲突时必须被阻断。

### 11.2 数据与回测测试

- 使用脱敏Longbridge fixture校验 valuation-rank 的 `rank/total` 语义、资金流字段和财报空值。
- point-in-time测试：任何财报发布日期晚于信号日都必须失败。
- IC、IR、分层收益、因子衰减、walk-forward和交易成本计算使用固定数据集回归。
- 参数邻域稳定性与IS/OOS退化检测。
- 同一快照、模型和策略版本重复计算结果100%一致。

### 11.3 在线监控

| 指标 | 目的 |
|---|---|
| 数据覆盖率/陈旧率 | 防止接口失败后模型仍输出高置信度 |
| 5/20/60日相对收益 | 验证不同持有周期 |
| 方向命中率及置信度分桶 | 检查70%置信度是否真的优于50% |
| 建议翻转率 | 发现模型不稳定或数据抖动 |
| 因子分布漂移 | 发现schema、市场结构或标准化异常 |
| 净超额收益与换手成本 | 判断模型是否具有经济价值 |
| LLM冲突率/越权率 | 确保解释层没有改变操作 |

### 11.4 成功标准

- 每个因子都能回答：数据来源、时间、原始值、标准化方法、历史有效性和失效条件。
- 模型晋级有验证报告和审批记录，不通过时对用户操作零影响。
- 最终评级、目标仓位和价格计划均可由确定性输入复算。
- 没有新数据时建议方向保持稳定。
- 模型表现退化后能够自动降级，不继续依靠LLM解释掩盖问题。

---

## 十二、发布顺序与回滚

1. **P2-A 数据快照**：持久化价格、估值、财报、预期和发布日期；不计算综合分。
2. **P2-B 离线研究**：完成单因子IC、分层、衰减和成本报告。
3. **P2-C Shadow**：页面展示因子，不影响评级；积累在线结果。
4. **P2-D Advisory**：通过晋级后，以小权重参与目标仓位；P1风险引擎始终拥有否决权。
5. **P2-E 校准**：依据实盘影子结果调整权重，不直接在线自学习。

回滚开关分离：

- `FACTOR_DATA_ENABLED`
- `FACTOR_SHADOW_ENABLED`
- `FACTOR_ADVISORY_ENABLED`
- `LLM_NARRATIVE_ENABLED`

关闭任何一层后，系统必须回退到上一版本的确定性规则，并保留原因与时间。

---

## 十三、待 DeepSeek 重点审查的问题

1. 五个核心因子是否存在显著重复暴露，怎样做行业中性而不过度消除真实信号？
2. 对个人组合而言，横截面选股信号与持仓减仓信号是否应使用不同模型？
3. 在Longbridge可得字段约束下，哪些质量指标能在US/HK保持可比，哪些必须采用行业适配？
4. 60个交易日影子期是否足以判断稳定性，哪些指标需要更长窗口？
5. 应如何设定晋级和自动降级阈值，既避免过拟合，又避免永远无法上线？
6. 因子目标仓位与P1风险贡献约束冲突时，怎样解释“个股优秀但不应加仓”？
7. DeepSeek还能提供哪些反例审查价值，而不会重新获得最终裁决权？

### 参考数据语义

- Longbridge Valuation Rank：<https://open.longbridge.com/docs/cli/fundamentals/valuation-rank>（每日行业 `rank/total`，不是股票自身历史PE值）
- Longbridge Capital：<https://open.longbridge.com/docs/cli/market-data/capital>（只提供当日资金流，跨日因子需要自行持久化）
- Longbridge Top Movers：<https://open.longbridge.com/docs/market/top-movers>（只适合异常事件与情绪解释）
- 回测使用的字段、权限和返回schema必须在实现时通过 CLI `--help` / `--schema` 与脱敏 fixture 锁定。

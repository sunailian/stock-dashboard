# P1 规格（Review Draft）：市场环境、投资策略配置与组合风险

> 状态：待 DeepSeek 反向审查，不可直接上线影响交易建议
>
> 决策风险：只读分析，不自动下单
>
> 角色：用户负责投资约束确认；Codex 负责数据契约、计算、实现与测试；DeepSeek 负责研究解释与反例审查
>
> 目标：让系统先回答“现在承担多少风险合适、组合风险来自哪里”，再讨论个股操作。

---

## 一、问题与设计原则

当前站点已具备实时持仓、原币价格、账户绩效、持仓建议和建议复盘，但还缺少三个基础能力：

1. 没有可配置的投资策略约束，无法合法推导目标仓位和再平衡漂移。
2. 没有市场环境状态，单股信号无法区分顺风与逆风。
3. 没有组合收益序列、相关性、尾部风险和风险贡献，集中度主要依靠静态阈值。

本阶段遵守以下硬原则：

- **事实与裁决确定化**：市场状态、风险指标、仓位上限和最终操作均由版本化规则计算；LLM 不得覆盖。
- **20% 是目标，不是打分输入**：不得为了接近目标年化而提高评级、放宽止损或增加杠杆。
- **不伪造精度**：没有日频净值就不声称精确回撤；没有历史汇率就明确标注币种风险覆盖不完整。
- **数据失败可降级，结论不可编造**：缺失信号返回 `unavailable` 或降低置信度，不用默认好消息替代。
- **所有结果可复现**：返回模型版本、数据时间、样本数、覆盖率、公式口径和触发的规则。
- **只读优先**：本阶段只展示研究和风险约束，不调用 Longbridge 下单接口。

### 非目标

- 不承诺或预测年化20%。
- 不进行自动交易、自动再平衡或杠杆调整。
- 不在缺少逐日净值与汇率历史时声称得到精确组合 TWR、日内回撤或跨币种风险。
- 不把单日资金流、异动榜或 LLM 情绪作为核心仓位开关。

---

## 二、Layer 0 · 投资策略配置（Investment Policy）

组合风险和再平衡必须依赖用户确认的配置，不能从绩效数据或年化目标反推。新增持久化配置 `INVESTMENT_POLICY`：

```json
{
  "version": 1,
  "base_currency": "CNY",
  "annual_return_objective": 0.20,
  "benchmark_by_market": {"US": "SPY.US", "HK": "HSI.HK"},
  "risk": {
    "target_volatility_annualized": null,
    "max_drawdown_tolerance": null,
    "minimum_cash_pct": null,
    "maximum_invested_pct": null
  },
  "limits": {
    "single_position_pct": null,
    "same_company_pct": null,
    "sector_pct": null,
    "leveraged_etf_pct": null
  },
  "target_bands": [],
  "confirmed_by_user": false,
  "updated_at": null
}
```

### 2.1 配置规则

- `null` 表示尚未确认；系统可以展示风险，但不得生成“偏离目标、应补仓多少”的确定性建议。
- `target_bands` 格式：`{"key":"NVDA.US","type":"symbol","min_pct":0.03,"target_pct":0.05,"max_pct":0.07}`；也支持 `sector`、`market`、`currency` 和 `cash`。
- 同公司跨市场标的以 `company_group` 合并，例如 BABA.US 与 9988.HK。
- 配置修改必须记录版本；分析结果保存 `policy_version`，旧结果不可套用新配置。
- 在用户确认前，可展示“研究用参考阈值”，但必须标注 `provisional=true`，不得伪装成用户的风险偏好。

### 2.2 新接口

- `GET /investment-policy`：读取当前配置。
- `PUT /investment-policy`：更新配置；要求现有会话鉴权，校验比例范围、上下限关系和合计约束。
- 第一版可使用服务端环境变量或受控 JSON；正式版迁移到持久化存储，不能依赖 FC 临时文件。

---

## 三、Layer 1 · 市场环境层（Regime Detection）

### 3.1 分市场计算

美股和港股分别计算，不用单一状态覆盖两个市场：

| 市场 | 趋势基准 | 市场宽度 | 波动率 | 辅助情绪 |
|---|---|---|---|---|
| US | SPY.US，252日复权日线 | `.SPX.US` 成分股涨跌统计或成分股报价 | 20/60日实现波动率；`.VIX.US` 可用时仅作辅助 | SPY.US 大单净流入、市场温度 |
| HK | HSI.HK，252日复权日线 | HSI.HK 成分股涨跌统计 | 20/60日实现波动率 | HSI 代理或成分资金流、市场温度 |

禁止使用：

- `top-movers` 计算市场宽度。该接口是异常波动样本，不代表全市场。
- 9988.HK 作为港股大盘代理。它是单一公司，不是市场指数。
- 休市时把涨跌数全为0解释为中性市场。应沿用最近有效交易日并标注 `as_of`。

### 3.2 信号定义

每个市场固定四组信号，总分0—100：

| 信号 | 权重 | 计算 |
|---|---:|---|
| 趋势 | 40 | close>MA200 得20；MA50>MA200 得20；否则对应为0 |
| 波动率 | 25 | 结合20日和60日年化波动率，按市场自身滚动历史分位评分；低于40%分位偏正，高于80%分位偏负 |
| 市场宽度 | 25 | `rise/(rise+fall)`，0.35映射0分、0.50映射12.5、0.65映射25，区间线性插值 |
| 辅助情绪 | 10 | 市场温度5分 + 当日大单净流入5分；只能小幅修正，不能单独改变状态 |

关键趋势数据缺失时，状态为 `unavailable`。其他信号缺失时按中性分计入，同时降低 `data_coverage` 和 `confidence`，不重新放大剩余信号。

### 3.3 状态与防抖

- `score >= 65`：`aggressive`
- `40 < score < 65`：`balanced`
- `score <= 40`：`defensive`
- 状态至少连续两次有效计算满足新状态，或一次跨越阈值5分以上，才允许切换。
- 每次状态变化保存 `previous_regime`、`change_reason`、`changed_at` 和触发信号。

### 3.4 市场状态不是仓位上限

市场层只输出风险乘数：

```json
{"aggressive": 1.00, "balanced": 0.85, "defensive": 0.65}
```

最终仓位约束由策略配置和组合风险共同计算：

```text
effective_position_cap
  = min(
      policy.maximum_invested_pct,
      volatility_budget_cap,
      drawdown_guard_cap
    )
```

市场乘数作用于新增风险预算，不把现有仓位机械压到固定的90%/75%/50%。如果策略配置未确认，只展示状态和参考乘数，不输出确定仓位上限。

### 3.5 输出 schema

```json
{
  "model_version": "market-regime-v1",
  "markets": {
    "US": {
      "regime": "balanced",
      "score": 58.4,
      "risk_multiplier": 0.85,
      "confidence": 0.88,
      "data_coverage": 0.90,
      "signals": {
        "trend": {"score": 40, "above_ma200": true, "ma50_above_ma200": true, "as_of": "2026-07-31"},
        "volatility": {"score": 12.5, "annualized_20d": 0.22, "annualized_60d": 0.19},
        "breadth": {"score": 5.9, "rise": 210, "fall": 290, "flat": 0, "ratio": 0.42},
        "sentiment": {"score": 0, "large_net_flow": null, "market_temperature": null}
      }
    },
    "HK": {"regime": "unavailable", "score": null, "confidence": 0, "data_coverage": 0}
  },
  "portfolio_weighted_regime": "balanced",
  "source": "longbridge",
  "fetched_at": "2026-08-01T04:00:00Z"
}
```

示例数值仅说明字段，不作为测试期待值。

---

## 四、Layer 2 · 组合风险层（Portfolio Risk）

### 4.1 输入与日期口径

- 实时账户：现有 `get_account_snapshot()`，包括持仓、市值、现金、币种与当前汇率。
- 行情：每个持仓至少252个复权日线；不足60日的标的不参与协方差并标记缺失。
- 权重：以净资产为分母，同时区分 `gross_exposure`、现金和负债；不能只以股票市值合计为分母。
- 同市场相关性：使用相同交易日期的收益率。
- 跨市场相关性：使用共同有效日期并返回 `cross_market_lag_warning=true`；不可把不同收盘时点当成完全同步。
- 基准币种：目标为CNY。若没有历史汇率，只能输出本币价格风险 + 当前币种敞口，并设置 `fx_history_covered=false`；不得称为完整CNY组合波动率。

### 4.2 核心指标

| 指标 | 方法 |
|---|---|
| 年化波动率 | 组合日收益标准差 × √252；有完整协方差时校验 `sqrt(w'Σw)` 一致性 |
| Historical VaR 95/99 | 252日组合收益的5%/1%历史分位 |
| CVaR / Expected Shortfall 95 | 小于等于VaR 95阈值的平均损失 |
| 最大回撤 | 252日组合净值曲线的峰谷最大跌幅，返回峰值/谷值日期 |
| 相关性 | Pearson 60日和252日双窗口；仅 `rho>0.75` 标记同向集中风险，`rho<-0.5` 标记潜在分散关系 |
| 风险贡献 | `RC_i = w_i × (Σw)_i / (w'Σw)`，返回各仓位对组合方差的贡献比例 |
| 集中度 | Top1、Top5、HHI、行业、市场、币种、同公司经济敞口、杠杆ETF敞口 |
| 压力测试 | 对科技下跌、利率上升、美元/港币变动等预定义冲击做情景估算，必须标注为情景而非预测 |
| 再平衡漂移 | 仅在 `target_bands` 已配置时计算；未配置返回 `not_configured` |

### 4.3 ETF 与行业口径

- BABA.US 与 9988.HK 合并为同公司经济敞口，但仍分别保留市场和币种敞口。
- 行业分类优先使用 Longbridge 公司/行业数据，硬编码仅作临时回退并标注 `sector_source=fallback`。
- ETF 在没有成分穿透数据时同时计入“ETF未穿透”警告；不能假装已经消除了与持仓股票的重叠。
- 做空、负现金和杠杆ETF使用绝对敞口计算 gross exposure，不能被多空净额掩盖。

### 4.4 风险预算与新增仓位审批

不再采用“防御模式把18%阈值改成12%”的单一规则。新增仓位必须同时通过：

1. 当前与交易后单股/同公司/行业限制。
2. 策略配置的现金下限和最大投资比例。
3. 交易后组合波动率、VaR和风险贡献没有超过配置。
4. 市场状态风险乘数后的新增风险预算仍为正。
5. 数据覆盖率达到最低标准；关键行情缺失时禁止加仓建议。

最终目标仓位计算必须返回每个上限的来源，例如：

```json
{
  "desired_weight_pct": 5.0,
  "binding_constraint": "same_company_limit",
  "constraints": {
    "signal_target_pct": 7.0,
    "single_position_cap_pct": 8.0,
    "same_company_remaining_pct": 5.0,
    "risk_budget_cap_pct": 5.8
  }
}
```

### 4.5 输出 schema

```json
{
  "model_version": "portfolio-risk-v1",
  "policy_version": 1,
  "policy_status": "unconfirmed",
  "snapshot_id": "sha256-of-position-price-policy",
  "risk_currency": "CNY",
  "fx_history_covered": false,
  "metrics": {
    "portfolio_vol_annualized": null,
    "historical_var_95_1d": null,
    "historical_cvar_95_1d": null,
    "max_drawdown_252d": null,
    "gross_exposure_pct": 0,
    "net_exposure_pct": 0
  },
  "risk_contributions": [],
  "correlation_risks": [],
  "potential_diversifiers": [],
  "concentration": {
    "top1_pct": 0,
    "top5_pct": 0,
    "hhi": 0,
    "sector_weights": [],
    "company_group_weights": [],
    "currency_weights": []
  },
  "rebalance": {"status": "not_configured", "drift": []},
  "quality": {
    "history_days": 0,
    "position_coverage_pct": 0,
    "fx_history_covered": false,
    "warnings": []
  },
  "source": "longbridge",
  "fetched_at": "2026-08-01T04:00:00Z"
}
```

---

## 五、API 与系统联动

### 5.1 路由

- `GET /investment-policy`
- `PUT /investment-policy`
- `GET /market?force=0`
- `GET /portfolio-risk?force=0`

均使用现有会话鉴权。错误响应必须包含：

```json
{"error":"...", "source":"...", "retryable":true, "fetched_at":"..."}
```

### 5.2 与 `/analysis` 联动

- `/analysis` 读取同一个 `snapshot_id` 对应的市场与组合风险缓存，不在请求内重复全量抓取所有K线。
- 风险数据可用时，将确定性约束结果传给决策引擎；DeepSeek只获得已经计算好的事实和最终允许动作范围。
- 市场或组合风险数据暂时失败时：
  - 账户实时数据仍然是必须项，失败继续阻断持仓建议。
  - 非关键风险信号可使用带时间戳的 last-known-good；过期后只允许 Hold/Reduce，不允许 Add/Buy。
- 返回 `market_model_version`、`risk_model_version`、`policy_version` 和 `snapshot_id`，用于建议复盘。

### 5.3 缓存与刷新

- 日线与静态行业数据：6小时；新交易日或 `force=1` 失效。
- 市场宽度、资金流、市场温度：交易时段15分钟；休市时缓存到下一交易日。
- 组合风险：30分钟，并在持仓数量、价格、汇率或策略配置签名变化时立即失效。
- 浏览器前端每5分钟读取一次服务端结果即可，不主动触发全量重算。

---

## 六、前端

### 6.1 市场环境

- 分别显示美股、港股状态，禁止只显示一个总徽章掩盖市场差异。
- 显示分数、置信度、覆盖率、最近交易日和状态变化原因。
- `aggressive` 中文使用“风险条件较友好”，避免表达成“应该满仓”。

### 6.2 组合风险

- 核心指标：年化波动率、Historical VaR、CVaR、252日最大回撤、现金比例。
- 展示最大风险贡献仓位、高相关对、行业/同公司/币种集中和数据缺口。
- 策略配置未确认时，显示“目标权重未配置”，不生成虚假的补仓数量。
- 所有指标提供口径提示；缺历史汇率时明确标识“暂未覆盖汇率历史风险”。

---

## 七、测试与成功指标

### 7.1 单元测试

- regime 分数边界、缺失信号、休市数据和两次确认防抖。
- Historical VaR、CVaR、最大回撤、协方差波动率、风险贡献之和。
- 同交易日对齐、跨市场日期错位、缺少历史、零波动和负仓位。
- 正相关与负相关分类不能混淆。
- 同公司跨市场合并、ETF未穿透、行业未知和净/总敞口。
- target band 未配置时不得输出补仓建议。
- 市场防御时风险乘数生效，但不能绕过或擅自改写用户策略配置。

### 7.2 契约与回归测试

- 为 Longbridge 实际响应保存脱敏 fixture，测试字段变化和空值。
- `/market`、`/portfolio-risk` 单个数据源失败时仍返回部分结果和质量状态。
- 现有账户、绩效、持仓建议19项测试全部继续通过。
- 同一 `snapshot_id + model_version + policy_version` 必须产生相同确定性结果。

### 7.3 成功指标

| 指标 | 验收目标 |
|---|---|
| 可复现性 | 相同快照重复计算100%一致 |
| 数据可追溯 | 每个关键指标均包含来源、as_of、样本数和模型版本 |
| 降级能力 | 任一非关键市场信号失败不白屏、不编造 |
| 风控覆盖 | 所有Add/Buy建议均返回交易后约束检查 |
| 建议稳定性 | 无数据或策略变化时不得因LLM随机性改变方向 |

---

## 八、发布阶段

1. **P1-A 数据与策略配置**：实现统一行情适配、行业/同公司映射、`investment-policy`；只展示数据质量。
2. **P1-B 市场环境影子模式**：至少记录20个交易日，不影响操作建议；检查状态翻转频率。
3. **P1-C 组合风险影子模式**：与人工样本核对VaR、回撤、风险贡献和集中度。
4. **P1-D Advisory**：通过验收后，风险预算才可以限制加仓；仍不自动下单。

回滚开关：`MARKET_REGIME_ENABLED`、`PORTFOLIO_RISK_ENABLED`、`RISK_GATING_ENABLED` 分离，禁止一个总开关导致无法独立回退。

---

## 九、待 DeepSeek 重点审查的问题

1. 0—100市场状态权重是否存在重复计量，阈值如何通过历史数据校准？
2. 在无法立即获得历史汇率时，跨币种组合风险应如何最诚实地降级？
3. 风险贡献和集中度哪个约束优先，是否需要对杠杆ETF单独建立情景模型？
4. 市场状态防抖的“两次确认/跨越5分”是否足够，如何避免错过快速崩盘？
5. 用户尚未确认风险偏好时，哪些指标可以展示，哪些操作建议必须禁止？

### 参考数据语义

- Longbridge Top Movers：<https://open.longbridge.com/docs/market/top-movers>（异常波动与相关新闻，不是全市场宽度）
- Longbridge Capital：<https://open.longbridge.com/docs/cli/market-data/capital>（当日大/中/小单分布与分钟净流入）
- Longbridge CLI 当前命令与字段以部署环境中的 `longbridge --help`、`longbridge <command> --help` 和 `--schema` 为准。

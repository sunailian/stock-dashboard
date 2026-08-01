# P1 规格：市场环境层 + 组合风险层

> 目标：让站点从"单股建议"升级为"知道现在该激进还是防守 + 组合哪里失衡"。
> 交付物：2 个新 API 路由（`/market`、`/portfolio-risk`）+ 前端 2 个新面板 + 与现有 `/analysis` 联动。
> 数据源：全部来自 Longbridge（CLI/MCP/OpenAPI 三通道，代码里统一封装）。

---

## 一、Layer 1 · 市场环境层（Regime Detection）

### 1.1 输入信号（4 组，全部 Longbridge 数据）

| # | 信号 | 数据源 | 标的 | 计算 |
|---|------|--------|------|------|
| 1 | 大盘趋势 | `candlesticks` (day, 250) | SPY.US（美股）+ 9988.HK 或恒指代理（港股，可选） | close vs MA200、MA50 vs MA200 |
| 2 | 波动率 | `candlesticks` (day, 30) → 20 日收益率年化 std | SPY.US | σ_annual = std(20d log returns) × √252 |
| 3 | 市场宽度 | `top_movers` (US, count=50) | 全市场 | 上涨家数 / 总数（涨跌家数比） |
| 4 | 资金情绪 | `capital_flow` (SPY.US) | SPY.US | 当日主力净流入方向 |

### 1.2 输出 schema

```json
{
  "regime": "aggressive | balanced | defensive",
  "regime_label": "进攻 | 平衡 | 防御",
  "total_position_cap_pct": 90,
  "cash_buffer_pct": 10,
  "risk_budget": 0.15,
  "signals": {
    "trend": {"above_ma200": true, "ma50_above_ma200": true, "trend_label": "上升"},
    "volatility": {"annualized_20d": 0.18, "vol_label": "中", "regime_contribution": "balanced"},
    "breadth": {"up_count": 32, "down_count": 18, "breadth_ratio": 0.64, "regime_contribution": "aggressive"},
    "capital_flow": {"main_net_inflow": 123456789.0, "flow_label": "流入", "regime_contribution": "aggressive"}
  },
  "combined_score": 7,
  "recommendation": "当前市场环境支持进攻策略，可维持较高仓位，但需保留现金缓冲。",
  "source": "longbridge",
  "fetched_at": "2026-08-01T04:00:00Z"
}
```

### 1.3 打分规则（0-10，默认 5）

| 信号 | 规则 | 分值 |
|------|------|------|
| 趋势 | close > MA200 且 MA50 > MA200 | +3 |
| 趋势 | 仅 close > MA200 | +1 |
| 波动率 | σ < 0.15 | +2 |
| 波动率 | σ > 0.28 | -2 |
| 宽度 | breadth_ratio > 0.6 | +2 |
| 宽度 | breadth_ratio < 0.4 | -2 |
| 资金 | 主力净流入 | +1 |
| 资金 | 主力净流出 | -1 |

**映射**：score ≥ 7 → aggressive；4 ≤ score ≤ 6 → balanced；≤ 3 → defensive。

**仓位上限**：aggressive=90%，balanced=75%，defensive=50%。现金缓冲=100%-上限。

### 1.4 缓存
- 缓存 15 分钟（市场环境变化慢，且 top_movers/capital_flow 是当日数据）
- `MARKET_CACHE = {'saved_at': 0, 'data': None}`

---

## 二、Layer 2 · 组合风险层（Portfolio Risk）

### 2.1 输入
- 现有 `get_account_snapshot()`（已有持仓/市值/汇率）
- 全持仓 `candlesticks` (day, 60) → 日收益序列
- 现有 `live_position_context()`（已有单仓/板块/公司敞口）

### 2.2 计算内容

| 指标 | 公式/方法 | 输出 |
|------|-----------|------|
| 组合日波动率 | Σ Σ wᵢwⱼσᵢσⱼρᵢⱼ（权重×协方差矩阵） | portfolio_vol（年化） |
| 相关性矩阵 | 持仓两两 Pearson 相关系数（60 日收益） | correlation_pairs: [{pair, rho}]，筛出 \|rho\|>0.7 |
| 预期回撤 | 组合波动率 × √持有期（简化 VaR，95% ≈ 1.65σ） | var_95_pct, expected_drawdown |
| 行业集中度 | 按真实经济敞口合并（BABA+9988 同组） | 每个 sector 的权重，>30% 标红 |
| 再平衡漂移 | 当前权重 vs 目标权重（从 performance/目标配置） | drift: [{symbol, current, target, delta}] |

### 2.3 输出 schema

```json
{
  "portfolio_vol_annualized": 0.21,
  "var_95_5d": 0.045,
  "expected_drawdown": 0.25,
  "correlation_pairs": [
    {"pair": "GOOG-MSRFT", "rho": 0.82, "risk": "同板块高相关，分散效果弱"},
    {"pair": "NVDA-SMH", "rho": 0.75, "risk": "半导体重叠"}
  ],
  "sector_weights": [
    {"sector": "科技", "weight": 0.28, "flag": "normal"},
    {"sector": "半导体", "weight": 0.34, "flag": "concentrated"}
  ],
  "drift": [
    {"symbol": "TSLA", "current_pct": 4.2, "target_pct": 6.0, "delta_pp": -1.8, "action": "低于目标，可考虑补仓"}
  ],
  "concentration_warnings": ["半导体板块合计 34%，超过 30% 上限"],
  "rebalance_suggestion": "建议将半导体板块降至 30% 以内，增加低相关资产。",
  "source": "longbridge",
  "fetched_at": "2026-08-01T04:00:00Z"
}
```

### 2.4 阈值规则
- 板块权重 > 30% → `concentrated` 警告
- 相关性 \|rho\| > 0.7 → 列为高相关对
- 权重漂移 > 5pp → 触发再平衡提醒
- 组合波动率 > 0.25 → 建议降杠杆/加现金

### 2.5 缓存
- 缓存 30 分钟（需要拉全持仓 K 线，较重；且日频数据变化慢）
- 首次调用或 force=1 时全量计算，否则用缓存

---

## 三、API 路由

### 3.1 `GET /market`
- 鉴权：`request_authorized()`（与现有路由一致）
- 响应：Layer 1 schema
- 失败：503 + `{'error': '市场环境数据获取失败', 'detail': ...}`

### 3.2 `GET /portfolio-risk`
- 鉴权：`request_authorized()`
- 响应：Layer 2 schema
- 失败：503 + detail（沿用现有模式）

### 3.3 联动 `/analysis`
- 在 `/analysis` 响应中注入 `market_regime`（来自 Layer 1）和 `portfolio_risk`（来自 Layer 2）
- prompt 中传给 DeepSeek：`市场环境: {regime_label}，组合波动率: {vol}，相关警告: {...}`
- 风控增强：regime=defensive 时，Buy/Overweight 的集中度阈值从 18% 降到 12%

---

## 四、前端

### 4.1 新增面板
- **市场环境面板**（放在 header 下方或 hero 区）：regime 徽章（进攻🟢/平衡🟡/防御🔴）+ 仓位上限 + 现金缓冲 + 4 信号摘要
- **组合风险面板**（替换或增强现有 sector/alloc 图）：组合波动率 + VaR + 高相关对列表 + 再平衡建议

### 4.2 数据流
- 页面加载时 `fetchAccount()` 成功后并行 `fetchMarket()` + `fetchPortfolioRisk()`
- 每 5 分钟刷新（market 15 分钟缓存，risk 30 分钟缓存，前端 5 分钟拉一次即可）
- 401 → showLogin

### 4.3 展示示例
```
[市场环境] 🟢 进攻 | 仓位上限 90% | 现金缓冲 10%
  趋势 ↑ | 波动 18% 中 | 宽度 64% | 资金流入

[组合风险] 波动率 21% | VaR(5日) 4.5% | 预期回撤 25%
  ⚠ 半导体 34% 集中 | 高相关: GOOG-MSFT 0.82
  💡 建议: 半导体降至 30%，补低相关资产
```

---

## 五、实现注意

1. **数据源统一**：现有 `longbridge_request()`（OpenAPI 签名）已可用，新增调用复用同一封装。MCP 工具（candlesticks/capital_flow/top_movers）对应 OpenAPI 路径需在实现时确认（candlesticks → /v1/quote/candlesticks，capital_flow → /v1/quote/capital-flow）。
2. **SPY 标的**：美股用 SPY.US；若持仓全是港股，可加恒指代理（9888.HK 或 HSI 指数——需确认 Longbridge 是否支持指数代码，不支持则用 9988.HK 作为港股大盘代理）。
3. **相关性计算**：用 `statistics` 或纯 Python 实现 Pearson（项目当前零第三方依赖哲学，避免 numpy 依赖）。60 日窗口，至少 30 个有效点才计算，否则该对标 None。
4. **错误降级**：某个信号失败（如 top_movers 超时）→ 该信号按中性处理（不给分），不阻断整个 /market。correlation 数据不足 → 只输出有足够样本的对。
5. **测试**：新增 `tests/test_market_risk.py`，覆盖：
   - regime 打分映射（各阈值边界）
   - 相关性矩阵（构造已知相关序列验证 rho）
   - 板块集中度阈值
   - 漂移计算
   - 防御模式下集中度阈值收紧
6. **可审计**：所有输出带 `source` + `fetched_at`，沿用现有 `data_scope` 哲学。

---

## 六、验收标准

- [ ] `GET /market` 返回完整 schema，regime 与实际市场状态合理
- [ ] `GET /portfolio-risk` 返回相关性/集中度/漂移
- [ ] `/analysis` 响应包含 market_regime + portfolio_risk，defensive 模式收紧阈值生效
- [ ] 测试全过（新增 + 现有 19 个）
- [ ] 前端两个新面板正常渲染，5 分钟刷新
- [ ] 任一数据源失败时优雅降级，不白屏

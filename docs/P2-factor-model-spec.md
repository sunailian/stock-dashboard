# P2 规格：个股多因子策略层（Multi-Factor Alpha）

> 目标：把现有 `/analysis` 从"动量+趋势"升级为五因子模型，让操作建议有基本面、资金面和情绪面的支撑。
> 交付物：因子计算模块 + `/analysis` 增强 + 因子明细展示 + 测试。
> 前置：P1 已完成（市场环境 + 组合风险）。

---

## 一、五因子模型

| 因子 | 权重 | 数据源 | 信号定义 | 打分 |
|------|------|--------|---------|------|
| **F1 动量/趋势** | 25% | `candlesticks` (day, 250) | close vs MA50/MA200、60日动量 | 现价>MA50>MA200 → 10分；每缺一档 -3 |
| **F2 估值分位** | 25% | `valuation` / `valuation_history` | PE/PB 5年分位 | 分位<30% → 10分；30-60% → 6分；>60% → 3分 |
| **F3 质量因子** | 20% | `financial_report` (最新季报) | ROE、负债率、毛利率、FCF | ROE>15% +2.5×4项 |
| **F4 资金流** | 15% | `capital_flow` | 主力净流入 5 日 | 连续净流入 → 10分；净流出 → 3分 |
| **F5 情绪/预期** | 15% | `consensus` + `quote`(IV) | 分析师评级变化、目标价上修 | 目标价>现价 20%+ → 10分；<现价 → 3分 |

### 合成规则
```
factor_score = Σ(Fi_weight × Fi_score)
rating_map: ≥8 → Buy, 6.5-8 → Overweight, 4.5-6.5 → Hold, 3-4.5 → Underweight, <3 → Sell
```

### 周期股修正
能源/化工/钢铁/航运/银行/地产（`SECTOR_CYCLICAL` 集合）：
- F2 估值分位**反向解读**（高 PE 往往在周期底部，低 PE 在顶部）
- 输出附加字段 `cyclical_caveat`

### 数据不足降级
- 某因子缺数据（如新股无 5 年估值历史）→ 该因子权重**重新归一化**到剩余因子，并在 `missing_factors` 标注
- 少于 3 个因子可用 → 回退到现有 `fallback_analysis`

---

## 二、`/analysis` 增强

### 请求不变，响应新增字段

```json
{
  "factor_analysis": {
    "F1_momentum": {"score": 8, "detail": "close>MA50>MA200，60日动量 +12%"},
    "F2_valuation": {"score": 6, "detail": "PE 分位 45%", "percentile": 0.45},
    "F3_quality": {"score": 7, "detail": "ROE 18%，负债率 42%"},
    "F4_capital_flow": {"score": 5, "detail": "5日主力净流入 +1.2亿"},
    "F5_sentiment": {"score": 7, "detail": "目标价高于现价 18%"},
    "factor_score": 6.9,
    "rating_implied": "Overweight",
    "missing_factors": [],
    "cyclical_caveat": null
  },
  "market_regime": "balanced",
  "portfolio_risk": {"portfolio_vol": 0.21, "concentration_warnings": []}
}
```

### 与现有逻辑的关系
- `factor_analysis.rating_implied` 是**模型原始输出**
- 现有 `normalize_analysis` + `validate_decision` 继续做硬风控审批（评级文字一致性、价格关系、集中度、证据校验）
- 若 factor_rating 与 DeepSeek 评级冲突：以 DeepSeek 为基准输出，但 `consistency` 审计里记录分歧；factor_rating 作为参考维度展示

### prompt 注入
在现有 DeepSeek prompt 的"持仓与真实技术数据"前追加：
```
多因子模型参考：{factor_analysis}
市场环境：{market_regime.regime_label}（仓位上限 {cap}%）
组合风险：波动率 {vol}，{concentration_warnings}
```

---

## 三、前端

### 3.1 个股详情弹窗增强
在现有"成本/现价/盈亏/操作建议"下方新增**因子雷达或条形图**：
```
因子评分：动量 8/10 ████████ | 估值 6/10 ██████ | 质量 7/10 ███████ | 资金 5/10 █████ | 情绪 7/10 ███████
综合因子分：6.9 → Overweight
⚠ 周期股提示（如适用）
```

### 3.2 推荐区增强
- 候选股推荐卡片增加 `factor_score` 和 2-3 个关键因子标签（如"估值低分位""主力流入"）

---

## 四、实现注意

1. **估值历史**：`valuation_history` 需要确认返回的 PE/PB 序列格式；若历史不足 1 年，降级为仅显示当前 PE 与行业对比（沿用 `longbridge-valuation` 的降级规则）。
2. **财务报告**：`financial_report` 取最新季度；注意港股/美股财报口径差异（ROE 计算用归母净利润/平均净资产）。
3. **资金流**：`capital_flow` 是当日数据（skill 文档明确），5 日净流入需要**每日缓存落盘**或接受只取当日。P2 第一版只取当日主力净流入，历史序列列为 P3。
4. **期权 IV**：`quote` 是否返回 IV 需验证；若不可用，F5 只用 consensus（分析师），IV 列为可选增强。
5. **归一化**：因子权重表用常量 `FACTOR_WEIGHTS = {'momentum':.25, 'valuation':.25, 'quality':.20, 'capital_flow':.15, 'sentiment':.15}`，缺失因子按比例重归一化——纯函数，可单测。
6. **测试**：新增 `tests/test_factor_analysis.py`：
   - 权重归一化（缺 F2 时其余权重正确重分配）
   - 周期股估值反向解读
   - rating_map 边界
   - 因子分与 DeepSeek 评级冲突时的审计记录

---

## 五、验收标准

- [ ] `/analysis` 返回 factor_analysis 完整字段
- [ ] 因子评分与真实数据一致（抽 3 只持仓人工核对 PE 分位/ROE/资金流）
- [ ] 周期股正确反向解读估值
- [ ] 缺数据时因子归一化正确、不崩
- [ ] 前端详情弹窗展示因子雷达
- [ ] 测试全过

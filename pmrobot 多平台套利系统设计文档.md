# **pmrobot 多平台套利系统设计文档 (v2.0)**

| 版本 | 修改日期 | 修改人 | 修改说明 |
| :---- | :---- | :---- | :---- |
| v1.0 | 2026-02-28 | Gemini | 初始版本：聚焦 Polymarket 与 Azuro 的 Web3 多平台套利设计 |
| v2.0 | 2026-02-28 | Antigravity | 修订版：锁定 Polygon 链上体育赛事跨平台套利，删除 Azuro 内部套利阶段，明确技术实现方案 |

## **1. 目标与范围**

利用 **Polymarket（CLOB 订单簿）** 与 **Azuro（AMM 流动性池）** 两种不同定价引擎的价格差进行**跨平台合成套利**。

**锁定范围：**
- **链：** Polygon（两平台共享同一链，资金无需跨链桥接）
- **赛道：** 体育赛事（Sports）——两平台主要重叠领域
- **策略：** 跨平台合成套利（买便宜平台 + 对冲贵平台）

**明确放弃：**
- ~~Azuro 平台内套利~~（AMM 机制下 Yes+No ≈ 1+fees，套利空间极小）
- ~~第一阶段独立实现~~（直接进入跨平台套利）

## **2. 顶层架构**

```
┌──────────────────────────────────────────────────────────────┐
│                    ArbitrageBot (main.py)                     │
│                    主程序 / 编排协调器                          │
├──────┬──────────┬──────────┬──────────┬──────────┬───────────┤
│Scan  │ CrossArb │ CrossExe │  Risk    │ Settler  │ Notifier  │
│扫描   │ 跨平台   │ 跨平台    │ 风控器   │ 结算器   │ 通知器    │
│      │ 检测器   │ 执行控制  │          │          │           │
└──┬───┴────┬─────┴────┬─────┴────┬─────┴────┬─────┴─────┬─────┘
   │        │          │          │          │           │
   ▼        ▼          ▼          ▼          ▼           ▼
┌──────────────────────────────────────────────────────────────┐
│             Exchange Adapter Layer (统一接口)                  │
├─────────────────────┬────────────────────────────────────────┤
│  PolymarketExchange  │          AzuroExchange                │
│  (CLOB + WebSocket)  │  (GraphQL Subgraph + LP Contract)    │
└─────────────────────┴────────────────────────────────────────┘
```

### **2.1 核心组件**

| 模块 | 职责 |
|------|------|
| **Exchange Adapter** | 抽象层，统一两平台的价格查询、下注、持仓查询接口 |
| **Market Alignment** | 结构化规则匹配两平台的同一事件（赛事ID/日期/队名），规则失败时 LLM 兜底 |
| **Cross-Platform Detector** | 实时对比两平台价格，发现跨平台套利机会 |
| **Cross-Platform Executor** | 并发向两平台下单，异常时紧急冲销 |

## **3. Exchange Adapter 设计**

### **3.1 抽象接口 (`BaseExchange`)**

```python
class BaseExchange(ABC):
    """统一交易所适配器接口"""

    @abstractmethod
    async def get_markets(self, sport: str = None) -> List[UnifiedMarket]:
        """获取市场列表"""

    @abstractmethod
    async def get_odds(self, market_id: str) -> UnifiedOdds:
        """获取当前赔率（含深度/滑点信息）"""

    @abstractmethod
    async def place_bet(self, market_id: str, outcome: str,
                        amount: float, min_odds: float) -> BetResult:
        """下注（带滑点保护）"""

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        """查询当前持仓"""
```

### **3.2 Azuro 适配器关键技术**

**数据源：** Azuro Data-Feed Subgraph on Polygon
```
https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon
```

**价格冲击模拟：** Azuro 是 AMM，大额交易有价格冲击。
- 使用合约的赔率计算函数进行预成交模拟
- `Effective_Price = Total_Cost / Expected_Payout`
- 下注时设置严格 `minOdds` 参数防止滑点

**下注流程：**
1. 查询 LP 合约获取当前赔率和 maxBet
2. Approve USDC 给 LP 合约
3. 调用 `lp.bet()`，传入 `conditionId`, `outcomeId`, `amount`, `minOdds`, `deadline`

## **4. 跨平台套利策略**

### **4.1 合成二元对冲**

**获利条件：** `Price_PM(Yes) + Price_AZ(No) < 1.0 - fees - gas` 或反之

机器人自动选择价格最低的平台执行 YES 侧，在另一个平台执行 NO 侧。

**举例：**
```
事件："曼城 vs 利物浦 — 曼城胜？"

Polymarket: Yes = $0.48 (Ask)
Azuro:      No  = $0.46 (有效价格，含价格冲击)
─────────────────────────
总成本:     $0.94
保证收益:   $1.00 - $0.94 = $0.06 (6.4%)
```

### **4.2 合成 Negative Risk 套利（暂不实现）**

> **状态：** 已评估，当前阶段不实现。

针对多选项市场（如"英超冠军是谁"），跨平台选取每个选项的最优报价：

**获利条件：** `min(PM_Yes_i, AZ_Yes_i)` 各项求和 `< 1.0 - fees`

**暂不实现原因：** Azuro 体育市场以二元（胜/负/平）为主，多选项市场（如联赛冠军）极少。Polymarket 与 Azuro 在多选项市场上的重叠率接近零，实际触发概率极低。待两平台多选项市场丰富后可考虑实现。

### **4.3 跨平台做空对齐（暂不实现）**

> **状态：** 已评估，当前阶段不实现。

若 Polymarket 出现 `Bid(Yes) + Bid(No) > 1.0`，且 Azuro 对同一事件有低价 outcome，可通过在 Polymarket Mint+Sell 同时在 Azuro 低价对冲。

**暂不实现原因：** 需要两个条件同时满足——PM 出现 Short 机会 + Azuro 有同一事件的低价 outcome，实际同时出现的概率极低。且 Azuro bet 为 NFT 无法即时退出，断腿风险过高。

## **5. 事件对齐设计**

### **5.1 结构化规则匹配（优先）**

体育赛事结构化信息丰富，大部分可用规则对齐：

1. **赛事类型 + 日期 + 参与方** → 精确匹配
2. **标准化映射表**：`"Manchester City" ↔ "Man City" ↔ "曼城"`
3. **结果映射**：`"Yes/No" ↔ "Win/Lose" ↔ "Home/Away"`

### **5.2 LLM 语义判定（兜底）**

仅规则匹配失败时调用，判断两平台 Question 是否逻辑等价。

- **批量判定：** 将多个未匹配的 Question 对（最多 10 对/批次）合并到一次 LLM 调用中，大幅减少 API 调用频率和 token 消耗
- **持久化缓存：** 判定结果通过 SHA-256 哈希存入本地 SQLite（`data/alignment_cache.db`），重启后自动加载，避免重复 LLM 调用
- **成本控制：** 预期大多数体育赛事通过结构化规则即可匹配，无需 LLM。配合批量调用 + SQLite 缓存，LLM 调用次数可降至最低
- **两阶段流程：** Phase 1 结构化匹配（快速、免费）→ Phase 2 收集全部未匹配对、批量调用 LLM

## **6. 执行策略：带滑点保护的并发执行**

### **6.1 Optimistic Concurrent Firing**

同时向两平台发送交易指令：
- **Polymarket 侧：** FOK Limit Order（Fill-or-Kill）
- **Azuro 侧：** `lp.bet()` + 严格 `minOdds`

### **6.2 异常冲销 (Emergency Unwind)**

若一侧失败、另一侧已成交：
- **Polymarket 已成交：** 反向市价单平仓
- **Azuro 已成交：** Azuro bet 为 NFT 凭证，需要等待结算或标记为裸头寸进行风险管理

> ⚠️ **重要风险：** Azuro 的 bet 是 NFT（ERC-721），下注后无法像订单簿一样即时卖出。断腿场景下 Azuro 侧的头寸只能持有至结算。因此需要在利润阈值中额外计入此风险溢价。

## **7. 配置扩展**

`.env` 新增配置项：

```env
# ═══ Azuro 配置 ═══
AZURO_ENABLED=true
AZURO_LP_ADDRESS=0x...                  # Azuro LP 合约地址 (Polygon)
AZURO_SUBGRAPH_URL=https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon

# 跨平台套利参数
CROSS_PLATFORM_ENABLED=true
CROSS_PROFIT_THRESHOLD=0.03             # 跨平台最低利润阈值 (3%，高于平台内因含额外风险)
CROSS_TRADE_SIZE=50                     # 跨平台单笔金额 (USDC，保守)
ALIGNMENT_USE_LLM=false                 # 是否启用 LLM 事件对齐兜底
LLM_API_KEY=                            # OpenAI/其他 LLM API Key
```

## **8. 风险提示**

1. **Azuro 断腿风险：** Azuro 下注后无法即时退出，跨平台失败时存在裸头寸风险
2. **结算时间差：** Polymarket 和 Azuro 的结算时间可能不同步，存在资金机会成本
3. **Gas 成本：** Azuro 下注需链上 Gas（Polygon Gas 通常便宜但极端行情可能激增）
4. **事件对齐错误：** 若匹配了两个不同事件，可能造成严重亏损（需人工审核高风险对齐）

## **9. 参考文档**

* **Azuro 开发者中心:** [https://gem.azuro.org/](https://gem.azuro.org/)
* **Azuro Data-Feed Subgraph:** [https://gem.azuro.org/hub/apps/APIs/overview](https://gem.azuro.org/hub/apps/APIs/overview)
* **Azuro Smart Contracts:** [https://github.com/Azuro-protocol/Azuro-v2-public](https://github.com/Azuro-protocol/Azuro-v2-public)
* **Polymarket 开发者文档:** [https://docs.polymarket.com/](https://docs.polymarket.com/)
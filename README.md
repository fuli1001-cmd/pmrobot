# Polymarket 套利机器人 (Polymarket Arbitrage Bot)

全自动化的 Polymarket 预测市场套利机器人。该机器人实时监控 Binary 市场和 Negative Risk 市场，利用价格无效率进行无风险套利。

## 💡 核心原理

本机器人利用预测市场的数学性质进行套利。当所有互斥结果的价格总和小于 1（或特定常数）时，即存在无风险获利空间。

### 1. Negative Risk 套利 (主要策略)
Negative Risk 事件包含多个互斥结果（例如“谁会被特朗普提名为财长？”），但其合约机制特殊。

#### 策略 A: Buy-All-Yes (买入所有 Yes)
*   **原理**：在一个互斥事件中，最终只有一个结果为 Yes (价值 $1)，其余均为 No (价值 $0)。
*   **获利条件**：如果购买所有结果的 Yes 合约总成本 < $1.0。
    *   `Sum(Price_Yes) < 1.0 - 费用 - 利润阈值`
*   **操作**：同时买入该事件下所有 Outcome 的 Yes 合约。

#### 策略 B: Buy-All-No (买入所有 No)
*   **原理**：在一个有 N 个结果的互斥事件中，最终有 N-1 个结果为 No (价值 $1)，只有一个结果为 Yes。
*   **获利条件**：如果购买所有结果的 No 合约总成本 < $(N-1)。
    *   `Sum(Price_No) < (N-1) - 费用 - 利润阈值`
*   **操作**：同时买入该事件下所有 Outcome 的 No 合约。

### 2. Binary 市场套利
*   **原理**：简单的二元市场（Yes/No）。
*   **获利条件**：`Price(Yes) + Price(No) < 1.0`。
*   **操作**：同时买入 Yes 和 No。

---

## 🚀 系统流程

### 1. 扫描 (Scanner)
机器人启动时，会扫描 Polymarket 上活跃的、有订单簿的市场。
*   过滤掉流动性低的市场。
*   识别 Negative Risk 事件组。

### 2. 监控 (Monitor)
建立 WebSocket 连接，实时接收订单簿 (Order Book) 推送。
*   当检测到满足套利条件的瞬时价格时，会立即在日志中输出标志：
    *   `🚀 [OPPORTUNITY] Negative Risk Arbitrage Detected`
*   **日志字段说明**：
    *   `net_profit`: 预期净收益率 (扣除预估费用后)。
    *   `threshold`: 配置文件中设置的最低利润阈值 (`PROFIT_THRESHOLD`)。
    *   **买入仅仅当**：`net_profit >= threshold`。

### 3. 执行 (Execute)
一旦发现机会：
*   **Dry Run 模式**：仅记录模拟交易日志，统计模拟利润。
*   **实盘模式**：调用 CLOB API 并发下单 (使用 `asyncio.gather`)，争取原子性成交。

### 4. 结算 (Settlement)
持有的一组互斥代币（如 Yes + No，或 Negative Risk 的全套代币）可以在到期前合并销毁，换回 USDC。
*   `PositionSettler` 模块会定期检查账户持仓。
*   自动调用 Gnosis Safe 分联合约 (CTF Exchange) 进行合并 (Merge) 操作，锁定利润并释放资金。

---

## 🛠️ 快速开始

### 1. 环境准备

```bash
# 克隆仓库
cd d:\projects\pmrobot

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

复制示例配置文件：

```bash
copy .env.example .env
```

编辑 `.env` 填入您的 API 密钥和私钥 (仅实盘需要)：

```env
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_PASSPHRASE=your_passphrase
PRIVATE_KEY=your_wallet_private_key
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/your_key
```

### 3. 运行

```bash
# 模拟运行 (Dry Run) - 安全，不消耗资金
python main.py --dry-run

# 实盘运行 (Production) - ⚠️ 涉及真实资金风险
python main.py
```

要查找套利机会日志，请在 `log.txt` 中搜索关键字：**`[OPPORTUNITY]`**。

---

## ⚙️ 关键配置说明

| 参数 | 默认值 | 说明 |
|-----------|---------|-------------|
| `PROFIT_THRESHOLD` | 0.008 (0.8%) | **买入触发条件**。只有当 (预期利润 / 总成本) >= 此阈值时，才会触发交易。 |
| `SINGLE_TRADE_SIZE` | 100 | 单笔交易金额 (USDC)。 |
| `MAX_SLIPPAGE` | 0.002 | 最大允许滑点。 |
| `MERGE_INTERVAL` | 3600 | 自动持仓合并的时间间隔 (秒)。 |

## ⚠️ 风险提示

*   **滑点风险**：从检测到成交存在时间差，价格可能变动。
*   **部分成交**：可能只有部分腿 (Leg) 成交，导致产生裸头寸风险。
*   **API 故障**：Polymarket API 或 RPC 节点可能不稳定。

**建议始终先在 `--dry-run` 模式下观察一段时间，确认策略表现。**

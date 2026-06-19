# 预测市场套利机器人 (Prediction Market Arbitrage Bot)

全自动化的多平台预测市场套利机器人。支持 **Polymarket** (CLOB) 平台内套利和 **Polymarket ↔ SX Bet** 跨平台合成套利，实时监控 Binary（二元）市场和 Negative Risk（负风险）市场，利用价格无效率自动捕获无风险（或接近无风险）利润。

> **一句话概括**：当一组互斥结果的价格之和偏离理论值时——无论在同一平台内或跨平台——通过买入或铸造+卖出来锁定差价利润。

---

## 📖 目录

- [背景知识：什么是预测市场和套利](#-背景知识什么是预测市场和套利)
- [核心策略详解](#-核心策略详解)
- [跨平台套利 (Cross-Platform)](#-跨平台套利-cross-platform)
- [系统架构](#-系统架构)
- [项目结构](#-项目结构)
- [快速开始](#-快速开始)
- [部署地区建议](#-部署地区建议)
- [配置参数](#-配置参数)
- [命令行选项](#-命令行选项)
- [通知渠道](#-通知渠道)
- [日志与调试](#-日志与调试)
- [风险提示](#-风险提示)
- [常见问题](#-常见问题)

---

## 📚 背景知识：什么是预测市场和套利

### 预测市场

预测市场（Prediction Market）是一种可以对真实世界事件结果下注的交易平台。比如：

> **"2024 年美国总统大选谁会赢？"**

市场会为每个候选人生成一个合约：

| 合约     | 当前价格 | 含义                         |
|----------|----------|------------------------------|
| Trump Yes | $0.55   | 市场认为 Trump 有 55% 概率赢 |
| Trump No  | $0.47   | 市场认为 Trump 有 47% 概率输 |

- **Yes 合约价格 $0.55** 意味着花 $0.55 买入，如果该结果发生，你将获得 $1.00（净赚 $0.45）。
- **No 合约价格 $0.47** 意味着花 $0.47 买入，如果该结果不发生，你将获得 $1.00。

> 💡 理论上，一个二元事件的 Yes + No 价格应该恰好等于 **$1.00**（因为两者互斥且穷尽）。但在实际交易中，由于流动性、延迟和做市行为，它们的和可能偏离 $1.00。这就是套利机会。

### 什么是套利

**套利（Arbitrage）** 是指利用价格差异，同时执行多笔交易来锁定无风险利润。

**简单例子**：如果你能以 $0.51（Yes）+ $0.47（No）= **$0.98** 的成本同时买入 Yes 和 No，无论结果如何你都将收到 $1.00，净赚 **$0.02**（约 2% 利润）。

### Polymarket 简介

[Polymarket](https://polymarket.com) 是基于 Polygon 区块链的去中心化预测市场。它使用 USDC（一种与美元 1:1 锚定的稳定币）作为交易货币，通过中央限价订单簿（CLOB）撮合交易。

**关键术语**：
- **USDC**：美元稳定币，1 USDC = $1.00
- **CLOB**：中央限价订单簿——类似股票交易所的撮合系统
- **CTF**：条件代币框架（Conditional Token Framework）——Polymarket 底层的智能合约，管理代币的铸造与合并
- **Polygon**：以太坊的 Layer 2 网络，交易快、Gas 费低
- **FOK 订单**：Fill-or-Kill，要么全部成交要么全部取消——用于保证原子性
- **Negative Risk 市场**：一种特殊市场类型，包含多个互斥结果（>2个）

### SX Bet 简介

[SX Bet](https://sx.bet) 是基于 SX Network（Arbitrum Orbit L2, chain ID 4162）的去中心化预测市场。同样使用 USDC (6 decimals) 作为基础代币，通过 peer-to-peer CLOB 撮合交易。

**关键属性**：
- **0% taker fee**（5% oracle fee 仅对赢利部分收取）
- **EIP-712 签名**：taker 下单需通过 EIP-712 typed data 签名
- **desiredOdds + oddsSlippage**：类似 FOK 的滑点保护
- **Taker minimum**：1 USDC
- **Odds 精度**：`percentageOdds = impliedOdds × 10^20`

---

## 💡 核心策略详解

### 1. Binary（二元）市场套利

最简单的形式：一个事件只有 Yes / No 两个结果。

**获利条件**：`Price(Yes) + Price(No) < 1.0`

**举例**：
```
市场："比特币会在年底突破 $100K 吗？"
  Yes 最低卖价 (Ask): $0.48
  No  最低卖价 (Ask): $0.50
  总成本: $0.48 + $0.50 = $0.98
  保证收益: $1.00 - $0.98 = $0.02 (2.0%)
```

**操作**：同时买入 Yes 和 No。不管结果如何，你必定收到 $1.00，锁定 $0.02 利润。

### 2. Negative Risk 套利（主要策略）

Negative Risk 事件包含 **3 个或更多互斥结果**。例如：

> "谁会被提名为财政部长？" → Bessent / Lutnick / Warsh / Other

#### 策略 A：Buy-All-Yes（买入所有 Yes）

**原理**：N 个互斥结果中，最终只有 1 个 Yes = $1.00，其余 = $0.00。

**获利条件**：`Sum(Yes 价格) < $1.00`

**举例（4 个结果）**：
```
Bessent Yes: $0.22    ← 买入
Lutnick Yes: $0.18    ← 买入
Warsh Yes:   $0.33    ← 买入
Other Yes:   $0.20    ← 买入
──────────────────
总成本:      $0.93
保证收益:    $1.00 - $0.93 = $0.07 (7.5% ROI)
```

不管谁最终被提名，你手上的那份 Yes 就值 $1.00，而你总共只花了 $0.93。

#### 策略 B：Buy-All-No（买入所有 No）

**原理**：N 个互斥结果中，最终只有 1 个会发生。对于**没发生**的那些结果，它们的 No 合约都值 $1.00；只有**实际发生**的那个结果，它的 No 合约值 $0.00。所以买入全部 No，最终会有 N-1 个 No 兑付为 $1.00。

**用一句话理解**：你赌"不是 A、不是 B、不是 C、不是 D"，其中必定有 3 个猜对了（因为只有 1 个会赢），所以你必定收到 3 × $1.00 = $3.00。

**获利条件**：`Sum(No 价格) < $(N-1)`

**举例（4 个结果）**：
```
问题："谁会被提名为财政部长？" → 4 个候选人

              No 价格    最终结局（假设 Bessent 被提名）
         ─────────────   ────────────────────────────
Bessent No:  $0.70   ←  他赢了 → 这张 No = $0.00（亏了）
Lutnick No:  $0.80   ←  他没赢 → 这张 No = $1.00（赚了）
Warsh No:    $0.65   ←  他没赢 → 这张 No = $1.00（赚了）
Other No:    $0.78   ←  他没赢 → 这张 No = $1.00（赚了）
             ──────
总成本:      $2.93
保证收回:    $(4-1) = $3.00（永远有 3 个 No 兑付）
净利润:      $3.00 - $2.93 = $0.07 (2.4% ROI)  ✅
```

> 💡 **为什么是 N-1？** 因为 4 个候选人里最终只有 1 个赢——赢的那个 No = $0（你赌他不赢，你输了），剩下 3 个没赢的 No 都 = $1（你赌他不赢，你对了）。所以固定回收 3 × $1 = $3 = $(N-1)。

#### 策略 C：Short Rebalance（做空再平衡）

**背景**：预测市场有时会出现"集体过度乐观"——各结果的 Yes 价格加起来超过 $1.00。理论上互斥事件的 Yes 之和应该等于 $1.00，超出部分就是我们可以捕获的利润。

**获利条件**：`Sum(Yes Bid) > $1.00` 且 `(N-1) - Sum(No Ask) > 阈值`

**举例（3 个候选人竞争一个席位）**：
```
问题："谁会成为下一任 CEO？" → 3 个候选人

              Yes 价格 (Bid)     No 价格 (Ask)
         ──────────────────     ─────────────
Alice:       $0.45               $0.52
Bob:         $0.35               $0.60
Carol:       $0.30               $0.68
             ──────
Sum(Yes) =   $1.10  ← 大于 $1.00！市场高估了！

操作：买入所有 No 合约
No 总成本:   $0.52 + $0.60 + $0.68 = $1.80
保证收回:    $(3-1) = $2.00（永远有 2 个 No 兑付为 $1.00）
净利润:      $2.00 - $1.80 = $0.20 (11.1% ROI)  ✅
```

### 3. Short Arbitrage（做空套利 — Mint+Sell）

**原理**：通过 CTF 合约铸造（Mint）一组完整的 Yes+No 代币，然后在市场上卖出获利。

**获利条件**：`Bid(Yes) + Bid(No) > 1.0 + 手续费 + Gas`

**举例**：
```
市场："比特币会在年底突破 $100K 吗？"
  Yes 最高买价 (Bid): $0.55
  No  最高买价 (Bid): $0.48
  总收入: $0.55 + $0.48 = $1.03
  铸造成本: $1.00 (CTF 合约 mint 1组代币)
  利润: $1.03 - $1.00 - 手续费 = ~$0.02 (2%)
```

**操作**：
1. 调用 CTF 合约的 `splitPosition` 铸造 Yes+No 代币对（花费 $1.00 USDC 得到 1 Yes + 1 No）
2. 同时以 FOK 订单卖出 Yes 和 No 代币
3. 如果总卖出价 > 铸造成本 + 手续费，即锁定利润

> 💡 与 Long Arb（买入两边等结算）不同，Short Arb 通过铸造+卖出**立即**实现利润，无需等待事件结算。但需要链上 Gas（或通过 Relayer 免 Gas）。

---

## 🔀 跨平台套利 (Cross-Platform)

### 原理

Polymarket (CLOB) 和 SX Bet (peer-to-peer CLOB) 使用独立的订单簿和不同的做市群体。对于同一体育赛事，两平台的价格可能存在显著差异。

**当前已实现策略：合成二元对冲（Binary Hedge）** — 在便宜的平台买 YES，在另一个平台买 NO。

```
事件："曼城 vs 利物浦 — 曼城胜？"

Polymarket: Yes = $0.48 (CLOB Best Ask)
SX Bet:     No  = $0.46 (VWAP for trade_size)
─────────────────────────
总成本:     $0.94
保证收益:   $1.00 - $0.94 = $0.06 (6.4%)
```

### 定价准确性

传统的最优报价（best-ask）只反映订单簿顶部的价格，可能与实际成交价差异很大。本系统使用 **VWAP（Volume-Weighted Average Price）** 定价：

- **SX Bet 端**：遍历整个订单簿，按 taker 价格排序，累积直到覆盖 `trade_size`，计算加权平均价格
- **Polymarket 端**：从 CLOB `/book` 接口获取完整 ask 列表，计算深度
- **双重深度门控**：PM 和 SX Bet 的可用深度必须 ≥ `trade_size`，否则放弃该机会

这消除了因薄流动性导致的"幻影套利"——之前版本中 75-90% 的套利信号都是这类虚假信号。

### 费用模型

| 费用项 | 数值 | 说明 |
|--------|------|------|
| SX Bet taker fee | 0% | 免费 |
| SX Bet oracle fee | 5% of winning profit | 因对冲策略只赢一侧，等效约 2.5% |
| CLOB slippage buffer | 0.5% | 补偿报价→成交间的价格变动 |
| SX Bet 执行成本 | ~$0.01 | SX Network gas 极低 |

### 事件对齐

两平台的事件名称格式不同（如 "Man City" vs "Manchester City"），需要先将同一事件配对：

1. **结构化规则（Phase 1）**：球队名标准化 + 日期 ± 6 小时 + 赛事类型 → 精确匹配（免费、快速）
2. **LLM 语义兜底（Phase 2）**：收集所有规则未匹配的 Question 对，**批量提交**给 LLM 判定（最多 10 对/批次），结果持久化缓存到 SQLite，避免重复调用

### EIP-712 签名

SX Bet 要求 taker 使用 EIP-712 structured data 签名。本系统实现了完整的签名流程：

- **Domain**：`{name: "SX Bet", version: domainVersion, chainId: 4162, verifyingContract: EIP712FillHasher}`
- **类型**：嵌套 `Details` + `FillObject` 结构体
- 使用 `eth_account.encode_typed_data` (v0.13.7+) 构造签名

### 关键特点

- **VWAP 定价**：使用完整订单簿计算加权平均成交价，而非 best-ask
- **双重深度门控**：PM 和 SX Bet 端均需足够流动性才会出手
- **滑点保护**：PM 用 FOK 限价单，SX Bet 用 `desiredOdds` + `oddsSlippage`
- **并发执行**：双腿同时下单，最大化成交概率
- **EIP-712 签名**：完整实现 SX Bet fill/v2 标准

> ⚠️ **未实现**：跨平台断腿处理（一侧成功另一侧失败时的自动平仓）尚为 TODO——初期小资金测试时此风险可接受。

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    ArbitrageBot (main.py)                       │
│                    主程序 / 编排协调器                             │
├──────────┬──────────┬──────────┬──────────┬──────────┬──────────┤
│ Scanner  │ Monitor  │ Executor │ Settler  │  Risk    │Cross-Plat│
│ 扫描器   │ 监控器   │ 执行器   │ 结算器   │ 风控器   │ 跨平台   │
└────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘
     │          │          │          │          │          │
 Gamma API  WebSocket   CLOB API  CTF 合约   本地状态   SX Bet API
 (REST)    (实时推送)  (下单/查询) (链上合并)  (统计)   (REST/EIP-712)
```

### 模块说明

| 模块 | 文件 | 职责 |
|------|------|------|
| **Scanner** | `core/scanner.py` | 启动时通过 Gamma API 扫描全量市场，过滤出流动性足够的 Binary 市场和 NegRisk 事件组 |
| **Monitor** | `core/monitor.py` | 通过 WebSocket 实时接收订单簿更新，内置 `ArbitrageDetector` 和 `NegativeRiskArbitrageDetector` 进行实时套利检测 |
| **Executor** | `core/executor.py` | 接收套利机会队列，通过 CLOB API 并发提交 FOK 订单；处理部分成交时的紧急平仓。支持 Long Arb（买入）和 Short Arb（Mint+卖出） |
| **Settler** | `core/settler.py` | 定期扫描 Polymarket 持仓，对可合并的头寸调用 CTF 合约的 `mergePositions`，将代币对换回 USDC |
| **Risk** | `core/risk.py` | 交易统计、市场冷却期、熔断器（连续失败自动暂停） |
| **CTF** | `core/ctf.py` | CTF 合约交互，封装 `splitPosition`（铸造 Yes+No 代币）和 `mergePositions`（合并代币回 USDC） |
| **Cross-Platform** | `core/cross_platform.py` | 跨平台套利检测器 + 执行控制器（PM ↔ SX Bet 二元对冲） |
| **Alignment** | `core/alignment.py` | 跨平台事件对齐（结构化规则 + LLM 兜底） |
| **SX Bet** | `exchanges/sxbet.py` | SX Bet 适配器：市场发现、VWAP + 深度计算、EIP-712 签名、taker fill |
| **Polymarket** | `exchanges/polymarket.py` | Polymarket 适配器：CLOB + WebSocket + Gamma API |
| **Notifier** | `utils/notifier.py` | 支持 Telegram、企业微信、或同时发送到两者 |

### 数据流

```
1. Scanner → 获取 Polymarket 市场列表 → 传给 Monitor
2. Monitor → WebSocket 订阅订单簿 → 检测到套利机会 → 放入队列
   ├─ Long Arb (Ask+Ask < 1.0) → long_opportunity_queue
   └─ Short Arb (Bid+Bid > 1.0) → short_opportunity_queue
3. Executor → 从队列取出机会 → 校验 + 下单 → 更新 Risk 统计
   ├─ Long Arb → 并发买入 Yes + No
   └─ Short Arb → CTF Mint → 并发卖出 Yes + No
4. Settler → 定期检查 PM 持仓 → 找到可合并头寸 → Merge → 收回 USDC
5. Refresher → 定期重新扫描市场 → 动态更新 Monitor 监控列表

═══ 跨平台 ═══
6. CrossScanner → 拉取 PM + SX Bet 市场 → 名称对齐 → 配对
7. CrossDetector → 获取双平台价格 (VWAP+深度) → 计算利润 → cross_queue
8. CrossExecutor → 从队列取出 → 并发双腿下单（PM FOK + SX fill/v2）
```

---

## 📁 项目结构

```
pmrobot/
├── main.py                 # 入口：ArbitrageBot 主循环（含跨平台编排）
├── gen_creds.py            # 辅助脚本：用私钥派生 CLOB API 凭证
├── requirements.txt        # Python 依赖
├── .env                    # 环境变量配置（需自行创建）
│
├── config/
│   ├── settings.py         # Pydantic 配置模型，从 .env 加载
│   └── constants.py        # 链上地址、API URL、阈值常量
│
├── exchanges/              # ★ 统一交易所适配器层
│   ├── base.py             # BaseExchange ABC + 统一数据模型
│   ├── polymarket.py       # Polymarket 适配器（CLOB + WebSocket + Gamma）
│   └── sxbet.py            # SX Bet 适配器（REST + VWAP + EIP-712）
│
├── core/
│   ├── scanner.py          # 市场扫描（Gamma REST API）
│   ├── monitor.py          # WebSocket 监控 + PM 套利检测器
│   ├── executor.py         # PM 订单执行引擎（CLOB API）
│   ├── settler.py          # PM 持仓结算（Relayer 无 Gas / EOA 直签）
│   ├── risk.py             # 风险管理器
│   ├── ctf.py              # CTF 合约交互（Mint/Merge）
│   ├── alignment.py        # ★ 跨平台事件对齐（结构化规则 + LLM 兜底）
│   └── cross_platform.py   # ★ 跨平台套利检测器 + 执行控制器
│
├── models/
│   ├── market.py           # 市场/事件/套利机会数据模型
│   ├── order.py            # 订单/订单簿/机会数据模型
│   ├── position.py         # 持仓/账户状态数据模型
│   └── cross_models.py     # ★ 跨平台套利机会 + 执行报告模型
│
├── utils/
│   ├── logger.py           # structlog 日志配置
│   ├── notifier.py         # 通知服务（Telegram / 企业微信）
│   ├── rate_limiter.py     # API 限速器
│   └── name_normalizer.py  # ★ 球队/选手名称标准化
│
├── tests/                  # 测试和调试脚本
│   ├── test_eip712.py      # EIP-712 签名验证
│   ├── test_monitor.py     # Monitor 单元测试
│   ├── test_scanner.py     # Scanner 单元测试
│   └── ...                 # 其他调试脚本
│
├── logs/                   # 运行日志输出目录
└── docs/                   # 设计文档
```

---

## 🚀 快速开始

### 前提条件

- Python 3.11+
- Polygon 钱包 (MATIC + USDC) — 用于 Polymarket
- SX Network 钱包 (USDC) — 用于 SX Bet 跨平台套利
- （可选）Polymarket CLOB API 凭证

### 1. 安装

```bash
# 克隆仓库
git clone <repo-url>
cd pmrobot

# 创建 conda 环境（推荐）
conda create -n pmrobot python=3.11
conda activate pmrobot

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

创建 `.env` 文件：

```bash
copy .env.example .env   # Windows
cp .env.example .env     # macOS/Linux
```

编辑 `.env`：

```env
# ═══ 必填（实盘交易） ═══
PRIVATE_KEY=0xYourPolygonWalletPrivateKey
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# CLOB API 凭证（可通过 gen_creds.py 从私钥派生）
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_PASSPHRASE=your_passphrase

# Proxy Wallet（Gnosis Safe 地址，由 Polymarket 为你创建）
PROXY_WALLET_ADDRESS=0xYourProxyWallet

# Relayer API Key（Short Arb 的 proxy mint 需要）
RELAYER_API_KEY=your_relayer_api_key
RELAYER_API_KEY_ADDRESS=0xYourSignerAddress
RELAYER_TX_TYPE=SAFE
# 如 proxy 余额为 pUSD，可改为 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
CTF_COLLATERAL_ADDRESS=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

# ═══ SX Bet（跨平台套利必填） ═══
SXBET_ENABLED=true
SXBET_API_KEY=your_sx_bet_api_key
SXBET_PRIVATE_KEY=0xYourSxNetworkPrivateKey   # 32 字节私钥，非地址！

# ═══ 跨平台套利 ═══
CROSS_PLATFORM_ENABLED=true
CROSS_PROFIT_THRESHOLD=0.01      # 1%（推荐初始值）
CROSS_TRADE_SIZE=25              # 每笔跨平台金额 (USDC)
ALIGNMENT_USE_LLM=true
LLM_API_KEY=your_llm_key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# ═══ 交易参数 ═══
PROFIT_THRESHOLD=0.005           # PM 平台内利润阈值 (0.5%)
MAX_TRADE_SIZE=20                # PM 平台内最大下单金额 (USDC)
DEPTH_SAFETY_MULTIPLIER=1.5      # 订单簿安全冗余倍数
MAX_SLIPPAGE=0.002               # 最大滑点 (0.2%)
MERGE_INTERVAL=600               # 自动合并间隔 (秒)
MARKET_REFRESH_INTERVAL=600      # 全量刷新 + 跨平台扫描间隔 (秒)

# ═══ 可选 ═══
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/...
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

DRY_RUN=true                     # 强烈推荐先 dry run
LOG_LEVEL=INFO
ENV=production
```

#### 如何获取 CLOB API 凭证

```bash
python gen_creds.py
```

从私钥派生 API Key / Secret / Passphrase。

### 3. 运行

```bash
# ✅ 推荐：先以 Dry Run 模式运行
python main.py --dry-run

# 调整日志级别
python main.py --dry-run --log-level DEBUG

# JSON 格式日志
python main.py --dry-run --log-json

# ⚠️ 实盘运行（真实资金！先确保 dry-run 稳定 24h+）
python main.py
```

## 🌍 部署地区建议

Polymarket 会按出口 IP 做 `geoblock`。本项目在 live 模式启动前会先调用官方接口 `https://polymarket.com/api/geoblock` 做预检查；如果当前出口 IP 属于 blocked 区域，机器人会直接拒绝启动实盘交易。

基于 Polymarket 官方 geoblock 文档，当前部署建议如下：

- **优先推荐：AWS `eu-west-1`（Ireland）**
  - 官方文档将其标记为 **Closest Non-Georestricted Region**
  - 地理上接近 Polymarket 主服务器，且不是官方 blocked 区域
- **不推荐：AWS `eu-west-2`（London）**
  - 官方文档写明 `Primary Servers: eu-west-2`
  - 但同一文档也明确 `GB United Kingdom` 属于 `Blocked`
  - 这意味着它虽然更近，但普通部署大概率会因英国出口 IP 被拒单
- **不推荐：美国与德国节点**
  - 官方 blocked 列表明确包含 `US United States` 和 `DE Germany`
  - 因此常见的 AWS 弗吉尼亚 `us-east-1`、法兰克福 `eu-central-1` 都不适合作为 live trading 出口

建议在目标机器上先手工确认一次：

```bash
curl https://polymarket.com/api/geoblock
```

只有返回 `blocked: false`，才说明该机器当前出口 IP 可用于 Polymarket 主站下单。最终是否可交易，以这个接口的实时返回为准，而不是仅凭机房名称判断。

官方参考：

- Polymarket Geographic Restrictions: `https://docs.polymarket.com/api-reference/geoblock`

---

## ⚙️ 配置参数

### Polymarket 平台内参数

| 参数 | 环境变量 | 默认值 | 范围 | 说明 |
|------|----------|--------|------|------|
| 利润阈值 | `PROFIT_THRESHOLD` | 0.008 (0.8%) | 0.001~0.1 | 套利触发最低利润率 |
| 最大下单金额 | `MAX_TRADE_SIZE` | 100.0 | 1~10000 | 实际下单金额上限，最终金额会按订单簿深度动态下调 |
| 深度安全倍数 | `DEPTH_SAFETY_MULTIPLIER` | 1.5 | 1~10 | 要求每条腿具备的订单簿冗余倍数，越大越保守 |
| 最大滑点 | `MAX_SLIPPAGE` | 0.002 (0.2%) | 0.01%~5% | 加权均价偏差上限 |
| 合并间隔 | `MERGE_INTERVAL` | 600 (10min) | 60~3600 | Settler 自动合并周期 |
| 刷新间隔 | `MARKET_REFRESH_INTERVAL` | 1800 (30min) | 0~86400 | 全量市场 + 跨平台扫描间隔 |

### 下单金额逻辑说明

- `MAX_TRADE_SIZE` 是单次机会允许使用的最大预算，不代表每次都会按这个金额下单。
- 系统会先根据 YES/NO 两条腿前几档可买深度，结合 `DEPTH_SAFETY_MULTIPLIER`，计算安全上限 `safe_max_size`。
- `safe_max_size = min(MAX_TRADE_SIZE, safe_max_self, safe_max_other)`，其中 `safe_max_self` / `safe_max_other` 是两条腿各自按深度和安全倍数推导出的安全预算上限。
- 然后系统会在不超过 `safe_max_size` 的候选金额里，寻找仍然满足利润阈值、平台最小下单约束、以及双腿都能完整吃到的最大可行金额，作为最终实际下单金额。
- 所以最终关系是：`实际下单金额 <= safe_max_size <= MAX_TRADE_SIZE`。
- 当 `safe_max_size` 本身也满足利润和最小下单约束时，最终实际下单金额就会等于 `safe_max_size`。
- 日志中的 `configured_max_trade_size`、`safe_max_trade_size`、`trade_size` 分别对应：配置上限、安全上限、最终实际下单金额。

### 跨平台参数

| 参数 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| 启用跨平台 | `CROSS_PLATFORM_ENABLED` | false | 启用 PM ↔ SX Bet 跨平台套利 |
| 利润阈值 | `CROSS_PROFIT_THRESHOLD` | 0.03 (3%) | 跨平台最低利润（含 oracle fee 后） |
| 单笔金额 | `CROSS_TRADE_SIZE` | 50.0 | 跨平台单笔 USDC 金额 |
| LLM 对齐 | `ALIGNMENT_USE_LLM` | false | 启用 LLM 事件名称对齐兜底 |
| LLM Key | `LLM_API_KEY` | — | OpenAI/DeepSeek 兼容 API Key |
| LLM URL | `LLM_BASE_URL` | api.openai.com/v1 | LLM API 基础 URL |
| LLM 模型 | `LLM_MODEL` | gpt-4o-mini | 模型名称 |

### SX Bet 参数

| 参数 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| 启用 SX Bet | `SXBET_ENABLED` | false | 启用 SX Bet 适配器 |
| API Key | `SXBET_API_KEY` | — | SX Bet REST API key |
| 私钥 | `SXBET_PRIVATE_KEY` | — | SX Network 钱包私钥（EIP-712 签名） |
| API URL | `SXBET_API_URL` | api.sx.bet | SX Bet API 基础 URL |
| Chain ID | `SXBET_CHAIN_ID` | 4162 | SX Network chain ID |

### 利润阈值调优建议

**PM 平台内**：
- 保守：`PROFIT_THRESHOLD=0.015`（1.5%）——只抓大机会
- 平衡：`PROFIT_THRESHOLD=0.008`（0.8%）——默认值
- 激进：`PROFIT_THRESHOLD=0.003`（0.3%）——频繁交易，利润薄

**跨平台**：
- 推荐初始值：`CROSS_PROFIT_THRESHOLD=0.01`（1%）
- ⚠️ 不建议低于 0.8%：扣除 SX Bet oracle fee（~2.5% of profit）+ slippage buffer（0.5%）后，低于 0.8% 的名义利润可能为负

### 初始测试资金推荐

| 平台 | 推荐金额 | 用途 |
|------|---------|------|
| Polymarket (Polygon USDC) | $300 | PM 平台内套利 + 跨平台 PM 腿 |
| SX Bet (SX Network USDC) | $200 | 跨平台 SX 腿 |
| **总计** | **$500** | 最小可运行配置 |

推荐初始参数：`MAX_TRADE_SIZE=20`, `DEPTH_SAFETY_MULTIPLIER=1.5`, `CROSS_TRADE_SIZE=25`

---

## 🖥️ 命令行选项

```
python main.py [选项]

选项：
  --env {production,testnet}  运行环境（默认 production）
  --dry-run                   模拟运行，不执行真实交易
  --log-level {DEBUG,INFO,WARNING,ERROR}
                              日志级别（默认 INFO）
  --log-json                  以 JSON 格式输出日志
```

---

## 📬 通知渠道

机器人支持三种通知模式：

1. **Telegram**：配置 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
2. **企业微信**：配置 `WECHAT_WEBHOOK_URL`（详见 [企业微信消息推送配置说明.md](企业微信消息推送配置说明.md)）
3. **双通道**：同时配置时，通知会并发发送到两个渠道
4. **无通知**：均未配置时仅写日志

通知内容包括：启动/关停、交易成功/失败、持仓合并、异常告警。

---

## 📋 日志与调试

日志输出到控制台和 `logs/pmrobot.log`（自动创建，每日轮转）。

### 关键日志关键词

| 搜索关键词 | 含义 |
|-----------|------|
| `opportunity detected` | PM 平台内套利机会被检测到 |
| `Cross-platform opportunities found` | 跨平台套利机会被检测到 |
| `DRY RUN` | 模拟交易记录 |
| `Emergency exit` | 部分成交，触发紧急平仓 |
| `Merge successful` | PM 持仓合并成功（USDC 回收） |
| `Pair evaluated` | 跨平台配对评估结果 |
| `Pair skipped` | 跨平台配对被跳过（深度不足/无报价） |
| `circuit breaker` | 熔断器触发（连续失败暂停交易） |
| `VWAP` | SX Bet VWAP 定价日志 |

### 调试技巧

```bash
# 查看实时跨平台机会
python main.py --dry-run --log-level DEBUG 2>&1 | findstr "opportunities found"

# 查看 SX Bet 深度数据
python main.py --dry-run --log-level DEBUG 2>&1 | findstr "Pair evaluated"

# 查看 PM 平台内机会
python main.py --dry-run --log-level DEBUG 2>&1 | findstr "opportunity"
```

---

## ⚠️ 风险提示

| 风险类型 | 说明 | 缓解措施 |
|----------|------|----------|
| **滑点风险** | 从检测到下单存在延迟 | PM: FOK 订单 + 滑点上限; SX: desiredOdds + oddsSlippage |
| **部分成交** | 多腿交易只有部分腿成交 | PM 平台内: 自动紧急平仓; 跨平台: TODO（初期小资金） |
| **API 故障** | API 或 RPC 不稳定 | WebSocket 自动重连 + 指数退避 |
| **Gas 消耗** | PM Settler merge + Short Arb mint | Gas 成本已纳入利润计算；支持 Relayer 免 Gas |
| **跨链风险** | PM (Polygon) 和 SX (SX Network) 在不同链 | 目前需手动再平衡资金 |
| **资金安全** | 私钥泄露 = 资金损失 | 环境变量存储，独立钱包，.gitignore 排除 .env |

### 安全建议

1. **务必先运行 `--dry-run`** 至少观察 24 小时
2. 使用独立的交易钱包，不要使用主钱包
3. 从小金额开始（推荐 PM $300 + SX $200）
4. 定期检查日志中的异常告警
5. 确保 `.env` 文件不被提交到版本控制

### 已知限制

| 功能 | 状态 | 说明 |
|------|------|------|
| PM 平台内套利 (5 种策略) | ✅ 已实现 | Binary Long/Short, NegRisk Buy-All-Yes/No, Short Rebalance |
| PM ↔ SX Bet 跨平台套利 | ✅ 已实现 | 二元对冲，VWAP 定价，EIP-712 签名 |
| SX Bet 平台内套利 | ❌ 未计划 | SX Bet 流动性不足，单平台套利机会极少 |
| 跨平台断腿处理 | ⚠️ TODO | 一侧成功另一侧失败时的自动平仓 |
| SX Bet 持仓追踪 | ⚠️ TODO | SX Bet 侧的 P&L 跟踪 |
| 跨平台资金再平衡 | ❌ 手动 | PM/SX 在不同链，需手动桥接 |
| SX Bet API 限速 | ⚠️ TODO | 无 429 退避重试 |

---

## ❓ 常见问题

### Q: Dry Run 模式下会消耗资金吗？
**A**: 不会。Dry Run 模拟所有交易和合并操作，余额显示为虚拟的 $10,000。

### Q: 套利真的是"无风险"吗？
**A**: 理论上是——只要所有腿同时成交。实际中最大风险是**部分成交**（只有部分订单填满），此时 PM 端会以折扣价紧急卖出止损。

### Q: 为什么跨平台利润阈值设得比平台内高？
**A**: 因为 SX Bet 有 5% oracle fee（等效 ~2.5%） + VWAP 滑点缓冲 0.5%，加上跨链结算时间更长。建议 `CROSS_PROFIT_THRESHOLD >= 0.01`（1%）。

### Q: SXBET_PRIVATE_KEY 怎么填？
**A**: 填 SX Network 上的钱包**私钥**（64 位十六进制, 0x 前缀），不是钱包地址（40 位）。私钥用于 EIP-712 签名下单。

### Q: 两个平台的资金需要在同一个钱包吗？
**A**: 不需要。PM 使用 Polygon 上的钱包（`PRIVATE_KEY`），SX Bet 使用 SX Network 上的钱包（`SXBET_PRIVATE_KEY`），可以是不同地址。

### Q: 如何获取 SX Bet API Key？
**A**: 访问 [SX Bet Developer Portal](https://api.docs.sx.bet) 注册获取。

### Q: 观察了很久都没有机会出现？
**A**: 建议：
- 适当降低 `PROFIT_THRESHOLD`
- 确保 `MARKET_REFRESH_INTERVAL > 0`
- 使用 `--log-level DEBUG` 查看 `Pair evaluated` 日志
- 跨平台机会通常出现在体育赛事密集时段

### Q: 机器人使用哪些 API？
**A**:
- **Gamma REST API** — PM 市场列表和元数据
- **CLOB WebSocket** — PM 实时订单簿推送
- **CLOB REST API** — PM 下单、查余额
- **Polygon RPC** — CTF 合约铸造/合并
- **SX Bet REST API** — SX 市场发现、报价、下单
- **SX Network RPC** — SX USDC 余额查询
- **LLM API**（可选）— 事件名称语义对齐

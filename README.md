# 预测市场套利机器人 (Prediction Market Arbitrage Bot)

全自动化的多平台预测市场套利机器人。支持 **Polymarket** 和 **Azuro** 两大平台，实时监控 Binary（二元）市场和 Negative Risk（负风险）市场，利用价格无效率自动捕获无风险（或接近无风险）利润。支持平台内套利（Long / Short / NegRisk）和**跨平台合成套利**（Polymarket vs Azuro）。

> **一句话概括**：当一组互斥结果的价格之和偏离理论值时——无论在同一平台内或跨平台——通过买入或铸造+卖出来锁定差价利润。

---

## 📖 目录

- [背景知识：什么是预测市场和套利](#-背景知识什么是预测市场和套利)
- [核心策略详解](#-核心策略详解)
- [跨平台套利 (Cross-Platform)](#-跨平台套利-cross-platform)
- [系统架构](#-系统架构)
- [项目结构](#-项目结构)
- [快速开始](#-快速开始)
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

**为什么有效？** ——当 Sum(Yes) > $1.00 时，意味着 Sum(No) < $(N-1)（因为对同一个结果，Yes 价格 ≈ 1 - No 价格）。我们通过买入被低估的 No 合约来间接做空被高估的 Yes 市场。

> 💡 这个策略的本质和 Buy-All-No 一样（都是买入全部 No），只是触发条件不同：Buy-All-No 直接看 No 价格之和是否低于 N-1，而 Short Rebalance 是通过观察 Yes 价格之和超过 1.00 来发现同样的机会。

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

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                   ArbitrageBot (main.py)                │
│                   主程序 / 编排协调器                      │
├─────────┬───────────┬───────────┬───────────┬───────────┤
│ Scanner │  Monitor  │ Executor  │  Settler  │   Risk    │
│  扫描器  │  监控器   │  执行器    │  结算器   │  风控器    │
└────┬────┴─────┬─────┴─────┬─────┴─────┬─────┴─────┬─────┘
     │          │           │           │           │
 Gamma API  WebSocket    CLOB API   CTF 合约    本地状态
 (REST)    (实时推送)   (下单/查询)  (链上合并)   (统计)
```

### 模块说明

| 模块 | 文件 | 职责 |
|------|------|------|
| **Scanner** | `core/scanner.py` | 启动时通过 Gamma API 扫描全量市场，过滤出流动性足够的 Binary 市场和 NegRisk 事件组 |
| **Monitor** | `core/monitor.py` | 通过 WebSocket 实时接收订单簿更新，内置 `ArbitrageDetector` 和 `NegativeRiskArbitrageDetector` 进行实时套利检测 |
| **Executor** | `core/executor.py` | 接收套利机会队列，通过 CLOB API 并发提交 FOK 订单；处理部分成交时的紧急平仓。支持 Long Arb（买入）和 Short Arb（Mint+卖出）两种执行路径 |
| **Settler** | `core/settler.py` | 定期扫描账户持仓，对可合并的头寸调用 CTF 合约的 `mergePositions` 函数，将代币对换回 USDC。支持 Relayer 无 Gas 模式和 EOA 直签模式 |
| **Risk** | `core/risk.py` | 交易统计、市场冷却期、熔断器（连续失败自动暂停） |
| **CTF** | `core/ctf.py` | CTF 合约交互，封装 `splitPosition`（铸造 Yes+No 代币）和 `mergePositions`（合并代币回 USDC）操作 |
| **Notifier** | `utils/notifier.py` | 支持 Telegram、企业微信、或同时发送到两者（CompositeNotifier） |

### 数据流

```
1. Scanner → 获取市场列表 → 传给 Monitor
2. Monitor → WebSocket 订阅订单簿 → 检测到套利机会 → 放入队列
   ├─ Long Arb (Ask+Ask < 1.0) → long_opportunity_queue
   └─ Short Arb (Bid+Bid > 1.0) → short_opportunity_queue
3. Executor → 从队列取出机会 → 校验 + 下单 → 更新 Risk 统计
   ├─ Long Arb → 并发买入 Yes + No
   └─ Short Arb → CTF Mint → 并发卖出 Yes + No
4. Settler → 定期检查持仓 → 找到可合并头寸 → Relayer/EOA Merge → 收回 USDC
5. Refresher → 定期重新扫描市场 → 动态更新 Monitor 的监控列表
6. [跨平台] CrossScanner → 拉取两平台市场 → 对齐事件 → 比较价格 → cross_queue
7. [跨平台] CrossExecutor → 从队列取出 → 并发双腿下单（PM FOK + Azuro LP.bet）
```

---

## 🔀 跨平台套利 (Cross-Platform)

### 原理

Polymarket（订单簿 CLOB）和 Azuro（AMM 流动性池）使用完全不同的定价引擎。对于同一体育赛事，两平台的价格可能存在显著差异。

**当前已实现策略：合成二元对冲（Binary Hedge）** — 在便宜的平台买 YES，在另一个平台买 NO。

```
事件："曼城 vs 利物浦 — 曼城胜？"

Polymarket: Yes = $0.48 (Best Ask)
Azuro:      No  = $0.46 (有效价格，含 AMM 价格冲击)
─────────────────────────
总成本:     $0.94
保证收益:   $1.00 - $0.94 = $0.06 (6.4%)
```

> 💡 **关于其他跨平台策略**：设计文档中的"合成 NegRisk 套利"和"跨平台做空对齐"经评估后暂不实现——前者因两平台多选项市场重叠率接近零，后者因所需条件（PM Short 机会 + Azuro 同一事件低价）同时出现的概率极低，且 Azuro NFT 凭证无法即时退出带来的断腿风险过高。

### 事件对齐

两平台的事件名称格式不同（如 "Man City" vs "Manchester City"），需要先将同一事件配对：

1. **结构化规则（Phase 1）**：球队名标准化 + 日期 ± 6 小时 + 赛事类型 → 精确匹配（免费、快速）
2. **LLM 语义兜底（Phase 2）**：收集所有规则未匹配的 Question 对，**批量提交**给 LLM 判定（最多 10 对/批次），结果持久化缓存到 SQLite，避免重复调用

### 关键特点

- **滑点保护：** Polymarket 用 FOK 限价单，Azuro 用 `minOdds` 参数
- **并发执行：** 双腿同时下单，最大化成交概率
- **断腿处理：** Polymarket 侧可市价反向平仓；Azuro 侧为 NFT，需持有至结算

> ⚠️ **风险：** Azuro bet 是 NFT 凭证，下注后无法即时退出。跨平台利润阈值默认 3%（高于平台内）以补偿此风险。

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
│   ├── polymarket.py       # Polymarket 适配器（CLOB + WebSocket）
│   └── azuro.py            # Azuro 适配器（GraphQL + LP 合约）
│
├── core/
│   ├── scanner.py          # 市场扫描（Gamma REST API）
│   ├── monitor.py          # WebSocket 监控 + 套利检测器
│   ├── executor.py         # 订单执行引擎（CLOB API）
│   ├── settler.py          # 持仓结算（Relayer 无 Gas / EOA 直签）
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
├── tests/
│   ├── test_monitor.py     # Monitor 单元测试
│   └── test_scanner.py     # Scanner 单元测试
│
├── logs/                   # 运行日志输出目录
└── docs/                   # 设计文档
```

---

## 🚀 快速开始

### 前提条件

- Python 3.11+
- 一个 Polygon 钱包（有少量 MATIC 用于 Gas）和 USDC
- （可选）Polymarket 账户的 CLOB API 凭证

### 1. 安装

```bash
# 克隆仓库
git clone <repo-url>
cd pmrobot

# 创建并激活虚拟环境
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

创建 `.env` 文件：

```bash
# Windows:
copy .env.example .env
# macOS/Linux:
cp .env.example .env
```

编辑 `.env`，填入以下信息：

```env
# ═══ 必填（实盘交易） ═══
PRIVATE_KEY=0xYourWalletPrivateKey
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# CLOB API 凭证（可通过 gen_creds.py 从私钥派生）
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_PASSPHRASE=your_passphrase

# Proxy Wallet（Gnosis Safe 地址，由 Polymarket 为你创建）
PROXY_WALLET_ADDRESS=0xYourProxyWallet

# ═══ 可选 ═══
# Builder Program 凭证（启用 Relayer 无 Gas 合并，不配时用 EOA 直签）
BUILDER_API_KEY=
BUILDER_SECRET=
BUILDER_PASSPHRASE=

# 交易参数
PROFIT_THRESHOLD=0.008          # 最低利润阈值 (0.8%)
SINGLE_TRADE_SIZE=100           # 单笔交易金额 (USDC)
MAX_SLIPPAGE=0.002              # 最大滑点 (0.2%)
MERGE_INTERVAL=600              # 自动合并间隔 (秒)
MARKET_REFRESH_INTERVAL=1800    # 全量市场刷新 + 跨平台扫描间隔 (秒，0=禁用)

# 通知 —— 两者均可配置，将同时发送
TELEGRAM_BOT_TOKEN=123456:ABC-DEF
TELEGRAM_CHAT_ID=-1001234567890
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx

# 其他
DRY_RUN=false
LOG_LEVEL=INFO
ENV=production                  # production / testnet
```

#### 如何获取 CLOB API 凭证

如果你有钱包私钥但没有 API 凭证，可以使用自带的派生脚本：

```bash
python gen_creds.py
```

该脚本使用 `py_clob_client` 从私钥派生 API Key / Secret / Passphrase，并打印到控制台。将结果填入 `.env` 即可。

### 3. 运行

```bash
# ✅ 推荐：先以 Dry Run 模式运行，观察日志
python main.py --dry-run

# 调整日志级别查看更多细节
python main.py --dry-run --log-level DEBUG

# JSON 格式日志（适合管道处理或 ELK 采集）
python main.py --dry-run --log-json

# ⚠️ 实盘运行（涉及真实资金！）
python main.py
```

---

## ⚙️ 配置参数

| 参数 | 环境变量 | 默认值 | 范围 | 说明 |
|------|----------|--------|------|------|
| 利润阈值 | `PROFIT_THRESHOLD` | 0.008 (0.8%) | 0.001~0.1 | 只有 `净利润率 ≥ 此值` 时才触发交易。越低越激进。 |
| 单笔金额 | `SINGLE_TRADE_SIZE` | 100.0 | 1~10000 | 每次套利投入的 USDC 总金额。NegRisk 策略会按结果数平分。 |
| 最大滑点 | `MAX_SLIPPAGE` | 0.002 (0.2%) | 0.01%~5% | 深度穿透后的加权均价与最优价的偏差上限。 |
| 合并间隔 | `MERGE_INTERVAL` | 600 (10分钟) | 60~3600 | Settler 自动检查持仓并合并的周期。 |
| 刷新间隔 | `MARKET_REFRESH_INTERVAL` | 1800 (30分钟) | 0~86400 | 定期重新扫描新市场/事件。设为 0 禁用。 |
| API 限速 | `API_RATE_LIMIT` | 10 req/s | 1~100 | Scanner 和 Executor 对 API 的请求速率上限。 |

### Azuro / 跨平台参数

| 参数 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| 启用 Azuro | `AZURO_ENABLED` | false | 启用 Azuro 交易所适配器 |
| LP 合约地址 | `AZURO_LP_ADDRESS` | — | Azuro LP 合约地址 (Polygon) |
| Core 合约地址 | `AZURO_CORE_ADDRESS` | — | Azuro Core 合约地址 (Polygon) |
| Subgraph URL | `AZURO_SUBGRAPH_URL` | (data-feed-polygon) | Azuro 数据子图端点 |
| 启用跨平台 | `CROSS_PLATFORM_ENABLED` | false | 启用跨平台套利 |
| 跨平台利润阈值 | `CROSS_PROFIT_THRESHOLD` | 0.03 (3%) | 跨平台最低利润阈值（高于平台内，因含 Azuro 断腿风险） |
| 跨平台金额 | `CROSS_TRADE_SIZE` | 50.0 | 跨平台单笔 USDC 金额 |
| LLM 对齐 | `ALIGNMENT_USE_LLM` | false | 启用 LLM 事件对齐兜底 |
| LLM Key | `LLM_API_KEY` | — | LLM API Key（支持 OpenAI/DeepSeek 等兼容 API） |
| LLM URL | `LLM_BASE_URL` | api.openai.com/v1 | LLM API 基础 URL（DeepSeek: `api.deepseek.com/v1`） |
| LLM 模型 | `LLM_MODEL` | gpt-4o-mini | 模型名称（DeepSeek: `deepseek-chat`） |

### 利润阈值调优建议

- **保守偏好**：`PROFIT_THRESHOLD=0.015`（1.5%）——只抓大机会，很少交易
- **平衡偏好**：`PROFIT_THRESHOLD=0.008`（0.8%）——默认值，适合大多数情况
- **激进偏好**：`PROFIT_THRESHOLD=0.003`（0.3%）——频繁交易，利润薄，对延迟敏感

> 对于 NegRisk 市场，利润阈值会根据结果数量动态调整（结果越多，阈值按比例放宽），具体逻辑见 `config/constants.py` 中的 `get_profit_threshold()`。
> 
> **跨平台阈值默认 3%**，显著高于平台内，因为 Azuro 下注后生成 NFT 凭证，无法像订单簿一样即时卖出——断腿场景下只能持有至结算。

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
3. **双通道**：同时配置时，通知会并发发送到两个渠道（任一成功即算成功）
4. **无通知**：均未配置时使用 DummyNotifier，仅写日志

通知内容包括：启动/关停、交易成功/失败、持仓合并、异常告警。

---

## 📋 日志与调试

日志输出到控制台和 `logs/pmrobot.log`（自动创建）。

### 关键日志关键词

| 搜索关键词 | 含义 |
|-----------|------|
| `opportunity detected` | 套利机会被检测到（已通过阈值） |
| `queuing for execution` | 机会通过风控，进入执行队列 |
| `DRY RUN` | 模拟交易记录 |
| `Emergency exit` | 部分成交，触发紧急平仓 |
| `Merge successful` | 持仓合并成功（USDC 回收） |
| `NegRisk Price sample` | 定期价格采样（用于观察市场状态） |
| `circuit breaker` | 熔断器触发（连续失败暂停交易） |

### 调试技巧

```bash
# 查看实时机会检测
python main.py --dry-run --log-level DEBUG 2>&1 | findstr /i "opportunity"

# 只看 NegRisk 价格采样
python main.py --dry-run 2>&1 | findstr "NegRisk Price sample"
```

---

## ⚠️ 风险提示

| 风险类型 | 说明 | 缓解措施 |
|----------|------|----------|
| **滑点风险** | 从检测到下单存在延迟，价格可能变动 | FOK 订单 + 滑点上限 (`MAX_SLIPPAGE`) |
| **部分成交** | 多腿交易中只有部分腿成交，产生裸头寸 | 自动紧急平仓（带重试），5%~11% 折价卖出 |
| **API 故障** | Polymarket API 或 Polygon RPC 可能不稳定 | WebSocket 自动重连 + 指数退避 |
| **Gas 消耗** | Settler 合并和 Short Arb 铸造需要链上 Gas | Gas 成本已纳入利润计算；可配置 Relayer 免 Gas 合并 |
| **资金安全** | 私钥泄露 = 资金损失 | 使用环境变量存储私钥，避免硬编码 |

### 安全建议

1. **务必先运行 `--dry-run`** 至少观察 24 小时，确认策略行为符合预期
2. 使用独立的交易钱包，不要使用存有大量资产的主钱包
3. 设置合理的 `SINGLE_TRADE_SIZE`，从小金额开始
4. 定期检查日志中的异常告警
5. 确保 `.env` 文件不被提交到版本控制（已在 `.gitignore` 中排除）

---

## ❓ 常见问题

### Q: Dry Run 模式下会消耗资金吗？
**A**: 不会。Dry Run 模拟所有交易和合并操作——Executor 的订单会被跳过（`SKIPPED`），Settler 的链上合并也会被跳过（仅记录日志），余额显示为虚拟的 $10,000。

### Q: 套利真的是"无风险"吗？
**A**: 理论上是——只要所有腿同时成交。实际中最大的风险是**部分成交**（只有部分订单填满），此时机器人会以折扣价紧急卖出已成交的部分以止损。

### Q: 为什么使用 FOK 订单？
**A**: FOK（Fill-or-Kill）确保每一腿要么完全成交，要么完全取消。这极大降低了多腿策略中出现"一只腿挂了"的概率。

### Q: 观察了很久都没有机会出现？
**A**: 套利机会是短暂的且竞争激烈。建议：
- 降低 `PROFIT_THRESHOLD`（如 0.003）
- 增大扫描范围（确保 `MARKET_REFRESH_INTERVAL > 0`）
- 使用 `--log-level DEBUG` 查看价格采样，确认市场数据正常
- 关注 NegRisk 事件（结果越多，定价低效的概率越高）

### Q: 如何获取 Proxy Wallet 地址？
**A**: 在 Polymarket 网站注册并连接钱包后，系统会为你创建一个 Gnosis Safe 代理钱包。可以在钱包设置页面找到它。这是 CLOB API 实际操作的地址。

### Q: 机器人使用哪些 API？
**A**:
- **Gamma REST API** — 获取市场列表和元数据
- **CLOB WebSocket** — 实时订单簿推送
- **CLOB REST API** — 下单、查余额
- **Polygon RPC** — 调用 CTF 合约进行链上铸造（Mint）和合并（Merge）
- **Relayer API**（可选）— 无 Gas meta-transaction 合并

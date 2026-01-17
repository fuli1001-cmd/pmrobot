# **Polymarket 自动化套利机器人顶层设计文档 (v1.3)**

## **1\. 项目概述**

本项目的目标是构建一个基于 Polymarket 预测市场的自动化套利系统。系统通过监控二元（Binary）及负风险（Negative Risk）市场的定价偏差，利用“荷兰书”（Dutch Book）原理，在 $\\sum Price(Outcome\_i) \< 1.00$ 时执行瞬时无风险套利。

## **2\. 核心数学公理**

* **概率守恒定律**：在有效市场中，$ Price(Yes) \+ Price(No) \= 1.00 $。  
* **套利判定不等式**：$$\\sum\_{i=1}^{n} \\text{AveragePrice}(Outcome\_i) \< 1.00 \- \\text{Threshold} \- \\text{Fees}$$  
  *注：*$\\text{AveragePrice}$ *必须通过深度穿透计算得出。*$\\text{Fees}$ *需根据 2026 年最新费率政策动态过滤。*

## **3\. 系统架构设计**

### **3.1 市场扫描模块 (Scanner \- Gamma API)**

* **功能**：全局搜索活跃市场，获取元数据。  
* **逻辑**：  
  * 过滤条件：active=true, closed=false, enable\_order\_book=true。  
  * **费率动态过滤 (2026 特色)**：  
    * 优先筛选政治预测、体育、长期事件等“零手续费”市场。  
    * 对 15 分钟加密货币市场实施白名单准入，需满足：$\\text{Profit} \> \\text{Taker Fee} (max 3.15\\%) \+ \\text{Threshold}$。

### **3.2 实时监控模块 (Monitor \- WebSocket)**

* **功能**：毫秒级监听订单簿变化。  
* **核心逻辑：深度穿透计算 (Depth Penetration)**：  
  * 输入：预设交易额 (e.g., 500 USDC)。  
  * 过程：向后遍历 Orderbook 档位，计算填满交易额所需的**加权平均价格**。

### **3.3 执行引擎 (Executor \- CLOB API)**

* **认证方式**：API Key \+ L1/L2 Headers。  
* **交易模式**：**Proxy 钱包 (Gnosis Safe) \+ Relayer**。  
  * **Gas 成本控制**：通过 Relayer 路由所有交易，实现“零 Gas”下单与结算。  
* **原子化执行逻辑**：  
  * **批量下单 (Batch Orders)**：单次请求包含 Yes 和 No 的双边订单。  
  * **FOK (Fill-Or-Kill)**：强制执行全额成交或全额取消。

### **3.4 结算模块 (Settler \- Web3.py)**

* **功能**：资金复利回收。  
* **逻辑**：  
  * **自动化合并 (Merge)**：通过 Relayer API 构建元交易签名发送。  
  * **批处理策略**：每 10 分钟或累积一定金额后集中合并，以节省 Relayer 额度。

## **4\. 环境配置与部署清单 (NEW)**

### **4.1 节点要求**

* **Polygon RPC**：优先选择 Alchemy/QuickNode 的 Pro 节点。  
* **环境隔离**：区分 .env.production 和 .env.testnet (Polygon Amoy)。

### **4.2 账户准备**

* **Polymarket Account**：需通过官方 UI 完成注册。  
* **Proxy Wallet**：确保账户已关联 Gnosis Safe 代理钱包。  
* **API Credentials**：从 clob.polymarket.com/settings 获取 API\_KEY, SECRET, PASSPHRASE。

### **4.3 Builder Tier 申请**

* **目标等级**：Verified Tier (1,500 txn/day)。  
* **申请理由**：自动化套利及高频 Merge 操作。

## **5\. 运行参数配置 (NEW)**

| 参数项 | 推荐值 | 说明 |
| :---- | :---- | :---- |
| **Profit Threshold** | **0.8%** | 建议初始设为 0.8%，在零费率市场极具竞争力。 |
| **Single Trade Size** | **100 \- 500 USDC** | 视市场深度而定，防止滑点过大。 |
| **Max Slippage** | **0.2%** | 允许的成交价偏离度。 |
| **Merge Interval** | **600s** | 10 分钟合并一次，平衡资金效率与 Relayer 额度。 |

## **6\. 异常处理与风控**

* **限流保护**：遵守 API 频率限制。  
* **额度报警**：当 Relayer 剩余额度低于 10% 时，通过 Telegram 发送告警。  
* **单腿头寸处理**：若 FOK 异常导致单边成交，机器人需立即执行“反向平仓”以止损。

## **7\. 开发参考资源**

* **官方文档主站**：[docs.polymarket.com](https://docs.polymarket.com/)  
* **Relayer 开发者参考**：[Relayer Client Documentation](https://docs.polymarket.com/developers/builders/relayer-client)

**设计者备注**：优先部署于零手续费市场，严格执行“批量下单 \+ FOK”，并优先使用 Relayer 实现全流程零 Gas 运营。
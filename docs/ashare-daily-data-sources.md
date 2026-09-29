# A 股日线数据源调研

> 核实时间：**2026-09-29**。文中「实测」均为当日从本机直接调用接口验证的结果；
> 库的维护状态来自 GitHub API；积分/频次等条款以各官网最新说明为准。

## 结论速览

| 数据源 | 类型 | 注册/Token | 实测状态（2026-09-29） | 推荐定位 |
| --- | --- | --- | --- | --- |
| 东方财富 push2his | 裸 HTTP 接口 | 不需要 | ✅ 可用，数据到当日 | 主力源（AKShare/efinance 的底层） |
| 腾讯 ifzq.gtimg.cn | 裸 HTTP 接口 | 不需要 | ✅ 可用，数据到当日 | 备用源，自带前/后复权 |
| 新浪 quotes.sina.cn | 裸 HTTP 接口 | 不需要 | ✅ 可用（需带 Referer） | 备用源，仅近期数据、不复权 |
| 雪球 stock.xueqiu.com | 裸 HTTP 接口 | 需要 Cookie | ⚠️ 无 Cookie 返回 400016 | 需登录态，一般不选 |
| 网易 chddata | 裸 HTTP 接口 | 不需要 | ❌ HTTP 502，已失效 | 排除 |
| **AKShare** | Python 库（爬虫类） | 不需要 | ✅ 活跃维护（2026-09-23 仍有推送） | **首选封装库** |
| **Baostock** | Python 库（专用协议） | 不需要 | ✅ 实测登录+取日线成功 | 历史回补/交叉校验 |
| efinance | Python 库（爬虫类） | 不需要 | ✅ 维护中（2026-07-17 推送） | 轻量备选，基于东财 |
| adata | Python 库 | 不需要 | ⚠️ 最近推送 2025-12-26 | 免费多源，但已 ~9 个月未更 |
| qstock | Python 库 | 不需要 | ⚠️ 最近推送 2025-03-16 | 停更约 1.5 年，不建议 |
| pytdx / mootdx（通达信协议） | Python 库 | 不需要 | ⚠️ 原版已归档；mootdx 2024-07 后未更 | 协议仍通，仅作最后兜底 |
| Tushare Pro | Python 库（注册制） | 需要 Token | 未实测（官方文档现行口径见下文） | 标准化好，注意单一服务商依赖 |
| 掘金量化（gm SDK / 东财掘金） | 量化终端 + 多语言 SDK | 需要账号 Token + 终端在线 | ⚠️ SDK 实测可装（3.0.187），取数依赖终端 | 研究回测实盘一体化时才值得用 |
| Wind / Choice / iFinD / JQData / RQData | 商业数据终端 | 付费 | — | 生产/合规场景 |
| QMT(miniQMT) / Ptrade | 券商通道 | 券商开户 | — | 行情+交易一体 |

**给本项目（honey-money）的一句话结论**：维持 README 既定方案，主力用 **AKShare**（底层东财接口），**Baostock** 做历史回补与交叉校验，东财裸接口做最快兜底；日线统一落 PostgreSQL 增量更新。

---

## 一、免费裸 HTTP 接口（curl 直接调，无需注册）

### 1. 东方财富 push2his —— 当前事实上的主力接口 ✅

AKShare 的 `stock_zh_a_hist` 与 efinance 的底层都是它。免注册、支持日/周/月/分钟线、三档复权、单只股票全历史可一次取回（实测浦发银行全史 6399 根，`dktotal` 字段可见）。

```bash
curl "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=5"
```

要点：

- `secid`：沪市 `1.代码`，深市 `0.代码`（如 `0.000001` 平安银行）。
- `klt`：`101` 日线 / `102` 周线 / `103` 月线 / `15/30/60` 分钟线。
- `fqt`：`0` 不复权 / `1` 前复权 / `2` 后复权。
- `ut=fa5fd1943c7b386f172d6893dbfba10b` 是公开的固定参数——**实测不带 `ut` 会返回空**，这是最常见的踩坑点。
- 备用域名 `91.push2his.eastmoney.com` 等前缀分流域名同样可用（实测通过）。
- 返回 `klines` 数组每根格式：`日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率`。
- 频控宽松但非官方承诺：高频抓取建议加限速/代理池（本项目已有 proxy_pool）。

### 2. 腾讯 ifzq.gtimg.cn ✅

```bash
curl "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600000,day,,,5,qfq"
```

- 复权：`qfq` 前复权 / `hfq` 后复权 / 留空不复权；周期：`day/week/month`。
- 代码前缀 `sh` / `sz`。
- 单次返回条数有上限（经验值约 800 根），取全历史需按日期分段翻页。
- 响应里附带 `qt` 实时快照字段，可顺带取最新价。

### 3. 新浪 quotes.sina.cn ✅（有限制）

```bash
curl -H "Referer: https://finance.sina.com.cn" \
  "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh600000&scale=240&ma=no&datalen=5"
```

- `scale=240` 即日线（`60/120` 为分钟级），实测需带 `Referer` 头。
- **不复权**，且 `datalen` 上限约 1000 出头——只能取近年数据，深历史拿不到。
- 定位：临时核对价格用，不适合做主数据源。

### 4. 雪球 stock.xueqiu.com ⚠️ 需 Cookie

`GET /v5/stock/chart/kline.json?symbol=SH600000&period=day&type=before&count=-5`。
实测无 Cookie 直接返回 `400016`（提示重新登录）。需先访问雪球官网取 `xq_a_token` 并随请求携带；token 会过期需维护。数据质量好（含前复权），但维护登录态的成本一般不值当。

### 5. 网易 quotes.money.163.com ❌ 已失效

历史 CSV 下载接口 `/service/chddata.html` 实测返回 **HTTP 502**，多年前已停止服务。网上老文章仍在推荐，勿再选用。

---

## 二、免费开源 Python 库（封装层）

### AKShare —— 首选 ✅

- GitHub `akfamily/akshare`：22.7k+ stars，最新版 **v1.18.97（2026-09-20 发布）**，最近推送 2026-09-23，维护非常活跃，接口失效修得快。
- 免注册、免 token，覆盖股票/基金/期货/宏观/另类数据，广度第一。

```python
import akshare as ak

df = ak.stock_zh_a_hist(
    symbol="600000", period="daily",
    start_date="20260901", end_date="20260929",
    adjust="qfq",  # "" 不复权 / "qfq" 前复权 / "hfq" 后复权
)
# 列：日期 开盘 收盘 最高 最低 成交量 成交额 振幅 涨跌幅 涨跌额 换手率
```

- 日线底层即东财 push2his 接口（见上），稳定性与东财一致。
- 爬虫类库的通病：源站改版接口会临时失效，需跟随升级 `pip install -U akshare`。

### Baostock —— 最稳的免费历史源 ✅（本机实测通过）

- 免注册免 token，专用 TCP 协议（非爬网页），提供 1990 年以来 A 股/指数日线、5/15/30/60 分钟线、复权因子、财务、宏观等。
- 当日数据盘后更新，**实测 2026-09-29 当天即可取到当日 K 线**。
- 安装注意：**本机默认的清华 pip 镜像里装不到，需指定官方源**（实测：清华源报 No matching distribution，`--index-url https://pypi.org/simple` 成功，Python 3.14 可用）。

```python
import baostock as bs

lg = bs.login()  # 免账号，直接 login()
rs = bs.query_history_k_data_plus(
    "sh.600000",
    "date,code,open,high,low,close,volume,amount,turn,pctChg",
    start_date="2026-09-22", frequency="d",
    adjustflag="2",  # 1 后复权 / 2 前复权 / 3 不复权
)
rows = []
while rs.error_code == "0" and rs.next():
    rows.append(rs.get_row_data())
bs.logout()
```

- 项目沉寂但服务稳定，是社区回测场景的默认历史源；缺点是无实时行情、数据更新有盘中延迟。

### efinance ✅

- GitHub `Micro-sheep/efinance`：4k stars，最近推送 2026-07-17，维护中。
- 基于东财接口，股票/基金/债券/期货，API 极简：`ef.stock.get_quote_history("600000")` 默认即日线。
- 与 AKShare 同源（东财），二选一即可，适合只想要轻量依赖的场景。

### adata ⚠️

- GitHub `1nchaos/adata`：5.2k stars，「免费开源 A 股数据库」，免 token、多源聚合，支持 `stock.market.get_market()` 取日 K。
- 最近推送 **2025-12-26**，约 9 个月未更新——接口一旦失效没人修，观望为宜。

### qstock ❌（停更）

- 最近推送 **2025-03-16**，停更约 1.5 年，不建议新项目采用。

### pytdx / mootdx（通达信协议）⚠️ 仅兜底

- 原版 `rainx/pytdx` 已**归档停更**（PyPI 停在 2017 年），但通达信行情协议多年未大改，实测社区反馈在线取数仍通。
- 封装版 `mootdx`（2.3k stars）最近推送 **2024-07-16**，也已两年未动。
- 定位：免费、无频控、速度快，但协议无官方支持，只作最后兜底。

### easyquotation（与本主题相关的补充）

- 5.4k stars，维护中（2026-02 推送），但它是**实时行情快照**（新浪/腾讯/集思录），不提供日线历史，日线需求不用考虑它。

---

## 三、注册制数据服务：Tushare Pro、掘金量化

### Tushare Pro（积分档位）

- 需注册拿 token。数据标准化程度高（复权因子、每日指标、财务一体），文档规范。
- 按官方现行文档（doc_id=27）：**`daily` 每日行情属基础积分权限**，每分钟最多 500 次、单次返回上限 6000 条，交易日 15:00–16:00 入库；未复权数据（配合 `adj_factor` 自行复权）。具体积分/频次以 [tushare.pro 积分表](https://tushare.pro/document/1?docid=108) 为准。
- 风险：单一服务商依赖 + 积分政策会变；社区惯用做法是「Tushare + AKShare/Baostock」双源。

```python
import tushare as ts
pro = ts.pro_api("YOUR_TOKEN")
df = pro.daily(ts_code="600000.SH", start_date="20260901", end_date="20260929")
```

### 掘金量化（Myquant gm SDK / 东财掘金）

**有接口，但形态和上面几类都不同：它首先是「研究 + 回测 + 仿真 + 实盘」一体化终端，数据接口是随平台附带的。** 只想拉日线落库的话，它不是好选择（依赖终端在线）；如果后续要做策略回测/模拟盘，它是一体化选项。

- **SDK**：官方 `pip install gm`（实测 PyPI 官方源可装，掘金3 SDK v3.0.187），另有 C++/C#/Matlab 版。取历史行情核心函数 `history()`，签名（实测从包内提取）：

```python
from gm.api import history
# history(symbol, frequency, start_time, end_time, fields=None,
#         skip_suspended=True, fill_missing=None, adjust=None, adjust_end_time='', df=False)
```

- **日线用法**：`symbol='SHSE.600000'`（沪 `SHSE.` / 深 `SZSE.`），`frequency='1d'`，`adjust` 传 `ADJUST_PRE`（前复权）/`ADJUST_POST`（后复权）/`ADJUST_NONE`（不复权）。
- **使用前提**：掘金账号 + token，且本地需运行**掘金量化终端**（Windows 客户端，SDK 经终端取数、版本需匹配）——这是它区别于 AKShare/Tushare 的最大门槛，NAS/无桌面环境下很难用。
- **权限**：免费账号可用回测与基础历史行情（日线够用），实时 tick、逐笔、分钟级等需数据权限；接口有频次限制，以官方权限说明为准。注意**老版数据函数（如 `get_fundamentals`）已于 2024-09-30 下线**，改用 `stk_get_*` 新系列。
- **「东财掘金」**：即东方财富证券定制版掘金量化终端（[emquant.18.cn](https://emquant.18.cn/help/doc/guide)），在东方财富证券客户端「量化」栏目下载；实盘权限在东财综合服务云平台申请。东财另配套 Choice 数据量化接口（quantapi.eastmoney.com，商业级）。

---

## 四、商业 / 专业级（付费）

| 产品 | 说明 |
| --- | --- |
| Wind 万得 | 券商/基金标配，最全最贵（按席位年费） |
| 东方财富 Choice | 金融终端，价格低于 Wind，A 股数据质量好 |
| 同花顺 iFinD | 性价比高，量化接口完善 |
| 聚宽 JQData / 米筐 RQData | 量化平台数据服务，按年订阅，适合回测 |
| 通联数据 优矿 | 同类量化数据服务 |
| QMT / miniQMT、Ptrade | 券商开户可得（通常有资金门槛），行情+交易一体，miniQMT 可用 Python（xtquant）拉数据 |

本项目当前阶段（日线研究/回测）用不到商业源，不展开。

---

## 五、其他途径

- **yfinance**：A 股可用 `600000.SS` / `000001.SZ` 后缀取到，但 15 分钟延迟、国内网络访问不稳，仅海外环境应急。
- **通达信本地数据文件**：客户端盘后下载的 `.day` 二进制文件可离线解析（mootdx/pytdx 支持读取），无频控，适合 NAS 本地化方案，但要自管客户端下载环节。

---

## 六、选型建议（结合 honey-money）

1. **主力：AKShare**（README 既定方案）——最活跃、覆盖最广、`stock_zh_a_hist` 前复权日线直接可用；封装时隔离在数据层模块内，别让接口签名渗透到业务代码。
2. **历史回补 + 交叉校验：Baostock**——非爬虫协议、稳定无频控，适合首次建库时拉全量历史，并在日常增量后抽查核对（两源收盘价不一致时告警）。
3. **最快兜底：东财裸接口**——AKShare 升级间隙若接口临时失效，直接调 push2his 保证当日增量不中断（记得带 `ut` 参数）。
4. **落库**：日线统一入 PostgreSQL（本项目已就绪），按 `code + date` 唯一键增量 upsert；复权方式建议**存不复权原始值 + 复权因子**，前复权值现算，避免除权后全表重刷。
5. **风险红线**：爬虫类源（东财系）随源站改版会临时失效，任何生产化抓取都要有「双源 + 重试 + 失败告警」，单源裸奔不可接受。

## 附录：实测命令（2026-09-29 全部通过/复现）

```bash
# 东财（注意 ut 参数，缺了返回空）
curl -H "User-Agent: Mozilla/5.0" "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=5"
# 腾讯（前复权日线）
curl "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600000,day,,,5,qfq"
# 新浪（日线，需 Referer）
curl -H "Referer: https://finance.sina.com.cn" "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh600000&scale=240&ma=no&datalen=5"
# 网易（已失效，实测 502）
curl -o /dev/null -w "%{http_code}\n" "http://quotes.money.163.com/service/chddata.html?code=0600000&start=20260101&end=20260929&fields=TCLOSE"
```

```bash
# Baostock 实测（注意：清华镜像装不到，用官方源）
python3 -m venv /tmp/bsvenv
/tmp/bsvenv/bin/pip install --index-url https://pypi.org/simple baostock
# 登录成功，sh.600000 取到 2026-09-22 ~ 09-29 共 5 根前复权日线（adjustflag=2）
```

### 复权参数速查

| 源 | 不复权 | 前复权 | 后复权 |
| --- | --- | --- | --- |
| 东财 / AKShare / efinance | `fqt=0` / `adjust=""` | `fqt=1` / `"qfq"` | `fqt=2` / `"hfq"` |
| 腾讯 | param 第 5 段留空 | `qfq` | `hfq` |
| 新浪 | 仅不复权 | — | — |
| Baostock | `adjustflag=3` | `adjustflag=2` | `adjustflag=1` |
| Tushare | `daily` 原始值 + `adj_factor` 自算 | — | — |

### 代码规则速查

| 源 | 沪市示例 | 深市示例 |
| --- | --- | --- |
| 东财 secid | `1.600000` | `0.000001` |
| 腾讯 / 新浪 | `sh600000` | `sz000001` |
| Baostock | `sh.600000` | `sz.000001` |
| Tushare | `600000.SH` | `000001.SZ` |
| 掘金 gm SDK | `SHSE.600000` | `SZSE.000001` |

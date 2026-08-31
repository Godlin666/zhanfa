# 港股 / 美股日线数据

每个交易日自动更新的港股、美股历史日线行情，存在阿里云 OSS（国内直连，不用翻墙）。

---

## 一、先看这个：数据是**不复权**的

价格是交易所当天的**原始成交价**，没有做除权除息调整。

这意味着：某只股票分红或拆股的当天，价格会凭空出现一个大跳空。如果你直接拿 `close`
算收益率，会把这个跳空误当成暴跌。

- 做**短期**（几天到几周）分析，影响很小，可以忽略
- 做**长期**收益率、回测、复利计算，请自行复权，或改用带复权的数据源

---

## 二、怎么拿数据

### 方式 A：浏览器直接点（最简单，适合看一眼）

这几个链接**不需要任何密码**，浏览器打开就能下：

| 链接 | 内容 |
|---|---|
| 链接 | 内容 | 大小 |
|---|---|---|
| `.../p/<PREFIX>/meta.json` | 更新时间、数据行数、日期范围 | 1 KB |
| `.../p/<PREFIX>/hk_recent90.zip` | 港股近期行情，24 万行 | 2.5 MB |
| `.../p/<PREFIX>/us_recent90.zip` | 美股近期行情，59 万行 | 8.1 MB |
| `.../p/<PREFIX>/symbols.csv` | 股票代码 ↔ 名称对照表 | 0.7 MB |

（完整地址是 `https://<BUCKET>.<ENDPOINT>/p/<PREFIX>/文件名`）

**不写代码**：下载 zip，双击解压出 CSV，用 Excel 打开即可。
行数都在 Excel 的 104 万行上限内，能完整装下。

**写代码**：zip 不用手动解压，pandas 一行直接读（已实测可用）：

```python
import pandas as pd
df = pd.read_csv("https://<BUCKET>.<ENDPOINT>/p/<PREFIX>/hk_recent90.zip",
                 dtype={"code": str})     # 加这个才不会把 00700 变成 700
```

### 方式 B：图形客户端（适合要全量数据、又不想写代码）

全量历史数据（两年、近 900 万行）放在需要密码的地方，用阿里云官方免费客户端取：

1. 下载 **ossbrowser**：<https://help.aliyun.com/zh/oss/developer-reference/ossbrowser-1>
   （Windows / macOS 都有）
2. 打开后填入下面这组信息，登录：

   ```
   AccessKeyId     : <找他要>
   AccessKeySecret : <找他要>
   Endpoint        : <ENDPOINT>
   Bucket          : <BUCKET>
   ```

3. 登录后进 `v1/full/` 目录，像用网盘一样双击下载。

### 方式 C：Python 脚本（适合要全量数据 + 写代码）

```bash
pip install oss2 pandas pyarrow
```

```python
import oss2, pandas as pd

bucket = oss2.Bucket(
    oss2.Auth("<AccessKeyId>", "<AccessKeySecret>"),
    "<ENDPOINT>", "<BUCKET>",
)

# 下载美股全量（Parquet 格式，读起来最快）
bucket.get_object_to_file("v1/full/us_daily.parquet", "us_daily.parquet")

df = pd.read_parquet("us_daily.parquet")
print(df.head())
print(len(df), "行")
```

---

## 三、有哪些文件

### 公开区 `p/<PREFIX>/` —— 不用密码

| 文件 | 内容 |
|---|---|
| `meta.json` | 元信息：更新时间、每个文件的行数和校验值 |
| `hk_recent90.zip` | 港股近期行情（24 万行，2.5 MB） |
| `us_recent90.zip` | 美股近期行情（59 万行，8.1 MB） |
| `symbols.csv` | 代码对照表（0.7 MB） |

> `recent90` 是**每只股票各自最后 90 条记录**，不是"最近 90 个日历日"。
> 因为各股停牌情况不同，整体日期跨度会略大于 90 个交易日。

### 全量区 `v1/full/` —— 需要密码

| 文件 | 内容 | 说明 |
|---|---|---|
| `hk_daily.csv.gz` | 港股全量，127 万行 | 12.2 MB，pandas 能直接读，不用解压 |
| `us_daily.csv.gz` | 美股全量，295 万行 | 40.6 MB，同上 |
| `hk_daily.parquet` | 港股全量，127 万行 | 11.0 MB，**推荐**，读取快得多 |
| `us_daily.parquet` | 美股全量，295 万行 | 36.0 MB，同上 |
| `symbols.csv` | 代码对照表 | 0.7 MB |
| `meta.json` | 元信息 | 1 KB |

> 全量文件行数远超 Excel 上限（美股 295 万行），Excel 打不开，只能用代码处理。
> 要用 Excel 请走上面公开区的 `recent90`。

---

## 四、字段说明

所有行情文件字段完全一致：

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | 文本 | 股票代码。港股是 5 位补零（`00700`），美股是交易所代码（`AAPL`） |
| `name` | 文本 | 公司名称 |
| `market` | 文本 | `HK` 或 `US` |
| `date` | 文本 | 交易日，`YYYY-MM-DD`，**当地交易日**（不是 UTC） |
| `open` | 数值 | 开盘价 |
| `high` | 数值 | 最高价 |
| `low` | 数值 | 最低价 |
| `close` | 数值 | 收盘价 |
| `volume` | 整数 | 成交量（股数） |

> ⚠️ 港股代码在 Excel 里会被吃掉前导零（`00700` 变成 `700`）。
> 导入时把该列指定为「文本」格式。

`symbols.csv` 额外字段：

| 字段 | 说明 |
|---|---|
| `yahoo_symbol` | Yahoo Finance 上的代码，如 `0700.HK`，方便自己去查更多信息 |
| `sector` / `industry` | 所属板块 / 行业。**大部分股票这两列是空的**，只有约 240 只有值 |
| `first_date` / `last_date` / `bars` | 该股票数据的起止日期和条数 |

---

## 五、更新时间

| 市场 | 更新时点 |
|---|---|
| 港股 | 每个交易日 **香港时间 16:45** 之后 |
| 美股 | 每个交易日 **北京时间次日 06:00** 之后 |

想确认拿到的是不是最新的，读 `meta.json` 里的 `generated_at`（UTC 时间）。

---

## 六、其他

- **数据来源**：Yahoo Finance，通过 `yfinance` 抓取
- **覆盖范围**：港股约 2700 只、美股约 6900 只，各约两年历史
- **数据质量**：不保证完整或准确。停牌、退市、代码变更等情况可能造成缺口，
  个别股票只有很少几条数据。自己用之前先看一眼 `symbols.csv` 里的 `bars` 列
- **成交量为 0** 的行通常是停牌日，此时开高低收四个价格往往相同，
  做统计时建议先过滤掉 `volume == 0` 的行
- **仅供个人研究参考，不构成任何投资建议**

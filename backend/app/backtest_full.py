"""全量回测（教学页）：同一策略对全市场股票一次性后台回测。

双表落库：backtest_full_run（任务元数据/进度/统计）+ backtest_full_row（每股指标行）。
执行模型：FastAPI 进程内 asyncio 任务——按批（CHUNK 只/批）加载行情，逐股复用
单股引擎与复权懒加载；互斥 = 同刻仅允许一个 running 任务；取消 = 进程内标志位；
后端重启时 running 旧任务标 interrupted（可重跑）。

模块级 store 函数 + `_connect` 均可 monkeypatch（测试离线替换）。
"""

import asyncio
import datetime as dt
import json

import psycopg

from . import config
from .backtest.data import load_history
from .backtest.runner import run_strategy

DEFAULT_FILTERS = {"exclude_st": True, "min_bars": 60,
                   "include_delisted": True}
CHUNK = 300             # 每批加载的股票数（控制单条 SQL 与内存占用）
SORTABLE = ("total_return", "annual_return", "max_drawdown", "sharpe",
            "win_rate", "trades", "code")

_RANGE_DAYS = {"1y": 365, "3y": 365 * 3, "5y": 365 * 5}   # 同 backtest_api

_CANCEL_FLAGS: set[int] = set()     # run_id → 请求取消（进程内）
_TASKS: dict[int, asyncio.Task] = {}  # run_id → 后台任务句柄（防引用丢失）

_DDL = """
CREATE TABLE IF NOT EXISTS backtest_full_run (
  id SERIAL PRIMARY KEY,
  strategy TEXT NOT NULL,
  range_key TEXT NOT NULL,
  params JSONB NOT NULL DEFAULT '{}',
  exec_mode TEXT NOT NULL DEFAULT 'next_open',
  commission DOUBLE PRECISION NOT NULL DEFAULT 2.5e-4,
  sell_tax DOUBLE PRECISION NOT NULL DEFAULT 5e-4,
  adjust BOOLEAN NOT NULL DEFAULT TRUE,
  filters JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  total INT NOT NULL DEFAULT 0,
  skipped INT NOT NULL DEFAULT 0,
  done INT NOT NULL DEFAULT 0,
  failed INT NOT NULL DEFAULT 0,
  stats JSONB,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS backtest_full_row (
  run_id INT NOT NULL REFERENCES backtest_full_run(id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  total_return DOUBLE PRECISION,
  annual_return DOUBLE PRECISION,
  max_drawdown DOUBLE PRECISION,
  sharpe DOUBLE PRECISION,
  win_rate DOUBLE PRECISION,
  trades INT,
  buyhold_return DOUBLE PRECISION,
  reason TEXT,
  PRIMARY KEY (run_id, code)
);
CREATE INDEX IF NOT EXISTS idx_bf_row_run ON backtest_full_row(run_id);
"""


def _connect():
    return psycopg.connect(config.DATABASE_URL)


# ------------------------------------------------------------ 纯函数 ----

def filter_universe(stocks: list[dict], bars_counts: dict, *,
                    exclude_st: bool = True, min_bars: int = 60,
                    include_delisted: bool = True):
    """全市场清单过滤：剔 ST/退（按名称，仅作用在市股）+ 区间内最少 K 线数
    + 退市股开关（include_delisted=False 回到纯在市口径）。

    返回 (kept, skipped)。名称含 ST（含 *ST）或「退」的**在市**股视为风险股
    剔除——退市股是修幸存者偏差的本体，不受该过滤影响；K 线数不足 min_bars
    的次新股/长期停牌股剔除。
    """
    kept = []
    for s in stocks:
        if not include_delisted and s.get("delisted"):
            continue
        name = s.get("name") or ""
        if exclude_st and not s.get("delisted") and (
                "ST" in name.upper() or "退" in name):
            continue
        if bars_counts.get(s["code"], 0) < min_bars:
            continue
        kept.append(s)
    return kept, len(stocks) - len(kept)


def compute_stats(rows: list[dict]) -> dict | None:
    """统计卡：参与数 / 正收益占比 / 总收益中位数 / 跑赢买入持有占比
    + 最好/最差收益 + 退市股单独总结（修幸存者偏差视角的对照组）。"""
    ok = [r for r in rows
          if r.get("status") == "ok" and r.get("total_return") is not None]
    if not ok:
        return None

    def _group_stats(sub: list[dict]) -> dict | None:
        if not sub:
            return None
        rets = sorted(r["total_return"] for r in sub)
        n = len(rets)
        median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
        beat = [r for r in sub if r.get("buyhold_return") is not None]
        return {
            "count": n,
            "positive_pct": sum(1 for x in rets if x > 0) / n,
            "median_return": median,
            "best_return": rets[-1],
            "worst_return": rets[0],
            "beat_buyhold_pct": (sum(1 for r in beat
                                     if r["total_return"] > r["buyhold_return"])
                                 / len(beat)) if beat else None,
        }

    overall = _group_stats(ok)
    delisted_rows = [r for r in ok if r.get("delisted")]
    overall["delisted"] = _group_stats(delisted_rows)
    overall["delisted_failed"] = sum(
        1 for r in rows if r.get("delisted") and r.get("status") != "ok")
    return overall


def row_from_result(code: str, name: str, result: dict) -> dict:
    """单股引擎结果 → backtest_full_row 行（message 空态映射为 failed+reason）。"""
    row = {"code": code, "name": name, "status": "ok", "total_return": None,
           "annual_return": None, "max_drawdown": None, "sharpe": None,
           "win_rate": None, "trades": None, "buyhold_return": None,
           "reason": None}
    if result.get("message"):
        return {**row, "status": "failed", "reason": result["message"]}
    m = result.get("metrics") or {}
    equity = result.get("equity") or []
    row.update({
        "total_return": m.get("total_return"),
        "annual_return": m.get("annual_return"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe": m.get("sharpe"),
        "win_rate": m.get("win_rate"),
        "trades": m.get("trades"),
        "buyhold_return": (equity[-1][2] - 1.0)
        if equity and len(equity[-1]) > 2 and equity[-1][2] is not None else None,
    })
    return row


# --------------------------------------------------------- store 层 ----

def ensure_schema(conn) -> None:
    conn.cursor().execute(_DDL)


def mark_interrupted_on_startup() -> None:
    """后端重启后把遗留的 running 任务标 interrupted（进程已死，任务不可能还在）。"""
    with _connect() as conn:
        conn.cursor().execute(
            "UPDATE backtest_full_run SET status='interrupted', "
            "finished_at=now() WHERE status IN ('running', 'canceling')")
        conn.commit()


def has_running_run() -> bool:
    with _connect() as conn:
        row = conn.cursor().execute(
            "SELECT 1 FROM backtest_full_run WHERE status='running' LIMIT 1"
        ).fetchone()
    return row is not None


def create_run(spec: dict) -> int:
    with _connect() as conn:
        cur = conn.cursor().execute(
            """INSERT INTO backtest_full_run
               (strategy, range_key, params, exec_mode, commission, sell_tax,
                adjust, filters, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running') RETURNING id""",
            (spec["strategy"], spec["range"], json.dumps(spec["params"]),
             spec["exec_mode"], spec["commission"], spec["sell_tax"],
             spec["adjust"], json.dumps(spec["filters"])))
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def get_run(run_id: int) -> dict | None:
    with _connect() as conn:
        cur = conn.cursor().execute(
            "SELECT id, strategy, range_key, params, exec_mode, commission,"
            " sell_tax, adjust, filters, status, total, skipped, done, failed,"
            " stats, error, created_at, finished_at"
            " FROM backtest_full_run WHERE id=%s", (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("id", "strategy", "range", "params", "exec_mode", "commission",
            "sell_tax", "adjust", "filters", "status", "total", "skipped",
            "done", "failed", "stats", "error", "created_at", "finished_at")
    d = dict(zip(keys, row))
    for k in ("params", "filters", "stats"):
        d[k] = json.loads(d[k]) if isinstance(d[k], str) else d[k]
    return d


def list_runs() -> list[dict]:
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT id, strategy, range_key, status, total, skipped, done,"
            " failed, stats, error, created_at, finished_at"
            " FROM backtest_full_run ORDER BY id DESC LIMIT 100").fetchall()
    keys = ("id", "strategy", "range", "status", "total", "skipped", "done",
            "failed", "stats", "error", "created_at", "finished_at")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        if isinstance(d["stats"], str):
            d["stats"] = json.loads(d["stats"])
        out.append(d)
    return out


def set_run_totals(run_id: int, total: int, skipped: int) -> None:
    with _connect() as conn:
        conn.cursor().execute(
            "UPDATE backtest_full_run SET total=%s, skipped=%s WHERE id=%s",
            (total, skipped, run_id))
        conn.commit()


def update_progress(run_id: int, done: int, failed: int) -> None:
    with _connect() as conn:
        conn.cursor().execute(
            "UPDATE backtest_full_run SET done=%s, failed=%s WHERE id=%s",
            (done, failed, run_id))
        conn.commit()


def insert_row(run_id: int, row: dict) -> None:
    with _connect() as conn:
        conn.cursor().execute(
            """INSERT INTO backtest_full_row
               (run_id, code, name, status, total_return, annual_return,
                max_drawdown, sharpe, win_rate, trades, buyhold_return, reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, code) DO UPDATE SET
                 name=EXCLUDED.name, status=EXCLUDED.status,
                 total_return=EXCLUDED.total_return,
                 annual_return=EXCLUDED.annual_return,
                 max_drawdown=EXCLUDED.max_drawdown,
                 sharpe=EXCLUDED.sharpe, win_rate=EXCLUDED.win_rate,
                 trades=EXCLUDED.trades,
                 buyhold_return=EXCLUDED.buyhold_return,
                 reason=EXCLUDED.reason""",
            (run_id, row["code"], row["name"], row["status"],
             row["total_return"], row["annual_return"], row["max_drawdown"],
             row["sharpe"], row["win_rate"], row["trades"],
             row["buyhold_return"], row["reason"]))
        conn.commit()


def finish_run(run_id: int, status: str,
               stats: dict | None = None, error: str | None = None) -> None:
    with _connect() as conn:
        conn.cursor().execute(
            "UPDATE backtest_full_run SET status=%s, stats=%s, error=%s,"
            " finished_at=now() WHERE id=%s",
            (status, json.dumps(stats) if stats is not None else None,
             error, run_id))
        conn.commit()
    _CANCEL_FLAGS.discard(run_id)


def request_cancel(run_id: int) -> bool:
    if get_run(run_id) is None:
        return False
    _CANCEL_FLAGS.add(run_id)
    return True


DELISTED_MODES = ("all", "only", "exclude")     # 退市股分组过滤白名单


def get_rows(run_id: int, sort: str = "total_return", desc: bool = True,
             page: int = 0, size: int = 50, delisted: str = "all") -> dict:
    """明细分页（列名白名单防注入）+ 该口径的部分统计。

    delisted：all=全部；only=仅退市股；exclude=仅在市股（join stock_basic）。
    退市股总结区块与主排行表共用本接口，各自带口径统计。
    """
    if sort not in SORTABLE:
        sort = "total_return"
    order = (f"{sort} DESC NULLS LAST" if desc
             else f"{sort} ASC NULLS LAST")
    if sort != "code":
        order += ", code ASC"
    join = ("JOIN stock_basic s ON s.code = r.code"
            if delisted in ("only", "exclude") else "")
    delist_where = {"only": " AND s.delisted = TRUE",
                    "exclude": " AND s.delisted = FALSE"}.get(delisted, "")
    with _connect() as conn:
        cur = conn.cursor()
        total = cur.execute(
            f"SELECT count(*) FROM backtest_full_row r {join} "
            f"WHERE r.run_id=%s{delist_where}", (run_id,)).fetchone()[0]
        rows = cur.execute(
            f"""SELECT r.code, r.name, r.status, r.total_return,
                       r.annual_return, r.max_drawdown, r.sharpe,
                       r.win_rate, r.trades, r.buyhold_return, r.reason
                FROM backtest_full_row r {join} WHERE r.run_id=%s{delist_where}
                ORDER BY {order} LIMIT %s OFFSET %s""",
            (run_id, size, page * size)).fetchall()
    keys = ("code", "name", "status", "total_return", "annual_return",
            "max_drawdown", "sharpe", "win_rate", "trades", "buyhold_return",
            "reason")
    return {"total": total,
            "rows": [dict(zip(keys, r)) for r in rows],
            "stats": compute_stats(ok_rows(run_id, delisted))}


def ok_rows(run_id: int) -> list[dict]:
    """统计用的成功行（收益两列 + 退市标记，退市股单独总结用）。"""
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT r.status, r.total_return, r.buyhold_return,"
            " COALESCE(b.delisted, FALSE)"
            " FROM backtest_full_row r"
            " LEFT JOIN stock_basic b ON b.code = r.code"
            " WHERE r.run_id=%s", (run_id,)).fetchall()
    return [{"status": r[0], "total_return": r[1], "buyhold_return": r[2],
             "delisted": bool(r[3])} for r in rows]


def top_codes(run_id: int, n: int) -> list[dict]:
    """收益排行前 n 的 {code, name}（仅成功行）。"""
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT code, name FROM backtest_full_row"
            " WHERE run_id=%s AND status='ok'"
            " ORDER BY total_return DESC NULLS LAST, code ASC LIMIT %s",
            (run_id, n)).fetchall()
    return [{"code": r[0], "name": r[1]} for r in rows]


def export_csv(run_id: int) -> str:
    """整 run 明细导出为 CSV 文本（UTF-8，含表头；失败行含原因列）。"""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["code", "name", "status", "total_return",
                     "annual_return", "max_drawdown", "sharpe", "win_rate",
                     "trades", "buyhold_return", "reason"])
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT code, name, status, total_return, annual_return,"
            " max_drawdown, sharpe, win_rate, trades, buyhold_return, reason"
            " FROM backtest_full_row WHERE run_id=%s ORDER BY code",
            (run_id,)).fetchall()
    for r in rows:
        writer.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


# --------------------------------------------------------- worker ----

def start_run_job(run_id: int, spec: dict) -> None:
    """创建后台任务（进程内 asyncio；句柄存模块字典防被 GC）。

    必须在事件循环内调用（POST 路由为 async def）。
    """
    _TASKS[run_id] = asyncio.create_task(execute_run(run_id, spec))


def _canceled(run_id: int) -> bool:
    return run_id in _CANCEL_FLAGS


def _range_start(range_key: str) -> str | None:
    today = dt.date.today()
    return (today - dt.timedelta(days=_RANGE_DAYS[range_key])).isoformat() \
        if range_key != "all" else None


def _load_universe(run_id: int, spec: dict) -> tuple[list[dict], int]:
    """全市场清单 + 区间内 K 线数 → 过滤后的 universe（同时落库 total/skipped）。"""
    start = _range_start(spec["range"])
    with _connect() as conn:
        stocks = conn.cursor().execute(
            "SELECT code, name FROM stock_basic ORDER BY code").fetchall()
        # 注意不可写 `(%s IS NULL OR trade_date >= %s)`：psycopg3 服务端绑定下
        # $1 仅出现在 IS NULL 位，Postgres 推断不出类型（IndeterminateDatatype）
        cur = conn.cursor()
        if start is None:
            counts = dict(cur.execute(
                "SELECT code, count(DISTINCT trade_date) FROM daily_bar"
                " GROUP BY code").fetchall())
        else:
            counts = dict(cur.execute(
                "SELECT code, count(DISTINCT trade_date) FROM daily_bar"
                " WHERE trade_date >= %s GROUP BY code", (start,)))
    filters = spec.get("filters") or {}
    include_delisted = bool(filters.get("include_delisted", True))
    with _connect() as conn:
        cur = conn.cursor()
        # 参数化 NULL 不可用（IndeterminateDatatype），按分支拼装
        if include_delisted:
            rows = cur.execute(
                "SELECT code, name, delisted FROM stock_basic "
                "ORDER BY code").fetchall()
        else:
            rows = cur.execute(
                "SELECT code, name, delisted FROM stock_basic "
                "WHERE delisted = FALSE ORDER BY code").fetchall()
        stocks = [{"code": c, "name": n, "delisted": bool(d)}
                  for c, n, d in rows]
        if start is None:
            counts = dict(cur.execute(
                "SELECT code, count(DISTINCT trade_date) FROM daily_bar"
                " GROUP BY code").fetchall())
        else:
            counts = dict(cur.execute(
                "SELECT code, count(DISTINCT trade_date) FROM daily_bar"
                " WHERE trade_date >= %s GROUP BY code", (start,)))
    kept, skipped = filter_universe(
        stocks, counts,
        exclude_st=bool(filters.get("exclude_st", True)),
        min_bars=int(filters.get("min_bars", 60)),
        include_delisted=include_delisted)
    set_run_totals(run_id, total=len(kept), skipped=skipped)
    return kept, skipped


def _run_one_stock(code: str, name: str, pack: dict, spec: dict) -> dict:
    """单股回测：从批数据包中切出该股，复用单股引擎（语义同 POST /api/backtest）。"""
    bars = pack.get("bars") or {}
    close = bars.get("close")
    # 注意不可写 `(bars.get("close") or {}).columns`：close 是 DataFrame，
    # 对其求真值直接 ValueError ambiguous
    if close is None or code not in close.columns:
        return {"code": code, "name": name, "status": "failed",
                "total_return": None, "annual_return": None,
                "max_drawdown": None, "sharpe": None, "win_rate": None,
                "trades": None, "buyhold_return": None,
                "reason": "区间内无行情数据"}
    single = {k: df[[code]] for k, df in bars.items()}
    result = run_strategy(
        single, strategy=spec["strategy"], params=spec.get("params") or {},
        exec_mode=spec.get("exec_mode", "next_open"),
        commission=spec.get("commission", 2.5e-4),
        sell_tax=spec.get("sell_tax", 5e-4),
        factors=pack.get("factors"))
    return row_from_result(code, name, result)


def _load_bars_chunk(codes: list[str], spec: dict) -> dict:
    """加载一批股票的行情（宽表 OHLC），adjust 开启时懒加载因子合成后复权。"""
    start = _range_start(spec["range"])
    bars = load_history(config.DATABASE_URL, codes,
                        start=start, end=dt.date.today().isoformat())
    if not bars:
        return {"bars": {}, "factors": None}
    if spec.get("adjust", True):
        from .adjust import ensure_and_apply
        try:
            bars, factors = ensure_and_apply(bars, codes, config.DATABASE_URL)
        except Exception:                       # 复权链路异常：降级不复权
            factors = None
        return {"bars": bars, "factors": factors}
    return {"bars": bars, "factors": None}


async def execute_run(run_id: int, spec: dict) -> None:
    """后台任务主体：过滤 → 分批加载 → 逐股回测落库 → 统计收尾。

    逐股 await asyncio.sleep(0) 让出事件循环；异常兜底置 failed 终态。
    """
    try:
        universe, skipped = await asyncio.to_thread(_load_universe, run_id, spec)
        done = failed = 0
        ok_rows_acc: list[dict] = []
        for i in range(0, len(universe), CHUNK):
            chunk = universe[i:i + CHUNK]
            pack = await asyncio.to_thread(
                _load_bars_chunk, [s["code"] for s in chunk], spec)
            for s in chunk:
                if _canceled(run_id):
                    finish_run(run_id, "canceled")
                    return
                try:
                    row = await asyncio.to_thread(
                        _run_one_stock, s["code"], s["name"], pack, spec)
                except Exception as exc:        # 单股异常不阻塞其余
                    row = {"code": s["code"], "name": s["name"],
                           "status": "failed", "total_return": None,
                           "annual_return": None, "max_drawdown": None,
                           "sharpe": None, "win_rate": None, "trades": None,
                           "buyhold_return": None,
                           "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
                await asyncio.to_thread(insert_row, run_id, row)
                done += 1
                if row["status"] == "failed":
                    failed += 1
                else:
                    ok_rows_acc.append(row)
                await asyncio.to_thread(update_progress, run_id, done, failed)
                await asyncio.sleep(0)
            await asyncio.sleep(0)
        if _canceled(run_id):
            finish_run(run_id, "canceled")
            return
        finish_run(run_id, "done", stats=compute_stats(ok_rows_acc))
    except Exception as exc:                    # 清单/批量加载等整体性失败
        finish_run(run_id, "failed",
                   error=f"{type(exc).__name__}: {str(exc)[:200]}")

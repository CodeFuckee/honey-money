"""全量回测（教学页）：同一策略对全市场股票一次性后台回测。

双表落库：backtest_full_run（任务元数据/进度/统计）+ backtest_full_row（每股指标行）。
执行模型：FastAPI 进程内 asyncio 任务——按批（CHUNK 只/批）加载行情，逐股复用
单股引擎与复权懒加载；互斥 = 同刻仅允许一个 running 任务；取消 = 进程内标志位；
后端重启时 running 旧任务标 interrupted（可重跑）。

模块级 store 函数 + `_connect` 均可 monkeypatch（测试离线替换）。
"""

import asyncio
import datetime as dt
import hashlib
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
  spec_hash TEXT,
  reuse_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  reused_from INT,
  reused_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS backtest_full_row (
  run_id INT NOT NULL REFERENCES backtest_full_run(id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  equity_snapshot JSONB,
  total_return DOUBLE PRECISION,
  annual_return DOUBLE PRECISION,
  max_drawdown DOUBLE PRECISION,
  sharpe DOUBLE PRECISION,
  win_rate DOUBLE PRECISION,
  trades INT,
  buyhold_return DOUBLE PRECISION,
  reason TEXT,
  source_run_id INT,
  PRIMARY KEY (run_id, code)
);
CREATE INDEX IF NOT EXISTS idx_bf_row_run ON backtest_full_row(run_id);
"""


def _connect():
    return psycopg.connect(config.DATABASE_URL)


# ------------------------------------------------------------ 纯函数 ----

def spec_hash(spec: dict) -> str:
    """同参任务的复用指纹：执行语义字段规范化后取 sha256 前 16 位。

    只含影响结果的字段（reuse 开关是执行策略，不影响指纹）。
    """
    payload = {k: spec.get(k) for k in
               ("strategy", "range", "params", "exec_mode", "commission",
                "sell_tax", "adjust", "filters")}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]

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


def row_from_result(code: str, name: str, result: dict,
                    snapshot_points: int = 120) -> dict:
    """单股引擎结果 → backtest_full_row 行（message 空态映射为 failed+reason）。"""
    row = {"code": code, "name": name, "status": "ok", "total_return": None,
           "annual_return": None, "max_drawdown": None, "sharpe": None,
           "win_rate": None, "trades": None, "buyhold_return": None,
           "reason": None, "equity_snapshot": None}
    if result.get("message"):
        return {**row, "status": "failed", "reason": result["message"]}
    m = result.get("metrics") or {}
    equity = result.get("equity") or []
    # 曲线快照：等距降采样并强制保留首尾点（全部股票折线图的数据源；
    # 5000+ 只不可能查看时重算回测，必须在运行时落库）
    snap = None
    if equity:
        n = len(equity)
        if n <= snapshot_points:
            picked = equity
        else:
            idx = list(dict.fromkeys(
                round(i * (n - 1) / (snapshot_points - 1))
                for i in range(snapshot_points)))
            picked = [equity[i] for i in idx]
        snap = [[p[0], round(float(p[1]), 6)] for p in picked]
    row.update({
        "equity_snapshot": snap,
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
    # 老库补列：曲线快照（全部股票折线图，2026-10-13）
    conn.cursor().execute("ALTER TABLE backtest_full_row "
                          "ADD COLUMN IF NOT EXISTS equity_snapshot JSONB")
    # 老库补列：同参结果复用（2026-10-14）——run 存指纹与复用来源，行存溯源
    conn.cursor().execute("ALTER TABLE backtest_full_run "
                          "ADD COLUMN IF NOT EXISTS spec_hash TEXT")
    conn.cursor().execute("ALTER TABLE backtest_full_run "
                          "ADD COLUMN IF NOT EXISTS reuse_enabled "
                          "BOOLEAN NOT NULL DEFAULT TRUE")
    conn.cursor().execute("ALTER TABLE backtest_full_run "
                          "ADD COLUMN IF NOT EXISTS reused_from INT")
    conn.cursor().execute("ALTER TABLE backtest_full_run "
                          "ADD COLUMN IF NOT EXISTS reused_count "
                          "INT NOT NULL DEFAULT 0")
    conn.cursor().execute("ALTER TABLE backtest_full_row "
                          "ADD COLUMN IF NOT EXISTS source_run_id INT")
    # 指纹索引必须在补列之后建（老库 _DDL 阶段 spec_hash 尚不存在，
    # 整批 DDL 会失败——曾致 ensure_schema 整体跳过）
    conn.cursor().execute("CREATE INDEX IF NOT EXISTS idx_bf_run_spec "
                          "ON backtest_full_run(spec_hash, status)")
    _backfill_spec_hashes(conn)


def _backfill_spec_hashes(conn) -> None:
    """老 run 指纹回填（幂等）：历史 run 无 spec_hash 时由落库参数现算，
    使其可被后续同参任务复用；新 run 由 create_run 写入，不进这里。"""
    rows = conn.cursor().execute(
        "SELECT id, strategy, range_key, params, exec_mode, commission,"
        " sell_tax, adjust, filters FROM backtest_full_run"
        " WHERE spec_hash IS NULL").fetchall()
    for (rid, strategy, range_key, params, exec_mode, commission,
         sell_tax, adjust, filters) in rows:
        spec = {"strategy": strategy, "range": range_key,
                "params": params if isinstance(params, dict)
                          else json.loads(params or "{}"),
                "exec_mode": exec_mode, "commission": commission,
                "sell_tax": sell_tax, "adjust": adjust,
                "filters": filters if isinstance(filters, dict)
                           else json.loads(filters or "{}")}
        conn.cursor().execute(
            "UPDATE backtest_full_run SET spec_hash=%s WHERE id=%s",
            (spec_hash(spec), rid))


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


# ------------------------------------------------------- 结果复用 ----

def find_reusable_run(spec_hash_: str, exclude_run_id: int) -> dict | None:
    """最近一个同指纹且完整完成的 run（复用来源候选；排除自身）。"""
    with _connect() as conn:
        row = conn.cursor().execute(
            "SELECT id, created_at FROM backtest_full_run"
            " WHERE spec_hash=%s AND status='done' AND id<>%s"
            " ORDER BY id DESC LIMIT 1",
            (spec_hash_, exclude_run_id)).fetchone()
    return {"id": row[0], "created_at": row[1]} if row else None


def reusable_ok_codes(src_run_id: int) -> list[str]:
    """源 run 中可复用的成功股清单（失败股不在内——复用时逐股重跑）。"""
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT code FROM backtest_full_row"
            " WHERE run_id=%s AND status='ok'", (src_run_id,)).fetchall()
    return [r[0] for r in rows]


def copy_ok_rows(src_run_id: int, dst_run_id: int, codes: list[str]) -> int:
    """源 run 成功行整批复制进新 run（含曲线快照；纯服务端 SQL 不经应用层）。

    仅复制 codes 清单内的行——universe 随 stock_basic/行情演变，复用必须以
    **当前**过滤结果为准（新上市股不复制、已跌出过滤的股不残留）；
    名称取 stock_basic 现值（改名/摘牌标记跟随最新）。返回复制行数。
    """
    if not codes:
        return 0
    with _connect() as conn:
        cur = conn.cursor().execute(
            """INSERT INTO backtest_full_row
               (run_id, code, name, status, equity_snapshot, total_return,
                annual_return, max_drawdown, sharpe, win_rate, trades,
                buyhold_return, reason, source_run_id)
               SELECT %s, r.code, COALESCE(b.name, r.name), r.status,
                      r.equity_snapshot, r.total_return, r.annual_return,
                      r.max_drawdown, r.sharpe, r.win_rate, r.trades,
                      r.buyhold_return, r.reason, %s
               FROM backtest_full_row r
               LEFT JOIN stock_basic b ON b.code = r.code
               WHERE r.run_id=%s AND r.status='ok' AND r.code = ANY(%s)
               ON CONFLICT (run_id, code) DO NOTHING""",
            (dst_run_id, src_run_id, src_run_id, codes))
        n = cur.rowcount
        conn.commit()
    return n if n and n > 0 else 0


def mark_reuse(run_id: int, src_run_id: int, count: int) -> None:
    with _connect() as conn:
        conn.cursor().execute(
            "UPDATE backtest_full_run SET reused_from=%s, reused_count=%s"
            " WHERE id=%s", (src_run_id, count, run_id))
        conn.commit()


def create_run(spec: dict) -> int:
    with _connect() as conn:
        cur = conn.cursor().execute(
            """INSERT INTO backtest_full_run
               (strategy, range_key, params, exec_mode, commission, sell_tax,
                adjust, filters, status, spec_hash, reuse_enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running',%s,%s) RETURNING id""",
            (spec["strategy"], spec["range"], json.dumps(spec["params"]),
             spec["exec_mode"], spec["commission"], spec["sell_tax"],
             spec["adjust"], json.dumps(spec["filters"]),
             spec_hash(spec), bool(spec.get("reuse", True))))
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def get_run(run_id: int) -> dict | None:
    with _connect() as conn:
        cur = conn.cursor().execute(
            "SELECT id, strategy, range_key, params, exec_mode, commission,"
            " sell_tax, adjust, filters, status, total, skipped, done, failed,"
            " stats, error, created_at, finished_at, reused_from, reused_count"
            " FROM backtest_full_run WHERE id=%s", (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("id", "strategy", "range", "params", "exec_mode", "commission",
            "sell_tax", "adjust", "filters", "status", "total", "skipped",
            "done", "failed", "stats", "error", "created_at", "finished_at",
            "reused_from", "reused_count")
    d = dict(zip(keys, row))
    for k in ("params", "filters", "stats"):
        d[k] = json.loads(d[k]) if isinstance(d[k], str) else d[k]
    return d


def list_runs() -> list[dict]:
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT id, strategy, range_key, status, total, skipped, done,"
            " failed, stats, error, created_at, finished_at,"
            " reused_from, reused_count"
            " FROM backtest_full_run ORDER BY id DESC LIMIT 100").fetchall()
    keys = ("id", "strategy", "range", "status", "total", "skipped", "done",
            "failed", "stats", "error", "created_at", "finished_at",
            "reused_from", "reused_count")
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
        import json as _json
        snap = row.get("equity_snapshot")
        conn.cursor().execute(
            """INSERT INTO backtest_full_row
               (run_id, code, name, status, total_return, annual_return,
                max_drawdown, sharpe, win_rate, trades, buyhold_return, reason,
                equity_snapshot)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, code) DO UPDATE SET
                 name=EXCLUDED.name, status=EXCLUDED.status,
                 total_return=EXCLUDED.total_return,
                 annual_return=EXCLUDED.annual_return,
                 max_drawdown=EXCLUDED.max_drawdown,
                 sharpe=EXCLUDED.sharpe, win_rate=EXCLUDED.win_rate,
                 trades=EXCLUDED.trades,
                 buyhold_return=EXCLUDED.buyhold_return,
                 reason=EXCLUDED.reason,
                 equity_snapshot=EXCLUDED.equity_snapshot""",
            (run_id, row["code"], row["name"], row["status"],
             row["total_return"], row["annual_return"], row["max_drawdown"],
             row["sharpe"], row["win_rate"], row["trades"],
             row["buyhold_return"], row["reason"],
             _json.dumps(snap) if snap is not None else None))
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


def ok_rows(run_id: int, delisted: str = "all") -> list[dict]:
    """统计用的成功行（收益两列 + 退市标记），支持退市股分组口径
    （get_rows 的分组统计与本函数必须同参——两处并发演进曾签名分裂）。"""
    delist_where = {"only": " AND COALESCE(b.delisted, FALSE) = TRUE",
                    "exclude": " AND COALESCE(b.delisted, FALSE) = FALSE"
                    }.get(delisted, "")
    with _connect() as conn:
        rows = conn.cursor().execute(
            "SELECT r.status, r.total_return, r.buyhold_return,"
            " COALESCE(b.delisted, FALSE)"
            " FROM backtest_full_row r"
            " LEFT JOIN stock_basic b ON b.code = r.code"
            " WHERE r.run_id=%s" + delist_where, (run_id,)).fetchall()
    return [{"status": r[0], "total_return": r[1], "buyhold_return": r[2],
             "delisted": bool(r[3])} for r in rows]


def get_curves(run_id: int, delisted: str = "all") -> list[dict]:
    """全部股票曲线快照直读（折线图显示所有股票的数据源，不重算回测）。"""
    delist_where = {"only": " AND COALESCE(b.delisted, FALSE) = TRUE",
                    "exclude": " AND COALESCE(b.delisted, FALSE) = FALSE"
                    }.get(delisted, "")
    join = ("JOIN stock_basic b ON b.code = r.code"
            if delisted != "all" else
            "LEFT JOIN stock_basic b ON b.code = r.code")
    with _connect() as conn:
        rows = conn.cursor().execute(
            f"""SELECT r.code, r.name, r.equity_snapshot
                FROM backtest_full_row r {join}
                WHERE r.run_id=%s AND r.equity_snapshot IS NOT NULL
                      {delist_where}
                ORDER BY r.code""", (run_id,)).fetchall()
    import json as _json
    return [{"code": r[0], "name": r[1],
             "equity": _json.loads(r[2]) if isinstance(r[2], str) else r[2]}
            for r in rows]


def top_codes(run_id: int, n: int, delisted: str = "all") -> list[dict]:
    """收益排行前 n 的 {code, name}（仅成功行），支持退市股分组口径。"""
    join = ("JOIN stock_basic s ON s.code = r.code"
            if delisted in ("only", "exclude") else "")
    delist_where = {"only": " AND s.delisted = TRUE",
                    "exclude": " AND s.delisted = FALSE"}.get(delisted, "")
    with _connect() as conn:
        rows = conn.cursor().execute(
            f"""SELECT r.code, r.name FROM backtest_full_row r {join}
                WHERE r.run_id=%s AND r.status='ok'{delist_where}
                ORDER BY r.total_return DESC NULLS LAST, r.code ASC LIMIT %s""",
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
    """后台任务主体：过滤 → （可选复用）→ 分批加载 → 逐股回测落库 → 统计收尾。

    复用：同指纹且已完成的 run，其成功行整批 SQL 复制（含曲线快照），
    仅重算其失败股 + 新进 universe 的股票——重复运算量从全市场缩到增量。
    逐股 await asyncio.sleep(0) 让出事件循环；异常兜底置 failed 终态。
    """
    try:
        universe, skipped = await asyncio.to_thread(_load_universe, run_id, spec)
        done = failed = 0
        pending = universe
        if spec.get("reuse", True):
            src = await asyncio.to_thread(
                find_reusable_run, spec_hash(spec), run_id)
            if src:
                ok_codes = set(await asyncio.to_thread(
                    reusable_ok_codes, src["id"]))
                targets = [s["code"] for s in universe if s["code"] in ok_codes]
                copied = await asyncio.to_thread(
                    copy_ok_rows, src["id"], run_id, targets)
                if copied:
                    done = copied
                    await asyncio.to_thread(mark_reuse, run_id, src["id"], copied)
                    target_set = set(targets)
                    pending = [s for s in universe
                               if s["code"] not in target_set]
        for i in range(0, len(pending), CHUNK):
            chunk = pending[i:i + CHUNK]
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
                await asyncio.to_thread(update_progress, run_id, done, failed)
                await asyncio.sleep(0)
            await asyncio.sleep(0)
        if _canceled(run_id):
            finish_run(run_id, "canceled")
            return
        # 统计终值直接聚合落库行（含复制的行；ok_rows 与分组统计同参）
        stats = compute_stats(await asyncio.to_thread(ok_rows, run_id))
        finish_run(run_id, "done", stats=stats)
    except Exception as exc:                    # 清单/批量加载等整体性失败
        finish_run(run_id, "failed",
                   error=f"{type(exc).__name__}: {str(exc)[:200]}")

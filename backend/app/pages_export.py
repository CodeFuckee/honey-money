"""GitHub Pages 静态快照导出（供定投回测日更顺带发布公开页）。

结构与后端 API 响应一致（前端 VITE_STATIC=1 构建时改读这些文件）：
  site-data/funds/dca.json                    排行全量行 + stats + meta
  site-data/funds/dca/scatter.{fee}.json      散点+分箱（fee ∈ gross/disc/nom）
  site-data/funds/dca/rounds/{code}.json      单基金三口径轮次 + totals
  site-data/funds/dca/navs/{code}.json        单基金复权净值折线（弹窗图）

`export_all()` 被 pages_publish.publish()（回测日更后的自动发布）与
scripts/export_static_api.py（手动导出驱动）共用。
"""

import json
from datetime import timedelta
from pathlib import Path

import psycopg  # noqa: F401  （占位：调用方负责连接；本模块只做导出编排）

from .dca_api import fetch_dca_list, fetch_dca_scatter

PAGE = 200
FEES = ("gross", "disc", "nom")

NAV_WINDOW_YEARS = 10        # 弹窗折线窗口：近 10 年（与回测 w10 同口径）
NAV_TARGET_POINTS = 300      # 折线降采样目标点数（950px 宽弹窗图足够平滑）
NAV_VALUE_SCALE = 10_000     # 复权净值 ×1e4 取整（4 位小数精度，前端还原）


def _write_json(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


def export_list(dsn: str, out_dir: Path, *, log=print) -> dict:
    """排行全量行（翻页拉全）+ 首页的 stats/meta（全局口径）。"""
    rows, stats, meta, total = [], None, None, 0
    offset = 0
    while True:
        body = fetch_dca_list(dsn, limit=PAGE, offset=offset)
        if body.get("status") != "up":
            raise RuntimeError(f"DB 不可达：{body.get('detail')}")
        rows.extend(body["rows"])
        total = body["total"]
        stats, meta = body["stats"], body["meta"]
        offset += PAGE
        if offset >= total or not body["rows"]:
            break
    _write_json(out_dir / "funds" / "dca.json",
                {"status": "up", "total": total, "rows": rows,
                 "stats": stats, "meta": meta})
    log(f"  排行行：{len(rows):,}（total={total:,}）")
    return {"rows": len(rows)}


def export_scatter(dsn: str, out_dir: Path, *, log=print) -> None:
    for fee in FEES:
        body = fetch_dca_scatter(dsn, fee=fee)
        if body.get("status") != "up":
            raise RuntimeError(f"scatter {fee} 失败：{body.get('detail')}")
        _write_json(out_dir / "funds" / "dca" / f"scatter.{fee}.json", body)


def export_rounds(dsn: str, out_dir: Path, *, log=print) -> int:
    """三口径轮次按基金聚合为单文件；无轮次基金不落文件（前端 404=无止盈）。"""
    n = 0
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT code, fee, seq, tp_date, round_days, gap_days, profit,
                       tp_nav, invested, sub_fee, redeem_fee, net_profit
                FROM fund_dca_rounds ORDER BY code, fee, seq""")
            cur_code, rounds, totals = None, {}, {}

            def flush(code):
                if code is None:
                    return
                _write_json(out_dir / "funds" / "dca" / "rounds" / f"{code}.json",
                            {"status": "up", "code": code,
                             "rounds": rounds, "totals": totals})

            for (code, fee, seq, tp_date, rd, gap, profit, tp_nav, inv,
                 sf, rf, np_) in cur:
                if code != cur_code:
                    flush(cur_code)
                    n += 1
                    cur_code, rounds, totals = code, {}, {}
                row = {"seq": seq,
                       "tp_date": tp_date.isoformat() if tp_date else None,
                       "round_days": rd, "gap_days": gap,
                       "profit": round(profit, 4) if profit is not None else None,
                       "tp_nav": tp_nav, "invested": inv,
                       "sub_fee": sf, "redeem_fee": rf, "net_profit": np_}
                rounds.setdefault(fee, []).append(row)
                t = totals.setdefault(fee, {"rounds": 0, "invested": 0.0,
                                            "net_profit": 0.0, "sub_fee": 0.0,
                                            "redeem_fee": 0.0})
                t["rounds"] += 1
                for k, v in (("invested", inv), ("net_profit", np_),
                             ("sub_fee", sf), ("redeem_fee", rf)):
                    t[k] = round(t[k] + (v or 0), 2)
            flush(cur_code)
            n += 1
    return n


def export_navs(dsn: str, out_dir: Path, *, log=print) -> int:
    """单基金复权净值折线（止盈明细弹窗图，补齐静态站无 /api navs 的缺口）。

    窗口=数据集最新净值日回溯 10 年（与回测 w10 同口径，窗口内成立即从成立
    起）；复权链起点=1（pct_chg 分红再投，缺失用单位净值比兜底——与前端
    动态口径逐位一致）。降采样至 ~300 点并保底含全部止盈日（与轮次止盈点
    对齐）；体积压缩：日期存窗口首日天偏移、净值存 ×1e4 整数。窗口内净值
    <2 点不成线，不落文件。返回落文件基金数。
    """
    tp_ord: dict[str, set[int]] = {}
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, tp_date FROM fund_dca_rounds WHERE tp_date IS NOT NULL")
            for code, d in cur:
                tp_ord.setdefault(code, set()).add(d.toordinal())
            cur.execute("SELECT max(nav_date) FROM fund_nav")
            row = cur.fetchone()
        if not row or row[0] is None:
            log("  fund_nav 为空，跳过净值折线导出")
            return 0
        window_end = row[0]
        try:
            cutoff = window_end.replace(year=window_end.year - NAV_WINDOW_YEARS)
        except ValueError:                      # 2/29 起点回退 10 年非闰年
            cutoff = window_end - timedelta(days=365 * NAV_WINDOW_YEARS + 3)
        nav_sql = ("SELECT nav_date, pct_chg, unit_nav FROM fund_nav "
                   "WHERE code = %s AND nav_date >= %s ORDER BY nav_date")
        out_dir = Path(out_dir) / "funds" / "dca" / "navs"
        n = 0
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM fund_dca_result ORDER BY code")
            codes = [r[0] for r in cur.fetchall()]
            for i, code in enumerate(codes, 1):
                cur.execute(nav_sql, (code, cutoff))
                rows = cur.fetchall()
                if len(rows) < 2:
                    continue
                dates, vals, idx_of = [], [], {}
                v, prev_unit = 1.0, None
                for d, pct, unit in rows:
                    if dates:
                        if pct is not None:
                            g = pct / 100.0
                        elif unit and prev_unit:
                            g = unit / prev_unit - 1.0
                        else:
                            g = 0.0
                        v *= 1.0 + g
                    dates.append(d)
                    vals.append(v)
                    idx_of[d.toordinal()] = len(dates) - 1
                    prev_unit = unit
                total = len(dates)
                stride = max(1, round(total / NAV_TARGET_POINTS))
                keep = set(range(0, total, stride))
                keep.add(total - 1)
                for od in tp_ord.get(code, ()):
                    j = idx_of.get(od)
                    if j is not None:
                        keep.add(j)
                sel = sorted(keep)
                d0 = dates[0]
                _write_json(out_dir / f"{code}.json",
                            {"code": code, "d0": d0.isoformat(),
                             "d": [(dates[j] - d0).days for j in sel],
                             "v": [round(vals[j] * NAV_VALUE_SCALE) for j in sel]})
                n += 1
                if i % 5000 == 0:
                    log(f"  净值折线 {i:,}/{len(codes):,}")
    return n


def export_all(dsn: str, out_dir: Path, *, log=print) -> dict:
    """全量导出静态快照到 out_dir（site-data 结构）。返回统计。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    list_stats = export_list(dsn, out_dir, log=log)
    export_scatter(dsn, out_dir, log=log)
    fund_count = export_rounds(dsn, out_dir, log=log)
    nav_count = export_navs(dsn, out_dir, log=log)
    stats = {"rows": list_stats["rows"], "round_funds": fund_count,
             "nav_funds": nav_count}
    log(f"导出完成：排行 {stats['rows']:,} 行 / 轮次基金 {fund_count:,} 只 / "
        f"净值折线 {nav_count:,} 只")
    return stats

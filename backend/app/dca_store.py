"""定投止盈回测结果存储：fund_dca_result / fund_dca_meta 两表 + CSV 导入。

回测是一次性离线产物（scripts/fund_dca_backtest.py 全市场逐基金模拟，
输出 scripts/data/fund_dca_backtest.csv），本模块把快照导入库中供
/api/funds/dca 查询。重新回测后重跑导入即刷新（DELETE+INSERT 幂等）。

alive（在市）口径在导入时计算：full_end（最后净值日）距数据集最新净值日
45 天内——fund_basic.delisted 按"不在披露渠道"差集标记，场内 ETF 会被
误标（见 fund-scale-and-dca-backtest 笔记），不能直接用。

CLI：.venv/bin/python -m app.dca_store <csv路径> [--amount 1000] [--take-profit 0.2]
"""

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path

import psycopg

from .dca_backtest import DCA_METHODS   # 定投方式白名单（引擎单一事实源）

DDL = """
CREATE TABLE IF NOT EXISTS fund_dca_result (
  code            VARCHAR(10) PRIMARY KEY,
  name            VARCHAR(128) NOT NULL,
  fund_type       VARCHAR(64),
  alive           BOOLEAN NOT NULL DEFAULT FALSE,
  scale_yi        DOUBLE PRECISION,
  scale_date      DATE,
  w10_start       DATE, w10_end       DATE,
  w10_months      INTEGER, w10_rounds INTEGER,
  w10_invested    DOUBLE PRECISION, w10_final_value DOUBLE PRECISION,
  w10_profit_rate DOUBLE PRECISION, w10_xirr        DOUBLE PRECISION,
  full_start      DATE, full_end      DATE,
  full_months     INTEGER, full_rounds INTEGER,
  full_invested   DOUBLE PRECISION, full_final_value DOUBLE PRECISION,
  full_profit_rate DOUBLE PRECISION, full_xirr        DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_alive ON fund_dca_result (alive);
CREATE INDEX IF NOT EXISTS idx_fund_dca_w10_xirr ON fund_dca_result (w10_xirr);

CREATE TABLE IF NOT EXISTS fund_dca_rounds (
  code       VARCHAR(10) NOT NULL,
  fee        VARCHAR(8)  NOT NULL DEFAULT 'gross',
  seq        INTEGER NOT NULL,
  tp_date    DATE NOT NULL,
  round_days INTEGER,
  gap_days   INTEGER,
  profit     DOUBLE PRECISION,
  tp_nav     DOUBLE PRECISION,
  invested   DOUBLE PRECISION,
  sub_fee    DOUBLE PRECISION,
  redeem_fee DOUBLE PRECISION,
  net_profit DOUBLE PRECISION,
  PRIMARY KEY (code, fee, seq)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_rounds_code ON fund_dca_rounds (code);

ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_min_days INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_max_days INTEGER;
-- 费用三口径年化（gross 沿用原 *_xirr 列；disc=申购一折、nom=名义，赎回费一致）
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_xirr_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_xirr_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_xirr_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_xirr_nom DOUBLE PRECISION;
-- 排行表格口径切换：轮次/总收益率也随口径入库（份额随申购费折损，
-- 止盈日与触发轮次可能位移）；间隔均值按口径由轮次表聚合（下方 _APPLY_GAP）
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_rounds_disc INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_rounds_nom INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_rounds_disc INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_rounds_nom INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_profit_rate_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_profit_rate_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_profit_rate_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_profit_rate_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days_nom DOUBLE PRECISION;
-- 「最近一次止盈后」当前轮（全史窗口，2026-11-19）：止盈日随费用口径位移
-- 三列，invested 与费用无关一列，收益率/年化三口径各一列
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_last_tp DATE;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_last_tp_disc DATE;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_last_tp_nom DATE;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_invested DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_profit_rate DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_profit_rate_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_profit_rate_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_xirr DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_xirr_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_cur_xirr_nom DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS fund_dca_meta (
  id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fund_count   INTEGER,
  params       JSONB
);
"""

# 一次性迁移：v1 轮次表无 fee 列（PK code,seq），数据可由 CSV 全量重导，
# 检测到旧结构直接弃表重建（导入前 ensure_schema 兜底，生产由重导填充）
_MIGRATE_ROUNDS_V2 = """
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_name = 'fund_dca_rounds')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_name = 'fund_dca_rounds'
                       AND column_name = 'fee') THEN
    DROP TABLE fund_dca_rounds;
  END IF;
END $$;
"""

# 列顺序即 INSERT 占位符顺序
_COLS = ["code", "name", "fund_type", "alive", "scale_yi", "scale_date",
         "w10_start", "w10_end", "w10_months", "w10_rounds",
         "w10_invested", "w10_final_value", "w10_profit_rate", "w10_xirr",
         "full_start", "full_end", "full_months", "full_rounds",
         "full_invested", "full_final_value", "full_profit_rate", "full_xirr",
         "w10_xirr_disc", "w10_xirr_nom", "full_xirr_disc", "full_xirr_nom",
         "w10_rounds_disc", "w10_rounds_nom", "full_rounds_disc",
         "full_rounds_nom", "w10_profit_rate_disc", "w10_profit_rate_nom",
         "full_profit_rate_disc", "full_profit_rate_nom",
         # 「最近一次止盈后」当前轮（全史窗口，2026-11-19）
         "full_last_tp", "full_last_tp_disc", "full_last_tp_nom",
         "full_cur_invested",
         "full_cur_profit_rate", "full_cur_profit_rate_disc",
         "full_cur_profit_rate_nom",
         "full_cur_xirr", "full_cur_xirr_disc", "full_cur_xirr_nom"]

_DATE_COLS = {"scale_date", "w10_start", "w10_end", "full_start", "full_end",
              "full_last_tp", "full_last_tp_disc", "full_last_tp_nom"}
_INT_COLS = {"w10_months", "w10_rounds", "full_months", "full_rounds",
             "w10_rounds_disc", "w10_rounds_nom",
             "full_rounds_disc", "full_rounds_nom"}
_FLOAT_COLS = {"scale_yi", "w10_invested", "w10_final_value", "w10_profit_rate",
               "w10_xirr", "full_invested", "full_final_value",
               "full_profit_rate", "full_xirr",
               "w10_xirr_disc", "w10_xirr_nom", "full_xirr_disc", "full_xirr_nom",
               "w10_profit_rate_disc", "w10_profit_rate_nom",
               "full_profit_rate_disc", "full_profit_rate_nom",
               "full_cur_invested",
               "full_cur_profit_rate", "full_cur_profit_rate_disc",
               "full_cur_profit_rate_nom",
               "full_cur_xirr", "full_cur_xirr_disc", "full_cur_xirr_nom"}

_ALIVE_WINDOW_DAYS = 45   # 最后净值日距数据集最新净值日的在市窗口

_INSERT = f"""
INSERT INTO fund_dca_result ({", ".join(_COLS)})
VALUES ({", ".join(["%s"] * len(_COLS))})
"""

_META_UPSERT = """
INSERT INTO fund_dca_meta (id, generated_at, fund_count, params)
VALUES (TRUE, now(), %s, %s::jsonb)
ON CONFLICT (id) DO UPDATE SET
  generated_at = now(), fund_count = EXCLUDED.fund_count,
  params = EXCLUDED.params
"""




def _cell(col: str, raw: str):
    """CSV 文本 → 目标类型；空串/坏值 → None（坏日期记 None 而非炸导入）。"""
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        if col in _DATE_COLS:
            return dt.date.fromisoformat(raw)
        if col in _INT_COLS:
            return int(float(raw))
        if col in _FLOAT_COLS:
            return float(raw)
    except ValueError:
        return None
    return raw


def parse_csv(path: str | Path) -> list[dict]:
    """fund_dca_backtest.csv → 行字典列表（含导入期计算的 alive）。"""
    with open(path, newline="") as f:
        raw_rows = list(csv.DictReader(f))
    rows = []
    for r in raw_rows:
        rows.append({col: _cell(col, r.get(col, "")) for col in _COLS})
    latest = max((r["full_end"] for r in rows if r["full_end"]), default=None)
    cutoff = (latest - dt.timedelta(days=_ALIVE_WINDOW_DAYS)) if latest else None
    for r in rows:
        r["alive"] = bool(cutoff and r["full_end"] and r["full_end"] >= cutoff)
    return rows


_ROUNDS_COLS = ["code", "fee", "seq", "tp_date", "round_days", "gap_days",
                "profit", "tp_nav", "invested", "sub_fee", "redeem_fee",
                "net_profit"]
_ROUNDS_DATE = {"tp_date"}
_ROUNDS_INT = {"seq", "round_days", "gap_days"}
_ROUNDS_FLOAT = {"profit", "tp_nav", "invested", "sub_fee", "redeem_fee",
                 "net_profit"}

_UPSERT_ROUND = f"""
INSERT INTO fund_dca_rounds ({", ".join(_ROUNDS_COLS)})
VALUES ({", ".join(["%s"] * len(_ROUNDS_COLS))})
"""

_RESET_GAP = """
UPDATE fund_dca_result SET gap_avg_days = NULL, gap_min_days = NULL,
  gap_max_days = NULL, gap_avg_days_disc = NULL, gap_avg_days_nom = NULL
"""

_APPLY_GAP = """
UPDATE fund_dca_result r SET
  gap_avg_days = t.g_avg, gap_min_days = t.g_min, gap_max_days = t.g_max,
  gap_avg_days_disc = t.d_avg, gap_avg_days_nom = t.n_avg
FROM (SELECT code,
             avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg,
             min(gap_days) FILTER (WHERE fee = 'gross') AS g_min,
             max(gap_days) FILTER (WHERE fee = 'gross') AS g_max,
             avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg,
             avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg
      FROM fund_dca_rounds GROUP BY code) t
WHERE r.code = t.code
"""


def parse_rounds_csv(path: str | Path) -> list[tuple]:
    """fund_dca_rounds.csv → (code, seq, tp_date, round_days, gap_days, profit) 元组列表。"""
    with open(path, newline="") as f:
        raw = list(csv.DictReader(f))
    rows = []
    for r in raw:
        vals = []
        for col in _ROUNDS_COLS:
            v = (r.get(col) or "").strip()
            try:
                if col in _ROUNDS_DATE:
                    v = dt.date.fromisoformat(v) if v else None
                elif col in _ROUNDS_INT:
                    v = int(float(v)) if v else None
                elif col in _ROUNDS_FLOAT:
                    v = float(v) if v else None
            except ValueError:
                v = None
            vals.append(v)
        rows.append(tuple(vals))
    return rows


def import_rounds(conn, rows: list[tuple]) -> int:
    """整表替换止盈轮次 + 刷新 fund_dca_result 间隔汇总列；事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_rounds")
        if rows:
            cur.executemany(_UPSERT_ROUND, rows)
        cur.execute(_RESET_GAP)
        cur.execute(_APPLY_GAP)
    return len(rows)



# ===== 多止盈点回测存储（市场级阈值对比，与旧表完全隔离；2026-11-15 定案） =====
# 列结构与旧表逐列一致 + tp 维度；旧表/旧导出/旧测试零改动，静态通道可复活。

TP_DDL = """
CREATE TABLE IF NOT EXISTS fund_dca_tp_result (
  tp              DOUBLE PRECISION NOT NULL,
  method          VARCHAR(16) NOT NULL DEFAULT 'amount',
  code            VARCHAR(10) NOT NULL,
  name            VARCHAR(128) NOT NULL,
  fund_type       VARCHAR(64),
  alive           BOOLEAN NOT NULL DEFAULT FALSE,
  scale_yi        DOUBLE PRECISION,
  scale_date      DATE,
  w10_start       DATE, w10_end       DATE,
  w10_months      INTEGER, w10_rounds INTEGER,
  w10_invested    DOUBLE PRECISION, w10_final_value DOUBLE PRECISION,
  w10_profit_rate DOUBLE PRECISION, w10_xirr        DOUBLE PRECISION,
  full_start      DATE, full_end      DATE,
  full_months     INTEGER, full_rounds INTEGER,
  full_invested   DOUBLE PRECISION, full_final_value DOUBLE PRECISION,
  full_profit_rate DOUBLE PRECISION, full_xirr        DOUBLE PRECISION,
  w10_xirr_disc DOUBLE PRECISION, w10_xirr_nom DOUBLE PRECISION,
  full_xirr_disc DOUBLE PRECISION, full_xirr_nom DOUBLE PRECISION,
  w10_rounds_disc INTEGER, w10_rounds_nom INTEGER,
  full_rounds_disc INTEGER, full_rounds_nom INTEGER,
  w10_profit_rate_disc DOUBLE PRECISION, w10_profit_rate_nom DOUBLE PRECISION,
  full_profit_rate_disc DOUBLE PRECISION, full_profit_rate_nom DOUBLE PRECISION,
  gap_avg_days DOUBLE PRECISION, gap_min_days INTEGER, gap_max_days INTEGER,
  gap_avg_days_disc DOUBLE PRECISION, gap_avg_days_nom DOUBLE PRECISION,
  -- 「最近一次止盈后」当前轮（全史窗口，2026-11-19，与旧表同组十列）
  full_last_tp DATE, full_last_tp_disc DATE, full_last_tp_nom DATE,
  full_cur_invested DOUBLE PRECISION,
  full_cur_profit_rate DOUBLE PRECISION,
  full_cur_profit_rate_disc DOUBLE PRECISION,
  full_cur_profit_rate_nom DOUBLE PRECISION,
  full_cur_xirr DOUBLE PRECISION,
  full_cur_xirr_disc DOUBLE PRECISION, full_cur_xirr_nom DOUBLE PRECISION,
  PRIMARY KEY (code, tp, method)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_alive ON fund_dca_tp_result (alive);
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_tp ON fund_dca_tp_result (tp, w10_xirr);
--（method 维度索引只在文末迁移块建——存量表 ALTER 加列之前建会 UndefinedColumn）

CREATE TABLE IF NOT EXISTS fund_dca_tp_rounds (
  tp         DOUBLE PRECISION NOT NULL,
  method     VARCHAR(16) NOT NULL DEFAULT 'amount',
  code       VARCHAR(10) NOT NULL,
  fee        VARCHAR(8)  NOT NULL DEFAULT 'gross',
  seq        INTEGER NOT NULL,
  tp_date    DATE NOT NULL,
  round_days INTEGER,
  gap_days   INTEGER,
  profit     DOUBLE PRECISION,
  tp_nav     DOUBLE PRECISION,
  invested   DOUBLE PRECISION,
  sub_fee    DOUBLE PRECISION,
  redeem_fee DOUBLE PRECISION,
  net_profit DOUBLE PRECISION,
  PRIMARY KEY (code, tp, method, fee, seq)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_rounds_code
  ON fund_dca_tp_rounds (code, tp);

CREATE TABLE IF NOT EXISTS fund_dca_tp_meta (
  id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fund_count   INTEGER,
  params       JSONB
);

-- 定投时机（当前回撤桶，2026-11-16 假设验证：-25~-30% 为甜蜜点、30%+ 警戒）
CREATE TABLE IF NOT EXISTS fund_dip_timing (
  code            VARCHAR(10) PRIMARY KEY,
  dd_current      DOUBLE PRECISION,
  days_since_peak INTEGER,
  dip_band        VARCHAR(16),
  computed_at     DATE
);
CREATE INDEX IF NOT EXISTS idx_fund_dip_band ON fund_dip_timing (dip_band);

-- 起点条件回测报告（假设验证统计落库：三组对照/深度桶/起点年份 × 止盈线）
CREATE TABLE IF NOT EXISTS fund_dip_validation (
  grp             VARCHAR(8) NOT NULL,
  bucket          VARCHAR(16) NOT NULL,
  tp              DOUBLE PRECISION NOT NULL,
  n               INTEGER,
  days_median     INTEGER,
  days_p25        INTEGER,
  days_p75        INTEGER,
  success_rate    DOUBLE PRECISION,
  xirr_median     DOUBLE PRECISION,
  xirr_p25        DOUBLE PRECISION,
  xirr_p75        DOUBLE PRECISION,
  PRIMARY KEY (grp, bucket, tp)
);

-- 已有部署的 tp 结果表补「最近一次止盈后」当前轮十列
-- （CREATE TABLE IF NOT EXISTS 对已存在表不生效，同 fund_dca_result 迁移）
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_last_tp DATE;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_last_tp_disc DATE;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_last_tp_nom DATE;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_invested DOUBLE PRECISION;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_profit_rate DOUBLE PRECISION;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_profit_rate_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_profit_rate_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_xirr DOUBLE PRECISION;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_xirr_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS full_cur_xirr_nom DOUBLE PRECISION;

-- 定投方式维度（2026-11-23）：存量表就地加 method 列（默认 amount，行数据
-- 不丢、无空窗——2026-10-26/11-20 两次漏迁移教训），PK 守护式纳入 method
-- （唯一性要求同 (code, tp, method)；新装表 CREATE 已含，此处只动旧表）
ALTER TABLE fund_dca_tp_result ADD COLUMN IF NOT EXISTS method VARCHAR(16) NOT NULL DEFAULT 'amount';
ALTER TABLE fund_dca_tp_rounds ADD COLUMN IF NOT EXISTS method VARCHAR(16) NOT NULL DEFAULT 'amount';
-- 加宽守护（2026-11-23 四轮：dd_ladder 9 字符超初版 VARCHAR(8)，实跑踩过）——
-- varchar 加宽为元数据级操作，幂等无害，存量值不受影响
ALTER TABLE fund_dca_tp_result ALTER COLUMN method TYPE VARCHAR(16);
ALTER TABLE fund_dca_tp_rounds ALTER COLUMN method TYPE VARCHAR(16);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    WHERE c.conrelid = 'fund_dca_tp_result'::regclass AND c.contype = 'p'
      AND 'method' = ANY (SELECT a.attname FROM pg_attribute a
                          WHERE a.attrelid = c.conrelid
                            AND a.attnum = ANY (c.conkey))
  ) THEN
    ALTER TABLE fund_dca_tp_result DROP CONSTRAINT fund_dca_tp_result_pkey;
    ALTER TABLE fund_dca_tp_result ADD PRIMARY KEY (code, tp, method);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    WHERE c.conrelid = 'fund_dca_tp_rounds'::regclass AND c.contype = 'p'
      AND 'method' = ANY (SELECT a.attname FROM pg_attribute a
                          WHERE a.attrelid = c.conrelid
                            AND a.attnum = ANY (c.conkey))
  ) THEN
    ALTER TABLE fund_dca_tp_rounds DROP CONSTRAINT fund_dca_tp_rounds_pkey;
    ALTER TABLE fund_dca_tp_rounds ADD PRIMARY KEY (code, tp, method, fee, seq);
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_method
  ON fund_dca_tp_result (method, tp, w10_xirr);
"""

_TP_COLS = ["tp", "method"] + _COLS
_TP_INSERT = f"""
INSERT INTO fund_dca_tp_result ({", ".join(_TP_COLS)})
VALUES ({", ".join(["%s"] * len(_TP_COLS))})
"""

# 列序必须与引擎元组 (code, method, tp, fee, seq, *ev) 逐位对齐——PG 隐式
# 类型转换会把错位写入"成功"（'000001'→tp 列 1.0），只能靠列清单对齐防错
_TP_ROUNDS_COLS = (_ROUNDS_COLS[:1] + ["method", "tp"] + _ROUNDS_COLS[1:])
_TP_UPSERT_ROUND = f"""
INSERT INTO fund_dca_tp_rounds ({", ".join(_TP_ROUNDS_COLS)})
VALUES ({", ".join(["%s"] * len(_TP_ROUNDS_COLS))})
"""

_TP_META_UPSERT = """
INSERT INTO fund_dca_tp_meta (id, generated_at, fund_count, params)
VALUES (TRUE, now(), %s, %s::jsonb)
ON CONFLICT (id) DO UPDATE SET
  generated_at = now(), fund_count = EXCLUDED.fund_count,
  params = EXCLUDED.params
"""

_RESET_GAP_TP = """
UPDATE fund_dca_tp_result SET gap_avg_days = NULL, gap_min_days = NULL,
  gap_max_days = NULL, gap_avg_days_disc = NULL, gap_avg_days_nom = NULL
"""

_APPLY_GAP_TP = """
UPDATE fund_dca_tp_result r SET
  gap_avg_days = t.g_avg, gap_min_days = t.g_min, gap_max_days = t.g_max,
  gap_avg_days_disc = t.d_avg, gap_avg_days_nom = t.n_avg
FROM (SELECT code, tp, method,
             avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg,
             min(gap_days) FILTER (WHERE fee = 'gross') AS g_min,
             max(gap_days) FILTER (WHERE fee = 'gross') AS g_max,
             avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg,
             avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg
      FROM fund_dca_tp_rounds GROUP BY code, tp, method) t
WHERE r.code = t.code AND r.tp = t.tp AND r.method = t.method
"""


# 基金风险/波动指标（复权口径，risk_metrics.compute_risk_metrics 日更全量
# 重算整表替换；列表排序/详情指标卡/排行展示共用——独立 DDL 便于启动清单
# 兜底建表，不必等首轮日更）
RISK_DDL = """
CREATE TABLE IF NOT EXISTS fund_risk_metrics (
  code            VARCHAR(10) PRIMARY KEY,
  vol_1y DOUBLE PRECISION, vol_down_1y DOUBLE PRECISION,
  mdd_1y DOUBLE PRECISION, sharpe_1y DOUBLE PRECISION,
  vol_3y DOUBLE PRECISION, vol_down_3y DOUBLE PRECISION,
  mdd_3y DOUBLE PRECISION, sharpe_3y DOUBLE PRECISION,
  vol_5y DOUBLE PRECISION, vol_down_5y DOUBLE PRECISION,
  mdd_5y DOUBLE PRECISION, sharpe_5y DOUBLE PRECISION,
  vol_all DOUBLE PRECISION, vol_down_all DOUBLE PRECISION,
  mdd_all DOUBLE PRECISION, sharpe_all DOUBLE PRECISION,
  roll_vol        DOUBLE PRECISION,
  vol_pctile      DOUBLE PRECISION,
  risk_score      DOUBLE PRECISION,
  osc_per_year    DOUBLE PRECISION, osc_regularity DOUBLE PRECISION,
  osc_pctile      DOUBLE PRECISION,
  as_of           DATE,
  nav_count       INTEGER,
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 已有部署的表补列（CREATE TABLE IF NOT EXISTS 对已存在表不生效）
_RISK_MIGRATE = (
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS risk_score DOUBLE PRECISION;\n"
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS osc_per_year DOUBLE PRECISION;\n"
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS osc_regularity DOUBLE PRECISION;\n"
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS osc_pctile DOUBLE PRECISION;"
)

# 综合波动分位回填（幂等，整表重算）：年化波动率/下行波动率/最大回撤三项
# **全史口径**（*_all，2026-11-18 用户需求「波动指标统计整个历史」）在全样本
# （非 NULL 行）中的 percent_rank 横截面等权平均，round(4)，越低越稳
# （三项都偏低=波动小回撤浅）；三指标全缺 → NULL。全史窗口只需 ≥20 个收益
# 样本（MIN_RETURNS），比 1y 窗口覆盖更多新基金。
# 日更 import_risk 同事务调用 + 启动清单兜底调用（部署即得历史数据）。
_RISK_SCORE_BACKFILL = """
UPDATE fund_risk_metrics rm
SET risk_score = t.score, osc_pctile = t.opct
FROM (
  SELECT code,
         round(((coalesce(pv, 0) + coalesce(pd, 0) + coalesce(pm, 0))
               / NULLIF((CASE WHEN pv IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN pd IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN pm IS NOT NULL THEN 1 ELSE 0 END), 0))
               ::numeric, 4) AS score,
         CASE WHEN osc_per_year IS NULL THEN NULL
              ELSE round(percent_rank() OVER (PARTITION BY osc_per_year IS NULL
                                              ORDER BY osc_per_year)::numeric, 4)
         END AS opct
  FROM (
    SELECT code,
           CASE WHEN vol_all IS NULL THEN NULL
                ELSE percent_rank() OVER (PARTITION BY vol_all IS NULL
                                          ORDER BY vol_all) END AS pv,
           CASE WHEN vol_down_all IS NULL THEN NULL
                ELSE percent_rank() OVER (PARTITION BY vol_down_all IS NULL
                                          ORDER BY vol_down_all) END AS pd,
           CASE WHEN mdd_all IS NULL THEN NULL
                ELSE percent_rank() OVER (PARTITION BY mdd_all IS NULL
                                          ORDER BY mdd_all) END AS pm,
           osc_per_year
    FROM fund_risk_metrics
  ) x
) t
WHERE rm.code = t.code
"""


def backfill_risk_scores(conn) -> None:
    """综合波动分位横截面回填（幂等整表重算；事务由调用方提交）。"""
    with conn.cursor() as cur:
        cur.execute(_RISK_SCORE_BACKFILL)


def ensure_tp_schema(conn) -> None:
    """多阈值新表 + 波动指标表幂等建表（调度器日更首跑前调用）。"""
    with conn.cursor() as cur:
        cur.execute(TP_DDL)
        cur.execute(RISK_DDL)
        cur.execute(_RISK_MIGRATE)
        cur.execute(_RISK_SCORE_BACKFILL)


def ensure_risk_schema(conn) -> None:
    """波动指标表幂等建表（启动清单兜底，缺表时浏览接口不至于整页 down）。"""
    with conn.cursor() as cur:
        cur.execute(RISK_DDL)
        cur.execute(_RISK_MIGRATE)
        cur.execute(_RISK_SCORE_BACKFILL)


def import_tp_rows(conn, rows: list[dict], *, params: dict | None = None) -> int:
    """多阈值结果快照整体替换（DELETE+INSERT）+ meta 单行 upsert。

    meta.fund_count 为去重基金数（行数 = 基金 × 阈值）；事务由调用方提交。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_tp_result")
        if rows:
            cur.executemany(_TP_INSERT,
                            [tuple(r[c] for c in _TP_COLS) for r in rows])
        n_funds = len({r["code"] for r in rows})
        cur.execute(_TP_META_UPSERT,
                    (n_funds, json.dumps(params or {}, ensure_ascii=False)))
    return len(rows)


def import_tp_rounds(conn, rows: list[tuple]) -> int:
    """多阈值轮次整表替换 + 按 (code, tp) 刷新结果表间隔汇总列。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_tp_rounds")
        if rows:
            cur.executemany(_TP_UPSERT_ROUND, rows)
        cur.execute(_RESET_GAP_TP)
        cur.execute(_APPLY_GAP_TP)
    return len(rows)

_TIMING_COLS = ["code", "dd_current", "days_since_peak", "dip_band",
                "computed_at"]
_TIMING_INSERT = f"""
INSERT INTO fund_dip_timing ({", ".join(_TIMING_COLS)})
VALUES ({", ".join(["%s"] * len(_TIMING_COLS))})
"""


def import_timing(conn, rows: list[tuple]) -> int:
    """时机表整体替换（DELETE+INSERT，日更顺带刷新）；事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dip_timing")
        if rows:
            cur.executemany(_TIMING_INSERT, rows)
    return len(rows)


# 列序与 risk_metrics.ROW_FIELDS 逐位对齐（risk_row 产码，code 前缀 +
# computed_at 由库端 now() 填充）；str 日期入 DATE 列同 import_timing 先例
_RISK_COLS = ["code", "vol_1y", "vol_down_1y", "mdd_1y", "sharpe_1y",
              "vol_3y", "vol_down_3y", "mdd_3y", "sharpe_3y",
              "vol_5y", "vol_down_5y", "mdd_5y", "sharpe_5y",
              "vol_all", "vol_down_all", "mdd_all", "sharpe_all",
              "roll_vol", "vol_pctile", "osc_per_year", "osc_regularity",
              "as_of", "nav_count"]
_RISK_INSERT = f"""
INSERT INTO fund_risk_metrics ({", ".join(_RISK_COLS)})
VALUES ({", ".join(["%s"] * len(_RISK_COLS))})
"""


def import_risk(conn, rows: list[tuple]) -> int:
    """波动指标整表替换（DELETE+INSERT，日更顺带刷新）；事务由调用方提交。

    替换后同事务回填综合波动分位（横截面指标须全样本落库后统一计算）。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_risk_metrics")
        if rows:
            cur.executemany(_RISK_INSERT, rows)
        cur.execute(_RISK_SCORE_BACKFILL)
    return len(rows)


# ===== 脏检测日更合并导入（2026-11-24） =====
# 与上方 import_* 整表替换并存：调度器脏检测路径用 merge_*（按 code 范围
# DELETE + INSERT，未变基金的原行原地保留），脚本/回放路径继续用 import_*。
# 范围替换先例见 scripts/dca_share_sample.py；"合并导入"必须保证同一引擎
# 版本——引擎/参数变更由引擎侧 engine_version() 自动触发全量（脏路径退化为
# 与整表替换等价：全部基金进 DELETE 范围）。

FP_DDL = """
CREATE TABLE IF NOT EXISTS fund_dca_fp (
  code        VARCHAR(10) PRIMARY KEY,
  fp          JSONB NOT NULL,
  engine      TEXT NOT NULL,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_fp_schema(conn) -> None:
    """基金输入指纹表幂等建表（脏检测日更前提，挂启动清单）。"""
    with conn.cursor() as cur:
        cur.execute(FP_DDL)


def load_fingerprints(conn) -> tuple[dict[str, tuple], str | None]:
    """上次日更落库的基金输入指纹 → ({code: fp 元组}, engine 版本串)。
    表空返回 ({}, None)（编排侧视为全量）。fp 以 jsonb 数组存储、读回
    转 tuple 与 load_input_fingerprints 的返回值逐位可比。"""
    out: dict[str, tuple] = {}
    engine: str | None = None
    with conn.cursor() as cur:
        cur.execute("SELECT code, fp, engine FROM fund_dca_fp")
        for code, fp, eng in cur.fetchall():
            out[str(code)] = tuple(fp)
            engine = eng          # 同批整表写入，单版本语义
    return out, engine


def replace_fingerprints(conn, fps: dict[str, tuple], engine: str) -> int:
    """指纹表整表替换（表小 ~2.7 万行，不做范围合并）；事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_fp")
        if fps:
            cur.executemany(
                "INSERT INTO fund_dca_fp (code, fp, engine) "
                "VALUES (%s, %s::jsonb, %s)",
                [(code, json.dumps(list(fp), ensure_ascii=False), engine)
                 for code, fp in fps.items()])
    return len(fps)


def _codes_arr(codes: list[str]) -> str:
    return "{" + ",".join(codes) + "}"


def merge_rows(conn, rows: list[dict], codes: list[str], *, fund_count: int,
               params: dict | None = None) -> int:
    """旧口径结果按 code 范围替换 + meta upsert。codes = 本次重算 ∪ gone
    （消失基金也要清行）；未变基金的原行原地保留。meta.fund_count 由调用
    方显式传全市场基金数（不再取本次行数）；事务由调用方提交。"""
    with conn.cursor() as cur:
        if codes:
            cur.execute("DELETE FROM fund_dca_result WHERE code = ANY(%s)",
                        (_codes_arr(codes),))
        if rows:
            cur.executemany(_INSERT, [tuple(r[c] for c in _COLS) for r in rows])
        cur.execute(_META_UPSERT,
                    (fund_count, json.dumps(params or {}, ensure_ascii=False)))
    return len(rows)


# 间隔聚合范围化变体：只对本次替换的基金 NULL 化 + 重聚合（照抄
# dca_share_sample 的防误清写法——全表 RESET 会抹掉未变基金的间隔列）
_RESET_GAP_CODED = """
UPDATE fund_dca_result SET gap_avg_days = NULL, gap_min_days = NULL,
  gap_max_days = NULL, gap_avg_days_disc = NULL, gap_avg_days_nom = NULL
WHERE code = ANY(%s)
"""

_APPLY_GAP_CODED = """
UPDATE fund_dca_result r SET
  gap_avg_days = t.g_avg, gap_min_days = t.g_min, gap_max_days = t.g_max,
  gap_avg_days_disc = t.d_avg, gap_avg_days_nom = t.n_avg
FROM (SELECT code,
             avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg,
             min(gap_days) FILTER (WHERE fee = 'gross') AS g_min,
             max(gap_days) FILTER (WHERE fee = 'gross') AS g_max,
             avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg,
             avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg
      FROM fund_dca_rounds WHERE code = ANY(%s) GROUP BY code) t
WHERE r.code = t.code
"""

_RESET_GAP_TP_CODED = """
UPDATE fund_dca_tp_result SET gap_avg_days = NULL, gap_min_days = NULL,
  gap_max_days = NULL, gap_avg_days_disc = NULL, gap_avg_days_nom = NULL
WHERE code = ANY(%s)
"""

_APPLY_GAP_TP_CODED = """
UPDATE fund_dca_tp_result r SET
  gap_avg_days = t.g_avg, gap_min_days = t.g_min, gap_max_days = t.g_max,
  gap_avg_days_disc = t.d_avg, gap_avg_days_nom = t.n_avg
FROM (SELECT code, tp, method,
             avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg,
             min(gap_days) FILTER (WHERE fee = 'gross') AS g_min,
             max(gap_days) FILTER (WHERE fee = 'gross') AS g_max,
             avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg,
             avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg
      FROM fund_dca_tp_rounds WHERE code = ANY(%s)
      GROUP BY code, tp, method) t
WHERE r.code = t.code AND r.tp = t.tp AND r.method = t.method
"""


def merge_rounds(conn, rows: list[tuple], codes: list[str]) -> int:
    """轮次按 code 范围替换 + 仅范围内基金重算间隔汇总列；事务由调用方提交。"""
    with conn.cursor() as cur:
        if codes:
            arr = _codes_arr(codes)
            cur.execute("DELETE FROM fund_dca_rounds WHERE code = ANY(%s)",
                        (arr,))
            if rows:
                cur.executemany(_UPSERT_ROUND, rows)
            cur.execute(_RESET_GAP_CODED, (arr,))
            cur.execute(_APPLY_GAP_CODED, (arr,))
    return len(rows)


def merge_tp_rows(conn, rows: list[dict], codes: list[str], *,
                  fund_count: int, params: dict | None = None) -> int:
    """多阈值结果按 code 范围替换 + meta upsert（语义同 merge_rows）。"""
    with conn.cursor() as cur:
        if codes:
            cur.execute("DELETE FROM fund_dca_tp_result WHERE code = ANY(%s)",
                        (_codes_arr(codes),))
        if rows:
            cur.executemany(_TP_INSERT,
                            [tuple(r[c] for c in _TP_COLS) for r in rows])
        cur.execute(_TP_META_UPSERT,
                    (fund_count, json.dumps(params or {}, ensure_ascii=False)))
    return len(rows)


def merge_tp_rounds(conn, rows: list[tuple], codes: list[str]) -> int:
    """多阈值轮次按 code 范围替换 + 仅范围内基金重算间隔汇总列。"""
    with conn.cursor() as cur:
        if codes:
            arr = _codes_arr(codes)
            cur.execute("DELETE FROM fund_dca_tp_rounds WHERE code = ANY(%s)",
                        (arr,))
            if rows:
                cur.executemany(_TP_UPSERT_ROUND, rows)
            cur.execute(_RESET_GAP_TP_CODED, (arr,))
            cur.execute(_APPLY_GAP_TP_CODED, (arr,))
    return len(rows)


def refresh_alive(conn, cutoff) -> None:
    """alive 全表集合刷新：45 天窗口相对全局 all_end，窗口推进时跳过重算
    的基金也要翻转（活→死单向；死而复生必伴随新净值=必在重算集）。cutoff
    为 date 或 ISO 串；None（空库）不动作。事务由调用方提交。"""
    if cutoff is None:
        return
    if isinstance(cutoff, str):
        cutoff = dt.date.fromisoformat(cutoff)
    with conn.cursor() as cur:
        cur.execute("UPDATE fund_dca_result SET alive = "
                    "(full_end IS NOT NULL AND full_end >= %s)", (cutoff,))
        cur.execute("UPDATE fund_dca_tp_result SET alive = "
                    "(full_end IS NOT NULL AND full_end >= %s)", (cutoff,))


def merge_timing(conn, rows: list[tuple], *, computed_at) -> int:
    """时机表合并：①清理已不在市基金的行（时机=「当前时机」语义，表内容
    恒等于 alive 集合；须在 refresh_alive 之后调用）→ ②插入本次重算的
    alive 基金新行 → ③保留行 computed_at 统一刷到本轮窗口末日（跳过基金
    行原样保留，仅时间戳刷新）。事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dip_timing t WHERE NOT EXISTS ("
                    "SELECT 1 FROM fund_dca_result r "
                    "WHERE r.code = t.code AND r.alive)")
        if rows:
            cur.executemany(_TIMING_INSERT, rows)
        cur.execute("UPDATE fund_dip_timing SET computed_at = %s",
                    (computed_at,))
    return len(rows)


def merge_risk(conn, rows: list[tuple], codes: list[str]) -> int:
    """波动指标按 code 范围替换 + 全表横截面回填（risk_score/osc_pctile
    是全样本 percent_rank，任何子集变更后都须整表重算——幂等，开销小）。"""
    with conn.cursor() as cur:
        if codes:
            cur.execute("DELETE FROM fund_risk_metrics WHERE code = ANY(%s)",
                        (_codes_arr(codes),))
        if rows:
            cur.executemany(_RISK_INSERT, rows)
        cur.execute(_RISK_SCORE_BACKFILL)
    return len(rows)


# ===== 止盈点对比市场级聚合快照（2026-11-25） =====
# /api/funds/dca/tp/compare 原先每请求现算聚合（fund_dca_tp_result 百万行级
# percentile_cont 分位，生产实测 ~2.2s/次、前端切换定投方式感知 2~4s）——
# 改为日更导入后顺带预计算落小表（method × equity × fee × tp = 5×2×3×8
# = 240 行），API 优先读快照（毫秒级），快照缺表/缺行回落现算。
# 口径锚点（2026-11-25 生产真机对跑逐值一致）：旧查询 WHERE {wx} IS NOT
# NULL 把「年化非空」宇宙作用到全部聚合列——年化 NULL（窗口过短）的行其
# 轮次/间隔列仍非空、不能混入统计（无过滤单遍版会被真机数据证伪：成功率
# /平均轮次/平均间隔全部偏移），因此单遍版每个聚合都必须带
# FILTER (WHERE {wx} IS NOT NULL)。

_TP_COMPARE_DDL = """
CREATE TABLE IF NOT EXISTS fund_dca_tp_compare (
  method       VARCHAR(16) NOT NULL,
  equity       BOOLEAN NOT NULL,
  fee          VARCHAR(8) NOT NULL,
  tp           DOUBLE PRECISION NOT NULL,
  sample_count INTEGER,
  w10_p25 DOUBLE PRECISION, w10_median DOUBLE PRECISION,
  w10_p75 DOUBLE PRECISION, w10_p90 DOUBLE PRECISION, w10_mean DOUBLE PRECISION,
  w10_positive_share DOUBLE PRECISION, w10_success_share DOUBLE PRECISION,
  w10_avg_rounds DOUBLE PRECISION,
  full_median DOUBLE PRECISION, full_mean DOUBLE PRECISION,
  full_positive_share DOUBLE PRECISION, full_success_share DOUBLE PRECISION,
  avg_gap_days DOUBLE PRECISION,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (method, equity, fee, tp)
);
"""

_FEE_COL_SUFFIX = {"gross": "", "disc": "_disc", "nom": "_nom"}

# 单费用口径的 14 项聚合列块（{wx} 年化 / {fx} 全史年化 / {wr} 轮次 /
# {fr} 全史轮次 / {gap} 平均间隔；后缀按 _FEE_COL_SUFFIX 替换）。列序与
# _tp_compare_row 映射、API 快照行映射逐位对齐；FILTER 口径锚点见节注释
_TP_AGG_BLOCK = """
  count({wx}) FILTER (WHERE {wx} IS NOT NULL),
  percentile_cont(0.25) WITHIN GROUP (ORDER BY {wx}) FILTER (WHERE {wx} IS NOT NULL),
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY {wx}) FILTER (WHERE {wx} IS NOT NULL),
  percentile_cont(0.75) WITHIN GROUP (ORDER BY {wx}) FILTER (WHERE {wx} IS NOT NULL),
  percentile_cont(0.9)  WITHIN GROUP (ORDER BY {wx}) FILTER (WHERE {wx} IS NOT NULL),
  avg({wx}) FILTER (WHERE {wx} IS NOT NULL),
  avg(({wx} > 0)::int) FILTER (WHERE {wx} IS NOT NULL),
  avg(({wr} > 0)::int) FILTER (WHERE {wx} IS NOT NULL),
  avg({wr}) FILTER (WHERE {wx} IS NOT NULL),
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {fx}) FILTER (WHERE {wx} IS NOT NULL),
  avg({fx}) FILTER (WHERE {wx} IS NOT NULL),
  avg(({fx} > 0)::int) FILTER (WHERE {wx} IS NOT NULL),
  avg(({fr} > 0)::int) FILTER (WHERE {wx} IS NOT NULL),
  avg({gap}) FILTER (WHERE {wx} IS NOT NULL)
"""

_TP_COMPARE_AGG_SQL = "SELECT tp,\n" + ",\n".join(
    _TP_AGG_BLOCK.format(wx=f"w10_xirr{sfx}", fx=f"full_xirr{sfx}",
                         wr=f"w10_rounds{sfx}", fr=f"full_rounds{sfx}",
                         gap=f"gap_avg_days{sfx}")
    for sfx in _FEE_COL_SUFFIX.values()) + """
FROM fund_dca_tp_result
WHERE alive AND method = %s{equity_clause}
GROUP BY tp ORDER BY tp
"""

_TP_COMPARE_COLS = ["method", "equity", "fee", "tp", "sample_count",
                    "w10_p25", "w10_median", "w10_p75", "w10_p90", "w10_mean",
                    "w10_positive_share", "w10_success_share",
                    "w10_avg_rounds", "full_median", "full_mean",
                    "full_positive_share", "full_success_share",
                    "avg_gap_days"]
_TP_COMPARE_INSERT = f"""
INSERT INTO fund_dca_tp_compare ({", ".join(_TP_COMPARE_COLS)})
VALUES ({", ".join(["%s"] * len(_TP_COMPARE_COLS))})
"""


def _tp_compare_row(method: str, equity: bool, fee: str, r: tuple) -> tuple:
    """单遍聚合行 → 快照行。一行含三费用块（r[1:15] gross、r[15:29] disc、
    r[29:43] nom，块序同 _TP_COMPARE_AGG_SQL 的 _FEE_COL_SUFFIX 拼接序），
    按 fee 取对应块；列内序同 _TP_AGG_BLOCK（count / w10 四分位+均值 /
    正收益 / 成功 / 平均轮次 / 全史四项 / 平均间隔）。精度圆整同 API 现算
    （分位/均值/占比 4 位、平均轮次 2 位、平均间隔 1 位——快照输出与现算
    输出逐值一致）。"""
    off = 1 + {"gross": 0, "disc": 14, "nom": 28}[fee]

    def q(i, nd):
        return round(r[off + i], nd) if r[off + i] is not None else None
    return (method, equity, fee, r[0],
            r[off] if r[off] is not None else None,
            q(1, 4), q(2, 4), q(3, 4), q(4, 4), q(5, 4),
            q(6, 4), q(7, 4), q(8, 2), q(9, 4), q(10, 4),
            q(11, 4), q(12, 4), q(13, 1))


def rebuild_tp_compare(conn, *, equity_types) -> int:
    """八档对比市场级聚合快照整表替换（method × equity × fee × tp 全组合，
    每行恒带三费用口径；DDL 幂等自带）。equity_types 为对比池基金类型
    白名单（dca_api.TP_COMPARE_EQUITY_TYPES，参数传入避免 store→api 反向
    依赖）。每 (method, equity) 单遍聚合三费用口径（FILTER 口径锚点见节
    注释）。事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute(_TP_COMPARE_DDL)
        rows = []
        for method in DCA_METHODS:
            for equity in (True, False):
                sql = _TP_COMPARE_AGG_SQL.replace(
                    "{equity_clause}", " AND fund_type = ANY(%s)" if equity
                    else "")
                params = (method, list(equity_types)) if equity else (method,)
                cur.execute(sql, params)
                for r in cur.fetchall():
                    for fee in _FEE_COL_SUFFIX:
                        rows.append(_tp_compare_row(method, equity, fee, r))
        cur.execute("DELETE FROM fund_dca_tp_compare")
        if rows:
            cur.executemany(_TP_COMPARE_INSERT, rows)
    return len(rows)


_VALIDATION_COLS = ["grp", "bucket", "tp", "n", "days_median", "days_p25",
                    "days_p75", "success_rate", "xirr_median", "xirr_p25",
                    "xirr_p75"]
_VALIDATION_INSERT = f"""
INSERT INTO fund_dip_validation ({", ".join(_VALIDATION_COLS)})
VALUES ({", ".join(["%s"] * len(_VALIDATION_COLS))})
"""


def import_dip_validation(conn, rows: list[tuple]) -> int:
    """验证报告整表替换（三维度共用一张表，重跑后旧数据不得残留）；
    事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dip_validation")
        if rows:
            cur.executemany(_VALIDATION_INSERT, rows)
    return len(rows)


# ===== 熊市断缴实验存储（离线脚本导入，fund_dip_validation 同族；2026-11-19） =====
# 三表：fund_bear_pause_fund（每基金 A/B/C/D 四组 × 止盈20%/无止盈两口径）/
# fund_bear_pause_market（子样本×口径配对统计）/ fund_bear_pause_meta（单行
# 参数）。计算引擎 app/bear_pause.py；导入 scripts/bear_pause_import.py；
# 重跑整表替换，静态快照不进日更。

BEAR_PAUSE_DDL = """
CREATE TABLE IF NOT EXISTS fund_bear_pause_fund (
  code         VARCHAR(10) PRIMARY KEY,
  span_days    INTEGER,
  months       INTEGER,
  ever_paused  BOOLEAN,
  episodes     INTEGER,
  pause_months INTEGER,
  dd_end       DOUBLE PRECISION,
  dd_min       DOUBLE PRECISION,
  in_universe  BOOLEAN NOT NULL DEFAULT TRUE,
  tp_a_wallet DOUBLE PRECISION, tp_a_xirr DOUBLE PRECISION,
  tp_a_out DOUBLE PRECISION, tp_a_rounds INTEGER,
  tp_b_wallet DOUBLE PRECISION, tp_b_xirr DOUBLE PRECISION,
  tp_b_out DOUBLE PRECISION, tp_b_rounds INTEGER,
  tp_c_wallet DOUBLE PRECISION, tp_c_xirr DOUBLE PRECISION,
  tp_c_out DOUBLE PRECISION, tp_c_rounds INTEGER,
  tp_d_wallet DOUBLE PRECISION, tp_d_xirr DOUBLE PRECISION,
  tp_d_out DOUBLE PRECISION, tp_d_rounds INTEGER,
  nt_a_wallet DOUBLE PRECISION, nt_a_xirr DOUBLE PRECISION,
  nt_a_out DOUBLE PRECISION, nt_a_rounds INTEGER,
  nt_b_wallet DOUBLE PRECISION, nt_b_xirr DOUBLE PRECISION,
  nt_b_out DOUBLE PRECISION, nt_b_rounds INTEGER,
  nt_c_wallet DOUBLE PRECISION, nt_c_xirr DOUBLE PRECISION,
  nt_c_out DOUBLE PRECISION, nt_c_rounds INTEGER,
  nt_d_wallet DOUBLE PRECISION, nt_d_xirr DOUBLE PRECISION,
  nt_d_out DOUBLE PRECISION, nt_d_rounds INTEGER
);

CREATE TABLE IF NOT EXISTS fund_bear_pause_market (
  subset       VARCHAR(16) NOT NULL,
  mode         VARCHAR(4) NOT NULL,
  n            INTEGER,
  med_out_a    DOUBLE PRECISION,
  med_wallet_a DOUBLE PRECISION,
  xirr_a_med   DOUBLE PRECISION,
  xirr_a_p25   DOUBLE PRECISION,
  xirr_a_p75   DOUBLE PRECISION,
  b_xirr_med DOUBLE PRECISION, b_xirr_lower_pct DOUBLE PRECISION,
  b_wallet_delta_med DOUBLE PRECISION, b_wallet_delta_p25 DOUBLE PRECISION,
  b_wallet_delta_p75 DOUBLE PRECISION, b_wallet_lower_pct DOUBLE PRECISION,
  c_xirr_med DOUBLE PRECISION, c_xirr_lower_pct DOUBLE PRECISION,
  c_wallet_delta_med DOUBLE PRECISION, c_wallet_delta_p25 DOUBLE PRECISION,
  c_wallet_delta_p75 DOUBLE PRECISION, c_wallet_lower_pct DOUBLE PRECISION,
  d_xirr_med DOUBLE PRECISION, d_xirr_lower_pct DOUBLE PRECISION,
  d_wallet_delta_med DOUBLE PRECISION, d_wallet_delta_p25 DOUBLE PRECISION,
  d_wallet_delta_p75 DOUBLE PRECISION, d_wallet_lower_pct DOUBLE PRECISION,
  PRIMARY KEY (subset, mode)
);

CREATE TABLE IF NOT EXISTS fund_bear_pause_meta (
  id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fund_count   INTEGER,
  params       JSONB
);
"""

# 元组列序契约（scripts/bear_pause_import.py 按此序构造行；键名与引擎
# bear_pause.run_fund / aggregate_market 产出的扁平 dict 一致）
BEAR_PAUSE_FUND_COLS = (["code", "span_days", "months", "ever_paused",
                         "episodes", "pause_months", "dd_end", "dd_min",
                         "in_universe"]
                        + [f"{p}_{a}_{k}" for p in ("tp", "nt")
                           for a in ("a", "b", "c", "d")
                           for k in ("wallet", "xirr", "out", "rounds")])
BEAR_PAUSE_MARKET_COLS = (["subset", "mode", "n", "med_out_a", "med_wallet_a",
                           "xirr_a_med", "xirr_a_p25", "xirr_a_p75"]
                          + [f"{a}_{k}" for a in ("b", "c", "d")
                             for k in ("xirr_med", "xirr_lower_pct",
                                       "wallet_delta_med", "wallet_delta_p25",
                                       "wallet_delta_p75", "wallet_lower_pct")])
_BEAR_PAUSE_FUND_INSERT = f"""
INSERT INTO fund_bear_pause_fund ({", ".join(BEAR_PAUSE_FUND_COLS)})
VALUES ({", ".join(["%s"] * len(BEAR_PAUSE_FUND_COLS))})
"""
_BEAR_PAUSE_MARKET_INSERT = f"""
INSERT INTO fund_bear_pause_market ({", ".join(BEAR_PAUSE_MARKET_COLS)})
VALUES ({", ".join(["%s"] * len(BEAR_PAUSE_MARKET_COLS))})
"""
_BEAR_PAUSE_META_UPSERT = """
INSERT INTO fund_bear_pause_meta (id, generated_at, fund_count, params)
VALUES (TRUE, now(), %s, %s::jsonb)
ON CONFLICT (id) DO UPDATE SET
  generated_at = now(), fund_count = EXCLUDED.fund_count,
  params = EXCLUDED.params
"""


def ensure_bear_pause_schema(conn) -> None:
    """断缴实验三表幂等建表（启动清单 + 导入脚本双保险）。"""
    with conn.cursor() as cur:
        cur.execute(BEAR_PAUSE_DDL)


def import_bear_pause(conn, fund_rows: list[tuple],
                      market_rows: list[tuple],
                      *, params: dict | None = None) -> tuple[int, int]:
    """断缴实验三表整表替换（重跑后旧数据不得残留）；事务由调用方提交。

    返回 (基金行数, 市场级行数)。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_bear_pause_fund")
        if fund_rows:
            cur.executemany(_BEAR_PAUSE_FUND_INSERT, fund_rows)
        cur.execute("DELETE FROM fund_bear_pause_market")
        if market_rows:
            cur.executemany(_BEAR_PAUSE_MARKET_INSERT, market_rows)
        cur.execute(_BEAR_PAUSE_META_UPSERT,
                    (len(fund_rows),
                     json.dumps(params or {}, ensure_ascii=False)))
    return len(fund_rows), len(market_rows)


def import_rows(conn, rows: list[dict], *, params: dict | None = None) -> int:
    """快照整体替换（DELETE+INSERT）+ meta 单行 upsert；单事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_result")
        cur.executemany(_INSERT, [tuple(r[c] for c in _COLS) for r in rows])
        cur.execute(_META_UPSERT, (len(rows), json.dumps(params or {}, ensure_ascii=False)))
    return len(rows)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_MIGRATE_ROUNDS_V2)   # v1 轮次表（无 fee 列）一次性弃表重建
        cur.execute(DDL)


def main():
    ap = argparse.ArgumentParser(description="导入定投止盈回测 CSV 到 fund_dca_result")
    ap.add_argument("csv", help="fund_dca_backtest.csv 路径")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL",
                     "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"))
    ap.add_argument("--amount", type=float, default=1000.0, help="月定投金额（元）")
    ap.add_argument("--take-profit", type=float, default=0.20, help="止盈线（0.2=20%%）")
    ap.add_argument("--min-age-years", type=float, default=0.0,
                    help="运作年限门槛（年，0=不过滤；仅写入 meta 供页面展示口径）")
    ap.add_argument("--min-scale-yi", type=float, default=None,
                    help="规模下限（亿元；不传=全市场，仅写入 meta 供页面展示口径）")
    ap.add_argument("--rounds-csv", default=None,
                    help="止盈轮次 CSV（缺省=结果 CSV 同目录 fund_dca_rounds.csv，无则跳过）")
    args = ap.parse_args()

    rows = parse_csv(args.csv)
    rounds_path = (Path(args.rounds_csv) if args.rounds_csv else
                   Path(args.csv).parent / "fund_dca_rounds.csv")
    rounds = parse_rounds_csv(rounds_path) if rounds_path.exists() else []
    latest = max((r["full_end"] for r in rows if r["full_end"]), default=None)
    params = {"amount": args.amount, "take_profit": args.take_profit,
              "scale_min_yi": args.min_scale_yi, "frequency": "monthly",
              "min_age_years": args.min_age_years or None,
              "window_end": latest.isoformat() if latest else None,
              "source": "scripts/fund_dca_backtest.py"}
    with psycopg.connect(args.dsn, connect_timeout=10) as conn:
        ensure_schema(conn)                    # 幂等：本地/新库首导直接可用
        n = import_rows(conn, rows, params=params)
        nr = import_rounds(conn, rounds) if rounds else 0
        conn.commit()
    alive = sum(1 for r in rows if r["alive"])
    print(f"导入 {n} 行（在市 {alive}）→ fund_dca_result，窗口截至 {latest}；"
          f"止盈轮次 {nr} 行")


if __name__ == "__main__":
    main()

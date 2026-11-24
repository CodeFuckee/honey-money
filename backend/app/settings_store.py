"""应用级键值设置（app_settings 表）。

当前唯一设置项 backtest_min_span_days：回测年化的基金运作期限下限
（天），替代 dca_backtest.MIN_SPAN_DAYS 常量的两处判定（全窗口年化与
止盈后当前轮年化，同值联动）。取值 90~3650、默认 90、不可关闭
（2026-11-24 用户定案）；修改后下一次回测生效（存量行不重算）。
本模块不依赖任何业务模块（default 由调用方传引擎常量，防循环导入）。
"""

SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

KEY_MIN_SPAN_DAYS = "backtest_min_span_days"
MIN_SPAN_FLOOR = 90
MIN_SPAN_CEIL = 3650


def ensure_settings_schema(conn) -> None:
    """幂等建表（启动兜底清单与设置页保存路径共用）。"""
    with conn.cursor() as cur:
        cur.execute(SETTINGS_DDL)


def get_setting(conn, key: str, default=None):
    """读单键原值；缺行返回 default。"""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return default if row is None else row[0]


def set_setting(conn, key: str, value: str) -> None:
    """写单键（upsert；提交由调用方事务完成）。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()", (key, value))


def get_min_span_days(conn, default: int = 90) -> int:
    """运作期限下限：缺行/坏值/越界一律回落 default（防手改库炸回测）。"""
    raw = get_setting(conn, KEY_MIN_SPAN_DAYS)
    if raw is None:
        return default
    try:
        value = validate_min_span_days(raw)
    except ValueError:
        return default
    return value


def validate_min_span_days(raw) -> int:
    """设置入口校验：整数且在 [90, 3650]；bool/浮点/字符串数字超界均拒。"""
    if isinstance(raw, bool):
        raise ValueError(f"运作期限天数须为整数天数：{raw!r}")
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f"运作期限天数须为整数天数：{raw!r}")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"运作期限天数须为整数天数：{raw!r}") from None
    if not (MIN_SPAN_FLOOR <= value <= MIN_SPAN_CEIL):
        raise ValueError(
            f"运作期限天数须在 {MIN_SPAN_FLOOR}~{MIN_SPAN_CEIL} 之间：{value}")
    return value

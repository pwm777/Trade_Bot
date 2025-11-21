""" backtest_setup.py
🔍 Находит доступные исторические данные (через SQLAlchemy) в базе MARKET_DB_DSN.
🕒 Определяет диапазон дат (MIN/MAX ts) для выбранных символов и таймфрейма (10s, 1m, 5m).
⚙️ Формирует конфиг для режима BACKTEST, включая параметры стратегии, скорость воспроизведения (BACKTEST_SPEED) и автоостановку.
📊 Печатает отчёт о доступных данных (по символам и общему диапазону).
"""
import config as cfg
from typing import Tuple, Optional, List, Dict
from datetime import datetime, UTC
from sqlalchemy import create_engine, text, inspect, bindparam
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.exc import SQLAlchemyError


# ----------------- Settings & Defaults -----------------
_DEFAULT_TABLES: Dict[str, str] = {
    "1m":  "candles_1m",
    "5m":  "candles_5m",
}
_PRIORITY = ["1m", "5m"]  # выбор по приоритету, если cfg.BACKTEST_TIMEFRAME не задан


# ----------------- Helpers (SQLAlchemy) -----------------
def _get_engine() -> Engine:
    """
    Возвращает SQLAlchemy Engine по DSN из cfg.MARKET_DB_DSN.
    Пример DSN: 'sqlite:///data/market.db'
    """
    dsn = getattr(cfg, "MARKET_DB_DSN", None)
    if not isinstance(dsn, str) or not dsn:
        raise ValueError("Invalid or missing cfg.MARKET_DB_DSN")
    return create_engine(dsn, future=True)

def _table_exists(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def _columns_exist(engine: Engine, table: str, columns: List[str]) -> bool:
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns(table)}
    return all(c in cols for c in columns)


def _resolve_tables_config() -> Dict[str, str]:
    """
    Берём mapping таймфрейм→таблица из cfg.MARKET_CANDLES_TABLES, иначе дефолт.
    """
    user_map = getattr(cfg, "MARKET_CANDLES_TABLES", None)
    if isinstance(user_map, dict) and user_map:
        # нормализуем ключи
        norm = {str(k).lower(): v for k, v in user_map.items()}
        # поддержим и без lower
        return {
            "10s": norm.get("10s", user_map.get("10s", _DEFAULT_TABLES["10s"])),
            "1m":  norm.get("1m",  user_map.get("1m",  _DEFAULT_TABLES["1m"])),
            "5m":  norm.get("5m",  user_map.get("5m",  _DEFAULT_TABLES["5m"])),
        }
    return _DEFAULT_TABLES.copy()


def _detect_timeframe_and_table(engine: Engine, preferred_tf: Optional[str]) -> Tuple[str, str]:
    """
    Выбирает таймфрейм и таблицу:
      1) Пытается предпочитаемый (cfg.BACKTEST_TIMEFRAME), если таблица существует и имеет колонки [symbol, ts].
      2) Иначе — первый доступный по PRIORITY.
    """
    tables_map = _resolve_tables_config()

    def is_valid(table: str) -> bool:
        return _table_exists(engine, table) and _columns_exist(engine, table, ["symbol", "ts"])

    # 1) Preferred
    if preferred_tf:
        preferred_tf = preferred_tf.strip().lower()
        if preferred_tf in ("10s", "1m", "5m"):
            tname = tables_map[preferred_tf]
            if is_valid(tname):
                return preferred_tf, tname

    # 2) Fallback by priority
    for tf in _PRIORITY:
        tname = tables_map[tf]
        if is_valid(tname):
            return tf, tname

    raise RuntimeError(
        "No suitable candles table found. "
        "Checked (10s, 1m, 5m) with required columns [symbol, ts]. "
        "Configure cfg.MARKET_CANDLES_TABLES or create the tables."
    )


def _filter_symbols_present(conn: Connection, table: str, symbols: List[str]) -> List[str]:
    """
    Возвращает подмножество symbols, реально присутствующее в таблице.
    """
    if not symbols:
        return []

    q = text(f"""
        SELECT DISTINCT symbol
        FROM {table}
        WHERE symbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    rows = conn.execute(q, {"symbols": symbols}).scalars().all()
    present = set(rows)
    return [s for s in symbols if s in present]


# ----------------- Public API -----------------
def get_available_data_range(symbols: list = None,
                             timeframe: Optional[str] = None) -> Tuple[Optional[int], Optional[int]]:
    """
    ✅ ИСПРАВЛЕНО: Начинаем бэктест с 101-го бара 5m (после warmup периода)
    """
    if symbols is None:
        symbols = ["ETHUSDT"]

    try:
        engine = _get_engine()

        with engine.connect() as conn:
            # ✅ Получаем диапазон 5m данных
            table_5m = "candles_5m"

            if not _table_exists(engine, table_5m):
                print(f"❌ Table {table_5m} not found")
                return None, None

            present_symbols = _filter_symbols_present(conn, table_5m, symbols)
            if not present_symbols:
                print(f"❌ No 5m data for symbols: {symbols}")
                return None, None

            # ✅ НОВАЯ ЛОГИКА: Получаем 101-ю свечу как стартовую точку
            q_warmup = text(f"""
                SELECT ts, ts_close
                FROM {table_5m}
                WHERE symbol IN :symbols
                ORDER BY ts ASC
                LIMIT 1 OFFSET 100
            """).bindparams(bindparam("symbols", expanding=True))

            row_start = conn.execute(q_warmup, {"symbols": present_symbols}).mappings().first()

            if not row_start:
                print("❌ Not enough 5m data (need at least 101 candles for warmup)")
                return None, None

            start_ts = row_start["ts"]

            # ✅ Получаем последнюю свечу
            q_end = text(f"""
                SELECT MAX(ts) AS end_ts, COUNT(*) AS total
                FROM {table_5m}
                WHERE symbol IN :symbols
            """).bindparams(bindparam("symbols", expanding=True))

            row_end = conn.execute(q_end, {"symbols": present_symbols}).mappings().one()
            end_ts = row_end["end_ts"]
            total_candles = row_end["total"]

            if not end_ts or start_ts >= end_ts:
                print("❌ Invalid time range after warmup")
                return None, None

            # ✅ Статистика
            print("📊 Backtest data range (after 100-bar 5m warmup):")
            print("-" * 60)

            # Показываем начало БД
            q_first = text(f"""
                SELECT MIN(ts) AS first_ts
                FROM {table_5m}
                WHERE symbol IN :symbols
            """).bindparams(bindparam("symbols", expanding=True))
            first_ts = conn.execute(q_first, {"symbols": present_symbols}).mappings().one()["first_ts"]

            first_date = datetime.fromtimestamp(first_ts / 1000, tz=UTC)
            start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC)
            end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC)

            warmup_hours = (start_ts - first_ts) / (1000 * 60 * 60)
            duration_hours = (end_ts - start_ts) / (1000 * 60 * 60)
            duration_days = duration_hours / 24

            print(f"📅 First 5m candle in DB: {first_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"⏩ Skipping warmup:       {warmup_hours:.1f} hours (100 bars × 5min)")
            print(f"🚀 Backtest starts:       {start_date.strftime('%Y-%m-%d %H:%M:%S')} UTC (bar #101)")
            print(f"🏁 Backtest ends:         {end_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"⏱️  Duration:              {duration_hours:.1f} hours ({duration_days:.1f} days)")
            print(f"📊 Total 5m candles:      {int(total_candles - 100):,} (excluding warmup)")
            print("-" * 60)

            return int(start_ts), int(end_ts)

    except Exception as e:
        print(f"❌ Error checking data range: {e}")
        import traceback
        traceback.print_exc()
        return None, None

async def build_backtest_config() -> dict:
    """
    Конфиг backtest с использованием полного диапазона данных и выбранного таймфрейма (20s/1m/5m).
    Не использует текущую дату — только границы данных в БД.
    """
    # ✅ СОЗДАЁМ TradingLogger ДО вызова build_runtime_config
    from trading_logger import TradingLogger
    import logging

    logger = logging.getLogger("BacktestSetup")

    # Создаём временный TradingLogger для получения engine
    market_db_path = cfg.MARKET_DB_DSN.replace("sqlite:///", "")
    trading_db_path = cfg.TRADING_DB_DSN.replace("sqlite:///", "")

    trading_logger = TradingLogger(
        market_db_path=market_db_path,
        trades_db_path=trading_db_path,
        on_alert=lambda level, data: None,
        pool_size=4,
        enable_async=False,  # ✅ Отключаем async для простоты
        logger_instance=logger
    )

    # ✅ ТЕПЕРЬ передаём trading_logger в build_runtime_config
    runtime_cfg = await cfg.build_runtime_config(trading_logger=trading_logger)

    test_symbols = list(runtime_cfg.get("symbols") or getattr(cfg, "TRADING_SYMBOLS", []) or [])
    if not test_symbols:
        raise ValueError("No trading symbols configured for backtest")

    # берём tf из конфигурации, если задан
    tf = getattr(cfg, "BACKTEST_TIMEFRAME", None)
    tf = tf.strip().lower() if isinstance(tf, str) and tf else None

    print("🔍 Analyzing complete historical data range...")
    start_ts, end_ts = get_available_data_range(test_symbols, timeframe=tf)

    if not start_ts or not end_ts:
        raise ValueError("❌ No historical data available for backtest. Run bot in DEMO mode first to collect data.")

    start_date = datetime.fromtimestamp(start_ts / 1000.0, tz=UTC)
    end_date = datetime.fromtimestamp(end_ts / 1000.0, tz=UTC)
    duration_days = (end_ts - start_ts) / (1000 * 60 * 60 * 24)

    print("\n✅ Using complete data range for backtest:")
    print(f"   📅 Start: {start_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   📅 End:   {end_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   ⏱️  Duration: {duration_days:.1f} days")

    backtest_speed = float(getattr(cfg, "BACKTEST_SPEED", 1.0))
    strategy_params = getattr(cfg, "STRATEGY_PARAMS", {}).get("CornEMA", {})

    runtime_cfg.update({
        "execution_mode": "BACKTEST",
        "symbols": test_symbols,
        "trading_symbols": test_symbols,
        "enable_trading": True,
        "backtest": {
            "start_time_ms": int(start_ts),
            "end_time_ms": int(end_ts),
            "speed": backtest_speed,
            "data_source": "database",
            "auto_shutdown": True,
            "period_description": f"Complete historical range ({duration_days:.1f} days)",
            "timeframe": tf or "auto",  # для прозрачности в логах
        },
        "strategy": {
            "name": "CornEMA",
            "parameters": strategy_params,
            "history_window": 50
        }
    })

    return runtime_cfg

if __name__ == "__main__":
    try:
        config = build_backtest_config()
        print("\n🧪 Backtest Configuration Ready!")
        print(f"📊 Period: {config['backtest']['period_description']}")
        print(f"🕒 Timeframe: {config['backtest'].get('timeframe')}")
        print(f"🎯 Symbols: {config['symbols']}")
    except Exception as e:
        print(f"❌ Error: {e}")

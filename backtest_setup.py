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
_PRIORITY = ["10s", "1m", "5m"]  # выбор по приоритету, если cfg.BACKTEST_TIMEFRAME не задан


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
    Определяет доступный диапазон исторических данных из БД для заданного (или авто-выбранного) таймфрейма.
    Использует MIN/MAX по таблице свечей, без привязки к текущей дате.

    Args:
        symbols: список символов для сечения.
        timeframe: "10s" | "1m" | "5m" | None. Если None — берём cfg.BACKTEST_TIMEFRAME, иначе авто-выбор по приоритету.

    Returns:
        (start_ts_ms, end_ts_ms) или (None, None) если данных нет.
    """
    if symbols is None:
        symbols = ["SOLUSDT", "ETHUSDT"]

    # нормализуем timeframe
    tf = (timeframe or getattr(cfg, "BACKTEST_TIMEFRAME", None))
    tf = tf.strip().lower() if isinstance(tf, str) and tf else None

    try:
        engine = _get_engine()
        chosen_tf, table = _detect_timeframe_and_table(engine, tf)

        with engine.connect() as conn:
            # ограничить символами, реально присутствующими
            present_symbols = _filter_symbols_present(conn, table, symbols)
            if not present_symbols:
                print(f"❌ No historical data found in database for requested symbols: {symbols} (table={table})")
                return None, None

            q_total = text(f"""
                SELECT MIN(ts) AS start_ts, MAX(ts) AS end_ts, COUNT(*) AS total_candles
                FROM {table}
                WHERE symbol IN :symbols
            """).bindparams(bindparam("symbols", expanding=True))

            row = conn.execute(q_total, {"symbols": present_symbols}).mappings().one()
            start_ts, end_ts, total_candles = row["start_ts"], row["end_ts"], row["total_candles"]

            if not total_candles or not start_ts or not end_ts:
                print(f"❌ No historical data found in database (empty range) for table={table}")
                return None, None

            # Статистика по символам
            print("📊 Available historical data:")
            print("-" * 60)
            q_symbol = text(f"""
                SELECT COUNT(*) AS c, MIN(ts) AS mn, MAX(ts) AS mx
                FROM {table}
                WHERE symbol = :symbol
            """)
            for symbol in present_symbols:
                rs = conn.execute(q_symbol, {"symbol": symbol}).mappings().one()
                count, min_ts, max_ts = rs["c"], rs["mn"], rs["mx"]
                if count and min_ts and max_ts:
                    min_date = datetime.fromtimestamp(min_ts / 1000.0, tz=UTC)
                    max_date = datetime.fromtimestamp(max_ts / 1000.0, tz=UTC)
                    hours_coverage = (max_ts - min_ts) / (1000 * 60 * 60)
                    print(f"📈 {symbol}: {count:,} candles")
                    print(f"   🕒 TF:   {chosen_tf}")
                    print(f"   📅 From: {min_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    print(f"   📅 To:   {max_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    print(f"   ⏱️  Coverage: {hours_coverage:.1f} hours ({hours_coverage / 24:.1f} days)\n")
                else:
                    print(f"❌ {symbol}: No data available in {table}")

            # Общая статистика
            start_date = datetime.fromtimestamp(start_ts / 1000.0, tz=UTC)
            end_date = datetime.fromtimestamp(end_ts / 1000.0, tz=UTC)
            total_hours = (end_ts - start_ts) / (1000 * 60 * 60)

            print("-" * 60)
            print(f"📊 Complete historical range (table: {table}, timeframe: {chosen_tf}):")
            print(f"   📅 Start: {start_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"   📅 End:   {end_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"   ⏱️  Duration: {total_hours:.1f} hours ({total_hours / 24:.1f} days)")
            print(f"   🔢 Total candles: {int(total_candles):,}")
            print("-" * 60)

            return int(start_ts), int(end_ts)

    except SQLAlchemyError as ext:
        print(f"❌ SQLAlchemy error while checking data range: {ext}")
        return None, None
    except Exception as ext:
        print(f"❌ Error checking data range: {ext}")
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

"""
market_data_utils.py - Утилиты для расчета индикаторов и работы с данными.
Содержит вычислительные функции, ensure_market_schema и DAO-методы для таблиц candles_1m и candles_5m.
"""

from __future__ import annotations
import time
import asyncio
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import  create_async_engine
from sqlalchemy import text
import logging
from typing import  List, Tuple, Any
import pandas as pd
import numpy as np
from config import TABLES
from dataclasses import dataclass
from typing import Optional
import statistics
from dataclasses import asdict,field
from sqlalchemy.ext.asyncio import AsyncEngine
from iqts_standards import FEATURE_NAME_MAP
from datetime import datetime, timedelta, timezone, UTC
from tqdm import tqdm

@dataclass
class CusumConfig:
    """Конфигурация CUSUM расчета"""
    normalize_window: int = 50
    eps: float = 0.5          # порог для z → BUY/SELL/HOLD
    h: float = 0.5            # чувствительность: k_t = h * rolling_sigma(Δclose)
    z_to_conf: float = 1.0    # множитель для confidence
    # Конфиги для разных тайм-фреймов
CUSUM_CONFIG_1M = CusumConfig(normalize_window=50, eps=0.5, h=0.7, z_to_conf=1.4)
# normalize_window_5m = 3*normalize_window_1m
CUSUM_CONFIG_5M = CusumConfig(normalize_window=100, eps=0.5, h=0.5, z_to_conf=1.0)

@dataclass
class CalculationMetrics:
    """Метрики расчета индикаторов"""
    symbol: str            # Добавляем поле symbol
    started_at: datetime
    completed_at: Optional[datetime] = None
    indicators_count: int = 0
    rows_processed: int = 0
    errors_count: int = 0
    duration_ms: float = 0.0

    def complete(self) -> None:
        self.completed_at = datetime.now(UTC)
        if self.started_at:
            self.duration_ms = (self.completed_at - self.started_at).total_seconds() * 1000
@dataclass
class IndicatorConfig:
    """Конфигурация периодов индикаторов"""

    ema_periods: List[int] = field(default_factory=lambda: [3, 7, 9, 15, 30])
    price_change_periods: List[int] = field(default_factory=lambda: [5, 20])
    cmo_period: int = 14
    adx_period: int = 14
    atr_period: int = 14
    macd_periods: Tuple[int, int, int] = (12, 26, 9)
    bb_period: int = 20
    vwap_period: int = 96
# =============================================================================
#  СХЕМА БД
# =============================================================================
# --- helpers for schema migrations -----------------------------------
from typing import Dict, Set
from sqlalchemy import text

def _table_columns(conn, table_name: str) -> Set[str]:
    """
    Возвращает множество существующих колонок таблицы (по PRAGMA table_info).
    """
    cols: Set[str] = set()
    try:
        res = conn.execute(text(f"PRAGMA table_info({table_name})"))
        # у sqlite у pragma table_info есть поле "name"
        for row in res.mappings():
            n = row.get("name")
            if isinstance(n, str):
                cols.add(n)
    except Exception as e:
        # логгер может называться иначе у вас — при необходимости замените
        try:
            print(f"[schema] failed to read columns for {table_name}: {e}")
        except Exception:
            pass
    return cols

def _add_missing_columns(conn, table_name: str, required_cols: Dict[str, str]) -> None:
    """
    Добавляет недостающие колонки в таблицу (idempotent).
    required_cols: {column_name: sql_type}
    """
    if not table_name or not required_cols:
        return
    existing = _table_columns(conn, table_name)
    for col, col_type in required_cols.items():
        if col in existing:
            continue
        try:
            conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN "{col}" {col_type}'))
            # можно залогировать успешное добавление
            # print(f"[schema] added column {col} {col_type} to {table_name}")
        except Exception as e:
            # если колонка уже есть/тип несовместим — просто сообщаем и идём дальше
            try:
                print(f"[schema] add column failed {table_name}.{col}: {e}")
            except Exception:
                pass


def ensure_market_schema(engine: Engine, logger: Optional[logging.Logger] = None) -> None:
    _log = logger or logging.getLogger("ensure_market_schema")

    # --- безопасно читаем имена таблиц из TABLES ---
    try:
        t1m = TABLES.get("candles_1m")  # может вернуть None
        t5m = TABLES.get("candles_5m")
    except Exception:
        t1m = None
        t5m = None

    if not t1m or not t5m:
        _log.warning("TABLES missing 'candles_1m'/'candles_5m'; using defaults 'candles_1m'/'candles_5m'.")
        t1m = t1m or "candles_1m"
        t5m = t5m or "candles_5m"

    # --- утилиты для сборки DDL и required_cols из FEATURE_NAME_MAP ---
    def _feature_cols_sql(tf: str) -> str:
        """Строка 'col TYPE' для секции фич конкретного ТФ на основе FEATURE_NAME_MAP."""
        fmap: Dict[str, Tuple[str, str]] = FEATURE_NAME_MAP.get(tf, {})
        if not fmap:
            _log.warning("FEATURE_NAME_MAP has no entries for tf=%s; DDL will contain only core columns.", tf)
            return ""
        return ",\n      " + ",\n      ".join(f"{col} {typ}" for _, (col, typ) in fmap.items())

    def _feature_required_cols(tf: str) -> Dict[str, str]:
        """Словарь {db_col: type} для требуемых фич конкретного ТФ."""
        fmap: Dict[str, Tuple[str, str]] = FEATURE_NAME_MAP.get(tf, {})
        return {col: typ for _, (col, typ) in fmap.items()}

    # --- CORE колонки (общие для всех ТФ) ---
    core_cols: Dict[str, str] = {
        "symbol": "TEXT", "ts": "INTEGER", "ts_close": "INTEGER",
        "open": "REAL", "high": "REAL", "low": "REAL", "close": "REAL",
        "volume": "REAL", "count": "INTEGER", "quote": "REAL",
        "finalized": "INTEGER", "checksum": "TEXT", "created_ts": "INTEGER",
    }

    # --- DDL с подстановкой фич из FEATURE_NAME_MAP ---
    ddl_1m = f"""
    CREATE TABLE IF NOT EXISTS {t1m} (
      symbol      TEXT    NOT NULL,
      ts          INTEGER NOT NULL,
      ts_close    INTEGER,
      open        REAL, high REAL, low REAL, close REAL,
      volume      REAL, count INTEGER, quote REAL,
      finalized   INTEGER DEFAULT 1,
      checksum    TEXT,
      created_ts  INTEGER{_feature_cols_sql("1m")},
      PRIMARY KEY(symbol, ts)
    );
    """
    ddl_5m = f"""
    CREATE TABLE IF NOT EXISTS {t5m} (
      symbol              TEXT    NOT NULL,
      ts                  INTEGER NOT NULL,
      ts_close            INTEGER,
      open REAL, high REAL, low REAL, close REAL,
      volume REAL, count INTEGER, quote REAL,
      finalized INTEGER DEFAULT 1,
      checksum  TEXT,
      created_ts INTEGER{_feature_cols_sql("5m")},
      PRIMARY KEY(symbol, ts)
    );
    """

    # --- индексы ---
    idx_1m = f'CREATE INDEX IF NOT EXISTS idx_{t1m}_symbol_ts ON {t1m}(symbol, ts);'
    idx_5m = f'CREATE INDEX IF NOT EXISTS idx_{t5m}_symbol_ts ON {t5m}(symbol, ts);'

    # --- required_cols: CORE + ФИЧИ из FEATURE_NAME_MAP ---
    required_cols_1m: Dict[str, str] = {**core_cols, **_feature_required_cols("1m")}
    required_cols_5m: Dict[str, str] = {**core_cols, **_feature_required_cols("5m")}

    with engine.begin() as conn:
        # ✅ ВКЛЮЧАЕМ WAL MODE И ОПТИМИЗАЦИИ
        try:
            # Проверяем текущий режим
            result = conn.execute(text("PRAGMA journal_mode"))
            current_mode = result.scalar()
            _log.info(f"Current SQLite journal mode: {current_mode}")

            # Включаем WAL mode
            result = conn.execute(text("PRAGMA journal_mode=WAL"))
            new_mode = result.scalar()
            _log.info(f"New SQLite journal mode: {new_mode}")

            # Оптимизации для WAL mode
            conn.execute(text("PRAGMA synchronous=NORMAL"))  # Баланс скорости/надежности
            conn.execute(text("PRAGMA journal_size_limit=67108864"))  # 64MB лимит WAL файла
            conn.execute(text("PRAGMA cache_size=-64000"))  # 64MB кэш
            conn.execute(text("PRAGMA busy_timeout=5000"))  # 5 секунд timeout при блокировках

            _log.info("✅ WAL mode and optimizations applied successfully")

        except Exception as e:
            _log.warning(f"⚠️ Failed to set WAL mode: {e}. Continuing with default mode.")

        # создаём таблицы
        conn.execute(text(ddl_1m))
        conn.execute(text(ddl_5m))


        # индексы
        conn.execute(text(idx_1m))
        conn.execute(text(idx_5m))


        # добавляем недостающие колонки (миграции)
        _add_missing_columns(conn, t1m, required_cols_1m)
        _add_missing_columns(conn, t5m, required_cols_5m)


# =============================================================================
#  ОСНОВНОЙ КЛАСС УТИЛИТ
# =============================================================================
def _cusum_online_delta_closes_with_z(
    closes: pd.Series,
    normalize_window: int = 50,
    eps: float = 0.5,     # порог для z → BUY/SELL/HOLD
    h: float = 0.5,       # чувствительность: k_t = h * rolling_sigma(Δclose)
    z_to_conf: float = 1.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """
    CUSUM по Δclose с анти-look-ahead нормализацией:
      - динамический порог k_t = h * σ_t(Δclose), где σ_t посчитана на прошлом окне
      - z-score также нормализуется по прошлому окну (shift(1))
    Возвращает:
      s     : накопитель CUSUM (Series[float])
      z     : z-score CUSUM (Series[float])
      state : 1=BUY, -1=SELL, 0=HOLD (Series[int])
      conf  : |z| * z_to_conf (Series[float])
    """
    closes = closes.astype(float)
    diffs = closes.diff().fillna(0.0)

    # Динамический порог по прошлому окну (без подсмотра вперёд)
    roll_sigma = diffs.rolling(normalize_window, min_periods=normalize_window).std(ddof=0).shift(1)
    k = (h * roll_sigma).fillna(0.0).to_numpy()

    s_up = 0.0
    s_dn = 0.0
    vals = []

    diffs_np = diffs.to_numpy()
    for x, k_i in zip(diffs_np, k):
        # односторонние накопители с порогом k_i
        s_up = max(0.0, s_up + x - k_i)
        s_dn = min(0.0, s_dn + x + k_i)
        vals.append(s_up if abs(s_up) >= abs(s_dn) else s_dn)

    s = pd.Series(vals, index=closes.index, dtype=float)

    # Анти-look-ahead нормализация CUSUM по прошлому окну
    roll = s.rolling(normalize_window, min_periods=normalize_window)
    mean = roll.mean().shift(1)
    std = roll.std(ddof=0).shift(1).replace(0.0, np.nan)

    z = (s - mean) / std
    z = z.fillna(0.0)

    state_arr = np.where(z > eps, 1, np.where(z < -eps, -1, 0)).astype(np.int8)
    state = pd.Series(state_arr, index=s.index)
    conf = z.abs() * float(z_to_conf)

    return s, z, state, conf

class MarketDataUtils:
    """
    Вычисления индикаторов и операции чтения/записи для candles_1m и candles_5m.
    ВНИМАНИЕ: все операции с БД переведены на AsyncEngine (sqlite+aiosqlite).
    Синхронный self.engine сохранён только для ensure_market_schema(...).
    """
    def __init__(self, market_engine: Engine, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        # Сохраняем синхронный Engine (для существующего ensure_market_schema)
        self.engine: Engine = market_engine

        self.cusum_config_1m = CUSUM_CONFIG_1M
        self.cusum_config_5m = CUSUM_CONFIG_5M
        # Создаём асинхронный движок по тому же DSN
        # Преобразуем sqlite:/// → sqlite+aiosqlite:///
        dsn = str(getattr(market_engine, "url", "sqlite:///market.sqlite"))
        if dsn.startswith("sqlite:///") and not dsn.startswith("sqlite+aiosqlite:///"):
            dsn = dsn.replace("sqlite:///", "sqlite+aiosqlite:///")
        self.aengine = create_async_engine(dsn, future=True)

        # Кеши и конфиг как раньше
        self._cache_1m: Dict[str, List[dict]] = {}
        self._cusum_1m_state: Dict[str, dict] = {}
        self.market_engine = market_engine     # сохраняем внешний движок
        self._engine = market_engine           # чтобы не падали старые методы
        self.cfg = {
            "features": {
                "required_warmup_5m": 96,  # из-за rolling VWAP(96)
                "cusum_1m": {
                    "min_warmup": 120,
                    "min_warmup_gap": 60,
                    "period": 14,
                    "normalize_window": 50,
                    "z_to_conf": 1.0,
                },
            },
            "incremental": {
                "last_k_5m": 270,  # 270 окно должно начинаться со 150
                "tail_1m_for_update": 200,
            },
        }
        self.indicator_config = IndicatorConfig()

        # Метрики
        self._metrics: Dict[str, CalculationMetrics] = {}

        # Версия и автор
        self.version = "1.0.0"
        self.created_at = datetime.now(UTC)
        self.created_by = "pwm777"

        self.logger.info(
            f"MarketDataUtils v{self.version} initialized by {self.created_by} "
            f"at {self.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        self.PHASE_1M = 50_000  # 1m свечи начинаются на 0:50
        self.PHASE_5M = 290_000  # 5m свечи начинаются на 4:50
        self.ONE_M_MS = 60_000
        self.FIVE_M_MS = 300_000

        ensure_market_schema(self.engine, self.logger)

    def calculate_cusum(self, closes: pd.Series, config: CusumConfig) -> dict[str, pd.Series]:
        """
        Единая функция расчета CUSUM с конфигурируемыми параметрами.
        ✅ ИСПРАВЛЕНИЯ:
        1. Использовать config.eps вместо жесткого значения
        2. Добавить логирование при недостаточных данных
        """
        closes = closes.astype(float)
        diffs = closes.diff().fillna(0.0)

        # Динамический порог по прошлому окну (без подсмотра вперёд)
        roll_sigma = diffs.rolling(config.normalize_window, min_periods=config.normalize_window).std(ddof=0).shift(1)
        k = (config.h * roll_sigma).fillna(0.0).to_numpy()

        s_up = 0.0
        s_dn = 0.0
        vals = []
        pos_vals = []
        neg_vals = []

        diffs_np = diffs.to_numpy()

        for i, (x, k_i) in enumerate(zip(diffs_np, k)):
            # односторонние накопители с порогом k_i
            s_up = max(0.0, s_up + x - k_i)
            s_dn = min(0.0, s_dn + x + k_i)

            vals.append(s_up if abs(s_up) >= abs(s_dn) else s_dn)
            pos_vals.append(s_up)
            neg_vals.append(s_dn)

        s = pd.Series(vals, index=closes.index, dtype=float)
        cusum_pos = pd.Series(pos_vals, index=closes.index, dtype=float)
        cusum_neg = pd.Series(neg_vals, index=closes.index, dtype=float)

        # Анти-look-ahead нормализация CUSUM по прошлому окну
        roll = s.rolling(config.normalize_window, min_periods=config.normalize_window)
        mean = roll.mean().shift(1)
        std = roll.std(ddof=0).shift(1).replace(0.0, np.nan)

        z = (s - mean) / std
        z = z.fillna(0.0)

        # ✅ ИСПРАВЛЕНИЕ: Используем config.eps вместо жесткого значения
        state_arr = np.where(z > config.eps, 1, np.where(z < -config.eps, -1, 0))
        state = pd.Series(state_arr.astype(int), index=s.index)
        conf = z.abs() * float(config.z_to_conf)

        # CUSUM price mean и std для цен закрытия
        cusum_price_mean = closes.rolling(config.normalize_window, min_periods=config.normalize_window).mean().shift(1)
        cusum_price_std = closes.rolling(config.normalize_window, min_periods=config.normalize_window).std(
            ddof=0).shift(1)

        return {
            'cusum': s,
            'cusum_zscore': z,
            'cusum_state': state,
            'cusum_conf': conf,
            'cusum_price_mean': cusum_price_mean,
            'cusum_price_std': cusum_price_std,
            'cusum_pos': cusum_pos,
            'cusum_neg': cusum_neg
        }

    def align_to_interval(self, ts: int, interval_ms: int, phase_ms: int) -> int:
        """
        Выравнивание timestamp к началу интервала с учетом фазирования.
        Args:
            ts: timestamp в миллисекундах
            interval_ms: длительность интервала (ONE_M_MS или FIVE_M_MS)
            phase_ms: смещение начала интервала (PHASE_1M или PHASE_5M)
        Returns:
            timestamp начала интервала
        Example:
            # 1m свеча 12:01:50 должна попасть в 5m свечу 12:00:50
            align_to_interval(ts_12_01_50, 300_000, 290_000) -> ts_12_00_50
        """
        return ((ts - phase_ms) // interval_ms) * interval_ms + phase_ms

    def set_indicator_config(self, config: IndicatorConfig) -> None:
        """Обновление конфигурации индикаторов"""
        self.indicator_config = config
        self.logger.info(f"Updated indicator configuration: {config}")

    def get_metrics(self, symbol: str) -> Optional[CalculationMetrics]:
        """Получение метрик расчета для символа"""
        return self._metrics.get(symbol)
    # ======================================================================
    # 5m FEATURES (ML)
    # ======================================================================

    def get_statistics(self) -> Dict[str, Any]:
        """Получение общей статистики работы"""
        stats = {
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "uptime_seconds": (datetime.now(UTC) - self.created_at).total_seconds(),
            "total_calculations": len(self._metrics),
            "active_symbols": len(set(m.symbol for m in self._metrics.values())),
            "total_errors": sum(m.errors_count for m in self._metrics.values()),
            "avg_duration_ms": statistics.mean(
                m.duration_ms for m in self._metrics.values()
                if m.duration_ms > 0
            ) if self._metrics else 0,
            "indicator_config": asdict(self.indicator_config),
        }
        return stats

    def backfill_5m_cusum(
            self,
            symbol: str = "ETHUSDT",
            days: int = 5,
            normalize_window: int = 150,
            z_to_conf: float = 1.0,
            batch_size: int = 1440
    ) -> dict:
        """
        Бэкфилл CUSUM полей для 5m-таблицы за последние `days` дней.
        """
        if not hasattr(self, "engine") or self.engine is None:
            raise RuntimeError("MarketDataUtils.backfill_5m_cusum: self.engine is not set")

        try:
            t5m = TABLES.get("candles_5m")
        except Exception:
            t5m = None
        t5m = t5m or "candles_5m"

        since_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

        with self.engine.begin() as conn:
            df = pd.read_sql(
                text(f"""
                    SELECT ts, close
                      FROM {t5m}
                     WHERE symbol = :sym
                       AND finalized = 1
                       AND ts >= :since
                     ORDER BY ts ASC
                """),
                conn,
                params={"sym": symbol, "since": since_ts}
            )

            if df.empty:
                return {"symbol": symbol, "rows": 0, "updated": 0, "since_ts": since_ts}

            cusum_results = self.calculate_cusum(df["close"], self.cusum_config_5m)

            payload = pd.DataFrame({
                "cusum": cusum_results['cusum'],
                "cusum_state": cusum_results['cusum_state'],
                "cusum_zscore": cusum_results['cusum_zscore'],
                "cusum_conf": cusum_results['cusum_conf'],
                "cusum_pos": cusum_results['cusum_pos'],
                "cusum_neg": cusum_results['cusum_neg'],
                "cusum_reason": pd.Series([None] * len(df), index=df.index, dtype=object),
                "ts": df["ts"].astype(int),
            })

            # ✅ Восстановлено: расчёт mean/std с shift(1) – анти-lookahead
            win = normalize_window
            payload["cusum_price_mean"] = (
                df["close"]
                .rolling(win, min_periods=win)
                .mean()
                .shift(1)
            )
            payload["cusum_price_std"] = (
                df["close"]
                .rolling(win, min_periods=win)
                .std(ddof=0)
                .shift(1)
            )

            sql_upd = text(f"""
                UPDATE {t5m}
                   SET cusum = :cusum,
                       cusum_state = :cusum_state,
                       cusum_zscore = :cusum_zscore,
                       cusum_conf = :cusum_conf,
                       cusum_price_mean = :cusum_price_mean,
                       cusum_price_std = :cusum_price_std,
                       cusum_pos = :cusum_pos,
                       cusum_neg = :cusum_neg,
                       cusum_reason = :cusum_reason
                 WHERE symbol = :symbol
                   AND ts = :ts
            """)

            updated = 0
            for start in range(0, len(payload), batch_size):
                chunk = payload.iloc[start:start + batch_size].copy()
                chunk["symbol"] = symbol
                conn.execute(sql_upd, chunk.to_dict(orient="records"))
                updated += len(chunk)

        return {
            "symbol": symbol,
            "rows": int(len(df)),
            "updated": int(updated),
            "since_ts": since_ts,
            "normalize_window": normalize_window,
            "z_to_conf": z_to_conf
        }

    async def compute_5m_features_bulk(self, symbol: str, bars_5m: List[dict]) -> int:
        """Оптимизированная версия с предзагрузкой 15-минутных окон CUSUM и прогресс-баром"""
        self.logger.info(f"🚀 START compute_5m_features_bulk for {symbol} with {len(bars_5m)} candles")

        metrics = CalculationMetrics(symbol=symbol, started_at=datetime.now(UTC))
        self._metrics[symbol] = metrics

        try:
            if not bars_5m:
                return 0

            saved_count = 0

            # ✅ 1. ПРЕДЗАГРУЖАЕМ 1m МАППИНГ (один раз)
            min_ts = bars_5m[0]['ts']
            max_ts = bars_5m[-1]['ts']
            candles_1m_map = await self._get_last_1m_candles(symbol, min_ts, max_ts)
            self.logger.info(f"📡 Pre-loaded {len(candles_1m_map)} 1m mappings")

            # ✅ 2. ПРЕДЗАГРУЖАЕМ CUSUM СИГНАЛЫ ДЛЯ КАЖДОГО 15-МИНУТНОГО ОКНА С ПРОГРЕСС-БАРОМ
            cusum_windows = {}
            self.logger.info("🔍 Pre-loading CUSUM windows for each 5m candle...")

            # ✅ ПРОГРЕСС-БАР ДЛЯ ПРЕДЗАГРУЗКИ CUSUM
            with tqdm(total=len(bars_5m), desc="🔄 Pre-loading CUSUM windows", unit="window",
                      bar_format='{l_bar}{bar:50}{r_bar}{bar:-50b}') as pbar:

                cusum_lookback_ms = 15 * 60_000  # Константа вместо магического числа
                for i, bar in enumerate(bars_5m):
                    ts_5m = bar['ts']
                    start_ts = ts_5m - cusum_lookback_ms
                    end_ts = ts_5m

                    cusum_signals = await self._get_cusum_signals_1m(symbol, start_ts, end_ts)
                    cusum_windows[ts_5m] = cusum_signals

                    # ✅ ОБНОВЛЯЕМ ПРОГРЕСС-БАР
                    pbar.update(1)
                    pbar.set_postfix(windows=len(cusum_windows), signals=len(cusum_signals))

            self.logger.info(f"✅ Pre-loaded CUSUM windows for {len(cusum_windows)} candles")

            # ✅ 3. ОСНОВНОЙ ЦИКЛ ОБРАБОТКИ С ПРОГРЕСС-БАРОМ
            with tqdm(total=len(bars_5m), desc="🎯 Calculating 5m indicators", unit="candle",
                      bar_format='{l_bar}{bar:50}{r_bar}{bar:-50b}') as pbar:

                min_required = int(self.cusum_config_5m.normalize_window)

                for i, current_bar in enumerate(bars_5m):
                    try:
                        # ✅ ПРАВИЛЬНАЯ ЛОГИКА: Используем срез из ВХОДНОГО массива
                        # Берем все свечи от начала до текущей (i+1)
                        all_bars = bars_5m[0:i + 1]

                        # ✅ Проверка минимального количества данных
                        if len(all_bars) < min_required:
                            self.logger.debug(
                                f"Skipping candle {i}: insufficient data "
                                f"({len(all_bars)} < {min_required} required for CUSUM 5m)"
                            )
                            # Сохраняем СЫРУЮ свечу без индикаторов
                            await self.upsert_candles_5m(symbol, [current_bar])
                            pbar.update(1)
                            pbar.set_postfix(saved=saved_count, skipped=f"{i}/{len(bars_5m)}")
                            continue

                        # ✅ Берем ПРЕДЗАГРУЖЕННЫЕ CUSUM сигналы для этой свечи
                        ts_5m = current_bar['ts']
                        cusum_signals = cusum_windows.get(ts_5m, [])

                        # ✅ Передаем ПРАВИЛЬНЫЙ контекст из входного массива
                        result = await self._compute_5m_features_for_last_candle(
                            symbol, all_bars, cusum_signals, candles_1m_map
                        )
                        if result > 0:
                            saved_count += 1

                        # ✅ ОБНОВЛЯЕМ ПРОГРЕСС-БАР
                        pbar.update(1)
                        pbar.set_postfix(
                            saved=saved_count,
                            cusum_signals=len(cusum_signals),
                            progress=f"{i + 1}/{len(bars_5m)}"
                        )

                    except Exception as e:
                        self.logger.error(f"❌ Error at bar {i}: {e}")
                        await self.upsert_candles_5m(symbol, [current_bar])
                        pbar.update(1)
                        pbar.set_postfix_str(f"❌ Error: {str(e)[:20]}...")
            # ✅ ФИНАЛИЗАЦИЯ
            metrics.complete()
            metrics.rows_processed = len(bars_5m)

            if metrics.errors_count > 0:
                print(f"⚠️  Errors: {metrics.errors_count}")

            return saved_count

        except Exception as e:
            self.logger.error(f"💥 Critical error: {e}", exc_info=True)
            return 0

    async def _compute_5m_features_for_last_candle_with_data(
            self, symbol: str, bars_5m: List[dict], cusum_signals: List[dict], candles_1m_map: dict
    ) -> int:
        """
        Версия _compute_5m_features_for_last_candle с предзагруженными данными
        """
        # Временно используем существующий метод, но передаем данные через атрибуты
        # или создадим адаптер

        try:
            # Создаем временный объект для передачи данных
            class TempData:
                def __init__(self, cusum_signals, candles_1m_map):
                    self.cusum_signals = cusum_signals
                    self.candles_1m_map = candles_1m_map

            temp_data = TempData(cusum_signals, candles_1m_map)

            # Вызываем существующий метод, но подменяем вызовы внутренних методов
            return await self._compute_5m_features_for_last_candle(symbol, bars_5m)

        except Exception as e:
            self.logger.error(f"Error in adapted 5m calculation: {e}")
            # Fallback: сохраняем последнюю свечу без индикаторов
            await self.upsert_candles_5m(symbol, [bars_5m[-1]])
            return 0

    async def compute_5m_features_incremental(self, symbol: str, new_bar_5m: dict) -> dict:
        """
        Инкрементальный расчет индикаторов для ОДНОЙ новой 5m свечи.
        Используется в live-режиме для обновления последней свечи.

        Args:
            symbol: торговый символ
            new_bar_5m: новая 5m свеча (dict)

        Returns:
            обогащенная свеча с индикаторами (dict)
        """
        try:
            # Получаем историю для расчета индикаторов
            last_k = int(self.cfg["incremental"]["last_k_5m"])
            history = await self.read_candles_5m(symbol, last_n=last_k - 1) or []
            history = list(reversed(history)) if history else []

            # Добавляем новую свечу
            history.append(new_bar_5m)

            # Если недостаточно данных - просто сохраняем без индикаторов
            if len(history) < 14:
                await self.upsert_candles_5m(symbol, [new_bar_5m])
                return new_bar_5m

            # Рассчитываем и сохраняем индикаторы
            await self._compute_5m_features_for_last_candle(symbol, history)

            # ✅ Небольшая задержка для WAL flush
            await asyncio.sleep(0.01)

            # ✅ Читаем обратно обогащенную свечу из БД
            enriched = await self.read_candles_5m(symbol, start_ts=new_bar_5m['ts'], end_ts=new_bar_5m['ts'])
            if enriched and len(enriched) > 0:
                return enriched[0]
            else:
                self.logger.warning(f"Failed to read back enriched candle for {symbol}@{new_bar_5m['ts']}")
                return new_bar_5m

        except Exception as e:
            self.logger.error(f"Incremental 5m update failed for {symbol}: {e}", exc_info=True)
            # Fallback: сохраняем хотя бы сырую свечу
            await self.upsert_candles_5m(symbol, [new_bar_5m])
            return new_bar_5m

    async def _compute_5m_features_for_last_candle(
            self,
            symbol: str,
            bars_5m: List[dict],
            preloaded_cusum_signals: Optional[List[dict]] = None,
            preloaded_candles_1m_map: Optional[dict] = None
    ) -> int:
        """
        Рассчитывает индикаторы для ТОЛЬКО ПОСЛЕДНЕЙ свечи в списке.
        ИСПРАВЛЕНИЕ: защита от перепутанных временных диапазонов
        """
        metrics = CalculationMetrics(
            symbol=symbol,
            started_at=datetime.now(UTC)
        )
        self._metrics[symbol] = metrics

        try:
            if not bars_5m or len(bars_5m) < 28:
                self.logger.warning(f"Insufficient data for {symbol}: {len(bars_5m)} bars")
                return 0

            # Индекс последней (новой) свечи
            last_idx = len(bars_5m) - 1
            base_bar = bars_5m[last_idx]

            # Подготовка данных для ВСЕЙ истории (нужна для расчета индикаторов)
            n = len(bars_5m)
            ts_list = [int(b['ts']) for b in bars_5m]
            opens = [float(b["open"]) for b in bars_5m]
            highs = [float(b["high"]) for b in bars_5m]
            lows = [float(b["low"]) for b in bars_5m]
            closes = [float(b["close"]) for b in bars_5m]
            volumes = [float(b.get("volume", 0.0)) for b in bars_5m]

            metrics.rows_processed = 1  # обрабатываем только 1 свечу

            # БЛОК 1: ПРЕДВАРИТЕЛЬНЫЕ ЗАПРОСЫ С ПРЕДЗАГРУЗКОЙ

            last_ts = ts_list[last_idx]
            # CUSUM окно: 15 минут назад от последней свечи
            cusum_lookback_ms = 15 * 60_000
            cusum_start_ts = last_ts - cusum_lookback_ms
            cusum_end_ts = last_ts
            # 1m микроструктура: 30 минут назад от последней свечи
            microstructure_lookback_ms = 30 * 60_000
            min_ts_1m = last_ts - microstructure_lookback_ms
            max_ts_1m = last_ts

            # Загружаем данные с правильными диапазонами
            if preloaded_cusum_signals is not None:
                cusum_signals = preloaded_cusum_signals
                self.logger.debug(f"✅ Using preloaded CUSUM signals: {len(cusum_signals)}")
            else:
                cusum_signals = await self._get_cusum_signals_1m(symbol, cusum_start_ts, cusum_end_ts, threshold=2.0)
                self.logger.debug(f"📡 Loaded CUSUM signals: {len(cusum_signals)}")

            if preloaded_candles_1m_map is not None:
                candles_1m_map = preloaded_candles_1m_map
                self.logger.debug(f"✅ Using preloaded 1m mappings: {len(candles_1m_map)}")
            else:
                candles_1m_map = await self._get_last_1m_candles(symbol, min_ts_1m, max_ts_1m)
                self.logger.debug(f"📡 Loaded 1m mappings: {len(candles_1m_map)}")

            # БЛОК 2: ВЕКТОРНЫЙ РАСЧЕТ ИНДИКАТОРОВ (для контекста)

            price_change_5_list = []
            for i in range(n):
                if i >= 5:
                    pc5 = ((closes[i] - closes[i - 5]) / closes[i - 5]) * 100
                    price_change_5_list.append(pc5)
                else:
                    price_change_5_list.append(None)

            # Тренд и импульс
            trend_momentum_z = self._z_score_series(price_change_5_list, window=20)

            # EMA7 для trend_acceleration
            ema7_list = [b.get('ema7') for b in bars_5m]
            if all(v is None for v in ema7_list):
                ema7_list = self._ema_series(closes, 7)
            trend_acceleration_ema7 = self._trend_acceleration_series(ema7_list)

            # Объем
            volume_ratio_ema3 = self._volume_ratio_ema3_series(volumes, ema_period=3)

            # Структура свечи
            candle_relative_body, upper_shadow_ratio, lower_shadow_ratio = self._candle_body_ratios(
                opens, highs, lows, closes
            )

            # VWAP
            vwap = self._calculate_vwap(bars_5m, period=96)
            price_vs_vwap = self._price_vs_vwap_series(closes, vwap)

            # CUSUM
            (cusum_1m_recent, cusum_1m_quality_score,
             cusum_1m_trend_aligned, cusum_1m_price_move) = self._cusum_1m_features(
                cusum_signals, ts_list, closes, volumes, price_change_5_list
            )

            # Микроструктура 1m для последней свечи
            last_ts = ts_list[last_idx]
            candle_1m = candles_1m_map.get(last_ts)
            if candle_1m:
                pattern, body_ratio, close_pos = self._pattern_features_1m(
                    candle_1m['open'], candle_1m['high'], candle_1m['low'],
                    candle_1m['close'], candle_1m['ema7']
                )
            else:
                pattern, body_ratio, close_pos = 0, 0.0, 0.5

            # БЛОК 3: РАСЧЕТ ИНДИКАТОРОВ ДЛЯ ПОСЛЕДНЕЙ СВЕЧИ
            indicators = {}
            i = last_idx

            # Окно данных для расчета
            min_window = 28
            actual_window = min(i + 1, min_window)

            window_data = {
                'closes': closes[max(0, i - actual_window + 1):i + 1],
                'opens': opens[max(0, i - actual_window + 1):i + 1],
                'highs': highs[max(0, i - actual_window + 1):i + 1],
                'lows': lows[max(0, i - actual_window + 1):i + 1],
                'volumes': volumes[max(0, i - actual_window + 1):i + 1]
            }

            if len(window_data['closes']) < 14:
                # Недостаточно данных
                await self.upsert_candles_5m(symbol, [base_bar])
                return 1

            try:
                # ✅ ДОБАВИТЬ ЛОГИРОВАНИЕ НАЧАЛА

                # === БАЗОВЫЕ ИНДИКАТОРЫ ===
                indicators["price_change_5"] = price_change_5_list[i]
                if indicators["price_change_5"] is not None:
                    metrics.indicators_count += 1

                # ✅ ЛОГИРОВАНИЕ ПОСЛЕ КАЖДОГО ИНДИКАТОРА
                self.logger.debug(f"price_change_5: {indicators['price_change_5']}")

                # CMO-14
                try:
                    cmo = self._cmo_series(window_data['closes'], 14)
                    indicators["cmo_14"] = cmo[-1] if cmo else None
                    metrics.indicators_count += 1
                    self.logger.debug(f"cmo_14: {indicators['cmo_14']} (from {len(cmo)} values)")
                except Exception as e:
                    self.logger.error(f"❌ CMO calculation failed: {e}")
                    indicators["cmo_14"] = None

                # MACD histogram
                try:
                    macd_data = self._macd_series(window_data['closes'], 12, 26, 9)
                    indicators["macd_histogram"] = macd_data[2][-1] if macd_data[2] else None
                    metrics.indicators_count += 1
                    self.logger.debug(f"macd_histogram: {indicators['macd_histogram']}")
                except Exception as e:
                    self.logger.error(f"❌ MACD calculation failed: {e}")
                    indicators["macd_histogram"] = None

                # DMI/ADX-14
                try:
                    dmi_data = self._dmi_adx_series(
                        window_data['highs'],
                        window_data['lows'],
                        window_data['closes'],
                        14
                    )
                    indicators["adx_14"] = dmi_data[2][-1] if dmi_data[2] else None
                    indicators["plus_di_14"] = dmi_data[0][-1] if dmi_data[0] else None
                    indicators["minus_di_14"] = dmi_data[1][-1] if dmi_data[1] else None
                    metrics.indicators_count += 3
                    self.logger.debug(
                        f"adx_14: {indicators['adx_14']}, "
                        f"plus_di: {indicators['plus_di_14']}, "
                        f"minus_di: {indicators['minus_di_14']}"
                    )
                except Exception as e:
                    self.logger.error(f"❌ DMI/ADX calculation failed: {e}")
                    indicators["adx_14"] = None
                    indicators["plus_di_14"] = None
                    indicators["minus_di_14"] = None

                # ATR нормализованный
                try:
                    atr_val = dmi_data[3][-1] if dmi_data[3] else None
                    indicators["atr_14_normalized"] = (atr_val / closes[i]) * 100 if atr_val and closes[
                        i] != 0 else None
                    metrics.indicators_count += 1
                    self.logger.debug(f"atr_14_normalized: {indicators['atr_14_normalized']}")
                except Exception as e:
                    self.logger.error(f"❌ ATR calculation failed: {e}")
                    indicators["atr_14_normalized"] = None

                # Bollinger Bands
                try:
                    bb = self._bollinger_bands_features(window_data['closes'], 20, 2.0)
                    bb_width = bb[0][-1] if bb[0] else None
                    bb_position = bb[1][-1] if bb[1] else None
                    indicators["bb_width"] = bb_width
                    indicators["bb_position"] = bb_position
                    metrics.indicators_count += 2
                    self.logger.debug(f"bb_width: {bb_width}, bb_position: {bb_position}")
                except Exception as e:
                    self.logger.error(f"❌ Bollinger Bands calculation failed: {e}")
                    indicators["bb_width"] = None
                    indicators["bb_position"] = None

                # === ML ФИЧИ ===
                try:
                    indicators["trend_momentum_z"] = trend_momentum_z[i] if i < len(trend_momentum_z) else None
                    indicators["trend_acceleration_ema7"] = trend_acceleration_ema7[i] if i < len(
                        trend_acceleration_ema7) else None
                    indicators["regime_volatility"] = (atr_val / closes[i]) if atr_val and closes[i] != 0 else None
                    indicators["volume_ratio_ema3"] = volume_ratio_ema3[i] if i < len(volume_ratio_ema3) else None
                    indicators["candle_relative_body"] = candle_relative_body[i] if i < len(
                        candle_relative_body) else None
                    indicators["upper_shadow_ratio"] = upper_shadow_ratio[i] if i < len(upper_shadow_ratio) else None
                    indicators["lower_shadow_ratio"] = lower_shadow_ratio[i] if i < len(lower_shadow_ratio) else None
                    indicators["price_vs_vwap"] = price_vs_vwap[i] if i < len(price_vs_vwap) else None
                    indicators["cusum_1m_recent"] = cusum_1m_recent[i] if i < len(cusum_1m_recent) else 0
                    indicators["cusum_1m_quality_score"] = cusum_1m_quality_score[i] if i < len(
                        cusum_1m_quality_score) else 0.0
                    indicators["cusum_1m_trend_aligned"] = cusum_1m_trend_aligned[i] if i < len(
                        cusum_1m_trend_aligned) else 0
                    indicators["cusum_1m_price_move"] = cusum_1m_price_move[i] if i < len(cusum_1m_price_move) else 0.0
                    indicators["is_trend_pattern_1m"] = pattern
                    indicators["body_to_range_ratio_1m"] = body_ratio
                    indicators["close_position_in_range_1m"] = close_pos
                    metrics.indicators_count += 17

                    self.logger.debug(f"ML features calculated: trend_momentum_z={indicators['trend_momentum_z']}")
                except Exception as e:
                    self.logger.error(f"❌ ML features calculation failed: {e}")
                    # Установка значений по умолчанию
                    for field in ['trend_momentum_z', 'trend_acceleration_ema7', 'regime_volatility',
                                  'volume_ratio_ema3', 'candle_relative_body', 'upper_shadow_ratio',
                                  'lower_shadow_ratio', 'price_vs_vwap']:
                        indicators[field] = None
                    for field in ['cusum_1m_recent', 'cusum_1m_trend_aligned']:
                        indicators[field] = 0
                    for field in ['cusum_1m_quality_score', 'cusum_1m_price_move']:
                        indicators[field] = 0.0
                    indicators["is_trend_pattern_1m"] = 0
                    indicators["body_to_range_ratio_1m"] = 0.0
                    indicators["close_position_in_range_1m"] = 0.5

                # Формируем выходную строку
                out_row = dict(base_bar)
                out_row.update(indicators)

            except Exception as e:
                self.logger.error(
                    f"❌ CRITICAL ERROR in indicator calculation for {symbol}@{base_bar.get('ts', 'N/A')}: {e}",
                    exc_info=True
                )
                metrics.errors_count += 1
                # Сохраняем базовую свечу без индикаторов
                out_row = dict(base_bar)
                for field in ['price_change_5', 'cmo_14', 'macd_histogram',
                              'adx_14', 'plus_di_14', 'minus_di_14', 'atr_14_normalized',
                              'bb_width', 'bb_position',
                              'trend_momentum_z', 'trend_acceleration_ema7',
                              'regime_volatility',
                              'volume_ratio_ema3',
                              'candle_relative_body', 'upper_shadow_ratio', 'lower_shadow_ratio',
                              'price_vs_vwap',
                              'cusum_1m_recent', 'cusum_1m_quality_score',
                              'cusum_1m_trend_aligned', 'cusum_1m_price_move',
                              'is_trend_pattern_1m', 'body_to_range_ratio_1m', 'close_position_in_range_1m']:
                    out_row[field] = None
            # Финализация метрик
            metrics.complete()

            # CUSUM 5m:
            try:
                cfg = self.cusum_config_5m
                win = cfg.normalize_window

                # Берем достаточно данных для расчета (минимум win для нормального rolling)
                min_data_needed = win
                data_to_use = min(min_data_needed, len(bars_5m))

                if data_to_use >= win:
                    # Берем closes для расчета CUSUM
                    close_data = [float(b["close"]) for b in bars_5m]
                    close_series = pd.Series(close_data)

                    # Используем единый метод calculate_cusum
                    cusum_results = self.calculate_cusum(close_series, cfg)

                    # Берем последние значения (для последней свечи)
                    last_idx = len(cusum_results['cusum']) - 1

                    # Заполняем out_row ВСЕМИ CUSUM полями
                    out_row.update({
                        "cusum": float(cusum_results['cusum'].iloc[last_idx]),
                        "cusum_state": int(cusum_results['cusum_state'].iloc[last_idx]),
                        "cusum_zscore": float(cusum_results['cusum_zscore'].iloc[last_idx]),
                        "cusum_conf": float(cusum_results['cusum_conf'].iloc[last_idx]),
                        "cusum_price_mean": float(cusum_results['cusum_price_mean'].iloc[last_idx]),
                        "cusum_price_std": float(cusum_results['cusum_price_std'].iloc[last_idx]),
                        "cusum_pos": float(cusum_results['cusum_pos'].iloc[last_idx]),
                        "cusum_neg": float(cusum_results['cusum_neg'].iloc[last_idx]),
                        "cusum_reason": f"z={cusum_results['cusum_zscore'].iloc[last_idx]:.3f}"
                    })

                else:
                    self.logger.warning(f"⚠️ [CUSUM 5m] Insufficient data: {data_to_use} < {win}")
                    # Устанавливаем значения по умолчанию
                    current_price = float(bars_5m[-1]["close"]) if bars_5m else 0.0
                    out_row.update({
                        "cusum": 0.0,
                        "cusum_state": 0,
                        "cusum_zscore": 0.0,
                        "cusum_conf": 0.0,
                        "cusum_price_mean": current_price,
                        "cusum_price_std": 0.0,
                        "cusum_pos": 0.0,
                        "cusum_neg": 0.0,
                        "cusum_reason": f"insufficient_data_{data_to_use}",
                    })

            except Exception as e:
                self.logger.error(f"❌ [CUSUM 5m] calculation failed for {symbol}: {e}", exc_info=True)
                # Fallback с текущей ценой
                current_price = float(bars_5m[-1]["close"]) if bars_5m else 0.0
                out_row.update({
                    "cusum": 0.0,
                    "cusum_state": 0,
                    "cusum_zscore": 0.0,
                    "cusum_conf": 0.0,
                    "cusum_price_mean": current_price,
                    "cusum_price_std": 0.0,
                    "cusum_pos": 0.0,
                    "cusum_neg": 0.0,
                    "cusum_reason": f"error: {str(e)[:50]}",
                })

            # СОХРАНЕНИЕ ТОЛЬКО ПОСЛЕДНЕЙ СВЕЧИ
            saved = await self.upsert_candles_5m(symbol, [out_row])

            self.logger.debug(
                f"Incremental 5m: {symbol}@{base_bar['ts']} - "
                f"{metrics.indicators_count} indicators in {metrics.duration_ms:.1f}ms"
            )

            return saved

        except Exception as e:
            metrics.errors_count += 1
            metrics.complete()
            self.logger.error(
                f"Error in _compute_5m_features_for_last_candle for {symbol}: {e}\n"
                f"Metrics: {metrics}",
                exc_info=True
            )
            return 0

    async def _get_cusum_signals_1m(self, symbol: str, start_ts: int, end_ts: int, threshold: float = 2.0) -> List[
        dict]:
        try:
            start_dt = datetime.fromtimestamp(start_ts / 1000, UTC)
            end_dt = datetime.fromtimestamp(end_ts / 1000, UTC)

            # ✅ ПРОВЕРЯЕМ ДИАПАЗОН ДАННЫХ В БД
            check_query = text(f"""
                SELECT 
                    MIN(ts) as min_ts,
                    MAX(ts) as max_ts,
                    COUNT(*) as total_count,
                    COUNT(CASE WHEN cusum_zscore IS NOT NULL THEN 1 END) as cusum_count
                FROM {TABLES['candles_1m']}
                WHERE symbol = :symbol
            """)

            async with self.aengine.begin() as conn:
                # Сначала проверяем общий диапазон данных
                check_result = await conn.execute(check_query, {"symbol": symbol})
                stats = check_result.mappings().first()


                # Основной запрос
                query = text(f"""
                    SELECT ts, cusum_zscore, volume, 
                           (close - LAG(close, 1) OVER (ORDER BY ts)) / LAG(close, 1) OVER (ORDER BY ts) * 100 as price_change_1
                    FROM {TABLES['candles_1m']}
                    WHERE symbol = :symbol
                      AND ts BETWEEN :start_ts AND :end_ts
                      AND cusum_zscore IS NOT NULL
                    ORDER BY ts
                """)

                result = await conn.execute(query, {
                    "symbol": symbol,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                })
                rows = result.mappings().all()

            signals = []
            for row in rows:
                signals.append({
                    'ts': int(row['ts']),
                    'signal_strength': float(row['cusum_zscore']) if row['cusum_zscore'] is not None else 0.0,
                    'volume': float(row['volume']) if row['volume'] is not None else 0.0,
                    'price_change': float(row['price_change_1']) if row['price_change_1'] is not None else 0.0
                })

            return signals

        except Exception as e:
            self.logger.error(f"❌ CUSUM query failed: {e}", exc_info=True)
            return []

    async def _get_last_1m_candles(self, symbol: str, start_ts: int, end_ts: int) -> dict:
        """
        Получение всех 1m свечей для диапазона и создание маппинга ts_5m -> 1m_candle.

        Args:
            symbol: торговый символ
            start_ts: начало диапазона
            end_ts: конец диапазона

        Returns:
            словарь {ts_5m: {open, high, low, close, ema7}}
        """
        try:
            query = text(f"""
                SELECT ts, open, high, low, close, ema7
                FROM {TABLES['candles_1m']}
                WHERE symbol = :symbol
                  AND ts BETWEEN :start_ts AND :end_ts
                ORDER BY ts
            """)

            async with self.aengine.begin() as conn:
                result = await conn.execute(query, {
                    "symbol": symbol,
                    "start_ts": start_ts,
                    "end_ts": end_ts
                })
                rows = result.mappings().all()

            # Создаем маппинг: для каждой 5m свечи находим последнюю 1m свечу
            mapping = {}

            # Группируем 1m свечи по 5m периодам
            for row in rows:
                ts_1m = int(row['ts'])
                # Находим к какой 5m свече относится эта 1m свеча (с учетом фазирования)
                # Определяем фазу 1m свечи
                is_phased = (ts_1m % self.ONE_M_MS) == 50_000
                ts_5m = self.align_to_interval(ts_1m, self.FIVE_M_MS, self.PHASE_5M) if is_phased else (
                            ts_1m // self.FIVE_M_MS) * self.FIVE_M_MS

                # Сохраняем последнюю 1m свечу для каждого 5m периода
                if ts_5m not in mapping or ts_1m > mapping[ts_5m]['ts_1m']:
                    ema7_val = float(row['ema7']) if row['ema7'] is not None else None
                    mapping[ts_5m] = {
                        'ts_1m': ts_1m,
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'ema7': ema7_val
                    }

            self.logger.debug(f"Mapped {len(mapping)} 5m periods to 1m candles for {symbol}")
            return mapping

        except Exception as e:
            self.logger.error(f"Failed to get 1m candles for {symbol}: {e}", exc_info=True)
            return {}

    @staticmethod
    def _cusum_1m_features(cusum_signals: List[dict], ts_5m_list: List[int],
                           close_5m: List[float], volume_5m: List[float],
                           price_change_5: List[Optional[float]]) -> tuple[
        List[int], List[float], List[int], List[float]]:
        """
        Агрегация CUSUM сигналов с 1m для каждой 5m свечи.

        Args:
            cusum_signals: список CUSUM сигналов из candles_1m
            ts_5m_list: временные метки 5m свечей
            close_5m: цены закрытия 5m
            volume_5m: объемы 5m
            price_change_5: изменение цены за 5 периодов (направление тренда)

        Returns:
            (cusum_1m_recent, cusum_1m_quality_score,
             cusum_1m_trend_aligned, cusum_1m_price_move)

        Note:
            cusum_1m_price_move теперь со знаком:
            - положительное значение = максимальное движение вверх (LONG)
            - отрицательное значение = максимальное движение вниз (SHORT)
            - определяется по преобладающему направлению CUSUM сигналов
        """
        recent: List[int] = []
        quality: List[float] = []
        trend_aligned: List[int] = []
        price_move: List[float] = []

        LOOKBACK_WINDOW = 15 * 60 * 1000  # 15 минут

        for i, ts in enumerate(ts_5m_list):
            # Найти CUSUM сигналы за последние 15 минут
            relevant_signals = [
                sig for sig in cusum_signals
                if ts - LOOKBACK_WINDOW <= sig['ts'] <= ts
            ]

            if relevant_signals:
                recent.append(1)

                # Качество: средняя сила сигналов, нормализованная к [0, 1]
                avg_strength = np.mean([abs(sig['signal_strength']) for sig in relevant_signals])
                quality.append(min(1.0, avg_strength / 3.0))

                # Всплеск объема: сравнение среднего объема сигналов с текущим
                avg_signal_volume = np.mean([sig['volume'] for sig in relevant_signals])
                current_volume = volume_5m[i]

                # Совпадение с трендом
                if price_change_5[i] is not None:
                    avg_price_change = np.mean([sig['price_change'] for sig in relevant_signals])
                    aligned = 1 if np.sign(avg_price_change) == np.sign(price_change_5[i]) else 0
                    trend_aligned.append(aligned)
                else:
                    trend_aligned.append(0)

                # ═══════════════════════════════════════════════════════════════
                # НОВАЯ ЛОГИКА: Направленное движение цены
                # ═══════════════════════════════════════════════════════════════

                # Разделяем движения по направлению
                positive_moves = [sig['price_change'] for sig in relevant_signals if sig['price_change'] > 0]
                negative_moves = [sig['price_change'] for sig in relevant_signals if sig['price_change'] < 0]

                # Находим максимальные движения в каждом направлении
                max_positive = max(positive_moves) if positive_moves else 0.0
                max_negative = min(negative_moves) if negative_moves else 0.0  # отрицательное число

                # Определяем преобладающее направление по средней силе CUSUM сигналов
                avg_cusum_strength = np.mean([sig['signal_strength'] for sig in relevant_signals])

                # Если преобладают положительные CUSUM (BUY) → берем max_positive
                # Если преобладают отрицательные CUSUM (SELL) → берем max_negative
                if avg_cusum_strength > 0:
                    # LONG направление: берем максимальное положительное движение
                    price_move.append(max_positive)
                elif avg_cusum_strength < 0:
                    # SHORT направление: берем максимальное отрицательное движение (со знаком минус)
                    price_move.append(max_negative)
                else:
                    # Нейтральное: берем движение с большим модулем
                    if abs(max_positive) >= abs(max_negative):
                        price_move.append(max_positive)
                    else:
                        price_move.append(max_negative)

            else:
                recent.append(0)
                quality.append(0.0)
                trend_aligned.append(0)
                price_move.append(0.0)

        return recent, quality, trend_aligned, price_move

    def _validate_input_bars(self, bars: List[dict]) -> bool:
        """Валидация входных данных"""
        if not bars:
            return False

        required = {"open", "high", "low", "close", "ts"}

        for bar in bars:
            # Проверка наличия полей
            if not all(field in bar for field in required):
                return False

            # Проверка типов и значений
            try:
                if not all(isinstance(float(bar[f]), (int, float))
                           and float(bar[f]) > 0 for f in ["open", "high", "low", "close"]):
                    return False

                if not isinstance(bar["ts"], (int, float)) or bar["ts"] <= 0:
                    return False

                # Проверка High/Low
                if not (float(bar["high"]) >= float(bar["open"]) and
                        float(bar["high"]) >= float(bar["close"]) and
                        float(bar["low"]) <= float(bar["open"]) and
                        float(bar["low"]) <= float(bar["close"])):
                    return False

            except (ValueError, TypeError):
                return False

        return True

    # ======================================================================
    # 1m CUSUM (warmup + incremental)
    # ======================================================================

    def _cosum_series(self, closes: List[float], period: int, normalize_window: int) -> List[Optional[float]]:
        """
        Backward-compatible CUSUM (alias).
        Параметр `period` сохранён для совместимости и на результат не влияет.
        """
        # помечаем параметр как "использованный", чтобы линтеры не ругались
        _ = int(period)

        s = pd.Series(closes, dtype="float64")
        # при None/NaN в исходных значениях защищаемся от вылетов
        s = s.ffill().bfill()
        diff = s.diff().fillna(0.0)

        # Односторонний накопительный сумматор (CUSUM)
        pos: List[float] = []
        csum = 0.0
        for d in diff.tolist():
            csum = max(0.0, csum + d) if d >= 0 else min(0.0, csum + d)
            pos.append(csum)

        # Нормализация: z-score по последнему значению в окне normalize_window
        series_pos = pd.Series(pos, dtype="float64")
        z = series_pos.rolling(window=normalize_window, min_periods=normalize_window).apply(
            lambda x: (x.iloc[-1] - np.nanmean(x)) / (np.nanstd(x) if np.nanstd(x) > 0 else np.nan),
            raw=False
        )

        return [None if pd.isna(v) or np.isinf(v) else float(v) for v in z.tolist()]

    async def upsert_candles_1m(self, symbol: str, bars_1m: List[dict]) -> int:
        if not bars_1m:
            return 0

        # Функция для безопасного приведения типов CUSUM полей
        def safe_cusum_value(value, field_name):
            if value is None:
                if field_name == 'cusum_state':
                    return 0  # DEFAULT для INTEGER
                elif field_name == 'cusum_conf':
                    return 0.0  # DEFAULT для REAL
                else:
                    return None

            try:
                if field_name == 'cusum_state':
                    # Приведение к int для INTEGER поля
                    if isinstance(value, (int, float, np.integer)):
                        return int(value)
                    elif isinstance(value, str):
                        # Конвертация из текста в число если нужно
                        if value.upper() == 'BUY':
                            return 1
                        elif value.upper() == 'SELL':
                            return -1
                        else:
                            return 0
                    else:
                        return 0
                elif field_name in ['cusum', 'cusum_zscore', 'cusum_conf', 'cusum_price_mean',
                                    'cusum_price_std', 'cusum_pos', 'cusum_neg']:
                    # Приведение к float для REAL полей
                    return float(value)
                else:
                    return value
            except (ValueError, TypeError) as e:
                self.logger.warning(f"Failed to convert {field_name}: {value}, error: {e}")
                if field_name == 'cusum_state':
                    return 0
                elif field_name == 'cusum_conf':
                    return 0.0
                else:
                    return None

        rows = []
        nowms = int(datetime.now().timestamp() * 1000)
        for b in bars_1m:
            row_data = {
                "symbol": symbol,
                "ts": int(b["ts"]),
                "ts_close": int(b.get("ts_close", b["ts"] + 59_999)),
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b.get("volume", 0.0)),
                "count": int(b.get("count", 0)),
                "quote": float(b.get("quote", 0.0)),
                "finalized": int(b.get("finalized", 1)),
                "checksum": b.get("checksum"),
                "created_ts": int(b.get("created_ts", nowms)),
                "ema3": b.get("ema3"),
                "ema7": b.get("ema7"),
                "ema9": b.get("ema9"),
                "ema15": b.get("ema15"),
                "ema30": b.get("ema30"),
                "cmo14": b.get("cmo14"),
                "adx14": b.get("adx14"), "plus_di14": b.get("plus_di14"), "minus_di14": b.get("minus_di14"),
                "atr14": b.get("atr14"),
            }

            # Безопасное приведение CUSUM полей с проверкой типов
            cusum_fields = {
                'cusum': 'real',
                'cusum_state': 'integer',  # INTEGER поле в БД
                'cusum_zscore': 'real',
                'cusum_conf': 'real',
                'cusum_price_mean': 'real',
                'cusum_price_std': 'real',
                'cusum_pos': 'real',
                'cusum_neg': 'real',
                'cusum_reason': 'text'
            }

            for field, field_type in cusum_fields.items():
                if field_type == 'integer':
                    row_data[field] = safe_cusum_value(b.get(field), field)
                else:
                    row_data[field] = safe_cusum_value(b.get(field), field)

            # ДЕБАГ: Логируем типы перед сохранением
            self.logger.debug(f"cusum_state: value={row_data['cusum_state']}, type={type(row_data['cusum_state'])}")

            rows.append(row_data)

        sql = text(f"""
            INSERT OR REPLACE INTO {TABLES['candles_1m']}
            (symbol, ts, ts_close, open, high, low, close, volume, count, quote, finalized, checksum, created_ts,
             ema3, ema7, ema9, ema15, ema30, cmo14,
             adx14, plus_di14, minus_di14, atr14,
             cusum, cusum_state, cusum_zscore, cusum_conf, 
             cusum_price_mean, cusum_price_std, cusum_pos, cusum_neg, cusum_reason)
            VALUES (:symbol, :ts, :ts_close, :open, :high, :low, :close, :volume, :count, :quote, :finalized, :checksum, :created_ts,
                    :ema3, :ema7, :ema9, :ema15, :ema30, :cmo14,
                    :adx14, :plus_di14, :minus_di14, :atr14,
                    :cusum, :cusum_state, :cusum_zscore, :cusum_conf, 
                    :cusum_price_mean, :cusum_price_std, :cusum_pos, :cusum_neg, :cusum_reason)
        """)

        try:
            async with self.aengine.begin() as conn:
                await conn.execute(sql, rows)
            return len(rows)
        except Exception as e:
            self.logger.error(f"Failed to upsert candles_1m for {symbol}: {e}", exc_info=True)
            return 0

    async def warmup_1m_indicators_and_cusum(self,symbol: str,
            bars_1m: List[dict],is_gap_warmup: bool = False ) -> dict:
        """
        Инкрементальный разогрев 1m индикаторов (идентично онлайн-режиму)
        Избегает look-ahead bias - использует только прошлые данные
        1. Проверка ОБЩЕГО количества данных в БД (не только новых свечей)
        2. Лучше логирование
        3. Валидация final_state на основе РЕАЛЬНЫХ данных в БД
        """
        if not bars_1m:
            return {"ok": False, "state": 0, "z": 0.0, "conf": 0.0, "reason": "no_data"}

        # ✅ Выбираем правильный порог
        if is_gap_warmup:
            min_warm = int(self.cfg["features"]["cusum_1m"].get("min_warmup_gap", 60))
            warmup_type = "gap"
        else:
            min_warm = int(self.cfg["features"]["cusum_1m"]["min_warmup"])
            warmup_type = "standard"

        self.logger.info(
            f"🔥 Starting 1m warmup for {symbol}: "
            f"{len(bars_1m)} candles, "
            f"min_warmup={min_warm} ({warmup_type})"
        )
        min_warm = int(self.cfg["features"]["cusum_1m"]["min_warmup"])
        saved_count = 0
        error_count = 0

        self.logger.info(f"🔥 Starting 1m warmup for {symbol}: {len(bars_1m)} candles, min_warmup={min_warm}")

        # ✅ ПОСЛЕДОВАТЕЛЬНАЯ ОБРАБОТКА КАК В ОНЛАЙН-РЕЖИМЕ
        for i, current_bar in enumerate(bars_1m):
            try:
                tail_n = int(self.cfg["incremental"]["tail_1m_for_update"])

                # Определяем доступную историю (только ПРОШЛЫЕ данные)
                history_end = i
                history_start = max(0, history_end - tail_n + 1)
                history_bars = bars_1m[history_start:history_end]

                # Добавляем текущую сырую свечу
                all_bars = history_bars + [current_bar]

                # ✅ ИСПОЛЬЗУЕМ ТОТ ЖЕ МЕТОД ЧТО И В ОНЛАЙН-РЕЖИМЕ
                result = await self._update_1m_indicators_for_last_candle(symbol, all_bars)

                if result.get("ok"):
                    saved_count += 1

                # Логируем прогресс
                if (i + 1) % 100 == 0:
                    self.logger.info(
                        f"📈 Processed {i + 1}/{len(bars_1m)} candles, "
                        f"z={result.get('z', 0.0):.3f}, state={result.get('state', 'N/A')}"
                    )

            except Exception as e:
                error_count += 1
                self.logger.error(f"❌ Error processing 1m bar {i} (ts={current_bar.get('ts')}): {e}")
                # Сохраняем сырую свечу как fallback
                await self.upsert_candles_1m(symbol, [current_bar])

        # ✅ НОВАЯ ЛОГИКА: Проверяем РЕАЛЬНОЕ количество данных в БД
        try:
            all_candles_in_db = await self.read_candles_1m(symbol, last_n=min_warm + 10)
            total_available = len(all_candles_in_db)
        except Exception as e:
            self.logger.error(f"Failed to read candles from DB: {e}")
            total_available = len(bars_1m)

        # ✅ ФИНАЛЬНОЕ СОСТОЯНИЕ (берем из последней рассчитанной свечи)
        final_state = {"ok": False, "state": 0, "z": 0.0, "conf": 0.0, "reason": "no_data"}

        if saved_count > 0:
            last_candles = await self.read_candles_1m(symbol, last_n=1)
            if last_candles:
                last_candle = last_candles[0]
                state_val = last_candle.get("cusum_state", 0)
                z_val = last_candle.get("cusum_zscore", 0.0)
                conf_val = last_candle.get("cusum_conf", 0.0)

                # ✅ Валидация значений
                try:
                    state_val = int(state_val) if state_val is not None else 0
                    z_val = float(z_val) if z_val is not None else 0.0
                    conf_val = float(conf_val) if conf_val is not None else 0.0
                except (TypeError, ValueError) as e:
                    self.logger.warning(f"Failed to convert state values: {e}")
                    state_val = 0
                    z_val = 0.0
                    conf_val = 0.0

                # ✅ КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Проверяем ОБЩЕЕ количество данных в БД
                is_ready = total_available >= min_warm

                final_state = {
                    "ok": is_ready,
                    "state": state_val,
                    "z": z_val,
                    "conf": conf_val,
                    "reason": "warmup_completed" if is_ready else f"insufficient_total_{total_available}/{min_warm}"
                }

        self.logger.info(
            f"✅ 1m warmup completed:\n"
            f"  Symbol: {symbol}\n"
            f"  Processed new: {saved_count}/{len(bars_1m)} candles\n"
            f"  Errors: {error_count}\n"
            f"  Total in DB: {total_available} candles\n"
            f"  Required: {min_warm}\n"
            f"  Ready: {final_state['ok']}\n"
            f"  Final state: {final_state['state']}, z={final_state['z']:.3f}, conf={final_state['conf']:.3f}"
        )

        return final_state

    async def update_1m_cusum(self, symbol: str, new_bar_1m: dict) -> dict:
        """
        Обновляет CUSUM для ОДНОЙ новой 1m свечи.
        ✅ ИСПРАВЛЕНИЯ:
        1. Добавить логирование
        2. Добавить валидацию new_bar_1m
        3. Обработка ошибок
        """
        if not new_bar_1m:
            self.logger.error("new_bar_1m is empty")
            return {"ok": False, "state": 0, "z": 0.0, "conf": 0.0, "reason": "empty_bar"}

        try:
            tail_n = int(self.cfg["incremental"]["tail_1m_for_update"])
            tail_desc = await self.read_candles_1m(symbol, last_n=tail_n - 1)
            history = list(reversed(tail_desc)) if tail_desc else []
            history.append(new_bar_1m)

            self.logger.debug(
                f"update_1m_cusum: symbol={symbol}, history_len={len(history)}, new_bar_ts={new_bar_1m.get('ts')}")

            result = await self._update_1m_indicators_for_last_candle(symbol, history)

            if result.get("ok"):
                self.logger.debug(
                    f"✅ CUSUM updated for {symbol}: z={result.get('z', 0.0):.3f}, state={result.get('state', 0)}")
            else:
                self.logger.warning(f"⚠️ CUSUM not ready yet for {symbol}: {result.get('reason', 'unknown')}")

            return result

        except Exception as e:
            self.logger.error(f"Error in update_1m_cusum for {symbol}: {e}", exc_info=True)
            return {"ok": False, "state": 0, "z": 0.0, "conf": 0.0, "reason": f"error: {str(e)[:50]}"}

    async def _update_1m_indicators_for_last_candle(self, symbol: str, bars_1m: List[dict]) -> dict:
        """
        Рассчитывает индикаторы для ТОЛЬКО ПОСЛЕДНЕЙ свечи.
        ✅ ИСПРАВЛЕННАЯ ВЕРСИЯ
        """
        if not bars_1m:
            return {"ok": False, "state": 0, "z": 0.0, "conf": 0.0, "reason": "no_data"}

        min_warm = int(self.cfg["features"]["cusum_1m"]["min_warmup"])
        n = len(bars_1m)
        last_idx = n - 1
        new_bar = bars_1m[last_idx]

        # ✅ Извлекаем массивы
        closes = [float(b["close"]) for b in bars_1m]
        highs = [float(b["high"]) for b in bars_1m]
        lows = [float(b["low"]) for b in bars_1m]

        # ✅ Рассчитываем CUSUM
        cusum_results = self.calculate_cusum(pd.Series(closes), self.cusum_config_1m)

        # ✅ ДОБАВИТЬ: Рассчитываем EMA индикаторы
        ema3_vals = self._ema_series(closes, 3)
        ema7_vals = self._ema_series(closes, 7)
        ema9_vals = self._ema_series(closes, 9)
        ema15_vals = self._ema_series(closes, 15)
        ema30_vals = self._ema_series(closes, 30)

        # ✅ ДОБАВИТЬ: Рассчитываем технические индикаторы
        cmo_vals = self._cmo_series(closes, 14)
        dmi_data = self._dmi_adx_series(highs, lows, closes, 14)

        enriched_bar = dict(new_bar)
        i = last_idx

        # ✅ ИСПРАВЛЕНИЕ: Используем то же окно что и в calculate_cusum
        win = int(self.cusum_config_1m.normalize_window)
        start_idx = max(0, i - win + 1)
        close_window = closes[start_idx:i + 1]

        # ✅ Вычисляем mean/std ТОЛЬКО если достаточно данных
        if len(close_window) >= win:
            cusum_price_mean = float(np.mean(close_window))
            cusum_price_std = float(np.std(close_window, ddof=0))
        else:
            cusum_price_mean = None
            cusum_price_std = None

        # ✅ Извлекаем значения CUSUM
        cusum_val = cusum_results['cusum'].iloc[i] if i < len(cusum_results['cusum']) else None
        cusum_zscore = cusum_results['cusum_zscore'].iloc[i] if i < len(cusum_results['cusum_zscore']) else None
        state_val = int(cusum_results['cusum_state'].iloc[i]) if i < len(cusum_results['cusum_state']) else 0

        if cusum_val is not None and cusum_zscore is not None:
            conf = abs(cusum_zscore) * float(self.cfg["features"]["cusum_1m"]["z_to_conf"])

            enriched_bar.update({
                "cusum": float(cusum_val),
                "cusum_state": state_val,  # ✅ INTEGER: 1, -1, 0
                "cusum_zscore": float(cusum_zscore),
                "cusum_conf": float(conf),
                "cusum_price_mean": float(cusum_price_mean) if cusum_price_mean is not None else None,
                "cusum_price_std": float(cusum_price_std) if cusum_price_std is not None else None,
                "cusum_pos": float(cusum_results['cusum_pos'].iloc[i]),
                "cusum_neg": float(cusum_results['cusum_neg'].iloc[i]),
                "cusum_reason": f"z={cusum_zscore:.2f}"
            })
        else:
            enriched_bar.update({
                "cusum": None,
                "cusum_state": 0,
                "cusum_zscore": None,
                "cusum_conf": 0.0,
                "cusum_price_mean": None,
                "cusum_price_std": None,
                "cusum_pos": None,
                "cusum_neg": None,
                "cusum_reason": "insufficient_data"
            })

        #  Заполняем EMA поля
        enriched_bar.update({
            "ema3": float(ema3_vals[i]) if i < len(ema3_vals) and ema3_vals[i] is not None else None,
            "ema7": float(ema7_vals[i]) if i < len(ema7_vals) and ema7_vals[i] is not None else None,
            "ema9": float(ema9_vals[i]) if i < len(ema9_vals) and ema9_vals[i] is not None else None,
            "ema15": float(ema15_vals[i]) if i < len(ema15_vals) and ema15_vals[i] is not None else None,
            "ema30": float(ema30_vals[i]) if i < len(ema30_vals) and ema30_vals[i] is not None else None,
            "cmo14": float(cmo_vals[i]) if i < len(cmo_vals) and cmo_vals[i] is not None else None,
            "adx14": float(dmi_data[2][i]) if i < len(dmi_data[2]) and dmi_data[2][i] is not None else None,
            "plus_di14": float(dmi_data[0][i]) if i < len(dmi_data[0]) and dmi_data[0][i] is not None else None,
            "minus_di14": float(dmi_data[1][i]) if i < len(dmi_data[1]) and dmi_data[1][i] is not None else None,
            "atr14": float(dmi_data[3][i]) if i < len(dmi_data[3]) and dmi_data[3][i] is not None else None,
        })

        await self.upsert_candles_1m(symbol, [enriched_bar])

        # ✅ Состояние (для логирования, не сохраняем в БД)
        ready = n >= min_warm
        return {
            "ok": ready,
            "state": state_val,  # ✅ INTEGER
            "z": float(cusum_zscore) if cusum_zscore is not None else 0.0,
            "conf": float(
                abs(cusum_zscore) * self.cfg["features"]["cusum_1m"]["z_to_conf"]) if cusum_zscore is not None else 0.0,
            "reason": "ready" if ready else "warmup"
        }

    async def calc_indicators_10s_history(self, symbol: str, candles_10s: List[dict]) -> List[dict]:
        """
        Инкрементальный расчёт индикаторов для исторических 10s свечей.
        Аналогично warmup_1m_indicators_and_cusum, но для 10s таймфрейма.

        Args:
            symbol: торговый символ
            candles_10s: список 10s свечей (должны быть отсортированы по ts)

        Returns:
            список свечей с рассчитанными индикаторами
        """
        if not candles_10s:
            self.logger.warning(f"calc_indicators_10s_history: no data for {symbol}")
            return candles_10s

        total = len(candles_10s)
        saved_count = 0
        tail_n = 30  # Окно для расчёта индикаторов

        self.logger.info(f"🔄 Starting 10s indicators calculation for {symbol}: {total} candles")

        # Инкрементальная обработка каждой свечи
        for i in range(total):
            try:
                # Берём историю только из ПРОШЛЫХ данных (избегаем look-ahead bias)
                history_end = i
                history_start = max(0, history_end - tail_n + 1)
                history_bars = candles_10s[history_start:history_end]

                # Добавляем текущую свечу
                all_bars = history_bars + [candles_10s[i]]

                # Рассчитываем индикаторы для последней свечи
                updated_bar = self._calculate_single_10s_indicators(symbol, all_bars)

                if updated_bar:
                    # Обновляем свечу в списке
                    candles_10s[i].update(updated_bar)
                    saved_count += 1

                # Логируем прогресс каждые 10 свечей или в конце
                if (i + 1) % 10 == 0 or (i + 1) == total:
                    self.logger.info(f"📈 Processed {i + 1}/{total} 10s candles")

            except Exception as e:
                self.logger.error(f"❌ Error processing 10s bar {i} (ts={candles_10s[i].get('ts')}): {e}")
                # Оставляем свечу с None индикаторами (уже есть в исходных данных)

        self.logger.info(
            f"✅ 10s indicators calculation completed: {saved_count}/{total} candles processed"
        )

        return candles_10s

    def _calculate_single_10s_indicators(self, symbol: str, bars_10s: List[dict]) -> Optional[dict]:
        """
        Рассчитывает индикаторы для ПОСЛЕДНЕЙ 10s свечи используя историю.

        Args:
            symbol: торговый символ
            bars_10s: история свечей + текущая (последняя будет обработана)

        Returns:
            dict с рассчитанными индикаторами или None при ошибке
        """
        if not bars_10s:
            return None

        try:
            # Извлекаем данные
            closes = [float(b['close']) for b in bars_10s]
            highs = [float(b['high']) for b in bars_10s]
            lows = [float(b['low']) for b in bars_10s]

            # Минимальное окно для расчёта
            min_len = 14  # для CMO и ATR
            if len(closes) < min_len:
                # Возвращаем None индикаторы
                return {
                    'ema3': None, 'ema9': None,
                    'cmo_14': None, 'atr_14': None,
                    'roc_3': None, 'roc_6': None,
                    'entry_signal': None,
                    'entry_confidence': None,
                    'entry_reason': 'insufficient_data'
                }

            # Рассчитываем индикаторы
            ema3_vals = self._ema_series(closes, 3)
            ema9_vals = self._ema_series(closes, 9)
            cmo_vals = self._cmo_series(closes, 14)
            atr_vals = self._atr_series(highs, lows, closes, 14)
            roc3_vals = self._roc_series(closes, 3)
            roc6_vals = self._roc_series(closes, 6)

            # Берём значения для последней свечи
            ema3 = ema3_vals[-1]
            ema9 = ema9_vals[-1]
            cmo_14 = cmo_vals[-1]
            atr_14 = atr_vals[-1]
            roc_3 = roc3_vals[-1]
            roc_6 = roc6_vals[-1]

            # Логика entry signal (как в market_aggregator)
            entry_signal = None
            entry_confidence = 0.0
            entry_reason = "no_signal"

            if ema3 is not None and ema9 is not None and cmo_14 is not None:
                cmo_value = cmo_14 if cmo_14 is not None else 0

                if ema3 > ema9 and cmo_value > 20:
                    entry_signal = 1  # BUY
                    entry_confidence = min(abs(cmo_value) / 100.0, 1.0)
                    entry_reason = "ema_cross_up_cmo_positive"
                elif ema3 < ema9 and cmo_value < -20:
                    entry_signal = -1  # SELL
                    entry_confidence = min(abs(cmo_value) / 100.0, 1.0)
                    entry_reason = "ema_cross_down_cmo_negative"
                else:
                    entry_signal = 0  # HOLD
                    entry_confidence = 0.0
                    entry_reason = "no_clear_trend"
            else:
                entry_reason = "insufficient_data"

            # Формируем результат
            return {
                'ema3': ema3,
                'ema9': ema9,
                'cmo_14': cmo_14,
                'atr_14': atr_14,
                'roc_3': roc_3,
                'roc_6': roc_6,
                'entry_signal': entry_signal,
                'entry_confidence': entry_confidence,
                'entry_reason': entry_reason
            }

        except Exception as e:
            self.logger.error(f"Error in _calculate_single_10s_indicators: {e}")
            return None

    @staticmethod
    def _roc_series(closes: List[float], period: int) -> List[Optional[float]]:
        """
        Rate of Change (ROC) индикатор.
        ROC = (Close[i] - Close[i-period]) / Close[i-period] * 100

        Args:
            closes: цены закрытия
            period: период для расчёта

        Returns:
            список ROC значений
        """
        result: List[Optional[float]] = []

        for i in range(len(closes)):
            if i < period:
                result.append(None)
            else:
                prev_close = closes[i - period]
                if prev_close != 0:
                    roc = ((closes[i] - prev_close) / prev_close) * 100.0
                    result.append(roc)
                else:
                    result.append(None)

        return result

    async def upsert_candles_5m(self, symbol: str, bars_5m: List[dict]) -> int:
        if not bars_5m:
            return 0
        sql = text(f"""
            INSERT OR REPLACE INTO {TABLES['candles_5m']}
            (symbol, ts, ts_close, open, high, low, close, volume, count, quote, finalized, checksum, created_ts,
             price_change_5, cmo_14, macd_histogram,
             adx_14, plus_di_14, minus_di_14, atr_14_normalized,
             bb_width, bb_position,
             trend_momentum_z, trend_acceleration_ema7,
             regime_volatility,
             volume_ratio_ema3,
             candle_relative_body, upper_shadow_ratio, lower_shadow_ratio,
             price_vs_vwap,
             cusum_1m_recent, cusum_1m_quality_score,
             cusum_1m_trend_aligned, cusum_1m_price_move,
             is_trend_pattern_1m, body_to_range_ratio_1m, close_position_in_range_1m,
             -- Группа 8: CUSUM 5m
             cusum, cusum_state, cusum_zscore, cusum_conf,
             cusum_reason,
             cusum_price_mean, cusum_price_std,
             cusum_pos, cusum_neg)
            VALUES (:symbol, :ts, :ts_close, :open, :high, :low, :close, :volume, :count, :quote, :finalized, :checksum, :created_ts,
                    :price_change_5, :cmo_14, :macd_histogram,
                    :adx_14, :plus_di_14, :minus_di_14, :atr_14_normalized,
                    :bb_width, :bb_position,
                    :trend_momentum_z, :trend_acceleration_ema7,
                    :regime_volatility,
                    :volume_ratio_ema3,
                    :candle_relative_body, :upper_shadow_ratio, :lower_shadow_ratio,
                    :price_vs_vwap,
                    :cusum_1m_recent, :cusum_1m_quality_score,
                    :cusum_1m_trend_aligned, :cusum_1m_price_move,
                    :is_trend_pattern_1m, :body_to_range_ratio_1m, :close_position_in_range_1m,
                    -- Группа 8: CUSUM 5m
                    :cusum, :cusum_state, :cusum_zscore, :cusum_conf,
                    :cusum_reason,
                    :cusum_price_mean, :cusum_price_std,
                    :cusum_pos, :cusum_neg)
        """)

        rows = []

        for b in bars_5m:
            # Для исторических данных created_ts = ts_close
            ts_close = int(b.get("ts_close", b["ts"] + 299_999))

            rows.append({
                "symbol": symbol,
                "ts": int(b["ts"]),
                "ts_close": ts_close,
                "open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]), "close": float(b["close"]),
                "volume": float(b.get("volume", 0.0)), "count": int(b.get("count", 0)),
                "quote": float(b.get("quote", 0.0)),
                "finalized": int(b.get("finalized", 1)),
                "checksum": b.get("checksum"),
                # ИСПРАВЛЕНИЕ: created_ts = ts_close для исторических данных
                "created_ts": ts_close,

                "price_change_5": b.get("price_change_5"),
                "cmo_14": b.get("cmo_14"),
                "macd_histogram": b.get("macd_histogram"),
                "adx_14": b.get("adx_14"), "plus_di_14": b.get("plus_di_14"), "minus_di_14": b.get("minus_di_14"),
                "atr_14_normalized": b.get("atr_14_normalized"),
                "bb_width": b.get("bb_width"), "bb_position": b.get("bb_position"),

                "trend_momentum_z": b.get("trend_momentum_z"),
                "trend_acceleration_ema7": b.get("trend_acceleration_ema7"),
                "regime_volatility": b.get("regime_volatility"),
                "volume_ratio_ema3": b.get("volume_ratio_ema3"),
                "candle_relative_body": b.get("candle_relative_body"),
                "upper_shadow_ratio": b.get("upper_shadow_ratio"),
                "lower_shadow_ratio": b.get("lower_shadow_ratio"),
                "price_vs_vwap": b.get("price_vs_vwap"),
                "cusum_1m_recent": b.get("cusum_1m_recent"),
                "cusum_1m_quality_score": b.get("cusum_1m_quality_score"),
                "cusum_1m_trend_aligned": b.get("cusum_1m_trend_aligned"),
                "cusum_1m_price_move": b.get("cusum_1m_price_move"),
                "is_trend_pattern_1m": b.get("is_trend_pattern_1m"),
                "body_to_range_ratio_1m": b.get("body_to_range_ratio_1m"),
                "close_position_in_range_1m": b.get("close_position_in_range_1m"),

                # CUSUM 5m
                "cusum": b.get("cusum"),
                "cusum_state": b.get("cusum_state"),
                "cusum_zscore": b.get("cusum_zscore"),
                "cusum_conf": b.get("cusum_conf"),
                "cusum_reason": b.get("cusum_reason"),
                "cusum_price_mean": b.get("cusum_price_mean"),
                "cusum_price_std": b.get("cusum_price_std"),
                "cusum_pos": b.get("cusum_pos"),
                "cusum_neg": b.get("cusum_neg"),
            })

        try:
            async with self.aengine.begin() as conn:
                await conn.execute(sql, rows)
            return len(rows)
        except Exception as e:
            self.logger.error(f"Failed to upsert candles_5m for {symbol}: {e}", exc_info=True)
            return 0

    async def read_candles_1m(
        self, symbol: str, start_ts: Optional[int] = None, end_ts: Optional[int] = None, last_n: Optional[int] = None
    ) -> List[dict]:
        """
        Чтение 1m свечей из БД.
        ✅ ИСПРАВЛЕНО: Данные возвращаются в порядке ASC (от старых к новым, от меньшего ts к большему)
        """
        try:
            async with self.aengine.connect() as conn:
                if last_n:
                    sql = text(f"""
                        SELECT * FROM {TABLES['candles_1m']}
                        WHERE symbol = :symbol
                        ORDER BY ts DESC
                        LIMIT :limit
                    """)
                    res = await conn.execute(sql, {"symbol": symbol, "limit": int(last_n)})
                    rows = res.mappings().all()
                    # ✅ ИСПРАВЛЕНИЕ: Реверсируем результат для порядка ASC (от старых к новым, от меньшего ts к большему)
                    return list(reversed([dict(r) for r in rows]))
                elif start_ts is not None and end_ts is not None:
                    sql = text(f"""
                        SELECT * FROM {TABLES['candles_1m']}
                        WHERE symbol = :symbol AND ts >= :start AND ts <= :end
                        ORDER BY ts ASC
                    """)
                    res = await conn.execute(sql, {"symbol": symbol, "start": int(start_ts), "end": int(end_ts)})
                    rows = res.mappings().all()
                    return [dict(r) for r in rows]
                else:
                    sql = text(f"""
                        SELECT * FROM {TABLES['candles_1m']}
                        WHERE symbol = :symbol
                        ORDER BY ts ASC
                    """)
                    res = await conn.execute(sql, {"symbol": symbol})
                    rows = res.mappings().all()
                    return [dict(r) for r in rows]
        except Exception as e:
            self.logger.error(f"read_candles_1m failed: {e}", exc_info=True)
            return []

    async def read_candles_5m(
            self,
            symbol: str,
            last_n: Optional[int] = None,
            start_ts: Optional[int] = None,
            end_ts: Optional[int] = None
    ) -> List[dict]:
        """
        Чтение 5m свечей из БД.

        Поддерживает два режима:
          1. last_n=N — последние N свечей (по умолчанию: 200)
          2. start_ts, end_ts — диапазон по времени [start_ts, end_ts] включительно

        ✅ Возвращает в порядке ASC (от старых к новым, от меньшего ts к большему)
        """
        try:
            async with self.aengine.connect() as conn:
                if start_ts is not None and end_ts is not None:
                    # Режим по диапазону времени
                    query = text(f"""
                        SELECT * FROM {TABLES['candles_5m']}
                        WHERE symbol = :symbol
                          AND ts >= :start_ts
                          AND ts <= :end_ts
                        ORDER BY ts ASC
                    """)
                    result = await conn.execute(query, {
                        "symbol": symbol,
                        "start_ts": int(start_ts),
                        "end_ts": int(end_ts)
                    })
                    rows = result.mappings().all()
                    return [dict(r) for r in rows]

                else:
                    # Режим last_n (совместимость со старыми вызовами)
                    limit = int(last_n) if last_n is not None else 200
                    query = text(f"""
                        SELECT * FROM {TABLES['candles_5m']}
                        WHERE symbol = :symbol
                        ORDER BY ts DESC
                        LIMIT :limit
                    """)
                    result = await conn.execute(query, {"symbol": symbol, "limit": limit})
                    rows = result.mappings().all()
                    # ✅ Важно: возвращаем в ASC порядке (от старых к новым)
                    data = [dict(row) for row in rows]
                    data.reverse()
                    return data

        except Exception as e:
            self.logger.error(f"Error reading 5m candles for {symbol}: {e}", exc_info=True)
            return []

    async def get_backtest_range(self, symbols: List[str]) -> Tuple[int, int]:
        """
        Возвращает (start_ts, end_ts) по пересечению доступных данных 5m для всех символов.
        """
        if not symbols:
            raise ValueError("symbols must be non-empty")
        start_ts: Optional[int] = None
        end_ts: Optional[int] = None
        try:
            async with self.aengine.connect() as conn:
                for sym in symbols:
                    qmin = text(f"SELECT MIN(ts) AS min_ts FROM {TABLES['candles_5m']} WHERE symbol=:s")
                    qmax = text(f"SELECT MAX(ts) AS max_ts FROM {TABLES['candles_5m']} WHERE symbol=:s")
                    rmin = await conn.execute(qmin, {"s": sym})
                    rmax = await conn.execute(qmax, {"s": sym})
                    min_ts = (await rmin.mappings().first())["min_ts"]
                    max_ts = (await rmax.mappings().first())["max_ts"]
                    if min_ts is None or max_ts is None:
                        raise RuntimeError(f"No 5m data for symbol {sym}")
                    min_ts = int(min_ts)
                    max_ts = int(max_ts)
                    start_ts = max(start_ts, min_ts) if start_ts is not None else min_ts
                    end_ts = min(end_ts, max_ts) if end_ts is not None else max_ts
            if start_ts is None or end_ts is None or start_ts > end_ts:
                raise RuntimeError("Invalid backtest range")
            return start_ts, end_ts
        except Exception as e:
            self.logger.error(f"get_backtest_range failed: {e}", exc_info=True)
            raise

    # ======================================================================
    # ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ (sync расчёт)
    # ======================================================================

    @staticmethod
    def _ema_series(close: List[float], period: int) -> List[Optional[float]]:
        if period <= 0:
            return [None] * len(close)
        alpha = 2.0 / (period + 1.0)
        out: List[Optional[float]] = []
        ema: Optional[float] = None  # Может быть None до инициализации

        for i, v in enumerate(close):
            if v is None:
                out.append(None)
                continue

            vf = float(v)

            # Инициализация seed только после накопления period значений
            if ema is None:
                if i < period - 1:
                    out.append(None)
                    continue
                elif i == period - 1:
                    ema = float(np.mean(close[:period]))  # начальное среднее
                else:
                    # fallback на seed при пропуске (редко)
                    ema = vf

            # Расчёт EMA после инициализации seed
            ema = (vf - ema) * alpha + ema
            out.append(ema)

        return out

    @staticmethod
    def _cmo_series(close: List[float], period: int = 14) -> List[Optional[float]]:
        if len(close) < period + 1:
            return [None] * len(close)
        ups = [0.0]
        downs = [0.0]
        for i in range(1, len(close)):
            ch = close[i] - close[i - 1]
            ups.append(max(ch, 0.0))
            downs.append(max(-ch, 0.0))
        ups_s = pd.Series(ups, dtype="float64").rolling(window=period, min_periods=period).sum()
        downs_s = pd.Series(downs, dtype="float64").rolling(window=period, min_periods=period).sum()
        cmo = 100.0 * (ups_s - downs_s) / (ups_s + downs_s).replace(0, np.nan)
        return [None if pd.isna(v) else float(v) for v in cmo.tolist()]

    @staticmethod
    def _bollinger_bands_features(close: List[float], period: int = 20, stdevs: float = 2.0) -> Tuple[List[Optional[float]], List[Optional[float]]]:
        s = pd.Series(close, dtype="float64")
        ma = s.rolling(window=period, min_periods=period).mean()
        std = s.rolling(window=period, min_periods=period).std(ddof=0)
        upper = ma + stdevs * std
        lower = ma - stdevs * std
        width = (upper - lower) / ma
        pos = (s - lower) / (upper - lower)
        return (
            [None if pd.isna(v) or np.isinf(v) else float(v) for v in width.tolist()],
            [None if pd.isna(v) or np.isinf(v) else float(v) for v in pos.tolist()],
        )

    @staticmethod
    def _atr_series(high: List[float], low: List[float], close: List[float], period: int = 14) -> List[Optional[float]]:
        """
        Average True Range (ATR) индикатор.

        Args:
            high: максимальные цены
            low: минимальные цены
            close: цены закрытия
            period: период для сглаживания

        Returns:
            список ATR значений
        """
        if len(high) < period + 1:
            return [None] * len(high)

        h = pd.Series(high, dtype="float64")
        l = pd.Series(low, dtype="float64")
        c = pd.Series(close, dtype="float64")

        prev_c = c.shift(1)

        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        tr1 = (h - l).abs()
        tr2 = (h - prev_c).abs()
        tr3 = (l - prev_c).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # ATR = RMA(TR, period) - используем EWM с alpha=1/period для Wilder's smoothing
        atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()

        return [None if pd.isna(v) or np.isinf(v) else float(v) for v in atr.tolist()]


    def _wilders_smoothing(self, series: pd.Series, period: int) -> pd.Series:
        if series is None or len(series) == 0 or period <= 1:
            return pd.Series(series, dtype="float64") if not isinstance(series, pd.Series) else series.astype("float64")

        s = pd.Series(series, dtype="float64").copy()
        out = pd.Series([np.nan] * len(s), index=s.index, dtype="float64")

        if len(s) < period:
            return out

        # Первое значение RMA = Simple Moving Average за первые 'period' элементов
        seed = float(s.iloc[:period].mean())
        out.iloc[period - 1] = seed  # <-- ПРАВИЛЬНО: .iloc[] для Series

        inv_p = 1.0 / float(period)
        for i in range(period, len(s)):
            prev = out.iloc[i - 1]
            current_val = s.iloc[i]
            if pd.isna(current_val):
                out.iloc[i] = np.nan
            else:
                rma = prev + (current_val - prev) * inv_p
                out.iloc[i] = rma

        return out

    def _dmi_adx_series(
        self, high: List[float], low: List[float], close: List[float], period: int = 14
    ) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
        h = pd.Series(high, dtype="float64")
        l = pd.Series(low, dtype="float64")
        c = pd.Series(close, dtype="float64")

        prev_h = h.shift(1)
        prev_l = l.shift(1)
        prev_c = c.shift(1)

        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm = pd.Series(0.0, index=h.index, dtype="float64")
        minus_dm = pd.Series(0.0, index=h.index, dtype="float64")
        cond_plus = (up_move > down_move) & (up_move > 0)
        cond_minus = (down_move > up_move) & (down_move > 0)
        plus_dm[cond_plus] = up_move[cond_plus]
        minus_dm[cond_minus] = down_move[cond_minus]

        tr1 = (h - l).abs()
        tr2 = (h - prev_c).abs()
        tr3 = (l - prev_c).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        plus_dm_rma = self._wilders_smoothing(plus_dm, period)
        minus_dm_rma = self._wilders_smoothing(minus_dm, period)
        tr_rma = self._wilders_smoothing(tr, period)

        valid = tr_rma > 0
        di_plus = pd.Series(np.nan, index=h.index, dtype="float64")
        di_minus = pd.Series(np.nan, index=h.index, dtype="float64")
        di_plus[valid] = 100.0 * (plus_dm_rma[valid] / tr_rma[valid])
        di_minus[valid] = 100.0 * (minus_dm_rma[valid] / tr_rma[valid])

        denom = (di_plus + di_minus)
        dx = pd.Series(np.nan, index=h.index, dtype="float64")
        nz = denom > 0
        dx[nz] = 100.0 * (di_plus[nz] - di_minus[nz]).abs() / denom[nz]

        adx = self._wilders_smoothing(dx, period)

        def tolist_opt(s: pd.Series) -> List[Optional[float]]:
            return [None if (pd.isna(v) or np.isinf(v)) else float(v) for v in s.tolist()]

        return (
            tolist_opt(di_plus),
            tolist_opt(di_minus),
            tolist_opt(adx),
            tolist_opt(tr_rma),  # ATR raw (RMA TR)
        )  # :contentReference[oaicite:4]{index=4}

    @staticmethod
    def _macd_series(close: List[float], fast: int, slow: int, signal: int) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
        s = pd.Series(close, dtype="float64")
        ema_fast = s.ewm(span=fast, adjust=False).mean()
        ema_slow = s.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - sig
        return (
            [None if pd.isna(v) else float(v) for v in macd.tolist()],
            [None if pd.isna(v) else float(v) for v in sig.tolist()],
            [None if pd.isna(v) else float(v) for v in hist.tolist()],
        )

    @staticmethod
    def _calculate_vwap(bars_5m: List[dict], period: int = 96) -> List[Optional[float]]:
        """
        Расчет VWAP со скользящим окном.

        Args:
            bars_5m: список свечей 5m с полями close, volume
            period: размер скользящего окна (по умолчанию 96 свечей)

        Returns:
            список VWAP значений (None для первых period-1 свечей)
        """
        vwap: List[Optional[float]] = []
        n = len(bars_5m)

        for i in range(n):
            # Для первых period-1 свечей возвращаем None
            if i < period - 1:
                vwap.append(None)
                continue

            # Вычисляем VWAP для окна [i-period+1, i]
            start_idx = i - period + 1
            total_pv = 0.0
            total_v = 0.0

            for j in range(start_idx, i + 1):
                p = float(bars_5m[j]["close"])
                v = float(bars_5m[j].get("volume", 0.0))
                total_pv += p * v
                total_v += v

            vwap.append((total_pv / total_v) if total_v > 0 else None)

        return vwap


    @staticmethod
    def _z_score_series(values: List[float], window: int = 20) -> List[Optional[float]]:
        """
        Расчет Z-score для серии значений.
        trend_momentum_z = Z-score от price_change_5

        Args:
            values: список значений (например, price_change_5)
            window: окно для расчета скользящего среднего и std

        Returns:
            список Z-score значений
        """
        s = pd.Series(values, dtype="float64")
        rolling_mean = s.rolling(window=window, min_periods=window).mean()
        rolling_std = s.rolling(window=window, min_periods=window).std(ddof=1)
        z_scores = (s - rolling_mean) / rolling_std
        return [None if pd.isna(v) or np.isinf(v) else float(v) for v in z_scores.tolist()]

    @staticmethod
    def _trend_acceleration_series(ema_values: List[Optional[float]]) -> List[Optional[float]]:
        """
        Расчет ускорения тренда как изменение EMA.
        trend_acceleration_ema7 = EMA(7)[i] - EMA(7)[i-1]

        Args:
            ema_values: значения EMA(7)

        Returns:
            список изменений EMA (ускорение)
        """
        result: List[Optional[float]] = [None]  # первое значение всегда None
        for i in range(1, len(ema_values)):
            if ema_values[i] is not None and ema_values[i - 1] is not None:
                result.append(ema_values[i] - ema_values[i - 1])
            else:
                result.append(None)
        return result

    @staticmethod
    def _regime_volatility_series(atr: List[Optional[float]], close: List[float]) -> List[Optional[float]]:
        """
        Расчет нормализованной волатильности.
        regime_volatility = ATR(14) / Close

        Args:
            atr: значения ATR(14)
            close: цены закрытия

        Returns:
            список нормализованной волатильности
        """
        result: List[Optional[float]] = []
        for i in range(len(close)):
            if atr[i] is not None and close[i] is not None and close[i] != 0:
                result.append(atr[i] / close[i])
            else:
                result.append(None)
        return result


    @staticmethod
    def _volume_ratio_ema3_series(volume: List[float], ema_period: int = 3) -> List[Optional[float]]:
        """
        Отношение объема к его EMA.
        volume_ratio_ema3 = Volume / EMA(Volume, 3)
        Args:
            volume: объемы
            ema_period: период EMA для объема
        Returns:
            отношение объема к EMA объема
        """
        ema_vol = MarketDataUtils._ema_series(volume, ema_period)
        result: List[Optional[float]] = []
        for i in range(len(volume)):
            if ema_vol[i] is not None and ema_vol[i] != 0:
                result.append(volume[i] / ema_vol[i])
            else:
                result.append(None)
        return result

    @staticmethod
    def _candle_body_ratios(open_prices: List[float], high: List[float],
                            low: List[float], close: List[float]) -> tuple[List[Optional[float]],
    List[Optional[float]],
    List[Optional[float]]]:
        """
        Метрики тела свечи и теней (нормализация к диапазону high-low).

        Args:
            open_prices: цены открытия
            high: максимальные цены
            low: минимальные цены
            close: цены закрытия

        Returns:
            (candle_relative_body, upper_shadow_ratio, lower_shadow)
            - candle_relative_body: размер тела / диапазон (high-low)
            - upper_shadow_ratio: верхняя тень / диапазон
            - lower_shadow_ratio: нижняя тень / диапазон
        """
        relative_body: List[Optional[float]] = []
        upper_shadow_ratio: List[Optional[float]] = []
        lower_shadow: List[Optional[float]] = []

        for i in range(len(close)):
            candle_range = high[i] - low[i]
            body = abs(close[i] - open_prices[i])

            if candle_range > 0:
                # Относительный размер тела
                relative_body.append(body / candle_range)

                # Верхняя и нижняя тени
                if close[i] >= open_prices[i]:  # бычья свеча
                    upper_shadow_ratio.append((high[i] - close[i]) / candle_range)
                    lower_shadow.append((open_prices[i] - low[i]) / candle_range)
                else:  # медвежья свеча
                    upper_shadow_ratio.append((high[i] - open_prices[i]) / candle_range)
                    lower_shadow.append((close[i] - low[i]) / candle_range)
            else:
                relative_body.append(0.0)
                upper_shadow_ratio.append(0.0)
                lower_shadow.append(0.0)

        return relative_body, upper_shadow_ratio, lower_shadow

    @staticmethod
    def _price_vs_vwap_series(close: List[float], vwap: List[Optional[float]]) -> List[Optional[float]]:
        """
        Отклонение цены от VWAP.
        price_vs_vwap = (Close - VWAP) / VWAP

        Args:
            close: цены закрытия
            vwap: значения VWAP

        Returns:
            список отклонений от VWAP
        """
        result: List[Optional[float]] = []
        for i in range(len(close)):
            if vwap[i] is not None and vwap[i] != 0:
                result.append((close[i] - vwap[i]) / vwap[i])
            else:
                result.append(None)
        return result

    @staticmethod
    def _pattern_features_1m(open_price: float, high: float, low: float, close: float,
                             ema_short: Optional[float]) -> tuple[int, float, float]:
        """
        Паттерновые признаки для ОДНОЙ 1m свечи.

        Args:
            open_price: цена открытия
            high: максимум
            low: минимум
            close: цена закрытия
            ema_short: короткая EMA (например, EMA(7))

        Returns:
            (is_trend_pattern, body_to_range_ratio, close_position_in_range)
        """
        candle_range = high - low
        body = abs(close - open_price)

        # is_trend_pattern: сильное тело (>60%) + направление совпадает с EMA
        is_trend_pattern = 0
        if candle_range > 0 and body / candle_range > 0.6:
            if ema_short is not None:
                trend_dir = 1 if close > ema_short else -1
                candle_dir = 1 if close > open_price else -1
                is_trend_pattern = 1 if trend_dir == candle_dir else 0
            else:
                # Если нет EMA - проверяем только силу тела (fallback)
                is_trend_pattern = 1

        # body_to_range_ratio
        body_to_range = body / candle_range if candle_range > 0 else 0.0

        # close_position_in_range [0, 1]
        close_position = (close - low) / candle_range if candle_range > 0 else 0.5

        return is_trend_pattern, body_to_range, close_position
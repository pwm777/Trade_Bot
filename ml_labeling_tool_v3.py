"""
ml_labeling_tool_v3.py
 версия инструмента разметки
"""
import numpy as np
from sqlalchemy import text
import pandas as pd
import sys
import hashlib, json
from dataclasses import dataclass
from typing import Tuple, List, Dict,  Any
from datetime import datetime, UTC
import warnings
import logging
import traceback

# --- DDL для таблиц снапшота тренировочного датасета ---
CREATE_TRAINING_DATASET_SQL = """
CREATE TABLE IF NOT EXISTS training_dataset (
    run_id           TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    timeframe        TEXT    NOT NULL,
    ts               INTEGER NOT NULL,
    datetime         TEXT    NOT NULL,
    reversal_label   INTEGER NOT NULL,
    sample_weight    REAL    NOT NULL,
    cmo_14           REAL,
    volume           REAL,
    trend_acceleration_ema7     REAL,
    regime_volatility           REAL,
    bb_width                    REAL,
    adx_14                      REAL,
    plus_di_14                  REAL,
    minus_di_14                 REAL,
    atr_14_normalized           REAL,
    volume_ratio_ema3           REAL,
    candle_relative_body        REAL,
    upper_shadow_ratio          REAL,
    lower_shadow_ratio          REAL,
    price_vs_vwap               REAL,
    bb_position                 REAL,
    cusum_1m_recent             INTEGER,
    cusum_1m_quality_score      REAL,
    cusum_1m_trend_aligned      INTEGER,
    cusum_1m_price_move         REAL,
    is_trend_pattern_1m         INTEGER,
    body_to_range_ratio_1m      REAL,
    close_position_in_range_1m  REAL,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (run_id, symbol, ts)
);
"""
CREATE_TRAINING_DATASET_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_run_id      ON training_dataset(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_symbol      ON training_dataset(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_run_sym     ON training_dataset(run_id, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_sym_ts      ON training_dataset(symbol, ts)",
    "CREATE INDEX IF NOT EXISTS idx_training_dataset_run_ts      ON training_dataset(run_id, ts)"
]

CREATE_TRAINING_DATASET_META_SQL = """
CREATE TABLE IF NOT EXISTS training_dataset_meta (
    run_id             TEXT PRIMARY KEY,
    status             TEXT NOT NULL,               -- CREATING|READY|FAILED
    error_msg          TEXT,
    symbol             TEXT NOT NULL,
    timeframe          TEXT NOT NULL,
    range_start_ts     INTEGER,
    range_end_ts       INTEGER,
    rows_total         INTEGER,
    pos_count          INTEGER,
    neg_count          INTEGER,
    class_dist_json    TEXT,
    hold_bars          INTEGER,
    buffer_bars        INTEGER,
    seed               INTEGER,
    labeling_method    TEXT,
    feature_names_json TEXT,
    featureset_version TEXT,
    features_hash      TEXT,
    config_json        TEXT,
    nan_drop_rows      INTEGER,
    issues_json        TEXT,
    source_hashes_json TEXT,
    created_at         TEXT NOT NULL
);
"""
CREATE_TRAINING_FEATURE_IMPORTANCE_SQL = """
CREATE TABLE IF NOT EXISTS training_feature_importance (
    run_id      TEXT    NOT NULL,
    model_name  TEXT    NOT NULL,
    feature     TEXT    NOT NULL,
    importance  REAL    NOT NULL,
    rank        INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (run_id, model_name, feature)
);
"""
CREATE_TRAINING_FEATURE_IMPORTANCE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_tfi_run_id       ON training_feature_importance(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_tfi_model_run    ON training_feature_importance(model_name, run_id)"
]
# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

import ruptures as rpt
RUPTURES_AVAILABLE = True

from sqlalchemy.engine import Engine, create_engine
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MARKET_DB_DSN: str = f"sqlite:///{DATA_DIR}/market_data.sqlite"

@dataclass
class LabelingConfig:
    """Конфигурация разметки с SQLAlchemy"""
    db_engine: Engine = None
    symbol: str = "ETHUSDT"
    timeframe: str = "5m"

    # PELT Online
    pelt_window: int = 1000
    pelt_pen: float = 1
    pelt_min_size: int = 10
    pelt_confirm_bar: int = 3

    # CUSUM
    cusum_z_threshold: float = 3 # минимальный |cusum_zscore|,
    cusum_conf_threshold: float = 1  # минимальная cusum_conf
    hold_bars: int = 3               # Более короткий холд
    buffer_bars: int = 5             # Меньше буферных баров

    # Extremum (min/max)
    extremum_confirm_bar: int = 2
    extremum_window: int = 8
    min_signal_distance: int = 3
    # Фильтры
    method: str = "CUSUM_EXTREMUM"
    # PnL параметры
    fee_percent: float = 0.0004
    min_profit_target: float = 0.001
    tool: Any = None

    def __post_init__(self):
        if self.db_engine is None:
            self.db_engine = create_engine(MARKET_DB_DSN)


class DataLoader:
    """Улучшенный загрузчик данных с SQLAlchemy"""

    def __init__(self, db_engine: Engine = None, symbol: str = "ETHUSDT",
                 timeframe: str = "5m", config: LabelingConfig = None):
        self.db_engine = db_engine or create_engine(MARKET_DB_DSN)
        self.symbol = symbol
        self.timeframe = timeframe
        self.config = config  # ← Сохраняем config
        self.feature_names = []
        self._initialize_features()

    def _initialize_features(self):
        """Инициализация списка фич для ML"""
        self.feature_names = [
            # 'trend_momentum_z',  # ← УДАЛИТЬ (дубликат bb_position)
             'cmo_14',
            # 'macd_histogram',
            'volume',
            'trend_acceleration_ema7', 'regime_volatility', 'bb_width', 'adx_14',
            'plus_di_14',
            'minus_di_14', 'atr_14_normalized', 'volume_ratio_ema3',
            'candle_relative_body', 'upper_shadow_ratio', 'lower_shadow_ratio',
            'price_vs_vwap', 'bb_position', 'cusum_1m_recent', 'cusum_1m_quality_score',
            'cusum_1m_trend_aligned', 'cusum_1m_price_move', 'is_trend_pattern_1m',
            'body_to_range_ratio_1m', 'close_position_in_range_1m',
        ]

    def connect(self) -> Engine:
        """Установка соединения с БД через SQLAlchemy"""
        try:
            # Проверяем соединение
            with self.db_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info(f"✅ Подключено к БД через SQLAlchemy: {self.db_engine.url}")
            return self.db_engine
        except Exception as err:
            raise ConnectionError(f"Ошибка подключения к БД: {err}")

    def disconnect(self):
        """Закрытие соединения - для SQLAlchemy обычно не требуется"""
        # SQLAlchemy автоматически управляет соединениями
        logger.info("✅ Соединение с БД будет закрыто автоматически")

    def load_indicators(self) -> pd.DataFrame:
        """Загрузка данных с индикаторами через SQLAlchemy"""
        query = """
            SELECT * FROM candles_5m 
            WHERE symbol = ? 
            ORDER BY ts
        """
        try:
            df = pd.read_sql_query(query, self.db_engine, params=(self.symbol,))

            if df.empty:
                raise ValueError(f"Нет данных для символа {self.symbol}")

            # Преобразование времени
            if 'ts' in df.columns and 'datetime' not in df.columns:
                df['datetime'] = pd.to_datetime(df['ts'], unit='ms')

            required_cols = ["cusum", "cusum_state", "cusum_zscore", "cusum_conf"]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                logger.info("CUSUM 5m: отсутствуют колонки %s (таблица candles_5m).", missing)
            else:
                # guard: если исторически встречались строковые статусы
                df["cusum_state"] = df["cusum_state"].replace({"BUY": 1, "SELL": -1, "HOLD": 0})
                # итог: Int64-категория 1/2/0, NaN -> 0
                df["cusum_state"] = pd.to_numeric(df["cusum_state"], errors="coerce").fillna(0).astype("Int64")

                # alias для отчётов/моделей (совпадает с кодировкой инструмента разметки)
                df["cusum_signal"] = df["cusum_state"]

                # числовые столбцы безопасно к float
                df["cusum"] = pd.to_numeric(df["cusum"], errors="coerce")
                df["cusum_zscore"] = pd.to_numeric(df["cusum_zscore"], errors="coerce")
                df["cusum_conf"] = pd.to_numeric(df["cusum_conf"], errors="coerce")

            logger.info(f"✅ Загружено {len(df)} свечей с индикаторами")
            return df

        except Exception as err:
            raise RuntimeError(f"Ошибка загрузки индикаторов: {err}")

    def validate_data_quality(self, df: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        """Расширенная валидация качества данных с детальной диагностикой"""
        if df.empty:
            return False, {"error": "DataFrame пуст"}

        required_columns = ['open', 'high', 'low', 'close', 'volume', 'ts']
        checks = {
            'min_rows': len(df) > 100,
            'required_columns': all(col in df.columns for col in required_columns),
            'no_nan_close': df['close'].isna().sum() == 0,
            'high_low_valid': (df['high'] >= df['low']).all(),
            'high_open_valid': (df['high'] >= df['open']).all(),
            'high_close_valid': (df['high'] >= df['close']).all(),
            'low_open_valid': (df['low'] <= df['open']).all(),
            'low_close_valid': (df['low'] <= df['close']).all(),
            'positive_volume': (df['volume'] >= 0).all(),
            'timestamp_monotonic': df['ts'].is_monotonic_increasing
        }

        quality_ok = all(checks.values())
        diagnostics = {
            'passed_checks': sum(checks.values()),
            'total_checks': len(checks),
            'failed_checks': [k for k, v in checks.items() if not v],
            'basic_stats': {
                'period': f"{df['datetime'].min()} to {df['datetime'].max()}" if 'datetime' in df.columns else 'N/A',
                'total_rows': len(df),
                'nan_count': df.isna().sum().sum()
            }
        }
        # DataLoader.validate_data_quality(df) → проверки CUSUM 5m
        for col in ["cusum", "cusum_zscore", "cusum_conf", "cusum_state"]:
            if col not in df.columns:
                logger.warning("Отсутствует колонка %s (CUSUM 5m).", col)

        if "cusum_state" in df.columns:
            bad_mask = ~df["cusum_state"].isin([0, 1, -1]) & df["cusum_state"].notna()
            if bad_mask.any():
                logger.warning("Неверные значения cusum_state встречаются на %d строках.", int(bad_mask.sum()))

        if not quality_ok:
            logger.warning(f"⚠️ Проблемы качества данных: {diagnostics['failed_checks']}")

        return quality_ok, diagnostics

    def load_labeled_data(self) -> pd.DataFrame:
        """Загрузка размеченных данных - ИСПРАВЛЕННАЯ ВЕРСИЯ С ПРОВЕРКАМИ"""
        query = """
            SELECT 
                lr.symbol,
                lr.timestamp as extreme_timestamp,
                lr.reversal_label,
                lr.reversal_confidence as confidence,
                lr.labeling_method as method,
                lr.price_change_after as pnl,
                lr.features_json,
                lr.created_at,
                c.* 
            FROM labeling_results lr
            LEFT JOIN candles_5m c ON lr.timestamp = c.ts AND lr.symbol = c.symbol
            WHERE lr.symbol = ?
            ORDER BY lr.timestamp
        """
        try:
            positives = pd.read_sql_query(
                query,
                self.db_engine,
                params=(self.symbol,)
            )

            if positives.empty:
                logger.warning(f"❌ Нет размеченных данных для символа {self.symbol}")
                return pd.DataFrame()

            # ⬇️ ИСПРАВЛЕНИЕ: проверяем существование колонки перед обработкой
            if 'extreme_timestamp' in positives.columns:
                # Преобразуем в числовой формат, некорректные значения станут NaN
                positives['extreme_timestamp'] = pd.to_numeric(positives['extreme_timestamp'], errors='coerce')
                # Удаляем строки с некорректными timestamp
                initial_count = len(positives)
                positives = positives.dropna(subset=['extreme_timestamp'])
                removed_count = initial_count - len(positives)
                if removed_count > 0:
                    logger.warning(f"⚠️ Удалено {removed_count} строк с некорректными timestamp")
            else:
                logger.warning("⚠️ Колонка 'extreme_timestamp' не найдена в результатах")

            logger.info(f"✅ Загружено {len(positives)} размеченных примеров для {self.symbol}")
            if 'reversal_label' in positives.columns:
                logger.info(f"📊 Распределение меток: {positives['reversal_label'].value_counts().to_dict()}")
            else:
                logger.warning("⚠️ Колонка 'reversal_label' не найдена")

            return positives

        except Exception as err:
            logger.error(f"❌ Ошибка загрузки размеченных данных: {err}")
            return pd.DataFrame()

    def safe_correlation_calculation(self, df, columns):
        """Безопасный расчет корреляций с обработкой нулевой дисперсии"""
        try:
            # Убедимся, что columns существует в df
            available_columns = [col for col in columns if col in df.columns]

            if len(available_columns) < 2:
                return pd.DataFrame()

            # Выбираем только числовые колонки
            numeric_cols = df[available_columns].select_dtypes(include=[np.number])

            if len(numeric_cols.columns) < 2:
                return pd.DataFrame()

            # Убираем колонки с нулевой дисперсией
            numeric_cols = numeric_cols.loc[:, numeric_cols.std() > 0]

            if len(numeric_cols.columns) < 2:
                return pd.DataFrame()

            # Убираем строки с NaN
            numeric_cols = numeric_cols.dropna()

            if len(numeric_cols) < 2:
                return pd.DataFrame()

            # Безопасный расчет корреляций
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                corr_matrix = numeric_cols.corr().abs()

            return corr_matrix

        except Exception as err:
            logger.debug(f"Ошибка расчета корреляций: {err}")
            return pd.DataFrame()

    def get_data_stats(self) -> Dict[str, Any]:
        """Получение статистики данных через SQLAlchemy"""
        stats = {
            'symbol': self.symbol,
            'total_candles': 0,
            'period': 'N/A',
            'total_labels': 0,
            'buy_labels': 0,
            'sell_labels': 0,
            'avg_confidence': 0.0
        }

        try:
            # Используем self.db_engine.connect() вместо self.conn
            with self.db_engine.connect() as conn:
                # Статистика свечей
                candles_result = conn.execute(
                    text("SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles_5m WHERE symbol = :symbol"),
                    {'symbol': self.symbol}
                ).fetchone()

                if candles_result:
                    total_candles, min_ts, max_ts = candles_result
                    stats['total_candles'] = total_candles or 0
                    if min_ts and max_ts:
                        stats['period'] = f"{pd.to_datetime(min_ts, unit='ms')} to {pd.to_datetime(max_ts, unit='ms')}"

                # Статистика меток
                labels_result = conn.execute(
                    text("""
                        SELECT COUNT(*), AVG(reversal_confidence),
                               SUM(CASE WHEN reversal_label = 1 THEN 1 ELSE 0 END),
                               SUM(CASE WHEN reversal_label = 2 THEN 1 ELSE 0 END)
                        FROM labeling_results WHERE symbol = :symbol
                    """),
                    {'symbol': self.symbol}
                ).fetchone()

                if labels_result:
                    total_labels, avg_conf, buy_labels, sell_labels = labels_result
                    stats['total_labels'] = total_labels or 0
                    stats['buy_labels'] = buy_labels or 0
                    stats['sell_labels'] = sell_labels or 0
                    stats['avg_confidence'] = float(avg_conf) if avg_conf else 0.0

        except Exception as err:
            logger.error(f"Ошибка получения статистики: {err}")

        return stats

class AdvancedLabelingTool:
    """
    Улучшенный инструмент разметки с SQLAlchemy
    """

    def _ensure_training_snapshot_tables(self) -> None:
        """
        Создаёт (если отсутствуют) таблицы training_dataset и training_dataset_meta + индексы.
        Умная миграция: пересоздает таблицу только если структура устарела.
        """

        with self.engine.begin() as conn:
            # Проверяем структуру существующей таблицы
            try:
                result = conn.execute(text("PRAGMA table_info(training_dataset)")).fetchall()
                existing_columns = [row[1] for row in result]

                # Проверяем наличие старых колонок
                has_old_structure = (
                        'features_json' in existing_columns or
                        'is_negative' in existing_columns or
                        'anti_trade_mask' in existing_columns
                )

                if has_old_structure:
                    logger.info("🔄 Обнаружена старая структура training_dataset - пересоздаем таблицу")
                    conn.execute(text("DROP TABLE IF EXISTS training_dataset"))
                elif existing_columns:
                    logger.info("✅ Структура training_dataset актуальна (29 колонок)")
            except Exception:
                # Таблицы нет - это нормально, создастся ниже
                logger.info("📋 Таблица training_dataset не существует - будет создана")

            # Создаем таблицы (IF NOT EXISTS защищает от повторного создания)
            conn.execute(text(CREATE_TRAINING_DATASET_SQL))
            for sql in CREATE_TRAINING_DATASET_INDEXES_SQL:
                conn.execute(text(sql))
            conn.execute(text(CREATE_TRAINING_DATASET_META_SQL))
            conn.execute(text(CREATE_TRAINING_FEATURE_IMPORTANCE_SQL))
            for sql in CREATE_TRAINING_FEATURE_IMPORTANCE_INDEXES_SQL:
                conn.execute(text(sql))

        logger.info("✅ Таблицы training_dataset, training_dataset_meta, training_feature_importance проверены/созданы")

    def __init__(self, config: LabelingConfig):
        self.config = config

        logger = logging.getLogger(__name__)
        _VALID_METHODS = {"CUSUM", "EXTREMUM", "PELT_ONLINE", "CUSUM_EXTREMUM"}

        m = (getattr(self.config, "method", None) or "CUSUM_EXTREMUM").upper()
        if m not in _VALID_METHODS:
            logger.warning(
                "Unknown labeling method '%s'. Falling back to 'CUSUM_EXTREMUM'. "
                "Allowed: %s",
                m, sorted(_VALID_METHODS),
            )
            m = "CUSUM_EXTREMUM"
        self.logger = logger
        self.config.method = m
        self.config.tool = self
        self.data_loader = DataLoader(
            db_engine=create_engine(MARKET_DB_DSN),
            symbol=config.symbol,
            timeframe=config.timeframe,
            config=config
        )

        self.labels = []
        self.engine = self.data_loader.connect()
        self.feature_names = self.data_loader.feature_names
        self._ensure_table_exists()
        self.config.pnl_threshold = 0.001
        logger.info(f"✅ Инициализирован AdvancedLabelingTool для {config.symbol}")

    def _check_table_exists(self, table_name: str) -> bool:
        """Проверка существования таблицы через SQLAlchemy"""
        from sqlalchemy import inspect
        try:
            inspector = inspect(self.engine)
            return table_name in inspector.get_table_names()
        except Exception as err:
            logger.error(f"Ошибка проверки таблицы {table_name}: {err}")
            return False

    def _validate_snapshot_frame(self, df: pd.DataFrame):
        """
        Проверяет и очищает датафрейм перед записью в БД.
        Валидирует структуру для 4-классовой модели.
        """
        # Обязательные колонки для новой структуры
        required = ["ts", "datetime", "reversal_label", "sample_weight"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"Snapshot validation failed: missing required columns: {missing}")

        # Проверка значений reversal_label (0,1,2,3)
        if not df["reversal_label"].isin([0, 1, 2, 3]).all():
            invalid_labels = df[~df["reversal_label"].isin([0, 1, 2, 3])]["reversal_label"].unique()
            raise ValueError(f"Invalid reversal_label values: {invalid_labels}. Expected: 0,1,2,3")

        # Удаление дубликатов по ts
        duplicates_count = df.duplicated(subset=["ts"]).sum()
        if duplicates_count > 0:
            logger.warning(f"⚠️  Найдено {duplicates_count} дубликатов по ts - удаляем")
            df = df.drop_duplicates(subset=["ts"], keep="first")

        # Удаление строк с NaN в критичных колонках
        critical_cols = ["ts", "reversal_label", "sample_weight"]
        nan_mask = df[critical_cols].isna().any(axis=1)
        nan_drop_rows = nan_mask.sum()
        if nan_drop_rows > 0:
            logger.warning(f"⚠️  Удаляем {nan_drop_rows} строк с NaN в критичных колонках")
            df = df[~nan_mask]

        issues = {
            "duplicates_removed": int(duplicates_count),
            "class_balance": df["reversal_label"].value_counts().to_dict()
        }

        return df, issues, int(nan_drop_rows), int(duplicates_count)

    def _ensure_table_exists(self):
        """Создание расширенных таблиц через SQLAlchemy (идемпотентно)"""
        from sqlalchemy import text

        # labeling_results: единая схема
        if not self._check_table_exists('labeling_results'):
            logger.info("📋 Создание таблицы labeling_results...")

            create_table_sql = text("""
                CREATE TABLE IF NOT EXISTS labeling_results (
                    symbol TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    timeframe TEXT NOT NULL,
                    reversal_label INTEGER NOT NULL,
                    reversal_confidence REAL DEFAULT 1.0,
                    labeling_method TEXT NOT NULL,
                    labeling_params TEXT,
                    extreme_index INTEGER,
                    extreme_price REAL,
                    extreme_timestamp INTEGER NOT NULL,
                    confirmation_index INTEGER,
                    confirmation_timestamp INTEGER,
                    price_change_after REAL,
                    features_json TEXT,
                    is_high_quality INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, extreme_timestamp, reversal_label)
                )
            """)

            # Индексы: запросы часто идут без reversal_label, добавим покрывающий индекс.
            create_index_1 = text("""
                CREATE INDEX IF NOT EXISTS idx_labeling_results_symbol_ts
                ON labeling_results(symbol, extreme_timestamp)
            """)
            # Если у тебя часто есть фильтр по timeframe — добавь композитный индекс:
            create_index_2 = text("""
                CREATE INDEX IF NOT EXISTS idx_labeling_results_symbol_tf_ts
                ON labeling_results(symbol, timeframe, extreme_timestamp)
            """)

            # Используем begin() для атомарности
            with self.engine.begin() as conn:
                conn.execute(create_table_sql)
                conn.execute(create_index_1)
                conn.execute(create_index_2)

            logger.info("✅ Таблица labeling_results создана")
        else:
            logger.info("✅ Таблица labeling_results уже существует")

    def load_data(self) -> pd.DataFrame:
        """Загрузка данных"""
        try:
            df = self.data_loader.load_indicators()
            quality_ok, diagnostics = self.data_loader.validate_data_quality(df)

            if not quality_ok:
                logger.warning(f"⚠️ Проблемы качества данных: {diagnostics}")

            return df
        except Exception as err:
            raise RuntimeError(f"Ошибка загрузки данных: {err}")

    def _get_all_existing_signals(self) -> List[Dict]:
        """Получение всех существующих меток символа из БД"""
        if not self._check_table_exists('labeling_results'):
            return []

        try:
            query = """
                SELECT extreme_timestamp, extreme_index, reversal_label, labeling_method 
                FROM labeling_results 
                WHERE symbol = :symbol 
                ORDER BY extreme_index
            """

            with self.engine.connect() as conn:
                result = conn.execute(
                    text(query),
                    {'symbol': self.config.symbol}
                ).fetchall()

            signals = []
            for extreme_ts, extreme_idx, reversal_label, method in result:
                signals.append({
                    'extreme_timestamp': extreme_ts,
                    'extreme_index': extreme_idx,
                    'reversal_label': reversal_label,
                    'labeling_method': method
                })

            logger.info(f"📊 Загружено {len(signals)} существующих меток из БД")
            return signals

        except Exception as err:
            logger.error(f"❌ Ошибка загрузки существующих меток: {err}")
            return []

    def _calculate_pnl_to_index(self, df: pd.DataFrame, entry_idx: int, signal_type: str, end_idx: int) -> Tuple[
        float, bool]:
        """Универсальный расчет PnL до конкретного индекса"""
        if end_idx >= len(df):
            return 0.0, False

        try:
            entry_price = df['close'].iloc[entry_idx]
            exit_price = df['close'].iloc[end_idx]

            if entry_price <= 0:
                return 0.0, False

            if signal_type == 'BUY':
                effective_entry = entry_price * (1 + self.config.fee_percent)
                effective_exit = exit_price * (1 - self.config.fee_percent)
                net_pnl = (effective_exit - effective_entry) / effective_entry
            else:  # SELL
                effective_entry = entry_price * (1 - self.config.fee_percent)
                effective_exit = exit_price * (1 + self.config.fee_percent)
                net_pnl = (effective_entry - effective_exit) / effective_entry

            is_profitable_enough = net_pnl >= self.config.min_profit_target
            return net_pnl, is_profitable_enough

        except (IndexError, ZeroDivisionError, KeyError) as err:
            logger.warning(f"Ошибка расчета PnL для индекса {entry_idx}: {err}")
            return 0.0, False

    # =========================================================================
    # ОСНОВНЫЕ МЕТОДЫ РАЗМЕТКИ (из исходного кода)
    # =========================================================================

    def _calculate_pnl(self, df: pd.DataFrame, entry_idx: int, signal_type: str) -> Tuple[float, bool]:
        if entry_idx + self.config.hold_bars >= len(df):
            return 0.0, False

        try:
            entry_price = df['close'].iloc[entry_idx]
            exit_price = df['close'].iloc[entry_idx + self.config.hold_bars]

            if entry_price <= 0:
                return 0.0, False

            if signal_type == 'BUY':
                effective_entry = entry_price * (1 + self.config.fee_percent)
                effective_exit = exit_price * (1 - self.config.fee_percent)
                net_pnl = (effective_exit - effective_entry) / effective_entry  # ← это уже в долях (0.01 = 1%)

            else:  # SELL
                effective_entry = entry_price * (1 - self.config.fee_percent)
                effective_exit = exit_price * (1 + self.config.fee_percent)
                net_pnl = (effective_entry - effective_exit) / effective_entry  # ← это уже в долях (0.01 = 1%)

            is_profitable_enough = net_pnl >= self.config.min_profit_target
            return net_pnl, is_profitable_enough

        except (IndexError, ZeroDivisionError, KeyError) as err:
            logger.warning(f"Ошибка расчета PnL для индекса {entry_idx}: {err}")
            return 0.0, False

    def _interpret_pnl_results(self, total_metrics: Dict[str, float]):
        """Интерпретация результатов PnL с корректными процентами"""
        total_pnl = total_metrics.get('total_pnl', 0)
        success_rate = total_metrics.get('success_rate', 0)
        pnl_ratio = total_metrics.get('pnl_ratio', 0)

        print(f"\n🔍 ИНТЕРПРЕТАЦИЯ РЕЗУЛЬТАТОВ:")

        # Оценка успешности
        if success_rate > 0.6:
            print(f"   🎯 Высокая успешность: {success_rate:.1%} (>60%)")
        elif success_rate > 0.4:
            print(f"   ✅ Средняя успешность: {success_rate:.1%} (40-60%)")
        else:
            print(f"   ⚠️  Низкая успешность: {success_rate:.1%} (<40%)")

        # Оценка соотношения PNL
        if pnl_ratio > 3:
            print(f"   💎 Отличное соотношение прибыли/убытков: {pnl_ratio:.1f}:1")
        elif pnl_ratio > 2:
            print(f"   ✅ Хорошее соотношение прибыли/убытков: {pnl_ratio:.1f}:1")
        elif pnl_ratio > 1:
            print(f"   ⚠️  Удовлетворительное соотношение: {pnl_ratio:.1f}:1")
        else:
            print(f"   ❌ Проблемное соотношение: {pnl_ratio:.1f}:1")

        # Оценка общей прибыльности (используем проценты как есть)
        if total_pnl > 0.1:  # >10%
            print(f"   🚀 Высокая общая прибыльность: {total_pnl:+.1%}")
        elif total_pnl > 0.02:  # >2%
            print(f"   ✅ Положительная прибыльность: {total_pnl:+.1%}")
        elif total_pnl > 0:  # >0%
            print(f"   ⚠️  Слабая прибыльность: {total_pnl:+.1%}")
        else:
            print(f"   ❌ Убыточная стратегия: {total_pnl:+.1%}")

    def _smart_confirmation_system(self, df: pd.DataFrame, signal_idx: int, signal_type: str) -> dict:
        """Умная система подтверждения"""
        confirm_bar = self._get_confirmation_bars(signal_type)

        confirmation_data = {
            'confirmed': False,
            'confidence_boost': 0.0,
            'early_rejection': False,
            'confirmation_index': signal_idx + confirm_bar,
            'price_change': 0.0
        }

        if signal_idx + 3 >= len(df):
            confirmation_data['early_rejection'] = True
            return confirmation_data

        price_at_signal = df['close'].iloc[signal_idx]
        price_after_3bars = df['close'].iloc[signal_idx + 3]
        expected_move = 0.005

        if signal_type == 'BUY':
            move_percent = (price_after_3bars - price_at_signal) / price_at_signal
            if move_percent < -expected_move:
                confirmation_data['early_rejection'] = True
        else:
            move_percent = (price_at_signal - price_after_3bars) / price_at_signal
            if move_percent < -expected_move:
                confirmation_data['early_rejection'] = True

        if not confirmation_data['early_rejection'] and signal_idx + confirm_bar < len(df):
            confirmation_data['confirmed'] = True
            confirmation_data['price_change'] = move_percent
            if abs(move_percent) > expected_move:
                confirmation_data['confidence_boost'] = 0.15

        return confirmation_data

    def merge_conflicting_labels(self):
        """Последовательная валидация всей разметки"""
        try:
            df = self.load_data()
            with self.engine.begin() as conn:
                # Получаем все BUY/SELL метки отсортированные по времени
                signals_query = """
                    SELECT rowid, extreme_timestamp, reversal_label, labeling_method, price_change_after
                    FROM labeling_results 
                    WHERE symbol = :symbol AND reversal_label IN (1,2)
                    ORDER BY extreme_timestamp
                """
                signals = conn.execute(text(signals_query), {'symbol': self.config.symbol}).fetchall()

                if not signals:
                    print("✅ Нет BUY/SELL меток для валидации")
                    return 0

                fixed_count = 0

                # Обрабатываем каждую пару последовательных сигналов
                for i in range(len(signals) - 1):
                    current_signal = signals[i]
                    next_signal = signals[i + 1]

                    current_ts = current_signal.extreme_timestamp
                    next_ts = next_signal.extreme_timestamp

                    # Получаем индексы для расчета PNL
                    current_idx_match = df.index[df['ts'] == current_ts].tolist()
                    next_idx_match = df.index[df['ts'] == next_ts].tolist()

                    if not current_idx_match or not next_idx_match:
                        continue

                    current_idx = int(current_idx_match[0])
                    next_idx = int(next_idx_match[0])

                    # Пересчитываем PNL до следующего сигнала
                    signal_type = 'BUY' if current_signal.reversal_label == 1 else 'SELL'
                    pnl, _ = self._calculate_pnl_to_index(df, current_idx, signal_type, next_idx)

                    # Если PNL < 0.1% - исправляем разметку
                    if abs(pnl) < 0.001:
                        print(f"🔄 Исправляем малоприбыльный сигнал на ts={current_ts}, PNL={pnl:.4f}")

                        # Удаляем текущий сигнал
                        conn.execute(text("DELETE FROM labeling_results WHERE rowid = :rowid"),
                                     {'rowid': current_signal.rowid})

                        # Ставим HOLD на следующем баре
                        if current_idx + 1 < len(df):
                            next_ts_hold = int(df.iloc[current_idx + 1]['ts'])
                            conn.execute(text("""
                                INSERT OR IGNORE INTO labeling_results 
                                (symbol, timestamp, timeframe, reversal_label, reversal_confidence, 
                                 labeling_method, extreme_timestamp, price_change_after)
                                VALUES (:symbol, :ts, :tf, 0, 1.0, 'VALIDATED_HOLD', :ts, 0.0)
                            """), {'symbol': self.config.symbol, 'ts': next_ts_hold, 'tf': self.config.timeframe})

                        fixed_count += 1

                print(f"✅ Исправлено малоприбыльных сигналов: {fixed_count}")
                return fixed_count

        except Exception as err:
            logger.error(f"❌ Ошибка валидации разметки: {err}")
            raise

    def _get_confirmation_bars(self, signal_type: str) -> int:
        """
        Возвращает базовое количество баров подтверждения в зависимости от метода разметки.
        Делает метод устойчивым к регистру/вводу пользователя.
        """
        import logging
        logger = logging.getLogger(__name__)

        # Нормализация метода к верхнему регистру (устойчивость к вводу)
        method = (getattr(self.config, "method", "") or "").upper()

        # Базовая логика (без изменений поведения для известных методов)
        if method == "PELT_ONLINE":
            base_confirmation = 3
        elif method in ("CUSUM", "EXTREMUM", "CUSUM_EXTREMUM"):
            base_confirmation = 2
        else:
            logger.warning(
                "Unknown labeling method '%s' in _get_confirmation_bars; using base_confirmation=2",
                method
            )
            base_confirmation = 2

        confirmation_bars = base_confirmation
        if self.config.method == 'EXTREMUM':
            confirmation_bars += 1  # специальная логика для EXTREMUM

        logger.debug(f"Определение подтверждения для {signal_type} сигнала")
        return max(1, min(5, confirmation_bars))  # ограничиваем разумными пределами

    def _pelt_offline_reversals(self, df: pd.DataFrame) -> List[Dict]:
        """
        PELT offline с автоподбором penalty и визуализацией процесса.
        Анализирует все данные сразу, использует будущее.
        Интерактивный выбор целевого количества сигналов + остановка по 'q'.
        ВХОД НА СЛЕДУЮЩЕЙ СВЕЧЕ ПОСЛЕ CHANGEPPOINT'а (как в extremum).
        """
        if not RUPTURES_AVAILABLE:
            logger.warning("⚠️ Библиотека ruptures недоступна")
            return []

        if len(df) < 500:
            logger.warning(f"⚠️ Недостаточно данных для PELT Offline: {len(df)}")
            return []

        # Импорт для проверки нажатия клавиш (Windows)
        try:
            import msvcrt
            has_keyboard_check = True
        except ImportError:
            has_keyboard_check = False
            logger.warning("⚠️ Интерактивная остановка недоступна (только Windows)")

        logger.info(f"📊 PELT Offline: анализ {len(df)} свечей...")

        # 🎯 ИНТЕРАКТИВНЫЙ ВЫБОР ЦЕЛЕВОГО КОЛИЧЕСТВА
        print("\n" + "=" * 60)
        print("🎯 НАСТРОЙКА PELT OFFLINE: Целевое количество сигналов")
        print("=" * 60)
        print("Выберите стратегию:")
        print("   📊 [1] Консервативная:  3-5/день   (~600-1000 сигналов)")
        print("   📊 [2] Сбалансированная: 10-15/день (~2000-3000 сигналов)")
        print("   📊 [3] Агрессивная:     20-30/день (~4000-6000 сигналов)")
        print("   ⚙️  [4] Своё значение")

        choice = input("\nВыбор [2]: ").strip()

        if choice == '1':
            target_signals_daily = 4.0
            print("✅ Выбрана консервативная стратегия: ~4 сигнала/день")
        elif choice == '3':
            target_signals_daily = 25.0
            print("✅ Выбрана агрессивная стратегия: ~25 сигналов/день")
        elif choice == '4':
            custom_input = input("Введите количество сигналов в день [12]: ").strip()
            if custom_input:
                try:
                    target_signals_daily = float(custom_input)
                    if target_signals_daily < 1 or target_signals_daily > 50:
                        print("⚠️ Значение вне диапазона (1-50), использую 12")
                        target_signals_daily = 12.0
                    else:
                        print(f"✅ Установлено: {target_signals_daily} сигналов/день")
                except ValueError:
                    print("⚠️ Некорректное значение, использую 12")
                    target_signals_daily = 12.0
            else:
                target_signals_daily = 12.0
        else:  # default или '2'
            target_signals_daily = 12.0
            print("✅ Выбрана сбалансированная стратегия: ~12 сигналов/день")

        print("=" * 60 + "\n")

        min_size = max(4, self.config.pelt_min_size)

        # Подготовка сигнала
        close_vals = df['close'].astype(float).values
        signal = np.log(np.clip(close_vals, 1e-12, None))
        n_samples = len(signal)

        # Определяем bars_per_day (для 5m = 288)
        bars_per_day = 288

        # ⚠️ КОРРЕКЦИЯ: changepoints ≠ сигналы
        SIGNAL_TO_CHANGEPOINT_RATIO = 2.55  # реальный коэффициент из практики

        target_changepoints = target_signals_daily * (n_samples / bars_per_day) * SIGNAL_TO_CHANGEPOINT_RATIO
        target_total = target_changepoints

        # Диапазон для подбора penalty
        target_low = int(target_total * 0.8)
        target_high = int(target_total * 1.2)

        # Ожидаемое количество сигналов (для информации)
        expected_signals = target_changepoints / SIGNAL_TO_CHANGEPOINT_RATIO
        expected_signals_daily = expected_signals * bars_per_day / n_samples

        # Автоподбор penalty
        start_pen, end_pen = 1e-7, 1e-2
        n_steps = 30

        best_penalty = None
        best_changepoints = None
        closest_distance = float('inf')

        # 🌡️ ВИЗУАЛИЗАЦИЯ ПОДБОРА
        print(f"🎯 Цель: {target_total:.1f} changepoints (для получения ~{target_signals_daily:.1f} сигналов/день)")
        print(f"🔍 Подбор оптимального penalty (диапазон: {target_low}-{target_high} точек)...")
        if has_keyboard_check:
            print(f"💡 Нажмите 'q' или Enter для немедленной остановки")
        print("Прогресс подбора:")

        pens = np.logspace(np.log10(start_pen), np.log10(end_pen), num=n_steps)

        # Счетчик для ранней остановки
        max_found = 0
        iterations_without_improvement = 0
        early_stop = False
        manual_stop = False

        for i, pen in enumerate(pens):
            # 🛑 ПРОВЕРКА НАЖАТИЯ КЛАВИШИ
            if has_keyboard_check and msvcrt.kbhit():
                key = msvcrt.getch()
                if key in [b'q', b'Q', b'\r']:  # q, Q или Enter
                    sys.stdout.write(f"\r🛑 Остановлено пользователем")
                    manual_stop = True
                    break

            try:
                pen_value = float(pen) if hasattr(pen, 'item') else pen
                algo = rpt.Pelt(model="l2", min_size=min_size, jump=5).fit(signal)
                changepoints = algo.predict(pen=pen_value)
                changepoints = [cp for cp in changepoints if cp < len(df)]
                n_cp = max(len(changepoints) - 1, 0)

                # 🛑 ВЫХОД ЕСЛИ ТОЧКИ УЖЕ НИЖЕ ЦЕЛЕВОГО ДИАПАЗОНА
                if n_cp < target_low and best_changepoints is not None:
                    sys.stdout.write(f"\r🛑 Выход: точки {n_cp} < минимума {target_low}")
                    break

                # ОТСЛЕЖИВАНИЕ МАКСИМУМА
                if n_cp > max_found:
                    max_found = n_cp
                    iterations_without_improvement = 0
                else:
                    iterations_without_improvement += 1

                dist = abs(n_cp - target_total)
                if dist < closest_distance:
                    closest_distance = dist
                    best_penalty = pen_value
                    best_changepoints = changepoints

                # 🌡️ ТЕРМОМЕТР
                bar_len = 20
                filled = int((i + 1) / n_steps * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                color = "\033[92m" if target_low <= n_cp <= target_high else "\033[0m"
                kb_hint = " [q-стоп]" if has_keyboard_check else ""
                sys.stdout.write(
                    f"\r  {bar} {i + 1}/{n_steps} | pen={pen_value:.8f} | {color}{n_cp:5d} точек{kb_hint}\033[0m"
                )
                sys.stdout.flush()

                # 🛑 РАННЯЯ ОСТАНОВКА
                if i >= 4:
                    if max_found < target_low * 0.5 and iterations_without_improvement >= 3:
                        sys.stdout.write(f"\r⚠️ Ранняя остановка на итерации {i + 1}/{n_steps}")
                        early_stop = True
                        break

            except Exception as err:
                continue

            if manual_stop:
                break

        sys.stdout.write("\n")

        # 🛑 ПОЛНАЯ ОСТАНОВКА, ЕСЛИ БЫЛО НАЖАТО 'Q'
        if manual_stop:
            print(
                f"✅ Подбор остановлен пользователем (найдено {len(best_changepoints) if best_changepoints else 0} точек)")
            if best_changepoints is None or len(best_changepoints) == 0:
                logger.warning("❌ Не удалось найти change points до остановки")
                return []
            # Продолжаем с последним best_penalty как рабочим результатом

        # 🔄 ДОПОЛНИТЕЛЬНЫЙ ПРОХОД ТОЛЬКО ЕСЛИ НЕ БЫЛО РУЧНОЙ ОСТАНОВКИ
        if not manual_stop and not early_stop and best_changepoints is not None:
            n_best = len(best_changepoints) - 1
            if n_best < target_low * 0.9:
                print(f"⚠️ Результат ниже диапазона ({n_best} < {target_low}). Пробуем меньшие penalty...")
                start_pen_new = start_pen / 100
                end_pen_new = best_penalty
                pens_extra = np.logspace(np.log10(start_pen_new), np.log10(end_pen_new), num=20)
                print("Дополнительный подбор:")

                for i, pen in enumerate(pens_extra):
                    if has_keyboard_check and msvcrt.kbhit():
                        key = msvcrt.getch()
                        if key in [b'q', b'Q', b'\r']:
                            sys.stdout.write(f"\r🛑 Остановлено пользователем")
                            manual_stop = True
                            break

                    try:
                        pen_value = float(pen) if hasattr(pen, 'item') else pen
                        algo = rpt.Pelt(model="l2", min_size=min_size, jump=5).fit(signal)
                        changepoints = algo.predict(pen=pen_value)
                        changepoints = [cp for cp in changepoints if cp < len(df)]
                        n_cp = max(len(changepoints) - 1, 0)
                        dist = abs(n_cp - target_total)
                        if dist < closest_distance:
                            closest_distance = dist
                            best_penalty = pen_value
                            best_changepoints = changepoints

                        bar_len = 15
                        filled = int((i + 1) / 20 * bar_len)
                        bar = "█" * filled + "░" * (bar_len - filled)
                        color = "\033[92m" if target_low <= n_cp <= target_high else "\033[0m"
                        kb_hint = " [q-стоп]" if has_keyboard_check else ""
                        sys.stdout.write(
                            f"\r  {bar} {i + 1}/20 | pen={pen_value:.8f} | {color}{n_cp:5d} точек{kb_hint}\033[0m"
                        )
                        sys.stdout.flush()
                    except Exception as err:
                        continue

                    if manual_stop:
                        break

                sys.stdout.write("\n")

        if not manual_stop and not early_stop:
            print("✅ Подбор завершён")

        if best_changepoints is None or len(best_changepoints) == 0:
            print("❌ Не удалось найти change points.")
            logger.warning("❌ Не удалось найти change points")
            return []

        # 📊 ДЕТАЛЬНАЯ СВОДКА
        changepoints_daily = (len(best_changepoints) * bars_per_day / n_samples) if n_samples > 0 else 0
        estimated_signals = (len(best_changepoints) - 1) / SIGNAL_TO_CHANGEPOINT_RATIO
        estimated_signals_daily = estimated_signals * bars_per_day / n_samples
        deviation = abs(estimated_signals_daily - target_signals_daily)

        print(f"\n📊 РЕЗУЛЬТАТЫ ПОДБОРА:")
        print(f"   🎯 Лучший penalty: {best_penalty:.7f}")
        print(f"   📈 Найдено changepoints: {len(best_changepoints)} (~{changepoints_daily:.1f}/день)")
        print(f"   🎯 Ожидаемые сигналы: ~{estimated_signals:.0f} (~{estimated_signals_daily:.1f}/день)")
        print(f"   🎯 Целевой диапазон changepoints: {target_low}-{target_high}")
        print(
            f"   {'✅' if deviation <= target_signals_daily * 0.3 else '⚠️'} Отклонение от цели: {deviation:.1f} сигналов/день")

        logger.info(
            f"✅ PELT Offline: penalty={best_penalty:.7f}, {len(best_changepoints)} changepoints, ~{estimated_signals:.0f} сигналов (~{estimated_signals_daily:.1f}/день)")

        changepoints = best_changepoints
        results = []

        # Определение BUY/SELL через анализ трендов между changepoints
        for i in range(len(changepoints) - 1):
            start = changepoints[i]  # индекс экстремума
            end = changepoints[i + 1]

            if start >= len(df) or end >= len(df):
                continue

            # Предыдущий сегмент (если есть)
            if i > 0:
                prev_start = changepoints[i - 1]
                if prev_start >= len(df):
                    continue

                # Тренд до текущего changepoint
                current_trend_up = df['close'].iat[end - 1] > df['close'].iat[start]
                # Предыдущий тренд
                prev_trend_up = df['close'].iat[start - 1] > df['close'].iat[prev_start]

                # Определяем реверс
                rev_type = None
                if not prev_trend_up and current_trend_up:
                    rev_type = "BUY"
                elif prev_trend_up and not current_trend_up:
                    rev_type = "SELL"

                if rev_type is None:
                    continue

                # 🔁 КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: ВХОД НА СЛЕДУЮЩЕЙ СВЕЧЕ ПОСЛЕ ЭКСТРЕМУМА
                entry_index = start + 1
                if entry_index >= len(df):
                    continue  # За пределами данных

                extreme_ts = int(df['ts'].iat[start])
                entry_ts = int(df['ts'].iat[entry_index])

                confidence = min(abs((df['close'].iat[end - 1] - df['close'].iat[start]) / df['close'].iat[start]),
                                 0.95)
                confidence = max(confidence, 0.5)

                # Подтверждение (можно использовать smart system)
                confirmation = self._smart_confirmation_system(df, entry_index, rev_type)
                conf_idx = confirmation['confirmation_index']
                if conf_idx >= len(df):
                    conf_idx = len(df) - 1

                results.append({
                    'index': entry_index,
                    'type': rev_type,
                    'confidence': confidence,
                    'extreme_index': start,
                    'extreme_timestamp': extreme_ts,
                    'confirmation_index': conf_idx,
                    'confirmation_timestamp': int(df['ts'].iat[conf_idx]),
                    'method': 'PELT_OFFLINE',
                    'reversal_label': 1 if rev_type == 'BUY' else 2,
                })

        logger.info(f"📊 PELT Offline найдено {len(results)} разворотов (вход на следующей свече)")
        if results:
            buy_count = sum(1 for r in results if r['type'] == 'BUY')
            sell_count = sum(1 for r in results if r['type'] == 'SELL')
            avg_conf = np.mean([r['confidence'] for r in results])
            print(f"📈 Сигналы: {buy_count} BUY, {sell_count} SELL, средняя уверенность: {avg_conf:.2f}\n")
            logger.info(f"📈 Детали: {buy_count} BUY, {sell_count} SELL, средняя уверенность: {avg_conf:.2f}")

        return results

    def _cusum_reversals(self, df):
        """
        Генерация реверсивных сигналов по готовым полям CUSUM 5m из БД.
        Кодировка: BUY=1, SELL=2, HOLD=0 (HOLD игнорируем).
        Отбор кандидатов по |cusum_zscore| и/или cusum_conf с подтверждением.
        """
        cfg = self.config  # LabelingConfig
        out = []

        # Требуемые колонки
        need = ["ts", "cusum_state", "cusum_zscore", "cusum_conf"]
        if any(c not in df.columns for c in need):
            logger.warning("CUSUM reversals: нет нужных колонок %s", need)
            return out

        # индексы-кандидаты: сильный z или уверенность
        z = df["cusum_zscore"].astype(float)
        conf = df["cusum_conf"].astype(float)
        state = df["cusum_state"].astype("Int64")  # 1/2/0

        cand_mask = (z.abs() >= float(getattr(cfg, "cusum_z_threshold", 1.0))) | \
                    (conf >= float(getattr(cfg, "cusum_conf_threshold", 0.6)))

        idxs = df.index[cand_mask & state.isin([1, 2])]
        if len(idxs) == 0:
            return out

        for i in idxs:
            s = int(state.loc[i])
            signal_type = "BUY" if s == 1 else "SELL"  # 0 (HOLD) сюда не попадает
            label = 1 if s == 1 else 2  # формат инструмента
            base_conf = float(conf.loc[i]) if pd.notna(conf.loc[i]) else 0.0
            base_conf = max(0.2, min(0.95, base_conf))  # мягкие границы

            # подтверждение
            confirm = self._smart_confirmation_system(df, i, signal_type)

            # Создаем полную структуру сигнала с ВСЕМИ необходимыми полями
            signal_data = {
                "index": int(i),
                "type": signal_type,  # "BUY"/"SELL"
                "confidence": max(0.0, min(0.99, base_conf + float(confirm.get("confidence_boost", 0.0)))),
                "extreme_timestamp": int(df["ts"].loc[i]) if "ts" in df.columns and pd.notna(df["ts"].loc[i]) else None,
                "confirmation_index": confirm['confirmation_index'],
                "confirmation_timestamp": int(df["ts"].iloc[confirm['confirmation_index']]) if confirm[
                                                                                                   'confirmation_index'] < len(
                    df) else None,
                "method": 'CUSUM'
            }

            out.append(signal_data)

        return out

    def _extremum_reversals(self, df: pd.DataFrame) -> List[Dict]:
        """Экстремум реверсии с последовательным связыванием и перескоком окна"""
        window = self.config.extremum_window
        confirm_bar = max(1, min(5, self.config.extremum_confirm_bar))
        min_distance = getattr(self.config, 'min_signal_distance', 10)
        low = df['low'].values
        high = df['high'].values
        results = []

        # Начинаем поиск с начала окна
        i = window
        last_extremum_type = None  # 'BUY' или 'SELL'

        while i < len(df) - window:
            current_low = low[i]
            current_high = high[i]

            # Проверяем на минимум в окне
            is_low_extreme = current_low == np.min(low[i - window:i + window + 1])
            # Проверяем на максимум в окне
            is_high_extreme = current_high == np.max(high[i - window:i + window + 1])

            signal_type = None

            # Определяем тип сигнала с учетом последовательности
            if is_low_extreme and last_extremum_type != 'BUY':
                signal_type = 'BUY'
            elif is_high_extreme and last_extremum_type != 'SELL':
                signal_type = 'SELL'

            # Если нашли подходящий экстремум
            if signal_type and i + confirm_bar < len(df):
                confirmation = self._smart_confirmation_system(df, i, signal_type)
                if not confirmation['early_rejection']:
                    results.append({
                        'index': i,
                        'type': signal_type,
                        'confidence': 0.7 + confirmation['confidence_boost'],
                        'extreme_timestamp': df['ts'].iloc[i],
                        'confirmation_index': confirmation['confirmation_index'],
                        'confirmation_timestamp': df['ts'].iloc[confirmation['confirmation_index']],
                        'method': 'EXTREMUM'
                    })
                    # Обновляем состояние и перескакиваем окно
                    last_extremum_type = signal_type
                    i += min_distance  # Перескакиваем на min_distance вперед
                    continue  # Пропускаем обычное увеличение i

            # Если экстремум не найден или не прошел подтверждение - двигаемся на 1 бар
            i += 1

        return results

    def _cusum_extremum_hybrid(self, df):
        """
        Гибрид CUSUM (из БД) + экстремумы. Тип должен совпадать, расстояние по индексам ≤ 2.
        Уверенность — среднее двух сигналов (или по вашей текущей формуле).
        """
        cusum_signals = self._cusum_reversals(df)
        extremum_signals = self._extremum_reversals(df)

        if not cusum_signals:
            return extremum_signals
        if not extremum_signals:
            return cusum_signals

        # быстрый мап по типу
        by_type = {"BUY": [], "SELL": []}
        for s in cusum_signals:
            by_type[s["type"]].append(s)

        out = []
        for e in extremum_signals:
            group = by_type.get(e["type"], [])
            # находим ближайший по индексу из CUSUM (порог 2 бара)
            best = None
            best_d = None
            for c in group:
                d = abs(int(c["index"]) - int(e["index"]))
                if d <= 2 and (best is None or d < best_d):
                    best, best_d = c, d
            if best is not None:
                # объединяем
                conf = (float(best["confidence"]) + float(e.get("confidence", 0.0))) / 2.0
                merged = e.copy()  # Используем копию extremum сигнала как основу
                merged["confidence"] = max(0.0, min(0.99, conf))
                # Сохраняем все необходимые поля из extremum сигнала
                out.append(merged)
            else:
                out.append(e)

        return out

    # =========================================================================
    # ВОССТАНОВЛЕННЫЕ МЕТОДЫ
    # =========================================================================

    def advanced_quality_analysis(self) -> Dict[str, Any]:
        """
        Расширенный анализ качества разметки с общим PNL+ и PNL-
        """
        logger.info("🔍 Запуск расширенного анализа качества...")

        # ⬇️ ГАРАНТИРУЕМ что всегда возвращается dict
        default_result = {
            'methods_performance': [],
            'total_metrics': {},
            'best_method': {'method': 'N/A', 'success_rate': 0.0},
            'data_quality_issues': [],
            'timestamp': datetime.now().isoformat()
        }

        try:
            # ⬇️ ОБНОВЛЕННЫЙ ЗАПРОС: добавляем суммы PNL+ и PNL-
            query = """
                SELECT 
                    labeling_method,
                    reversal_label,
                    COUNT(*) as total_signals,
                    AVG(reversal_confidence) as avg_confidence,
                    AVG(price_change_after) as avg_profit,
                    SUM(price_change_after) as total_pnl,
                    -- ⬇️ ДОБАВЛЯЕМ СУММЫ ДЛЯ PNL+ И PNL-
                    SUM(CASE WHEN price_change_after > 0 THEN price_change_after ELSE 0 END) as total_positive_pnl,
                    SUM(CASE WHEN price_change_after < 0 THEN price_change_after ELSE 0 END) as total_negative_pnl,
                    SUM(CASE WHEN price_change_after >= :min_profit THEN 1 ELSE 0 END) as profitable_signals,
                    SUM(CASE WHEN price_change_after < -:min_profit THEN 1 ELSE 0 END) as loss_signals,
                    MIN(price_change_after) as min_profit,
                    MAX(price_change_after) as max_profit
                FROM labeling_results 
                WHERE symbol = :symbol
                GROUP BY labeling_method, reversal_label
            """

            df_quality = pd.read_sql_query(
                query,
                self.engine,
                params={
                    'min_profit': self.config.min_profit_target,
                    'symbol': self.config.symbol
                }
            )

            if df_quality.empty:
                logger.warning("❌ Нет данных для анализа качества")
                return default_result

            # === ВАЛИДАЦИЯ МЕТОК С PNL=0 ===
            validation_warnings = []

            # Загружаем все метки для валидации
            all_labels_query = """
                SELECT 
                    extreme_timestamp,
                    reversal_label,
                    price_change_after,
                    labeling_method
                FROM labeling_results 
                WHERE symbol = :symbol
                ORDER BY extreme_timestamp
            """

            df_all_labels = pd.read_sql_query(
                all_labels_query,
                self.engine,
                params={'symbol': self.config.symbol}
            )

            validated_zero_pnl = 0
            invalid_zero_pnl = 0

            # Проверяем только BUY/SELL метки
            for idx, row in df_all_labels[df_all_labels['reversal_label'].isin([1, 2])].iterrows():
                if row['price_change_after'] == 0.0:
                    # Ищем следующую метку
                    next_labels = df_all_labels[df_all_labels['extreme_timestamp'] > row['extreme_timestamp']]

                    if next_labels.empty:
                        validation_warnings.append({
                            'timestamp': row['extreme_timestamp'],
                            'label': row['reversal_label'],
                            'method': row['labeling_method'],
                            'issue': 'Нет следующей метки'
                        })
                        invalid_zero_pnl += 1
                    else:
                        next_label = next_labels.iloc[0]
                        if next_label['reversal_label'] != 0:
                            validation_warnings.append({
                                'timestamp': row['extreme_timestamp'],
                                'label': row['reversal_label'],
                                'method': row['labeling_method'],
                                'issue': f'Следующая метка не HOLD (reversal_label={next_label["reversal_label"]})'
                            })
                            invalid_zero_pnl += 1
                        else:
                            validated_zero_pnl += 1

            analysis = {
                'methods_performance': df_quality.to_dict('records'),
                'total_metrics': self._calculate_total_metrics(df_quality),
                'best_method': self._find_best_method(df_quality),
                'data_quality_issues': self._detect_data_quality_issues(),
                'validation': {
                    'validated_zero_pnl': validated_zero_pnl,
                    'invalid_zero_pnl': invalid_zero_pnl,
                    'warnings': validation_warnings
                },
                'timestamp': datetime.now().isoformat()
            }

            self._log_quality_analysis(analysis)

            # Вывод результатов валидации
            if validated_zero_pnl > 0:
                print(f"\n✅ Валидировано меток с PnL=0: {validated_zero_pnl}")

            if validation_warnings:
                print(f"\n⚠️  WARNINGS: Найдено {len(validation_warnings)} некорректных меток с PnL=0:")
                for w in validation_warnings[:10]:  # показываем первые 10
                    label_str = 'BUY' if w['label'] == 1 else 'SELL'
                    print(f"   • ts={w['timestamp']} | {label_str} | {w['method']} | {w['issue']}")
                if len(validation_warnings) > 10:
                    print(f"   ... и еще {len(validation_warnings) - 10} warnings")

            return analysis

        except Exception as err:
            logger.error(f"Ошибка расширенного анализа: {err}")
            return default_result

    def _calculate_total_metrics(self, df_quality: pd.DataFrame) -> Dict[str, float]:
        """Расчет общих метрик с PNL+ и PNL-"""
        if df_quality.empty:
            return {}

        total_profitable = df_quality['profitable_signals'].sum()
        total_signals = df_quality['total_signals'].sum()
        overall_success = total_profitable / total_signals if total_signals > 0 else 0

        # ⬇️ ДОБАВЛЯЕМ РАСЧЕТ PNL+ И PNL-
        total_positive_pnl = df_quality['total_positive_pnl'].sum()
        total_negative_pnl = df_quality['total_negative_pnl'].sum()
        total_pnl = df_quality['total_pnl'].sum()

        return {
            'total_signals': int(total_signals),
            'profitable_signals': int(total_profitable),
            'success_rate': float(overall_success),
            'avg_confidence': float(df_quality['avg_confidence'].mean()),
            'avg_profit': float(df_quality['avg_profit'].mean()),
            'total_pnl': float(total_pnl),
            # ⬇️ НОВЫЕ МЕТРИКИ
            'total_positive_pnl': float(total_positive_pnl),
            'total_negative_pnl': float(total_negative_pnl),
            'pnl_ratio': abs(total_positive_pnl / total_negative_pnl) if total_negative_pnl != 0 else float('inf')
        }

    def _find_best_method(self, df_quality: pd.DataFrame) -> Dict[str, Any]:
        """Поиск лучшего метода - ВСПОМОГАТЕЛЬНЫЙ МЕТОД"""
        if df_quality.empty:
            return {'method': 'N/A', 'success_rate': 0.0}

        # Используем правильное имя колонки из SQL-запроса
        if 'labeling_method' not in df_quality.columns:
            # Если колонка называется иначе, используем первую доступную
            method_col = df_quality.columns[0] if len(df_quality.columns) > 0 else 'method'
        else:
            method_col = 'labeling_method'

        profitable_methods = df_quality[df_quality['profitable_signals'] > 0].copy()

        if profitable_methods.empty:
            return {'method': 'N/A', 'success_rate': 0.0}

        profitable_methods['success_rate'] = (
                profitable_methods['profitable_signals'] / profitable_methods['total_signals']
        )

        best_idx = profitable_methods['success_rate'].idxmax()
        best_method = profitable_methods.loc[best_idx]

        return {
            'method': best_method[method_col],
            'success_rate': float(best_method['success_rate']),
            'total_signals': int(best_method['total_signals']),
            'avg_profit': float(best_method['avg_profit'])
        }

    def configure_settings(self):
        """Настройка параметров - базовая реализация"""
        print("\n⚙️  НАСТРОЙКА ПАРАМЕТРОВ:")
        print("Доступные параметры:")
        print(f"1. Метод разметки: {self.config.method}")
        print(f"2. Min profit target: {self.config.min_profit_target}")
        print(f"3. Hold bars: {self.config.hold_bars}")
        print("Реализация настройки параметров требует дополнительной разработки")

    def show_stats(self):
        """Показать статистику - базовая реализация"""
        stats = self.data_loader.get_data_stats()
        print("\n📊 СТАТИСТИКА ДАННЫХ:")
        for key, value in stats.items():
            print(f"   {key}: {value}")

    def _detect_data_quality_issues(self) -> List[str]:
        """Обнаружение проблем качества данных - SQLAlchemy версия"""
        issues = []

        try:
            # Используем self.engine вместо self.conn
            # Проверка на дубликаты меток
            query_duplicates = """
                SELECT timestamp, COUNT(*) as cnt 
                FROM labeling_results 
                WHERE symbol = :symbol
                GROUP BY timestamp 
                HAVING COUNT(*) > 1
            """
            duplicates = pd.read_sql_query(
                query_duplicates,
                self.engine,
                params={'symbol': self.config.symbol}
            )
            if not duplicates.empty:
                issues.append(f"Обнаружены дубликаты меток: {len(duplicates)} случаев")

            # Остальные проверки аналогично исправляем...
            # ...

        except Exception as err:
            issues.append(f"Ошибка при проверке качества: {err}")

        return issues

    def _log_quality_analysis(self, analysis: Dict[str, Any]):
        """Логирование анализа качества с корректным форматированием процентов"""
        logger.info("\n" + "=" * 60)
        logger.info("📊 РАСШИРЕННЫЙ АНАЛИЗ КАЧЕСТВА")
        logger.info("=" * 60)

        total_metrics = analysis.get('total_metrics', {})
        best_method = analysis.get('best_method', {})
        issues = analysis.get('data_quality_issues', [])

        total_pnl_value = total_metrics.get('total_pnl', 0)
        total_positive_pnl = total_metrics.get('total_positive_pnl', 0)
        total_negative_pnl = total_metrics.get('total_negative_pnl', 0)
        pnl_ratio = total_metrics.get('pnl_ratio', 0)
        avg_profit = total_metrics.get('avg_profit', 0)

        # ⬇️ ИСПРАВЛЕНО: умножаем на 100 для процентов
        logger.info(f"📈 Общая успешность: {total_metrics.get('success_rate', 0):.1%}")
        logger.info(f"💰 Общий PNL+: {total_positive_pnl:+.3f} ({total_positive_pnl * 100:+.1f}%)")
        logger.info(f"💸 Общий PNL-: {total_negative_pnl:+.3f} ({total_negative_pnl * 100:+.1f}%)")
        logger.info(f"📊 Соотношение PNL: {pnl_ratio:.1f}:1" if pnl_ratio != float(
            'inf') else "📊 Соотношение PNL: ∞ (нет убытков)")
        logger.info(f"🏆 Лучший метод: {best_method.get('method', 'N/A')} ({best_method.get('success_rate', 0):.1%})")
        logger.info(f"📊 Всего сигналов: {total_metrics.get('total_signals', 0)}")
        logger.info(f"💵 Средний PnL: {avg_profit:.4f} ({avg_profit:.1%})")
        logger.info(f"🎯 Совокупный PnL: {total_pnl_value:+.3f} ({total_pnl_value * 100:+.1f}%)")

        if issues:
            logger.warning("⚠️ Обнаруженные проблемы:")
            for issue in issues:
                logger.warning(f"   • {issue}")
        else:
            logger.info("✅ Критических проблем не обнаружено")

        # ⬇️ ИСПРАВЛЕННЫЙ КОНСОЛЬНЫЙ ВЫВОД
        print(f"\n🎯 ИТОГИ АНАЛИЗА:")
        print(f"   📈 Успешность: {total_metrics.get('success_rate', 0):.1%} сделок")
        print(f"   💰 Прибыль: {total_positive_pnl:+.3f} ({total_positive_pnl * 100:+.1f}%)")
        print(f"   💸 Убытки: {total_negative_pnl:+.3f} ({total_negative_pnl * 100:+.1f}%)")
        print(f"   📊 Чистая прибыль: {total_pnl_value:+.3f} ({total_pnl_value * 100:+.1f}%)")

        if pnl_ratio > 2:
            print(f"   ✅ Соотношение: {pnl_ratio:.1f}:1 (отличное)")
        elif pnl_ratio > 1:
            print(f"   ⚠️  Соотношение: {pnl_ratio:.1f}:1 (удовлетворительное)")
        else:
            print(f"   ❌ Соотношение: {pnl_ratio:.1f}:1 (требует улучшения)")

    def export_feature_importance(self, *args, run_id: str | None = None, model_name: str = "unknown",
                                  top_n: int | None = None, **kwargs) -> int:
        """
        Сохраняет важность признаков в SQLite (таблица training_feature_importance).
        Поддерживает гибкие входы:
          • export_feature_importance(df, ...)            # df с колонками ['feature','importance'] или индекс=feature
          • export_feature_importance(series, ...)        # pandas.Series: index=feature, values=importance
          • export_feature_importance(dict, ...)          # dict: feature -> importance
          • export_feature_importance(list_of_tuples, ...)# [(feature, importance), ...]
        Параметры:
          run_id    — к какому снапшоту относится важность (обязательно для консистентности; если None, берём последний READY по symbol)
          model_name— имя/идентификатор модели (например, 'lgbm_v1')
          top_n     — при задании сохраняем только top-N по важности
        Возвращает: число сохранённых строк.
        """


        # 0) ensure DDL
        self._ensure_training_snapshot_tables()

        # 1) определить run_id, если не передан
        if run_id is None:
            with self.engine.begin() as conn:
                row = conn.execute(text("""
                    SELECT run_id
                      FROM training_dataset_meta
                     WHERE status='READY' AND symbol=:symbol
                  ORDER BY created_at DESC
                     LIMIT 1
                """), {"symbol": self.config.symbol}).mappings().first()
            if not row:
                raise RuntimeError("Нет готового снапшота (status=READY). Укажите run_id вручную.")
            run_id = row["run_id"]

        # 2) извлечь входные данные importance
        importance_obj = None
        if args:
            importance_obj = args[0]
        elif "importance" in kwargs:
            importance_obj = kwargs["importance"]
        elif "df" in kwargs:
            importance_obj = kwargs["df"]

        if importance_obj is None:
            raise ValueError("Не переданы данные важности фич (df/series/dict/list).")

        # 3) нормализация входа -> DataFrame с колонками ['feature','importance']
        if isinstance(importance_obj, pd.DataFrame):
            df_imp = importance_obj.copy()
            # допускаем разные варианты имен
            if "feature" not in df_imp.columns or "importance" not in df_imp.columns:
                if df_imp.shape[1] == 1:  # одна колонка важностей, index = feature
                    df_imp = df_imp.reset_index()
                    df_imp.columns = ["feature", "importance"]
                elif df_imp.shape[1] >= 2:
                    # пробуем первые две как feature/importance
                    cols = list(df_imp.columns)
                    df_imp = df_imp[[cols[0], cols[1]]].copy()
                    df_imp.columns = ["feature", "importance"]
        elif hasattr(importance_obj, "to_frame"):  # Series
            s = importance_obj
            df_imp = s.to_frame(name="importance").reset_index()
            if df_imp.columns[0] != "feature":
                df_imp.columns = ["feature", "importance"]
        elif isinstance(importance_obj, dict):
            df_imp = pd.DataFrame(list(importance_obj.items()), columns=["feature", "importance"])
        elif isinstance(importance_obj, (list, tuple)):
            df_imp = pd.DataFrame(importance_obj, columns=["feature", "importance"])
        else:
            raise TypeError(f"Неподдерживаемый тип входа для важности фич: {type(importance_obj)}")

        # убрать NaN/inf и привести типы
        df_imp = df_imp.dropna(subset=["feature"]).copy()
        df_imp["importance"] = pd.to_numeric(df_imp["importance"], errors="coerce")
        df_imp = df_imp.replace([np.inf, -np.inf], np.nan).dropna(subset=["importance"])

        if df_imp.empty:
            raise ValueError("Таблица важности фич пуста после нормализации входных данных.")

        # нормировать на сумму=1 (общепринято для интерпретации)
        ssum = float(df_imp["importance"].sum())
        if ssum > 0:
            df_imp["importance"] = df_imp["importance"] / ssum

        # сортировка и топ-N
        df_imp = df_imp.sort_values("importance", ascending=False).reset_index(drop=True)
        if top_n is not None and top_n > 0:
            df_imp = df_imp.head(int(top_n))

        df_imp["rank"] = np.arange(1, len(df_imp) + 1, dtype=int)
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        df_imp["run_id"] = run_id
        df_imp["model_name"] = str(model_name)
        df_imp["created_at"] = created_at

        # порядок колонок для записи
        out_cols = ["run_id", "model_name", "feature", "importance", "rank", "created_at"]
        df_out = df_imp[out_cols].copy()

        # 4) сохранить в БД (upsert по PRIMARY KEY)
        inserted = 0
        with self.engine.begin() as conn:
            # удалим предыдущие записи для (run_id, model_name), чтобы не копить ряды при повторном экспорте
            conn.execute(text("""
                DELETE FROM training_feature_importance
                 WHERE run_id=:rid AND model_name=:mname
            """), {"rid": run_id, "mname": model_name})

            df_out.to_sql("training_feature_importance", self.engine, if_exists="append", index=False)
            inserted = len(df_out)

        logger.info("✅ Важность фич сохранена: run_id=%s, model=%s, rows=%d", run_id, model_name, inserted)
        # для удобства — печать top-10
        preview = df_out.sort_values("rank").head(10)
        print("\n🏷️ TOP важностей (до 10 строк):")
        for _, r in preview.iterrows():
            print(f"  {r['rank']:>2}. {r['feature']:<30} {r['importance']:.4f}")

        return inserted

    def cross_validation_split(self, n_splits: int = 5, test_size: float = 0.2) -> Dict[str, Any]:
        """
        Временная кросс-валидация БЕЗ генерации негативов.
        Датасет формируется как при снапшоте:
          - метки из labeling_results (reversal_label ∈ {0,1,2})
          - фичи из candles_* (load_indicators)
          - жёсткая валидация (_validate_snapshot_frame)
        Разбиение: time-based, без утечки будущего.
        """
        import pandas as pd
        from collections import Counter
        import traceback
        logger.info(f"🎯 Создание {n_splits}-fold кросс-валидации...")

        try:
            # 1) Сборка датасета как в снапшоте
            raw_df, _meta = self._build_training_snapshot_dataframe()

            # 2) Жёсткая валидация/очистка
            df_clean, issues, nan_drop_rows, duplicates_count = self._validate_snapshot_frame(raw_df)
            if df_clean.empty:
                raise ValueError("Датасет пуст после валидации")

            dataset = df_clean.reset_index(drop=True)

            # 3) Временная метка
            if "ts" in dataset.columns:
                try:
                    # Пробуем миллисекунды (стандарт для Binance)
                    dataset["datetime"] = pd.to_datetime(dataset["ts"], unit="ms", utc=True)
                except (pd.errors.OutOfBoundsDatetime, OverflowError):
                    try:
                        # Если не сработало - пробуем секунды
                        dataset["datetime"] = pd.to_datetime(dataset["ts"], unit="s", utc=True)
                    except (pd.errors.OutOfBoundsDatetime, OverflowError):
                        # Крайний случай - синтетическая шкала
                        logger.warning("⚠️ Не удалось конвертировать ts, используем синтетическую шкалу")
                        dataset["datetime"] = pd.date_range(start="2020-01-01", periods=len(dataset), freq="5min",
                                                            tz="UTC")
            elif "datetime" not in dataset.columns:
                dataset["datetime"] = pd.date_range(start="2020-01-01", periods=len(dataset), freq="5min", tz="UTC")
            # 4) Сортировка по времени
            dataset = dataset.sort_values("datetime").reset_index(drop=True)

            total_samples = len(dataset)
            if not (0 < test_size < 1):
                raise ValueError("test_size должен быть в (0,1)")
            test_samples = max(1, int(total_samples * test_size))

            # sanity-check на объём
            if total_samples < max(n_splits * 10, n_splits + test_samples):
                raise ValueError(
                    f"Недостаточно данных: {total_samples} samples для {n_splits} фолдов (test_size={test_size})")

            logger.info(f"📊 Датасет для CV: {total_samples} строк")

            # 5) Формируем временные фолды (blocked, без перемешивания)
            splits = []
            # шаг смещения окна так, чтобы мы получили n_splits фолдов
            step = (total_samples - test_samples) // n_splits
            step = max(step, 1)

            for i in range(n_splits):
                start_idx = i * step
                end_idx = start_idx + test_samples
                if end_idx > total_samples:
                    end_idx = total_samples
                    start_idx = max(0, end_idx - test_samples)

                test_indices = list(range(start_idx, end_idx))
                train_indices = list(range(0, start_idx)) + list(range(end_idx, total_samples))

                # на всякий: без пересечений и покрытие всех индексов
                if len(set(train_indices).intersection(test_indices)) != 0:
                    logger.warning(f"⚠️ Пересечение индексов в фолде {i + 1}")
                if len(set(train_indices + test_indices)) != total_samples:
                    logger.warning(f"⚠️ Неполное покрытие индексов в фолде {i + 1}")

                split_info = {
                    "fold": i + 1,
                    "train_indices": train_indices,
                    "test_indices": test_indices,
                    "train_size": len(train_indices),
                    "test_size": len(test_indices),
                    "train_period": (
                        f"{dataset.iloc[train_indices[0]]['datetime']} → {dataset.iloc[train_indices[-1]]['datetime']}"
                        if train_indices else "N/A"
                    ),
                    "test_period": (
                        f"{dataset.iloc[test_indices[0]]['datetime']} → {dataset.iloc[test_indices[-1]]['datetime']}"
                        if test_indices else "N/A"
                    ),
                }
                splits.append(split_info)

            result = {
                "n_splits": n_splits,
                "test_size": test_size,
                "total_samples": total_samples,
                "splits": splits,
                "class_distribution": dict(Counter(dataset["reversal_label"])),
            }

            logger.info(f"✅ Кросс-валидация создана: {n_splits} фолдов, {total_samples} samples")
            print("\n✅ Кросс-валидация создана успешно!")
            print(f"📊 Всего samples: {total_samples}")
            print(f"🎯 Фолдов: {n_splits}")
            print(f"📈 Распределение классов: {result['class_distribution']}")
            return result

        except Exception as err:
            logger.error(f"❌ Ошибка создания кросс-валидации: {err}")
            logger.error(f"🔍 Детали ошибки: {traceback.format_exc()}")
            return {}

    def detect_label_leakage(self) -> Dict[str, Any]:
        """
        Обнаружение утечки меток — с расширенной диагностикой
        """
        logger.info("🔍 Проверка на утечку меток...")

        duplicate_pairs = []
        high_corr_features = []
        issues = []

        try:
            df_positives = self.data_loader.load_labeled_data()

            if df_positives.empty:
                return {
                    'leakage_detected': False,
                    'issues': ['Нет размеченных меток для анализа'],
                    'high_corr_features': [],
                    'duplicate_feature_pairs': [],
                    'total_positives': 0
                }

            logger.info(f"📊 Анализируем {len(df_positives)} размеченных примеров")

            # РАСШИРЕННАЯ ДИАГНОСТИКА
            print(f"\n📈 ДЕТАЛЬНАЯ СТАТИСТИКА МЕТОК:")
            print(f"   • Всего меток: {len(df_positives)}")
            print(f"   • BUY (1): {len(df_positives[df_positives['reversal_label'] == 1])}")
            print(f"   • SELL (2): {len(df_positives[df_positives['reversal_label'] == 2])}")

            # Анализ по методам разметки
            if 'method' in df_positives.columns:
                method_stats = df_positives['method'].value_counts()
                print(f"   • По методам: {method_stats.to_dict()}")

            # Анализ качества меток
            if 'pnl' in df_positives.columns:
                avg_pnl = df_positives['pnl'].mean()
                profitable = len(df_positives[df_positives['pnl'] > 0])
                success_rate = profitable / len(df_positives)
                print(f"   • Успешность: {success_rate:.1%} ({profitable}/{len(df_positives)})")
                print(f"   • Средний PnL: {avg_pnl:.4f}")

            # Проверка 1: Высокая корреляция фич с меткой
            available_features = [col for col in self.data_loader.feature_names if col in df_positives.columns]

            print(f"\n🔍 АНАЛИЗ ФИЧ ({len(available_features)} доступно):")

            feature_correlations = []
            for feature in available_features:
                if df_positives[feature].dtype not in [np.float64, np.int64]:
                    continue

                clean_data = df_positives[[feature, 'reversal_label']].dropna()
                if len(clean_data) < 10:  # Минимум 10 samples
                    continue

                try:
                    correlation = clean_data[feature].corr(clean_data['reversal_label'])
                    if not np.isnan(correlation):
                        feature_correlations.append((feature, abs(correlation)))

                        if abs(correlation) > 0.8:
                            high_corr_features.append((feature, correlation))
                except:
                    continue

            # Сортируем фичи по корреляции
            feature_correlations.sort(key=lambda x: x[1], reverse=True)

            # Показываем топ-5 самых коррелированных фич
            if feature_correlations:
                print("   🏆 Топ-5 фич по корреляции с меткой:")
                for feature, corr in feature_correlations[:5]:
                    leak_warning = " ⚠️ УТЕЧКА!" if abs(corr) > 0.8 else ""
                    print(f"      • {feature}: {corr:.4f}{leak_warning}")

            if high_corr_features:
                issues.append(f"Очень высокая корреляция с меткой: {len(high_corr_features)} фич")
                for feature, corr in high_corr_features:
                    logger.warning(f"   ⚠️ {feature}: {corr:.4f}")

            # Проверка 2: Дублирующиеся фичи
            if len(available_features) > 1:
                try:
                    # Используем только числовые фичи
                    numeric_features = [f for f in available_features
                                        if df_positives[f].dtype in [np.float64, np.int64]]

                    if len(numeric_features) > 1:
                        corr_matrix = self.data_loader.safe_correlation_calculation(df_positives, numeric_features)

                        for i in range(len(corr_matrix.columns)):
                            for j in range(i + 1, len(corr_matrix.columns)):
                                if corr_matrix.iloc[i, j] > 0.95:
                                    duplicate_pairs.append((
                                        corr_matrix.columns[i],
                                        corr_matrix.columns[j],
                                        corr_matrix.iloc[i, j]
                                    ))

                        if duplicate_pairs:
                            issues.append(f"Дублирующиеся фичи: {len(duplicate_pairs)} пар")
                            print(f"\n🔁 ДУБЛИРУЮЩИЕСЯ ФИЧИ:")
                            for pair in duplicate_pairs[:3]:  # Показываем только первые 3
                                print(f"   • {pair[0]} ≈ {pair[1]} (corr={pair[2]:.3f})")
                except Exception as corr_err:
                    issues.append(f"Ошибка расчета корреляций: {corr_err}")

            leakage_detected = len(high_corr_features) > 0

            result = {
                'leakage_detected': leakage_detected,
                'issues': issues,
                'high_corr_features': high_corr_features,
                'duplicate_feature_pairs': duplicate_pairs,
                'total_positives': len(df_positives),
                'available_features': len(available_features),
                'feature_correlations': feature_correlations[:10]  # Топ-10 фич
            }

            if leakage_detected:
                logger.warning("⚠️ Обнаружена потенциальная утечка меток!")
                print("\n❌ ВЫВОД: Обнаружена утечка меток!")
            else:
                logger.info("✅ Утечка меток не обнаружена")
                print("\n✅ ВЫВОД: Утечка меток не обнаружена")

            return result

        except Exception as err:
            logger.error(f"❌ Ошибка проверки утечки: {err}")
            return {
                'leakage_detected': False,
                'issues': [f'Ошибка при анализе: {err}'],
                'high_corr_features': [],
                'duplicate_feature_pairs': [],
                'total_positives': 0
            }

    # =========================================================================
    # СУЩЕСТВУЮЩИЕ МЕТОДЫ ИЗ ИСХОДНОГО КОДА (адаптированные)
    # =========================================================================

    def save_to_db(self, results: List[Dict]):
        """Сохранение результатов в БД через SQLAlchemy - УЛУЧШЕННАЯ ОБРАБОТКА ОШИБОК"""
        from sqlalchemy import text

        insert_sql = text("""
            INSERT OR REPLACE INTO labeling_results 
            (symbol, timestamp, timeframe, reversal_label, reversal_confidence,
             labeling_method, labeling_params, extreme_index, extreme_price, extreme_timestamp,
             confirmation_index, confirmation_timestamp, price_change_after, features_json,
             is_high_quality, created_at)
            VALUES (:symbol, :timestamp, :timeframe, :reversal_label, :reversal_confidence,
                    :labeling_method, :labeling_params, :extreme_index, :extreme_price, :extreme_timestamp,
                    :confirmation_index, :confirmation_timestamp, :price_change_after, :features_json,
                    :is_high_quality, CURRENT_TIMESTAMP)
        """)

        successful_saves = 0
        with self.engine.connect() as conn:
            for res in results:
                try:
                    #   гарантируем что timestamp будут INTEGER
                    timestamp = int(res.get('timestamp', 0))
                    extreme_timestamp = int(res.get('extreme_timestamp', 0))
                    confirmation_timestamp = int(res.get('confirmation_timestamp', 0))

                    #   проверяем обязательные поля
                    if timestamp == 0 or extreme_timestamp == 0:
                        logger.warning(f"⚠️ Пропуск записи с некорректными timestamp: {res.get('symbol')}")
                        continue

                    conn.execute(insert_sql, {
                        'symbol': res['symbol'],
                        'timestamp': timestamp,
                        'timeframe': res['timeframe'],
                        'reversal_label': res['reversal_label'],
                        'reversal_confidence': res.get('reversal_confidence', 1.0),
                        'labeling_method': res['labeling_method'],
                        'labeling_params': json.dumps(res.get('labeling_params', {})),
                        'extreme_index': res.get('extreme_index'),
                        'extreme_price': res.get('extreme_price'),
                        'extreme_timestamp': extreme_timestamp,
                        'confirmation_index': res.get('confirmation_index'),
                        'confirmation_timestamp': confirmation_timestamp,
                        'price_change_after': res.get('price_change_after'),
                        'features_json': res.get('features_json'),
                        'is_high_quality': res.get('is_high_quality', 1)
                    })
                    successful_saves += 1

                except Exception as row_err:
                    logger.error(f"❌ Ошибка сохранения записи {res.get('symbol', 'N/A')}: {row_err}")
                    continue

            conn.commit()

        logger.info(f"✅ Сохранено {successful_saves}/{len(results)} меток через SQLAlchemy")
        if successful_saves < len(results):
            logger.warning(f"⚠️ Не сохранено {len(results) - successful_saves} меток из-за ошибок")

    def manual_mode(self):
        """Ручная разметка - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        df = self.load_data()
        print("\n=== РУЧНАЯ РАЗМЕТКА ===")
        print("Формат: <индекс>,<BUY/SELL>. 'done' для завершения.")

        while True:
            user_input = input(">> ").strip()
            if user_input.lower() == 'done':
                break
            if ',' not in user_input:
                print("Формат: индекс,тип")
                continue
            try:
                idx_str, typ = user_input.split(',')
                idx = int(idx_str.strip())
                typ = typ.strip().upper()
                if typ not in ['BUY', 'SELL']:
                    print("Только BUY или SELL")
                    continue
                if idx < 0 or idx >= len(df):
                    print(f"0 ≤ индекс < {len(df)}")
                    continue

                exit_idx = min(idx + self.config.hold_bars, len(df) - 1)
                pnl, is_profitable = self._calculate_pnl_to_index(df, idx, typ, exit_idx)
                print(f"PnL: {pnl:.4f} (требуется: ≥{self.config.min_profit_target:.4f})")

                if not is_profitable:
                    print(f"⚠️  Метка не достигла profit target! Все равно сохранить? (y/n)")
                    confirm = input(">> ").strip().lower()
                    if confirm != 'y':
                        continue

                label = 1 if typ == 'BUY' else 2

                row_dict = df.iloc[idx].to_dict()
                for k, v in row_dict.items():
                    if pd.isna(v):
                        row_dict[k] = None
                    elif isinstance(v, pd.Timestamp):
                        row_dict[k] = v.isoformat()
                    elif isinstance(v, (np.integer, np.int64)):
                        row_dict[k] = int(v)
                    elif isinstance(v, (np.floating, np.float64)):
                        row_dict[k] = float(v)
                    elif isinstance(v, str):
                        row_dict[k] = v

                timestamp = int(df['ts'].iloc[idx])

                result = {
                    'symbol': self.config.symbol,
                    'timestamp': timestamp,  # ← INTEGER
                    'timeframe': self.config.timeframe,
                    'reversal_label': label,
                    'reversal_confidence': 1.0,
                    'labeling_method': 'MANUAL',
                    'extreme_index': idx,
                    'extreme_price': df['close'].iloc[idx],
                    'extreme_timestamp': timestamp,  # ← INTEGER
                    'confirmation_index': idx,
                    'confirmation_timestamp': timestamp,  # ← INTEGER
                    'price_change_after': pnl,
                    'features_json': json.dumps(row_dict),
                    'is_high_quality': 1 if is_profitable else 0
                }
                self.save_to_db([result])
                self.labels.append({'index': idx, 'type': typ, 'pnl': pnl})
                print(f"✅ Метка {typ} сохранена (PnL: {pnl:.4f})")
            except Exception as err:
                print(f"Ошибка: {err}")

    def analyze_pnl_distribution(self, method: str = None):
        """
        Анализ распределения PnL через гистограмму
        """
        try:
            import matplotlib.pyplot as plt
            import seaborn as sns
        except ImportError:
            print("❌ Для анализа PnL требуется matplotlib и seaborn")
            print("   Установите: pip install matplotlib seaborn")
            return

        logger.info("📊 Анализ распределения PnL...")

        try:
            # Загружаем размеченные данные
            query = """
                SELECT labeling_method, price_change_after as pnl, reversal_label
                FROM labeling_results 
                WHERE symbol = :symbol
            """

            if method:
                query += " AND labeling_method = :method"
                params = {'symbol': self.config.symbol, 'method': method}
            else:
                params = {'symbol': self.config.symbol}

            df_pnl = pd.read_sql_query(query, self.engine, params=params)

            if df_pnl.empty:
                logger.warning("❌ Нет данных для анализа PnL")
                return

            print(f"\n📈 СТАТИСТИКА PnL:")
            print(f"   • Всего сигналов: {len(df_pnl)}")
            print(f"   • Средний PnL: {df_pnl['pnl'].mean():.4f}")
            print(f"   • Медиана PnL: {df_pnl['pnl'].median():.4f}")
            print(f"   • Std PnL: {df_pnl['pnl'].std():.4f}")
            print(f"   • Min PnL: {df_pnl['pnl'].min():.4f}")
            print(f"   • Max PnL: {df_pnl['pnl'].max():.4f}")
            print(f"   • PnL > 0: {(df_pnl['pnl'] > 0).sum()} ({(df_pnl['pnl'] > 0).mean():.1%})")
            print(f"   • PnL < 0: {(df_pnl['pnl'] < 0).sum()} ({(df_pnl['pnl'] < 0).mean():.1%})")

            # Анализ по методам
            if 'labeling_method' in df_pnl.columns:
                print(f"\n📊 РАСПРЕДЕЛЕНИЕ ПО МЕТОДАМ:")
                method_stats = df_pnl.groupby('labeling_method').agg({
                    'pnl': ['count', 'mean', 'std', 'min', 'max'],
                }).round(4)
                print(method_stats)

            # Создаем гистограмму
            plt.figure(figsize=(12, 8))

            # Гистограмма 1: Общее распределение
            plt.subplot(2, 2, 1)
            plt.hist(df_pnl['pnl'], bins=50, alpha=0.7, edgecolor='black')
            plt.axvline(df_pnl['pnl'].mean(), color='red', linestyle='--', label=f'Среднее: {df_pnl["pnl"].mean():.4f}')
            plt.axvline(0, color='green', linestyle='-', label='Нулевая отметка')
            plt.xlabel('PnL')
            plt.ylabel('Частота')
            plt.title('Распределение PnL (все методы)')
            plt.legend()
            plt.grid(True, alpha=0.3)

            # Гистограмма 2: Только положительные PnL
            plt.subplot(2, 2, 2)
            positive_pnl = df_pnl[df_pnl['pnl'] > 0]['pnl']
            if len(positive_pnl) > 0:
                plt.hist(positive_pnl, bins=30, alpha=0.7, color='green', edgecolor='black')
                plt.axvline(positive_pnl.mean(), color='red', linestyle='--',
                            label=f'Среднее: {positive_pnl.mean():.4f}')
                plt.xlabel('PnL (> 0)')
                plt.ylabel('Частота')
                plt.title('Распределение положительных PnL')
                plt.legend()
                plt.grid(True, alpha=0.3)

            # Boxplot по методам
            plt.subplot(2, 2, 3)
            if 'labeling_method' in df_pnl.columns and df_pnl['labeling_method'].nunique() > 1:
                df_pnl.boxplot(column='pnl', by='labeling_method', ax=plt.gca())
                plt.title('PnL по методам разметки')
                plt.suptitle('')  # Убираем автоматический заголовок
                plt.xticks(rotation=45)

            # Кумулятивная распределение
            plt.subplot(2, 2, 4)
            sorted_pnl = np.sort(df_pnl['pnl'])
            cumulative = np.arange(1, len(sorted_pnl) + 1) / len(sorted_pnl)
            plt.plot(sorted_pnl, cumulative, linewidth=2)
            plt.axvline(0, color='green', linestyle='--', alpha=0.7, label='Нулевая отметка')
            plt.xlabel('PnL')
            plt.ylabel('Кумулятивная вероятность')
            plt.title('Кумулятивное распределение PnL')
            plt.grid(True, alpha=0.3)
            plt.legend()

            plt.tight_layout()

            # Сохраняем график
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"pnl_analysis_{self.config.symbol}_{timestamp}.png"
            plt.savefig(filename, dpi=150, bbox_inches='tight')
            plt.show()

            print(f"\n💾 График сохранен как: {filename}")

            # Детальный анализ выбросов
            self._analyze_pnl_outliers(df_pnl)

        except Exception as err:
            logger.error(f"❌ Ошибка анализа PnL: {err}")
            print(f"❌ Ошибка: {err}")

    def _analyze_pnl_outliers(self, df_pnl: pd.DataFrame):
        """Анализ выбросов в PnL"""
        print(f"\n🔍 АНАЛИЗ ВЫБРОСОВ PnL:")

        Q1 = df_pnl['pnl'].quantile(0.25)
        Q3 = df_pnl['pnl'].quantile(0.75)
        IQR = Q3 - Q1
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR

        outliers = df_pnl[(df_pnl['pnl'] < lower_bound) | (df_pnl['pnl'] > upper_bound)]

        print(f"   • Выбросы (IQR метод): {len(outliers)} сигналов")
        print(f"   • Границы выбросов: [{lower_bound:.4f}, {upper_bound:.4f}]")

        if not outliers.empty and 'labeling_method' in outliers.columns:
            print(f"   • Выбросы по методам:")
            outlier_methods = outliers['labeling_method'].value_counts()
            for method, count in outlier_methods.items():
                print(f"      - {method}: {count} выбросов")

        # Анализ экстремальных значений
        if len(df_pnl) > 0:
            extreme_positive = df_pnl.nlargest(5, 'pnl')
            extreme_negative = df_pnl.nsmallest(5, 'pnl')

            print(f"\n📈 ТОП-5 самых прибыльных сигналов:")
            for _, row in extreme_positive.iterrows():
                method = row.get('labeling_method', 'N/A')
                print(f"   • {method}: {row['pnl']:.4f}")

            print(f"\n📉 ТОП-5 самых убыточных сигналов:")
            for _, row in extreme_negative.iterrows():
                method = row.get('labeling_method', 'N/A')
                print(f"   • {method}: {row['pnl']:.4f}")

    def quick_pnl_analysis(self):
        """Быстрый анализ PnL без графиков"""
        query = """
            SELECT labeling_method, price_change_after as pnl 
            FROM labeling_results 
            WHERE symbol = :symbol
        """

        try:
            df_pnl = pd.read_sql_query(query, self.engine, params={'symbol': self.config.symbol})

            if df_pnl.empty:
                print("❌ Нет данных для анализа")
                return

            print(f"\n📊 БЫСТРЫЙ АНАЛИЗ PnL:")
            print(f"   • Всего сигналов: {len(df_pnl)}")
            print(f"   • Средний PnL: {df_pnl['pnl'].mean():.4f}")
            print(f"   • Медиана PnL: {df_pnl['pnl'].median():.4f}")
            print(f"   • Успешность: {(df_pnl['pnl'] > 0).mean():.1%}")

            # Анализ по методам
            if 'labeling_method' in df_pnl.columns:
                print(f"\n📈 ПО МЕТОДАМ:")
                for method in df_pnl['labeling_method'].unique():
                    method_data = df_pnl[df_pnl['labeling_method'] == method]
                    success_rate = (method_data['pnl'] > 0).mean()
                    avg_pnl = method_data['pnl'].mean()
                    print(
                        f"   • {method}: {len(method_data)} сигналов, успешность: {success_rate:.1%}, средний PnL: {avg_pnl:.4f}")

        except Exception as err:
            print(f"❌ Ошибка быстрого анализа PnL: {err}")

    def clear_labeling_table(self, confirmation_required: bool = True):
        """
        Очистка таблицы с разметками
        """
        try:
            if confirmation_required:
                # Показываем статистику перед очисткой
                stats_query = """
                    SELECT labeling_method, COUNT(*) as count 
                    FROM labeling_results 
                    WHERE symbol = :symbol 
                    GROUP BY labeling_method
                """
                stats = pd.read_sql_query(stats_query, self.engine, params={'symbol': self.config.symbol})

                if stats.empty:
                    print("✅ Таблица labeling_results уже пуста")
                    return

                print(f"\n⚠️  ВНИМАНИЕ: БУДЕТ УДАЛЕНО ВСЕХ МЕТОК!")
                print(f"📊 Текущая статистика для {self.config.symbol}:")
                total_count = 0
                for _, row in stats.iterrows():
                    print(f"   • {row['labeling_method']}: {row['count']} меток")
                    total_count += row['count']
                print(f"   • ВСЕГО: {total_count} меток")

                confirm = input(f"\n❓ Вы уверены? Это действие необратимо! (y/N): ").strip().lower()
                if confirm != 'y':
                    print("❌ Очистка отменена")
                    return

            # Выполняем очистку
            delete_query = "DELETE FROM labeling_results WHERE symbol = :symbol"
            with self.engine.connect() as conn:
                result = conn.execute(text(delete_query), {'symbol': self.config.symbol})
                conn.commit()

            deleted_count = result.rowcount
            logger.info(f"🧹 Очищено {deleted_count} меток для символа {self.config.symbol}")
            print(f"✅ Очищено {deleted_count} меток")

        except Exception as err:
            logger.error(f"❌ Ошибка очистки таблицы: {err}")
            print(f"❌ Ошибка: {err}")

    def clear_all_labeling_tables(self):
        """
        Очистка ВСЕХ данных разметки (все символы)
        """
        try:
            print(f"\n⚠️  КРИТИЧЕСКОЕ ВНИМАНИЕ!")
            print(f"   Будет удалена ВСЯ таблица labeling_results!")
            print(f"   Все метки для всех символов будут потеряны!")

            # Показываем общую статистику
            stats_query = "SELECT COUNT(*) as total_count FROM labeling_results"
            total_count = pd.read_sql_query(stats_query, self.engine).iloc[0]['total_count']
            print(f"   Всего меток в базе: {total_count}")

            confirm1 = input(f"\n❓ Вы абсолютно уверены? (yes/NO): ").strip().lower()
            if confirm1 != 'yes':
                print("❌ Очистка отменена")
                return

            # Очищаем всю таблицу
            delete_query = "DELETE FROM labeling_results"
            with self.engine.connect() as conn:
                result = conn.execute(text(delete_query))
                conn.commit()

            deleted_count = result.rowcount
            logger.info(f"🧹 Очищена вся таблица labeling_results: {deleted_count} меток")
            print(f"✅ Очищена вся таблица: {deleted_count} меток")

        except Exception as err:
            logger.error(f"❌ Ошибка полной очистки таблицы: {err}")
            print(f"❌ Ошибка: {err}")

    def mark_unprofitable_ranges_as_negatives(self) -> int:
        """
        Помечает убыточные BUY/SELL (price_change_after < threshold) и добавляет HOLD-точки в окне [текущий сигнал; следующий сигнал):
          • Обнуляет price_change_after у «лузеров» до 0.0 (как флаг применения правила)
          • Вставляет HOLD-END (label=0) на последнем баре ПЕРЕД следующим сигналом
          • Вставляет HOLD-MID (label=0) в середине окна, если окно достаточно длинное и выдержана минимальная дистанция до конца
        Возвращает количество обновлённых «лузеров».
        """

        import pandas as pd
        from sqlalchemy import text

        # ------------------------ Параметры из конфига (с дефолтами) ------------------------
        thr = float(getattr(self.config, "min_profit_target", 0.001))  # порог убыточности
        symbol = self.config.symbol
        tf = self.config.timeframe
        candles_table = f"candles_{tf}"

        # минимальная длина окна (в барах) для постановки второго HOLD (MID)
        min_window_bars = int(getattr(self.config, "hold_min_window_bars", 6))
        # минимальная дистанция (в барах) между HOLD-MID и HOLD-END
        min_mid_end_gap = int(getattr(self.config, "hold_min_mid_end_gap", 3))
        # смещения от краёв окна, в которых MID не ставим (чтобы не прилипал к краям)
        margin_left = int(getattr(self.config, "hold_mid_margin_left", 1))
        margin_right = int(getattr(self.config, "hold_mid_margin_right", 1))

        logger.info(f"🔧 Поиск убыточных меток: pnl < {thr} | {symbol} {tf} | MID+END HOLD")

        updated_count = 0
        inserted_rows = []

        with self.engine.begin() as conn:
            # 1) Убыточные BUY/SELL для текущего символа/TF
            losers = pd.read_sql(
                text("""
                    SELECT reversal_label, extreme_timestamp, price_change_after
                      FROM labeling_results
                     WHERE symbol=:symbol AND timeframe=:tf
                       AND reversal_label IN (1,2)      -- BUY/SELL
                       AND price_change_after < :thr
                """),
                conn,
                params={"symbol": symbol, "tf": tf, "thr": thr},
            )

            if losers.empty:
                logger.info(f"ℹ️ Нет убыточных меток (pnl<{thr})")
                return 0

            # 2) Существующие HOLD (для идемпотентности)
            existing_holds = set()
            res = conn.execute(
                text("""
                    SELECT extreme_timestamp
                      FROM labeling_results
                     WHERE symbol=:symbol AND timeframe=:tf
                       AND reversal_label=0
                """),
                {"symbol": symbol, "tf": tf},
            )
            for r in res:
                existing_holds.add(int(r[0]))

            new_holds_in_batch = set()

            # 3) Обработка каждого «лузера»
            for _, row in losers.iterrows():
                ts_cur = int(row.extreme_timestamp)

                # 3.1) Обнуляем PnL у самого лузера (как флаг, что правило применено)
                conn.execute(
                    text("""
                        UPDATE labeling_results
                           SET price_change_after = 0.0
                         WHERE symbol=:symbol AND timeframe=:tf
                           AND extreme_timestamp=:ts
                    """),
                    {"symbol": symbol, "tf": tf, "ts": ts_cur},
                )
                updated_count += 1

                # 3.2) Следующий сигнал (граница окна)
                next_sig = conn.execute(
                    text("""
                        SELECT MIN(extreme_timestamp)
                          FROM labeling_results
                         WHERE symbol=:symbol AND timeframe=:tf
                           AND extreme_timestamp > :ts
                    """),
                    {"symbol": symbol, "tf": tf, "ts": ts_cur},
                ).fetchone()

                if not next_sig or not next_sig[0]:
                    # Хвост истории — без следующего сигнала не ставим HOLD (чтобы не смотреть в будущее)
                    continue

                ts_next = int(next_sig[0])

                # 3.3) Список баров между текущим и следующим сигналом (исключая крайние)
                bars = conn.execute(
                    text(f"""
                        SELECT ts
                          FROM {candles_table}
                         WHERE symbol=:symbol
                           AND ts > :ts_cur
                           AND ts < :ts_next
                         ORDER BY ts ASC
                    """),
                    {"symbol": symbol, "ts_cur": ts_cur, "ts_next": ts_next},
                ).fetchall()

                if not bars:
                    # Между сигналами нет ни одной свечи — ставить HOLD бессмысленно
                    continue

                ts_list = [int(b[0]) for b in bars]
                n = len(ts_list)

                # ------------------------ HOLD-END (всегда, если есть место) ------------------------
                ts_end = ts_list[-1]  # последний бар ПЕРЕД следующим сигналом

                def _try_queue_hold(ts_hold: int, method_tag: str):
                    """Локальный помощник: добавить HOLD, если его ещё нет."""
                    if ts_hold in existing_holds or ts_hold in new_holds_in_batch:
                        return False
                    inserted_rows.append({
                        "symbol": symbol,
                        "timestamp": ts_hold,
                        "timeframe": tf,
                        "reversal_label": 0,  # HOLD
                        "reversal_confidence": 1.0,
                        "labeling_method": method_tag,  # 'HOLD_AFTER_LOSS_END' или 'HOLD_AFTER_LOSS_MID'
                        "extreme_timestamp": ts_hold,  # единообразно
                        "price_change_after": 0.0,
                    })
                    new_holds_in_batch.add(ts_hold)
                    return True

                _try_queue_hold(ts_end, "HOLD_AFTER_LOSS_END")

                # ------------------------ HOLD-MID (по условиям) ------------------------
                # Требуем минимальную длину окна, отступы от краёв и минимальную дистанцию до END
                if n >= min_window_bars:
                    # Геометрический центр окна с отступами
                    left = margin_left
                    right = n - 1 - margin_right
                    if left <= right:
                        mid_idx = (left + right) // 2  # середина с учётом margins
                        ts_mid = ts_list[mid_idx]

                        # Дистанция MID → END в барах
                        gap = (n - 1) - mid_idx
                        if gap >= min_mid_end_gap and ts_mid != ts_end:
                            _try_queue_hold(ts_mid, "HOLD_AFTER_LOSS_MID")

            # 4) Пакетная вставка HOLD
            if inserted_rows:
                pd.DataFrame(inserted_rows).to_sql(
                    "labeling_results", conn, if_exists="append", index=False
                )

        logger.info(
            f"✅ Обновлено лузеров: {updated_count} | добавлено HOLD: {len(inserted_rows)} "
            f"(END всегда при наличии окна; MID — при n>={min_window_bars} и gap>={min_mid_end_gap})"
        )
        return updated_count

    def _calculate_unprofitable_hold_ranges(self, df: pd.DataFrame, signals: List[Dict]) -> List[Dict]:
        """
        Диапазоны между последовательными BUY/SELL, где PnL < порога (или <0 — см. условие).
        Возвращает элементы: {start_index, end_index, pnl, signal_type, range_length}
        """
        if not signals or len(signals) < 2:
            return []

        # — фильтруем только реальные сделки и приводим типы
        clean = []
        ts_to_idx = {int(ts): i for i, ts in enumerate(df["ts"].astype(int))}
        for s in signals:
            rl = int(s.get("reversal_label", -1))
            if rl not in (1, 2):
                continue
            idx = s.get("extreme_index")
            if idx is None or not isinstance(idx, (int, np.integer)):
                ts = s.get("extreme_timestamp")
                if ts is None or int(ts) not in ts_to_idx:
                    continue
                idx = ts_to_idx[int(ts)]
            if 0 <= idx < len(df):
                clean.append({"extreme_index": int(idx), "reversal_label": rl})

        if len(clean) < 2:
            return []

        clean.sort(key=lambda x: x["extreme_index"])
        hold_ranges = []

        for i in range(len(clean) - 1):
            start_idx = clean[i]["extreme_index"]
            raw_end = clean[i + 1]["extreme_index"]
            # считаем PnL до последнего БАРА перед следующим сигналом
            end_idx = max(start_idx + 1, raw_end - 1)
            if end_idx <= start_idx or end_idx >= len(df):
                continue

            signal_type = 'BUY' if clean[i]["reversal_label"] == 1 else 'SELL'
            pnl, _ = self._calculate_pnl_to_index(df, start_idx, signal_type, end_idx)

            # выбери одно из условий:
            # if pnl < 0:                                  # строго убыточные
            if pnl < float(self.config.min_profit_target):  # «неприбыльные по порогу»
                hold_ranges.append({
                    "start_index": start_idx,
                    "end_index": raw_end,  # важно: полуинтервал [start, raw_end)
                    "pnl": float(pnl),
                    "signal_type": signal_type,
                    "range_length": raw_end - start_idx
                })

        self.logger.info(f"📊 Найдено {len(hold_ranges)} убыточных диапазонов между сигналами")
        return hold_ranges

    def _run_auto_with_method(self, strategy_func, method_name: str, df: pd.DataFrame = None):
        """Запуск автоматического режима с расчетом PnL до следующей метки"""
        if df is None:
            df = self.load_data()
        signals = strategy_func(df)
        results = []

        # Сортируем сигналы по индексу для поиска следующей метки
        sorted_signals = sorted(signals, key=lambda x: x['index'])

        for i, sig in enumerate(sorted_signals):
            idx = sig['index']

            # Определяем индекс выхода: либо следующая метка, либо hold_bars
            if i + 1 < len(sorted_signals):
                # Есть следующий сигнал - выходим на баре перед ним
                next_idx = sorted_signals[i + 1]['index']
                exit_idx = max(idx + 1, next_idx - 1)  # минимум +1 бар от входа
            else:
                # Последний сигнал - используем hold_bars
                exit_idx = idx + self.config.hold_bars

            # Проверка границ
            if exit_idx >= len(df):
                exit_idx = len(df) - 1

            if exit_idx <= idx:  # защита от некорректных индексов
                continue

            # Расчет PnL до найденного индекса выхода
            pnl, is_profitable = self._calculate_pnl_to_index(df, idx, sig['type'], exit_idx)

            row_dict = df.iloc[idx].to_dict()
            for k, v in row_dict.items():
                if pd.isna(v):
                    row_dict[k] = None
                elif isinstance(v, pd.Timestamp):
                    row_dict[k] = v.isoformat()
                elif isinstance(v, (np.integer, np.int64)):
                    row_dict[k] = int(v)
                elif isinstance(v, (np.floating, np.float64)):
                    row_dict[k] = float(v)
                elif isinstance(v, str):
                    row_dict[k] = v

            timestamp = int(df['ts'].iloc[idx])
            extreme_timestamp = int(sig.get('extreme_timestamp', timestamp))
            confirmation_timestamp = int(sig.get('confirmation_timestamp', timestamp))

            # создаем безопасный словарь параметров без циклических ссылок
            labeling_params = {}
            for k, v in self.config.__dict__.items():
                if k in ['db_engine', 'tool']:  # исключаем несериализуемые объекты
                    continue
                try:
                    # Пробуем сериализовать каждый параметр
                    json.dumps(v)
                    labeling_params[k] = v
                except (TypeError, ValueError):
                    # Если не сериализуется, преобразуем в строку
                    labeling_params[k] = str(v)

            results.append({
                'symbol': self.config.symbol,
                'timestamp': timestamp,
                'timeframe': self.config.timeframe,
                'reversal_label': 1 if sig['type'] == 'BUY' else 2,
                'reversal_confidence': sig['confidence'],
                'labeling_method': method_name,
                'labeling_params': json.dumps(labeling_params),
                'extreme_index': idx,
                'extreme_price': float(df['close'].iloc[idx]),
                'extreme_timestamp': extreme_timestamp,
                'confirmation_index': sig['confirmation_index'],
                'confirmation_timestamp': confirmation_timestamp,
                'price_change_after': pnl,
                'features_json': json.dumps(row_dict),
                'is_high_quality': 1 if is_profitable else 0
            })

        if results:
            self.save_to_db(results)
            profitable_count = sum(1 for r in results if r['is_high_quality'] == 1)
            total_count = len(results)
            print(
                f"✅ Сохранено {total_count} сигналов ({profitable_count} прибыльных, {total_count - profitable_count} убыточных)")
            return len(results)
        else:
            print(f"❌ Сигналы не найдены")
            return 0

    def _run_auto(self, strategy_func, method_name: str):
        """Обертка для совместимости"""
        return self._run_auto_with_method(strategy_func, method_name)

    def enhanced_main_menu(self):
        """Улучшенное главное меню - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        while True:
            print("\n" + "=" * 60)
            print("           ML LABELING TOOL v3 — РАСШИРЕННОЕ МЕНЮ")
            print("=" * 60)

            stats = self.data_loader.get_data_stats()
            print(
                f"📊 Символ: {self.config.symbol} | Свечей: {stats.get('total_candles', 'N/A')} | Меток: {stats.get('total_labels', 'N/A')}")

            print("\n🎯 Режимы разметки:")
            print("[1] PELT Offline - автоподбор penalty (авто)")
            print("[2] CUSUM (авто)")
            print("[3] Min/Max экстремумы (авто)")
            print("[4] CUSUM + Min/Max гибрид (авто)")
            print("[5] Ручная разметка")

            print("\n📈 Анализ и качество:")
            print("[6] Расширенный анализ качества")
            print("[7] Проверка на утечку меток")
            print("[8] Экспорт важности фич")
            print("[9] Кросс-валидация")
            print("[16] Анализ распределения PnL")
            print("[17] Быстрый анализ PnL")

            print("\n⚙️ Управление данными:")
            print("[11] Настройки параметров")
            print("[13] Статистика меток")
            print("[14] Экспорт данных для обучения")
            print("[18] Очистка таблицы меток (текущий символ)")
            print("[19] Очистка таблицы меток по всем символам")
            print("[20] Пометка убыточных диапазонов как негативов")
            print("[21] Устранение дублирования меток")


            print("\n[0] Выход")

            choice = input("\nВаш выбор: ").strip()

            if choice == '1':
                self._run_auto(self._pelt_offline_reversals, "PELT_OFFLINE")
            elif choice == '2':
                self._run_auto(self._cusum_reversals, "CUSUM")
            elif choice == '3':
                self._run_auto(self._extremum_reversals, "EXTREMUM")
            elif choice == '4':
                self._run_auto(self._cusum_extremum_hybrid, "CUSUM_EXTREMUM")
            elif choice == '5':
                self.manual_mode()
            elif choice == '6':
                self.advanced_quality_analysis()
            elif choice == '7':
                self.detect_label_leakage()
            elif choice == '8':
                try:
                    print("\n🏷️ Экспорт важности фич в БД (training_feature_importance)")
                    rid = input("run_id (пусто = последний READY): ").strip() or None
                    model_name = input("Имя модели [unknown]: ").strip() or "unknown"
                    top_n_inp = input("Сохранить top-N (пусто = все): ").strip()
                    top_n = int(top_n_inp) if top_n_inp else None

                    saved = self.export_feature_importance(run_id=rid, model_name=model_name, top_n=top_n)
                    print(f"✅ Сохранено записей важности: {saved}")
                except Exception as err:
                    print(f"❌ Ошибка: {err}")

            elif choice == '9':
                try:
                    n_splits = int(input("Количество фолдов [5]: ") or "5")
                    result = self.cross_validation_split(n_splits=n_splits)
                    if result:
                        print(f"✅ Создано {n_splits} фолдов кросс-валидации")
                except Exception as err:
                    print(f"❌ Ошибка: {err}")

            elif choice == '11':
                self.configure_settings()
            elif choice == '13':
                self.show_stats()
            elif choice == '14':
                try:
                    print("\n📦 Зафиксировать снапшот тренировочного датасета в БД")
                    print("ℹ️  Используются все готовые метки из labeling_results")
                    print(f"   Метод разметки: {self.config.method}")
                    print(f"   Символ: {self.config.symbol}, таймфрейм: {self.config.timeframe}")

                    confirm = input("\n❓ Продолжить создание снапшота? (y/N): ").strip().lower()
                    if confirm != 'y':
                        print("❌ Создание снапшота отменено")
                        continue

                    run_id = self.create_training_snapshot()
                    print(f"✅ Снапшот создан: run_id={run_id}")
                except Exception as err:
                    print(f"❌ Ошибка при экспорте: {err}")
            elif choice == '16':
                try:
                    method = input("Метод для анализа [опционально]: ").strip()
                    if method:
                        self.analyze_pnl_distribution(method=method)
                    else:
                        self.analyze_pnl_distribution()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '17':
                try:
                    self.quick_pnl_analysis()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '18':
                try:
                    self.clear_labeling_table()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '19':
                try:
                    self.clear_all_labeling_tables()
                except Exception as err:
                    print(f"❌ Ошибка: {err}")
            elif choice == '20':
                    try:
                        count = self.mark_unprofitable_ranges_as_negatives()
                        print(f"✅ Проставлены HOLD-метки: {count}")
                    except Exception as err:
                        print(f"❌ Ошибка: {err}")
            elif choice == '21':
                try:
                    count = self.merge_conflicting_labels()
                    print(f"✅ Объединено конфликтных меток: {count}")
                except Exception as err:
                    print(f"❌ Ошибка: {err}")

            elif choice == '0':
                print("👋 До свидания!")
                break
            else:
                print("❌ Неверный выбор")

    def _expand_hold_ranges(self, labels_df: pd.DataFrame, all_timestamps: set) -> dict:
        """
        ВЕРСИЯ БЕЗ РАСШИРЕНИЯ HOLD.
        Возвращает только исходные метки по исходным ts, без заливки промежутков.
        Надёжно работает при дубликатах колонок и нескольких строках на один ts.
        """

        if labels_df is None or labels_df.empty:
            return {}

        # 1) убрать дубликаты колонок, сохранить первый столбец с данным именем
        labels_df = labels_df.loc[:, ~labels_df.columns.duplicated()].copy()

        # 2) найти колонки 'ts' и 'reversal_label' (без учета регистра)
        cols_lower = {c.lower(): c for c in labels_df.columns}
        ts_col = cols_lower.get('ts')
        lab_col = cols_lower.get('reversal_label')

        if ts_col is None or lab_col is None:
            self.logger.warning("Нет необходимых колонок 'ts' и/или 'reversal_label' в labels_df")
            return {}

        df = labels_df[[ts_col, lab_col]].copy()

        # 3) привести к числовым типам; выбросить NaN/нечисловые
        df[ts_col] = pd.to_numeric(df[ts_col], errors='coerce')
        df[lab_col] = pd.to_numeric(df[lab_col], errors='coerce')
        df = df.dropna(subset=[ts_col, lab_col])

        # 4) привести к int (после dropna безопасно)
        df[ts_col] = df[ts_col].astype(np.int64)
        df[lab_col] = df[lab_col].astype(np.int64)

        # 5) оставить только ts, которые есть в all_timestamps (если заданы)
        if all_timestamps:
            # all_timestamps может быть set(int) — ок
            df = df[df[ts_col].isin(all_timestamps)]

        if df.empty:
            return {}

        # 6) если по одному ts несколько меток — берём ПОСЛЕДНЮЮ (по порядку строк)
        # (Если нужно первую — замените .last() на .first())
        df = df.groupby(ts_col, as_index=False)[lab_col].last()

        # 7) вернуть как dict{ts:int -> label:int}
        return dict(zip(df[ts_col].tolist(), df[lab_col].tolist()))

    def _build_training_snapshot_dataframe(self):
        import pandas as pd
        from sqlalchemy import text
        market_df = self.data_loader.load_indicators()
        if market_df is None or market_df.empty:
            raise RuntimeError("Пустые рыночные данные")
        if "ts" not in market_df.columns:
            raise RuntimeError("В market_df отсутствует колонка 'ts'")
        logger.info(f"✅ Загружено {len(market_df)} свечей с индикаторами")
        with self.engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT extreme_timestamp AS ts, reversal_label
                FROM labeling_results
                WHERE symbol=:symbol AND timeframe=:timeframe AND reversal_label IN (0,1,2)
                ORDER BY extreme_timestamp
            """), {"symbol": self.config.symbol, "timeframe": self.config.timeframe}).fetchall()
        if not rows:
            raise RuntimeError("Нет меток в labeling_results")
        labels_df = pd.DataFrame(rows, columns=["ts", "reversal_label"])
        logger.info(
            f"✅ Загружено {len(labels_df)} меток (HOLD: {(labels_df['reversal_label'] == 0).sum()}, BUY: {(labels_df['reversal_label'] == 1).sum()}, SELL: {(labels_df['reversal_label'] == 2).sum()})")
        all_timestamps = set(market_df['ts'].values)
        expanded_labels = self._expand_hold_ranges(labels_df, all_timestamps)
        hold_count = sum(1 for l in expanded_labels.values() if l == 0)
        logger.info(f"✅ После расширения HOLD: {hold_count}")
        market_df['reversal_label'] = market_df['ts'].map(expanded_labels).fillna(-1).astype(int)
        market_df['reversal_label'] = market_df['reversal_label'].replace({-1: 0, 0: 0, 1: 1, 2: 2})
        class_counts_before = market_df['reversal_label'].value_counts().to_dict()
        logger.info(
            f"   ДО downsample: NO_SIGNAL={class_counts_before.get(0, 0)}, BUY={class_counts_before.get(1, 0)}, SELL={class_counts_before.get(2, 0)}, HOLD={class_counts_before.get(3, 0)}")
        no_signal = market_df[market_df['reversal_label'] == 0]
        signals = market_df[market_df['reversal_label'] != 0]
        n_no_signal = 10000
        no_signal_sample = no_signal.sample(n=min(n_no_signal, len(no_signal)), random_state=42)
        logger.info(f"✅ Downsample NO_SIGNAL: {len(no_signal)} → {len(no_signal_sample)}")

        # Разделяем signals на HOLD и BUY/SELL
        hold_signals = signals[signals['reversal_label'] == 3]
        trade_signals = signals[signals['reversal_label'].isin([1, 2])]

        # Downsample HOLD до размера BUY+SELL
        n_hold = len(trade_signals)
        hold_sample = hold_signals.sample(n=min(n_hold, len(hold_signals)), random_state=42) if len(
            hold_signals) > 0 else hold_signals
        logger.info(f"✅ Downsample HOLD: {len(hold_signals)} → {len(hold_sample)}")

        dataset_df = pd.concat([no_signal_sample, hold_sample, trade_signals], ignore_index=True).sort_values(
            'ts').reset_index(drop=True)
        class_counts_after = dataset_df['reversal_label'].value_counts().to_dict()
        total = len(dataset_df)
        logger.info(
            f"✅ Финальный датасет: {total} (NO_SIGNAL={class_counts_after.get(0, 0)}, BUY={class_counts_after.get(1, 0)}, SELL={class_counts_after.get(2, 0)}, HOLD={class_counts_after.get(3, 0)})")
        max_count = max(class_counts_after.values())
        weights_map = {label: max_count / class_counts_after.get(label, 1) for label in [0, 1, 2]}
        dataset_df['sample_weight'] = dataset_df['reversal_label'].map(weights_map)
        allowed_columns = ['ts', 'reversal_label', 'sample_weight', 'datetime', 'cmo_14', 'volume', 'trend_acceleration_ema7',
                           'regime_volatility', 'bb_width', 'adx_14', 'plus_di_14','minus_di_14', 'atr_14_normalized',
                           'volume_ratio_ema3', 'candle_relative_body', 'upper_shadow_ratio', 'lower_shadow_ratio',
                           'price_vs_vwap', 'bb_position', 'cusum_1m_recent', 'cusum_1m_quality_score',
                           'cusum_1m_trend_aligned', 'cusum_1m_price_move', 'is_trend_pattern_1m',
                           'body_to_range_ratio_1m', 'close_position_in_range_1m']
        dataset_df = dataset_df[[c for c in allowed_columns if c in dataset_df.columns]]
        dataset_df['symbol'] = self.config.symbol
        dataset_df['run_id'] = None
        dataset_df['timeframe'] = self.config.timeframe
        dataset_df['created_at'] = None
        meta_info = {
            "class_dist": {"no_signal": int(class_counts_after.get(0, 0)), "buy": int(class_counts_after.get(1, 0)),
                           "sell": int(class_counts_after.get(2, 0)), "hold": int(class_counts_after.get(3, 0)),
                           "total": total}, "buffer_bars": getattr(self.config, "buffer_bars", None),
            "seed": getattr(self.config, "seed", None),
            "config_json": {"method": self.config.method, "timeframe": self.config.timeframe,
                            "symbol": self.config.symbol}, "issues": {}}
        return dataset_df, meta_info

    def create_training_snapshot(self, run_id: str | None = None) -> str:
        """
        Формирует БД-снапшот тренировочного датасета:
          - гарантирует наличие таблиц snapshot (DDL)
          - пишет meta(status=CREATING)
          - собирает датасет (позитивы/негативы, anti_trade_mask, sample_weight)
          - валидирует данные
          - пишет строки в training_dataset и агрегаты в training_dataset_meta
          - переключает meta(status=READY)
        Возвращает run_id.
        """

        self._ensure_training_snapshot_tables()

        # Параметры и run_id
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not run_id:
            payload = {
                "symbol": self.config.symbol,
                "timeframe": self.config.timeframe,
                "method": self.config.method,
            }
            sh = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:8]
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
            run_id = f"{self.config.symbol.replace('/', '_')}_{self.config.timeframe}_{stamp}_{sh}"
        # META: status=CREATING
        meta_defaults = {
            "run_id": run_id,
            "status": "CREATING",
            "error_msg": None,
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "created_at": created_at
        }
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO training_dataset_meta(run_id,status,error_msg,symbol,timeframe,created_at)
                VALUES (:run_id,:status,:error_msg,:symbol,:timeframe,:created_at)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, error_msg=NULL
            """), meta_defaults)

        try:
            # 1) Сборка датасета (ваш существующий конвейер)
            dataset_df, meta_info = self._build_training_snapshot_dataframe()

            # 2) Валидация/очистка
            try:
                df_clean, issues, nan_drop_rows, duplicates_count = self._validate_snapshot_frame(dataset_df)
            except Exception as e:
                logger.info("Snapshot validation failed: %s", e)
                raise

            # 3) Диапазон дат и метрики
            range_start_ts = int(df_clean["ts"].min()) if len(df_clean) else None
            range_end_ts = int(df_clean["ts"].max()) if len(df_clean) else None

            if "anti_trade_mask" in df_clean.columns and len(df_clean) > 0:
                try:
                    issues["anti_trade_coverage"] = float(df_clean["anti_trade_mask"].mean())
                except Exception:
                    pass

            rows_total = int(len(df_clean))
            pos_count = int((df_clean["reversal_label"].isin([1, 2])).sum()) if rows_total else 0
            hold_bars = int((df_clean["reversal_label"] == 0).sum()) if rows_total else 0
            neg_count = 0  # по новой логике негативы не генерируем

            # 4) Запись строк snapshot
            df_clean = df_clean.copy()
            df_clean["run_id"] = run_id
            df_clean["symbol"] = self.config.symbol
            df_clean["timeframe"] = self.config.timeframe
            df_clean["created_at"] = created_at

            # Колонки для новой структуры (29 колонок)
            required_columns = [
                'run_id', 'symbol', 'timeframe', 'ts', 'datetime', 'reversal_label', 'sample_weight',
                'cmo_14','volume', 'trend_acceleration_ema7', 'regime_volatility', 'bb_width', 'adx_14',
                'plus_di_14', 'minus_di_14', 'atr_14_normalized', 'volume_ratio_ema3', 'candle_relative_body',
                'upper_shadow_ratio', 'lower_shadow_ratio', 'price_vs_vwap', 'bb_position',
                'cusum_1m_recent', 'cusum_1m_quality_score', 'cusum_1m_trend_aligned',
                'cusum_1m_price_move', 'is_trend_pattern_1m', 'body_to_range_ratio_1m',
                'close_position_in_range_1m', 'created_at'
            ]

            # Оставляем только колонки из DDL
            final_columns = [col for col in required_columns if col in df_clean.columns]
            df_for_db = df_clean[final_columns].copy()

            # batch insert
            df_for_db.to_sql("training_dataset", self.engine, if_exists="append", index=False)

            # 5) Обновление META (READY) — без featureset/source_hash/feature_names
            meta_payload = {
                "run_id": run_id,
                "status": "READY",
                "error_msg": None,
                "rows_total": rows_total,
                "pos_count": pos_count,
                "neg_count": neg_count,
                "hold_bars": hold_bars,
                "class_dist_json": json.dumps(meta_info.get("class_dist", {})),
                "buffer_bars": meta_info.get("buffer_bars"),
                "seed": meta_info.get("seed"),
                "labeling_method": self.config.method,
                "config_json": json.dumps(meta_info.get("config_json", {})),
                "nan_drop_rows": int(nan_drop_rows),
                "issues_json": json.dumps(issues, ensure_ascii=False),
                "range_start_ts": range_start_ts,
                "range_end_ts": range_end_ts,
            }
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE training_dataset_meta
                       SET status=:status,
                           error_msg=:error_msg,
                           rows_total=:rows_total,
                           pos_count=:pos_count,
                           neg_count=:neg_count,
                           class_dist_json=:class_dist_json,
                           hold_bars=:hold_bars,
                           buffer_bars=:buffer_bars,
                           seed=:seed,
                           labeling_method=:labeling_method,
                           config_json=:config_json,
                           nan_drop_rows=:nan_drop_rows,
                           issues_json=:issues_json,
                           range_start_ts=:range_start_ts,
                           range_end_ts=:range_end_ts
                     WHERE run_id=:run_id
                """), meta_payload)

            logger.info(
                "✅ Snapshot READY | run_id=%s | rows=%s | pos=%s | neg=%s | anti_trade=%s | range=[%s..%s] | nan_drop=%s",
                run_id, rows_total, pos_count, neg_count,
                (f"{issues.get('anti_trade_coverage'):.4f}" if isinstance(issues.get("anti_trade_coverage"),
                                                                          float) else "N/A"),
                range_start_ts, range_end_ts, nan_drop_rows
            )
            return run_id

        except Exception as e:
            err = str(e)
            logger.info("❌ Ошибка формирования снапшота: %s", err)
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE training_dataset_meta
                       SET status='FAILED', error_msg=:err
                     WHERE run_id=:run_id
                """), {"run_id": run_id, "err": err})
            raise

    def close(self):
        """Корректно закрывает соединения и освобождает ресурсы."""
        try:
            if hasattr(self, "data_loader") and hasattr(self.data_loader, "db_engine"):
                self.data_loader.db_engine.dispose()
                logger.info("🔌 SQLAlchemy engine закрыт.")
            if hasattr(self, "engine"):
                self.engine.dispose()
                logger.info("🧹 Подключение к базе данных закрыто.")
        except Exception as err:
            logger.warning(f"⚠️ Ошибка при закрытии соединений: {err}")


# === ЗАПУСК ===
if __name__ == '__main__':
    tool = None
    try:
        # ИСПОЛЬЗУЕМ НАСТРОЙКИ ПО УМОЛЧАНИЮ ИЗ LabelingConfig
        # без дублирования параметров
        config = LabelingConfig(
            symbol="ETHUSDT",
            # Все остальные параметры берутся из defaults класса LabelingConfig
        )

        tool = AdvancedLabelingTool(config)
        tool.enhanced_main_menu()

    except KeyboardInterrupt:
        print("\n👋 Прервано пользователем.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")

        logger.error(f"Детали: {traceback.format_exc()}")
        sys.exit(1)
    finally:
        if tool is not None:
            tool.close()
# train_ml_global_v2.py
"""
Обучение LightGBM модели с использованием данных из ml_labeling_tool_v3.py
и индикаторов из MarketDataUtils

Автор: pwm777
Дата: 2025-10-25 (обновлено)
Версия: 2.0 (интеграция с labeling tool)

Описание:
- Использует SQLite базу данных из ml_labeling_tool_v3.py
- Все признаки рассчитываются по тем же формулам, что и в боевом боте
- Автоматическая загрузка данных из market_data.sqlite
- Поддержка балансировки датасета как в labeling tool

Запуск:
    python train_ml_global_v2.py
"""

import sys
import logging
from sqlalchemy import create_engine, text
from datetime import datetime
import json
from typing import Tuple
import warnings
import lightgbm as lgb
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
from collections import Counter
import joblib
from pathlib import Path

warnings.filterwarnings('ignore')
import re
import os, numpy as np, pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.preprocessing import label_binarize, StandardScaler
from sklearn.metrics import precision_recall_curve, average_precision_score

TIMEFRAME_TO_BARS = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24}

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MARKET_DB_DSN: str = f"sqlite:///{DATA_DIR}/market_data.sqlite"
IGNORE_CLASS_3 = False  # ← отключаем: класс 3 объединяется с 0
MERGE_CLASS_3_TO_0 = True  # ← активный режим: 3→0
# ──────────────────────────────────────────────────────────────
# СПИСОК ПРИЗНАКОВ (совместимый с labeling tool)
# ──────────────────────────────────────────────────────────────
feature_names = [
    'cmo_14',
    'volume',
    'trend_acceleration_ema7',
    'regime_volatility',
    'bb_width',
    'adx_14',
    'plus_di_14',  # ← ДОБАВЛЕН
    'minus_di_14',
    'atr_14_normalized',
    'volume_ratio_ema3',
    'candle_relative_body',
    'upper_shadow_ratio',
    'lower_shadow_ratio',
    'price_vs_vwap',
    'bb_position',
    'cusum_1m_recent',
    'cusum_1m_quality_score',
    'cusum_1m_trend_aligned',
    'cusum_1m_price_move',
    'is_trend_pattern_1m',
    'body_to_range_ratio_1m',
    'close_position_in_range_1m'
]


# ──────────────────────────────────────────────────────────────
# КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ
# ──────────────────────────────────────────────────────────────
def _infer_bars_per_day_from_run_id(run_id: str, default: int = 288) -> int:
    """
    Пытается вытащить таймфрейм из run_id формата ..._<tf>_...
    и вернуть соответствующее количество баров в сутках.
    """
    m = re.search(r"_(\d+[mh])_", str(run_id).lower())
    if not m:
        return default
    tf = m.group(1)
    TIMEFRAME_TO_BARS = {
        "1m": 1440,
        "3m": 480,
        "5m": 288,
        "15m": 96,
        "30m": 48,
        "1h": 24,
        "2h": 12,
        "4h": 6,
        "6h": 4,
        "12h": 2,
        "1d": 1,
    }
    return TIMEFRAME_TO_BARS.get(tf, default)


class DataLoader:
    """Загрузка данных из SQLite базы ml_labeling_tool_v2.py"""

    def __init__(self, db_dsn: str = MARKET_DB_DSN, symbol: str = "ETHUSDT"):
        self.db_dsn = db_dsn
        self.db_path = DATA_DIR / "market_data.sqlite"
        self.symbol = symbol
        self.engine = None

    def connect(self):
        """Установка соединения с БД"""
        if not self.db_path.exists():
            raise FileNotFoundError(f"База данных не найдена: {self.db_path}")
        self.engine = create_engine(self.db_dsn)
        logger.info(f"✅ Подключено к БД: {self.db_path}")

    def close(self):
        """Закрытие соединения"""
        if self.engine:
            self.engine.dispose()
            logger.info("✅ Соединение с БД закрыто")

    def load_market_data(self) -> pd.DataFrame:
        """Загрузка свечных данных из candles_5m"""
        if not self.engine:
            self.connect()

        query = text("""
            SELECT * FROM candles_5m 
            WHERE symbol = :symbol 
            ORDER BY ts
        """)

        with self.engine.connect() as conn:
            df = pd.read_sql_query(query, conn, params={"symbol": self.symbol})

        if df.empty:
            raise ValueError(f"Нет данных для символа {self.symbol}")

        # Преобразование времени
        if 'ts' in df.columns and 'datetime' not in df.columns:
            df['datetime'] = pd.to_datetime(df['ts'], unit='ms')

        logger.info(f"✅ Загружено {len(df)} свечей из candles_5m")
        return df

    def load_training_dataset(self, run_id: str) -> pd.DataFrame:
        """Загрузка готового датасета из training_dataset"""
        if not self.engine:
            self.connect()

        query = text("""
            SELECT * FROM training_dataset
            WHERE run_id = :run_id
            ORDER BY ts
        """)

        with self.engine.connect() as conn:
            df = pd.read_sql_query(query, conn, params={"run_id": run_id})

        if df.empty:
            raise ValueError(f"❌ Нет данных для run_id={run_id}")

        logger.info(f"✅ Загружено {len(df)} образцов из training_dataset")
        logger.info(f"   Классы: {df['reversal_label'].value_counts().to_dict()}")
        return df


# ──────────────────────────────────────────────────────────────
# КОЛЛБЭК «ТЕРМОМЕТР ПРОГРЕССА» ДЛЯ LIGHTGBM
# ──────────────────────────────────────────────────────────────

def thermometer_progress_callback(logger: logging.Logger, width: int = 30, period: int = 10):
    """Прогресс-бар по итерациям бустинга"""
    import sys

    def _cb(env):
        begin = getattr(env, 'begin_iteration', 0) or 0
        end = getattr(env, 'end_iteration', None)
        if end is None or end <= begin:
            end = begin + int(env.params.get('num_boost_round', 0) or 0)
            if end <= begin:
                end = begin + 1

        total = max(1, end - begin)
        iter_now = int(getattr(env, 'iteration', 0) or 0)
        done = max(0, iter_now - begin + 1)
        pct = min(1.0, max(0.0, done / total))
        filled = int(round(pct * width))
        bar = '█' * filled + '░' * (width - filled)

        # Метрики валидации
        val_metric_name = None
        val_metric_value = None
        evals = getattr(env, 'evaluation_result_list', None)
        if evals:
            for res in evals:
                if isinstance(res, (list, tuple)) and len(res) >= 3:
                    data_name, metric_name, metric_val = res[0], res[1], res[2]
                    if str(data_name).startswith(('valid', 'val')):
                        val_metric_name = str(metric_name)
                        try:
                            val_metric_value = float(metric_val)
                        except Exception:
                            val_metric_value = None
                        break

        should_print = (iter_now > 0 and iter_now % period == 0) or (done >= total)

        if should_print:
            if val_metric_name is not None and val_metric_value is not None:
                msg = f"[{iter_now:4d}/{total}] {val_metric_name}:{val_metric_value:.5f} | {bar} {int(pct * 100):3d}%"
            else:
                msg = f"[{iter_now:4d}/{total}] {bar} {int(pct * 100):3d}%"

            if done >= total:
                print(f"\r{msg}")
                sys.stdout.flush()
            else:
                print(f"\r{msg}", end='', flush=True)

    return _cb


# ──────────────────────────────────────────────────────────────
# ГЛАВНЫЙ КЛАСС ModelTrainer
# ──────────────────────────────────────────────────────────────

class ModelTrainer:
    def __init__(self, db_dsn: str, symbol: str):
        self.db_dsn = db_dsn
        self.symbol = symbol
        self.data_loader = DataLoader(db_dsn, symbol)
        self.feature_names = feature_names

    def prepare_training_data(self, run_id: str) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
        """Подготовка данных из training_dataset"""
        df = self.data_loader.load_training_dataset(run_id)

        # Фичи уже в датасете
        X = df[self.feature_names]
        y = df['reversal_label']
        w = df['sample_weight']

        # Проверка пропусков
        missing = X.isnull().sum()
        if missing.any():
            logger.warning(f"⚠️  Обнаружены пропуски:\n{missing[missing > 0]}")
            X = X.fillna(0)

        logger.info(f"✅ Подготовлены данные: {len(X)} примеров, {len(self.feature_names)} признаков")
        logger.info(f"   Распределение классов: {y.value_counts().to_dict()}")

        return X, y, w

    def tune_tau_for_spd_range(
            self,
            y_val: np.ndarray,
            proba: np.ndarray,  # shape (n,3) — [HOLD, BUY, SELL]
            bars_per_day: int,
            spd_min: float = 30.0,
            spd_max: float = 50.0,
            precision_min: float = 0.60,
            delta: float = 0.08,
            cooldown_bars: int = 2,
    ):
        """
        Подбирает tau (порог max(p_buy,p_sell)) так, чтобы SPD был в [spd_min, spd_max].
        Возвращает (tau, stats_dict). Если не найдено — берёт tau с SPD ближайшим к центру диапазона.
        """
        # предрасчёты
        p_buy, p_sell = proba[:, 1], proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        taus = np.quantile(maxp, np.linspace(0.30, 0.95, 48))

        best_in = None  # (cand, key)  — лучший внутри диапазона
        best_near = None  # (cand, keyn) — ближайший к центру диапазона
        target = 0.5 * (spd_min + spd_max)
        n = len(y_val)

        for tau_cand in sorted(taus):
            # единый расчёт метрик/act через helper
            stats = self._eval_decision_metrics(
                y_true=np.asarray(y_val),
                proba=np.asarray(proba),
                tau=float(tau_cand),
                delta=float(delta),
                cooldown_bars=int(cooldown_bars),
                bars_per_day=int(bars_per_day),
            )
            spd = stats['spd']
            prec = stats['precision_macro_buy_sell']
            rec = stats['recall_macro_buy_sell']
            f1 = stats['f1_macro_buy_sell']
            # восстановим количество сигналов из SPD (после cooldown)
            signals = int(round(spd * max(1, n) / max(1, bars_per_day)))

            cand = (float(tau_cand), float(spd), float(prec), float(rec), float(f1), int(signals))

            # внутри диапазона — максимизируем precision, затем F1, затем ближе к центру, затем больший τ
            if spd_min <= spd <= spd_max and prec >= precision_min:
                key = (prec, f1, -abs(spd - target), float(tau_cand))
                if (best_in is None) or (key > best_in[1]):
                    best_in = (cand, key)

            # ближайший к центру — на случай, если в диапазоне нет подходящих
            gap = abs(spd - target)
            keyn = (-gap, prec, f1)  # минимизируем gap, затем макс. precision/F1
            if (best_near is None) or (keyn > best_near[1]):
                best_near = (cand, keyn)

        # основной выбор
        chosen = (best_in[0] if best_in is not None else best_near[0])
        tau_chosen, spd, prec, rec, f1, signals = chosen

        # ── Локальный подъём τ вверх (если не хуже и остаёмся в текущем окне SPD) ──
        upper = min(float(tau_chosen) + 0.05, 0.999)
        ref_grid = np.linspace(float(tau_chosen), upper, 31)  # шаг ≈0.0017

        best_ref = None  # (key_ref, t, stats_ref)
        for t in ref_grid:
            stats_ref = self._eval_decision_metrics(
                y_true=np.asarray(y_val),
                proba=np.asarray(proba),
                tau=float(t),
                delta=float(delta),
                cooldown_bars=int(cooldown_bars),
                bars_per_day=int(bars_per_day),
            )
            spd_r = stats_ref['spd']
            prec_r = stats_ref['precision_macro_buy_sell']
            f1_r = stats_ref['f1_macro_buy_sell']

            # приоритет качества; окно SPD используем текущее (spd_min..spd_max) и текущий precision_min
            if (spd_min <= spd_r <= spd_max) and (prec_r >= precision_min):
                # ключ: макс F1, затем больший τ, затем ближе к центру SPD
                key_ref = (f1_r, float(t), -abs(spd_r - target))
                if (best_ref is None) or (key_ref > best_ref[0]):
                    best_ref = (key_ref, float(t), stats_ref)

        # применяем улучшение, если найдено
        if best_ref is not None:
            _, tau_new, sref = best_ref
            tau_chosen = tau_new
            spd = float(sref['spd'])
            prec = float(sref['precision_macro_buy_sell'])
            rec = float(sref['recall_macro_buy_sell'])
            f1 = float(sref['f1_macro_buy_sell'])
            signals = int(round(spd * max(1, len(y_val)) / max(1, bars_per_day)))

        # hit_range должен отражать факт попадания ИТОГОВОГО выбора в окно и по precision
        in_range = (spd_min <= spd <= spd_max) and (prec >= precision_min)

        return float(tau_chosen), {
            "spd": float(spd),
            "precision_macro_buy_sell": float(prec),
            "recall_macro_buy_sell": float(rec),
            "f1_macro_buy_sell": float(f1),
            "signals": int(signals),
            "delta": float(delta),
            "cooldown_bars": int(cooldown_bars),
            "range": [float(spd_min), float(spd_max)],
            "hit_range": bool(in_range),
        }

    @staticmethod
    def _eval_decision_metrics(y_true: np.ndarray,
                               proba: np.ndarray,  # shape (n,3) [HOLD, BUY, SELL]
                               tau: float,
                               delta: float,
                               cooldown_bars: int,
                               bars_per_day: int) -> dict:
        """
        Унифицированный расчёт act/предсказаний/метрик, используемый и в тюнере, и в sensitivity.
        ВАЖНО: метрики считаются ТОЛЬКО на индексе act=True (как в тюнере), labels=[1,2].
        """
        p_buy = proba[:, 1]
        p_sell = proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        margin = np.abs(p_buy - p_sell)

        act = (maxp >= tau) & (margin >= delta)

        # cooldown по индексам срабатываний
        idx = np.where(act)[0]
        if idx.size > 0:
            keep = [idx[0]]
            for i in idx[1:]:
                if i - keep[-1] >= cooldown_bars:
                    keep.append(i)
            sel = np.zeros_like(act, dtype=bool)
            sel[np.array(keep, dtype=int)] = True
            act = sel

        # предсказания 0/1/2 по политике
        pred = np.zeros(len(proba), dtype=int)  # HOLD=0
        buy_ge_sell = p_buy >= p_sell
        pred[act & buy_ge_sell] = 1
        pred[act & (~buy_ge_sell)] = 2

        # SPD
        spd_val = act.sum() * bars_per_day / max(1, len(y_true))

        # метрики на активном подмножестве (как в тюнере)
        if np.any(act):
            pm, rm, fm, _ = precision_recall_fscore_support(
                y_true[act], pred[act], labels=[1, 2], average='macro', zero_division=0
            )
        else:
            pm = rm = fm = 0.0

        return {
            'spd': float(spd_val),
            'precision_macro_buy_sell': float(pm),
            'recall_macro_buy_sell': float(rm),
            'f1_macro_buy_sell': float(fm),
            # дублируем для удобства
            'tau': float(tau),
            'delta': float(delta),
            'cooldown_bars': int(cooldown_bars),
        }

    @staticmethod
    def decide(proba, tau, delta=0.08, cooldown_bars=2):
        p_buy, p_sell = proba[:, 1], proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        margin = np.abs(p_buy - p_sell)
        act = (maxp >= tau) & (margin >= delta)

        # Apply cooldown
        idx = np.where(act)[0]
        if idx.size > 0:
            keep = [idx[0]]
            for i in idx[1:]:
                if i - keep[-1] >= cooldown_bars:
                    keep.append(i)
            sel = np.zeros_like(act, dtype=bool)
            sel[np.array(keep, dtype=int)] = True
            act = sel

        pred = np.zeros(len(proba), dtype=int)
        pred[act] = np.where(p_buy[act] >= p_sell[act], 1, 2)
        return pred

    def train_model(self, run_id: str, use_scaler: bool = True) -> dict:
        """Обучение модели (вариант A: игнорируем класс 3 полностью)"""

        logger.info("\n" + "=" * 60)
        logger.info("ОБУЧЕНИЕ МОДЕЛИ LIGHTGBM")
        logger.info("=" * 60)

        # Подготовка данных
        X, y, w = self.prepare_training_data(run_id)

        # Разделение на train/val по времени (80/20)
        split_idx = int(len(X) * 0.8)
        X_train, X_val = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_val = y.iloc[:split_idx], y.iloc[split_idx:]
        w_train, w_val = w.iloc[:split_idx], w.iloc[split_idx:]

        # --- Полностью исключаем служебный класс 3 из обучения и валидации ---
        mask_tr = (y_train != 3)
        mask_va = (y_val != 3)

        X_train = X_train[mask_tr]
        y_train = y_train[mask_tr]
        w_train = w_train[mask_tr]

        X_val = X_val[mask_va]
        y_val = y_val[mask_va]
        w_val = w_val[mask_va]

        NUM_CLASS = 3
        REPORT_LABELS = [1, 2, 0]  # Порядок меток для отчёта: BUY, SELL, HOLD(0)
        REPORT_NAMES = ['BUY', 'SELL', 'HOLD']

        logger.info("⚖️  Используем веса из training_dataset")

        # Датасеты LightGBM
        train_data = lgb.Dataset(X_train, label=y_train, weight=w_train)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

        # ═══════════════════════════════════════════════════════════
        # SCALER (ОПЦИОНАЛЬНО)
        # ═══════════════════════════════════════════════════════════
        scaler = None
        if use_scaler:
            logger.info("📊 Создание StandardScaler и нормализация данных...")
            scaler = StandardScaler()
            scaler.fit(X_train)

            # Применяем нормализацию
            X_train_scaled = scaler.transform(X_train)
            X_val_scaled = scaler.transform(X_val)

            # Пересоздаём датасеты с нормализованными данными
            train_data = lgb.Dataset(X_train_scaled, label=y_train, weight=w_train)
            val_data = lgb.Dataset(X_val_scaled, label=y_val, reference=train_data)

            logger.info(f"✅ Scaler обучен и применен на {len(X_train)} образцах")
        else:
            logger.info("⚠️  Scaler отключен - обучение на RAW признаках")
            # Датасеты уже созданы выше с исходными данными

        # Параметры модели (3 класса)
        params = {
            'objective': 'multiclass',
            'num_class': NUM_CLASS,
            'metric': 'multi_logloss',
            'boosting_type': 'gbdt',
            'num_leaves': 20,
            'learning_rate': 0.02,
            'feature_fraction': 0.75,
            'bagging_fraction': 0.75,
            'bagging_freq': 5,
            'verbose': -1,
            'min_child_samples': 8,
            'max_depth': 6,
            'lambda_l1': 0.5,
            'lambda_l2': 0.5,
            'min_gain_to_split': 0.05,
            'boost_from_average': False,
            'seed': 42,
            'bagging_seed': 42,
            'feature_fraction_seed': 42,
        }

        # Обучение
        model = lgb.train(
            params,
            train_data,
            valid_sets=[val_data],
            valid_names=['valid_0'],
            num_boost_round=1000,
            callbacks=[
                thermometer_progress_callback(logger, width=30, period=10),
                lgb.early_stopping(stopping_rounds=100, first_metric_only=True),
            ],
        )

        # Предсказания и метрики (argmax по вероятностям)
        if use_scaler and scaler is not None:
            y_val_pred_proba = model.predict(X_val_scaled)
        else:
            y_val_pred_proba = model.predict(X_val)
        y_val_pred = y_val_pred_proba.argmax(axis=1) if getattr(y_val_pred_proba, "ndim", 1) == 2 else y_val_pred_proba

        # ──────────────────────────────────────────────────────────────
        # 🔍 Диагностика частоты сигналов и подбор порогов
        # ──────────────────────────────────────────────────────────────
        TF2BARS = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24}
        tf = str(getattr(self, "timeframe", "5m")).lower()
        bars_per_day = TF2BARS.get(tf, 288)

        # Перебор precision_min 0.55–0.75 (шаг 0.05)
        precision_grid = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.9]
        candidates = []
        for pm in precision_grid:
            try:
                tau_i, tstats_i = self.tune_tau_for_spd_range(
                    y_val=np.asarray(y_val),
                    proba=np.asarray(y_val_pred_proba),  # shape (n, 3)
                    bars_per_day=bars_per_day,
                    spd_min=4.0,
                    spd_max=10.0,
                    precision_min=pm,
                    delta=0.08,
                    cooldown_bars=2,
                )
                candidates.append({
                    'precision_min': pm,
                    'tau': float(tau_i),
                    'spd': float(tstats_i.get('spd', float('nan'))),
                    'precision_macro_buy_sell': float(tstats_i.get('precision_macro_buy_sell', float('nan'))),
                    'f1_macro_buy_sell': float(tstats_i.get('f1_macro_buy_sell', float('nan'))),
                    'hit_range': bool(tstats_i.get('hit_range', False)),
                    'delta': float(tstats_i.get('delta', 0.08)),
                    'cooldown_bars': int(tstats_i.get('cooldown_bars', 2)),
                    '_tstats': tstats_i,
                })
            except Exception as e:
                logging.warning(f"precision_min={pm:.2f}: sweep failed with error: {e}")

        def _key(c):
            # 1) hit_range=True; 2) max precision; 3) max f1; 4) min tau
            return (1 if c.get('hit_range') else 0,
                    c.get('precision_macro_buy_sell', float('-inf')),
                    c.get('f1_macro_buy_sell', float('-inf')),
                    -c.get('tau', float('inf')))

        if not candidates:
            raise RuntimeError("Precision sweep failed: no candidates collected")

        best = max(candidates, key=_key)
        tau = best['tau']
        tstats = best['_tstats']
        delta = best.get('delta', 0.08)
        cooldown_bars = best.get('cooldown_bars', 2)

        logging.info("🔧 Precision sweep results (pm, tau, spd≈, prec≈, f1≈, hit):")
        for c in candidates:
            logging.info(f"  pm={c['precision_min']:.2f}, tau={c['tau']:.3f}, spd≈{c['spd']:.1f}, "
                         f"prec≈{c['precision_macro_buy_sell']:.3f}, f1≈{c['f1_macro_buy_sell']:.3f}, "
                         f"hit={c['hit_range']}")
        logging.info(f"✅ Picked precision_min={best['precision_min']:.2f} → "
                     f"tau={tau:.3f}, spd≈{best['spd']:.1f}, "
                     f"precision≈{best['precision_macro_buy_sell']:.3f}, f1≈{best['f1_macro_buy_sell']:.3f}, "
                     f"hit_range={best['hit_range']}")

        logger.info(
            "🔧 Tuned thresholds: tau=%.3f, delta=%.2f, cooldown=%d → spd≈%.1f/day, "
            "precision≈%.3f, recall≈%.3f, f1≈%.3f (hit_range=%s)"
            % (tau, tstats['delta'], tstats['cooldown_bars'],
               tstats['spd'], tstats['precision_macro_buy_sell'],
               tstats['recall_macro_buy_sell'], tstats['f1_macro_buy_sell'],
               tstats['hit_range'])
        )

        # Просчёт SPD по принятой decision policy для лога
        if getattr(y_val_pred_proba, "ndim", 1) == 2 and y_val_pred_proba.shape[1] >= 3:
            p_buy = y_val_pred_proba[:, 1]
            p_sell = y_val_pred_proba[:, 2]
            maxp = np.maximum(p_buy, p_sell)
            margin = np.abs(p_buy - p_sell)
            act = (maxp >= tau) & (margin >= delta)

            idx = np.where(act)[0]
            if idx.size > 0:
                keep = [idx[0]]
                for i in idx[1:]:
                    if i - keep[-1] >= cooldown_bars:
                        keep.append(i)
                sel = np.zeros_like(act, dtype=bool)
                sel[np.array(keep, dtype=int)] = True
                act = sel

            spd_est = act.sum() * bars_per_day / max(1, len(y_val))
            logger.info(
                f"📊 Decision policy: tau={tau:.2f}, delta={delta:.2f}, cooldown={cooldown_bars} → spd≈{spd_est:.1f}/day")

        # чувствительность по tau
        # >>> sensitivity check (единый блок, расширенная сетка по tau, короткий свод логов)

        # Расширяем сетку по tau: ±0.05 и ±0.03 дополнительно к ±0.02
        _tau_offsets = [-0.05, -0.03, -0.02, 0.0, 0.02, 0.03, 0.05]
        _delta_offsets = [-0.02, 0.0, 0.02]  # delta оставляем как было

        # чувствительность по tau
        tau_sensitivity = []
        for off in _tau_offsets:
            tau_x = float(np.clip(tau + off, 0.0, 1.0))
            r = self._eval_decision_metrics(
                y_true=np.asarray(y_val),
                proba=np.asarray(y_val_pred_proba),
                tau=tau_x,
                delta=delta,
                cooldown_bars=cooldown_bars,
                bars_per_day=bars_per_day,
            )
            tau_sensitivity.append(r)

        # чувствительность по delta
        delta_sensitivity = []
        for off in _delta_offsets:
            delta_x = float(max(0.0, delta + off))
            r = self._eval_decision_metrics(
                y_true=np.asarray(y_val),
                proba=np.asarray(y_val_pred_proba),
                tau=tau,
                delta=delta_x,
                cooldown_bars=cooldown_bars,
                bars_per_day=bars_per_day,
            )
            delta_sensitivity.append(r)

        # ── Короткий свод логов: текущий τ и два ближайших соседа ──
        # сортируем по близости к текущему tau и берём топ-3
        _tau_sorted = sorted(tau_sensitivity, key=lambda r: abs(r['tau'] - float(tau)))[:3]
        # упорядочим в логах по возрастанию tau
        _tau_sorted = sorted(_tau_sorted, key=lambda r: r['tau'])

        logging.info("🔍 Sensitivity (tau near current):")
        for r in _tau_sorted:
            logging.info(f"  tau={r['tau']:.3f} → spd≈{r['spd']:.1f}, f1≈{r['f1_macro_buy_sell']:.3f}")

        # для delta лог оставим компактным (все 3 точки)
        logging.info("🔍 Sensitivity (delta±0.02):")
        for r in delta_sensitivity:
            logging.info(f"  delta={r['delta']:.2f} → spd≈{r['spd']:.1f}, f1≈{r['f1_macro_buy_sell']:.3f}")

        # Блок метрик
        val_acc = accuracy_score(y_val, y_val_pred)
        train_dist = Counter(y_train)
        val_dist = Counter(y_val)
        pred_dist = Counter(y_val_pred)

        logger.info(f"\n📊 Распределение классов:")
        logger.info(f"  Train: {dict(train_dist)}")
        logger.info(f"  Val:   {dict(val_dist)}")
        logger.info(f"  Pred:  {dict(pred_dist)}")

        prec, rec, f1, _ = precision_recall_fscore_support(
            y_val, y_val_pred,
            labels=REPORT_LABELS,  # [1,2,0]
            average=None,
            zero_division=0
        )
        cm = confusion_matrix(y_val, y_val_pred, labels=REPORT_LABELS)

        decision_policy = {
            'tau': tau,
            'delta': tstats['delta'],
            'cooldown_bars': tstats['cooldown_bars'],
            'bars_per_day': bars_per_day,
            'val_spd': tstats['spd'],
            'val_precision_macro_buy_sell': tstats['precision_macro_buy_sell'],
            'val_recall_macro_buy_sell': tstats['recall_macro_buy_sell'],
            'val_f1_macro_buy_sell': tstats['f1_macro_buy_sell'],
            'target_spd_range': tstats['range'],
            'hit_range': tstats['hit_range'],
            'precision_min': best.get('precision_min', 0.60),
        }
        precision_min_sweep = [
            {
                'precision_min': c['precision_min'],
                'tau': c['tau'],
                'spd': c['spd'],
                'precision_macro_buy_sell': c['precision_macro_buy_sell'],
                'f1_macro_buy_sell': c['f1_macro_buy_sell'],
                'hit_range': c['hit_range'],
            }
            for c in candidates
        ]

        metrics = {}
        metrics['decision_policy'] = decision_policy
        metrics['precision_min_sweep'] = precision_min_sweep
        metrics.update({
            'val_accuracy': float(val_acc),
            'precision': {name: float(val) for name, val in zip(REPORT_NAMES, prec)},
            'recall': {name: float(val) for name, val in zip(REPORT_NAMES, rec)},
            'f1_score': {name: float(val) for name, val in zip(REPORT_NAMES, f1)},
            'confusion_matrix': cm.tolist(),
            'best_iteration': int(getattr(model, 'best_iteration', 0) or 0),
            'class_distribution': {
                'train': {int(k): int(v) for k, v in train_dist.items()},
                'val': {int(k): int(v) for k, v in val_dist.items()},
                'pred': {int(k): int(v) for k, v in pred_dist.items()}
            },
            'tau_sensitivity': tau_sensitivity,
            'delta_sensitivity': delta_sensitivity,
        })

        # Сохранение модели в СОВРЕМЕННОМ ФОРМАТЕ
        os.makedirs("models", exist_ok=True)
        model_filename = f"models/ml_global_{self.symbol.replace('/', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.joblib"

        # Подготовка метаданных
        model_metadata = {
            'version': '1.1',
            'format': 'modern_dict',
            'instrument': self.symbol,
            'exchange': 'Binance',
            'timeframe': '5m',
            'feature_count': len(self.feature_names),
            'trained_at': datetime.now().isoformat(),
            'training_samples': len(X_train),
            'val_samples': len(X_val),
            'val_accuracy': float(val_acc),
            'best_iteration': int(getattr(model, 'best_iteration', 0) or 0),
            'run_id': run_id,
            'decision_policy': decision_policy,
            'scaler_used': use_scaler,  # Флаг использования scaler
        }

        # Сохранение в правильном формате (словарь)
        model_package = {
            'model': model,
            'scaler': scaler,  # Может быть None если use_scaler=False
            'metadata': model_metadata,
            'timeframe': '5m',
            'min_confidence': 0.65,
            'required_warmup': 60
        }

        joblib.dump(model_package, model_filename)
        logger.info(f"✅ Модель сохранена в СОВРЕМЕННОМ ФОРМАТЕ: {model_filename}")
        logger.info(f"   - Model: lgb.Booster")
        logger.info(f"   - Scaler: {'StandardScaler' if scaler else 'None (RAW features)'}")
        logger.info(f"   - Metadata: {len(model_metadata)} fields")

        try:
            tau_left = max(0.0, float(tau) - 0.05)
            tau_right = min(0.999, float(tau) + 0.05)
            tau_grid = np.arange(tau_left, tau_right + 1e-9, 0.002)

            spd_curve = []
            f1_curve = []
            for tcur in tau_grid:
                s = self._eval_decision_metrics(
                    y_true=np.asarray(y_val),
                    proba=np.asarray(y_val_pred_proba),
                    tau=float(tcur),
                    delta=float(delta),
                    cooldown_bars=int(cooldown_bars),
                    bars_per_day=int(bars_per_day),
                )
                spd_curve.append(s['spd'])
                f1_curve.append(s['f1_macro_buy_sell'])

            curve_prefix = str(Path("models/training_logs") / Path(model_filename).with_suffix('').name)

            plt.figure(figsize=(7, 4))
            plt.plot(tau_grid, spd_curve, linewidth=2)
            plt.axvline(float(tau), linestyle='--')
            plt.title('SPD vs tau')
            plt.xlabel('tau')
            plt.ylabel('signals per day')
            plt.tight_layout()
            plt.savefig(f"{curve_prefix}_tau_curve_spd.png")
            plt.close()

            plt.figure(figsize=(7, 4))
            plt.plot(tau_grid, f1_curve, linewidth=2)
            plt.axvline(float(tau), linestyle='--')
            plt.title('F1 (macro BUY/SELL on act) vs tau')
            plt.xlabel('tau')
            plt.ylabel('F1 macro (BUY/SELL)')
            plt.tight_layout()
            plt.savefig(f"{curve_prefix}_tau_curve_f1.png")
            plt.close()

        except Exception as _e:
            logging.warning(f"tau curves plotting skipped: {_e}")
        # <<< tau-curves

        # Сохранение метрик
        self.save_training_report(metrics, model_filename)

        # Диагностики
        diag_prefix = Path("models/training_logs") / Path(model_filename).with_suffix('').name
        self.post_training_diagnostics(
            model=model,
            X_val=X_val,
            y_val=y_val,
            y_val_pred_proba=y_val_pred_proba,
            prefix_path=str(diag_prefix),
            bars_per_day=bars_per_day
        )

        return metrics

    def plot_precision_spd_curve(self, y_val, y_val_pred_proba, bars_per_day: int,
                                 delta: float = 0.08, cooldown_bars: int = 2,
                                 prefix_path: str = "models/training_logs/diag") -> None:
        """
        Строит зависимости SPD(τ) и Precision/Recall/F1 от SPD для BUY/SELL (macro)
        и сохраняет:
          - *_tau_sweep.csv
          - *_spd_vs_tau.png
          - *_prf_vs_spd.png
        Параметры:
          y_val                : np.ndarray[int] (0=HOLD, 1=BUY, 2=SELL)
          y_val_pred_proba     : np.ndarray[float] shape=(n,3)
          bars_per_day         : 288 для 5m, 96 для 15m и т.п.
          delta                : |p_buy - p_sell| минимум, чтобы не брать «сомнительные» точки
          cooldown_bars        : минимальный зазор между сигналами в барах
          prefix_path          : путь префикса для сохранения артефактов
        """
        import numpy as np
        import pandas as pd
        import matplotlib.pyplot as plt
        from sklearn.metrics import precision_score, recall_score, f1_score

        proba = np.asarray(y_val_pred_proba)
        p_buy, p_sell = proba[:, 1], proba[:, 2]
        maxp = np.maximum(p_buy, p_sell)
        margin = np.abs(p_buy - p_sell)

        def _apply_cooldown(mask: np.ndarray, cd: int) -> np.ndarray:
            if cd <= 0:
                return mask
            idx = np.where(mask)[0]
            if idx.size == 0:
                return mask
            keep = [idx[0]]
            last = idx[0]
            for i in idx[1:]:
                if i - last >= cd:
                    keep.append(i)
                    last = i
            out = np.zeros_like(mask, dtype=bool)
            out[np.array(keep, dtype=int)] = True
            return out

        taus = np.linspace(0.45, 0.70, 26)  # можно сузить/расширить
        rows = []
        n = len(y_val)

        for tau in taus:
            act = (maxp >= tau) & (margin >= delta)
            act = _apply_cooldown(act, cooldown_bars)

            signals = int(act.sum())
            spd = signals * bars_per_day / max(1, n)

            if signals == 0:
                prec = rec = f1 = 0.0
            else:
                pred_dir = np.where(p_buy[act] >= p_sell[act], 1, 2)  # 1=BUY, 2=SELL
                true_dir = y_val[act]
                # macro по BUY/SELL (без HOLD)
                prec = precision_score(true_dir, pred_dir, labels=[1, 2], average='macro', zero_division=0)
                rec = recall_score(true_dir, pred_dir, labels=[1, 2], average='macro', zero_division=0)
                f1 = f1_score(true_dir, pred_dir, labels=[1, 2], average='macro', zero_division=0)

            rows.append((tau, spd, prec, rec, f1, signals))

        df = pd.DataFrame(rows, columns=['tau', 'spd_per_day', 'precision', 'recall', 'f1', 'signals'])
        csv_path = f"{prefix_path}_tau_sweep.csv"
        df.to_csv(csv_path, index=False)

        # ── График SPD(τ)
        plt.figure(figsize=(8, 5))
        plt.plot(df['tau'], df['spd_per_day'], marker='o')
        plt.xlabel('tau')
        plt.ylabel('SPD (signals/day)')
        plt.title('Signals per day vs tau')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_spd_vs_tau.png")
        plt.close()

        # ── График Precision/Recall/F1 vs SPD
        plt.figure(figsize=(8, 5))
        plt.plot(df['spd_per_day'], df['precision'], marker='o', label='Precision (macro BUY/SELL)')
        plt.plot(df['spd_per_day'], df['recall'], marker='o', label='Recall (macro BUY/SELL)')
        plt.plot(df['spd_per_day'], df['f1'], marker='o', label='F1 (macro BUY/SELL)')
        plt.xlabel('SPD (signals/day)')
        plt.ylabel('score')
        plt.title('Precision / Recall / F1 vs SPD')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_prf_vs_spd.png")
        plt.close()

    def post_training_diagnostics(self, model, X_val, y_val, y_val_pred_proba,
                                  prefix_path: str, bars_per_day: int = 288):
        """
        Диагностики после обучения...
        """
        os.makedirs(os.path.dirname(prefix_path), exist_ok=True)

        # гарантируем не-None и int
        try:
            bars_per_day = int(bars_per_day) if bars_per_day is not None else 288
        except Exception:
            bars_per_day = 288
        """
        Диагностики после обучения:
        - важность признаков (gain)
        - гистограммы вероятностей p(BUY)/p(SELL) для TP/FP/FN/TN
        - max-proba vs истинный/предсказанный класс
        - PR-кривые (one-vs-rest) для 3 классов
        Все сохраняется с префиксом prefix_path_* в models/training_logs.
        """

        os.makedirs(os.path.dirname(prefix_path), exist_ok=True)
        # === 0) Подготовка ===
        # ожидаем метки: 0=HOLD, 1=BUY, 2=SELL  (у тебя так в тренере)
        class_names = ['BUY', 'SELL', 'HOLD']  # для вывода в порядке [1,2,0]
        label_order = [1, 2, 0]

        proba = np.asarray(y_val_pred_proba)
        if proba.ndim == 1:
            # если вдруг уже метки, а не proba — быстро «расщепить» в фиктивный one-hot
            k = int(proba.max()) + 1
            tmp = np.zeros((len(proba), 3), dtype=float)
            tmp[np.arange(len(proba)), np.clip(proba.astype(int), 0, 2)] = 1.0
            proba = tmp

        # индексы колонок в proba по меткам [0,1,2]
        p_hold = proba[:, 0]
        p_buy = proba[:, 1]
        p_sell = proba[:, 2]

        # если есть feature_names — используем для импорта
        feat_names = getattr(self, "feature_names", None)
        if feat_names is None or len(feat_names) != X_val.shape[1]:
            feat_names = [f"f{i}" for i in range(X_val.shape[1])]

        # === 1) Важность признаков (gain) ===
        try:
            gain = model.feature_importance(importance_type='gain')
            df_imp = (pd.DataFrame({'feature': feat_names, 'gain': gain})
                      .sort_values('gain', ascending=False)
                      .head(30))
            plt.figure(figsize=(9.0, max(4.0, 0.3 * float(len(df_imp)))))
            sns.barplot(data=df_imp, x='gain', y='feature')
            plt.title('Feature Importance (gain) — top 30')
            plt.tight_layout()
            plt.savefig(f"{prefix_path}_feat_importance.png")
            plt.close()
        except Exception as e:
            try:
                split = model.feature_importance(importance_type='split')
                df_imp = (pd.DataFrame({'feature': feat_names, 'split': split})
                          .sort_values('split', ascending=False)
                          .head(30))
                plt.figure(figsize=(9.0, max(4.0, 0.3 * float(len(df_imp)))))
                sns.barplot(data=df_imp, x='split', y='feature')
                plt.title('Feature Importance (split) — top 30')
                plt.tight_layout()
                plt.savefig(f"{prefix_path}_feat_importance.png")
                plt.close()
            except Exception:
                pass  # нет важностей — пропускаем

        # === 2) Гистограммы p(BUY)/p(SELL) для TP/FP/FN/TN ===
        # соберем предсказанные метки
        y_pred = proba.argmax(axis=1)

        def hist_one(prob, true_class, name, fname):
            mask_pos = (y_val == true_class)
            mask_pred_pos = (y_pred == true_class)

            # TP: true=class & pred=class
            tp = prob[mask_pos & mask_pred_pos]
            # FP: true!=class & pred=class
            fp = prob[(~mask_pos) & mask_pred_pos]
            # FN: true=class & pred!=class
            fn = prob[mask_pos & (~mask_pred_pos)]
            # TN: true!=class & pred!=class
            tn = prob[(~mask_pos) & (~mask_pred_pos)]

            plt.figure(figsize=(8, 5))
            bins = 30
            sns.histplot(tp, bins=bins, stat='density', label='TP', alpha=0.6)
            sns.histplot(fp, bins=bins, stat='density', label='FP', alpha=0.6)
            sns.histplot(fn, bins=bins, stat='density', label='FN', alpha=0.6)
            sns.histplot(tn, bins=bins, stat='density', label='TN', alpha=0.6)
            plt.legend()
            plt.xlabel(f"p({name})")
            plt.ylabel("density")
            plt.title(f"Distributions for {name}")
            plt.tight_layout()
            plt.savefig(fname)
            plt.close()

        hist_one(p_buy, 1, "BUY", f"{prefix_path}_proba_hist_BUY.png")
        hist_one(p_sell, 2, "SELL", f"{prefix_path}_proba_hist_SELL.png")

        # === 3) Max-proba vs классы ===
        maxp = proba.max(axis=1)
        plt.figure(figsize=(8, 5))
        sns.scatterplot(x=np.arange(len(maxp)), y=maxp,
                        hue=[{0: 'HOLD', 1: 'BUY', 2: 'SELL'}[c] for c in y_val],
                        s=12, linewidth=0)
        plt.title("Max class probability vs true class (val order)")
        plt.xlabel("index in validation set (chronological)")
        plt.ylabel("max proba")
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_maxproba_scatter.png")
        plt.close()

        # === 4) PR-кривые (one-vs-rest) ===
        Y_bin = label_binarize(y_val, classes=[0, 1, 2])  # столбцы: HOLD, BUY, SELL
        curves = [
            ("BUY", Y_bin[:, 1], p_buy),
            ("SELL", Y_bin[:, 2], p_sell),
            ("HOLD", Y_bin[:, 0], p_hold),
        ]
        plt.figure(figsize=(8, 6))
        for name, y_true_bin, y_score in curves:
            precision, recall, _ = precision_recall_curve(y_true_bin, y_score)
            ap = average_precision_score(y_true_bin, y_score)
            plt.plot(recall, precision, label=f"{name} (AP={ap:.3f})")
        plt.xlabel("Recall")
        plt.ylabel("Precision")
        plt.title("Precision–Recall curves (one-vs-rest)")
        plt.legend()
        plt.tight_layout()
        plt.savefig(f"{prefix_path}_pr_curves.png")
        plt.close()

        self.plot_precision_spd_curve(
            y_val=y_val,
            y_val_pred_proba=y_val_pred_proba,
            bars_per_day=bars_per_day,  # 288 для 5m; передай из train_model
            delta=0.08,
            cooldown_bars=2,
            prefix_path=prefix_path
        )

    def save_training_report(self, metrics: dict, model_path: str):
        """Сохранение отчета о обучении"""
        os.makedirs("models/training_logs", exist_ok=True)

        report = {
            'training_date': datetime.now().isoformat(),
            'symbol': self.symbol,
            'db_dsn': self.db_dsn,
            'model_path': model_path,
            'metrics': metrics,
            'feature_names': self.feature_names
        }

        report_filename = f"models/training_logs/training_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_filename, 'w') as f:
            json.dump(report, f, indent=2)

        # Визуализация confusion matrix с корректными подписями
        try:
            import numpy as np
            import matplotlib.pyplot as plt
            import seaborn as sns

            cm = np.array(metrics.get('confusion_matrix', []))
            if cm.size == 0:
                raise ValueError("confusion_matrix пустая или отсутствует в metrics")

            n = cm.shape[0]

            # Попытка взять подписи из metrics['precision'].keys()
            prec_map = metrics.get('precision', {})
            labels = list(prec_map.keys()) if isinstance(prec_map, dict) else None

            # Фолбэки по размеру матрицы
            if not labels or len(labels) != n:
                if n == 3:
                    # Порядок соответствует отчету: BUY, SELL, HOLD
                    labels = ['BUY', 'SELL', 'HOLD']
                elif n == 4:
                    # Если где-то остались 4 класса
                    labels = ['BUY', 'SELL', 'HOLD', 'ANTI']
                else:
                    labels = [f"class_{i}" for i in range(n)]

            plt.figure(figsize=(8, 6))
            sns.heatmap(cm, annot=True, fmt='d',
                        xticklabels=labels, yticklabels=labels)
            plt.title('Confusion Matrix')
            plt.tight_layout()
            plt.savefig(report_filename.replace('.json', '_cm.png'))
            plt.close()
        except Exception as e:
            logger.warning(f"Не удалось создать визуализацию: {e}")

        logger.info(f"✅ Отчет сохранен: {report_filename}")

    def close(self):
        """Корректное закрытие"""
        self.data_loader.close()


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    """Основная функция"""
    print("🚀 ЗАПУСК ОБУЧЕНИЯ МОДЕЛИ (v2.0 - SQLite)")
    print("=" * 50)

    # Проверка существования БД
    db_file = DATA_DIR / "market_data.sqlite"
    if not db_file.exists():
        print(f"❌ База данных {db_file} не найдена!")
        print("   Сначала запустите:")
        print("   1. ml_data_preparation.py - для загрузки данных")
        print("   2. ml_labeling_tool_v3.py - для разметки данных")
        return 1
    trainer = None
    try:
        # Настройки
        db_dsn = MARKET_DB_DSN
        symbol = "ETHUSDT"
        # Получить последний run_id
        engine = create_engine(MARKET_DB_DSN)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT run_id FROM training_dataset_meta ORDER BY created_at DESC LIMIT 1"))
            row = result.fetchone()
        engine.dispose()

        if not row:
            print("❌ Нет готовых snapshot в training_dataset_meta!")
            print("   Создайте snapshot через [14] в ml_labeling_tool_v3.py")
            return 1

        run_id = row[0]

        print(f"📊 База данных: {DATA_DIR / 'market_data.sqlite'}")
        print(f"🎯 Символ: {symbol}")
        print(f"📦 Run ID: {run_id}")
        print("=" * 50)

        # Обучение
        trainer = ModelTrainer(db_dsn, symbol)

        # Параметр use_scaler можно настроить (по умолчанию True)
        use_scaler = False  # Установить False для обучения на RAW признаках
        metrics = trainer.train_model(run_id, use_scaler=use_scaler)

        # Вывод результатов
        print("\n🎯 РЕЗУЛЬТАТЫ ОБУЧЕНИЯ:")
        print(f"   Точность: {metrics['val_accuracy']:.4f}")
        print(
            f"   Precision BUY/SELL/HOLD: {metrics['precision']['BUY']:.4f}/{metrics['precision']['SELL']:.4f}/{metrics['precision']['HOLD']:.4f}")
        print(
            f"   Recall BUY/SELL/HOLD: {metrics['recall']['BUY']:.4f}/{metrics['recall']['SELL']:.4f}/{metrics['recall']['HOLD']:.4f}")
        print(
            f"   F1 BUY/SELL/HOLD: {metrics['f1_score']['BUY']:.4f}/{metrics['f1_score']['SELL']:.4f}/{metrics['f1_score']['HOLD']:.4f}")
        print("\n✅ Обучение завершено успешно!")
        return 0

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return 1
    finally:
        if trainer:
            trainer.close()


if __name__ == '__main__':
    sys.exit(main())
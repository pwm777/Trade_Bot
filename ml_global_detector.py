"""
ml_global_detector.py
ML-детектор разворотов на основе LightGBM для глобального таймфрейма (5m)

Поддерживает модели, обученные:
- В пакетном формате (trainer v2.1.1): окно lookback × base_features, scaler, decision_policy (tau/delta/cooldown)
- В legacy-формате (raw Booster): одно-бартовый вход (обратная совместимость)

Классы:
- 0: FLAT (нет разворота)
- 1: BUY reversal (разворот вверх)
- 2: SELL reversal (разворот вниз)
"""

from typing import Dict, Optional, Any, List
import numpy as np
import pandas as pd
from datetime import datetime
import os
import logging
from datetime import UTC
import lightgbm as lgb
import joblib

from iqts_standards import (
    DetectorSignal, Detector,
    normalize_signal, Timeframe
)

class MLGlobalDetector(Detector):
    """
    ML-детектор на основе LightGBM для глобального таймфрейма (5m)

    Работает с окном истории (скользящее окно), совпадающим с обучением:
    вектор признаков формируется в порядке [t0, t-1, ..., t-(lookback-1)] по списку base_feature_names.
    """

    def __init__(self, timeframe: Timeframe = "5m",
                 model_path: str = 'models/ml_global_5m_lgbm.joblib',
                 use_fallback: bool = False,
                 name: str = None, use_scaler: Optional[bool] = None):

        super().__init__(name or f"ml_global_{timeframe}")

        abs_path = os.path.abspath(model_path)
        self.logger.setLevel(logging.INFO)
        self.last_confidence = None
        self.timeframe = timeframe
        self.use_fallback = use_fallback
        self.model_path = model_path

        # Инициализация основных атрибутов модели
        self.model: Optional[lgb.Booster] = None
        self.use_scaler = use_scaler

        # Базовые признаки — будут заменены при загрузке пакетной модели (из метаданных)
        self.base_feature_names: List[str] = [
            'cmo_14', 'volume', 'trend_acceleration_ema7', 'regime_volatility',
            'bb_width', 'adx_14', 'plus_di_14', 'minus_di_14', 'atr_14_normalized',
            'volume_ratio_ema3', 'candle_relative_body', 'upper_shadow_ratio',
            'lower_shadow_ratio', 'price_vs_vwap', 'bb_position',
            'cusum_1m_quality_score', 'cusum_1m_trend_aligned', 'cusum_1m_price_move',
            'is_trend_pattern_1m', 'body_to_range_ratio_1m', 'close_position_in_range_1m'
        ]
        # lookback по умолчанию (будет заменён из модели при загрузке пакетного формата)
        self.lookback: int = 1
        # Полные имена windowed-признаков (генерируются из base_feature_names и lookback)
        self.feature_names: List[str] = self._generate_windowed_feature_names()

        # Порог и warmup — будут заменены при загрузке модельного пакета
        self.min_confidence = 0.53
        self.scaler = None
        self.required_warmup = 60  # общий тёплый старт (может быть больше, чем lookback)

        # Decision policy (из trainer): tau/delta/cooldown/bars_per_day
        self.decision_policy: Optional[Dict[str, Any]] = None
        self._last_signal_ts: Optional[int] = None  # для cooldown (ts последнего срабатывания)

        # Метаданные модели
        self.model_metadata = {
            'version': 'unknown',
            'instrument': 'ETH/USDT',
            'exchange': 'Binance',
            'timeframe': timeframe,
            'feature_count': len(self.feature_names),
            'trained_at': None,
            'training_samples': None,
            'val_accuracy': None
        }

        # ═══════════════════════════════════════════════════════════
        # ПРОСТАЯ ЗАГРУЗКА МОДЕЛИ БЕЗ РЕКУРСИИ
        # ═══════════════════════════════════════════════════════════
        if model_path and os.path.exists(abs_path):
            try:
                self.load_model(abs_path)
                self.logger.info(f"✅ ML модель успешно загружена из {abs_path}")
            except Exception as e:
                self.logger.error(f"❌ Ошибка загрузки модели: {e}")
                if not use_fallback:
                    raise
                else:
                    self.logger.warning("🔄 Режим fallback активирован")
        else:
            self.logger.error(f"❌ Файл модели не найден: {abs_path}")
            if not use_fallback:
                raise FileNotFoundError(f"Model file not found: {abs_path}")
            else:
                self.logger.warning("🔄 Режим fallback активирован")

    # ───────────────────────────────────────────────────────────
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ───────────────────────────────────────────────────────────
    def _generate_windowed_feature_names(self) -> List[str]:
        """
        Генерирует имена признаков для окна lookback в порядке:
        [feat_t0 for all base], [feat_t-1 for all base], ..., [feat_t-(lookback-1) ...]
        Совпадает с порядком в trainer (window[::-1].ravel()).
        """
        names: List[str] = []
        if self.lookback <= 1:
            # без окна — только текущий бар
            names = [f"{feat}_t0" for feat in self.base_feature_names]
        else:
            # t0 — последний бар окна (самая свежая свеча)
            for feat in self.base_feature_names:
                names.append(f"{feat}_t0")
            # t-1 ... t-(lookback-1)
            for lag in range(1, self.lookback):
                for feat in self.base_feature_names:
                    names.append(f"{feat}_t-{lag}")
        return names

    def _validate_features(self, features: np.ndarray) -> bool:
        """Проверяет, что массив признаков не содержит NaN или Inf."""
        if features is None:
            self.logger.warning("[VALIDATOR] Features array is None")
            return False
        has_nan = np.isnan(features).any()
        has_inf = np.isinf(features).any()
        return not (has_nan or has_inf)

    # ═══════════════════════════════════════════════════════════════
    # ИЗВЛЕЧЕНИЕ ПРИЗНАКОВ
    # ═══════════════════════════════════════════════════════════════
    def extract_features(self, df: pd.DataFrame) -> np.ndarray:
        """
        Извлекает признаки для модели:
        - Пакетная модель (с окнами): формирует окно из последних lookback баров и разворачивает вектор [t0, t-1, ...]
        - Legacy-модель: берёт последний бар (как раньше)
        """
        # Проверка, сколько баров доступно для окна
        min_bars = max(1, self.lookback)
        if len(df) < min_bars:
            raise ValueError(f"Insufficient bars for window: need {min_bars}, got {len(df)}")

        # Проверка наличия всех базовых признаков (по колонкам df)
        missing_features = [col for col in self.base_feature_names if col not in df.columns]
        available_features = [col for col in self.base_feature_names if col in df.columns]
        if missing_features:
            self.logger.error(f"❌ MISSING FEATURES ({len(missing_features)}): {missing_features}")
            self.logger.info(f"✅ AVAILABLE FEATURES ({len(available_features)}): {available_features}")
            # Показать несколько последних значений доступных фич
            for feature in available_features[:5]:
                sample_value = df[feature].iloc[-1] if len(df) > 0 else "N/A"
                self.logger.info(f"   {feature}: {sample_value}")
            raise ValueError(f"Missing ML features: {missing_features}")

        # Оконный режим (lookback > 1) — формируем (lookback, n_features) → разворачиваем в [t0, t-1, ...]
        if self.lookback > 1:
            # Берём последние lookback строк в хронологическом порядке
            tail = df.iloc[-self.lookback:]
            # Матрица признаков: строки — бары по времени (от старого к новому), столбцы — base_features
            window = tail[self.base_feature_names].to_numpy(dtype=float)  # shape: (lookback, n_features)
            # На всякий случай заменим NaN/Inf
            window = np.nan_to_num(window, nan=0.0, posinf=0.0, neginf=0.0)

            # Переупорядочиваем строки, чтобы первым шёл t0 (последний бар окна), затем t-1, ... — как в trainer
            window_ordered = window[::-1, :]  # shape: (lookback, n_features), первый ряд — t0
            # Разворачиваем в вектор: [t0_feat1..featN, t-1_feat1..featN, ...]
            features_array = window_ordered.reshape(1, -1).astype(np.float32)

            # Валидация
            if not self._validate_features(features_array):
                self.logger.warning("Features contain NaN/Inf, cleaning...")
                features_array = np.nan_to_num(features_array, nan=0.0, posinf=0.0, neginf=0.0)

            self.logger.info(f"✅ ML FEATURE DIAGNOSTIC (windowed) - OK | "
                             f"window_shape={window.shape}, vector_dim={features_array.shape[1]}")
            return features_array

        # Иначе — legacy режим (один бар)
        features = []
        for feature_name in self.base_feature_names:
            value = df[feature_name].iloc[-1]
            if pd.isna(value):
                self.logger.warning(f"Feature '{feature_name}' is NaN, replacing with 0.0")
                value = 0.0
            features.append(float(value))
        features_array = np.array(features, dtype=float).reshape(1, -1)

        if not self._validate_features(features_array):
            self.logger.warning("Features contain NaN/Inf, cleaning...")
            features_array = np.nan_to_num(features_array, nan=0.0, posinf=0.0, neginf=0.0)

        self.logger.info("✅ ML FEATURE DIAGNOSTIC (legacy) - OK")
        return features_array

    # ═══════════════════════════════════════════════════════════════
    # ОСНОВНОЙ МЕТОД АНАЛИЗА
    # ═══════════════════════════════════════════════════════════════
    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """
        Инференс LightGBM по входным данным для заданного таймфрейма (поддержка окон).
        """
        self.logger.info(f"🔄 Анализ тренда детектором LightGBM ")
        # 1) Валидация структуры входа
        if not data or not isinstance(data, dict):
            self.logger.error(f"❌ Invalid data structure: {type(data)}")
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "invalid_data_structure",
                "metadata": {"detector": "ml", "timeframe": self.timeframe}
            })

        # 2) Наличие нужного ТФ
        if self.timeframe not in data:
            self.logger.error(f"❌ Missing timeframe {self.timeframe} in data. Available: {list(data.keys())}")
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "missing_timeframe",
                "metadata": {"detector": "ml", "missing_tf": self.timeframe, "available_tfs": list(data.keys())}
            })

        df = data[self.timeframe]

        # Диагностика входного DataFrame
        self.logger.info(f"🔍 ML DETECTOR DIAGNOSTIC:")
        self.logger.info(f"  DataFrame shape: {df.shape}")
        self.logger.info(f"  Columns (first 15): {df.columns.tolist()[:15]}")
        last_ts = None
        if 'ts' in df.columns:
            last_ts = int(df['ts'].iloc[-1])
            self.logger.info(f"  last ts: {last_ts}")
        elif 'timestamp' in df.columns:
            last_ts = int(df['timestamp'].iloc[-1])
            self.logger.info(f"  last timestamp: {last_ts}")

        # 3) Проверка на пустые данные
        if df.empty:
            self.logger.error(f"❌ DataFrame for {self.timeframe} is empty")
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "empty_dataframe",
                "metadata": {"detector": "ml", "timeframe": self.timeframe}
            })

        # 4) Нормализация колонок (ts -> timestamp) — опционально
        if 'ts' in df.columns and 'timestamp' not in df.columns:
            df = df.rename(columns={'ts': 'timestamp'})
            data[self.timeframe] = df  # обновляем в data
            last_ts = int(df['timestamp'].iloc[-1])

        # 5) Проверка обязательных колонок OHLCV
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            self.logger.error(f"❌ Missing required columns: {missing_cols}")
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "missing_required_columns",
                "metadata": {"detector": "ml", "missing_cols": missing_cols}
            })

        # 6) Warmup: для корректного окна нужно минимум lookback баров
        min_bars = max(self.required_warmup, self.lookback)
        if len(df) < min_bars:
            self.logger.warning(f"⚠️ Insufficient data: {len(df)} < {min_bars} "
                                f"(required_warmup={self.required_warmup}, lookback={self.lookback})")
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "insufficient_warmup",
                "metadata": {
                    "detector": "ml",
                    "required": int(min_bars),
                    "actual": int(len(df))
                }
            })
        else:
            # Логируем начало анализа
            self.logger.info(f"🎯 Starting ML analysis: {len(df)} candles available (last={last_ts})")

        # 7) Модель загружена?
        if self.model is None:
            self.logger.error("❌ Model not loaded! Call load_model() first.")
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "model_not_loaded",
                "metadata": {"detector": "ml"}
            })

        self.logger.info(f"✅ All basic validations passed for {self.timeframe}")

        # ───────────────────────────────────────────────────────────
        # ИЗВЛЕЧЕНИЕ ПРИЗНАКОВ
        # ───────────────────────────────────────────────────────────
        try:
            X = self.extract_features(df)  # shape: (1, lookback * n_features) для пакетной модели
        except Exception as e:
            self.logger.error(f"❌ Feature extraction failed: {e}", exc_info=True)
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "feature_extraction_error",
                "metadata": {"detector": "ml", "error": str(e)}
            })

        # ───────────────────────────────────────────────────────────
        # МАСШТАБИРОВАНИЕ
        # ───────────────────────────────────────────────────────────
        try:
            if self.use_scaler and self.scaler is not None:
                X_scaled = self.scaler.transform(X)
                self.logger.debug("🔍 Using StandardScaler")
            else:
                X_scaled = X
                self.logger.debug("🔍 Using RAW features")
        except Exception as e:
            self.logger.error(f"❌ Feature scaling failed: {e}")
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "scaling_error",
                "metadata": {"detector": "ml", "error": str(e)}
            })

        # ───────────────────────────────────────────────────────────
        # ПРЕДСКАЗАНИЕ И ПРИМЕНЕНИЕ ПОЛИТИКИ ПОРОГОВ (tau/delta/cooldown)
        # ───────────────────────────────────────────────────────────
        try:
            probabilities = self.model.predict(X_scaled)[0]  # [p0, p1, p2]
            flat_p, buy_p, sell_p = float(probabilities[0]), float(probabilities[1]), float(probabilities[2])

            # Базовое направление по максимальной вероятности
            prediction_idx = int(np.argmax(probabilities))
            predicted_class_confidence = float(probabilities[prediction_idx])
            direction_map = {0: 0, 1: 1, 2: -1}
            predicted_direction = direction_map.get(prediction_idx, 0)

            # ✅ ИНИЦИАЛИЗАЦИЯ ПЕРЕМЕННЫХ ДО УСЛОВИЙ
            ok = False
            reason = "no_trend_signal"

            policy = self.decision_policy or self.model_metadata.get("decision_policy")
            if policy:
                tau = float(policy.get("tau", 0.5))
                delta = float(policy.get("delta", 0.08))
                cooldown_bars = int(policy.get("cooldown_bars", 0))

                # Он-акт логика как в trainer: maxp и margin
                maxp = max(buy_p, sell_p)
                margin = abs(buy_p - sell_p)

                act = (maxp >= tau) and (margin >= delta)

                # Применяем cooldown по барам (если есть исторический ts)
                if act and cooldown_bars > 0 and last_ts is not None:
                    if self._last_signal_ts is not None:
                        # Сколько баров прошло с последнего сигнала
                        try:
                            ts_col = 'timestamp' if 'timestamp' in df.columns else 'ts'
                            passed_bars = int((df[ts_col] > self._last_signal_ts).sum())
                        except Exception:
                            passed_bars = 0
                        if passed_bars < cooldown_bars:
                            act = False
                            reason = "cooldown_active"

                # ✅ ОПРЕДЕЛЯЕМ ok И reason НА ОСНОВЕ act
                if predicted_direction == 0:
                    ok = True
                    reason = "no_trend_signal"
                else:
                    ok = act
                    if ok:
                        reason = "trend_confirmed"
                    elif reason == "cooldown_active":
                        # Уже установлен выше
                        pass
                    else:
                        reason = "weak_trend_signal"  # ✅ Валидный ReasonCode

                # Если сработал сигнал — фиксируем ts
                if ok and predicted_direction != 0 and last_ts is not None:
                    self._last_signal_ts = last_ts

            else:
                # Fallback: без политики — простой порог уверенности
                if predicted_direction == 0:
                    ok = True
                    reason = "no_trend_signal"
                else:
                    ok = (predicted_class_confidence >= self.min_confidence)
                    reason = "trend_confirmed" if ok else "weak_trend_signal"

            self.last_confidence = predicted_class_confidence

            self.logger.info(
                f"🔄 ML результат: dir={predicted_direction} | conf={predicted_class_confidence:.3f} | "
                f"BUY={buy_p:.3f} | SELL={sell_p:.3f} | FLAT={flat_p:.3f} | "
                f"policy={'on' if policy else 'off'} | ok={ok} | reason={reason}"
            )

            return normalize_signal({
                "ok": ok,
                "direction": predicted_direction,
                "confidence": predicted_class_confidence,
                "reason": reason,
                "metadata": {
                    "detector": "ml",
                    "timeframe": self.timeframe,
                    "probabilities": {"FLAT": flat_p, "BUY": buy_p, "SELL": sell_p},
                    "predicted_class_confidence": predicted_class_confidence,
                    "feature_count": int(X.shape[1]),
                    "model_version": self.model_metadata.get("version", "unknown"),
                    "lookback": int(self.lookback),
                    "base_feature_count": int(len(self.base_feature_names)),
                    "decision_policy": policy or {},
                }
            })

        except Exception as e:
            self.logger.error(f"❌ Prediction failed: {e}", exc_info=True)
            return normalize_signal({
                "ok": False,
                "direction": 0,  # FLAT
                "confidence": 0.0,
                "reason": "prediction_error",
                "metadata": {"detector": "ml", "error": str(e)}
            })

    def load_model(self, path: str):
        """
        Загрузка модели с диагностикой и поддержкой пакетного формата trainer'а (v2.1.1)
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Model file not found: {path}")

        try:
            self.logger.info(f"🔄 Загрузка модели из {path}...")
            loaded_data = joblib.load(path)

            # СОВРЕМЕННЫЙ ФОРМАТ (из trainer)
            if isinstance(loaded_data, dict):

                self.model = loaded_data.get("model")
                if self.model is None:
                    raise ValueError("Dictionary does not contain 'model' key")

                self.scaler = loaded_data.get("scaler")
                self.model_metadata = loaded_data.get("metadata", {})

                # Обновление параметров из модельного пакета
                self.timeframe = loaded_data.get("timeframe", self.timeframe)
                self.min_confidence = loaded_data.get("min_confidence", self.min_confidence)
                self.required_warmup = loaded_data.get("required_warmup", self.required_warmup)

                # Decision policy
                self.decision_policy = self.model_metadata.get("decision_policy")

                # Базовые признаки и окно (обязательно для оконного режима)
                self.base_feature_names = loaded_data.get("base_feature_names", self.base_feature_names)
                self.lookback = int(loaded_data.get("lookback", max(1, self.lookback)))
                # Сгенерировать полные имена оконных признаков (для диагностики)
                self.feature_names = self._generate_windowed_feature_names()

                # Определение использования скейлера
                scaler_used = self.model_metadata.get("scaler_used", False)
                if hasattr(self, "use_scaler") and getattr(self, "use_scaler") is None:
                    self.use_scaler = scaler_used

                self.logger.info(
                    f"✅ Модель загружена: timeframe={self.timeframe}, "
                    f"scaler={'✓' if self.scaler else '✗'}, "
                    f"lookback={self.lookback}, base_features={len(self.base_feature_names)}, "
                    f"vector_dim={len(self.feature_names)}, "
                    f"policy={'✓' if self.decision_policy else '✗'}"
                )

                # ВАЛИДАЦИЯ МОДЕЛИ
                if not isinstance(self.model, lgb.Booster):
                    raise TypeError(f"Model must be lgb.Booster, got {type(self.model).__name__}")

            # LEGACY ФОРМАТ (без окна, совместимость)
            elif isinstance(loaded_data, lgb.Booster):
                self.model = loaded_data
                self.scaler = None
                self.model_metadata = {
                    "version": "legacy",
                    "loaded_at": datetime.now(UTC).isoformat(),
                    "format": "raw_booster",
                    "scaler_used": False,
                }
                self.use_scaler = False
                self.lookback = 1
                self.feature_names = self._generate_windowed_feature_names()
                self.decision_policy = None
                self.logger.info("✅ Legacy модель загружена (RAW features, single-bar)")

            else:
                raise TypeError(f"Unsupported model format: {type(loaded_data)}")

        except Exception as e:
            self.logger.error(f"❌ Ошибка загрузки модели: {e}", exc_info=True)
            raise

    # ═══════════════════════════════════════════════════════════════
    # МЕТОДЫ ИНТЕРФЕЙСА DETECTOR
    # ═══════════════════════════════════════════════════════════════

    def get_required_bars(self) -> Dict[str, int]:
        """Минимальное количество баров для анализа (учитывает lookback)."""
        return {self.timeframe: max(int(self.required_warmup), int(self.lookback))}
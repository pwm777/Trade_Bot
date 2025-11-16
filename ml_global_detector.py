"""
ml_global_detector.py
ML-детектор разворотов на основе LightGBM для глобального таймфрейма (5m)
Описание:
- Использует  признаки из OHLCV данных ETH/USDT
- Классификация: FLAT (0), BUY reversal (1), SELL reversal (2)
- Работает совместно с cosum-детекторами (1m)
- Автоматический fallback на cosum при отсутствии модели

"""

from typing import Dict, Optional, Any
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
     normalize_signal, Timeframe)

class MLGlobalDetector(Detector):
    """
    ML-детектор на основе LightGBM для глобального таймфрейма (5m)

    Принцип работы:
    1. Получает OHLCV данные 5m из market_data
    3. Нормализует признаки через StandardScaler
    4. Предсказывает класс через обученную LightGBM модель
    5. Возвращает DetectorSignal в стандартном формате IQTS

    Классы:
    - 0: FLAT (нет разворота)
    - 1: BUY reversal (разворот вверх)
    - 2: SELL reversal (разворот вниз)

    Интеграция:
    - Реализует интерфейс DetectorInterface
    - Заменяет GlobalTrendDetector в ExitSignalDetector
    - Работает с каскадной логикой exit_system.py

    Пример использования:
        detector = MLGlobalDetector(
            timeframe='5m',
            model_path='models/ml_global_5m_lgbm.joblib',
            name='ml_global_5m'
        )
        # signal = {'ok': True, 'direction': 'BUY', 'confidence': 0.78, ...}
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
        self.feature_names = [
            'cmo_14', 'volume', 'trend_acceleration_ema7', 'regime_volatility',
            'bb_width', 'adx_14', 'plus_di_14', 'minus_di_14', 'atr_14_normalized',
            'volume_ratio_ema3', 'candle_relative_body', 'upper_shadow_ratio',
            'lower_shadow_ratio', 'price_vs_vwap', 'bb_position', 'cusum_1m_recent',
            'cusum_1m_quality_score', 'cusum_1m_trend_aligned', 'cusum_1m_price_move',
            'is_trend_pattern_1m', 'body_to_range_ratio_1m', 'close_position_in_range_1m'
        ]

        self.min_confidence = 0.53
        self.scaler = None
        self.required_warmup = 60

        # Метаданные модели
        self.model_metadata = {
            'version': '1.0',
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

    def get_status(self) -> Dict[str, Any]:
        """Получение статуса ML детектора"""
        return {
            'ok': self.model is not None,
            'confidence': self.last_confidence,
            'model_loaded': self.model is not None,
            'scaler_available': getattr(self, 'scaler', None) is not None,
            'feature_count': len(self.feature_names),
            'required_warmup': self.required_warmup,
            'min_confidence': self.min_confidence,
            'model_metadata': self.model_metadata
        }

    # ═══════════════════════════════════════════════════════════════
    # ИЗВЛЕЧЕНИЕ ПРИЗНАКОВ
    # ═══════════════════════════════════════════════════════════════

    def extract_features(self, df: pd.DataFrame) -> np.ndarray:
        """
        Извлекает все признаки с детальной диагностикой
        """

        # 🔥 ДЕТАЛЬНАЯ ДИАГНОСТИКА ФИЧ
        # Проверка наличия всех feature колонок
        missing_features = [col for col in self.feature_names if col not in df.columns]
        available_features = [col for col in self.feature_names if col in df.columns]

        if missing_features:
            self.logger.error(f"❌ MISSING FEATURES ({len(missing_features)}): {missing_features}")
            self.logger.info(f"✅ AVAILABLE FEATURES ({len(available_features)}): {available_features}")

            # Проверяем первые несколько строк доступных фич
            for feature in available_features[:5]:  # первые 5 чтобы не заспамить
                sample_value = df[feature].iloc[-1] if len(df) > 0 else "N/A"
                self.logger.info(f"   {feature}: {sample_value}")

            raise ValueError(f"Missing ML features: {missing_features}")

        # Проверка качества данных в фичах
        problematic_features = []
        for feature_name in self.feature_names:
            values = df[feature_name]
            if values.isna().all() or values.isnull().all():
                problematic_features.append(f"{feature_name} (all NaN)")
            elif (values == 0).all():
                problematic_features.append(f"{feature_name} (all zeros)")

        if problematic_features:
            self.logger.warning(f"⚠️ PROBLEMATIC FEATURES: {problematic_features}")

        # Извлекаем последние значения всех признаков
        features = []
        for feature_name in self.feature_names:
            value = df[feature_name].iloc[-1]

            # Обработка None/NaN
            if pd.isna(value):
                self.logger.warning(f"Feature '{feature_name}' is NaN, replacing with 0.0")
                value = 0.0

            features.append(float(value))

        # Преобразуем в numpy array (1, n_features)
        features_array = np.array(features).reshape(1, -1)

        # Валидация на NaN/Inf
        if not self._validate_features(features_array):
            self.logger.warning("Features contain NaN/Inf, cleaning...")
            features_array = np.nan_to_num(features_array, nan=0.0, posinf=0.0, neginf=0.0)

        self.logger.info("✅ ML FEATURE DIAGNOSTIC - OK")
        return features_array

    # ═══════════════════════════════════════════════════════════════
    # ОСНОВНОЙ МЕТОД АНАЛИЗА
    # ═══════════════════════════════════════════════════════════════

    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """
        Инференс LightGBM по входным данным для заданного таймфрейма.
        """
        self.logger.info(f"🔄 Анализ тренда детектором LightGBM ")
        # 1) Валидация структуры входа - УПРОЩЕННАЯ ВАЛИДАЦИЯ
        if not data or not isinstance(data, dict):
            self.logger.error(f"❌ Invalid data structure: {type(data)}")
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "invalid_data_structure",
                "metadata": {"detector": "ml", "timeframe": self.timeframe}
            })

        # 2) Наличие нужного ТФ
        if self.timeframe not in data:
            self.logger.error(f"❌ Missing timeframe {self.timeframe} in data. Available: {list(data.keys())}")
            return normalize_signal({
                "ok": False,
                "direction": 0, #"FLAT"
                "confidence": 0.0,
                "reason": "missing_timeframe",
                "metadata": {"detector": "ml", "missing_tf": self.timeframe, "available_tfs": list(data.keys())}
            })

        df = data[self.timeframe]

        # ✅ ДОБАВИТЬ ЭТО ЛОГИРОВАНИЕ
        self.logger.info(f"🔍 ML DETECTOR DIAGNOSTIC:")
        self.logger.info(f"  DataFrame shape: {df.shape}")
        self.logger.info(f"  DataFrame type: {type(df)}")
        self.logger.info(f"  Index type: {type(df.index).__name__}")
        self.logger.info(f"  Index name: {df.index.name}")
        if hasattr(df.index, 'dtype'):
            self.logger.info(f"  Index dtype: {df.index.dtype}")
        self.logger.info(f"  Has 'ts' column: {'ts' in df.columns}")
        self.logger.info(f"  Has 'timestamp' column: {'timestamp' in df.columns}")
        self.logger.info(f"  Columns (first 15): {df.columns.tolist()[:15]}")

        # 3) Проверка на пустые данные
        if df.empty:
            self.logger.error(f"❌ DataFrame for {self.timeframe} is empty")
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "empty_dataframe",
                "metadata": {"detector": "ml", "timeframe": self.timeframe}
            })

        # 4) Нормализация колонок (ts -> timestamp)
        if 'ts' in df.columns and 'timestamp' not in df.columns:
            df = df.rename(columns={'ts': 'timestamp'})
            data[self.timeframe] = df  # обновляем в data

        # 5) Проверка обязательных колонок
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            self.logger.error(f"❌ Missing required columns: {missing_cols}")
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "missing_required_columns",
                "metadata": {"detector": "ml", "missing_cols": missing_cols}
            })

        # 6) Warmup
        if len(df) < self.required_warmup:
            self.logger.warning(f"⚠️ Insufficient data: {len(df)} < {self.required_warmup}")
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "insufficient_warmup",
                "metadata": {
                    "detector": "ml",
                    "required": self.required_warmup,
                    "actual": len(df)
                }
            })
        else:
            # ✅ НОВОЕ: Логируем начало анализа
            self.logger.info(
                f"🎯 Starting ML analysis: {len(df)} candles available "
                f"(last candle ts={df['ts'].iloc[-1] if 'ts' in df.columns else 'N/A'})"
            )

        # 7) Модель загружена?
        if self.model is None:
            self.logger.error("❌ Model not loaded! Call load_model() first.")
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "model_not_loaded",
                "metadata": {"detector": "ml"}
            })

        self.logger.info(f"✅ All basic validations passed for {self.timeframe}")

        # ───────────────────────────────────────────────────────────
        # ИЗВЛЕЧЕНИЕ ПРИЗНАКОВ
        # ───────────────────────────────────────────────────────────
        try:
            features = self.extract_features(df)
        except Exception as e:
            self.logger.error(f"❌ Feature extraction failed: {e}", exc_info=True)
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "feature_extraction_error",
                "metadata": {"detector": "ml", "error": str(e)}
            })

        # ───────────────────────────────────────────────────────────
        # МАСШТАБИРОВАНИЕ
        # ───────────────────────────────────────────────────────────
        try:
            if self.use_scaler and self.scaler is not None:
                features_scaled = self.scaler.transform(features)
                self.logger.debug("🔍 Using StandardScaler")
            else:
                features_scaled = features
                self.logger.debug("🔍 Using RAW features")
        except Exception as e:
            self.logger.error(f"❌ Feature scaling failed: {e}")
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "scaling_error",
                "metadata": {"detector": "ml", "error": str(e)}
            })

        # ───────────────────────────────────────────────────────────
        # ПРЕДСКАЗАНИЕ
        # ───────────────────────────────────────────────────────────
        try:
            X_last = features_scaled[-1:].astype(np.float32)
            probabilities = self.model.predict(X_last)[0]

            # Обработка результатов
            flat_p, buy_p, sell_p = float(probabilities[0]), float(probabilities[1]), float(probabilities[2])

            prediction_idx = int(np.argmax(probabilities))
            predicted_class_confidence = float(probabilities[prediction_idx])

            self.last_confidence = predicted_class_confidence
            direction_map = {0: 0, 1: 1, 2: -1}
            predicted_direction = direction_map.get(prediction_idx, 0)

            predicted_class_confidence = float(probabilities[prediction_idx])

            # Применение порога уверенности
            if predicted_direction == 0:
                ok = True
                reason = "no_trend_signal"
            else:  # BUY (1) или SELL (-1)
                self.logger.info(
                    f"🔄 ML порог: min_confidence={self.min_confidence} | conf={predicted_class_confidence:.3f} | "
                )
                ok = (predicted_class_confidence >= self.min_confidence)
                reason = "trend_confirmed" if ok else "weak_trend_signal"

            self.logger.info(
                f"🔄 ML результат: {predicted_direction} | conf={predicted_class_confidence:.3f} | "
                f"BUY={buy_p:.3f} | SELL={sell_p:.3f} | FLAT={flat_p:.3f} | ok={ok} | reason={reason}"
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
                    "feature_count": int(features.shape[1]),
                    "model_version": self.model_metadata.get("version", "unknown")
                }
            })

        except Exception as e:
            self.logger.error(f"❌ Prediction failed: {e}", exc_info=True)
            return normalize_signal({
                "ok": False,
                "direction": 0, #FLAT
                "confidence": 0.0,
                "reason": "prediction_error",
                "metadata": {"detector": "ml", "error": str(e)}
            })

    def load_model(self, path: str):
        """
        Загрузка модели с улучшенной диагностикой
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

                # Обновление параметров из модели
                self.timeframe = loaded_data.get("timeframe", self.timeframe)
                self.min_confidence = loaded_data.get("min_confidence", self.min_confidence)
                self.required_warmup = loaded_data.get("required_warmup", self.required_warmup)

                # Определение использования скейлера
                scaler_used = self.model_metadata.get("scaler_used", False)
                if hasattr(self, "use_scaler") and getattr(self, "use_scaler") is None:
                    self.use_scaler = scaler_used

                self.logger.info(
                    f"✅ Модель загружена: timeframe={self.timeframe}, "
                    f"scaler={'✓' if self.scaler else '✗'}, "
                    f"features={len(self.feature_names)}"
                )

            # LEGACY ФОРМАТ
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
                self.logger.info("✅ Legacy модель загружена (RAW features)")

            else:
                raise TypeError(f"Unsupported model format: {type(loaded_data)}")
            self.min_confidence = 0.53  # Игнорируем значение из модели
            self.logger.info(f"🔧 min_confidence overridden to {self.min_confidence}")
            # ВАЛИДАЦИЯ МОДЕЛИ
            if not isinstance(self.model, lgb.Booster):
                raise TypeError(f"Model must be lgb.Booster, got {type(self.model).__name__}")

        except Exception as e:
            self.logger.error(f"❌ Ошибка загрузки модели: {e}", exc_info=True)
            raise

    # ═══════════════════════════════════════════════════════════════
    # МЕТОДЫ ИНТЕРФЕЙСА DETECTOR
    # ═══════════════════════════════════════════════════════════════

    def get_required_bars(self) -> Dict[str, int]:
        """Минимальное количество баров для анализа"""
        return {self.timeframe: self.required_warmup}


    def _validate_features(self, features: np.ndarray) -> bool:
        """
        Проверяет, что массив признаков не содержит NaN или Inf.
        Возвращает True, если всё корректно, иначе False.
        """

        if features is None:
            self.logger.warning("[VALIDATOR] Features array is None")
            return False

        # Проверка на NaN и Inf
        has_nan = np.isnan(features).any()
        has_inf = np.isinf(features).any()


        return not (has_nan or has_inf)

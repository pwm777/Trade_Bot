"""
iqts_detectors.py
Объединённый модуль с детекторами для упрощённой системы.
"""

from typing import Dict, cast, Optional
import logging
import numpy as np
import pandas as pd
from iqts_standards import (
    DetectorSignal, Detector,
    normalize_signal, validate_market_data, Timeframe,
    DetectorMetadata
)
from ml_global_detector import MLGlobalDetector
from market_data_utils import CusumConfig, CUSUM_CONFIG_1M,  CUSUM_CONFIG_5M


class MLGlobalTrendDetector(Detector):
    """
    ML-детектор для определения глобального тренда (5m).
    Использует ML-модель с fallback на CUSUM детектор.
    """

    def __init__(self, timeframe: Timeframe = "5m",
                 model_path: str = None,
                 use_fallback: bool = True,
                 name: str = None,
                 cusum_config: Optional[CusumConfig] = None):
        """Инициализация ML глобального тренд-детектора."""

        super().__init__(name=name or f"ml_global_{timeframe}")

        self.timeframe: Timeframe = timeframe
        self.model_path = model_path
        self.use_fallback = use_fallback
        self.using_fallback = False  # ← Важно! Изначально False
        self.ml_detector = None
        self.fallback_detector = None
        self.cusum_config = cusum_config

        # Настройка logger
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = True
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        self.logger.info(
            f"Initializing MLGlobalTrendDetector:\n"
            f"  Timeframe: {timeframe}\n"
            f"  Model path: {model_path}\n"
            f"  Use fallback: {use_fallback}"
        )

        # ✅ Инициализируем ML модель
        if model_path:
            try:
                self.ml_detector = MLGlobalDetector(
                    timeframe=timeframe,
                    model_path=model_path
                )
                self.logger.info(f"✅ ML model loaded successfully")
            except Exception as e:
                self.logger.error(f"❌ Failed to load ML model: {e}")
                if use_fallback:
                    self.logger.info("🔄 Activating CUSUM fallback due to ML failure...")
                    self._activate_fallback()
                else:
                    raise

        # ✅ ИЗМЕНЕНО: Только СОЗДАЕМ fallback, НЕ активируем
        if use_fallback and not self.fallback_detector:
            try:
                self.fallback_detector = GlobalTrendDetector(
                    timeframe=self.timeframe,
                    name=f"fallback_{self.timeframe}",
                    cusum_config=self.cusum_config
                )
                self.logger.info(f"✅ CUSUM fallback prepared (standby mode)")
            except Exception as e:
                self.logger.warning(f"⚠️ Failed to prepare fallback: {e}")

    def _activate_fallback(self):
        """
        Активация CUSUM fallback детектора.

        ✅ ИСПРАВЛЕНИЕ: метод НЕ принимает параметр cusum_config,
        используем self.cusum_config вместо этого
        """
        try:
            # ✅ Используем self.cusum_config который был сохранен в __init__
            self.fallback_detector = GlobalTrendDetector(
                timeframe=self.timeframe,
                name=f"fallback_{self.timeframe}",
                cusum_config=self.cusum_config
            )
            self.using_fallback = True
            self.logger.info(f"✅ CUSUM fallback activated for {self.timeframe}")
        except Exception as e:
            self.logger.error(f"❌ Failed to activate fallback: {e}")
            raise RuntimeError(f"Failed to activate fallback detector: {e}") from e

    def get_required_bars(self) -> Dict[str, int]:
        """Минимальное количество баров для анализа"""
        if self.ml_detector and not self.using_fallback:
            return self.ml_detector.get_required_bars()
        elif self.fallback_detector:
            return self.fallback_detector.get_required_bars()
        else:
            return {self.timeframe: 100}

    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """Анализ с ML или fallback"""

        # ✅ ДОБАВИТЬ ДИАГНОСТИКУ В НАЧАЛО
        self.logger.info("=" * 80)
        self.logger.info("🚀 MLGlobalTrendDetector.analyze() called")
        self.logger.info(f"   ml_detector exists: {self.ml_detector is not None}")
        self.logger.info(f"   fallback_detector exists: {self.fallback_detector is not None}")
        self.logger.info(f"   using_fallback flag: {self.using_fallback}")
        self.logger.info(f"   Input data keys: {list(data.keys()) if isinstance(data, dict) else 'NOT A DICT'}")

        if self.ml_detector and not self.using_fallback:
            self.logger.info("🔄 Attempting ML detector analysis...")
            try:
                signal = await self.ml_detector.analyze(data)

                # ✅ ЛОГИРОВАТЬ РЕЗУЛЬТАТ ML
                self.logger.info(
                    f"✅ ML detector result: ok={signal['ok']}, direction={signal['direction']}, confidence={signal['confidence']:.3f}, reason={signal.get('reason', 'N/A')}")

                # ✅ ПРОВЕРКА: если ML вернул ошибку
                if not signal['ok']:
                    reason = signal.get('reason', 'unknown')
                    self.logger.info(f"⚠️ ML returned ok=False, reason={reason}")

                    # ✅ Fallback активируем ТОЛЬКО при настоящих ошибках
                    ERROR_REASONS = {
                        'invalid_data_structure',
                        'missing_timeframe',
                        'empty_dataframe',
                        'missing_required_columns',
                        'insufficient_warmup',
                        'model_not_loaded',
                        'feature_extraction_error',
                        'scaling_error',
                        'prediction_error'
                    }

                    if reason in ERROR_REASONS:
                        self.logger.warning(f"🔄 ML detector error ({reason}), switching to fallback...")
                        if self.use_fallback and not self.using_fallback:
                            self._activate_fallback()
                            return await self.analyze(data)  # Рекурсивный вызов с fallback
                        else:
                            # Fallback уже активен или недоступен
                            pass
                    else:
                        # Это не ошибка, а нормальное состояние (weak_trend_signal, no_trend_signal, cooldown_active)
                        self.logger.debug(f"✅ ML returned valid state: {reason}")

                enriched_metadata = {
                    **signal.get('metadata', {}),
                    'detector_type': 'ml',
                    'detector_class': 'MLGlobalDetector',
                    'model_used': True,
                    'model_path': self.model_path,
                    'fallback_available': self.use_fallback,
                    'timeframe': self.timeframe
                }
                return {
                    **signal,
                    'metadata': cast(DetectorMetadata, enriched_metadata)
                }

            except Exception as e:
                self.logger.error(f"❌ ML detector exception: {e}", exc_info=True)
                self.logger.error(
                    f"   Fallback: {'activating' if self.use_fallback else 'unavailable'}")
                if self.use_fallback and not self.using_fallback:
                    self._activate_fallback()
                    return await self.analyze(data)
                else:
                    return normalize_signal({
                        "ok": False,
                        "direction": 0,
                        "confidence": 0.0,
                        "reason": "detector_error",
                        "metadata": {"error": str(e), "detector_type": "ml_failed", "fallback_available": False}
                    })
        else:
            # ✅ ЛОГИРОВАТЬ ИСПОЛЬЗОВАНИЕ FALLBACK
            self.logger.info("🔄 Using fallback detector (ML not available or disabled)")
            self.logger.info(
                f"   Reason: ml_detector={self.ml_detector is not None}, using_fallback={self.using_fallback}")

            if self.fallback_detector:
                self.logger.info("🔄 Calling fallback detector...")
                result = await self.fallback_detector.analyze(data)
                self.logger.info(
                    f"✅ Fallback result: ok={result['ok']}, direction={result['direction']}, confidence={result['confidence']:.3f}, reason={result.get('reason', 'N/A')}")
                return result
            else:
                self.logger.error("❌ Neither ML nor fallback detector available!")
                return normalize_signal({
                    "ok": False,
                    "direction": 0,
                    "confidence": 0.0,
                    "reason": "detector_error",
                    "metadata": {
                        "ml_detector": self.ml_detector is not None,
                        "fallback_detector": self.fallback_detector is not None,
                        "using_fallback": self.using_fallback
                    }
                })

    def get_status(self) -> Dict:
        """Статус детектора"""
        status = {
            'timeframe': self.timeframe,
            'ml_available': self.ml_detector is not None,
            'using_fallback': self.using_fallback,
            'fallback_available': self.fallback_detector is not None,
            'model_path': self.model_path,
            'ok': True,
            'confidence': 0.0
        }
        if self.ml_detector and not self.using_fallback:
            status['active_detector'] = 'ml'
            status['detector_class'] = 'MLGlobalDetector'
            try:
                ml_status = self.ml_detector.get_status() if hasattr(self.ml_detector, 'get_status') else {}
                status.update(ml_status)
            except Exception as e:
                status['ml_status_error'] = str(e)
        elif self.fallback_detector:
            status['active_detector'] = 'cusum_fallback'
            status['detector_class'] = 'GlobalTrendDetector'
            try:
                fallback_status = self.fallback_detector.get_status()
                status.update(fallback_status)
            except Exception as e:
                status['fallback_status_error'] = str(e)
        else:
            status['active_detector'] = 'none'
            status['ok'] = False
        return status

    def reset_state(self):
        """Сброс внутреннего состояния"""
        if self.fallback_detector:
            self.fallback_detector.reset_state()

class RoleBasedOnlineTrendDetector(Detector):
    """
    Role-based тренд-детектор на основе готовых CUSUM данных из БД.
    Анализирует cusum_state, cusum_conf и другие поля.
    """

    def __init__(self, timeframe: Timeframe = "1m",
                 role: str = "trend",
                 name: str = None):
        """
        Инициализация role-based детектора.

        ✅ ИСПРАВЛЕНИЯ:
        1. Явно передаем name родительскому классу Detector
        2. Сохраняем timeframe как атрибут экземпляра
        3. Инициализируем пороги на основе role

        Args:
            timeframe: таймфрейм анализа (1m, 5m, 10s и т.д.)
            role: роль детектора (trend, entry, exit)
            name: имя детектора для логирования
        """
        # ✅ ИСПРАВЛЕНИЕ: Передаем name родительскому классу Detector
        super().__init__(name=name or f"{role}_{timeframe}")

        self.timeframe: Timeframe = timeframe
        self.role: str = role
        self.signal_count: int = 0

        # Пороги в зависимости от role
        role_thresholds = {
            "trend": 0.55,  # Тренд-детектор: пороги средние
            "entry": 0.60,  # Entry-детектор: пороги выше
            "exit": 0.50,  # Exit-детектор: пороги ниже (быстрее выход)
            "global": 0.60,  # Глобальный: пороги выше
        }

        self.min_confidence = role_thresholds.get(role, 0.55)
        self.required_warmup = 50

        self.logger.info(
            f"Initialized {role}_{timeframe} detector:\n"
            f"  Role: {role}\n"
            f"  Timeframe: {timeframe}\n"
            f"  Min confidence threshold: {self.min_confidence}\n"
            f"  Required warmup: {self.required_warmup}"
        )

    def get_required_bars(self) -> Dict[str, int]:
        return {self.timeframe: self.required_warmup}

    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """
        Анализирует готовые CUSUM данные из БД и формирует сигнал.
        CUSUM уже рассчитан при агрегации свечей.

        ✅ ИСПРАВЛЕНИЯ v3:
        1. Инициализация всех переменных в начале
        2. Валидация cusum_state как INTEGER перед использованием
        3. Проверка NaN ПЕРЕД преобразованием типов
        4. Единая точка нормализации confidence
        5. Устранены дубликаты кода
        6. Улучшено логирование ошибок
        7. ✅ НОВОЕ: Валидация консистентности ok + reason
        """
        self.logger.debug(f"[{self.role}] Analyzing {self.timeframe}")

        # ═══════════════════════════════════════════════════════════
        # 1. ВАЛИДАЦИЯ ДАННЫХ
        # ═══════════════════════════════════════════════════════════
        if self.timeframe not in data:
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "insufficient_data",
                "metadata": {"error": f"no_data_for_{self.timeframe}"}
            })

        df = data[self.timeframe]

        if not validate_market_data({self.timeframe: df}):
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "invalid_data",
                "metadata": {"timeframe": self.timeframe}
            })

        if len(df) < self.required_warmup:
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "insufficient_warmup",
                "metadata": {"required": self.required_warmup, "actual": len(df)}
            })

        # ═════════════════════════════════════════════════════════════
        # 2. ИНИЦИАЛИЗАЦИЯ ПЕРЕМЕННЫХ (для избежания UnboundLocalError)
        # ═══════════════════════════════════════════════════════════════
        cusum_state_raw = None
        cusum_conf_raw = None
        cusum_zscore_raw = None

        # ═══════════════════════════════════════════════════════════
        # 3. ЧТЕНИЕ ГОТОВЫХ CUSUM ДАННЫХ ИЗ БД
        # ═══════════════════════════════════════════════════════════
        try:
            # Проверка наличия необходимых колонок
            required_cols = ['cusum_state', 'cusum_conf', 'cusum_reason',
                             'cusum_zscore', 'cusum_pos', 'cusum_neg']
            missing_cols = [col for col in required_cols if col not in df.columns]

            if missing_cols:
                self.logger.error(
                    f"[{self.role}] Missing CUSUM columns: {missing_cols}. "
                    f"Available columns: {list(df.columns)[:20]}"
                )
                return normalize_signal({
                    "ok": False,
                    "direction": 0,
                    "confidence": 0.0,
                    "reason": "missing_cusum_data",
                    "metadata": {"missing_columns": missing_cols}
                })

            # ✅ БЕЗОПАСНОЕ извлечение скалярных значений
            try:
                # ✅ ШАГ 1: Извлекаем raw значения
                cusum_state_raw = df['cusum_state'].iloc[-1]
                cusum_conf_raw = df['cusum_conf'].iloc[-1]
                cusum_zscore_raw = df['cusum_zscore'].iloc[-1]
                cusum_pos_raw = df['cusum_pos'].iloc[-1]
                cusum_neg_raw = df['cusum_neg'].iloc[-1]
                cusum_reason_raw = df['cusum_reason'].iloc[-1]

                # ✅ ШАГ 2: Проверяем NaN ПЕРЕД преобразованием типов
                if pd.isna(cusum_state_raw) or pd.isna(cusum_conf_raw):
                    self.logger.warning(
                        f"[{self.role}] NaN in CUSUM data: "
                        f"state={cusum_state_raw}, conf={cusum_conf_raw}"
                    )
                    return normalize_signal({
                        "ok": False,
                        "direction": 0,
                        "confidence": 0.0,
                        "reason": "invalid_cusum_data",
                        "metadata": {
                            "cusum_state": "NaN" if pd.isna(cusum_state_raw) else str(cusum_state_raw),
                            "cusum_conf": "NaN" if pd.isna(cusum_conf_raw) else str(cusum_conf_raw),
                            "reason": "null_values"
                        }
                    })

                # ✅ ШАГ 3: Преобразуем типы с валидацией
                try:
                    # cusum_state должен быть INTEGER: 1 (BUY), -1 (SELL), 0 (FLAT)
                    cusum_state = int(cusum_state_raw)

                    # ✅ Валидируем что значение корректно
                    if cusum_state not in (1, -1, 0):
                        self.logger.warning(
                            f"[{self.role}] Invalid cusum_state={cusum_state}, "
                            f"expected 1, -1, or 0. Normalizing..."
                        )
                        # Нормализуем к ближайшему корректному значению
                        cusum_state = 1 if cusum_state > 0 else (-1 if cusum_state < 0 else 0)
                        self.logger.info(f"[{self.role}] Normalized cusum_state to {cusum_state}")

                except (ValueError, TypeError) as e:
                    self.logger.error(
                        f"[{self.role}] Failed to convert cusum_state '{cusum_state_raw}' to int: {e}"
                    )
                    return normalize_signal({
                        "ok": False,
                        "direction": 0,
                        "confidence": 0.0,
                        "reason": "cusum_state_conversion_error",
                        "metadata": {"error": str(e), "raw_value": str(cusum_state_raw)}
                    })

                # Преобразуем остальные значения
                cusum_conf = float(cusum_conf_raw)
                cusum_zscore = float(cusum_zscore_raw)
                cusum_pos = float(cusum_pos_raw) if not pd.isna(cusum_pos_raw) else 0.0
                cusum_neg = float(cusum_neg_raw) if not pd.isna(cusum_neg_raw) else 0.0
                cusum_reason = str(cusum_reason_raw) if not pd.isna(cusum_reason_raw) else "unknown"
                original_conf = cusum_conf

            except (ValueError, TypeError) as conv_err:
                self.logger.error(
                    f"[{self.role}] Type conversion error: {conv_err}. "
                    f"Raw values: state={cusum_state_raw}, conf={cusum_conf_raw}, "
                    f"zscore={cusum_zscore_raw}"
                )
                return normalize_signal({
                    "ok": False,
                    "direction": 0,
                    "confidence": 0.0,
                    "reason": "cusum_conversion_error",
                    "metadata": {"error": str(conv_err)}
                })

            # ✅ ДИАГНОСТИКА: Логируем значения после преобразования
            self.logger.debug(
                f"[{self.role}] CUSUM values (after conversion): "
                f"state={cusum_state} (type={type(cusum_state).__name__}), "
                f"conf={cusum_conf:.3f}, zscore={cusum_zscore:.3f}, "
                f"pos={cusum_pos:.3f}, neg={cusum_neg:.3f}, reason={cusum_reason}"
            )

        except (KeyError, IndexError) as e:
            self.logger.error(
                f"[{self.role}] Error accessing CUSUM data: {e}",
                exc_info=True
            )
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "cusum_read_error",
                "metadata": {"error": str(e), "role": self.role, "timeframe": self.timeframe}
            })

        # ═══════════════════════════════════════════════════════════
        # 4. ✅ ЕДИНАЯ ТОЧКА НОРМАЛИЗАЦИИ CONFIDENCE (БЕЗ ДУБЛИКАТОВ)
        # ═══════════════════════════════════════════════════════════

        # CUSUM confidence в БД - это abs(z-score)
        # Нужно нормализовать к [0, 1]
        # z-score обычно в диапазоне [-10, 10], но может быть и больше

        # Нормализация: берем абсолют и масштабируем
        # Используем формулу: conf_normalized = min(1.0, |z_score| / z_threshold)
        Z_SCORE_THRESHOLD = 3.0  # Значение, при котором confidence становится 1.0
        normalized_conf = min(1.0, abs(cusum_conf) / Z_SCORE_THRESHOLD)

        self.logger.debug(
            f"[{self.role}] Confidence normalization: "
            f"{original_conf:.3f} (raw z-score) → {normalized_conf:.3f} [0,1]"
        )

        # ═══════════════════════════════════════════════════════════
        # 5. ПРИМЕНЕНИЕ ПОРОГА CONFIDENCE (ROLE-BASED)
        # ═══════════════════════════════════════════════════════════

        # Сигнал OK только если:
        # - есть направление (не FLAT)
        # - нормализованная уверенность >= порога роли
        ok = (cusum_state != 0) and (normalized_conf >= self.min_confidence)

        # Определяем причину сигнала
        if cusum_state == 0:
            reason = "no_trend_signal"
        elif normalized_conf < self.min_confidence:
            reason = "low_confidence"
        else:
            reason = cusum_reason

        # Увеличиваем счетчик при валидном сигнале
        if ok:
            self.signal_count += 1
            self.logger.info(
                f"[{self.role}] ✅ Valid signal #{self.signal_count}: "
                f"direction={cusum_state}, conf={normalized_conf:.3f}"
            )

        # ═══════════════════════════════════════════════════════════
        # 6. ФОРМИРОВАНИЕ DETECTOR SIGNAL
        # ═══════════════════════════════════════════════════════════
        metadata = {
            "role": self.role,
            "timeframe": self.timeframe,
            "z_score": float(cusum_zscore),
            "cusum_pos": float(cusum_pos),
            "cusum_neg": float(cusum_neg),
            "signal_count": int(self.signal_count),
            "min_confidence_threshold": float(self.min_confidence),
            "original_cusum_conf": float(original_conf),  # ✅ Оригинальное значение
            "normalized_conf": float(normalized_conf),  # ✅ Нормализованное значение
            "z_score_threshold": float(Z_SCORE_THRESHOLD)  # ✅ Порог нормализации
        }

        signal = {
            "ok": bool(ok),
            "direction": int(cusum_state),  # ✅ INTEGER: 1, -1, 0
            "confidence": float(normalized_conf),  # ✅ Нормализованное значение [0, 1]
            "reason": reason,
            "metadata": metadata
        }

        self.logger.debug(
            f"[{self.role}] Final signal: direction={cusum_state}, "
            f"conf={normalized_conf:.3f} (orig={original_conf:.3f}), "
            f"ok={ok}, reason={reason}"
        )

        # ═══════════════════════════════════════════════════════════
        # 7. ✅ ВАЛИДАЦИЯ КОНСИСТЕНТНОСТИ ok + reason
        # ═══════════════════════════════════════════════════════════

        # Нормализуем сигнал ПЕРЕД валидацией
        result = normalize_signal(signal)

        # Множество причин, которые НЕ должны сопровождать ok=True
        INVALID_REASONS_FOR_OK_TRUE = {
            "invalid_data",
            "insufficient_data",
            "insufficient_warmup",
            "detector_error",
            "invalid_price",
            "outside_trading_hours",
            "daily_limit_reached"
        }

        # Множество причин, которые ДОЛЖНЫ сопровождать ok=True
        VALID_REASONS_FOR_OK_TRUE = {
            "trend_confirmed",
            "entry_confirmed",
            "hierarchical_confirmed",
            "three_level_confirmed"
        }

        # ✅ ПРОВЕРКА 1: ok=True но reason указывает на проблему
        if result["ok"] and result["reason"] in INVALID_REASONS_FOR_OK_TRUE:
            self.logger.error(
                f"⚠️ [{self.role}] INCONSISTENT SIGNAL: ok=True but reason='{result['reason']}'\n"
                f"  Direction: {result['direction']}\n"
                f"  Confidence: {result['confidence']:.3f}\n"
                f"  ➡️  Forcing ok=False to maintain consistency"
            )

            # ✅ Сохраняем оригинальную причину в metadata ПЕРЕД изменением
            original_reason = result["reason"]

            # Исправляем противоречие - создаем новый сигнал
            result = normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": original_reason,  # ✅ Используем переменную, не result["reason"]
                "metadata": {
                    **result.get("metadata", {}),
                    "fixed_inconsistency": True,
                    "original_ok": True,
                    "original_direction": result["direction"],
                    "original_confidence": result["confidence"]
                }
            })

        # ✅ ПРОВЕРКА 2: ok=False но reason НЕ в списке error-причин
        elif not result["ok"]:
            current_reason = result["reason"]

            # Проверяем что reason указывает на успешный сигнал
            is_success_reason = (
                    current_reason in VALID_REASONS_FOR_OK_TRUE or
                    (isinstance(current_reason, str) and current_reason.startswith("z="))
            )

            # Также проверяем что это НЕ явная ошибка
            is_not_error = current_reason not in INVALID_REASONS_FOR_OK_TRUE

            if is_success_reason and is_not_error:
                self.logger.warning(
                    f"⚠️ [{self.role}] REVERSE INCONSISTENCY: ok=False but reason='{current_reason}'\n"
                    f"  Direction: {result['direction']}\n"
                    f"  Confidence: {result['confidence']:.3f}\n"
                    f"  ➡️  Correcting reason based on actual state"
                )

                # ✅ Определяем правильную типизированную причину
                if result["direction"] == 0:
                    corrected_reason = "no_trend_signal"
                elif result["confidence"] < self.min_confidence:
                    corrected_reason = "weak_trend_signal"
                else:
                    corrected_reason = "detector_error"

                # ✅ Создаем НОВЫЙ сигнал с исправленной причиной
                result = normalize_signal({
                    "ok": False,
                    "direction": result["direction"],
                    "confidence": result["confidence"],
                    "reason": corrected_reason,  # ✅ Типизированное значение
                    "metadata": {
                        **result.get("metadata", {}),
                        "original_reason": current_reason,  # ✅ Сохраняем в metadata
                        "reason_corrected": True
                    }
                })

        # ✅ ФИНАЛЬНОЕ ЛОГИРОВАНИЕ
        self.logger.debug(
            f"[{self.role}] ✅ Signal after consistency check: "
            f"ok={result['ok']}, direction={result['direction']}, "
            f"conf={result['confidence']:.3f}, reason={result['reason']}"
        )

        return result

    def get_status(self) -> Dict:
        return {
            'role': self.role,
            'timeframe': self.timeframe,
            'signal_count': self.signal_count,
            'min_confidence': self.min_confidence,
            'required_warmup': self.required_warmup,
            'detector_type': 'cusum'
        }

    def reset_state(self):
        """Сброс счетчика сигналов"""
        self.signal_count = 0


class GlobalTrendDetector(Detector):
    """
    Fallback CUSUM-детектор для глобального тренда (5m).
    Используется в MLGlobalTrendDetector при недоступности ML-модели.
    """

    def __init__(self, timeframe: Timeframe = "5m",
                 name: str = None,
                 cusum_config: Optional[CusumConfig] = None):
        """
        Инициализация глобального CUSUM детектора.

        ✅ ИСПРАВЛЕНИЯ:
        1. Явно передаем name родительскому классу Detector
        2. Сохраняем timeframe как атрибут экземпляра
        3. Получаем параметры из CusumConfig вместо жесткого кодирования
        4. ✅ ПОЛНАЯ ИНИЦИАЛИЗАЦИЯ ВСЕХ АТРИБУТОВ

        Args:
            timeframe: таймфрейм анализа (5m)
            name: имя детектора для логирования
            cusum_config: конфигурация CUSUM
        """
        # ✅ ИСПРАВЛЕНИЕ: Передаем name родительскому классу Detector
        super().__init__(name=name or f"global_cusum_{timeframe}")

        self.logger.setLevel(logging.INFO)
        self.timeframe: Timeframe = timeframe

        # ✅ ИСПРАВЛЕНИЕ: Импорты были перемещены в начало файла
        if cusum_config is None:
            # Выбираем конфиг в зависимости от timeframe
            if timeframe == "5m":
                cusum_config = CUSUM_CONFIG_5M
            elif timeframe == "1m":
                cusum_config = CUSUM_CONFIG_1M
            else:
                # Fallback на 5m конфиг для неизвестных таймфреймов
                self.logger.warning(f"Unknown timeframe {timeframe}, using 5m config")
                cusum_config = CUSUM_CONFIG_5M

        self.cusum_config = cusum_config

        # ✅ ПОЛНАЯ ИНИЦИАЛИЗАЦИЯ ВСЕХ АТРИБУТОВ
        self.cusum_threshold = cusum_config.h * 2.0  # Порог теперь конфигурируемый
        self.required_warmup = cusum_config.normalize_window
        self.cusum_pos = 0.0
        self.cusum_neg = 0.0
        self.price_history = []
        self.max_history = 30

        self.logger.info(
            f"GlobalTrendDetector initialized:\n"
            f"  Timeframe: {timeframe}\n"
            f"  CUSUM threshold: {self.cusum_threshold:.2f}\n"
            f"  Required warmup: {self.required_warmup}\n"
            f"  Normalize window: {cusum_config.normalize_window}\n"
            f"  Config eps: {cusum_config.eps}\n"
            f"  Config h: {cusum_config.h}"
        )

    def get_required_bars(self) -> Dict[str, int]:
        """Минимальное количество баров для анализа"""
        return {self.timeframe: self.required_warmup}

    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """
        Анализ глобального тренда с помощью CUSUM
        ✅ ИСПРАВЛЕНИЯ:
        1. Использовать self.cusum_config.eps вместо жесткого значения
        2. Лучшее логирование
        """
        if self.timeframe not in data:
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "insufficient_data",
                "metadata": {"error": f"no_data_for_{self.timeframe}"}
            })

        df = data[self.timeframe]

        # Валидация
        if len(df) < self.required_warmup:
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "insufficient_warmup",
                "metadata": {"required": self.required_warmup, "actual": len(df)}
            })

        if df['close'].isna().any():
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "nan_in_prices"
            })

        # ✅ ИСПРАВЛЕНО: Явное преобразование к float
        current_price = float(df['close'].iloc[-1])
        if current_price <= 0:
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "invalid_price"
            })

        # Обновление истории цен
        self.price_history.append(current_price)
        if len(self.price_history) > self.max_history:
            self.price_history.pop(0)

        if len(self.price_history) < 10:
            return normalize_signal({
                "ok": False,
                "direction": 0,
                "confidence": 0.0,
                "reason": "price_history_warmup"
            })

        # Расчет среднего и стд цены
        mean_price = float(np.mean(self.price_history))
        std_price = float(np.std(self.price_history))

        if std_price == 0:
            std_price = 0.001  # Избегаем деления на 0

        # ✅ ИСПРАВЛЕНО: Все переменные теперь чистые float
        z_score = (current_price - mean_price) / std_price

        # Обновление CUSUM
        self.cusum_pos = max(0.0, self.cusum_pos + z_score - 0.5)
        self.cusum_neg = max(0.0, self.cusum_neg - z_score - 0.5)

        # ✅ ИСПРАВЛЕНИЕ: Генерация сигнала на основе конфига
        direction = 0
        confidence = 0.0
        reason = "no_signal"

        if self.cusum_pos > self.cusum_threshold:
            direction = 1  # BUY
            confidence = min(1.0, self.cusum_pos / (self.cusum_threshold * 2))
            reason = "global_cusum_long"
            self.logger.info(f"🟢 GlobalTrendDetector: BUY signal (cusum_pos={self.cusum_pos:.2f})")
            self.cusum_pos = 0.0  # reset после сигнала

        elif self.cusum_neg > self.cusum_threshold:
            direction = -1  # SELL
            confidence = min(1.0, self.cusum_neg / (self.cusum_threshold * 2))
            reason = "global_cusum_short"
            self.logger.info(f"🔴 GlobalTrendDetector: SELL signal (cusum_neg={self.cusum_neg:.2f})")
            self.cusum_neg = 0.0  # reset после сигнала

        ok = direction != 0

        metadata = {
            "detector_type": "cusum_fallback",
            "timeframe": self.timeframe,
            "z_score": float(z_score),
            "cusum_pos": float(self.cusum_pos),
            "cusum_neg": float(self.cusum_neg),
            "price_mean": float(mean_price),
            "price_std": float(std_price),
            "history_length": len(self.price_history),
            "threshold": float(self.cusum_threshold)
        }

        signal = {
            "ok": bool(ok),
            "direction": direction,
            "confidence": float(confidence),
            "reason": reason,
            "metadata": metadata
        }

        return normalize_signal(signal)

    def reset_state(self):
        """Сброс внутреннего состояния"""
        self.cusum_pos = 0.0
        self.cusum_neg = 0.0
        self.price_history = []

    def get_status(self) -> Dict:
        """Статус детектора для мониторинга"""
        return {
            "timeframe": self.timeframe,
            "cusum_pos": self.cusum_pos,
            "cusum_neg": self.cusum_neg,
            "history_length": len(self.price_history),
            "threshold": self.cusum_threshold,
            "ok": True
        }
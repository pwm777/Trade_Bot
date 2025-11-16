"""
multi_timeframe_confirmator.py
Иерархический конфирматор сигналов между таймфреймами (например, 1m и 10s).

Основное назначение:
--------------------
Проверяет согласованность направлений и уверенности сигналов между несколькими
детекторами (обычно "trend"), работающими на разных таймфреймах.
Не изменяет (не штрафует) уверенности подчинённых детекторов — только фильтрует,
агрегирует и формирует итоговый комбинированный сигнал.

Алгоритм работы:
----------------
1. **Валидация данных** — проверяет структуру входного словаря `data`
   и наличие минимального количества баров на каждом используемом таймфрейме.
2. **Запрос детекторов** — асинхронно вызывает:
     - `trend_detector.analyze(data)`
     - `entry_detector.analyze(data)`
3. **Пороговые фильтры (гейты)**:
     - Проверяет `ok` и `confidence` каждого сигнала.
     - Сравнивает их с `min_trend_confidence` и `min_entry_confidence`.
4. **Согласование направлений**:
     - Проверяет совпадение направлений (`BUY`/`SELL`) при включённом флаге `direction_agreement_required`.
     - При несогласии возвращает `direction=FLAT` и причину `"direction_disagreement"`.
5. **Агрегация уверенности**:
     - Вызывает `_calculate_combined_confidence(trend_signal, entry_signal)`,
       чтобы получить итоговую уверенность без дополнительного «штрафа» на волатильность.
6. **Формирование итогового сигнала**:
     - Собирает `DetectorSignal` с полями:
       `{ok, direction, confidence, reason, metadata}`.
     - В `metadata` сохраняет исходные confidence и направления обоих детекторов.
7. **Обновление состояния**:
     - Сохраняет сигнал в `_last_signal` через `_set_last_signal(out)`.
     - Обновляет `last_confirmed_direction` и счётчик `confirmation_count`.

Возвращаемое значение:
----------------------
`DetectorSignal` — стандартизированный словарь:
    {
      "ok": bool,
      "direction": "BUY" | "SELL" | "FLAT",
      "confidence": float,
      "reason": str,
      "metadata": dict
    }

Особенности:
------------
- Работает асинхронно (`async def analyze`).
- Не модифицирует сигналы дочерних детекторов, а только проверяет их согласие.
- Используется в составе `HierarchicalQualityTrendSystem` как часть трёхуровневой архитектуры.
- Поддерживает историю последних сигналов (`_update_trend_history`, `_update_entry_history`)
  для последующего анализа качества.
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, List, cast, Any
from datetime import datetime
import logging
from iqts_standards import (
    DetectorSignal,
    normalize_signal, validate_market_data,
    Detector,Timeframe,map_reason
)
from iqts_detectors import (
    RoleBasedOnlineTrendDetector,
    MLGlobalTrendDetector
)


class ThreeLevelHierarchicalConfirmator(Detector):
    """
    3-уровневый иерархический конфирматор
    Глобальный тренд (5m) → Локальный тренд (1m)
    """

    def __init__(self,
                 global_timeframe: Timeframe = "5m",
                 trend_timeframe: Timeframe = "1m",
                 name: str = "ThreeLevelHierarchicalConfirmator"):
        super().__init__(name)

        self.global_timeframe: Timeframe = global_timeframe
        self.trend_timeframe: Timeframe = trend_timeframe
        self._last_signal = None
        # ✅ ГЛОБАЛЬНЫЙ ДЕТЕКТОР (5m) — ML с fallback
        self.global_detector = MLGlobalTrendDetector(
            model_path="models/ml_global_5m_lgbm.joblib",
            use_fallback=True,
            name=f"exit_global_{global_timeframe}"
        )
        self.global_detector.timeframe = global_timeframe

        # ✅ ЛОКАЛЬНЫЙ ТРЕНД (1m) - CUSUM
        self.trend_detector = RoleBasedOnlineTrendDetector(
            timeframe=trend_timeframe,  # "1m"
            role="trend",
            name=f"trend_{trend_timeframe}"
        )


        # Пороги уверенности для каждого уровня
        self.min_global_confidence = 0.6
        self.min_trend_confidence = 0.55

        # Требование согласованности направлений
        self.direction_agreement_required = True

        # Веса для комбинирования уверенности
        self.weights = {
            'global': 0.5,  # 50% - глобальный тренд (самый важный)
            'trend': 0.3,  # 30% - локальный тренд
        }

        # История сигналов
        self.global_signal_history = []
        self.trend_signal_history = []
        self.max_history_length = 10
        self.disagreement_signal_history: List = []
        # Состояние
        self.last_confirmed_direction = None
        self.confirmation_count = 0

        # Настройка логгера confirmator'а
        self._setup_logging()

    def _setup_logging(self):
        """Настройка формата логирования в стандартном стиле"""
        # Убедимся, что у этого логгера нет дублирующих обработчиков
        if self.logger.handlers:
            for handler in self.logger.handlers:
                self.logger.removeHandler(handler)

        self.logger.setLevel(logging.INFO)

        # Создаем форматтер с нужным форматом
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Добавляем консольный обработчик
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        # Отключаем propagation чтобы избежать дублирования
        self.logger.propagate = False

    def get_required_bars(self) -> Dict[Timeframe, int]:
        """Объединенные требования от всех трех детекторов"""
        requirements = {}

        # Собираем требования от всех трех детекторов
        for detector in [self.global_detector, self.trend_detector]:
            detector_reqs = detector.get_required_bars()
            for tf, bars in detector_reqs.items():
                requirements[tf] = max(requirements.get(tf, 0), bars)

        return requirements

    async def warmup_from_history(self, data: Dict[Timeframe, pd.DataFrame]) -> None:
        """
        Разогрев всего 3-уровнего стека историческими барами.
        Вызывается один раз после загрузки истории в БД.
        """
        if not validate_market_data(data):
            self.logger.warning("Разогрев пропущен – данные плохие")
            return

        # порядок не критичен, но начинаем с самого верхнего
        for det, name in ((self.global_detector, "global"),
                          (self.trend_detector, "trend")):

            # у тренд/entry детекторов уже есть warmup_from_history
            if hasattr(det, "warmup_from_history"):
                tf = det.timeframe
                df = data.get(tf)
                if df is None or df.empty:
                    self.logger.warning("Нет данных %s для разогрева %s", tf, name)
                    continue

                ok = det.warmup_from_history(df)  # он синхронный
                self.logger.info("%s разогрет: %s", name, "ОК" if ok else "FAIL")

            else:
                self.logger.info(f"{name} не умеет разогрев – пропускаем")

        self.logger.info("✅ warmup TwoLevelConfirmator ok")

    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """
        2-уровневый анализ с улучшенной диагностикой
        """
        self.logger.info(
            f"analyze() called with data keys: {list(data.keys())}"
        )

        # ДИАГНОСТИКА: логируем структуру данных
        for tf, df in data.items():
            bars_count = len(df) if isinstance(df, pd.DataFrame) else 0

            if bars_count > 0:
                self.logger.debug(
                    f" {tf} first timestamp: {df.index[0] if hasattr(df.index[0], 'strftime') else df.index[0]}")
                self.logger.debug(
                    f" {tf} last timestamp: {df.index[-1] if hasattr(df.index[-1], 'strftime') else df.index[-1]}")

        # 1. Валидация данных
        if not validate_market_data(data):
            self.logger.warning(f" validate_market_data FAILED")
            out = self._error_signal("invalid_data", "validate_market_data_failed", {})
            self._set_last_signal(out)
            self._log_result(out)
            return out

        # 2. Проверка прогрева с детальной диагностикой
        required = self.get_required_bars()

        for timeframe, min_bars in required.items():
            tf = cast(Timeframe, timeframe)
            df_tf = data.get(tf)
            have_bars = len(df_tf) if isinstance(df_tf, pd.DataFrame) else 0

            if df_tf is None or have_bars < int(min_bars):
                self.logger.warning(f" Insufficient data for {tf}: need {min_bars}, have {have_bars}")
                out = self._error_signal(
                    "insufficient_data",
                    f"warmup_not_satisfied_{tf}",
                    {"timeframe": tf, "required": min_bars, "have": have_bars}
                )
                self._set_last_signal(out)
                self._log_result(out)
                return out

        # 3. LEVEL 1: Анализ глобального тренда (5m) с диагностикой
        self.logger.info(f" Calling global_detector.analyze()...")
        global_signal = await self.global_detector.analyze(data)

        self.logger.info(f" Global detector result: ok={global_signal.get('ok')}, "
                         f"direction={global_signal.get('direction')}, "
                         f"confidence={global_signal.get('confidence'):.2f}, "
                         f"reason={global_signal.get('reason')}")

        global_conf = float(global_signal.get("confidence", 0.0))
        global_dir = int(global_signal.get("direction", 0))

        # ✅ ИСПРАВЛЕНО: Используем исходный reason от детектора
        if not global_signal.get("ok", False):
            original_reason = global_signal.get("reason", "no_global_trend")
            out = self._error_signal(
                original_reason,
                "global_detector_not_ok",
                {"global_reason": original_reason}
            )
            self._set_last_signal(out)
            self._log_result(out)
            return out

        if global_dir != 0 and global_conf < self.min_global_confidence:
            out = self._error_signal(
                "weak_global_trend",
                "global_conf_below_threshold",
                {
                    "threshold": self.min_global_confidence,
                    "confidence": global_conf
                }
            )
            self._set_last_signal(out)
            self._log_result(out)
            return out

        # 4. LEVEL 2: Анализ локального тренда (1m)
        self.logger.info(f" Calling trend_detector.analyze()...")
        trend_signal = await self.trend_detector.analyze(data)
        self._update_trend_history(trend_signal)

        self.logger.info(f" Trend detector result: ok={trend_signal.get('ok')}, "
                         f"direction={trend_signal.get('direction')}, "
                         f"confidence={trend_signal.get('confidence'):.2f}, "
                         f"reason={trend_signal.get('reason')}")

        # ✅ ИСПРАВЛЕНО: Обработка слабого или отсутствующего тренда
        trend_conf = float(trend_signal.get("confidence", 0.0))
        trend_dir = int(trend_signal.get("direction", 0))
        trend_ok = trend_signal.get("ok", False)

        # Если confidence ниже порога → считаем слабым, но не блокируем
        if trend_dir != 0 and trend_conf < self.min_trend_confidence:
            self.logger.info(
                f" Weak trend signal: conf={trend_conf:.2f} < threshold={self.min_trend_confidence}. "
                f"Treating as FLAT, continuing with global direction."
            )
            trend_signal = normalize_signal({
                "ok": True,
                "direction": 0,
                "confidence": 0.0,
                "reason": "no_trend_signal",
                "metadata": {
                    **trend_signal.get("metadata", {}),
                    "original_confidence": trend_conf,
                    "original_direction": trend_dir,
                    "reason_override": "weak_confidence_below_threshold"
                }
            })
            trend_dir = 0
            trend_conf = 0.0

        # Если trend_detector вернул ok=False → логируем, но продолжаем
        elif not trend_ok:
            self.logger.info(
                f" Trend detector returned ok=False (reason={trend_signal.get('reason')}). "
                f"Global signal is strong - continuing analysis with trend as FLAT."
            )
            original_reason = trend_signal.get("reason", "no_trend_signal")
            safe_reason = map_reason(str(original_reason))

            trend_signal = normalize_signal({
                "ok": True,
                "direction": 0,
                "confidence": 0.0,
                "reason": safe_reason,
                "metadata": {
                    **trend_signal.get("metadata", {}),
                    "original_ok": False,
                    "original_reason": original_reason,
                    "reason_override": "detector_not_ok_but_continued"
                }
            })
            trend_dir = 0
            trend_conf = 0.0

        # ✅ ЕДИНАЯ ПРОВЕРКА согласованности через _check_two_level_consistency
        consistency = self._check_two_level_consistency(global_signal, trend_signal)

        consistent = consistency['consistent']
        consistency_reason = consistency['reason']
        final_direction = consistency['final_direction']

        self.logger.info(
            f"Consistency check: consistent={consistent}, reason={consistency_reason}, "
            f"final_dir={final_direction}"
        )

        # ✅ ИСПРАВЛЕНИЕ: При несогласии возвращаем ok=False с метаданными
        if not consistent:
            if consistency_reason == 'direction_disagreement':
                # ✅ Генерируем correlation_id
                from iqts_standards import create_correlation_id
                correlation_id = create_correlation_id()

                # Сохраняем метаданные для кэширования
                result_dict = {
                    'ok': False,
                    'direction': 0,
                    'confidence': 0.0,
                    'reason': 'direction_disagreement',
                    'metadata': {
                        'global_direction': consistency['global_direction'],
                        'global_confidence': consistency['global_confidence'],
                        'trend_direction': consistency['trend_direction'],
                        'trend_confidence': consistency['trend_confidence'],
                        'extra': {
                            'correlation_id': correlation_id,
                            'global_reason': global_signal.get('reason', ''),
                            'trend_reason': trend_signal.get('reason', '')
                        }
                    }
                }

                # ✅ НОРМАЛИЗУЕМ результат до DetectorSignal
                result = normalize_signal(result_dict)

                self.logger.warning(
                    f"⚠️ DIRECTION DISAGREEMENT: global={consistency['global_direction']} "
                    f"vs trend={consistency['trend_direction']} - will cache for later"
                )

                # ✅ Используем существующий список global_signal_history
                self._update_signal_history(
                    result,
                    self.global_signal_history,
                    level_name="global_disagreement"
                )

                self._set_last_signal(result)
                self._log_result(result)
                return result

            else:
                # Другие причины несогласия (weak_signals и т.д.)
                result = self._error_signal(
                    consistency_reason,
                    "consistency_check_failed",
                    {
                        'global_direction': consistency.get('global_direction', 0),
                        'trend_direction': consistency.get('trend_direction', 0),
                        'global_confidence': consistency.get('global_confidence', 0.0),
                        'trend_confidence': consistency.get('trend_confidence', 0.0)
                    }
                )
                self._set_last_signal(result)
                self._log_result(result)
                return result

        # 8. Комбинируем уверенность
        combined_confidence = self._calculate_weighted_confidence(
            global_signal, trend_signal)

        self.logger.info(f" Combined confidence: {combined_confidence:.3f} "
                         f"(global: {global_conf:.3f}, trend: {trend_conf:.3f})")

        # 9. Формируем итоговый сигнал
        from iqts_standards import create_correlation_id
        correlation_id = create_correlation_id()

        # ✅ ПРАВИЛЬНАЯ ЛОГИКА reason:
        if final_direction == 0:
            final_reason = "no_trend_signal"
        elif consistency_reason == "global_flat_confirmed":
            final_reason = "trend_confirmed"  # Оба согласны, но FLAT
        else:
            final_reason = "two_level_confirmed"

        out = normalize_signal({
            "ok": True,
            "direction": final_direction,
            "confidence": combined_confidence,
            "reason": final_reason,  # ✅ НЕ "invalid_data"!
            "metadata": {
                "stage": "two_level_confirmator",
                "global_timeframe": self.global_timeframe,
                "trend_timeframe": self.trend_timeframe,
                "global_confidence": global_conf,
                "trend_confidence": trend_conf,
                "global_direction": global_dir,
                "trend_direction": trend_dir,
                "weighted_confidence": combined_confidence,
                "confirmation_count": self.confirmation_count,
                "consistency": consistency,
                "global_trend_strength": global_signal.get("metadata", {}).get("global_trend_strength"),
                "trend_quality": global_signal.get("metadata", {}).get("trend_quality"),
                "extra": {
                    "correlation_id": correlation_id
                }
            }
        })

        # СОХРАНЕНИЕ СОСТОЯНИЯ И ЛОГИРОВАНИЕ
        self._set_last_signal(out)
        self.last_confirmed_direction = final_direction
        self.confirmation_count += 1
        self._log_result(out)

        return out

    def update_parameters(self, **kwargs):
        """Обновление параметров конфирмации (для адаптации под рынок без перезагрузки)"""
        updated = {}
        if 'min_global_confidence' in kwargs:
            self.min_global_confidence = float(kwargs['min_global_confidence'])
            updated['min_global_confidence'] = self.min_global_confidence
        if 'min_trend_confidence' in kwargs:
            self.min_trend_confidence = float(kwargs['min_trend_confidence'])
            updated['min_trend_confidence'] = self.min_trend_confidence
        if 'direction_agreement_required' in kwargs:
            self.direction_agreement_required = bool(kwargs['direction_agreement_required'])
            updated['direction_agreement_required'] = self.direction_agreement_required
        if 'weights' in kwargs and isinstance(kwargs['weights'], dict):
            # Проверим корректность весов
            w = kwargs['weights']
            if set(w.keys()) == {'global', 'trend'}:
                total = sum(w.values())
                if abs(total - 1.0) > 1e-6:
                    # Нормализуем, если сумма ≠ 1
                    self.weights = {k: v / total for k, v in w.items()}
                else:
                    self.weights = w.copy()
                updated['weights'] = self.weights
        if updated:
            self.logger.info(f" Parameters updated: {updated}")

    def get_recent_performance(self) -> Dict[str, Any]:
        """Анализ недавней производительности трёх уровней"""
        result: Dict[str, Any] = {'analyzed_levels': []}

        for level_name, history in [
            ('global', self.global_signal_history),
            ('trend', self.trend_signal_history),
        ]:
            if len(history) < 3:
                result[f'{level_name}_status'] = 'insufficient_history'
                continue

            recent = history[-10:]
            valid = [h for h in recent if h['ok']]

            if not valid:
                result[f'{level_name}_status'] = 'no_valid_signals'
                continue

            success_rate = len(valid) / len(recent)
            avg_conf = np.mean([h['confidence'] for h in valid])
            directions = [h['direction'] for h in valid if h['direction'] != 0]
            dir_changes = sum(1 for i in range(1, len(directions)) if directions[i] != directions[i-1]) if len(directions) > 1 else 0

            result[f'{level_name}_status'] = 'ok'
            result[f'{level_name}_metrics'] = {
                'signals_analyzed': len(recent),
                'valid_signals': len(valid),
                'success_rate': float(success_rate),
                'avg_confidence': float(avg_conf),
                'direction_changes': dir_changes,
                'stable_direction_ratio': (len(directions) - dir_changes) / len(directions) if directions else 1.0
            }
            result['analyzed_levels'].append(level_name)

        # Общая статистика подтверждений
        if self.trend_signal_history:
            recent_trend = self.trend_signal_history[-10:]
            total_recent = len(recent_trend)
            confirmed = sum(1 for h in recent_trend if h.get('signal', {}).get('ok') and h['direction'] == self.last_confirmed_direction)
            result['overall_confirmation_rate'] = confirmed / total_recent if total_recent else 0.0

        return result

    def _log_result(self, signal: DetectorSignal):
        """Логирование результата анализа"""
        self.logger.info(f"Result: ok={signal.get('ok')}, "
                         f"direction={signal.get('direction')}, "
                         f"confidence={signal.get('confidence'):.2f}, "
                         f"reason={signal.get('reason')}")

    def _error_signal(self, reason: str, why: str, extra: Dict = None) -> DetectorSignal:
        """Создание сигнала об ошибке"""
        metadata = {"stage": "three_level_confirmator", "why": why}
        if extra:
            metadata.update(extra)

        return normalize_signal({
            "ok": False,
            "direction": 0,
            "confidence": 0.0,
            "reason": reason,
            "metadata": metadata
        })

    def _calculate_weighted_confidence(self,
                                       global_signal: DetectorSignal,
                                       trend_signal: DetectorSignal) -> float:
        """
        Взвешенное комбинирование уверенности двух уровней
        Глобальный тренд имеет наибольший вес (70%)
        """
        c_global = float(global_signal.get("confidence", 0.0))
        c_trend = float(trend_signal.get("confidence", 0.0))

        combined = (
                self.weights['global'] * c_global +
                self.weights['trend'] * c_trend
        )
        return max(0.0, min(1.0, combined))

    # Строки 498-512

    def _check_two_level_consistency(
            self,
            global_signal: DetectorSignal,
            trend_signal: DetectorSignal
    ) -> Dict[str, Any]:
        """
        Проверяет согласованность между глобальным и трендовым детекторами.

        Логика:
        1. FLAT на глобальном уровне (direction=0) - всегда валиден
        2. Направления совпадают - согласованы
        3. Trend=FLAT, но global сильный - может быть согласован (с пониженной уверенностью)
        4. Направления противоположны - несогласованы
        """
        global_dir = int(global_signal.get('direction', 0))
        trend_dir = int(trend_signal.get('direction', 0))
        global_conf = float(global_signal.get('confidence', 0.0))
        trend_conf = float(trend_signal.get('confidence', 0.0))

        # ═══════════════════════════════════════════════════════════
        # ПРИОРИТЕТ 1: Global = FLAT (direction=0)
        # ═══════════════════════════════════════════════════════════
        if global_dir == 0:
            self.logger.info(
                f"🔵 Global FLAT detected (conf={global_conf:.2f}). "
                f"Trend direction ({trend_dir}) is informational only."
            )
            return {
                'consistent': True,
                'reason': 'global_flat_confirmed',
                'final_direction': 0,
                'global_direction': global_dir,
                'trend_direction': trend_dir,
                'global_confidence': global_conf,
                'trend_confidence': trend_conf
            }

        # ═══════════════════════════════════════════════════════════
        # ПРИОРИТЕТ 2: Направления совпадают (включая оба FLAT)
        # ═══════════════════════════════════════════════════════════
        if global_dir == trend_dir:
            self.logger.info(
                f"✅ Directions MATCH: global={global_dir}, trend={trend_dir}"
            )
            return {
                'consistent': True,
                'reason': 'directions_aligned',
                'final_direction': global_dir,
                'global_direction': global_dir,
                'trend_direction': trend_dir,
                'global_confidence': global_conf,
                'trend_confidence': trend_conf
            }

        # ═══════════════════════════════════════════════════════════
        # ПРИОРИТЕТ 3: Global тренд, Trend = FLAT
        # ═══════════════════════════════════════════════════════════
        if global_dir != 0 and trend_dir == 0:
            # Проверяем силу глобального сигнала
            if global_conf >= self.min_global_confidence:
                self.logger.info(
                    f"⚡ Strong global trend (dir={global_dir}, conf={global_conf:.2f}) "
                    f"with FLAT local trend - accepting with reduced confidence"
                )
                return {
                    'consistent': True,
                    'reason': 'strong_global_overrides_flat_trend',
                    'final_direction': global_dir,
                    'global_direction': global_dir,
                    'trend_direction': trend_dir,
                    'global_confidence': global_conf,
                    'trend_confidence': trend_conf,
                    'adjusted_confidence': global_conf * 0.8  # Снижение на 20%
                }
            else:
                self.logger.warning(
                    f"⚠️ Weak global trend (conf={global_conf:.2f}) + FLAT local trend"
                )
                return {
                    'consistent': False,
                    'reason': 'weak_signals',
                    'final_direction': 0,
                    'global_direction': global_dir,
                    'trend_direction': trend_dir,
                    'global_confidence': global_conf,
                    'trend_confidence': trend_conf
                }

        # ═══════════════════════════════════════════════════════════
        # ПРИОРИТЕТ 4: Направления противоположны (BUY vs SELL)
        # ═══════════════════════════════════════════════════════════
        self.logger.warning(
            f"❌ Directions OPPOSITE: global={global_dir}, trend={trend_dir}"
        )
        return {
            'consistent': False,
            'reason': 'direction_disagreement',
            'final_direction': 0,
            'global_direction': global_dir,
            'trend_direction': trend_dir,
            'global_confidence': global_conf,
            'trend_confidence': trend_conf
        }

    def _set_last_signal(self, signal: DetectorSignal):
        """Сохраняет последний сигнал для мониторинга"""
        self._last_signal = signal

    def get_last_signal(self) -> Optional[DetectorSignal]:
        """Возвращает последний сигнал"""
        return self._last_signal

    def _update_signal_history(self,
                               signal: DetectorSignal,
                               history_list: List,
                               level_name: str = "unknown") -> None:
        """Универсальное обновление истории сигналов"""
        try:
            history_list.append({
                'timestamp': datetime.now(),
                'signal': signal,
                'ok': signal.get("ok", False),
                'direction': signal.get("direction") if signal.get("ok", False) else None,
                'confidence': signal.get("confidence", 0.0)
            })

            if len(history_list) > self.max_history_length:
                history_list.pop(0)

        except Exception as e:
            self.logger.error(f"Error updating {level_name} signal history: {e}")

    # ════════════════════════════════════════════════════════════
    # ЗАМЕНИТЬ существующие методы:
    # ════════════════════════════════════════════════════════════
    def _update_global_history(self, signal: DetectorSignal):
        """Обновление истории глобальных сигналов"""
        self._update_signal_history(signal, self.global_signal_history, "global")

    def _update_trend_history(self, signal: DetectorSignal):
        """Обновление истории трендовых сигналов"""
        self._update_signal_history(signal, self.trend_signal_history, "trend")

    def get_system_status(self) -> Dict:
        """Получение статуса 3-уровневой системы"""
        return {
            'global_timeframe': self.global_timeframe,
            'trend_timeframe': self.trend_timeframe,
            'confirmation_count': self.confirmation_count,
            'last_confirmed_direction': self.last_confirmed_direction,
            'global_history_length': len(self.global_signal_history),
            'trend_history_length': len(self.trend_signal_history),
            'trend_history_length_duplicate': len(self.trend_signal_history),
            'global_detector_status': self.global_detector.get_status(),
            'trend_detector_status': self.trend_detector.get_status(),
            'confidence_weights': self.weights,
            'parameters': {
                'min_global_confidence': self.min_global_confidence,
                'min_trend_confidence': self.min_trend_confidence,
                'direction_agreement_required': self.direction_agreement_required
            }
        }

    def reset_state(self):
        """Сброс состояния конфирматора"""
        self.global_signal_history = []
        self.trend_signal_history = []
        self.last_confirmed_direction = None
        self.confirmation_count = 0
        self.global_detector.reset_state()
        self.trend_detector.reset_state()

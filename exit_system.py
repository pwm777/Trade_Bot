# exit_system.py
from __future__ import annotations
from typing import Dict,  Tuple, TypedDict, Any, cast, Optional
from datetime import datetime, timedelta
import pandas as pd
import logging
from iqts_standards import (DetectorSignal,
            validate_market_data, Timeframe, normalize_direction,
                            DirectionLiteral)
from iqts_detectors import (RoleBasedOnlineTrendDetector, MLGlobalTrendDetector)
from risk_manager import Direction

class ExitDecision(TypedDict, total=False):
    should_exit: bool
    reason: str
    urgency: str
    confidence: float
    details: Dict[str, Any]
    pnl_pct: float
    type: str
    # ✅ Новые поля для управления стопами (Task 4)
    new_stop_loss: Optional[float]
    new_take_profit: Optional[float]
    trailing_type: Optional[str]
    stop_distance_pct: Optional[float]

class ExitSignalDetector:
    """
    Детектор сигналов на выход из позиции с каскадной логикой

    КЛЮЧЕВОЙ ПРИНЦИП:
    Младшие таймфреймы (1m) разворачивают старший (5m).
    5m не может развернуться без разворота младших.
    Поэтому выходим УПРЕЖДАЮЩЕ, не дожидаясь полного разворота 5m.
    """

    def __init__(self,
                 global_timeframe: Timeframe = "5m",
                 trend_timeframe: Timeframe = "1m"):

        self.global_timeframe: Timeframe = global_timeframe
        self.trend_timeframe: Timeframe = trend_timeframe

        # Детекторы для анализа разворота
        self.global_detector = MLGlobalTrendDetector(
            model_path="models/ml_global_5m_lgbm.joblib",
            use_fallback=True,
            name=f"exit_global_{global_timeframe}"
        )

        self.trend_detector = RoleBasedOnlineTrendDetector(
            role="trend",
            name=f"exit_trend_{trend_timeframe}"
        )

        # Пороги для каскадного анализа
        # Строки 50-55
        self.cascading_thresholds = {
            'both_levels_sum': 0.65,  # ✅ Снижено с 0.7 (для 2 уровней)
            'global_hint': 0.3,  # ✅ Без изменений
            'trend_min': 0.25,  # ✅ Минимум для 1m
        }

        # Классические пороги (запасной вариант)
        self.classic_thresholds = {
            'high_global_reversal': 0.75,
            'high_trend_weak': 0.65,
            'high_global_hint': 0.5,
            'medium_trend_weak': 0.65,
            'medium_trend_hint': 0.5,
            'low_total': 0.6
        }

        self.logger = logging.getLogger(self.__class__.__name__)

    async def analyze_exit_signal(self,
                                  data: Dict[Timeframe, pd.DataFrame],
                                  position_direction: DirectionLiteral) -> Dict:
        """
        Анализ сигнала на выход с приоритетом каскадной логики

        Приоритеты:
        0. Каскадный разворот (младшие → старший)
        1. Глобальный разворот (классический HIGH)
        2. Локальный разворот с глобальным намеком (HIGH)
        3. Локальное ослабление (MEDIUM)
        4. Разворот младших (MEDIUM)
        5. Общая уверенность (LOW)
        """

        if not validate_market_data(data):
            return {
                'should_exit': False,
                'reason': 'invalid_data',
                'urgency': 'low',
                'confidence': 0.0,
                'details': {}
            }

        # Анализируем все три уровня
        global_signal = await self.global_detector.analyze(data)
        trend_signal = await self.trend_detector.analyze(data)

        # Проверяем разворот и ослабление на каждом уровне
        exit_signals = {
            'global_reversal': self._check_reversal(global_signal, position_direction),
            'trend_weakening': self._check_weakening(trend_signal, position_direction),
            'trend_reversal': self._check_reversal(trend_signal, position_direction),
        }

        # Комбинируем сигналы с приоритетом каскадной логики
        exit_decision = self._combine_exit_signals(exit_signals, position_direction)

        return exit_decision

    def _check_reversal(self, signal: DetectorSignal, position_direction: 'DirectionLiteral') -> Dict[str, Any]:
        """
        Проверка полного разворота тренда
        Если мы в BUY, а сигнал показывает SELL - это разворот
        """
        if not signal.get("ok", False):
            return {
                'detected': False,
                'confidence': 0.0,
                'signal_direction': None,
                'signal_ok': False
            }

        signal_direction = signal.get("direction", 0)  # int: 1/-1/0
        signal_confidence = signal.get("confidence", 0.0)

        # Нормализуем position_direction к int
        pos_dir = normalize_direction(position_direction)  # 1/-1/0

        # Разворот = противоположное направление
        is_reversal = (pos_dir == 1 and signal_direction == -1) or (pos_dir == -1 and signal_direction == 1)

        return {
            'detected': is_reversal,
            'confidence': signal_confidence if is_reversal else 0.0,
            'signal_direction': signal_direction,
            'signal_ok': True
        }

    def _check_weakening(self, signal: DetectorSignal, position_direction: DirectionLiteral) -> Dict:
        """
        Проверка ослабления тренда
        Тренд в нашу сторону, но уверенность падает
        """
        signal_direction = signal.get("direction", "FLAT")
        signal_confidence = signal.get("confidence", 0.0)
        signal_ok = signal.get("ok", False)

        # Тренд в нашу сторону, но слабый
        is_same_direction = (position_direction == signal_direction)
        is_weak = signal_confidence < 0.65 or not signal_ok

        is_weakening = is_same_direction and is_weak

        return {
            'detected': is_weakening,
            'confidence': 1.0 - signal_confidence if is_weakening else 0.0,
            'signal_direction': signal_direction,
            'signal_ok': signal_ok
        }

    def _check_cascading_reversal(self, signals: Dict, position_direction: DirectionLiteral) -> Dict:
        """
        КЛЮЧЕВОЙ МЕТОД: Проверка каскадного разворота (2 уровня)

        Логика:
        1. Младший таймфрейм (1m) развернулся (detected=True)
        2. Его сила достаточна
        3. Глобальный (5m) показывает намек на разворот (>30%)
        → Разворот 5m НЕИЗБЕЖЕН, выходим упреждающе!
        """
        global_rev = signals['global_reversal']
        trend_rev = signals['trend_reversal']
        trend_weak = signals['trend_weakening']

        # УСЛОВИЕ 1: Оба уровня показывают проблему
        all_levels_detect = (
                (trend_rev['detected'] or trend_weak['detected']) and
                global_rev['detected']
        )

        # УСЛОВИЕ 2: Суммарная уверенность ДВУХ уровней
        trend_confidence = max(trend_rev['confidence'], trend_weak['confidence'])
        total_confidence = trend_confidence + global_rev['confidence']  # ✅ Только 2 уровня

        # УСЛОВИЕ 3: Глобальный показывает намек
        global_hint = global_rev['confidence'] >= self.cascading_thresholds['global_hint']

        # УСЛОВИЕ 4: Тренд достаточно силен
        trend_strong = trend_confidence >= self.cascading_thresholds['lower_tf_min']

        # УСЛОВИЕ 5: Взвешенная сила (только 1m, без весов)
        lower_tf_weighted = trend_confidence  # ✅ Только 1m

        # ФИНАЛЬНОЕ РЕШЕНИЕ
        cascading_exit = (
                all_levels_detect and
                total_confidence >= 0.65 and  # ✅ Снижено с 0.7 (было для 3 уровней)
                global_hint and
                trend_strong
        )

        if cascading_exit:
            self.logger.info(
                f"🔥 КАСКАДНЫЙ РАЗВОРОТ: "
                f"1m={trend_confidence:.2f} + "
                f"5m={global_rev['confidence']:.2f} = {total_confidence:.2f}"
            )

            return {
                'detected': True,
                'urgency': 'high',
                'reason': 'cascading_reversal',
                'confidence': total_confidence / 2.0,  # ✅ Средняя по ДВУМ уровням
                'details': {
                    'type': 'cascading',
                    'trend_confidence': trend_confidence,
                    'global_confidence': global_rev['confidence'],
                    'total_confidence': total_confidence,
                    'interpretation': (
                        f"Младший таймфрейм разворачивает старший: "
                        f"1m({trend_confidence:.2f}) → "
                        f"5m({global_rev['confidence']:.2f}). "
                        f"Разворот 5m неизбежен, выход упреждающий!"
                    )
                }
            }

        return {'detected': False}

    def _combine_exit_signals(self, signals: Dict, position_direction: DirectionLiteral) -> Dict:
        """
        Комбинируем сигналы с приоритетом каскадной логики

        Приоритеты (по убыванию):
        0. Каскадный разворот (младшие → старший) [HIGH]
        1. Глобальный разворот (5m полностью развернулся) [HIGH]
        2. Локальный + глобальный намек [HIGH]
        3. Локальное ослабление без глобального [MEDIUM]
        4. Разворот младших без глобального [MEDIUM]
        5. Общая взвешенная уверенность [LOW]
        """

        global_rev = signals['global_reversal']
        trend_weak = signals['trend_weakening']
        trend_rev = signals['trend_reversal']

        # ═══════════════════════════════════════════════════════════════
        # ПРИОРИТЕТ 0: КАСКАДНЫЙ РАЗВОРОТ (УПРЕЖДАЮЩИЙ ВЫХОД)
        # ═══════════════════════════════════════════════════════════════

        cascading = self._check_cascading_reversal(signals, position_direction)
        if cascading['detected']:
            return {
                'should_exit': True,
                'reason': cascading['reason'],
                'urgency': cascading['urgency'],
                'confidence': cascading['confidence'],
                'details': cascading['details']
            }

        # ═══════════════════════════════════════════════════════════════
        # КЛАССИЧЕСКИЕ УСЛОВИЯ (запасной вариант)
        # ═══════════════════════════════════════════════════════════════

        # Веса для расчета общей уверенности (используется только для LOW)
        weights = {'global': 0.6, 'trend': 0.4}

        total_confidence_weighted = (
                weights['global'] * global_rev['confidence'] +
                weights['trend'] * trend_weak['confidence']
        )

        should_exit = False
        urgency = 'low'
        reason = 'no_exit_signal'
        confidence = 0.0

        # ───────────────────────────────────────────────────────────────
        # ПРИОРИТЕТ 1: КРИТИЧЕСКИЙ - Полный разворот глобального тренда
        # ───────────────────────────────────────────────────────────────

        if global_rev['detected'] and global_rev['confidence'] > self.classic_thresholds['high_global_reversal']:
            should_exit = True
            urgency = 'high'
            reason = 'global_trend_reversal'
            confidence = global_rev['confidence']

        # ───────────────────────────────────────────────────────────────
        # ПРИОРИТЕТ 2: ВЫСОКИЙ - Локальный разворот + намек на глобальный
        # ───────────────────────────────────────────────────────────────

        elif trend_weak['detected'] and trend_weak['confidence'] > self.classic_thresholds['high_trend_weak']:
            if global_rev['confidence'] > self.classic_thresholds['high_global_hint']:
                should_exit = True
                urgency = 'high'
                reason = 'trend_weakening_with_global_hint'
                confidence = (trend_weak['confidence'] + global_rev['confidence']) / 2.0
            else:
                should_exit = True
                urgency = 'medium'
                reason = 'local_trend_weakening'
                confidence = trend_weak['confidence']

        # ───────────────────────────────────────────────────────────────
        # ПРИОРИТЕТ 4: НИЗКИЙ - Общая взвешенная уверенность
        # ───────────────────────────────────────────────────────────────

        elif total_confidence_weighted > self.classic_thresholds['low_total']:
            should_exit = True
            urgency = 'low'
            reason = 'combined_exit_confidence'
            confidence = total_confidence_weighted

        return {
            'should_exit': should_exit,
            'reason': reason,
            'urgency': urgency,
            'confidence': confidence,
            'details': {
                'global_reversal': global_rev,
                'trend_weakening': trend_weak,
                'trend_reversal': trend_rev,
                'position_direction': position_direction,
                'total_weighted': total_confidence_weighted
            }
        }


class AdaptiveExitManager:
    """
    Адаптивный менеджер выхода из позиций
    комбинирует жесткие уровни, сигналы (с каскадной логикой) и защиту прибыли
    """

    def __init__(self,
                 global_timeframe: Timeframe = "5m",
                 trend_timeframe: Timeframe = "1m"):

        # Детектор сигналов на выход (с каскадной логикой)
        self.exit_detector = ExitSignalDetector(
            global_timeframe=global_timeframe,
            trend_timeframe=trend_timeframe
        )

        # Параметры трейлинг стопа
        self.trailing_stop_activation = 0.015  # 1.5% прибыли
        self.trailing_stop_distance = 0.01  # 1% от пика

        # Параметры защиты прибыли
        self.breakeven_activation = 0.008  # 0.8% прибыли

        # Максимальное время удержания (адаптивное)
        self.max_hold_time_base = timedelta(hours=2)

        self.logger = logging.getLogger(self.__class__.__name__)

    def _calculate_pnl_pct(self,
                           entry_price: float,
                           current_price: float,
                           direction: DirectionLiteral) -> float:
        """Расчет PnL в процентах"""
        if direction == Direction.BUY:
            return (current_price - entry_price) / entry_price
        elif direction == Direction.SELL:
            return (entry_price - current_price) / entry_price
        else:
            return 0.0

    async def should_exit_position(self,
                                   position: Dict,
                                   market_data: Dict[Timeframe, pd.DataFrame],
                                   current_price: float) -> Tuple[bool, str, ExitDecision]:

        # В начале метода добавить строгую валидацию:
        if not position or not isinstance(position, dict):
            self.logger.error("Invalid position data")
            return False, "invalid_position", ExitDecision(should_exit=False, reason="invalid_position")

        signal = position.get('signal')
        if not signal or not isinstance(signal, dict):
            self.logger.error("Invalid signal in position")
            return False, "invalid_signal", ExitDecision(should_exit=False, reason="invalid_signal")

        # Проверка обязательных полей
        required_fields = ['direction', 'entry_price', 'stop_loss', 'take_profit']
        missing = [f for f in required_fields if f not in signal]
        if missing:
            self.logger.error(f"Missing required signal fields: {missing}")
            return False, "missing_signal_fields", ExitDecision(
                should_exit=False,
                reason=f"missing_fields: {missing}",
                details={"missing": missing}
            )
        opened_at = position['opened_at']
        direction = normalize_direction(signal.get('direction'))
        entry_price = signal.get('entry_price', 0.0)
        stop_loss = signal.get('stop_loss', 0.0)
        take_profit = signal.get('take_profit', 0.0)

        # Расчет текущей прибыли
        pnl_pct = self._calculate_pnl_pct(entry_price, current_price, direction)

        # ═══════════════════════════════════════════════════════════════
        # LAYER 1: ЖЕСТКИЕ ВЫХОДЫ (Защита капитала)
        # ═══════════════════════════════════════════════════════════════

        hard_exit = self._check_hard_exits(
            direction, current_price, stop_loss, take_profit, opened_at, pnl_pct
        )
        if hard_exit['should_exit']:
            self.logger.info(f"⛔ Hard exit: {hard_exit['reason']}")
            return True, hard_exit['reason'], cast(ExitDecision, hard_exit)

        # ═══════════════════════════════════════════════════════════════
        # LAYER 2: СИГНАЛЫ НА ВЫХОД (Каскадный анализ тренда)
        # ═══════════════════════════════════════════════════════════════

        signal_exit = await self.exit_detector.analyze_exit_signal(
            market_data, direction
        )

        # Логика выхода по сигналам с учетом urgency и PnL
        if signal_exit['should_exit']:
            urgency = signal_exit['urgency']

            # HIGH urgency: выходим ВСЕГДА (независимо от PnL)
            if urgency == 'high':
                if pnl_pct > 0:
                    self.logger.info(
                        f"🔴 HIGH urgency exit with PROFIT: {signal_exit['reason']} "
                        f"(PnL={pnl_pct:.2%}, conf={signal_exit['confidence']:.2f})"
                    )
                else:
                    self.logger.warning(
                        f"🔴 HIGH urgency exit with LOSS: {signal_exit['reason']} "
                        f"(PnL={pnl_pct:.2%}, conf={signal_exit['confidence']:.2f})"
                    )
                return True, "signal_exit_high", cast(ExitDecision, signal_exit)

            # MEDIUM urgency: выходим только при прибыли
            elif urgency == 'medium' and pnl_pct > 0:
                self.logger.info(
                    f"🟠 MEDIUM urgency exit with PROFIT: {signal_exit['reason']} "
                    f"(PnL={pnl_pct:.2%}, conf={signal_exit['confidence']:.2f})"
                )
                return True, "signal_exit_medium", cast(ExitDecision, signal_exit)

            # LOW urgency: игнорируем (слабый сигнал)
            elif urgency == 'low':
                self.logger.debug(
                    f"🟡 LOW urgency signal ignored: {signal_exit['reason']} "
                    f"(conf={signal_exit['confidence']:.2f})"
                )

        # ═══════════════════════════════════════════════════════════════
        # LAYER 3: ЗАЩИТА ПРИБЫЛИ (Трейлинг и break-even)
        # ═══════════════════════════════════════════════════════════════

        profit_exit = self._check_profit_protection(
            direction, current_price, entry_price, pnl_pct, position
        )
        if profit_exit['should_exit']:
            self.logger.info(
                f"💰 Profit protection exit: {profit_exit['reason']} "
                f"(PnL={pnl_pct:.2%})"
            )
            return True, profit_exit['reason'], cast(ExitDecision, profit_exit)

        # Позиция удерживается
        return False, "no_exit_condition", cast(ExitDecision, {
            'pnl_pct': pnl_pct,
            'signal_exit': signal_exit,
            'hard_exit': hard_exit,
            'profit_exit': profit_exit
        })

    def _check_hard_exits(self,
                          direction: DirectionLiteral,
                          current_price: float,
                          stop_loss: float,
                          take_profit: float,
                          opened_at: datetime,
                          pnl_pct: float) -> Dict:
        """Проверка жестких условий выхода"""

        # 1. Стоп-лосс
        if direction == Direction.BUY and current_price <= stop_loss:
            return {'should_exit': True, 'reason': 'stop_loss_hit', 'type': 'hard'}
        elif direction == Direction.SELL and current_price >= stop_loss:
            return {'should_exit': True, 'reason': 'stop_loss_hit', 'type': 'hard'}

        # 2. Тейк-профит
        if direction == Direction.BUY and current_price >= take_profit:
            return {'should_exit': True, 'reason': 'take_profit_hit', 'type': 'hard'}
        elif direction == Direction.SELL and current_price <= take_profit:
            return {'should_exit': True, 'reason': 'take_profit_hit', 'type': 'hard'}

        # 3. Адаптивное максимальное время
        max_hold_time = self.max_hold_time_base
        if pnl_pct > 0.02:  # 2%+ прибыли → держим дольше
            max_hold_time = self.max_hold_time_base * 1.5
        elif pnl_pct < -0.01:  # 1%+ убытка → закрываем быстрее
            max_hold_time = self.max_hold_time_base * 0.7

        hold_time = datetime.now() - opened_at
        if hold_time > max_hold_time:
            return {
                'should_exit': True,
                'reason': 'max_hold_time',
                'type': 'hard',
                'hold_time_hours': hold_time.total_seconds() / 3600,
                'pnl_pct': pnl_pct
            }

        return {'should_exit': False, 'reason': 'no_hard_exit', 'type': 'hard'}

    def _check_profit_protection(self,
                                 direction: DirectionLiteral,
                                 current_price: float,
                                 entry_price: float,
                                 pnl_pct: float,
                                 position: Dict) -> Dict:
        """Проверка защиты прибыли (трейлинг стоп, break-even)"""

        # Работаем только при прибыли
        if pnl_pct <= 0:
            return {'should_exit': False, 'reason': 'no_profit', 'type': 'protection'}

        # ✅ ИСПРАВЛЕНО: Правильная инициализация exit_tracking
        tracking = position.get('exit_tracking')
        if tracking is None:
            # Используем entry_price из сигнала, а не current_price
            tracking = {
                'peak_price': entry_price,
                'breakeven_moved': False,
                'trailing_active': False
            }
            position['exit_tracking'] = tracking

        # Обновляем пик цены
        if direction == Direction.BUY:
            tracking['peak_price'] = max(tracking['peak_price'], current_price)
        elif direction == Direction.SELL:
            tracking['peak_price'] = min(tracking['peak_price'], current_price)

        # 1. Break-even стоп (при небольшой прибыли)
        if pnl_pct >= self.breakeven_activation and not tracking['breakeven_moved']:
            tracking['breakeven_moved'] = True
            # Отмечаем флаг, физическое перемещение SL в update_position_stops()

        # 2. Трейлинг стоп (при существенной прибыли)
        if pnl_pct >= self.trailing_stop_activation:
            tracking['trailing_active'] = True

            # Расчет трейлинга
            if direction == 'BUY':
                peak_price = tracking['peak_price']
                trailing_stop = peak_price * (1 - self.trailing_stop_distance)
                if current_price <= trailing_stop:
                    return {
                        'should_exit': True,
                        'reason': 'trailing_stop_hit',
                        'type': 'protection',
                        'peak_price': peak_price,
                        'trailing_stop': trailing_stop,
                        'pnl_pct': pnl_pct
                    }
            else:  # SELL
                peak_price = tracking['peak_price']
                trailing_stop = peak_price * (1 + self.trailing_stop_distance)
                if current_price >= trailing_stop:
                    return {
                        'should_exit': True,
                        'reason': 'trailing_stop_hit',
                        'type': 'protection',
                        'peak_price': peak_price,
                        'trailing_stop': trailing_stop,
                        'pnl_pct': pnl_pct
                    }

        return {'should_exit': False, 'reason': 'no_protection_exit', 'type': 'protection'}

    def update_position_stops(self,
                              position: Dict,
                              current_price: float) -> Dict:
        signal = position['signal']
        direction = normalize_direction(signal.get('direction', 0))
        entry_price = signal.get('entry_price', 0.0)
        original_stop_loss = signal.get('stop_loss', 0.0)

        tracking = position.get('exit_tracking', {})

        # Расчет PnL
        pnl_pct = self._calculate_pnl_pct(entry_price, current_price, direction)

        new_stop_loss = original_stop_loss

        # Break-even стоп (безубыток + буфер)
        if pnl_pct >= self.breakeven_activation and tracking.get('breakeven_moved', False):
            if direction == Direction.BUY:
                new_stop_loss = entry_price * 1.002  # +0.2% буфер
            elif direction == Direction.SELL:
                new_stop_loss = entry_price * 0.998  # -0.2% буфер

        # Трейлинг стоп (двигается за ценой)
        if tracking.get('trailing_active', False):
            peak_price = tracking.get('peak_price', current_price)

            if direction == Direction.BUY:
                trailing_stop = peak_price * (1 - self.trailing_stop_distance)
                new_stop_loss = max(new_stop_loss, trailing_stop)  # Никогда не опускаем
            elif direction == Direction.SELL:
                trailing_stop = peak_price * (1 + self.trailing_stop_distance)
                new_stop_loss = min(new_stop_loss, trailing_stop)  # Никогда не поднимаем

        return {
            'stop_loss': new_stop_loss,
            'updated': new_stop_loss != original_stop_loss,
            'reason': 'trailing' if tracking.get('trailing_active') else 'breakeven'
        }

    # ========================================================================
    # РАСЧЁТ TRAILING STOP (НОВОЕ для DI)
    # ========================================================================

    def calculate_trailing_stop(
            self,
            current_price: float,
            entry_price: float,
            side: str,  # "LONG" или "SHORT"
            max_pnl_percent: float,
            current_stop_price: Optional[float] = None,
            symbol: str = "UNKNOWN"
    ) -> Dict[str, Any]:
        """
        Расчёт нового уровня trailing stop на основе максимального PnL.

        **НОВЫЙ МЕТОД для Dependency Injection в PositionManager.**

        Заменяет PositionManager.compute_trailing_level() — бизнес-логика расчёта
        trailing стопов должна быть в ExitManager, а не в PositionManager.

        Args:
            current_price: Текущая рыночная цена
            entry_price: Цена входа в позицию
            side: Направление позиции ("LONG" или "SHORT")
            max_pnl_percent: Максимальный достигнутый PnL (%)
            current_stop_price: Текущая цена стоп-лосса (для проверки выгодности)
            symbol: Торговый символ (для логов)

        Returns:
            Dict с ключами:
            - new_stop: float | None — новая цена стопа (или None если не выгодно)
            - beneficial: bool — выгодно ли обновление
            - reason: str — причина решения
            - distance_from_entry_pct: float — расстояние от входа (%)
            - trailing_pct: float — используемый процент trailing

        Examples:
            >>> exit_mgr = AdaptiveExitManager()
            >>> result = exit_mgr.calculate_trailing_stop(
            ...     current_price=3275.0,
            ...     entry_price=3250.0,
            ...     side="LONG",
            ...     max_pnl_percent=1.5,
            ...     current_stop_price=3240.0,
            ...     symbol="ETHUSDT"
            ... )
            >>> print(result['new_stop'])  # 3250.0 (безубыток)
            >>> print(result['beneficial'])  # True
        """
        try:
            # Валидация входных данных
            if current_price <= 0:
                raise ValueError(f"current_price must be positive, got {current_price}")

            if entry_price <= 0:
                raise ValueError(f"entry_price must be positive, got {entry_price}")

            if side not in ("LONG", "SHORT"):
                raise ValueError(f"side must be 'LONG' or 'SHORT', got {side}")

            # Получаем конфиг trailing (используем внутренние параметры класса)
            trailing_pct = self.trailing_stop_distance * 100  # Конвертируем в проценты
            min_distance_pct = 0.1  # 0.1% минимальное расстояние до текущей цены

            self.logger.debug(
                f"calculate_trailing_stop called for {symbol}: "
                f"side={side}, current={current_price:.2f}, entry={entry_price:.2f}, "
                f"max_pnl={max_pnl_percent:.2f}%, trailing={trailing_pct:.2f}%"
            )

            # Расчёт порога стопа от максимального PnL
            # Отступаем на trailing_pct от пика
            stop_pnl_threshold = max(0, max_pnl_percent - trailing_pct)

            self.logger.debug(
                f"Stop PnL threshold: {stop_pnl_threshold:.2f}% "
                f"(max_pnl={max_pnl_percent:.2f}% - trailing={trailing_pct:.2f}%)"
            )

            # Convert side string to Direction enum
            direction = Direction.BUY if side == "LONG" else Direction.SELL if side == "SHORT" else Direction.FLAT

            # Расчёт новой цены стопа от entry_price
            if direction == Direction.BUY:
                # Для лонга: стоп = entry * (1 + stop_pnl_threshold/100)
                new_stop = entry_price * (1 + stop_pnl_threshold / 100)

                # Проверка выгодности: новый стоп должен быть ВЫШЕ старого
                if current_stop_price is not None:
                    if new_stop <= current_stop_price:
                        return {
                            'new_stop': None,
                            'beneficial': False,
                            'reason': (
                                f'Not beneficial for LONG: new_stop {new_stop:.2f} <= '
                                f'current_stop {current_stop_price:.2f}'
                            ),
                            'distance_from_entry_pct': stop_pnl_threshold,
                            'trailing_pct': trailing_pct
                        }

                # Проверка минимального расстояния от текущей цены
                max_allowed_stop = current_price * (1 - min_distance_pct / 100)
                if new_stop >= max_allowed_stop:
                    return {
                        'new_stop': None,
                        'beneficial': False,
                        'reason': (
                            f'Stop too close to current price for LONG: '
                            f'new_stop {new_stop:.2f} >= max_allowed {max_allowed_stop:.2f}'
                        ),
                        'distance_from_entry_pct': stop_pnl_threshold,
                        'trailing_pct': trailing_pct
                    }

            elif direction == Direction.SELL:
                # Для шорта: стоп = entry * (1 - stop_pnl_threshold/100)
                new_stop = entry_price * (1 - stop_pnl_threshold / 100)

                # Проверка выгодности: новый стоп должен быть НИЖЕ старого
                if current_stop_price is not None:
                    if new_stop >= current_stop_price:
                        return {
                            'new_stop': None,
                            'beneficial': False,
                            'reason': (
                                f'Not beneficial for SHORT: new_stop {new_stop:.2f} >= '
                                f'current_stop {current_stop_price:.2f}'
                            ),
                            'distance_from_entry_pct': stop_pnl_threshold,
                            'trailing_pct': trailing_pct
                        }

                # Проверка минимального расстояния от текущей цены
                min_allowed_stop = current_price * (1 + min_distance_pct / 100)
                if new_stop <= min_allowed_stop:
                    return {
                        'new_stop': None,
                        'beneficial': False,
                        'reason': (
                            f'Stop too close to current price for SHORT: '
                            f'new_stop {new_stop:.2f} <= min_allowed {min_allowed_stop:.2f}'
                        ),
                        'distance_from_entry_pct': stop_pnl_threshold,
                        'trailing_pct': trailing_pct
                    }

            # Успешный расчёт
            self.logger.info(
                f"✅ Trailing stop calculated for {symbol}: "
                f"{side} new_stop={new_stop:.2f} (distance from entry: {stop_pnl_threshold:.2f}%)"
            )

            return {
                'new_stop': float(new_stop),
                'beneficial': True,
                'reason': f'Trailing stop updated: max_pnl={max_pnl_percent:.2f}%',
                'distance_from_entry_pct': float(stop_pnl_threshold),
                'trailing_pct': float(trailing_pct),
                'entry_price': float(entry_price),
                'current_price': float(current_price),
                # ✅ Новые поля для ExitDecision (Task 4)
                'new_stop_loss': float(new_stop),
                'new_take_profit': None,  # Не изменяется при trailing
                'trailing_type': 'adaptive_trailing',
                'stop_distance_pct': float(stop_pnl_threshold)
            }

        except Exception as e:
            self.logger.error(f"❌ Error calculating trailing stop for {symbol}: {e}")
            return {
                'new_stop': None,
                'beneficial': False,
                'reason': f'Error: {str(e)}',
                'distance_from_entry_pct': 0.0,
                'trailing_pct': 0.0,
                'error': str(e),
                # ✅ Новые поля для ExitDecision (Task 4) - ошибочный случай
                'new_stop_loss': None,
                'new_take_profit': None,
                'trailing_type': None,
                'stop_distance_pct': None
            }

    def update_trailing_state(self, position: Dict, current_price: float) -> Dict[str, Any]:
        """
        Централизованное управление peak_price, breakeven, trailing_active.
        Возвращает:
          {
            'new_stop_loss': Optional[float],
            'changed': bool,
            'reason': str,
            'tracking': updated_tracking
          }
        """
        signal = position.get("signal", {})
        direction_raw = signal.get("direction", 0)
        entry_price = signal.get("entry_price", 0.0)
        original_stop = signal.get("stop_loss", 0.0)

        if entry_price <= 0 or current_price <= 0:
            return {'changed': False, 'reason': 'invalid_price'}

        direction = normalize_direction(direction_raw)

        tracking = position.get("exit_tracking")
        if tracking is None:
            tracking = {
                "peak_price": entry_price,
                "breakeven_moved": False,
                "trailing_active": False
            }
            position["exit_tracking"] = tracking

        # Обновление peak_price
        if direction == Direction.BUY:
            tracking["peak_price"] = max(tracking["peak_price"], current_price)
        elif direction == Direction.SELL:
            tracking["peak_price"] = min(tracking["peak_price"], current_price)

        pnl_pct = (current_price - entry_price) / entry_price if direction == Direction.BUY else (
                                                                                                 entry_price - current_price) / entry_price if direction == Direction.SELL else 0.0

        new_stop = original_stop
        changed = False
        reason = "no_change"

        # breakeven
        if pnl_pct >= self.breakeven_activation and not tracking["breakeven_moved"]:
            tracking["breakeven_moved"] = True
            if direction == Direction.BUY:
                new_stop = entry_price * 1.002  # +0.2%
            elif direction == Direction.SELL:
                new_stop = entry_price * 0.998  # -0.2%
            changed = True
            reason = "breakeven_adjust"

        # trailing activation
        if pnl_pct >= self.trailing_stop_activation:
            tracking["trailing_active"] = True
            peak = tracking["peak_price"]
            if direction == Direction.BUY:
                trailing_stop = peak * (1 - self.trailing_stop_distance)
                if trailing_stop > new_stop:
                    new_stop = trailing_stop
                    changed = True
                    reason = "trailing_adjust"
            elif direction == Direction.SELL:
                trailing_stop = peak * (1 + self.trailing_stop_distance)
                if trailing_stop < new_stop:
                    new_stop = trailing_stop
                    changed = True
                    reason = "trailing_adjust"

        return {
            "new_stop_loss": new_stop if changed else None,
            "changed": changed,
            "reason": reason,
            "tracking": tracking,
            "pnl_pct": pnl_pct
        }

    def _get_trailing_config_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """
        Получить конфигурацию trailing stop для символа.

        **ВСПОМОГАТЕЛЬНЫЙ МЕТОД** для совместимости с PositionManager.

        Args:
            symbol: Торговый символ

        Returns:
            Dict с параметрами trailing stop
        """
        try:
            # Пытаемся загрузить из config.py
            from config import get_trailing_stop_config
            return get_trailing_stop_config(symbol)
        except Exception as e:
            self.logger.warning(
                f"Failed to load trailing config for {symbol}: {e}. "
                f"Using default values."
            )
            # Fallback на дефолтные значения класса
            return {
                "enabled": True,
                "trailing_percent": self.trailing_stop_distance * 100,  # 1%
                "min_profit_percent": self.trailing_stop_activation * 100,  # 1.5%
                "activation_delay_candles": 3,
                "max_updates_per_position": 20,
                "price_change_threshold_percent": 0.1,
                "min_stop_distance_pct": 0.1
            }
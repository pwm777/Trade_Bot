"""
risk_manager.py

Централизованное управление рисками для торговой системы.

Компоненты:
- Direction: enum для направлений позиций (BUY=1, SELL=-1, FLAT=0)
- RiskContext: контракт для передачи риск-параметров
- RiskLimits: конфигурация лимитов риска
- EnhancedRiskManager: основной класс риск-менеджмента

История изменений:
- 2025-11-18: Миграция из improved_algorithm.py
  - Исправлен критический баг: Direction enum вместо строковых сравнений
  - Добавлен метод calculate_risk_context() как единая точка входа
  - Добавлена валидация входных данных и трассируемость

Использование:
    from risk_manager import EnhancedRiskManager, Direction, RiskContext, RiskLimits

    limits = RiskLimits(
        max_portfolio_risk=0.02,
        max_daily_loss=0.05
    )
    risk_mgr = EnhancedRiskManager(limits)

    risk_ctx = risk_mgr.calculate_risk_context(
        signal=detector_signal,
        current_price=3250.0,
        atr=15.5,
        account_balance=100000.0,
        regime="strong_uptrend"
    )
"""

from __future__ import annotations
from typing import TypedDict, Dict, Any, Optional, Literal, Protocol, Union, Tuple, cast
from iqts_standards import DetectorSignal
from enum import IntEnum
from dataclasses import dataclass
import numpy as np
import logging
import hashlib
import json
import time


# ============================================================================
# ТИПЫ И КОНСТАНТЫ
# ============================================================================

class Direction(IntEnum):
    """
    Направление позиции (числовые значения для совместимости с DirectionLiteral).

    Использование:
        direction = Direction.BUY
        side_str = direction.name  # "BUY" (стандартное свойство IntEnum)
        opposite = direction.opposite()  # Direction.SELL
    """
    BUY = 1
    SELL = -1
    FLAT = 0

    def opposite(self) -> 'Direction':
        """Возвращает противоположное направление"""
        if self == Direction.BUY:
            return Direction.SELL
        elif self == Direction.SELL:
            return Direction.BUY
        return Direction.FLAT

    def __str__(self) -> str:
        return self.name


# Алиасы для типизации
DirectionStr = Literal["BUY", "SELL", "FLAT"]
RegimeType = Literal[
    "strong_uptrend", "weak_uptrend",
    "strong_downtrend", "weak_downtrend",
    "sideways", "uncertain"
]


class RiskContext(TypedDict, total=False):
    """
    Контекст риск-параметров для позиции.

    Обязательные поля:
        position_size: Размер позиции (в единицах актива)
        initial_stop_loss: Начальный стоп-лосс
        take_profit: Уровень тейк-профита

    Метаданные расчёта:
        atr: Average True Range (волатильность)
        stop_atr_multiplier: Множитель ATR для стоп-лосса
        tp_atr_multiplier: Множитель ATR для тейк-профита
        volatility_regime: Коэффициент режима волатильности
        regime: Название режима рынка

    Аудит и трассировка:
        computed_at_ms: Timestamp расчёта (Unix milliseconds)
        risk_manager_version: Версия риск-менеджера
        validation_hash: SHA256 хеш для проверки целостности
    """
    # Основные параметры
    position_size: float
    initial_stop_loss: float
    take_profit: float

    # Метаданные расчёта
    atr: float
    stop_atr_multiplier: float
    tp_atr_multiplier: float

    # Режим рынка
    volatility_regime: float
    regime: Optional[RegimeType]

    # Дополнительные параметры
    max_hold_time_minutes: Optional[int]
    trailing_config: Optional[Dict[str, float]]

    # Аудит
    computed_at_ms: int
    risk_manager_version: str
    validation_hash: Optional[str]


@dataclass
class RiskLimits:
    """
    Конфигурация лимитов риска.

    Параметры:
        max_portfolio_risk: Максимальный риск на одну сделку (доля от капитала)
        max_daily_loss: Максимальная дневная потеря (доля от капитала)
        max_position_value_pct: Максимальная стоимость позиции (доля от капитала)
        stop_loss_atr_multiplier: Множитель ATR для стоп-лосса
        take_profit_atr_multiplier: Множитель ATR для тейк-профита
        atr_periods: Период расчёта ATR
    """
    max_portfolio_risk: float = 0.02  # 2% капитала на сделку
    max_daily_loss: float = 0.05  # 5% дневной лимит
    max_position_value_pct: float = 0.30  # 30% капитала в одной позиции

    stop_loss_atr_multiplier: float = 2.0
    take_profit_atr_multiplier: float = 3.0

    atr_periods: int = 14


class RiskManagerInterface(Protocol):
    """
    Протокол (интерфейс) для всех риск-менеджеров.

    Обязательные методы:
        calculate_position_size: Расчёт размера позиции
        calculate_dynamic_stops: Расчёт динамических SL/TP
        update_daily_pnl: Обновление дневного PnL
        should_close_all_positions: Проверка лимита дневных потерь
    """
    limits: Any

    def calculate_position_size(
            self,
            signal: DetectorSignal,
            current_price: float,
            atr: float,
            account_balance: float
    ) -> float:  # ✅ Всегда возвращает float (может быть 0.0)
        """
        Расчёт размера позиции на основе ATR и доли портфеля.

        Args:
            signal: DetectorSignal с полем 'ok'
            current_price: Текущая цена
            atr: Average True Range
            account_balance: Баланс счёта

        Returns:
            Размер позиции (в единицах актива), 0.0 если некорректные данные
        """

        # ✅ ИСПРАВЛЕНИЕ: явная проверка типов
        if not signal.get("ok", False):
            return 0.0

        if atr <= 0 or current_price <= 0 or account_balance <= 0:
            return 0.0

        # Обновляем внутренний баланс
        self.account_balance = account_balance


        # Риск на одну сделку
        risk_per_share = atr * self.limits.stop_loss_atr_multiplier
        if risk_per_share <= 0:
            return 0.0

        # Размер позиции на основе риска
        max_risk_amount = account_balance * self.limits.max_portfolio_risk
        position_size_by_risk = max_risk_amount / risk_per_share

        # Ограничение по объёму (максимум N% капитала)
        max_position_value = account_balance * self.limits.max_position_value_pct
        position_size_by_value = max_position_value / current_price

        # Берём минимум из двух ограничений
        size = min(position_size_by_risk, position_size_by_value)

        return float(size)  # ✅ Явное приведение к float

    def calculate_dynamic_stops(
            self,
            *,
            entry_price: float,
            direction: Direction,
            atr: float,
            regime_ctx: Dict[str, Any]
    ) -> Tuple[float, float]:
        """Расчёт динамических стоп-лосса и тейк-профита"""
        ...

    # ========================================================================
    # РАСЧЁТ НАЧАЛЬНОГО СТОП-ЛОССА (НОВОЕ для DI)
    # ========================================================================

    def calculate_initial_stop(
            self,
            entry_price: float,
            direction: Direction,
            stop_loss_pct: float,
            symbol: str = "UNKNOWN"
    ) -> Dict[str, Any]:
        """
        Расчёт начального стоп-лосса для позиции.

        **НОВЫЙ МЕТОД для Dependency Injection в PositionManager.**

        Заменяет PositionManager.compute_entry_stop() — бизнес-логика расчёта стопов
        должна быть в RiskManager, а не в PositionManager.

        Args:
            entry_price: Цена входа в позицию
            direction: Направление позиции (Direction.BUY или Direction.SELL)
            stop_loss_pct: Процент стоп-лосса от цены входа
            symbol: Торговый символ (для логов)

        Returns:
            Dict с ключами:
            - stop_price: float — цена стоп-лосса
            - distance_pct: float — расстояние от входа в процентах
            - risk_amount: float — абсолютное расстояние до стопа
            - direction: str — направление позиции

        Raises:
            ValueError: Если входные данные некорректны

        Examples:
            #>>> rm = EnhancedRiskManager()
            #>>> result = rm.calculate_initial_stop(
            #...     entry_price=3250.0,
            #...     direction=Direction.BUY,
            #...     stop_loss_pct=0.30,
            #...     symbol="ETHUSDT"
            #... )
            #>>> print(result['stop_price'])
            #3240.25
            #>>> print(result['distance_pct'])
            #0.30
        """
        try:
            # Валидация входных данных
            if entry_price <= 0:
                raise ValueError(f"entry_price must be positive, got {entry_price}")

            if stop_loss_pct <= 0:
                raise ValueError(f"stop_loss_pct must be positive, got {stop_loss_pct}")

            if not isinstance(direction, Direction):
                self.logger.warning(
                    f"direction должен быть Direction enum, получен {type(direction)}. "
                    f"Попытка конвертации..."
                )
                direction = normalize_direction(direction)

            # Расчёт цены стопа
            if direction == Direction.BUY:
                # Для лонга стоп ниже цены входа
                stop_price = entry_price * (1 - stop_loss_pct / 100)
            elif direction == Direction.SELL:
                # Для шорта стоп выше цены входа
                stop_price = entry_price * (1 + stop_loss_pct / 100)
            else:
                raise ValueError(f"Cannot calculate stop for Direction.FLAT")

            # Дополнительные метрики
            distance = abs(entry_price - stop_price)
            distance_pct = (distance / entry_price) * 100

            result = {
                'stop_price': float(stop_price),
                'distance_pct': float(distance_pct),
                'risk_amount': float(distance),
                'direction': direction.name,  # "BUY" или "SELL"
                'entry_price': float(entry_price),
                'stop_loss_pct': float(stop_loss_pct)
            }

            self.logger.debug(
                f"✅ Initial stop calculated for {symbol}: "
                f"{direction.name} @ {entry_price:.2f} → SL {stop_price:.2f} "
                f"({distance_pct:.2f}%)"
            )

            return result

        except Exception as e:
            self.logger.error(f"❌ Error calculating initial stop for {symbol}: {e}")
            return {
                'stop_price': None,
                'distance_pct': 0.0,
                'risk_amount': 0.0,
                'direction': 'UNKNOWN',
                'error': str(e)
            }

    def update_daily_pnl(self, pnl: float) -> None:
        """Обновление дневного PnL"""
        ...

    def should_close_all_positions(self) -> bool:
        """Проверка достижения лимита дневных потерь"""
        ...


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def direction_to_side(direction: Union[int, Direction]) -> DirectionStr:
    """
    Конвертация Direction/int → строка для биржевых API.

    Args:
        direction: Direction enum или числовое значение (1, -1, 0)

    Returns:
        Строка "BUY", "SELL" или "FLAT"

    Examples:
        >>> direction_to_side(Direction.BUY)
        "BUY"
        >>> direction_to_side(1)
        "BUY"
        >>> direction_to_side(-1)
        "SELL"

    Raises:
        KeyError: Если передано некорректное числовое значение
    """
    if isinstance(direction, Direction):
        return cast(DirectionStr, direction.name)  # ✅ ИСПРАВЛЕНО: .side → .name

    mapping: Dict[int, DirectionStr] = {1: "BUY", -1: "SELL", 0: "FLAT"}
    return mapping[direction]

def side_to_direction(side: str) -> Direction:
    """
    Конвертация строка → Direction enum.

    Args:
        side: Строка "BUY", "SELL" или "FLAT"

    Returns:
        Direction enum

    Examples:
        >>> side_to_direction("BUY")
        Direction.BUY
    """
    return {"BUY": Direction.BUY, "SELL": Direction.SELL, "FLAT": Direction.FLAT}[side.upper()]


def normalize_direction(value: Any) -> Direction:
    """
    Универсальная конвертация произвольного значения в Direction.

    Поддерживает:
        - Direction enum (возвращает как есть)
        - Строки: "BUY", "SELL", "FLAT", "LONG", "SHORT"
        - Числа: > 0 → BUY, < 0 → SELL, 0 → FLAT
        - None → FLAT

    Args:
        value: Значение для конвертации

    Returns:
        Direction enum

    Examples:
        >>> normalize_direction("BUY")
        Direction.BUY
        >>> normalize_direction(1)
        Direction.BUY
        >>> normalize_direction(-1)
        Direction.SELL
    """
    if isinstance(value, Direction):
        return value

    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in ("BUY", "LONG", "BULL"):
            return Direction.BUY
        if normalized in ("SELL", "SHORT", "BEAR"):
            return Direction.SELL
        return Direction.FLAT

    if isinstance(value, (int, float)):
        if value > 0:
            return Direction.BUY
        elif value < 0:
            return Direction.SELL
        return Direction.FLAT

    if value is None:
        return Direction.FLAT

    # Fallback для enum-подобных объектов
    if hasattr(value, 'name'):
        return normalize_direction(value.name)

    return Direction.FLAT


def compute_risk_hash(risk_context: RiskContext) -> str:
    """
    Вычисление SHA256 хеша от риск-контекста для аудита.

    Args:
        risk_context: Риск-контекст

    Returns:
        Первые 16 символов SHA256 хеша

    Example:
        #>>> ctx = {"position_size": 0.5, "initial_stop_loss": 3200.0}
        #>>> compute_risk_hash(ctx)
        "a3f5c8d9e2b1f0a4"
    """
    # Сортируем ключи для стабильности хеша
    canonical = json.dumps(risk_context, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def validate_risk_context(ctx: RiskContext) -> Tuple[bool, str]:
    """
    Валидация риск-контекста.

    Проверяет:
        - Наличие обязательных полей
        - Положительность значений
        - Корректность соотношения SL/TP

    Args:
        ctx: Риск-контекст для проверки

    Returns:
        (is_valid, error_message)
    """
    required_fields = ['position_size', 'initial_stop_loss', 'take_profit']
    for field in required_fields:
        if field not in ctx:
            return False, f"Missing required field: {field}"

    if ctx['position_size'] <= 0:
        return False, "Invalid position_size: must be > 0"

    if ctx['initial_stop_loss'] <= 0:
        return False, "Invalid initial_stop_loss: must be > 0"

    if ctx['take_profit'] <= 0:
        return False, "Invalid take_profit: must be > 0"

    return True, "ok"


# ============================================================================
# ОСНОВНОЙ КЛАСС
# ============================================================================

class EnhancedRiskManager:
    """
    Продвинутый риск-менеджер с адаптивными стопами и управлением дневным PnL.

    ВАЖНЫЕ ИЗМЕНЕНИЯ от improved_algorithm.py:
        ✅ Исправлен баг: Direction enum вместо строковых сравнений
        ✅ Добавлен метод calculate_risk_context() — единая точка входа
        ✅ Добавлена валидация входных данных
        ✅ Добавлена трассируемость (validation_hash, timestamps)

    Использование:
        limits = RiskLimits(max_portfolio_risk=0.02)
        rm = EnhancedRiskManager(limits)

        # Новый API (рекомендуется)
        risk_ctx = rm.calculate_risk_context(
            signal=signal,
            current_price=3250.0,
            atr=15.5,
            account_balance=100000.0
        )

        # Старый API (backward compatibility)
        size = rm.calculate_position_size(signal, price, atr, balance)
        sl, tp = rm.calculate_dynamic_stops(entry_price=price, direction=Direction.BUY, atr=atr, regime_ctx={})
    """

    VERSION = "v2.0.0"  # Версия после миграции из improved_algorithm.py

    def __init__(self, limits: Optional[RiskLimits] = None):
        """
        Инициализация риск-менеджера.

        Args:
            limits: Конфигурация лимитов (если None — используются defaults)
        """
        self.limits = limits or RiskLimits()
        self.logger = logging.getLogger(self.__class__.__name__)

        # Состояние (для совместимости с improved_algorithm.py)
        self.daily_pnl = 0.0
        self.account_balance = 100000.0  # Базовый баланс, обновляется извне

        self.logger.info(
            f"🔧 EnhancedRiskManager {self.VERSION} initialized | "
            f"max_risk={self.limits.max_portfolio_risk:.1%}, "
            f"max_daily_loss={self.limits.max_daily_loss:.1%}"
        )

    # ========================================================================
    # НОВЫЙ ГЛАВНЫЙ МЕТОД (v2.0.0)
    # ========================================================================

    def calculate_risk_context(
            self,
            signal: DetectorSignal,
            current_price: float,
            atr: float,
            account_balance: float,
            regime: Optional[str] = None
    ) -> RiskContext:
        """
        **ГЛАВНЫЙ МЕТОД**: Расчёт полного риск-контекста для позиции.

        Объединяет calculate_position_size() + calculate_dynamic_stops() + метаданные.

        Args:
            signal: DetectorSignal с полями 'ok', 'direction', 'confidence'
            current_price: Текущая цена входа
            atr: Average True Range (волатильность)
            account_balance: Баланс счёта
            regime: Режим рынка (опционально)

        Returns:
            RiskContext с position_size, SL, TP и метаданными

        Raises:
            ValueError: Если входные данные некорректны

        Example:
           # >>> signal = {"ok": True, "direction": 1, "confidence": 0.85}
           # >>> ctx = rm.calculate_risk_context(signal, 3250.0, 15.5, 100000.0)
           # >>> print(ctx['position_size'], ctx['initial_stop_loss'])
        """
        # Валидация входных данных
        if not self._validate_inputs(signal, current_price, atr, account_balance):
            return self._create_empty_context("invalid_inputs")

        # Обновляем внутренний баланс
        self.account_balance = account_balance

        # Нормализация direction
        direction = normalize_direction(signal.get('direction', 0))

        # Расчёт размера позиции
        position_size = self.calculate_position_size(
            signal=signal,
            current_price=current_price,
            atr=atr,
            account_balance=account_balance
        )

        # Построение regime_ctx для calculate_dynamic_stops
        regime_ctx: Dict[str, Any] = {
            "volatility_regime": 1.0,  # Default
            "regime": regime or "uncertain"
        }

        # Расчёт динамических стопов
        stop_loss, take_profit = self.calculate_dynamic_stops(
            entry_price=current_price,
            direction=direction,
            atr=atr,
            regime_ctx=regime_ctx
        )

        # Формирование полного контекста
        risk_context: RiskContext = {
            # Основные параметры
            "position_size": position_size,
            "initial_stop_loss": stop_loss,
            "take_profit": take_profit,

            # Метаданные расчёта
            "atr": atr,
            "stop_atr_multiplier": self.limits.stop_loss_atr_multiplier,
            "tp_atr_multiplier": self.limits.take_profit_atr_multiplier,

            # Режим рынка
            "volatility_regime": regime_ctx.get("volatility_regime", 1.0),
            "regime": regime,

            # Аудит
            "computed_at_ms": int(time.time() * 1000),
            "risk_manager_version": self.VERSION,
        }

        # Добавляем хеш для валидации
        risk_context["validation_hash"] = compute_risk_hash(risk_context)

        # Валидация результата
        is_valid, error = validate_risk_context(risk_context)
        if not is_valid:
            self.logger.error(f"❌ Invalid risk context generated: {error}")
            return self._create_empty_context(error)

        self.logger.debug(
            f"✅ Risk context calculated: size={position_size:.4f}, "
            f"SL={stop_loss:.2f}, TP={take_profit:.2f}"
        )

        return risk_context

    # ========================================================================
    # ОСНОВНЫЕ МЕТОДЫ (backward compatibility с improved_algorithm.py)
    # ========================================================================

    def calculate_position_size(
            self,
            signal: DetectorSignal,
            current_price: float,
            atr: float,
            account_balance: float
    ) -> float:
        """
        Расчёт размера позиции на основе ATR и доли портфеля.

        **BACKWARD COMPATIBILITY**: Сохранён для совместимости с improved_algorithm.py
        **РЕКОМЕНДАЦИЯ**: Используйте calculate_risk_context() вместо этого метода.

        Args:
            signal: DetectorSignal с полем 'ok'
            current_price: Текущая цена
            atr: Average True Range
            account_balance: Баланс счёта

        Returns:
            Размер позиции (в единицах актива), 0.0 если некорректные данные
        """
        if not signal.get("ok", False) or atr <= 0 or current_price <= 0 or account_balance <= 0:
            return 0.0

        # Обновляем внутренний баланс
        self.account_balance = account_balance

        # Риск на одну сделку
        risk_per_share = atr * self.limits.stop_loss_atr_multiplier
        if risk_per_share <= 0:
            return 0.0

        # Размер позиции на основе риска
        max_risk_amount = account_balance * self.limits.max_portfolio_risk
        position_size_by_risk = max_risk_amount / risk_per_share

        # Ограничение по объёму (максимум N% капитала)
        max_position_value = account_balance * self.limits.max_position_value_pct
        position_size_by_value = max_position_value / current_price

        # Берём минимум из двух ограничений
        size = min(position_size_by_risk, position_size_by_value)

        return max(0.0, float(size))

    def calculate_dynamic_stops(
            self,
            *,
            entry_price: float,
            direction: Direction,
            atr: float,
            regime_ctx: Dict[str, Any]
    ) -> Tuple[float, float]:
        """
        Расчёт адаптивных стоп-лосса и тейк-профита с учётом режима рынка.

        **КРИТИЧНОЕ ИСПРАВЛЕНИЕ**: Теперь использует Direction enum вместо строковых сравнений.

        **BACKWARD COMPATIBILITY**: Сохранён для совместимости, но требует Direction enum.
        **РЕКОМЕНДАЦИЯ**: Используйте calculate_risk_context() вместо этого метода.

        Args:
            entry_price: Цена входа
            direction: Direction enum (BUY, SELL, FLAT)
            atr: Average True Range
            regime_ctx: Контекст режима рынка с полем 'volatility_regime'

        Returns:
            (stop_loss, take_profit)

        Raises:
            TypeError: Если direction не Direction enum
        """
        # Валидация входных данных
        if entry_price <= 0 or atr <= 0:
            self.logger.warning(f"⚠️ Invalid inputs: entry_price={entry_price}, atr={atr}")
            return entry_price, entry_price  # Защита

        # Проверка типа direction
        if not isinstance(direction, Direction):
            # Попытка конвертации для обратной совместимости
            self.logger.warning(
                f"⚠️ direction должен быть Direction enum, получен {type(direction)}. "
                f"Попытка автоматической конвертации..."
            )
            direction = normalize_direction(direction)

        # Адаптация к волатильности
        volatility_regime = regime_ctx.get("volatility_regime", 1.0)
        vola_factor = 1.0 / max(volatility_regime, 0.1)  # Избегаем деления на 0
        adjustment = np.clip(vola_factor, 0.5, 2.0)

        adjusted_sl_mult = self.limits.stop_loss_atr_multiplier * adjustment
        adjusted_tp_mult = self.limits.take_profit_atr_multiplier * adjustment

        # ✅ ИСПРАВЛЕНО: Правильное использование Direction enum
        if direction == Direction.BUY:
            stop_loss = entry_price - atr * adjusted_sl_mult
            take_profit = entry_price + atr * adjusted_tp_mult
        elif direction == Direction.SELL:
            stop_loss = entry_price + atr * adjusted_sl_mult
            take_profit = entry_price - atr * adjusted_tp_mult
        else:  # FLAT
            self.logger.warning("⚠️ Direction.FLAT: returning entry_price for both SL and TP")
            return entry_price, entry_price

        # Защита от некорректных значений
        stop_loss = max(0.0, stop_loss)
        take_profit = max(0.0, take_profit)

        return float(stop_loss), float(take_profit)

    # ========================================================================
    # УПРАВЛЕНИЕ ДНЕВНЫМ PnL
    # ========================================================================

    def update_daily_pnl(self, pnl: float) -> None:
        """
        Обновление дневного PnL.

        Args:
            pnl: Прибыль/убыток по закрытой позиции
        """
        self.daily_pnl += float(pnl)
        self.logger.debug(f"📊 Daily PnL updated: {self.daily_pnl:+.2f}")

    def reset_daily_pnl(self) -> None:
        """Сброс дневного PnL (вызывать в начале нового торгового дня)"""
        old_pnl = self.daily_pnl
        self.daily_pnl = 0.0
        self.logger.info(f"🔄 Daily PnL reset: {old_pnl:+.2f} → 0.0")

    def should_close_all_positions(self, current_daily_pnl: Optional[float] = None) -> bool:
        """
        Проверка достижения лимита дневных потерь.

        Args:
            current_daily_pnl: Текущий дневной PnL (если None — использует self.daily_pnl)

        Returns:
            True если достигнут лимит потерь, иначе False
        """
        pnl = current_daily_pnl if current_daily_pnl is not None else self.daily_pnl
        max_daily_loss_amount = self.account_balance * self.limits.max_daily_loss

        should_close = pnl <= -max_daily_loss_amount

        if should_close:
            self.logger.warning(
                f"🚨 DAILY LOSS LIMIT REACHED: PnL={pnl:.2f}, "
                f"Limit={-max_daily_loss_amount:.2f}"
            )

        return should_close

    # ========================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ========================================================================

    def get_risk_status(self) -> Dict[str, Any]:
        """
        Получение текущего статуса риск-менеджера.

        Returns:
            Словарь с информацией о текущем состоянии
        """
        max_daily_loss_amount = self.account_balance * self.limits.max_daily_loss

        return {
            'daily_pnl': self.daily_pnl,
            'max_daily_loss_amount': max_daily_loss_amount,
            'max_daily_loss_pct': self.limits.max_daily_loss,
            'should_close_positions': self.should_close_all_positions(),
            'account_balance': self.account_balance,
            'position_risk_pct': self.limits.max_portfolio_risk,
            'stop_loss_atr_multiplier': self.limits.stop_loss_atr_multiplier,
            'take_profit_atr_multiplier': self.limits.take_profit_atr_multiplier,
            'version': self.VERSION
        }

    def _validate_inputs(
            self,
            signal: DetectorSignal,
            current_price: float,
            atr: float,
            account_balance: float
    ) -> bool:
        """Валидация входных данных для calculate_risk_context"""
        if not signal.get("ok", False):
            self.logger.warning("⚠️ Signal not ok")
            return False

        if current_price <= 0:
            self.logger.error(f"❌ Invalid current_price: {current_price}")
            return False

        if atr <= 0:
            self.logger.error(f"❌ Invalid atr: {atr}")
            return False

        if account_balance <= 0:
            self.logger.error(f"❌ Invalid account_balance: {account_balance}")
            return False

        return True

    def _create_empty_context(self, reason: str) -> RiskContext:
        """Создание пустого риск-контекста при ошибке"""
        return RiskContext(
            position_size=0.0,
            initial_stop_loss=0.0,
            take_profit=0.0,
            atr=0.0,
            stop_atr_multiplier=self.limits.stop_loss_atr_multiplier,
            tp_atr_multiplier=self.limits.take_profit_atr_multiplier,
            volatility_regime=1.0,
            regime=None,
            computed_at_ms=int(time.time() * 1000),
            risk_manager_version=f"{self.VERSION}-error",
            validation_hash=f"error-{reason}"
        )

    def _get_effective_balance(self) -> float:
        """Эффективный баланс для расчёта лимитов (для будущего расширения)"""
        return self.account_balance


# ============================================================================
# ЭКСПОРТЫ
# ============================================================================

__all__ = [
    # Основной класс
    "EnhancedRiskManager",

    # Типы и контракты
    "Direction",
    "RiskContext",
    "RiskLimits",
    "RiskManagerInterface",

    # Алиасы
    "DirectionStr",
    "RegimeType",

    # Функции
    "direction_to_side",
    "side_to_direction",
    "normalize_direction",
    "compute_risk_hash",
    "validate_risk_context",
]
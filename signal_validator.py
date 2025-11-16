"""
signal_validator.py - Централизованная валидация сигналов
Единая точка валидации для всех типов торговых сигналов
"""

from __future__ import annotations
from typing import Dict, Any, Optional, Tuple, List, Union
from decimal import Decimal
import logging

from iqts_standards import (
    StrategySignal, TradeSignalIQTS, DetectorSignal,
    DirectionLiteral, OrderReq
)

logger = logging.getLogger(__name__)


class ValidationResult:
    """Результат валидации сигнала"""
    
    def __init__(self, valid: bool, errors: Optional[List[str]] = None, warnings: Optional[List[str]] = None):
        self.valid = valid
        self.errors = errors or []
        self.warnings = warnings or []
    
    def __bool__(self) -> bool:
        return self.valid
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'valid': self.valid,
            'errors': self.errors,
            'warnings': self.warnings
        }


class SignalValidator:
    """
    Централизованный валидатор сигналов
    Проверяет DetectorSignal, TradeSignal, TradeSignalIQTS на корректность
    """
    
    def __init__(self, strict_mode: bool = False):
        """
        Args:
            strict_mode: Если True, warnings считаются ошибками
        """
        self.strict_mode = strict_mode
        self.logger = logger
    
    # === DetectorSignal валидация ===

    def validate_detector_signal(self, signal: DetectorSignal) -> ValidationResult:
        """
        Валидация DetectorSignal

        Проверяет:
        - Обязательные поля (ok, direction, confidence)
        - Допустимые значения direction
        - Диапазон confidence (0.0-1.0)
        - Согласованность ok/confidence
        """
        errors = []
        warnings = []

        # 1. Обязательные поля
        required_fields = ['ok', 'direction', 'confidence']
        for field in required_fields:
            if field not in signal:
                errors.append(f"Missing required field: {field}")

        if errors:
            return ValidationResult(False, errors)

        # 2. Валидация direction
        direction = signal.get('direction')
        valid_directions = list(DirectionLiteral.__args__)
        if direction not in valid_directions:
            errors.append(f"Invalid direction: {direction}. Must be one of {valid_directions}")

        # Прерываемся при ошибке направления
        if errors:
            return ValidationResult(False, errors)

        # 3. Валидация confidence
        confidence = signal.get('confidence')
        try:
            conf_float = float(confidence)
            if not (0.0 <= conf_float <= 1.0):
                errors.append(f"confidence must be in range [0.0, 1.0], got {conf_float}")
                return ValidationResult(False, errors)  # ← выходим сразу
        except (ValueError, TypeError):
            errors.append(f"confidence must be a number, got {type(confidence)}")
            return ValidationResult(False, errors)  # ← выходим сразу

        # К этому моменту conf_float гарантированно float и в диапазоне [0.0, 1.0]

        # 4. Согласованность ok/confidence
        ok = signal.get('ok')
        if ok is True and conf_float == 0.0:
            warnings.append("Signal marked ok=True but confidence=0.0")
        if ok is False and conf_float > 0.5:
            warnings.append(f"Signal marked ok=False but confidence={conf_float:.2f} is high")

        # 5. Metadata валидация (опционально)
        metadata = signal.get('metadata', {})
        if not isinstance(metadata, dict):
            warnings.append(f"metadata should be dict, got {type(metadata)}")

        valid = len(errors) == 0 and (not self.strict_mode or len(warnings) == 0)
        return ValidationResult(valid, errors, warnings)

    # === TradeSignalIQTS валидация ===
    
    def validate_trade_signal_iqts(self, signal: TradeSignalIQTS) -> ValidationResult:
        """
        Валидация TradeSignalIQTS (выходной сигнал ImprovedQualityTrendSystem)
        
        Проверяет:
        - Обязательные поля (direction, entry_price, position_size, stop_loss, take_profit)
        - Положительные значения цен и размера
        - Risk/Reward соотношение
        - Согласованность стопов с направлением
        """
        errors = []
        warnings = []
        
        # 1. Обязательные поля
        required_fields = ['direction', 'entry_price', 'position_size', 'stop_loss', 'take_profit', 'confidence']
        for field in required_fields:
            if field not in signal:
                errors.append(f"Missing required field: {field}")
        
        if errors:
            return ValidationResult(False, errors)
        
        # 2. Валидация direction
        direction = signal.get('direction')
        if direction not in ['BUY', 'SELL']:
            errors.append(f"TradeSignalIQTS direction must be BUY or SELL, got {direction}")
            return ValidationResult(False, errors)
        
        # 3. Валидация цен
        entry_price = signal.get('entry_price', 0.0)
        stop_loss = signal.get('stop_loss', 0.0)
        take_profit = signal.get('take_profit', 0.0)
        position_size = signal.get('position_size', 0.0)
        
        if entry_price <= 0:
            errors.append(f"entry_price must be positive, got {entry_price}")
        if stop_loss <= 0:
            errors.append(f"stop_loss must be positive, got {stop_loss}")
        if take_profit <= 0:
            errors.append(f"take_profit must be positive, got {take_profit}")
        if position_size <= 0:
            errors.append(f"position_size must be positive, got {position_size}")
        
        if errors:
            return ValidationResult(False, errors)
        
        # 4. Валидация согласованности стопов с направлением
        if direction == 'BUY':
            if stop_loss >= entry_price:
                errors.append(f"BUY: stop_loss ({stop_loss}) must be < entry_price ({entry_price})")
            if take_profit <= entry_price:
                errors.append(f"BUY: take_profit ({take_profit}) must be > entry_price ({entry_price})")
        else:  # SELL
            if stop_loss <= entry_price:
                errors.append(f"SELL: stop_loss ({stop_loss}) must be > entry_price ({entry_price})")
            if take_profit >= entry_price:
                errors.append(f"SELL: take_profit ({take_profit}) must be < entry_price ({entry_price})")
        
        if errors:
            return ValidationResult(False, errors)
        
        # 5. Risk/Reward соотношение
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0
        
        if rr_ratio < 1.0:
            warnings.append(f"Poor risk/reward ratio: {rr_ratio:.2f} (< 1.0)")
        elif rr_ratio < 1.5:
            warnings.append(f"Low risk/reward ratio: {rr_ratio:.2f} (< 1.5)")
        
        # 6. Confidence проверка
        confidence = signal.get('confidence', 0.0)
        if confidence < 0.5:
            warnings.append(f"Low confidence: {confidence:.2f}")
        
        # 7. Размер позиции (предупреждения)
        if position_size * entry_price < 5.0:
            warnings.append(f"Position value {position_size * entry_price:.2f} USDT is very small (< 5 USDT)")
        
        valid = len(errors) == 0 and (not self.strict_mode or len(warnings) == 0)
        return ValidationResult(valid, errors, warnings)
    
    # === TradeSignal валидация (для PositionManager) ===
    
    def validate_trade_signal(self, signal: StrategySignal) -> ValidationResult:
        """
        Валидация TradeSignal (входной сигнал PositionManager)
        
        Проверяет:
        - Обязательные поля (symbol, intent, decision_price)
        - Допустимые значения intent
        - Корректность цены
        - Наличие correlation_id
        """
        errors = []
        warnings = []
        
        # 1. Обязательные поля
        required_fields = ['symbol', 'intent', 'decision_price']
        for field in required_fields:
            if field not in signal:
                errors.append(f"Missing required field: {field}")
        
        if errors:
            return ValidationResult(False, errors)
        
        # 2. Валидация intent
        intent = signal.get('intent')
        valid_intents = ['LONG_OPEN', 'LONG_CLOSE', 'SHORT_OPEN', 'SHORT_CLOSE', 'WAIT', 'HOLD', 'FLAT']
        if intent not in valid_intents:
            errors.append(f"Invalid intent: {intent}. Must be one of {valid_intents}")
        
        # 3. Валидация decision_price
        decision_price = signal.get('decision_price', 0)
        if decision_price <= 0:
            errors.append(f"decision_price must be positive, got {decision_price}")
        
        # 4. Проверка correlation_id (предупреждение)
        if 'correlation_id' not in signal:
            warnings.append("Missing correlation_id - signal deduplication won't work")
        
        # 5. Валидация WAIT сигналов (должны содержать stop_update)
        if intent == 'WAIT':
            metadata = signal.get('metadata', {})
            if 'stop_update' not in metadata:
                warnings.append("WAIT signal without stop_update in metadata")
            else:
                stop_update = metadata.get('stop_update')
                if not isinstance(stop_update, dict) or 'new_stop_price' not in stop_update:
                    errors.append("WAIT signal: stop_update must contain 'new_stop_price'")
        
        # 6. Валидация символа
        symbol = signal.get('symbol', '')
        if not symbol or not isinstance(symbol, str):
            errors.append(f"Invalid symbol: {symbol}")
        
        valid = len(errors) == 0 and (not self.strict_mode or len(warnings) == 0)
        return ValidationResult(valid, errors, warnings)
    
    # === OrderReq валидация ===
    
    def validate_order_req(self, order_req: OrderReq) -> ValidationResult:
        """
        Валидация OrderReq перед отправкой на биржу
        
        Проверяет:
        - Обязательные поля
- Корректность типа ордера
        - Положительность цен и количества
        - Специфические требования для STOP/LIMIT ордеров
        """
        errors = []
        warnings = []
        
        # 1. Обязательные поля
        required_fields = ['client_order_id', 'symbol', 'side', 'type', 'qty']
        for field in required_fields:
            if field not in order_req:
                errors.append(f"Missing required field: {field}")
        
        if errors:
            return ValidationResult(False, errors)
        
        # 2. Валидация side
        side = order_req.get('side')
        if side not in ['BUY', 'SELL']:
            errors.append(f"Invalid side: {side}. Must be BUY or SELL")
        
        # 3. Валидация type
        order_type = order_req.get('type')
        valid_types = ['MARKET', 'LIMIT', 'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET']
        if order_type not in valid_types:
            errors.append(f"Invalid type: {order_type}. Must be one of {valid_types}")
        
        # 4. Валидация qty
        qty = order_req.get('qty')
        try:
            qty_decimal = Decimal(str(qty))
            if qty_decimal <= 0:
                errors.append(f"qty must be positive, got {qty}")
        except (ValueError, TypeError):
            errors.append(f"qty must be a number, got {type(qty)}")
        
        # 5. Специфические проверки для LIMIT
        if order_type == 'LIMIT':
            if 'price' not in order_req or order_req.get('price') is None:
                errors.append("LIMIT order requires 'price'")
            else:
                try:
                    price = Decimal(str(order_req['price']))
                    if price <= 0:
                        errors.append(f"LIMIT price must be positive, got {price}")
                except (ValueError, TypeError):
                    errors.append(f"LIMIT price must be a number")
        
        # 6. Специфические проверки для STOP/TAKE_PROFIT
        if order_type in ['STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET']:
            if 'stop_price' not in order_req or order_req.get('stop_price') is None:
                errors.append(f"{order_type} order requires 'stop_price'")
            else:
                try:
                    stop_price = Decimal(str(order_req['stop_price']))
                    if stop_price <= 0:
                        errors.append(f"{order_type} stop_price must be positive, got {stop_price}")
                except (ValueError, TypeError):
                    errors.append(f"{order_type} stop_price must be a number")
        
        # 7. Проверка correlation_id (предупреждение)
        if 'correlation_id' not in order_req:
            warnings.append("Missing correlation_id - order tracking will be limited")
        
        valid = len(errors) == 0 and (not self.strict_mode or len(warnings) == 0)
        return ValidationResult(valid, errors, warnings)
    
    # === Комплексная валидация ===
    
    def validate_signal_flow(self,
                            detector_signal: Optional[DetectorSignal] = None,
                            trade_signal_iqts: Optional[TradeSignalIQTS] = None,
                            trade_signal: Optional[StrategySignal] = None,
                            order_req: Optional[OrderReq] = None) -> Dict[str, ValidationResult]:
        """
        Валидация всего потока: DetectorSignal → TradeSignalIQTS → TradeSignal → OrderReq
        
        Returns:
            Dict с результатами валидации каждого этапа
        """
        results = {}
        
        if detector_signal is not None:
            results['detector_signal'] = self.validate_detector_signal(detector_signal)
        
        if trade_signal_iqts is not None:
            results['trade_signal_iqts'] = self.validate_trade_signal_iqts(trade_signal_iqts)
        
        if trade_signal is not None:
            results['trade_signal'] = self.validate_trade_signal(trade_signal)
        
        if order_req is not None:
            results['order_req'] = self.validate_order_req(order_req)
        
        return results
    
    # === Утилиты ===
    
    @staticmethod
    def check_price_sanity(price: float, 
                          symbol: str = "UNKNOWN",
                          min_price: float = 0.0001,
                          max_price: float = 1000000.0) -> Tuple[bool, Optional[str]]:
        """
        Проверка разумности цены
        
        Args:
            price: Проверяемая цена
            symbol: Символ для контекста
            min_price: Минимальная допустимая цена
            max_price: Максимальная допустимая цена
        
        Returns:
            (valid, error_message)
        """
        if price <= 0:
            return False, f"Price must be positive for {symbol}"
        
        if price < min_price:
            return False, f"Price {price} is too low for {symbol} (min: {min_price})"
        
        if price > max_price:
            return False, f"Price {price} is too high for {symbol} (max: {max_price})"
        
        return True, None
    
    @staticmethod
    def check_stop_loss_sanity(entry_price: float,
                               stop_loss: float,
                               direction: DirectionLiteral,
                               max_stop_distance_pct: float = 10.0) -> Tuple[bool, Optional[str]]:
        """
        Проверка разумности стоп-лосса
        
        Args:
            entry_price: Цена входа
            stop_loss: Уровень стоп-лосса
            direction: Направление позиции
            max_stop_distance_pct: Максимальное расстояние стопа в %
        
        Returns:
            (valid, error_message)
        """
        if direction == 'BUY':
            if stop_loss >= entry_price:
                return False, f"BUY: stop_loss ({stop_loss}) must be < entry_price ({entry_price})"
        elif direction == 'SELL':
            if stop_loss <= entry_price:
                return False, f"SELL: stop_loss ({stop_loss}) must be > entry_price ({entry_price})"
        else:
            return False, f"Invalid direction for stop check: {direction}"
        
        # Проверка расстояния
        stop_distance_pct = abs(stop_loss - entry_price) / entry_price * 100
        if stop_distance_pct > max_stop_distance_pct:
            return False, f"Stop loss too far from entry: {stop_distance_pct:.2f}% (max: {max_stop_distance_pct}%)"
        
        if stop_distance_pct < 0.1:
            return False, f"Stop loss too close to entry: {stop_distance_pct:.2f}% (min: 0.1%)"
        
        return True, None
    
    @staticmethod
    def calculate_risk_reward_ratio(entry_price: float,
                                    stop_loss: float,
                                    take_profit: float) -> float:
        """
        Расчет соотношения риск/прибыль
        
        Returns:
            Risk/Reward ratio (reward / risk)
        """
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        
        if risk <= 0:
            return 0.0
        
        return reward / risk


# === Глобальный валидатор (singleton) ===

_global_validator: Optional[SignalValidator] = None


def get_validator(strict_mode: bool = False) -> SignalValidator:
    """
    Получить глобальный экземпляр валидатора
    
    Args:
        strict_mode: Если True, warnings считаются ошибками
    
    Returns:
        SignalValidator instance
    """
    global _global_validator
    
    if _global_validator is None:
        _global_validator = SignalValidator(strict_mode=strict_mode)
    
    return _global_validator


# === Декоратор для валидации ===

def validate_signal(signal_type: str = 'auto'):
    """
    Декоратор для автоматической валидации сигналов
    
    Args:
        signal_type: Тип сигнала ('detector', 'trade_iqts', 'trade', 'order', 'auto')
    
    Example:
        @validate_signal('trade_iqts')
        async def analyze_and_trade(self, market_data) -> TradeSignalIQTS:
            # ... код генерации сигнала ...
            return signal
    """
    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            
            if result is None:
                return result
            
            validator = get_validator()
            
            # Автоопределение типа
            if signal_type == 'auto':
                if 'position_size' in result:
                    validation_result = validator.validate_trade_signal_iqts(result)
                elif 'intent' in result:
                    validation_result = validator.validate_trade_signal(result)
                elif 'ok' in result:
                    validation_result = validator.validate_detector_signal(result)
                else:
                    return result
            elif signal_type == 'detector':
                validation_result = validator.validate_detector_signal(result)
            elif signal_type == 'trade_iqts':
                validation_result = validator.validate_trade_signal_iqts(result)
            elif signal_type == 'trade':
                validation_result = validator.validate_trade_signal(result)
            else:
                return result
            
            if not validation_result.valid:
                logger.error(
                    f"Signal validation failed in {func.__name__}: {validation_result.errors}"
                )
                if validation_result.warnings:
                    logger.warning(
                        f"Signal warnings in {func.__name__}: {validation_result.warnings}"
                    )
                return None
            
            if validation_result.warnings:
                logger.warning(
                    f"Signal warnings in {func.__name__}: {validation_result.warnings}"
                )
            
            return result

        def sync_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)

            if result is None:
                return result

            validator = get_validator()

            if signal_type == 'auto':
                if 'position_size' in result:
                    validation_result = validator.validate_trade_signal_iqts(result)
                elif 'intent' in result:
                    validation_result = validator.validate_trade_signal(result)
                elif 'ok' in result:
                    validation_result = validator.validate_detector_signal(result)
                else:
                    return result
            elif signal_type == 'detector':
                validation_result = validator.validate_detector_signal(result)
            elif signal_type == 'trade_iqts':
                validation_result = validator.validate_trade_signal_iqts(result)
            elif signal_type == 'trade':
                validation_result = validator.validate_trade_signal(result)
            else:
                return result

            if not validation_result.valid:
                logger.error(f"Signal validation failed in {func.__name__}: {validation_result.errors}")
                if validation_result.warnings:
                    logger.warning(f"Signal warnings in {func.__name__}: {validation_result.warnings}")
                return None

            if validation_result.warnings:
                logger.warning(f"Signal warnings in {func.__name__}: {validation_result.warnings}")

            return result

        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


# === Экспортируемые функции для быстрой проверки ===

def quick_validate_detector_signal(signal: DetectorSignal, verbose: bool = False) -> Union[bool, ValidationResult]:
    result = get_validator().validate_detector_signal(signal)
    if verbose:
        return result
    return result.valid


def quick_validate_trade_signal_iqts(signal: TradeSignalIQTS) -> bool:
    """Быстрая проверка TradeSignalIQTS (возвращает bool)"""
    return bool(get_validator().validate_trade_signal_iqts(signal))


def quick_validate_trade_signal(signal: StrategySignal) -> bool:
    """Быстрая проверка TradeSignal (возвращает bool)"""
    return bool(get_validator().validate_trade_signal(signal))


def quick_validate_order_req(order_req: OrderReq) -> bool:
    """Быстрая проверка OrderReq (возвращает bool)"""
    return bool(get_validator().validate_order_req(order_req))


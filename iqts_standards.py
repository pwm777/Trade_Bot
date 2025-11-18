"""
 iqts_standards.py
 Единый источник истины для Improved Quality Trend System (IQTS)
 Содержит:
 - типы сигналов, направлений, режимов
 - протоколы (Detector, RiskManager)
 - утилиты: валидация данных, конвертация сигналов
 - метаданные и контексты
"""

from __future__ import annotations
from typing import (
    TypedDict, Literal, Protocol, Dict, Any, List, Optional, runtime_checkable, Callable, cast, Union
)
import pandas as pd
from enum import Enum
from dataclasses import dataclass
from abc import ABC, abstractmethod
from decimal import Decimal
import uuid
import time
import logging
from datetime import datetime, UTC
# =============================================================================
# === ЛИТЕРАЛЫ / КОНСТАНТЫ =====================================================
# =============================================================================

Timeframe = Literal["1m", "5m", "15m", "1h"]

# ============================================================================
# === DIRECTION TYPES (ЕДИНАЯ СИСТЕМА КООРДИНАТ) =============================
# ============================================================================

class Direction(IntEnum):
    """
    Направление позиции (числовые значения для совместимости с DirectionLiteral).

    Значения:
        BUY = 1   -> Покупка (Long)
        SELL = -1 -> Продажа (Short)
        FLAT = 0  -> Нет позиции (Hold)

    Использование:
        direction = Direction.BUY  # Значение: 1
        if direction == Direction.BUY:
            side = direction.side  # "BUY"
        opposite = direction.opposite()  # Direction.SELL
    """
    BUY = 1
    SELL = -1
    FLAT = 0

    @property
    def side(self) -> str:
        """Строковое представление для биржевых API"""
        return self.name  # "BUY", "SELL", "FLAT"

    def opposite(self) -> 'Direction':
        """Возвращает противоположное направление"""
        if self == Direction.BUY:
            return Direction.SELL
        elif self == Direction.SELL:
            return Direction.BUY
        return Direction.FLAT

    def __str__(self) -> str:
        return self.name


# Числовой формат (backward compatibility для DetectorSignal)
DirectionLiteral = Literal[1, -1, 0]
"""
DEPRECATED: Используйте Direction enum вместо этого.
Числовой формат направления для обратной совместимости.

Значения:
    1  -> BUY/LONG  (эквивалентно Direction.BUY)
    -1 -> SELL/SHORT (эквивалентно Direction.SELL)
    0  -> FLAT/HOLD  (эквивалентно Direction.FLAT)
"""

# Строковый формат (для биржевых API)
DirectionStr = Literal["BUY", "SELL", "FLAT"]
"""
Строковый формат направления для биржевых API и OrderReq.

Значения:
    "BUY"  -> Покупка (эквивалентно Direction.BUY или 1)
    "SELL" -> Продажа (эквивалентно Direction.SELL или -1)
    "FLAT" -> Нет позиции (эквивалентно Direction.FLAT или 0)
"""

# Обобщённый тип (для функций, принимающих любой формат)
DirectionType = Union[Direction, DirectionLiteral, DirectionStr, int, str]

MarketRegimeLiteral = Literal[
    "strong_uptrend", "weak_uptrend",
    "strong_downtrend", "weak_downtrend",
    "sideways", "uncertain"
]

# =============================================================================
# === REASON CODES (ЕДИНЫЙ ИСТОЧНИК ИСТИНЫ) ===================================
# =============================================================================

# Все допустимые коды причин с категориями и алиасами
_REASON_CODE_DEFINITIONS = {
    # Успешные сигналы
    "hierarchical_confirmed": {"category": "success", "aliases": []},
    "trend_confirmed": {"category": "success", "aliases": ["TREND"]},
    "entry_confirmed": {"category": "success", "aliases": ["ENTRY"]},
    "three_level_confirmed": {"category": "success", "aliases": []},

    # Отклонения по слабым сигналам
    "no_trend_signal": {"category": "weak_signal", "aliases": []},
    "weak_trend_signal": {"category": "weak_signal", "aliases": []},
    "no_entry_signal": {"category": "weak_signal", "aliases": []},
    "weak_entry_signal": {"category": "weak_signal", "aliases": []},

    # Отклонения по несогласованности
    "direction_disagreement": {"category": "disagreement", "aliases": []},
    "consistency_check_failed": {"category": "disagreement", "aliases": []},

    # Фильтры
    "time_filter": {"category": "filter", "aliases": []},
    "volume_filter": {"category": "filter", "aliases": []},
    "volatility_filter": {"category": "filter", "aliases": []},
    "quality_filter": {"category": "filter", "aliases": []},
    "consistency_filter": {"category": "filter", "aliases": ["CONSISTENCY"]},

    # Проблемы с данными
    "insufficient_data": {"category": "data_issue", "aliases": []},
    "invalid_data": {"category": "data_issue", "aliases": ["DATA"]},
    "insufficient_warmup": {"category": "data_issue", "aliases": ["WARMUP"]},

    # Ограничения и риски
    "invalid_price": {"category": "risk", "aliases": ["PRICE"]},
    "daily_limit_reached": {"category": "risk", "aliases": ["LIMIT"]},
    "outside_trading_hours": {"category": "risk", "aliases": ["HOURS"]},
}

# Автоматически генерируемые структуры
_VALID_REASON_CODES = frozenset(_REASON_CODE_DEFINITIONS.keys())

# Обратная карта алиасов -> канонический код
_REASON_MAP = {}
for canonical_code, meta in _REASON_CODE_DEFINITIONS.items():
    for alias in meta["aliases"]:
        _REASON_MAP[alias] = canonical_code

# Type hint для статической проверки
ReasonCode = Literal[
    "hierarchical_confirmed",
    "trend_confirmed",
    "entry_confirmed",
    "three_level_confirmed",
    "no_trend_signal",
    "weak_trend_signal",
    "no_entry_signal",
    "weak_entry_signal",
    "direction_disagreement",
    "consistency_check_failed",
    "time_filter",
    "volume_filter",
    "volatility_filter",
    "quality_filter",
    "consistency_filter",
    "insufficient_data",
    "invalid_data",
    "insufficient_warmup",
    "invalid_price",
    "daily_limit_reached",
    "outside_trading_hours",
    "detector_error"
]

REQUIRED_OHLCV_COLUMNS = ("open", "high", "low", "close")
INVALID_DATA: ReasonCode = "invalid_data"
DEFAULT_TRADING_HOURS = (6, 22)


# =============================================================================
# === UTILITY FUNCTIONS =======================================================
# =============================================================================

def safe_nested_getattr(obj: Any, attr_path: str, default: Any = None) -> Any:
    """
    Безопасное получение вложенных атрибутов через точечную нотацию.

    Функция позволяет обращаться к вложенным атрибутам объектов без риска
    получить AttributeError. Если любой из атрибутов в цепочке отсутствует
    или равен None, возвращается значение по умолчанию.

    Args:
        obj: Объект для доступа к атрибутам
        attr_path: Путь к атрибуту через точку (например, "a.b.c")
        default: Значение, возвращаемое если атрибут не найден (по умолчанию None)

    Returns:
        Значение атрибута или default если атрибут не найден/равен None

    Notes:
        - Функция безопасна для использования с None объектами
        - Логирует ошибки только в debug режиме
        - Обрабатывает AttributeError, TypeError, ValueError
    """
    if not attr_path:
        return default

    try:
        attrs = attr_path.split('.')
        result = obj

        for attr in attrs:
            # Если промежуточный результат None - возвращаем default
            if result is None:
                return default

            # Проверяем наличие атрибута перед доступом
            if not hasattr(result, attr):
                return default

            result = getattr(result, attr)

        # Если финальный результат None - возвращаем default
        return result if result is not None else default

    except (AttributeError, TypeError, ValueError) as e:
        # Логируем только в debug режиме для предотвращения спама
        logger = logging.getLogger(__name__)
        logger.debug(f"safe_nested_getattr({attr_path}): {type(e).__name__}: {e}")
        return default


# === БАЗОВЫЕ ТИПЫ ===

# Signal Types
SignalIntent = Literal["LONG_OPEN", "LONG_CLOSE", "SHORT_OPEN", "SHORT_CLOSE", "WAIT", "HOLD", "FLAT"]

# Order Types (соответствие БД)
OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]
OrderStatus = Literal[
    "NEW", "WORKING", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED", "PENDING_CANCEL"]

# Position Types (соответствие БД)
PositionSide = Literal["LONG", "SHORT"]
PositionStatus = Literal["OPEN", "CLOSING", "CLOSED", "FLAT"]

# Execution Modes
ExecutionMode = Literal["LIVE", "DEMO", "BACKTEST"]

# Connection Status
ConnectionStatus = Literal["connected", "disconnected", "connecting", "error"]


# =============================================================================
# === МЕТАДАННЫЕ И СИГНАЛЫ ====================================================
# =============================================================================

class DetectorMetadata(TypedDict, total=False):
    z_score: float
    cusum_pos: float
    cusum_neg: float
    vola_flag: bool
    atr: float
    volume_ratio: float
    volatility_ratio: float
    filters_passed: List[str]
    regime: MarketRegimeLiteral
    regime_confidence: float
    risk_reward_ratio: float
    trade_number_today: int
    signal_source: str
    extra: Dict[str, Any]


class DetectorSignal(TypedDict, total=False):
    ok: bool
    direction: Literal[1, -1, 0]
    confidence: float
    reason: ReasonCode
    metadata: DetectorMetadata


class TradeSignalIQTS(TypedDict, total=False):
    direction: Literal[1, -1, 0]
    entry_price: float
    position_size: float
    stop_loss: float
    take_profit: float
    confidence: float
    regime: MarketRegimeLiteral
    metadata: DetectorMetadata


class TradeSignal(TypedDict, total=False):
    """Сигнал от стратегии"""
    correlation_id: str
    symbol: str
    intent: SignalIntent
    reason_code: str
    emitted_at_ms: int
    decision_price: Optional[float]
    risk_context: Dict[str, Any]
    stop_update: Optional[Dict[str, Any]]
    confidence: Optional[float]
    metadata: Optional[Dict[str, Any]]


class TradeResult(TypedDict, total=False):
    position_id: str
    pnl: float
    regime: MarketRegimeLiteral
    signal_source: str
    confidence: float
    opened_at: datetime
    closed_at: datetime
    close_price: float
    direction: Literal[1, -1, 0]
    entry_price: float


class RegimeInfo(TypedDict, total=False):
    regime: MarketRegimeLiteral
    confidence: float
    volatility_level: float
    trend_strength: float
    volume_profile: Literal["high", "normal", "low"]


class RiskContext(TypedDict, total=False):
    """
    Контекст риск-параметров для позиции.

    ВАЖНО: Расширен в v2.0 для поддержки EnhancedRiskManager.
    """
    # Основные параметры позиции
    position_size: float
    initial_stop_loss: float
    take_profit: float

    # Метаданные расчёта
    atr: float
    stop_atr_multiplier: float
    tp_atr_multiplier: float

    # Режим рынка
    volatility_regime: float
    regime: Optional[str]
    regime_confidence: float

    # Аудит и трассировка (v2.0)
    computed_at_ms: int
    risk_manager_version: str
    validation_hash: Optional[str]

    # Дополнительные параметры (v2.0)
    max_hold_time_minutes: Optional[int]
    trailing_config: Optional[Dict[str, float]]

    # Backward compatibility (deprecated, алиас для initial_stop_loss)
    stop_loss: Optional[float]

# =============================================================================
# === BOT LIFECYCLE TYPES =====================================================
# =============================================================================

class BotLifecycleEvent(TypedDict, total=False):
    """Событие жизненного цикла бота"""
    event_type: str
    timestamp_ms: int
    data: Dict[str, Any]


class ConnectionState(TypedDict, total=False):
    """Состояние подключения компонента"""
    status: ConnectionStatus
    timestamp_ms: int
    error: Optional[str]
    component: Optional[str]


# === Callback Types ===

BotLifecycleEventHandler = Callable[[BotLifecycleEvent], None]
AlertCallback = Callable[[str, Dict[str, Any]], None]


# === Exceptions ===

class BotLifecycleError(Exception):
    """Ошибка жизненного цикла бота"""
    pass


class ComponentInitializationError(Exception):
    """Ошибка инициализации компонента"""
    pass


# =============================================================================
# === ПРОТОКОЛЫ (ИНТЕРФЕЙСЫ) ==================================================
# =============================================================================


@runtime_checkable
class DetectorInterface(Protocol):
    """Протокол для всех детекторов тренда."""

    def get_required_bars(self) -> Dict[Timeframe, int]: ...

    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal: ...

    def validate_data(self, data: Dict[Timeframe, pd.DataFrame]) -> bool: ...


@runtime_checkable
class RiskManagerInterface(Protocol):
    """Протокол управления рисками."""

    def calculate_position_size(
            self,
            *,
            signal: DetectorSignal,
            current_price: float,
            atr: float,
            account_balance: float
    ) -> float: ...

    def calculate_dynamic_stops(
            self,
            *,
            entry_price: float,
            direction: Union[Direction, DirectionLiteral],
            atr: float,
            regime_ctx: RiskContext
    ) -> tuple[float, float]: ...

    def update_daily_pnl(self, pnl: float) -> None: ...

    def should_close_all_positions(self) -> bool: ...


@runtime_checkable
class MonitoringInterface(Protocol):
    """Протокол мониторинга и отчётности."""

    async def send_alert(self, alert: Dict[str, Any]) -> None: ...

    def generate_performance_report(self, trading_system: TradingSystemInterface) -> Dict[str, Any]: ...


@runtime_checkable
class TradingSystemInterface(Protocol):
    """Главной торговой системы."""

    async def analyze_and_trade(self, market_data: Dict[Timeframe, pd.DataFrame]) -> Optional[TradeSignalIQTS]: ...

    def get_system_status(self) -> SystemStatus: ...

    def update_performance(self, trade_result: Dict[str, Any]) -> None: ...


# === Bot Component Interfaces ===

@runtime_checkable
class StrategyInterface(Protocol):
    """Интерфейс торговой стратегии"""

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Optional[StrategySignal]: ...

    def get_required_history(self) -> int: ...


# === EVENT HANDLERS ===
class MarketEvent(TypedDict, total=False):
    """Событие от MarketAggregator"""
    event_type: str
    symbol: str
    timestamp_ms: int
    data: Dict[str, Any]
    severity: Literal["info", "warning", "error"]


MarketEventHandler = Callable[[MarketEvent], None]


class NetConnState(TypedDict, total=False):
    """Состояние соединения"""
    status: ConnectionStatus
    last_heartbeat: Optional[int]
    reconnect_count: int
    error_message: Optional[str]
    connected_at: Optional[int]
    last_error_at: Optional[int]


@runtime_checkable
class PositionManagerInterface(Protocol):
    """Интерфейс менеджера позиций"""

    def handle_signal(self, signal: StrategySignal) -> Optional[OrderReq]: ...

    def update_on_fill(self, fill: OrderUpd) -> None: ...

    def get_open_positions_snapshot(self) -> Dict[str, PositionSnapshot]: ...

    def get_stats(self) -> Dict[str, Any]: ...


# Добавить в ExchangeManagerInterface (iqts_standards.py, после строки 444):

@runtime_checkable
class ExchangeManagerInterface(Protocol):
    """Интерфейс менеджера биржи"""

    async def place_order(self, order_req: OrderReq) -> Dict[str, Any]: ...

    async def cancel_order(self, client_order_id: str) -> Dict[str, Any]: ...

    def disconnect_user_stream(self) -> None: ...

    def check_stops_on_price_update(
            self,
            symbol: str,
            current_price: float,
            correlation_id: Optional[str] = None
    ) -> None: ...

    def get_active_orders(self, symbol: str) -> List[Dict[str, Any]]: ...

class ExchangeEvent(TypedDict, total=False):
    """События биржи"""
    event_type: str
    symbol: Optional[str]
    timestamp_ms: int
    data: Dict[str, Any]


ExchangeEventHandler = Callable[[ExchangeEvent], None]


@runtime_checkable
class MarketAggregatorInterface(Protocol):
    """Интерфейс агрегатора рыночных данных"""

    async def start_async(self, symbols: List[str], *, history_window: int = 50) -> None: ...

    async def wait_for_completion(self) -> None: ...

    def stop(self) -> None: ...

    def shutdown(self) -> None: ...

    def add_event_handler(self, handler: MarketEventHandler) -> None: ...

    def get_stats(self) -> Dict[str, Any]: ...

    def get_connection_state(self) -> NetConnState: ...

    def fetch_recent(self, symbol: str, limit: int = 10) -> List[Candle1m]: ...

    async def fetch_candles(self, symbol: str, interval: str, start_time: int,
                            end_time: int, limit: int = 1000) -> List[Dict[str, Any]]: ...

    def get_buffer_history(self, symbol: str, count: int = 10, *,
                           exclude_current: bool = False) -> List[Candle1m]: ...


@runtime_checkable
class MainBotInterface(Protocol):
    """Интерфейс главного бота"""

    async def bootstrap(self) -> None: ...

    def handle_candle_ready(self, symbol: str, candle: Any, recent_stack: List[Any]) -> None: ...

    def get_stats(self) -> Dict[str, Any]: ...

    def get_component_health(self) -> Dict[str, Any]: ...

    def add_event_handler(self, handler: Callable) -> None: ...


# =============================================================================
# === СИСТЕМНЫЙ СТАТУС И КОНФИГ ===============================================
# =============================================================================

class SystemStatus(TypedDict, total=False):
    current_regime: MarketRegimeLiteral
    regime_confidence: float
    trades_today: int
    max_daily_trades: int
    total_trades: int
    win_rate: float
    total_pnl: float
    current_parameters: Dict[str, Any]


class RiskConfig(TypedDict, total=False):
    max_position_risk: float
    max_daily_loss: float
    atr_periods: int
    stop_atr_multiplier: float
    tp_atr_multiplier: float


class QualityDetectorConfig(TypedDict, total=False):
    trend_timeframe: Timeframe
    entry_timeframe: Timeframe
    max_daily_trades: int
    min_volume_ratio: float
    max_volatility_ratio: float


class MonitoringConfig(TypedDict, total=False):
    enabled: bool
    telegram_enabled: bool
    email_enabled: bool


class TradingSystemConfig(TypedDict, total=False):
    account_balance: float
    max_daily_trades: int
    max_daily_loss: float
    time_window_hours: tuple[int, int]
    risk_management: RiskConfig
    quality_detector: QualityDetectorConfig
    monitoring: MonitoringConfig


# =============================================================================
# === КЛАССЫ СОВМЕСТИМОСТИ position_manager.py ================================
# =============================================================================

class StrategySignal(TypedDict, total=False):
    """Сигнал от стратегии"""
    correlation_id: str
    symbol: str
    intent: SignalIntent
    reason_code: ReasonCode
    emitted_at_ms: int
    decision_price: Optional[float]
    risk_context: Dict[str, Any]
    stop_update: Optional[Dict[str, Any]]
    confidence: Optional[float]
    metadata: Optional[Dict[str, Any]]


class OrderReq(TypedDict, total=False):
    """Запрос на создание ордера"""
    client_order_id: str
    symbol: str
    type: OrderType
    side: OrderSide
    time_in_force: Optional[str]
    qty: Decimal
    price: Optional[Decimal]
    stop_price: Optional[Decimal]
    reduce_only: bool
    correlation_id: str
    metadata: Optional[Dict[str, Any]]


class OrderUpd(TypedDict, total=False):
    """Обновление статуса ордера"""
    client_order_id: str
    exchange_order_id: Optional[str]
    symbol: str
    side: OrderSide
    status: OrderStatus
    qty: Optional[Decimal]
    price: Optional[Decimal]
    filled_qty: Optional[Decimal]
    avg_price: Optional[Decimal]
    commission: Optional[Decimal]
    ts_ms_exchange: Optional[int]
    trade_id: Optional[str]
    reduce_only: bool
    correlation_id: str


class PositionSnapshot(TypedDict, total=False):
    """Снимок текущей позиции"""
    symbol: str
    status: PositionStatus
    side: Optional[PositionSide]
    qty: Decimal
    avg_entry_price: Decimal
    market_price: Optional[float]
    realized_pnl_usdt: Optional[Decimal]
    unrealized_pnl_usdt: Optional[Decimal]
    created_ts: int
    updated_ts: int
    fee_total_usdt: Optional[Decimal]
    correlation_id: Optional[str]
    stop_loss: Optional[Decimal]
    take_profit: Optional[Decimal]
    exit_tracking: Optional[Dict[str, Any]]


class PositionEvent(TypedDict, total=False):
    """События позиций"""
    event_type: str
    symbol: str
    timestamp_ms: int
    correlation_id: str
    position_data: Dict[str, Any]


class PriceFeed(Protocol):
    """Источник текущих цен"""

    def __call__(self, symbol: str) -> Optional[float]: ...


# === CORE EVENT HANDLERS ===
EventHandler = Callable[[PositionEvent], None]

# === STRATEGY TYPES ===
TradeSignalIntent = Literal["OPEN", "CLOSE", "WAIT"]
PositionType = Literal["LONG", "SHORT", "FLAT"]


class Candle1m(TypedDict, total=False):
    """1-минутная свеча (соответствует структуре таблицы candles_1m)"""
    symbol: str
    ts: int
    ts_close: Optional[int]
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    count: Optional[int]
    quote: Optional[Decimal]
    finalized: bool
    checksum: str
    created_ts: int

    # EMA индикаторы
    ema3: Optional[Decimal]
    ema7: Optional[Decimal]
    ema9: Optional[Decimal]
    ema15: Optional[Decimal]
    ema30: Optional[Decimal]

    # Технические индикаторы
    cmo14: Optional[Decimal]
    adx14: Optional[Decimal]
    plus_di14: Optional[Decimal]
    minus_di14: Optional[Decimal]
    atr14: Optional[Decimal]

    # CUSUM индикаторы
    cusum: Optional[Decimal]
    cusum_state: Optional[int]
    cusum_zscore: Optional[Decimal]
    cusum_conf: Optional[Decimal]
    cusum_reason: Optional[str]
    cusum_price_mean: Optional[Decimal]
    cusum_price_std: Optional[Decimal]
    cusum_pos: Optional[Decimal]
    cusum_neg: Optional[Decimal]

# =============================================================================
# === УТИЛИТЫ =================================================================
# =============================================================================

class CoverageStats(TypedDict, total=False):
    """Статистика покрытия данных"""
    symbol: str
    total_bars: int
    first_ts_ms: Optional[int]
    last_ts_ms: Optional[int]
    gaps_count: int
    zero_volume_count: int
    coverage_percentage: float
    time_range_hours: Optional[float]


class ValidationResult(TypedDict, total=False):
    """Результат валидации одной свечи"""
    symbol: str
    bar_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_valid: bool
    issues: List[str]


class MarketDataDBInterface(Protocol):
    """Интерфейс для работы с рыночными данными в БД"""

    def ensure_schema(self) -> None: ...

    def detect_source_table(self) -> Optional[str]: ...

    def validate_data(self, symbol: str, limit: int = 10) -> List[ValidationResult]: ...

    def get_coverage_stats(self, symbol: str) -> CoverageStats: ...

    def get_recent_candles(self, symbol: str, limit: int = 100) -> List[Candle1m]: ...

    def get_diagnostic_info(self, symbol: str) -> Dict[str, Any]: ...


def normalize_trading_hours(cfg: TradingSystemConfig) -> tuple[int, int]:
    """Нормализует торговые часы в диапазоне 0–23."""
    lo, hi = cfg.get("time_window_hours", DEFAULT_TRADING_HOURS)
    lo = max(0, min(23, int(lo)))
    hi = max(0, min(23, int(hi)))
    return (lo, hi) if lo <= hi else (hi, lo)


def map_reason(reason: str) -> ReasonCode:
    """
    Преобразует строку причины в стандартизированный ReasonCode.

    Поддерживает:
    - Канонические коды из ReasonCode (lowercase)
    - Алиасы из _REASON_MAP (например, "TREND" -> "trend_confirmed")

    Args:
        reason: Строка с кодом причины или алиасом

    Returns:
        Канонический ReasonCode или "invalid_data" если код не распознан

    Examples:
        >>> map_reason("TREND")
        "trend_confirmed"
        >>> map_reason("trend_confirmed")
        "trend_confirmed"
        >>> map_reason("invalid_reason")
        "invalid_data"
    """
    if not reason:
        return INVALID_DATA

    # Сначала проверяем алиасы (TREND, ENTRY и т.д.)
    key = str(reason).strip().upper()
    if key in _REASON_MAP:
        return _REASON_MAP[key]

    # Затем проверяем канонические коды (lowercase)
    canonical = str(reason).strip().lower()
    if canonical in _VALID_REASON_CODES:
        return cast(ReasonCode, canonical)

    # Если ничего не подошло - возвращаем invalid_data
    return INVALID_DATA


def get_reason_category(reason: ReasonCode) -> str:
    """
    Возвращает категорию кода причины.

    Категории:
    - success: Успешные сигналы
    - weak_signal: Отклонения по слабым сигналам
    - disagreement: Несогласованность между детекторами
    - filter: Отклонения по фильтрам
    - data_issue: Проблемы с данными
    - risk: Ограничения и риски

    Args:
        reason: Код причины

    Returns:
        Название категории или "unknown"

    Examples:
        >>> get_reason_category("trend_confirmed")
        "success"
        >>> get_reason_category("invalid_data")
        "data_issue"
        >>> get_reason_category("time_filter")
        "filter"
    """
    meta1 = _REASON_CODE_DEFINITIONS.get(reason)
    return meta1["category"] if meta1 else "unknown"


def is_successful_reason(reason: ReasonCode) -> bool:
    """
    Проверяет, является ли код причины успешным сигналом.

    Args:
        reason: Код причины

    Returns:
        True если код относится к категории "success", иначе False

    Examples:
        >>> is_successful_reason("trend_confirmed")
        True
        >>> is_successful_reason("hierarchical_confirmed")
        True
        >>> is_successful_reason("invalid_data")
        False
        >>> is_successful_reason("time_filter")
        False
    """
    return get_reason_category(reason) == "success"


class Direction(Enum):
    """Направление торговли"""
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"


@dataclass
class SignalOut:
    """Выходной сигнал от CUSUM детектора"""
    signal: int  # 1 ->'long', -1->'short',0-> 'wait'
    strength: float
    reason: ReasonCode
    z: float
    cusum_pos: float
    cusum_neg: float
    vola_flag: bool


class Detector(ABC):
    """
    Абстрактный базовый класс для всех детекторов.

    ✅ ИСПРАВЛЕНИЕ: Конструктор должен принимать name, но НЕ timeframe
    Конкретные реализации могут добавить свои параметры через **kwargs
    """

    def __init__(self, name: str = None):
        """
        Инициализация базового детектора.

        Args:
            name: Имя детектора для логирования
        """
        self.name = name or self.__class__.__name__
        self.logger = logging.getLogger(self.name)
        self._last_signal = None
        self._created_at = datetime.now(UTC)

    @abstractmethod
    def get_required_bars(self) -> Dict[Timeframe, int]:
        """Возвращает минимальное количество баров для каждого таймфрейма"""
        pass

    @abstractmethod
    async def analyze(self, data: Dict[Timeframe, pd.DataFrame]) -> DetectorSignal:
        """Основной метод анализа"""
        pass

    def validate_data(self, data: Dict[Timeframe, pd.DataFrame]) -> bool:
        """Валидация входных данных"""
        return validate_market_data(data)

    def get_status(self) -> Dict[str, Any]:
        """Базовый статус для мониторинга"""
        sig = self._last_signal or {}
        return {
            "name": self.name,
            "class": self.__class__.__name__,
            "created_at": self._created_at.isoformat() + "Z",
            "last_signal_ok": sig.get("ok"),
            "last_signal_direction": sig.get("direction"),
            "last_signal_confidence": sig.get("confidence"),
        }

    def _set_last_signal(self, signal: DetectorSignal) -> None:
        """Protected helper для сохранения последнего сигнала"""
        self._last_signal = signal


# --- Конвертеры сигналов ---
def normalize_signal(raw: Any) -> DetectorSignal:
    """
    ПРОСТО приводит произвольный сигнал к DetectorSignal стандарту.
    НЕ исправляет логику детекторов!
    """
    if isinstance(raw, dict):
        # Берем значения как есть - ответственность на детекторе
        d = normalize_direction(raw.get("direction"))
        conf = float(raw.get("confidence", 0.0))
        reason = map_reason(raw.get("reason", ""))
        ok = bool(raw.get("ok", False))  # Не пытаемся угадывать!
        metadata = dict(raw.get("metadata", {}))

        # Просто копируем дополнительные поля в metadata
        for k in ("timeframe", "role", "vola_flag", "z_score", "trend_confidence", "entry_confidence", "consistency"):
            if k in raw and k not in metadata:
                metadata[k] = raw[k]

        # ВОЗВРАЩАЕМ КАК ЕСТЬ - детектор сам отвечает за логику
        result: DetectorSignal = {
            "ok": ok,
            "direction": cast(DirectionLiteral, d),
            "confidence": conf,
            "reason": reason,
            "metadata": cast(DetectorMetadata, metadata),
        }
        return result

    # Обработка не-dict объектов (только конвертация формата)
    d = normalize_direction(getattr(raw, "direction", None))
    conf = float(getattr(raw, "confidence", 0.0))
    reason = map_reason(getattr(raw, "reason", ""))
    ok = bool(getattr(raw, "ok", False))  # Не угадываем!
    meta1 = {}

    for k in ("metadata", "timeframe", "role", "vola_flag", "z_score", "trend_confidence", "entry_confidence",
              "consistency"):
        if hasattr(raw, k):
            v = getattr(raw, k)
            if k == "metadata" and isinstance(v, dict):
                meta1.update(v)
            else:
                meta1[k] = v

    # ПРОСТО КОНВЕРТИРУЕМ ФОРМАТ
    result: DetectorSignal = {
        "ok": ok,
        "direction": cast(DirectionLiteral, d),
        "confidence": conf,
        "reason": reason,
        "metadata": cast(DetectorMetadata, meta),
    }
    return result

def normalize_direction(direction: Any) -> Literal[1, -1, 0]:
    if direction is None:
        return 0
    if isinstance(direction, str):
        d = direction.strip().upper()
        if d in ("BUY", "LONG", "BULL"):
            return 1
        if d in ("SELL", "SHORT", "BEAR"):
            return -1
        return 0
    if isinstance(direction, (int, float)):
        if direction > 0:
            return 1
        if direction < 0:
            return -1
        return 0
    # enum fallback
    name = getattr(direction, "name", None)
    if isinstance(name, str):
        return normalize_direction(name)
    return 0


# ============================================================================
# === DIRECTION CONVERSION FUNCTIONS (v2.0) ==================================
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
        return cast(DirectionStr, direction.side)

    mapping: Dict[int, DirectionStr] = {1: "BUY", -1: "SELL", 0: "FLAT"}
    return mapping[direction]


def direction_to_int(direction: Union[Direction, DirectionStr, str]) -> DirectionLiteral:
    """
    Конвертация Direction/str → числовое значение.

    Args:
        direction: Direction enum или строка ("BUY", "SELL", "FLAT")

    Returns:
        Числовое значение (1, -1, 0)

    Examples:
        >>> direction_to_int(Direction.BUY)
        1
        >>> direction_to_int("BUY")
        1
        >>> direction_to_int("SELL")
        -1

    Raises:
        KeyError: Если передана некорректная строка
    """
    if isinstance(direction, Direction):
        return cast(DirectionLiteral, direction.value)

    if isinstance(direction, str):
        mapping: Dict[str, DirectionLiteral] = {
            "BUY": 1, "LONG": 1, "BULL": 1,
            "SELL": -1, "SHORT": -1, "BEAR": -1,
            "FLAT": 0, "HOLD": 0, "WAIT": 0
        }
        return mapping[direction.upper()]

    return cast(DirectionLiteral, direction)


def side_to_direction(side: str) -> Direction:
    """
    Конвертация строка → Direction enum.

    Args:
        side: Строка "BUY", "SELL" или "FLAT" (регистронезависимо)

    Returns:
        Direction enum

    Examples:
        >>> side_to_direction("BUY")
        Direction.BUY
        >>> side_to_direction("sell")
        Direction.SELL
        >>> side_to_direction("LONG")
        Direction.BUY

    Raises:
        KeyError: Если передана некорректная строка
    """
    mapping = {
        "BUY": Direction.BUY,
        "LONG": Direction.BUY,
        "BULL": Direction.BUY,
        "SELL": Direction.SELL,
        "SHORT": Direction.SELL,
        "BEAR": Direction.SELL,
        "FLAT": Direction.FLAT,
        "HOLD": Direction.FLAT,
        "WAIT": Direction.FLAT,
    }
    return mapping[side.upper()]


def normalize_direction_v2(value: Any) -> Direction:
    """
    Универсальная конвертация произвольного значения в Direction enum.

    НОВАЯ ВЕРСИЯ (v2.0): Возвращает Direction enum вместо int.
    Для старого поведения используйте normalize_direction().

    Поддерживает:
        - Direction enum (возвращает как есть)
        - Строки: "BUY", "SELL", "FLAT", "LONG", "SHORT", "HOLD"
        - Числа: > 0 → BUY, < 0 → SELL, 0 → FLAT
        - None → FLAT

    Args:
        value: Значение для конвертации

    Returns:
        Direction enum

    Examples:
        >>> normalize_direction_v2("BUY")
        Direction.BUY
        >>> normalize_direction_v2(1)
        Direction.BUY
        >>> normalize_direction_v2(-1)
        """
def validate_market_data(data: Dict[Timeframe, pd.DataFrame]) -> bool:
    """Light-версия без тяжёлых pandas-операций"""
    if not isinstance(data, dict) or not data:
        return False

    for tf, df in data.items():
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False

        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                return False

    return True

def validate_system_status(st: SystemStatus) -> SystemStatus:
    """Заполняет пропущенные поля SystemStatus дефолтами."""
    defaults = {
        "current_regime": "uncertain",
        "regime_confidence": 0.0,
        "trades_today": 0,
        "max_daily_trades": 0,
        "total_trades": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "current_parameters": {},
    }
    result = defaults.copy()
    result.update({k: v for k, v in st.items() if v is not None})
    return cast(SystemStatus, result)

# ====Сигналы свечей тайм-фреймов 10с, 1мин, 5мин====
FEATURE_NAME_MAP: dict[str, dict[str, tuple[str, str]]] = {
    "1m": {
        "ema3": ("ema3", "REAL"),
        "ema7": ("ema7", "REAL"),
        "ema9": ("ema9", "REAL"),
        "ema15": ("ema15", "REAL"),
        "ema30": ("ema30", "REAL"),
        "cmo14": ("cmo14", "REAL"),
        "adx14": ("adx14", "REAL"),
        "plus_di14": ("plus_di14", "REAL"),
        "minus_di14": ("minus_di14", "REAL"),
        "atr14": ("atr14", "REAL"),
        "cusum": ("cusum", "REAL"),
        "cusum_state": ("cusum_state", "INTEGER"),
        "cusum_zscore": ("cusum_zscore", "REAL"),
        "cusum_conf": ("cusum_conf", "REAL"),
        "cusum_reason": ("cusum_reason", "TEXT"),
        "cusum_price_mean": ("cusum_price_mean", "REAL"),
        "cusum_price_std": ("cusum_price_std", "REAL"),
        "cusum_pos": ("cusum_pos", "REAL"),
        "cusum_neg": ("cusum_neg", "REAL"),
    },
    "5m": {
        # Базовые изменения цены
        "price_change_5": ("price_change_5", "REAL"),

        # Группа 1: Тренд и импульс (4)
        "trend_momentum_z": ("trend_momentum_z", "REAL"),
        "cmo_14": ("cmo_14", "REAL"),
        "macd_histogram": ("macd_histogram", "REAL"),
        "trend_acceleration_ema7": ("trend_acceleration_ema7", "REAL"),

        # Группа 2: Волатильность и режим (3)
        "regime_volatility": ("regime_volatility", "REAL"),
        "bb_width": ("bb_width", "REAL"),
        "adx_14": ("adx_14", "REAL"),
        "plus_di_14": ("plus_di_14", "REAL"),
        "minus_di_14": ("minus_di_14", "REAL"),
        "atr_14_normalized": ("atr_14_normalized", "REAL"),

        # Группа 3: Объём (1)
        "volume_ratio_ema3": ("volume_ratio_ema3", "REAL"),

        # Группа 4: Структура свечи (3)
        "candle_relative_body": ("candle_relative_body", "REAL"),
        "upper_shadow_ratio": ("upper_shadow_ratio", "REAL"),
        "lower_shadow_ratio": ("lower_shadow_ratio", "REAL"),

        # Группа 5: Положение цены (2)
        "price_vs_vwap": ("price_vs_vwap", "REAL"),
        "bb_position": ("bb_position", "REAL"),

        # Группа 6: CUSUM с 1m (5)
        "cusum_1m_recent": ("cusum_1m_recent", "INTEGER"),
        "cusum_1m_quality_score": ("cusum_1m_quality_score", "REAL"),
        "cusum_1m_trend_aligned": ("cusum_1m_trend_aligned", "INTEGER"),
        "cusum_1m_price_move": ("cusum_1m_price_move", "REAL"),

        # Группа 7: Микроструктура 1m (3)
        "is_trend_pattern_1m": ("is_trend_pattern_1m", "INTEGER"),
        "body_to_range_ratio_1m": ("body_to_range_ratio_1m", "REAL"),
        "close_position_in_range_1m": ("close_position_in_range_1m", "REAL"),

        # Группа 8: CUSUM 5m
        "cusum": ("cusum", "REAL"),
        "cusum_state": ("cusum_state", "INTEGER"),
        "cusum_zscore": ("cusum_zscore", "REAL"),
        "cusum_conf": ("cusum_conf", "REAL"),  # |z| * z_to_conf
        "cusum_reason": ("cusum_reason", "TEXT"),
        "cusum_price_mean": ("cusum_price_mean", "REAL"),
        "cusum_price_std": ("cusum_price_std", "REAL"),
        "cusum_pos": ("cusum_pos", "REAL"),
        "cusum_neg": ("cusum_neg", "REAL"),
    },
}
# ============ Время ===============================
_SIM_TIME_MS: Optional[int] = None


def get_current_timestamp_ms() -> int:
    """Return current time in ms: simulated in BACKTEST (if set), otherwise wall-clock."""
    if _SIM_TIME_MS is not None:
        return _SIM_TIME_MS
    return time.time_ns() // 1_000_000


def set_simulated_time(ts_ms: Optional[int]) -> None:
    """Set current simulated time in milliseconds."""
    global _SIM_TIME_MS
    _SIM_TIME_MS = ts_ms


def clear_simulated_time() -> None:
    """Disable simulation mode (use wall-clock again)."""
    set_simulated_time(None)


def is_simulated_time_enabled() -> bool:
    """True if get_current_timestamp_ms() will return simulated time."""
    return _SIM_TIME_MS is not None


def create_correlation_id() -> str:
    """Генерация correlation_id"""
    ts = get_current_timestamp_ms()
    uid = str(uuid.uuid4())[:8]
    return f"{ts}-{uid}"

# =============================================================================
# === RE-EXPORTS ДЛЯ СОВМЕСТИМОСТИ ===========================================
# =============================================================================

# Ре-экспортируем ключевые детекторы для использования во всех модулях
try:
    from iqts_detectors import (
        RoleBasedOnlineTrendDetector,
        MLGlobalTrendDetector,
        GlobalTrendDetector
    )
except ImportError:
    # Fallback для случаев когда iqts_detectors недоступен
    class RoleBasedOnlineTrendDetector(Detector):
        """Заглушка для совместимости"""

        def get_required_bars(self) -> Dict[Timeframe, int]: return {}

        async def analyze(self, data): return normalize_signal({"ok": False})


    class MLGlobalTrendDetector(Detector):
        """Заглушка для совместимости"""

        def get_required_bars(self) -> Dict[Timeframe, int]: return {}

        async def analyze(self, data): return normalize_signal({"ok": False})


    class GlobalTrendDetector(Detector):
        """Заглушка для совместимости"""

        def get_required_bars(self) -> Dict[Timeframe, int]: return {}

        async def analyze(self, data): return normalize_signal({"ok": False})

# =============================================================================
# === ЭКСПОРТ ==================================================================
# =============================================================================

__all__ = [
    # Типы и литералы
    "Timeframe",
    "DirectionLiteral",

    # Direction types (v2.0)
    "Direction",
    "DirectionStr",
    "DirectionType",

    "MarketRegimeLiteral",
    "ReasonCode",
    "ExecutionMode",
    "ConnectionStatus",
    "Callable",
    # Метаданные и сигналы
    "DetectorMetadata",
    "DetectorSignal",
    "TradeSignalIQTS",
    "RegimeInfo",
    "RiskContext",
    "SystemStatus",
    "StrategySignal",
    "OrderReq",
    "OrderUpd",
    "PositionSnapshot",
    "Candle1m",

    # Lifecycle types
    "BotLifecycleEvent",
    "ConnectionState",
    "BotLifecycleEventHandler",
    "ExchangeEventHandler",
    "MarketEventHandler",
    "AlertCallback",
    "BotLifecycleError",
    "ComponentInitializationError",

    # Конфигурации
    "RiskConfig", "QualityDetectorConfig",
    "MonitoringConfig", "TradingSystemConfig",

    # Протоколы
    "DetectorInterface", "RiskManagerInterface", "MonitoringInterface",
    "TradingSystemInterface", "StrategyInterface",
    "PositionManagerInterface", "ExchangeManagerInterface",
    "MarketAggregatorInterface", "MainBotInterface",
    "FEATURE_NAME_MAP",

    # Утилиты
    "normalize_signal", "normalize_direction",

    # Direction conversion (v2.0)
    "direction_to_side",
    "direction_to_int",
    "side_to_direction",
    "normalize_direction_v2",

     "validate_market_data",
    "validate_system_status", "normalize_trading_hours",
    "map_reason", "get_reason_category", "is_successful_reason",
    "get_current_timestamp_ms",
    "create_correlation_id",
    "set_simulated_time",
    "safe_nested_getattr",

    # Классы
    "Direction", "SignalOut", "Detector",
    "TradeResult", "NetConnState", "clear_simulated_time",
    "ExchangeEvent", "PriceFeed", "OrderType", "REQUIRED_OHLCV_COLUMNS",
    # Детекторы (re-exports)
    "RoleBasedOnlineTrendDetector",
    "MLGlobalTrendDetector",
    "GlobalTrendDetector",

]
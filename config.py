"""
config.py - Конфигурация торгового бота
Все основные параметры и настройки системы
"""

from __future__ import annotations
from typing import Dict, Any, List, Literal, Optional
from decimal import Decimal
import os
from pathlib import Path
from dotenv import load_dotenv
# Загрузка переменных окружения из .env файла
try:
    load_dotenv()
except ImportError:
    print("python-dotenv не установлен. Используются системные переменные окружения.")

# === ОСНОВНЫЕ ПУТИ И DSN ===

# Базовая директория данных
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Строки подключения к БД
MARKET_DB_DSN: str = f"sqlite:///{DATA_DIR}/market_data.sqlite"
TRADING_DB_DSN: str = f"sqlite:///{DATA_DIR}/trading_data.sqlite"

#  загрузка истории для прогрева индикаторов
HISTORY_CONFIG = {
    "days_back": 1,  # Сколько дней истории загружать
}

# Таблицы БД
TABLES: Dict[str, str] = {
    "candles_1m":  "candles_1m",    # 1мин свечи + индикаторы 1m
    "candles_5m":  "candles_5m",    # 5мин свечи + ML-сигналы 5m
    "symbols":     "symbols",
    "positions":   "positions",
    "orders":      "orders",
    "trades":      "trades",
}

# === РЕЖИМЫ РАБОТЫ ===

# Основные флаги
DEMO_MODE: bool = True
IS_TESTNET: bool = False
EXECUTION_MODE: Literal["LIVE", "DEMO", "BACKTEST"] = "DEMO"

# === WEBSOCKET НАСТРОЙКИ ===

# Основные WebSocket настройки для Binance Futures
WEBSOCKET_CONFIG: Dict[str, Any] = {
    # Основные URL
    "futures_base_url": "wss://fstream.binance.com/ws/",
    "spot_base_url": "wss://stream.binance.com:9443/ws/",

    # Настройки соединения
    "ping_interval": int(os.getenv("WS_PING_INTERVAL", "20")),
    "ping_timeout": int(os.getenv("WS_PING_TIMEOUT", "10")),
    "close_timeout": int(os.getenv("WS_CLOSE_TIMEOUT", "10")),
    "max_size": int(os.getenv("WS_MAX_SIZE", "1000000")),  # 1MB

    # Настройки переподключения
    "auto_reconnect": bool(os.getenv("WS_AUTO_RECONNECT", "true").lower() == "true"),
    "reconnect_delay": int(os.getenv("WS_RECONNECT_DELAY", "5")),  # секунды
    "max_reconnect_attempts": int(os.getenv("WS_MAX_RECONNECT_ATTEMPTS", "10")),
    "exponential_backoff": bool(os.getenv("WS_EXPONENTIAL_BACKOFF", "true").lower() == "true"),
    "max_reconnect_delay": int(os.getenv("WS_MAX_RECONNECT_DELAY", "60")),  # максимальная задержка

    # Настройки буферизации
    "buffer_size": int(os.getenv("WS_BUFFER_SIZE", "1000")),
    "flush_interval": int(os.getenv("WS_FLUSH_INTERVAL", "1000")),  # миллисекунды
    "max_buffer_age": int(os.getenv("WS_MAX_BUFFER_AGE", "5000")),  # миллисекунды

    # Настройки heartbeat
    "heartbeat_interval": int(os.getenv("WS_HEARTBEAT_INTERVAL", "180")),  # 3 минуты
    "heartbeat_timeout": int(os.getenv("WS_HEARTBEAT_TIMEOUT", "10")),

    # Настройки обработки сообщений
    "message_queue_size": int(os.getenv("WS_MESSAGE_QUEUE_SIZE", "10000")),

    # Настройки логирования WebSocket
    "log_reconnects": bool(os.getenv("WS_LOG_RECONNECTS", "true").lower() == "true"),
}


# === ТОРГОВЫЕ ПАРАМЕТРЫ ===

# Комиссии и балансы
FEE_RATE: float = 0.0004  # 0.04% для Binance Futures
DEMO_BALANCE_USDT: int = 10000

# Основные торговые лимиты
MAX_POSITIONS: int = 5
POSITION_SIZE_PERCENT: float = 20.0  # % от баланса на позицию
MAX_DAILY_TRADES: int = 20
MAX_DAILY_LOSS_PERCENT: float = 5.0

# === ВРЕМЕННЫЕ ИНТЕРВАЛЫ ===

# Агрегация данных
AGGREGATION_TIMEFRAME: str = "1m"
CANDLE_INTERVAL_MS: int = 20_000

# Таймауты и интервалы
CONNECTION_TIMEOUT_SECONDS: int = 20
HEARTBEAT_INTERVAL_SECONDS: int = 20
RECONNECT_DELAY_SECONDS: int = 5
MAX_RECONNECT_ATTEMPTS: int = 10

# === СИМВОЛЫ И ИНСТРУМЕНТЫ ===

# Целевое количество символов
TARGET_SYMBOLS_COUNT: int = 50

# Основные торговые символы
TRADING_SYMBOLS: List[str] = [
    "ETHUSDT"
]

# Потоки данных
STREAMS: List[str] = [
    "aggTrade",
    "kline_1m",
    "bookTicker",
    "miniTicker"
]

STRATEGY_PARAMS: Dict[str, Any] = {
    "entry_stoploss_pct": 0.17,
    "cooldown_bars": 6,
    "history_window": 50,

     # ✅ COSUM SENSITIVITY
    "cosum_sensitivity": {
            "min_reversal_threshold_pct": 2.25,  # ⬆️ Увеличено для снижения чувствительности
            "min_absolute_change": 0.04,  # ⬆️ Увеличен порог
            "require_strong_peak": True,  # ✅ Строгая проверка: True
            "lookback_bars": 8  # ⬆️ Больше свечей для анализа
        }
}

# === НАСТРОЙКИ БИРЖ ===

# Режимы исполнения с поддержкой переменных окружения
EXECUTION_MODES: Dict[str, Dict[str, Any]] = {
    "LIVE": {
        "base_url": os.getenv("BINANCE_FUTURES_API_URL", "https://fapi.binance.com"),
        "ws_url": os.getenv("BINANCE_FUTURES_WS_URL", "wss://fstream.binance.com"),
        "demo_mode": False,
        "api_key": os.getenv("BINANCE_API_KEY", ""),
        "api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "timeout_seconds": int(os.getenv("API_TIMEOUT_SECONDS", "30")),
        "testnet": False
    },
    "DEMO": {
        "base_url": os.getenv("BINANCE_FUTURES_API_URL", "https://fapi.binance.com"),
        "ws_url": os.getenv("BINANCE_FUTURES_WS_URL", "wss://fstream.binance.com"),
        "demo_mode": True,
        "latency_ms": int(os.getenv("DEMO_LATENCY_MS", "50")),
        "slippage_pct": float(os.getenv("DEMO_SLIPPAGE_PCT", "0.01")),
        "timeout_seconds": int(os.getenv("API_TIMEOUT_SECONDS", "30")),
        "api_key": "",  # Не требуется для DEMO
        "api_secret": "",
        "testnet": False
    },
    "BACKTEST": {
        "base_url": "http://localhost:8080",
        "ws_url": None,
        "demo_mode": True,
        "timeout_seconds": 30,
        "api_key": "",
        "api_secret": "",
        "testnet": False
    }
}

# === НАСТРОЙКИ СИМВОЛОВ ===

# Настройки по символам
SYMBOL_SETTINGS: Dict[str, Dict[str, Any]] = {

    "ETHUSDT": {
        "leverage": 10,
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.001"),
        "min_notional": Decimal("5.0"),
        "price_precision": 2,
        "quantity_precision": 3
    },
    "DEFAULT": {
        "leverage": 5,
        "tick_size": Decimal("0.0001"),
        "step_size": Decimal("0.001"),
        "min_notional": Decimal("5.0"),
        "price_precision": 4,
        "quantity_precision": 3
    }
}

# === НАСТРОЙКИ ФЬЮЧЕРСОВ ===

# Общие настройки фьючерсов
FUTURES_SETTINGS: Dict[str, Any] = {
    "margin_type": "ISOLATED",  # ISOLATED или CROSSED
    "default_leverage": 10,
    "max_leverage": 20,
    "position_mode": "HEDGE",  # HEDGE или ONE_WAY
    "auto_add_margin": False,
    "dual_side_position": True
}

# === РИСК-МЕНЕДЖМЕНТ ===

# Риск-менеджмент для фьючерсов (единственный источник статических SL/TP)
FUTURES_RISK_MANAGEMENT: Dict[str, Any] = {
    "max_leverage_per_symbol": {
        "ETHUSDT": 10,
        "DEFAULT": 10
    },
    "stop_loss_percent": 0.2,  # ✅ ЕДИНСТВЕННЫЙ источник входного стоп-лосса
    "take_profit_percent": 1.8,
    "max_drawdown_percent": 10.0,
    "daily_loss_limit_percent": 3.0,
    "cooldown_bars_after_loss": 3,
    "max_concurrent_positions": 5,
    "min_time_between_trades_minutes": 3
}

# === НАСТРОЙКИ АДАПТИВНОГО ВЫХОДА ===

# Настройки trailing stop (ТОЛЬКО для динамического trailing)
TRAILING_STOP_CONFIG: Dict[str, Any] = {
    # Основные настройки
    "enabled": bool(os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"),
    "trailing_distance_percent": float(os.getenv("TRAILING_DISTANCE_PERCENT", "0.05")),  # 0.05% от пика

    # Условия активации
    "min_profit_activation_percent": float(os.getenv("TRAILING_MIN_PROFIT", "0.2")),  # Минимум 0.2% прибыли
    "activation_delay_candles": int(os.getenv("TRAILING_ACTIVATION_DELAY", "3")),  # Ждать 3 свечи после входа

    # Настройки по символам (если нужна кастомизация)
    "symbol_overrides": {

        "ETHUSDT": {
            "trailing_distance_percent": 0.05,
            "min_profit_activation_percent": 0.2
        }

    },

    # Дополнительные настройки
    "update_frequency": "every_candle",  # "every_candle" или "price_change"
    "price_change_threshold_percent": 0.05,  # Обновлять при изменении цены на 0.05%
    "max_updates_per_position": int(os.getenv("TRAILING_MAX_UPDATES", "20")),  # Лимит обновлений на позицию

    # Логирование и мониторинг
    "log_updates": bool(os.getenv("TRAILING_LOG_UPDATES", "true").lower() == "true"),
    "alert_on_trigger": bool(os.getenv("TRAILING_ALERT_TRIGGER", "true").lower() == "true")
}

# === КОНФИГУРАЦИЯ СТРАТЕГИЙ ===


# === КОНФИГУРАЦИЯ БЭКТЕСТА ===

# Настройки стратегии для бэктеста (убрано дублирование)
BACKTEST_STRATEGY_CONFIG: Dict[str, Any] = {
    "strategy_name": "Victory",
    # Все остальные параметры берутся из основной конфигурации
}

# Настройки БД для бэктеста
BACKTEST_DB_CONFIG: Dict[str, Any] = {
    "market_db": MARKET_DB_DSN,
    "trading_db": TRADING_DB_DSN,
    "chunk_size": 1000,
    "memory_limit_mb": 512,
    "parallel_processing": True,
    "save_intermediate_results": True
}

BACKTEST_SPEED: float = float(os.getenv("BACKTEST_SPEED", "550.0"))

# === ЛОГИРОВАНИЕ И МОНИТОРИНГ ===

# Настройки логирования
LOGGING_CONFIG: Dict[str, Any] = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file_path": f"{DATA_DIR}/trading_bot.log",
    "max_file_size_mb": int(os.getenv("LOG_MAX_SIZE_MB", "100")),
    "backup_count": int(os.getenv("LOG_BACKUP_COUNT", "5")),
    "console_output": bool(os.getenv("LOG_CONSOLE", "true").lower() == "true")
}

# Настройки метрик
METRICS_CONFIG: Dict[str, Any] = {
    "enabled": bool(os.getenv("METRICS_ENABLED", "true").lower() == "true"),
    "flush_interval_seconds": int(os.getenv("METRICS_FLUSH_INTERVAL", "60")),
    "batch_size": int(os.getenv("METRICS_BATCH_SIZE", "100")),
    "retention_days": int(os.getenv("METRICS_RETENTION_DAYS", "30")),
    "export_format": os.getenv("METRICS_EXPORT_FORMAT", "json"),
    "export_path": f"{DATA_DIR}/metrics"
}

# === АЛЕРТИНГ И УВЕДОМЛЕНИЯ ===

# Настройки алертов
ALERT_CONFIG: Dict[str, Any] = {
    "enabled": bool(os.getenv("ALERTS_ENABLED", "true").lower() == "true"),
    "channels": ["log", "file"],  # Можно добавить "telegram", "email"
    "levels": {
        "position_opened": "INFO",
        "position_closed": "INFO",
        "stop_loss_hit": "WARNING",
        "daily_loss_limit": "ERROR",
        "connection_lost": "ERROR",
        "system_error": "CRITICAL"
    },
    "file_path": f"{DATA_DIR}/alerts.log",
    "max_alerts_per_hour": int(os.getenv("ALERTS_MAX_PER_HOUR", "100"))
}

# === ПРОИЗВОДИТЕЛЬНОСТЬ И ОГРАНИЧЕНИЯ ===

# Настройки производительности
PERFORMANCE_CONFIG: Dict[str, Any] = {
    "max_memory_usage_mb": int(os.getenv("MAX_MEMORY_MB", "1024")),
    "max_cpu_usage_percent": int(os.getenv("MAX_CPU_PERCENT", "80")),
    "gc_interval_seconds": int(os.getenv("GC_INTERVAL", "300")),
    "connection_pool_size": int(os.getenv("CONNECTION_POOL_SIZE", "10")),
    "async_task_limit": int(os.getenv("ASYNC_TASK_LIMIT", "50")),
    "buffer_sizes": {
        "candle_buffer": int(os.getenv("CANDLE_BUFFER_SIZE", "1000")),
        "order_buffer": int(os.getenv("ORDER_BUFFER_SIZE", "500")),
        "event_buffer": int(os.getenv("EVENT_BUFFER_SIZE", "1000"))
    }
}

# === СРЕДА И РАЗВЕРТЫВАНИЕ ===

# Настройки среды
ENVIRONMENT_CONFIG: Dict[str, Any] = {
    "environment": os.getenv("TRADING_ENV", "development"),
    "debug_mode": os.getenv("DEBUG", "false").lower() == "true",
    "health_check_port": int(os.getenv("HEALTH_CHECK_PORT", "8080")),
    "metrics_port": int(os.getenv("METRICS_PORT", "8081")),
    "graceful_shutdown_timeout": int(os.getenv("SHUTDOWN_TIMEOUT", "30")),
    "startup_timeout": int(os.getenv("STARTUP_TIMEOUT", "60"))
}


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def get_trailing_stop_config(symbol: str = None) -> Dict[str, Any]:
    """Получить настройки trailing stop для символа."""
    base_config = TRAILING_STOP_CONFIG.copy()

    if symbol and symbol in TRAILING_STOP_CONFIG.get("symbol_overrides", {}):
        overrides = TRAILING_STOP_CONFIG["symbol_overrides"][symbol]
        base_config.update(overrides)

    return {
        "enabled": base_config["enabled"],
        "trailing_distance_percent": base_config["trailing_distance_percent"],
        "min_profit_activation_percent": base_config["min_profit_activation_percent"],
        "activation_delay_candles": base_config["activation_delay_candles"],
        "update_frequency": base_config["update_frequency"],
        "price_change_threshold_percent": base_config["price_change_threshold_percent"],
        "max_updates_per_position": base_config["max_updates_per_position"],
        "log_updates": base_config["log_updates"]
    }


def get_symbol_config(symbol: str) -> Dict[str, Any]:
    """Получить настройки символа из SYMBOL_SETTINGS."""
    return SYMBOL_SETTINGS.get(symbol, SYMBOL_SETTINGS["DEFAULT"]).copy()



def get_websocket_config() -> Dict[str, Any]:
    """Получить конфигурацию WebSocket."""
    return WEBSOCKET_CONFIG.copy()


# === ВАЛИДАЦИЯ КОНФИГУРАЦИИ ===

def validate_config() -> List[str]:
    """Валидация конфигурации. Возвращает список ошибок."""
    errors = []

    # Проверяем обязательные параметры
    if not TRADING_SYMBOLS:
        errors.append("TRADING_SYMBOLS cannot be empty")

    if FEE_RATE < 0 or FEE_RATE > 0.01:
        errors.append(f"Invalid FEE_RATE: {FEE_RATE}")

    if DEMO_BALANCE_USDT <= 0:
        errors.append(f"Invalid DEMO_BALANCE_USDT: {DEMO_BALANCE_USDT}")

    if CANDLE_INTERVAL_MS <= 0:
        errors.append(f"Invalid CANDLE_INTERVAL_MS: {CANDLE_INTERVAL_MS}")

    # Проверяем пути к файлам
    try:
        DATA_DIR.mkdir(exist_ok=True)
    except Exception as e:
        errors.append(f"Cannot create data directory: {e}")

    # Проверяем настройки WebSocket
    ws_config = WEBSOCKET_CONFIG
    if ws_config["ping_interval"] <= 0:
        errors.append("WebSocket ping_interval must be positive")

    if ws_config["buffer_size"] <= 0:
        errors.append("WebSocket buffer_size must be positive")

    # Проверяем API ключи для LIVE режима
    if EXECUTION_MODE == "LIVE":
        live_config = EXECUTION_MODES["LIVE"]
        if not live_config["api_key"]:
            errors.append("BINANCE_API_KEY environment variable is required for LIVE mode")
        if not live_config["api_secret"]:
            errors.append("BINANCE_API_SECRET environment variable is required for LIVE mode")

    return errors


def get_default_config():
    return {
        'symbol': 'ETHUSDT',
        'timeframes': ['1m', '5m'],
        'loop_interval': 10,
        'max_open_positions': 3,

        'trading_system': {
            'account_balance': 100000,
            'max_daily_trades': 15,
            'max_daily_loss': 0.02,

            'quality_detector': {
                'global_timeframe': '5m',
                'trend_timeframe': '1m',
                'entry_timeframe': '1m',
                'max_daily_trades': 15,
                'min_volume_ratio': 1.3,
                'max_volatility_ratio': 1.4,

                # ⚠️ ДОБАВЬТЕ КОНФИГ ДЛЯ ГЛОБАЛЬНОГО ДЕТЕКТОРА
                'global_detector': {
                    'timeframe': '5m',
                    'model_path': 'models/ml_global_5m_lgbm.joblib',
                    'use_fallback': False,
                    'name': 'ml_global_5m'
                }
            },

            'risk_management': {
                'max_position_risk': 0.02,
                'max_daily_loss': 0.05,
                'atr_periods': 14,
                'stop_atr_multiplier': 2.0,
                'tp_atr_multiplier': 3.0
            }
        },

        'monitoring': {
            'enabled': True,
            'telegram': {'enabled': False},
            'email': {'enabled': False}
        },

        'logging': {
            'file_enabled': True,
            'file_path': 'enhanced_trading_bot.log'
        }
    }

def build_runtime_config(trading_logger: Optional[Any] = None) -> Dict[str, Any]:
    """
    Единый конфиг для запуска бота и агрегатора.
    Args:
        trading_logger: Экземпляр TradingLogger с подключением к БД.
                       Обязателен только в режиме BACKTEST.
    Returns:
        Полная конфигурация для инициализации бота и MarketAggregator.
    """
    exec_cfg = EXECUTION_MODES["DEMO"] if EXECUTION_MODE == "DEMO" else EXECUTION_MODES.get(EXECUTION_MODE, {})

    # Для BACKTEST режима получаем временные метки из БД
    backtest_config = {
        "start_time_ms": None,
        "end_time_ms": None,
        "speed": BACKTEST_SPEED,
        "data_source": "database"
    }

    if EXECUTION_MODE == "BACKTEST":
        if trading_logger is None:
            raise ValueError("trading_logger is required for BACKTEST mode")

        try:
            from market_data_utils import MarketDataUtils
            utils = MarketDataUtils(trading_logger.market_engine)
            start_ts, end_ts = utils.get_backtest_range(TRADING_SYMBOLS)

            backtest_config.update({
                "start_time_ms": int(start_ts),
                "end_time_ms": int(end_ts)
            })
        except ImportError:
            raise RuntimeError("market_data_utils is required for BACKTEST mode but not available")
        except Exception as e:
            print(f"Error getting backtest range: {e}")
            raise

    return {
        "execution_mode": EXECUTION_MODE,
        "symbols": TRADING_SYMBOLS,
        "interval_ms": CANDLE_INTERVAL_MS,
        "market_db_dsn": MARKET_DB_DSN,
        "trading_db_dsn": TRADING_DB_DSN,

        "backtest": backtest_config,

        "exchange": {
            "base_url": exec_cfg.get("base_url"),
            "ws_url": exec_cfg.get("ws_url"),
            "timeout_seconds": exec_cfg.get("timeout_seconds", 30),
            "demo_mode": exec_cfg.get("demo_mode", True),
            "api_key": exec_cfg.get("api_key", ""),
            "api_secret": exec_cfg.get("api_secret", ""),
        },
        "websocket": WEBSOCKET_CONFIG,

        "market_data": {
            "interval_ms": CANDLE_INTERVAL_MS,
            "loop_interval": 10,
            "history_limit": 50,
            "buffer_size": PERFORMANCE_CONFIG["buffer_sizes"]["candle_buffer"]
        },
        "database": {
            "market_db_path": MARKET_DB_DSN,
            "trades_db_path": TRADING_DB_DSN,
            "pool_size": PERFORMANCE_CONFIG["connection_pool_size"]
        },

        "trailing_stop": TRAILING_STOP_CONFIG,
        "risk_management": FUTURES_RISK_MANAGEMENT,

        "symbols_meta": {symbol: get_symbol_config(symbol) for symbol in TRADING_SYMBOLS}
    }
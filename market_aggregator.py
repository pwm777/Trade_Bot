"""
market_aggregator.py
Обновлённый модуль MarketAggregator:
- DEMO (Binance Futures aggTrade) с фазовой агрегацией 1m(-10s) и 5m(-10s)
- Свечи сдвинуты на 10 секунд в будущее: начало T-L минут-10с, конец T-10с
- Интеграция с MarketDataUtils: CUSUM(1m) и 5m ML-features (bulk/incremental)
- Совместимость с существующими интерфейсами и backtest/live заглушками
"""

from __future__ import annotations
import logging
import threading
import asyncio
import json
import websockets
from typing import (Dict, Any, List, Callable,
                    Coroutine, TypeVar, cast, Optional, Awaitable)
from decimal import Decimal
from collections import deque
from datetime import datetime, UTC
from sqlalchemy import text
from iqts_standards import (
    MarketAggregatorInterface, NetConnState,
    MarketEventHandler, ExecutionMode, get_current_timestamp_ms,
    set_simulated_time, clear_simulated_time, ConnectionStatus,
    Candle1m,
)
logger = logging.getLogger(__name__)
from abc import ABC, abstractmethod
T = TypeVar("T")

class BaseMarketAggregator(ABC):
    """Базовый класс для всех агрегаторов с общей логикой"""
    BASE_INTERVAL_MS: int
    ONE_M_MS: int
    FIVE_M_MS: int

    def __init__(self, logger_instance: Optional[logging.Logger] = None):
        self.logger = logger_instance or logging.getLogger(__name__)
        if self.logger:
            self.logger.error(
                f"🔴 DemoMarketAggregatorPhased logger initialized: name={self.logger.name}, level={self.logger.level}")
        else:
            print("🔴 ERROR: logger is None!")
        self._is_running = False
        self._main_lock = threading.RLock()

        # Стандартная структура состояния соединения
        self._connection_state: NetConnState = {
            "status": "disconnected",
            "last_heartbeat": None,
            "reconnect_count": 0,
            "error_message": None,
            "connected_at": None,
            "last_error_at": None,
        }

        # Стандартная структура статистики
        self._stats: Dict[str, Any] = {
            "candles_processed": 0,
            "symbols_active": 0,
            "connection_state": "disconnected",
            "current_mode": self._get_mode(),
            "is_running": False,
            "active_symbols": [],
            "last_candle_ts": None,
        }

        # Таски для отслеживания
        self._running_tasks: Dict[str, asyncio.Task] = {}

    @abstractmethod
    def _get_mode(self) -> str:
        """Возвращает режим работы агрегатора"""
        pass

    @abstractmethod
    async def start_async(self, symbols: List[str], *, history_window: int = 50) -> None:
        """
        Асинхронный запуск агрегатора.
        Должен быть реализован всеми подклассами.
        """
        pass


    def _create_or_cancel_task(self, name: str, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """
        Регистрирует/пересоздаёт фоновую задачу с именем `name`.
        Принимает именно coroutine-объект (а не произвольный Awaitable).
        """
        # отменим существующую
        task = self._running_tasks.get(name)
        if task and not task.done():
            task.cancel()

        # создадим новую
        new_task: asyncio.Task[T] = asyncio.create_task(coro)
        self._running_tasks[name] = new_task
        return new_task

    def _convert_to_decimal(self, value: Any) -> Decimal:
        """Унифицированное преобразование в Decimal"""
        if value is None:
            return Decimal("0")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def _convert_to_float(self, value: Any) -> float:
        """Унифицированное преобразование в float"""
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value))

    def _cancel_all_tasks(self) -> None:
        """
        Отменяет все задачи, запущенные через _create_or_cancel_task.
        """
        for tname, task in list(self._running_tasks.items()):
            if task and not task.done():
                task.cancel()
        self._running_tasks.clear()

    def _candle_dict_to_candle1m(self, d: dict) -> Candle1m:
        """
        Преобразование dict → Candle1m с сохранением ВСЕХ полей.

        """
        base_candle = {
            "symbol": d["symbol"],
            "ts": d["ts"],
            "ts_close": d.get("ts_close", d["ts"] + 59_999),
            "open": d["open"],
            "high": d["high"],
            "low": d["low"],
            "close": d["close"],
            "volume": d.get("volume", 0.0),
            "count": d.get("count", 0),
            "quote": d.get("quote", 0.0),
            "finalized": d.get("finalized", 1),
            "checksum": d.get("checksum"),
            "created_ts": d.get("created_ts", int(datetime.now(UTC).timestamp() * 1000)),
        }

        #  Копируем ВСЕ остальные поля (индикаторы)
        for key, value in d.items():
            if key not in base_candle:
                base_candle[key] = value

        return cast(Candle1m, base_candle)

    def stop(self) -> None:
        """Базовая остановка агрегатора"""
        with self._main_lock:
            self._is_running = False
            self._stats["is_running"] = False
            self._stats["connection_state"] = "disconnected"
            self._connection_state["status"] = cast(ConnectionStatus, "disconnected")

            # Отмена всех задач
            for name, task in self._running_tasks.items():
                if not task.done():
                    task.cancel()
            self._running_tasks.clear()

    def shutdown(self) -> None:
        """Алиас для stop"""
        self.stop()

    def get_stats(self) -> Dict[str, Any]:
        """Получение копии статистики"""
        with self._main_lock:
            return self._stats.copy()

    def get_connection_state(self) -> NetConnState:
        """Получение копии состояния соединения"""
        with self._main_lock:
            return cast(NetConnState, self._connection_state.copy())

# ======================================================================
# ВСПОМОГАТЕЛЬНОЕ — бакетизация с фазой
# ======================================================================

def bucket_ts_with_phase(ts_ms: int, interval_ms: int, phase_ms: int) -> int:
    """
    Вычисляет начало ведра (bucket) с фазовым сдвигом.
    Пример: для 1m и phase=10s: бакеты ...:00:10, :01:10, ...
    """
    x = ts_ms - phase_ms
    return (x // interval_ms) * interval_ms + phase_ms


def finalize_cutoff(now_ms: int, interval_ms: int, safety_ms: int = 1000) -> int:
    """Граница финализации: всё, что строго старше (now - interval - safety) — «готово»."""
    return now_ms - interval_ms - safety_ms


# ======================================================================
# LIVE MARKET AGGREGATOR (заглушка как в исходнике)
# ======================================================================

class LiveMarketAggregator(BaseMarketAggregator):
    """
    Live-агрегатор под реальные WS данные.
    Реальную подписку/агрегацию подключаем по аналогии с DEMO.
    """

    def __init__(self,
                 db_dsn: str,
                 on_candle_ready: Callable[[str, Candle1m, List[Candle1m]], Awaitable[None]],
                 on_connection_state_change: Optional[Callable[[NetConnState], None]] = None,
                 interval_ms: int = 10_000,
                 logger_instance: Optional[logging.Logger] = None,
                 trading_logger: Optional[Any] = None):

        super().__init__(logger_instance)

        self.db_dsn = db_dsn
        self.on_candle_ready = on_candle_ready
        self.on_connection_state_change = on_connection_state_change
        self.interval_ms = interval_ms
        self.BASE_INTERVAL_MS = interval_ms
        self.trading_logger = trading_logger

        self._symbol_buffers: Dict[str, deque] = {}
        self._active_symbols: List[str] = []

        # Дополнительные поля статистики для live режима
        self._stats.update({
            "reconnects_count": 0,
            "avg_latency_ms": 0.0,
            "uptime_seconds": 0.0,
            "tasks_count": 0
        })

        # MarketDataUtils
        self._market_data_utils = None
        if trading_logger and hasattr(trading_logger, "market_engine"):
            try:
                from market_data_utils import MarketDataUtils
                self._market_data_utils = MarketDataUtils(trading_logger.market_engine, self.logger)
            except Exception as e:
                self.logger.warning(f"MarketDataUtils not available: {e}")

    def _get_mode(self) -> str:
        return "live"

    async def start_async(self, symbols: List[str], *, history_window: int = 10) -> None:
        self._active_symbols = symbols
        self._is_running = True
        self._stats["is_running"] = True
        self._stats["active_symbols"] = symbols
        self._stats["symbols_active"] = len(symbols)
        self._stats["connection_state"] = "connected"
        self._connection_state["status"] = cast(ConnectionStatus, "CONNECTED")
        self._connection_state["connected_at"] = get_current_timestamp_ms()
        for s in symbols:
            self._symbol_buffers[s] = deque(maxlen=130)

        # ----------  загрузка и прогрев индикаторов ----------
        if self._market_data_utils:
            try:
                # загружаем хвост последних N баров и выполняем warmup
                for s in symbols:
                    last_1m = await self._market_data_utils.read_candles_1m(s, last_n=max(120, history_window)) or []
                    last_5m = await self._market_data_utils.read_candles_5m(s, last_n=max(130, history_window)) or []
                    if last_1m:
                        await self._market_data_utils.warmup_1m_indicators_and_cusum(s, last_1m)
                    if last_5m and len(last_5m) >= 96:
                        await self._market_data_utils.compute_5m_features_bulk(s, last_5m)
                    self._symbol_buffers[s].extend(last_5m[-history_window:])
                self.logger.info(f"Indicators warm-up completed ({history_window} bars) for {symbols}")
            except Exception as e:
                self.logger.warning(f"Warm-up failed: {e}")
        # ---------------------------------------------------------------

        self.logger.info(f"LiveMarketAggregator started for: {symbols}")

    def stop(self) -> None:
        self._is_running = False
        self._stats["is_running"] = False
        self._connection_state["status"] = cast(ConnectionStatus, "disconnected")

    async def wait_for_completion(self) -> None:
        while self._is_running:
            await asyncio.sleep(1.0)

    def add_event_handler(self, handler: MarketEventHandler) -> None:
        pass

    def get_stats(self) -> Dict[str, Any]:
        return self._stats.copy()

    def get_connection_state(self) -> NetConnState:
        return cast(NetConnState, self._connection_state.copy())

    def fetch_recent(self, symbol: str, limit: int = 10) -> List[Candle1m]:
        buf = list(self._symbol_buffers.get(symbol, []))
        return buf[-limit:] if limit > 0 else buf

    def shutdown(self) -> None:
        self.stop()


# ======================================================================
# DEMO MARKET AGGREGATOR — Binance Futures aggTrade → 1m(+10s) & 5m(+10s)
# ======================================================================

class DemoMarketAggregatorPhased(BaseMarketAggregator):
    """
    DEMO агрегатор для Binance Futures kline свечей:
    - WebSocket подписка на kline 1m и 5m (combined streams)
    - Прямая обработка готовых свечей от Binance
    - Без фазового сдвига, без каскадной агрегации
    - При получении свечи: upsert + расчет индикаторов

    Рассчитываемые индикаторы:
    - 1m: CUSUM, volume analysis
    - 5m: SMA, EMA, RSI, MACD, VWAP(96), ML features
    """

    # Интервалы свечей (без фазового сдвига)
    ONE_M_MS = 60_000
    FIVE_M_MS = 300_000

    def __init__(self, config: Dict[str, Any],
                 on_candle_ready: Callable[[str, Candle1m, List[Candle1m]], Coroutine[Any, Any, None]],
                 on_connection_state_change: Optional[Callable[[NetConnState], None]],
                 logger_instance: logging.Logger,
                 trading_logger: Optional[Any]) -> None:

        super().__init__(logger_instance)

        self.config = config
        self.on_candle_ready = on_candle_ready
        self.on_connection_state_change = on_connection_state_change
        self.trading_logger = trading_logger

        self.websocket_config = config.get("websocket", {
            "futures_base_url": "wss://fstream.binance.com",
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_reconnect_attempts": 10,
        })

        self._active_symbols: List[str] = []
        self._symbol_buffers_1m: Dict[str, deque] = {}
        self._symbol_buffers_5m: Dict[str, deque] = {}

        self._last_historical_ts: Dict[str, int] = {}
        self._last_historical_ts_5m: Dict[str, int] = {}
        self._websocket = None
        self._ws_task = None
        self._should_reconnect = True

        # ✅ НОВОЕ: Очередь для последовательной обработки 5m свечей
        self._candle_5m_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._candle_5m_worker_task: Optional[asyncio.Task] = None

        self.logger.info("✅ 5m candle queue initialized (maxsize=5)")

        # Дополнительные счетчики для статистики
        self._stats.update({
            "ws_messages_received": 0,
            "klines_1m_received": 0,
            "klines_5m_received": 0,
            "klines_5m_dropped": 0,  # ✅ НОВОЕ: Счётчик пропущенных свечей
            "finalized_1m": 0,
            "finalized_5m": 0,
        })

        # MarketDataUtils
        self._market_data_utils = None
        if self.trading_logger and hasattr(self.trading_logger, 'market_engine'):
            try:
                from market_data_utils import MarketDataUtils
                self._market_data_utils = MarketDataUtils(self.trading_logger.market_engine, self.logger)
            except Exception as e:
                self.logger.warning(f"MarketDataUtils not available: {e}")

    def _get_mode(self) -> str:
        return "demo"

    # ---------------------- Персист и индикаторы ----------------------

    async def _persist_and_update_1m_async(self, symbol: str, cdict: dict) -> None:
        if not self._market_data_utils:
            return

        # update_1m_cusum сам рассчитывает индикаторы и сохраняет обогащенную свечу
        await self._market_data_utils.update_1m_cusum(symbol, cdict)

    # ---------------------- Старт/стоп ----------------------

    async def start_async(self, symbols: List[str], *, history_window: int = 50) -> None:
        self.logger.info(f"Starting DemoMarketAggregatorPhased for {symbols} (kline mode)")
        with self._main_lock:
            self._active_symbols = symbols
            self._is_running = True
            self._stats["is_running"] = True
            self._stats["active_symbols"] = symbols
            self._stats["symbols_active"] = len(symbols)
            self._stats["connection_state"] = "connecting"
            for s in symbols:
                self._symbol_buffers_1m[s] = deque(maxlen=400)
                self._symbol_buffers_5m[s] = deque(maxlen=400)

        # ✅ НОВОЕ: Запускаем worker для обработки очереди 5m свечей
        if not self._candle_5m_worker_task or self._candle_5m_worker_task.done():
            self._candle_5m_worker_task = asyncio.create_task(self._process_5m_queue_worker())
            self.logger.info("✅ 5m queue worker started")

        # Загрузка последней исторической 1m свечи
        if self._market_data_utils:
            for s in symbols:
                try:
                    last_1m = await self._market_data_utils.read_candles_1m(s, last_n=1)
                    if last_1m:
                        last_ts = last_1m[0]['ts']
                        self._last_historical_ts[s] = last_ts
                        self.logger.info(
                            f"Last historical 1m for {s}: {last_ts} "
                            f"({datetime.fromtimestamp(last_ts / 1000, UTC).isoformat()})"
                        )
                except Exception as e:
                    self.logger.warning(f"Could not load last historical 1m for {s}: {e}")

        # Загрузка последней исторической 5m свечи
        if self._market_data_utils:
            for s in symbols:
                try:
                    last_5m = await self._market_data_utils.read_candles_5m(s, last_n=1)
                    if last_5m:
                        last_ts_5m = last_5m[0]['ts']
                        self._last_historical_ts_5m[s] = last_ts_5m
                        self.logger.info(
                            f"Last historical 5m for {s}: {last_ts_5m} "
                            f"({datetime.fromtimestamp(last_ts_5m / 1000, UTC).isoformat()})"
                        )
                except Exception as e:
                    self.logger.warning(f"Could not load last historical 5m for {s}: {e}")

        # Запуск WS
        await self._connect_ws(symbols)

    def stop(self) -> None:
        try:
            # ✅ НОВОЕ: Останавливаем worker очереди (ПЕРЕД установкой _is_running = False)
            if self._candle_5m_worker_task and not self._candle_5m_worker_task.done():
                self._candle_5m_worker_task.cancel()
                self.logger.info("✅ 5m queue worker stop requested")

            with self._main_lock:
                self._is_running = False
                self._should_reconnect = False
                self._stats["is_running"] = False
                self._stats["connection_state"] = "disconnected"
                self._connection_state["status"] = cast(ConnectionStatus, "disconnected")

            # Останов WS
            if self._ws_task and not self._ws_task.done():
                self._ws_task.cancel()
            if self._websocket and not getattr(self._websocket, "closed", True):
                asyncio.create_task(self._websocket.close())

            # Отмена всех фоновых задач
            self._cancel_all_tasks()

        except Exception as e:
            self.logger.error(f"Stop error: {e}")

    def shutdown(self) -> None:
        self.stop()

    async def wait_for_completion(self) -> None:
        while self._is_running:
            await asyncio.sleep(0.25)

    # ---------------------- WebSocket ----------------------

    async def _connect_ws(self, symbols: List[str]) -> None:
        try:
            base = self.websocket_config["futures_base_url"].rstrip("/").replace("/ws", "")
            # Подписка на kline 1m и 5m для всех символов
            streams_1m = [f"{s.lower()}@kline_1m" for s in symbols]
            streams_5m = [f"{s.lower()}@kline_5m" for s in symbols]
            all_streams = streams_1m + streams_5m
            stream = "/".join(all_streams)
            ws_url = f"{base}/stream?streams={stream}"
            self.logger.info(f"Connecting WS: {ws_url}")

            self._websocket = await websockets.connect(
                ws_url,
                ping_interval=self.websocket_config.get("ping_interval", 20),
                ping_timeout=self.websocket_config.get("ping_timeout", 20),
            )
            with self._main_lock:
                self._connection_state["status"] = cast(ConnectionStatus, "connected")
                self._connection_state["connected_at"] = get_current_timestamp_ms()
                self._stats["connection_state"] = "connected"
            if self.on_connection_state_change:
                self.on_connection_state_change(self._connection_state.copy())
            self._ws_task = asyncio.create_task(self._ws_loop())
        except Exception as e:
            self.logger.error(f"WS connect error: {e}")
            await self._schedule_reconnect()

    async def _ws_loop(self) -> None:
        try:
            async for message in self._websocket:
                with self._main_lock:
                    self._stats["ws_messages_received"] += 1
                    self._connection_state["last_heartbeat"] = get_current_timestamp_ms()
                try:
                    obj = json.loads(message)
                    data = obj.get("data", obj)

                    # Проверяем, что это kline событие
                    if data.get("e") == "kline":
                        kline = data.get("k")
                        if not kline:
                            continue

                        interval = kline.get("i")
                        if interval == "1m":
                            self._on_kline_1m(kline)
                        elif interval == "5m":
                            self._on_kline_5m(kline)
                except Exception as e:
                    self.logger.error(f"WS message parse error: {e}")
        except websockets.exceptions.ConnectionClosed:
            self.logger.warning("WS closed")
            await self._schedule_reconnect()
        except Exception as e:
            self.logger.error(f"WS loop error: {e}")
            await self._schedule_reconnect()

    async def _schedule_reconnect(self) -> None:
        with self._main_lock:
            if not self._is_running or not self._should_reconnect:
                return
            rc = int(self._connection_state.get("reconnect_count", 0)) + 1
            self._connection_state["reconnect_count"] = rc
            self._connection_state["status"] = cast(ConnectionStatus, "RECONNECTING")
            self._stats["connection_state"] = "reconnecting"
        delay = min(5 * (2 ** (rc - 1)), 60)
        await asyncio.sleep(delay)
        if self._is_running and self._should_reconnect:
            await self._connect_ws(self._active_symbols)

    # ---------------------- Обработка kline ----------------------

    def _on_kline_1m(self, kline: Dict[str, Any]) -> None:
        """
        Обработка kline 1m от Binance.
        Финализируем только закрытые свечи (x=true).
        """
        symbol = kline.get("s", "").upper()
        is_closed = kline.get("x", False)

        with self._main_lock:
            if symbol not in self._active_symbols:
                return
            self._stats["klines_1m_received"] += 1

        # Обрабатываем только закрытые свечи
        if not is_closed:
            return

        # Преобразуем в Candle1m
        candle = self._kline_to_candle1m(kline, interval_ms=self.ONE_M_MS)
        if not candle:
            return

        # Проверка на перекрытие с исторической свечой
        if symbol in self._last_historical_ts:
            last_hist_ts = self._last_historical_ts[symbol]
            if candle["ts"] <= last_hist_ts:
                self.logger.debug(
                    f"Skipping overlapping 1m candle for {symbol}: "
                    f"ts={candle['ts']} <= last_historical={last_hist_ts}"
                )
                return

        asyncio.create_task(self._on_candle_ready_1m(symbol, candle))

    def _on_kline_5m(self, kline: Dict[str, Any]) -> None:
        """
        Обработка kline 5m от Binance.
        Финализируем только закрытые свечи (x=true).
        """
        symbol = kline.get("s", "").upper()
        is_closed = kline.get("x", False)

        with self._main_lock:
            if symbol not in self._active_symbols:
                return
            self._stats["klines_5m_received"] += 1

        # Обрабатываем только закрытые свечи
        if not is_closed:
            return

        # Преобразуем в Candle1m
        candle = self._kline_to_candle1m(kline, interval_ms=self.FIVE_M_MS)
        if not candle:
            return

        # ✅ ИСПРАВЛЕНО: Пропускаем только СТРОГО старые свечи
        if symbol in self._last_historical_ts_5m:
            last_hist_ts = self._last_historical_ts_5m[symbol]
            if candle["ts"] < last_hist_ts:  # ← Было <=
                self.logger.debug(
                    f"Skipping overlapping 5m candle for {symbol}: "
                    f"ts={candle['ts']} < last_historical={last_hist_ts}"
                )
                return

            # ✅ НОВОЕ: Обновляем маркер после обработки новой свечи
            if candle["ts"] >= last_hist_ts:
                self._last_historical_ts_5m[symbol] = candle["ts"]
                self.logger.debug(f"Updated last_historical_ts_5m for {symbol}: {candle['ts']}")

        # ✅ НОВОЕ: Добавляем свечу в очередь вместо немедленной обработки
        try:
            self._candle_5m_queue.put_nowait((symbol, candle))
            self.logger.debug(
                f"📥 5m candle added to queue: {symbol} @ {candle['ts']} "
                f"(queue size: {self._candle_5m_queue.qsize()})"
            )
        except asyncio.QueueFull:
            # Очередь переполнена - пропускаем свечу
            self.logger.warning(
                f"⚠️ 5m queue is FULL ({self._candle_5m_queue.maxsize}), "
                f"dropping candle for {symbol} @ {candle['ts']}"
            )
            with self._main_lock:
                self._stats["klines_5m_dropped"] += 1

    def _kline_to_candle1m(self, kline: Dict[str, Any], interval_ms: int) -> Optional[Candle1m]:
        """
        Преобразование формата kline Binance в Candle1m

        Формат kline:
        {
            "t": 123400000,  # start time
            "T": 123460000,  # close time
            "s": "BTCUSDT",
            "i": "1m",
            "o": "0.0010",   # open
            "c": "0.0020",   # close
            "h": "0.0025",   # high
            "l": "0.0015",   # low
            "v": "1000",     # volume
            "n": 100,        # number of trades
            "x": false,      # is closed
            "q": "1.0000",   # quote asset volume
        }
        """
        try:
            symbol = kline.get("s", "").upper()
            start_time = int(kline.get("t", 0))
            close_time = int(kline.get("T", start_time + interval_ms - 1))

            candle: Candle1m = {
                "symbol": symbol,
                "ts": start_time,
                "ts_close": close_time,
                "open": Decimal(str(kline.get("o", "0"))),
                "high": Decimal(str(kline.get("h", "0"))),
                "low": Decimal(str(kline.get("l", "0"))),
                "close": Decimal(str(kline.get("c", "0"))),
                "volume": Decimal(str(kline.get("v", "0"))),
                "count": int(kline.get("n", 0)),
                "quote": Decimal(str(kline.get("q", "0"))),
                "finalized": bool(kline.get("x", False)),
                "checksum": f"binance_kline_{start_time}",
                "created_ts": get_current_timestamp_ms(),
            }
            return candle
        except Exception as e:
            self.logger.error(f"kline conversion error: {e}")
            return None

    def _candle_to_dict(self, candle: Candle1m, *, interval_ms: int = 0) -> Dict[str, Any]:
        """Candle1m → dict для персиста в БД"""
        return {
            "symbol": candle["symbol"],
            "ts": candle["ts"],
            "ts_close": candle["ts_close"],
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle["volume"]),
            "count": candle["count"],
            "quote": float(candle["quote"]) if candle.get("quote") else 0.0,
            "finalized": 1 if candle["finalized"] else 0,
            "checksum": candle.get("checksum"),
            "created_ts": candle["created_ts"],
        }

    # ---------------------- Event callbacks ----------------------

    async def _on_candle_ready_1m(self, symbol: str, candle: Candle1m) -> None:
        """
        Обработка готовой 1m свечи:
        1. Увеличение счетчика статистики
        2. Преобразование в dict
        3. ✅ ЖДЕМ расчет индикаторов (CUSUM)
        4. Добавление в буфер ПОСЛЕ расчета индикаторов
        5. Callback пользователю с полной свечой
        """
        self.logger.info(f"📊 Calling on_candle_ready for {symbol} 1m @ {candle['ts']}")
        with self._main_lock:
            self._stats["finalized_1m"] += 1

        cdict = self._candle_to_dict(candle, interval_ms=self.ONE_M_MS)

        # ✅ ЖДЕМ расчет индикаторов CUSUM
        if self._market_data_utils:
            try:
                self.logger.info(f"🔄 Starting 1m CUSUM calculation for {symbol}@{cdict['ts']}")

                # ✅ update_1m_cusum САМ сохраняет обогащенную свечу в БД
                result = await self._market_data_utils.update_1m_cusum(symbol, cdict)

                if result and result.get("ok"):
                    self.logger.info(f"✅ 1m CUSUM ready for {symbol}@{cdict['ts']}: z={result.get('z', 0):.2f}")

                    # ✅ Небольшая задержка для flush БД (WAL mode)
                    await asyncio.sleep(0.005)

                    # ✅ Получаем обогащенную свечу из БД
                    enriched_candles = await self._market_data_utils.read_candles_1m(
                        symbol,
                        start_ts=cdict['ts'],
                        end_ts=cdict['ts']
                    )

                    if enriched_candles and len(enriched_candles) > 0:
                        enriched = enriched_candles[0]

                        # ✅ ПРОВЕРЯЕМ что индикаторы ДЕЙСТВИТЕЛЬНО есть
                        has_indicators = (
                                enriched.get('ema3') is not None and
                                enriched.get('ema7') is not None and
                                enriched.get('cusum_zscore') is not None
                        )

                        if has_indicators:
                            cdict = enriched  # ✅ Используем обогащенную свечу
                            self.logger.info(f"✅ Enriched 1m candle loaded for {symbol}@{cdict['ts']}")
                        else:
                            self.logger.error(
                                f"❌ 1m candle loaded but missing indicators: "
                                f"ema3={enriched.get('ema3')}, ema7={enriched.get('ema7')}, cusum_zscore={enriched.get('cusum_zscore')}"
                            )
                            # ❌ НЕ продолжаем - пропускаем callback
                            return
                    else:
                        self.logger.error(f"❌ Failed to load enriched 1m candle from DB for {symbol}@{cdict['ts']}")
                        # ❌ НЕ продолжаем - пропускаем callback
                        return
                else:
                    self.logger.error(f"❌ 1m CUSUM calculation returned error for {symbol}@{cdict['ts']}")
                    # ❌ НЕ продолжаем
                    return

            except Exception as e:
                self.logger.error(f"❌ 1m CUSUM calculation failed for {symbol}@{cdict['ts']}: {e}", exc_info=True)
                # ❌ НЕ продолжаем - не отправляем свечу без индикаторов
                return
        else:
            self.logger.error(f"❌ MarketDataUtils not available for {symbol}@{cdict['ts']}")
            # ❌ НЕ продолжаем
            return

        # ✅ Добавляем в буфер ТОЛЬКО ОБОГАЩЕННУЮ свечу с проверенными индикаторами
        with self._main_lock:
            self._symbol_buffers_1m[symbol].append(cdict)

        # ✅ Колбэк с ПОЛНОЙ свечой
        with self._main_lock:
            recent = list(self._symbol_buffers_1m[symbol])[-20:]
        recent_c10 = [self._candle_dict_to_candle1m(d) for d in recent]

        try:
            result = self.on_candle_ready(symbol, self._candle_dict_to_candle1m(cdict), recent_c10)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self.logger.error(f"on_candle_ready error (1m): {e}", exc_info=True)

    async def _on_candle_ready_5m(self, symbol: str, candle: Candle1m) -> None:
        """
        Обработка готовой 5m свечи:
        1. Увеличение счетчика статистики
        2. Преобразование в dict
        3. ✅ ЖДЕМ расчет индикаторов (ML features)
        4. Добавление в буфер ПОСЛЕ расчета индикаторов
        5. Callback пользователю с полной свечой
        """
        with self._main_lock:
            self._stats["finalized_5m"] += 1

        cdict = self._candle_to_dict(candle, interval_ms=self.FIVE_M_MS)

        # ✅ ПОЛУЧАЕМ ОБОГАЩЕННУЮ СВЕЧУ НАПРЯМУЮ И СОХРАНЯЕМ В БД
        if self._market_data_utils:
            try:
                ts_5m = cdict['ts']

                self.logger.info(f"🔄 Starting 5m indicators calculation for {symbol}@{ts_5m}")
                cdict = await self._market_data_utils.compute_5m_features_incremental(symbol, cdict)
                self.logger.info(
                    f"✅ Enriched 5m candle ready and saved to DB for {symbol}@{cdict['ts']} "
                    f"(cmo_14={cdict.get('cmo_14')}, adx_14={cdict.get('adx_14')})"
                )

            except Exception as e:
                self.logger.error(f"❌ 5m indicators calculation failed for {symbol}@{cdict['ts']}: {e}", exc_info=True)
                return  # ❌ Пропускаем callback - ошибка расчета
        else:
            self.logger.error(f"❌ MarketDataUtils not available for {symbol}@{cdict['ts']}")
            return  # ❌ Пропускаем callback - нет утилиты

        # ✅ Добавляем ОБОГАЩЕННУЮ свечу (УЖЕ СОХРАНЕННУЮ В БД) в буфер
        with self._main_lock:
            self._symbol_buffers_5m[symbol].append(cdict)

        # ✅ Колбэк с ПОЛНОЙ ОБОГАЩЕННОЙ свечой (ПОСЛЕ записи в БД)
        with self._main_lock:
            recent = list(self._symbol_buffers_5m[symbol])[-50:]
        recent_c10 = [self._candle_dict_to_candle1m(d) for d in recent]

        self.logger.info(f"🎯 Calling on_candle_ready AFTER DB write for {symbol} @ {cdict['ts']}")

        try:
            result = self.on_candle_ready(symbol, self._candle_dict_to_candle1m(cdict), recent_c10)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self.logger.error(f"on_candle_ready error (5m): {e}", exc_info=True)

    async def _process_5m_queue_worker(self) -> None:
        """
        Worker для последовательной обработки 5m свечей из очереди.
        
        ✅ ГАРАНТИРУЕТ:
        - Свечи обрабатываются строго по одной (FIFO)
        - Следующая свеча начинает обработку ТОЛЬКО после завершения предыдущей
        - Исключает параллельные запросы к БД
        - Предотвращает connection pool exhaustion
        """
        self.logger.info("🔄 5m queue worker started")
        
        while self._is_running:
            try:
                # ✅ Ждём свечу из очереди с таймаутом для проверки _is_running
                try:
                    symbol, candle = await asyncio.wait_for(
                        self._candle_5m_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # Таймаут - проверяем флаг и продолжаем ожидание
                    continue
                
                self.logger.info(
                    f"📤 Processing 5m candle from queue: {symbol} @ {candle['ts']} "
                    f"(remaining in queue: {self._candle_5m_queue.qsize()})"
                )
                
                # ✅ Измеряем время обработки
                process_start = asyncio.get_event_loop().time()
                
                try:
                    # Обрабатываем свечу
                    await self._on_candle_ready_5m(symbol, candle)
                    
                    process_time = asyncio.get_event_loop().time() - process_start
                    
                    self.logger.info(
                        f"✅ 5m candle processed in {process_time:.2f}s: "
                        f"{symbol} @ {candle['ts']}"
                    )
                    
                except Exception as e:
                    self.logger.error(
                        f"❌ Error processing 5m candle from queue: "
                        f"{symbol} @ {candle['ts']}: {e}",
                        exc_info=True
                    )
                finally:
                    # ✅ Сообщаем очереди что задача завершена
                    self._candle_5m_queue.task_done()
                    
            except asyncio.CancelledError:
                self.logger.info("🛑 5m queue worker cancelled")
                break
            except Exception as e:
                self.logger.error(f"❌ Unexpected error in 5m queue worker: {e}", exc_info=True)
                await asyncio.sleep(1)  # Небольшая задержка перед повтором
        
        self.logger.info("🛑 5m queue worker stopped")

    # ---------------------- Интерфейс ----------------------

    def add_event_handler(self, handler: MarketEventHandler) -> None:
        pass

    def fetch_recent(self, symbol: str, limit: int = 10) -> List[Candle1m]:
        # Вернём из 5m-буфера для общности
        with self._main_lock:
            buf_dicts = list(self._symbol_buffers_5m.get(symbol, []))
        dicts = buf_dicts[-limit:] if limit > 0 else buf_dicts
        return [self._candle_dict_to_candle1m(d) for d in dicts]

    def get_buffer_history(self, symbol: str, count: int = 10, *, exclude_current: bool = False) -> List[Candle1m]:
        with self._main_lock:
            buf_dicts = list(self._symbol_buffers_5m.get(symbol, []))
        if exclude_current and buf_dicts:
            buf_dicts = buf_dicts[:-1]
        dicts = buf_dicts[-count:] if count > 0 else buf_dicts
        return [self._candle_dict_to_candle1m(d) for d in dicts]

    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики"""
        with self._main_lock:
            return self._stats.copy()
# ======================================================================
# BACKTEST MARKET AGGREGATOR
# ======================================================================

class BacktestMarketAggregatorFixed(BaseMarketAggregator):
    """
    Бэктест-агрегатор для симуляции рынка из данных SQLite.
    Читает из candles_1m/candles_5m
    """

    def __init__(self,
                 trading_logger: Optional[Any],
                 on_candle_ready: Callable[[str, Candle1m, List[Candle1m]], Awaitable[None]],
                 symbols: List[str],
                 virtual_clock_start_ms: int,
                 virtual_clock_end_ms: int,
                 interval_ms: int = 60_000,
                 logger: Optional[logging.Logger] = None):

        super().__init__(logger)

        self.trading_logger = trading_logger
        self.on_candle_ready = on_candle_ready
        self.BASE_INTERVAL_MS = interval_ms
        self.symbols = symbols
        self.virtual_clock_start_ms = virtual_clock_start_ms
        self.virtual_clock_end_ms = virtual_clock_end_ms
        self.interval_ms = interval_ms

        self._symbol_buffers: Dict[str, deque] = {s: deque(maxlen=200) for s in symbols}

        # ✅ NEW: Synchronization Events for 5m candle processing
        self._processing_5m_candle: Dict[str, asyncio.Event] = {
            s: asyncio.Event() for s in symbols
        }
        for event in self._processing_5m_candle.values():
            event.set()  # Initially ready

        # SQLAlchemy engine
        from sqlalchemy import create_engine
        db_dsn = "sqlite:///data/market_data.sqlite"
        if trading_logger and hasattr(trading_logger, 'config') and trading_logger.config:
            db_dsn = trading_logger.config.get("market_db_dsn", db_dsn)
        self._engine = create_engine(db_dsn)

        # Дополнительные поля для backtest
        self._stats.update({
            "backtest_start_ms": virtual_clock_start_ms,
            "backtest_end_ms": virtual_clock_end_ms,
            "backtest_completed": False,
            "backtest_progress": 0.0,
        })

    def _get_mode(self) -> str:
        return "backtest"

    async def _replay_loop(self, symbols: List[str], from_ts: int, to_ts: int, speed: float = 1.0) -> None:
        """Воспроизведение исторических данных"""
        self.logger.info(f"Starting replay from {from_ts} to {to_ts} for {symbols}")

        # ✅ Load BOTH 1m and 5m candles with UNION query
        sql = text("""
            SELECT '1m' as timeframe, symbol, ts, ts_close, open, high, low, close, volume, count, quote, finalized, checksum, created_ts
            FROM candles_1m
            WHERE symbol = :symbol AND ts >= :from_ts AND ts <= :to_ts
            
            UNION ALL
            
            SELECT '5m' as timeframe, symbol, ts, ts_close, open, high, low, close, volume, count, quote, finalized, checksum, created_ts
            FROM candles_5m
            WHERE symbol = :symbol AND ts >= :from_ts AND ts <= :to_ts
            
            ORDER BY ts ASC, timeframe ASC
        """)

        try:
            for s in symbols:
                with self._engine.connect() as conn:
                    rows = list(
                        conn.execute(sql, {"symbol": s, "from_ts": int(from_ts), "to_ts": int(to_ts)}).mappings())

                for r in rows:
                    if not self._is_running:
                        break

                    set_simulated_time(int(r["ts"]))

                    # ✅ Extract timeframe metadata
                    candle_dict = dict(r)
                    timeframe = candle_dict.pop('timeframe', '1m')
                    
                    # Используем базовый метод преобразования
                    candle = self._candle_dict_to_candle1m(candle_dict)
                    candle['_timeframe'] = timeframe  # ✅ Add metadata for downstream processing

                    self._symbol_buffers[s].append(candle)
                    recent = list(self._symbol_buffers[s])[-50:]

                    # ✅ NEW: For 5m candles, wait for previous processing to complete
                    if timeframe == '5m':
                        self.logger.debug(f"⏳ Waiting for previous 5m candle processing to complete for {s}")
                        await self._processing_5m_candle[s].wait()
                        self._processing_5m_candle[s].clear()  # Block next candle
                        self.logger.debug(f"✅ Ready to process new 5m candle for {s}")

                    try:
                        # ✅ ИСПРАВЛЕНИЕ: Правильная обработка async/sync callback
                        result = self.on_candle_ready(s, candle, recent)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        self.logger.error(f"on_candle_ready error: {e}", exc_info=True)
                    finally:
                        # ✅ NEW: Unblock next 5m candle after processing (success or error)
                        if timeframe == '5m':
                            self._processing_5m_candle[s].set()
                            self.logger.debug(f"🔓 Released lock for next 5m candle for {s}")

                    with self._main_lock:
                        self._stats["candles_processed"] += 1
                        self._stats["last_candle_ts"] = int(r["ts"])

                    if speed > 0:
                        await asyncio.sleep(0.001 / speed)

            with self._main_lock:
                self._stats["backtest_completed"] = True
        except Exception as e:
            self.logger.error(f"Replay error: {e}")
        finally:
            try:
                clear_simulated_time()
            except Exception:
                pass

    async def start_async(self, symbols: List[str], *, history_window: int = 50) -> None:
        """
        Запускает бэктест-агрегатор.
        В режиме BACKTEST сразу начинается воспроизведение истории.
        """
        self.logger.info(f"Starting BacktestMarketAggregatorFixed for {symbols}")

        with self._main_lock:
            self._active_symbols = symbols
            self._is_running = True
            self._stats["is_running"] = True
            self._stats["active_symbols"] = symbols
            self._stats["symbols_active"] = len(symbols)
            self._stats["connection_state"] = "connected"

            # Инициализация буферов
            for s in symbols:
                if not hasattr(self, '_symbol_buffers'):
                    self._symbol_buffers = {}
                self._symbol_buffers[s] = deque(maxlen=400)

        # Определение скорости из config или по умолчанию
        speed = getattr(self, 'speed', 1.0)  # Убедитесь, что speed установлен при создании

        # Запуск воспроизведения
        self._running_tasks['replay'] = asyncio.create_task(
            self._replay_loop(symbols, self.virtual_clock_start_ms, self.virtual_clock_end_ms, speed=speed)
        )

        self.logger.info(f"Backtest started from {self.virtual_clock_start_ms} to {self.virtual_clock_end_ms}")

    async def wait_for_completion(self) -> None:
        """Ожидание завершения бэктеста"""
        while self._is_running and not self._stats.get("backtest_completed", False):
            await asyncio.sleep(0.05)

    def fetch_recent(self, symbol: str, limit: int = 10) -> List[Candle1m]:
        """Получение последних свечей для символа"""
        with self._main_lock:
            data = list(self._symbol_buffers.get(symbol, []))
            return data[-limit:] if limit > 0 else data

    def get_buffer_history(self, symbol: str, count: int = 10, *, exclude_current: bool = False) -> List[Candle1m]:
        """Получение истории из буфера"""
        with self._main_lock:
            buf = list(self._symbol_buffers.get(symbol, []))
        if exclude_current and buf:
            buf = buf[:-1]
        return buf[-count:] if count > 0 else buf

    def add_event_handler(self, handler: MarketEventHandler) -> None:
        """Добавление обработчика событий (заглушка)"""
        pass



# ======================================================================
# FACTORY
# ======================================================================

class MarketAggregatorFactory:

    @staticmethod
    def validate_config(execution_mode: str, config: Dict[str, Any]) -> List[str]:
        errors = []
        try:
            required_fields = ["symbols", "market_db_dsn"]
            for f in required_fields:
                if f not in config:
                    errors.append(f"Missing required field: {f}")
            if "symbols" in config:
                s = config["symbols"]
                if not isinstance(s, list) or not s or not all(isinstance(x, str) for x in s):
                    errors.append("symbols must be a non-empty list[str]")
        except Exception as e:
            errors.append(f"Validation error: {e}")
        return errors

    @staticmethod
    def _create_live_aggregator(
        config: Dict[str, Any],
        on_candle_ready: Callable,
        on_connection_state_change: Optional[Callable],
        event_handlers: Optional[List[MarketEventHandler]],
        logger_instance: logging.Logger,
        trading_logger: Optional[Any]
    ) -> MarketAggregatorInterface:
        db_dsn = config.get("market_db_dsn", "sqlite:///data/market_data.sqlite")
        interval_ms = config.get("interval_ms", 60_000)  # не используется в DEMO/фазах
        aggr = LiveMarketAggregator(
            db_dsn=db_dsn,
            on_candle_ready=on_candle_ready,
            on_connection_state_change=on_connection_state_change,
            interval_ms=interval_ms,
            logger_instance=logger_instance,
            trading_logger=trading_logger
        )
        if event_handlers:
            for h in event_handlers:
                aggr.add_event_handler(h)
        logger_instance.info("Created LiveMarketAggregator")
        return cast(MarketAggregatorInterface, aggr)

    @staticmethod
    def _create_demo_aggregator(
        config: Dict[str, Any],
        on_candle_ready: Callable,
        on_connection_state_change: Optional[Callable],
        event_handlers: Optional[List[MarketEventHandler]],
        logger_instance: logging.Logger,
        trading_logger: Optional[Any]
    ) -> MarketAggregatorInterface:
        aggr = DemoMarketAggregatorPhased(
            config=config,
            on_candle_ready=on_candle_ready,
            on_connection_state_change=on_connection_state_change,
            logger_instance=logger_instance,
            trading_logger=trading_logger
        )
        if event_handlers:
            for h in event_handlers:
                aggr.add_event_handler(h)
        logger_instance.info("Created DemoMarketAggregatorPhased (1m+5m with 10s phase shift)")
        return cast(MarketAggregatorInterface, aggr)

    @staticmethod
    def _create_backtest_aggregator(
        config: Dict[str, Any],
        on_candle_ready: Callable,
        on_connection_state_change: Optional[Callable],
        event_handlers: Optional[List[MarketEventHandler]],
        logger_instance: logging.Logger,
        trading_logger: Optional[Any]
    ) -> MarketAggregatorInterface:
        symbols = config.get("symbols", [])
        backtest_config = config.get("backtest", {})
        aggr = BacktestMarketAggregatorFixed(
            trading_logger=trading_logger,
            on_candle_ready=on_candle_ready,
            symbols=symbols,
            virtual_clock_start_ms=backtest_config.get("start_time_ms", 0),
            virtual_clock_end_ms=backtest_config.get("end_time_ms", 0),
            interval_ms=60_000,
            logger=logger_instance
        )
        if event_handlers:
            for h in event_handlers:
                aggr.add_event_handler(h)
        logger_instance.info("Created BacktestMarketAggregatorFixed")
        return cast(MarketAggregatorInterface, aggr)

    @staticmethod
    def create_market_aggregator(
            execution_mode: ExecutionMode,  # Literal["LIVE","DEMO","BACKTEST"]
            config: Dict[str, Any],
            on_candle_ready: Callable[[str, Candle1m, List[Candle1m]], Awaitable[None]],
            on_connection_state_change: Optional[Callable[[NetConnState], None]] = None,
            event_handlers: Optional[List[MarketEventHandler]] = None,
            logger_instance: Optional[logging.Logger] = None,
            trading_logger: Optional[Any] = None
    ) -> MarketAggregatorInterface:
        factory_logger = logger_instance or logger
        factory_logger.info(f"Creating MarketAggregator for mode: {execution_mode}")

        if execution_mode == "LIVE":
            return MarketAggregatorFactory._create_live_aggregator(
                config, on_candle_ready, on_connection_state_change,
                event_handlers, factory_logger, trading_logger
            )
        elif execution_mode == "DEMO":
            return MarketAggregatorFactory._create_demo_aggregator(
                config, on_candle_ready, on_connection_state_change,
                event_handlers, factory_logger, trading_logger
            )
        elif execution_mode == "BACKTEST":
            return MarketAggregatorFactory._create_backtest_aggregator(
                config, on_candle_ready, on_connection_state_change,
                event_handlers, factory_logger, trading_logger
            )
""" trading_logger.py - модуль работы с БД orders, positions, trades
Логирование событий: сигналы, сделки, ошибки.
Работу с базой данных: операции CRUD для позиций, ордеров и истории торгов.
Асинхронную обработку: буферизацию и фоновую запись логов для повышения производительности.
Статистику и мониторинг: сбор метрик и аналитики по торговым операциям.
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional, Callable
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy import text
from decimal import Decimal
import asyncio
import time
import logging

# ✅ ИСПРАВЛЕНО: Импорты из iqts_standards.py
from iqts_standards import (
    # Типы для БД
OrderReq, OrderUpd,Candle1m,
    # Утилиты
    get_current_timestamp_ms
)

logger = logging.getLogger(__name__)


# === ДОПОЛНИТЕЛЬНЫЕ ТИПЫ ДЛЯ БД ===

class SymbolInfo(Dict[str, Any]):
    """Информация о символе (расширяемый dict)"""
    pass


class TradeRecord(Dict[str, Any]):
    """Запись о торговле (расширяемый dict)"""
    pass


class PositionRecord(Dict[str, Any]):
    """Запись позиции в БД (расширяемый dict)"""
    pass


class OrderRecord(Dict[str, Any]):
    """Запись ордера в БД (расширяемый dict)"""
    pass

# === CALLBACK TYPES ===

AlertCallback = Callable[[str, Dict[str, Any]], None]


class TradingLogger:
    """
    Единый менеджер данных, объединяющий:
    1. Логирование событий (signals, trades, errors)
    2. CRUD операции с БД (positions, orders, symbols)
    3. Асинхронные очереди для производительности
    4. Автоматическое логирование при изменении данных
    """

    def __init__(self,
                 market_db_path: str = "trading_databases.sqlite",
                 trades_db_path: str = "position_trades.sqlite",
                 on_alert: Optional[AlertCallback] = None,
                 pool_size: int = 4,
                 enable_async: bool = True,
                 logger_instance: Optional[logging.Logger] = None):

        self.market_db_path = market_db_path
        self.trades_db_path = trades_db_path

        # Database engines
        self.market_engine = create_engine(f"sqlite:///{market_db_path}")
        self.trades_engine = create_engine(f"sqlite:///{trades_db_path}")

        # Logging config
        self.on_alert = on_alert
        self.pool_size = pool_size
        self.enable_async = enable_async
        self.logger = logger_instance or logger

        # Internal state
        self._dedup_cache: Dict[str, int] = {}
        self._stats = {
            "signals_logged": 0,
            "trades_logged": 0,
            "errors_logged": 0,
            "positions_created": 0,
            "orders_created": 0,
            "duplicates_rejected": 0
        }

        # Async components (lazy initialization)
        self._async_queues: Optional[Dict[str, Any]] = None
        self._async_workers: Optional[List[Any]] = None
        self._is_async_started = False
        self._last_candle: Dict[str, Any] = {}

        try:
            self.ensure_trading_schema()
        except Exception as e:
            self.logger.error(f"TradingLogger schema init failed: {e}")

    async def on_candle_ready(
        self,
        symbol: str,
        candle: Candle1m,
        recent: List[Candle1m]
    ) -> None:
        """Обработчик новой свечи"""
        try:
            self._last_candle[symbol] = dict(candle)
            self.logger.debug(
                f"New candle for {symbol}: {candle['ts']} "
                f"O:{float(candle['open'])} H:{float(candle['high'])} "
                f"L:{float(candle['low'])} C:{float(candle['close'])}"
            )
        except Exception as e:
            self.logger.error(f"Error processing candle for {symbol}: {e}")

    async def on_market_event(self, event: Dict[str, Any]) -> None:
        """Обработчик рыночных событий"""
        try:
            event_type = event.get("event_type")
            if not event_type:
                return

            self.logger.debug(f"Market event: {event_type}")

            # Обработка специфичных типов событий
            if event_type == "trade":
                symbol = event.get("symbol")
                price = event.get("price")
                qty = event.get("qty")
                self.logger.info(f"Trade: {symbol} {price} x {qty}")

        except Exception as e:
            self.logger.error(f"Error processing market event: {e}")

    def on_connection_state_change(self, state: Dict[str, Any]) -> None:
        """Обработчик изменения состояния соединения"""
        try:
            status = state.get("status", "unknown")
            self.logger.info(f"Connection state changed to: {status}")

            if status == "connected":
                self.logger.info("Market data connection established")
            elif status == "disconnected":
                self.logger.warning("Market data connection lost")
            elif status == "error":
                error_msg = state.get("error_message", "unknown error")
                self.logger.error(f"Market data connection error: {error_msg}")

        except Exception as e:
            self.logger.error(f"Error processing connection state change: {e}")

    def get_last_candle(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Получение последней свечи для символа"""
        return self._last_candle.get(symbol)

    def _normalize_db_value(self, v):
        """Приводит значения к типам, которые безусловно принимает sqlite3."""
        if v is None:
            return None
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    def _normalize_params(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Нормализация параметров для SQLite."""
        return {k: self._normalize_db_value(v) for k, v in data.items()}

    # === Logging Methods ===

    def record_signal(self, symbol: str, signal_type: str, **kwargs) -> None:
        """Синхронная запись сигнала."""
        signal_data = {
            "symbol": symbol,
            "signal_type": signal_type,
            "timestamp_ms": get_current_timestamp_ms(),
            **kwargs
        }
        self._write_log_entry("signal", signal_data, kwargs.get("dedup_key"))

    def record_trade(self, trade_data: Dict[str, Any], **kwargs) -> None:
        """Синхронная запись торгового события."""
        self._write_log_entry("trade", trade_data, kwargs.get("dedup_key"))

    def record_error(self, error_data: Dict[str, Any], **kwargs) -> None:
        """Синхронная запись ошибки."""
        self._write_log_entry("error", error_data, kwargs.get("dedup_key"))

        if self.on_alert:
            try:
                self.on_alert("error", error_data)
            except Exception as e:
                self.logger.error(f"Alert callback failed: {e}")

    async def record_signal_async(self, symbol: str, signal_type: str, **kwargs) -> None:
        """Асинхронная запись сигнала."""
        if not self.enable_async:
            self.record_signal(symbol, signal_type, **kwargs)
            return

        await self._ensure_async_started()
        signal_data = {
            "symbol": symbol,
            "signal_type": signal_type,
            "timestamp_ms": get_current_timestamp_ms(),
            **kwargs
        }
        await self._enqueue_async("signal", signal_data)

    async def record_trade_async(self, trade_data: Dict[str, Any], **kwargs) -> None:
        """Асинхронная запись торгового события."""
        if not self.enable_async:
            self.record_trade(trade_data, **kwargs)
            return

        await self._ensure_async_started()
        await self._enqueue_async("trade", trade_data)

    async def record_error_async(self, error_data: Dict[str, Any], **kwargs) -> None:
        """Асинхронная запись ошибки."""
        if not self.enable_async:
            self.record_error(error_data, **kwargs)
            return

        await self._ensure_async_started()
        await self._enqueue_async("error", error_data)

    async def flush(self) -> None:
        """Принудительный сброс асинхронных буферов."""
        if self._async_queues:
            for queue in self._async_queues.values():
                await queue.put({"_flush": True})

    # === Compatibility methods ===

    def log_signal_generated(self, symbol: str, intent: str, confidence: float,
                             reason: str, correlation_id: Optional[str] = None, **kwargs) -> None:
        """Логирование торгового сигнала (для совместимости)."""
        self.record_signal(
            symbol=symbol,
            signal_type="SIGNAL_GENERATED",
            intent=intent,
            confidence=confidence,
            reason=reason,
            correlation_id=correlation_id,
            **kwargs)

    def log_position_opened(self, symbol: str, side: str, qty: float,
                            price: Optional[float] = None, correlation_id: Optional[str] = None, **kwargs) -> None:
        """Логирование открытия позиции."""
        self.record_signal(
            symbol=symbol,
            signal_type="POSITION_OPENED",
            side=side,
            qty=qty,
            price=price,
            correlation_id=correlation_id,
            **kwargs)

    def log_position_closed(self, symbol: str, pnl: float, exit_price: Optional[float] = None,
                            correlation_id: Optional[str] = None, **kwargs) -> None:
        """Логирование закрытия позиции."""
        self.record_signal(
            symbol=symbol,
            signal_type="POSITION_CLOSED",
            pnl=pnl,
            exit_price=exit_price,
            correlation_id=correlation_id,
            **kwargs)

    def log_order_created(self, client_order_id: str, symbol: str, side: str, type: str,
                          qty: float, price: Optional[float] = None, correlation_id: Optional[str] = None,
                          **kwargs) -> None:
        """Логирование создания ордера."""
        self.record_signal(
            symbol=symbol,
            signal_type="ORDER_CREATED",
            client_order_id=client_order_id,
            side=side,
            order_type=type,
            qty=qty,
            price=price,
            correlation_id=correlation_id,
            **kwargs)

    # === Universal logging methods ===

    def log(self, entry_type: str, data: Dict[str, Any], **kwargs) -> None:
        """Универсальный метод логирования."""
        try:
            loop = asyncio.get_running_loop()
            if loop and self.enable_async:
                asyncio.create_task(self._log_async(entry_type, data, **kwargs))
            else:
                self._log_sync(entry_type, data, **kwargs)
        except RuntimeError:
            self._log_sync(entry_type, data, **kwargs)

    async def log_async(self, entry_type: str, data: Dict[str, Any], **kwargs) -> None:
        """Явно асинхронное логирование."""
        await self._log_async(entry_type, data, **kwargs)

    # === Database CRUD Operations ===

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """Получить информацию о символе."""
        try:
            with self.market_engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT symbol, base_asset, quote_asset, price_precision, quantity_precision,
                           tick_size, step_size, min_notional, leverage_max, status, updated_ts
                    FROM symbols WHERE symbol = :symbol
                """), {"symbol": symbol}).mappings().first()

                if result:
                    return SymbolInfo(dict(result))
        except Exception as e:
            self.record_error({"error_type": "symbol_lookup", "symbol": symbol, "error": str(e)})
        return None

    def get_all_symbols(self) -> List[SymbolInfo]:
        """Получить все активные символы."""
        try:
            with self.market_engine.connect() as conn:
                results = conn.execute(text("""
                    SELECT symbol, base_asset, quote_asset, price_precision, quantity_precision,
                           tick_size, step_size, min_notional, leverage_max, status, updated_ts
                    FROM symbols 
                    WHERE status IS NULL OR status != 'DISABLED'
                    ORDER BY symbol
                """)).mappings().all()

                return [SymbolInfo(dict(r)) for r in results]
        except Exception as e:
            self.record_error({"error_type": "symbols_lookup", "error": str(e)})
            return []

    def upsert_symbol(self, symbol_info: SymbolInfo) -> None:
        """Добавить или обновить информацию о символе."""
        try:
            with self.market_engine.begin() as conn:
                conn.execute(text("""
                    INSERT OR REPLACE INTO symbols 
                    (symbol, base_asset, quote_asset, price_precision, quantity_precision,
                     tick_size, step_size, min_notional, leverage_max, status, updated_ts)
                    VALUES (:symbol, :base_asset, :quote_asset, :price_precision, :quantity_precision,
                            :tick_size, :step_size, :min_notional, :leverage_max, :status, :updated_ts)
                """), {
                    **symbol_info,
                    "updated_ts": get_current_timestamp_ms()
                })

                self.record_signal(
                    symbol_info["symbol"],
                    "SYMBOL_UPDATED",
                    action="upsert"
                )
        except Exception as e:
            self.record_error({
                "error_type": "symbol_upsert",
                "symbol": symbol_info.get("symbol"),
                "error": str(e)
            })

    # === Position Management ===

    def create_position(self, position: PositionRecord) -> Optional[int]:
        """Создать новую позицию. Возвращает ID или None."""
        try:
            normalized_position = self._normalize_params(dict(position))
            normalized_position["created_ts"] = get_current_timestamp_ms()
            normalized_position["updated_ts"] = get_current_timestamp_ms()

            with self.trades_engine.begin() as conn:
                result = conn.execute(text("""
                    INSERT INTO positions 
                    (symbol, side, status, entry_ts, entry_price, qty, position_usdt,
                     leverage, reason_entry, correlation_id, fee_total_usdt, created_ts, updated_ts)
                    VALUES (:symbol, :side, :status, :entry_ts, :entry_price, :qty, :position_usdt,
                            :leverage, :reason_entry, :correlation_id, :fee_total_usdt, :created_ts, :updated_ts)
                """), normalized_position)

                position_id = result.lastrowid
                self._stats["positions_created"] += 1

                self.record_signal(
                    position["symbol"],
                    "POSITION_CREATED",
                    position_id=position_id,
                    side=position["side"],
                    entry_price=float(position["entry_price"]),
                    qty=float(position["qty"]),
                    correlation_id=position.get("correlation_id")
                )

                return position_id
        except Exception as e:
            self.record_error({
                "error_type": "position_create",
                "symbol": position.get("symbol"),
                "error": str(e)
            })
            return None

    def update_position(self, position_id: int, updates: Dict[str, Any]) -> bool:
        """Обновить позицию по ID."""
        if not updates:
            return True

        try:
            updates["updated_ts"] = get_current_timestamp_ms()
            normalized_updates = self._normalize_params(updates)

            set_clauses = [f"{key} = :{key}" for key in normalized_updates.keys()]
            set_clause = ", ".join(set_clauses)

            with self.trades_engine.begin() as conn:
                result = conn.execute(text(f"""
                    UPDATE positions SET {set_clause} WHERE id = :position_id
                """), {**normalized_updates, "position_id": position_id})

                if result.rowcount > 0:
                    self.record_signal(
                        "SYSTEM",
                        "POSITION_UPDATED",
                        position_id=position_id,
                        updates=list(updates.keys())
                    )
                    return True
        except Exception as e:
            self.record_error({
                "error_type": "position_update",
                "position_id": position_id,
                "error": str(e)
            })
        return False

    def get_position_by_id(self, position_id: int) -> Optional[PositionRecord]:
        """Получить позицию по ID."""
        try:
            with self.trades_engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT * FROM positions WHERE id = :position_id
                """), {"position_id": position_id}).mappings().first()

                if result:
                    position_data = dict(result)
                    for field in ["entry_price", "qty", "position_usdt", "exit_price",
                                  "realized_pnl_usdt", "realized_pnl_pct", "leverage", "fee_total_usdt"]:
                        if position_data.get(field) is not None:
                            position_data[field] = Decimal(str(position_data[field]))
                    return PositionRecord(position_data)
        except Exception as e:
            self.record_error({
                "error_type": "position_lookup",
                "position_id": position_id,
                "error": str(e)
            })
        return None


    def close_position(self, position_id: int, exit_price: Decimal, exit_reason: str = "MANUAL",
                       exit_fee: Decimal = None, gross_pnl_usdt: Decimal = None, gross_pnl_pct: Decimal = None,
                       net_pnl_usdt: Decimal = None, net_pnl_pct: Decimal = None) -> bool:
        """Закрыть позицию с автоматическим созданием trade record."""
        try:
            position_before_close = self.get_position_by_id(position_id)
            if not position_before_close:
                self.logger.error(f"Position {position_id} not found")
                return False

            symbol = position_before_close["symbol"]

            if all(v is not None for v in [gross_pnl_usdt, gross_pnl_pct, net_pnl_usdt, net_pnl_pct, exit_fee]):
                entry_fee = position_before_close.get("fee_total_usdt", Decimal('0')) or Decimal('0')
                total_fees = entry_fee + exit_fee

                self.logger.info(
                    f"Closing position {position_id}: "
                    f"gross={float(gross_pnl_usdt):.4f} USDT, "
                    f"net={float(net_pnl_usdt):.4f} USDT, "
                    f"fees={float(total_fees):.4f} USDT"
                )
            else:
                last_orders = self.get_orders_for_position(position_id, status="FILLED", limit=1)

                if last_orders:
                    last_order = last_orders[0]
                    order_type = str(last_order.get("type", "")).upper()

                    if order_type in ["STOP_MARKET", "STOP", "STOP_LOSS"]:
                        exit_reason = "STOP_LOSS"
                    elif order_type in ["TAKE_PROFIT", "TAKE_PROFIT_MARKET"]:
                        exit_reason = "TAKE_PROFIT"

                entry_price = position_before_close["entry_price"]
                qty = position_before_close["qty"]
                side = position_before_close["side"]

                if side == "LONG":
                    gross_pnl_usdt = (exit_price - entry_price) * qty
                else:
                    gross_pnl_usdt = (entry_price - exit_price) * qty

                position_usdt = entry_price * qty
                gross_pnl_pct = (gross_pnl_usdt / position_usdt * 100) if position_usdt > 0 else Decimal('0')

                existing_fees = position_before_close.get("fee_total_usdt", Decimal('0')) or Decimal('0')
                exit_position_usdt = exit_price * qty
                exit_fee = exit_position_usdt * Decimal('0.001') if exit_fee is None else exit_fee
                total_fees = existing_fees + exit_fee

                net_pnl_usdt = gross_pnl_usdt - total_fees
                net_pnl_pct = (net_pnl_usdt / position_usdt * 100) if position_usdt > 0 else Decimal('0')

            success = self.update_position(position_id, {
                "status": "CLOSED",
                "exit_ts": get_current_timestamp_ms(),
                "exit_price": exit_price,
                "reason_exit": exit_reason,
                "realized_pnl_usdt": net_pnl_usdt,
                "realized_pnl_pct": net_pnl_pct,
                "fee_total_usdt": total_fees
            })

            if success:
                closed_position = self.get_position_by_id(position_id)
                if closed_position:
                    self._create_trade_record_from_position(
                        closed_position,
                        gross_pnl_usdt=gross_pnl_usdt,
                        gross_pnl_pct=gross_pnl_pct
                    )

            return success

        except Exception as e:
            self.record_error({
                "error_type": "position_close",
                "position_id": position_id,
                "error": str(e)
            })
            return False

    def get_orders_for_position(self, position_id: int, status: str = None, limit: int = None) -> List[Dict[str, Any]]:
        """Получить ордера для позиции."""
        try:
            query = "SELECT * FROM orders WHERE position_id = :position_id"
            params = {"position_id": position_id}

            if status:
                query += " AND status = :status"
                params["status"] = status

            query += " ORDER BY created_ts DESC"

            if limit:
                query += f" LIMIT {limit}"

            with self.trades_engine.connect() as conn:
                result = conn.execute(text(query), params)
                columns = result.keys()

                return [dict(zip(columns, row)) for row in result]

        except Exception as e:
            self.logger.error(f"Error getting orders for position {position_id}: {e}")
            return []

    def _create_trade_record_from_position(self, position, gross_pnl_usdt: Decimal = None,
                                           gross_pnl_pct: Decimal = None):
        """Создать trade record из закрытой позиции."""
        try:
            duration_seconds = None
            if position.get("entry_ts") and position.get("exit_ts"):
                duration_seconds = (position["exit_ts"] - position["entry_ts"]) // 1000

            if gross_pnl_usdt is None or gross_pnl_pct is None:
                entry_price = position.get("entry_price")
                exit_price = position.get("exit_price")
                qty = position.get("qty")
                side = position.get("side")
                position_usdt = position.get("position_usdt")

                if entry_price and exit_price and qty and side:
                    if side == "LONG":
                        gross_pnl_usdt = (exit_price - entry_price) * qty
                    else:
                        gross_pnl_usdt = (entry_price - exit_price) * qty

                    if position_usdt and position_usdt > 0:
                        gross_pnl_pct = (gross_pnl_usdt / position_usdt) * 100

            bars_in_trade = None
            if position.get("entry_ts") and position.get("exit_ts"):
                time_diff_ms = position["exit_ts"] - position["entry_ts"]
                bars_in_trade = max(1, time_diff_ms // 30000)

            trade_record = {
                "symbol": position["symbol"],
                "entry_ts": position.get("entry_ts"),
                "exit_ts": position.get("exit_ts"),
                "entry_price": position.get("entry_price"),
                "exit_price": position.get("exit_price"),
                "side": position.get("side"),
                "quantity": position.get("qty"),
                "position_size_usdt": position.get("position_usdt"),
                "gross_pnl_percent": gross_pnl_pct,
                "gross_pnl_usdt": gross_pnl_usdt,
                "net_pnl_percent": position.get("realized_pnl_pct"),
                "net_pnl_usdt": position.get("realized_pnl_usdt"),
                "fee_total": position.get("fee_total_usdt"),
                "duration_seconds": duration_seconds,
                "reason": position.get("reason_entry", "SIGNAL"),
                "exit_reason": position.get("reason_exit", "SIGNAL"),
                "bars_in_trade": bars_in_trade
            }

            normalized_record = self._normalize_params(trade_record)
            return self.create_trade_record(normalized_record)

        except Exception as e:
            self.logger.error(f"Error creating trade record: {e}")
            return None

    # === Order Management ===

    def create_order_from_req(self, order_req: OrderReq, position_id: Optional[int] = None) -> bool:
        """Создать ордер из OrderReq."""
        try:
            existing_order = self.get_order(order_req["client_order_id"])
            if existing_order:
                self.logger.warning(f"Order {order_req['client_order_id']} already exists")
                return True

            order_record: OrderRecord = {
                "client_order_id": order_req["client_order_id"],
                "position_id": position_id,
                "symbol": order_req["symbol"],
                "type": order_req["type"],
                "side": order_req["side"],
                "tif": order_req.get("time_in_force") if order_req["type"] in ["LIMIT", "STOP",
                                                                               "STOP_MARKET"] else None,
                "qty": order_req["qty"],
                "price": order_req.get("price"),
                "stop_price": order_req.get("stop_price"),
                "reduce_only": 1 if order_req.get("reduce_only", False) else 0,
                "status": "NEW",
                "correlation_id": order_req.get("correlation_id"),
                "cancel_requested": 0,
                "created_ts": get_current_timestamp_ms(),
                "updated_ts": get_current_timestamp_ms(),
            }

            params = self._normalize_params(dict(order_record))

            with self.trades_engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO orders 
                    (client_order_id, position_id, symbol, type, side, tif, qty, price,
                     stop_price, reduce_only, status, correlation_id, cancel_requested, created_ts, updated_ts)
                    VALUES (:client_order_id, :position_id, :symbol, :type, :side, :tif, :qty, :price,
                            :stop_price, :reduce_only, :status, :correlation_id, :cancel_requested, :created_ts, :updated_ts)
                """), params)

            self._stats["orders_created"] += 1
            self.record_signal(
                order_req["symbol"],
                "ORDER_CREATED",
                client_order_id=order_req["client_order_id"],
                position_id=position_id,
                order_type=order_req["type"],
                side=order_req["side"]
            )
            return True

        except Exception as e:
            self.record_error({
                "error_type": "order_create",
                "client_order_id": order_req.get("client_order_id"),
                "error": str(e)
            })
            return False

    def update_order_on_upd(self, order_upd: OrderUpd) -> None:
        """Обновить ордер при получении обновления от биржи."""
        try:
            updates = {
                "status": order_upd["status"],
                "updated_ts": get_current_timestamp_ms()
            }

            if order_upd.get("exchange_order_id"):
                updates["exchange_order_id"] = order_upd["exchange_order_id"]

            self.update_order(order_upd["client_order_id"], updates)

        except Exception as e:
            self.record_error({
                "error_type": "order_update",
                "client_order_id": order_upd.get("client_order_id"),
                "error": str(e)
            })

    def update_order(self, client_order_id: str, updates: Dict[str, Any]) -> bool:
        """Обновить ордер по client_order_id."""
        try:
            if not client_order_id or not updates:
                return False

            normalized_updates = self._normalize_params(updates)
            if "updated_ts" not in normalized_updates:
                normalized_updates["updated_ts"] = get_current_timestamp_ms()

            set_parts = [f"{key} = :{key}" for key in normalized_updates.keys()]
            params = {"client_order_id": client_order_id, **normalized_updates}

            sql = f"UPDATE orders SET {', '.join(set_parts)} WHERE client_order_id = :client_order_id"

            with self.trades_engine.begin() as conn:
                result = conn.execute(text(sql), params)
                return result.rowcount > 0

        except Exception as e:
            self.record_error({
                "error_type": "order_update",
                "client_order_id": client_order_id,
                "error": str(e)
            })
            return False

    def get_order(self, client_order_id: str) -> Optional[OrderRecord]:
        """Получить ордер по client_order_id."""
        try:
            with self.trades_engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT * FROM orders WHERE client_order_id = :client_order_id
                """), {"client_order_id": client_order_id}).mappings().first()

                if result:
                    return OrderRecord(dict(result))

        except Exception as e:
            self.record_error({
                "error_type": "order_lookup",
                "client_order_id": client_order_id,
                "error": str(e)
            })
        return None

    # === Trade History ===

    def create_trade_record(self, trade: TradeRecord) -> Optional[int]:
        """Создать запись о торговле."""
        try:
            params = self._normalize_params(dict(trade))

            with self.trades_engine.begin() as conn:
                result = conn.execute(text("""
                    INSERT INTO trades 
                    (symbol, entry_ts, exit_ts, entry_price, exit_price, side, quantity,
                     position_size_usdt, gross_pnl_percent, gross_pnl_usdt, net_pnl_percent,
                     net_pnl_usdt, fee_total, duration_seconds, reason, exit_reason, bars_in_trade)
                    VALUES (:symbol, :entry_ts, :exit_ts, :entry_price, :exit_price, :side,
                            :quantity, :position_size_usdt, :gross_pnl_percent, :gross_pnl_usdt,
                            :net_pnl_percent, :net_pnl_usdt, :fee_total, :duration_seconds,
                            :reason, :exit_reason, :bars_in_trade)
                """), params)

                trade_id = result.lastrowid
                return trade_id

        except Exception as e:
            self.record_error({
                "error_type": "trade_record_create",
                "symbol": trade.get("symbol"),
                "error": str(e)
            })
            return None

    def get_trade_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[TradeRecord]:
        """Получить историю торгов."""
        try:
            where_clause = ""
            params = {"limit": limit}

            if symbol:
                where_clause = "WHERE symbol = :symbol"
                params["symbol"] = symbol

            with self.trades_engine.connect() as conn:
                results = conn.execute(text(f"""
                    SELECT * FROM trades {where_clause} ORDER BY exit_ts DESC LIMIT :limit
                """), params).mappings().all()

                return [TradeRecord(dict(r)) for r in results]
        except Exception as e:
            self.record_error({
                "error_type": "trade_history_lookup",
                "symbol": symbol,
                "error": str(e)
            })
            return []

    # === Statistics ===

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику работы logger'а."""
        return self._stats.copy()

    def get_trading_stats(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Получить статистику торгов."""
        try:
            where_clause = ""
            params = {}

            if symbol:
                where_clause = "WHERE symbol = :symbol"
                params["symbol"] = symbol

            with self.trades_engine.connect() as conn:
                stats = conn.execute(text(f"""
                    SELECT 
                        COUNT(*) as total_trades,
                        COUNT(CASE WHEN net_pnl_usdt > 0 THEN 1 END) as winning_trades,
                        SUM(net_pnl_usdt) as total_pnl_usdt
                    FROM trades {where_clause} WHERE exit_ts IS NOT NULL
                """), params).mappings().first()

                if stats and stats["total_trades"] > 0:
                    win_rate = (stats["winning_trades"] / stats["total_trades"]) * 100
                    return {
                        "total_trades": stats["total_trades"],
                        "winning_trades": stats["winning_trades"],
                        "win_rate_percent": round(win_rate, 2),
                        "total_pnl_usdt": float(stats["total_pnl_usdt"] or 0)
                    }
        except Exception as e:
            self.record_error({"error_type": "trading_stats", "error": str(e)})

        return {"total_trades": 0, "win_rate_percent": 0, "total_pnl_usdt": 0}

    # === Lifecycle Management ===

    async def start_async(self) -> None:
        """Запуск асинхронных компонентов."""
        if self.enable_async and not self._is_async_started:
            await self._start_async_workers()

    async def stop_async(self) -> None:
        """Остановка асинхронных компонентов."""
        if self._is_async_started:
            await self._stop_async_workers()

    def close(self) -> None:
        """Закрыть соединения с БД."""
        try:
            self.market_engine.dispose()
            self.trades_engine.dispose()
        except Exception as e:
            self.logger.error(f"Error closing TradingLogger: {e}")

    # === Private Methods ===

    def _write_log_entry(self, entry_type: str, data: Dict[str, Any], dedup_key: Optional[str] = None) -> None:
        """Запись лог-записи с фильтрацией."""
        if self._check_duplicate(dedup_key):
            return

        if entry_type == "signal":
            intent = data.get("intent", "")
            if intent in ["LONG_OPEN", "SHORT_OPEN", "LONG_CLOSE", "SHORT_CLOSE"]:
                self.logger.info(f"[SIGNAL] {data.get('symbol', '')} {intent}")
            else:
                self.logger.debug(f"[SIGNAL] {data.get('symbol', '')} {intent}")
        elif entry_type == "error":
            self.logger.error(f"[ERROR] {data}")

        self._stats[f"{entry_type}s_logged"] += 1

    def _check_duplicate(self, dedup_key: Optional[str]) -> bool:
        """Проверка дедупликации."""
        if not dedup_key:
            return False

        current_time = get_current_timestamp_ms()
        if dedup_key in self._dedup_cache:
            if current_time - self._dedup_cache[dedup_key] < 3600000:
                self._stats["duplicates_rejected"] += 1
                return True

        self._dedup_cache[dedup_key] = current_time
        return False

    def _log_sync(self, entry_type: str, data: Dict[str, Any], **kwargs) -> None:
        """Синхронное логирование."""
        if entry_type == "signal":
            self.record_signal(data.get("symbol", "UNKNOWN"), data.get("signal_type", "UNKNOWN"), **data)
        elif entry_type == "trade":
            self.record_trade(data, **kwargs)
        elif entry_type == "error":
            self.record_error(data, **kwargs)

    async def _log_async(self, entry_type: str, data: Dict[str, Any], **kwargs) -> None:
        """Асинхронное логирование."""
        if entry_type == "signal":
            await self.record_signal_async(data.get("symbol", "UNKNOWN"), data.get("signal_type", "UNKNOWN"), **data)
        elif entry_type == "trade":
            await self.record_trade_async(data, **kwargs)
        elif entry_type == "error":
            await self.record_error_async(data, **kwargs)

    # === Async Infrastructure ===

    async def _ensure_async_started(self) -> None:
        """Ленивая инициализация async компонентов."""
        if not self._is_async_started and self.enable_async:
            await self._start_async_workers()

    async def _start_async_workers(self) -> None:
        """Запуск асинхронных воркеров."""
        if self._is_async_started:
            return

        self._async_queues = {
            "signal": asyncio.Queue(maxsize=1000),
            "trade": asyncio.Queue(maxsize=1000),
            "error": asyncio.Queue(maxsize=1000)
        }

        self._async_workers = []
        for queue_type, queue in self._async_queues.items():
            for i in range(self.pool_size):
                worker = asyncio.create_task(self._async_worker(queue_type, queue))
                self._async_workers.append(worker)

        self._is_async_started = True

    async def _stop_async_workers(self) -> None:
        """Остановка асинхронных воркеров."""
        if not self._is_async_started:
            return

        if self._async_queues:
            for queue in self._async_queues.values():
                await queue.put({"_stop": True})

        if self._async_workers:
            await asyncio.gather(*self._async_workers, return_exceptions=True)

        self._async_queues = None
        self._async_workers = None
        self._is_async_started = False

    async def _async_worker(self, queue_type: str, queue: asyncio.Queue) -> None:
        """Асинхронный воркер."""
        batch = []
        last_flush = time.time()

        while True:
            try:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    item = None

                if item and item.get("_stop"):
                    break

                if item and not item.get("_flush"):
                    batch.append(item)

                if (time.time() - last_flush > 5.0) or len(batch) >= 10:
                    if batch:
                        for entry in batch:
                            self._write_log_entry(queue_type, entry)
                        batch.clear()
                        last_flush = time.time()

            except Exception as e:
                self.logger.error(f"Async worker error: {e}")

    async def _enqueue_async(self, entry_type: str, data: Dict[str, Any]) -> None:
        """Поместить данные в асинхронную очередь."""
        if self._async_queues and entry_type in self._async_queues:
            try:
                await self._async_queues[entry_type].put(data)
            except asyncio.QueueFull:
                self._write_log_entry(entry_type, data)

    def get_open_positions_db(self, symbol: Optional[str] = None) -> List[PositionRecord]:
        """Получить открытые позиции из БД (было get_open_positions)."""
        try:
            where_clause = "WHERE status IN ('OPEN', 'CLOSING')"
            params = {}

            if symbol:
                where_clause += " AND symbol = :symbol"
                params["symbol"] = symbol

            with self.trades_engine.connect() as conn:
                results = conn.execute(text(f"""
                    SELECT * FROM positions {where_clause} ORDER BY entry_ts DESC
                """), params).mappings().all()

                positions = []
                for r in results:
                    position_data = dict(r)
                    # Преобразуем Decimal поля
                    for field in ["entry_price", "qty", "position_usdt", "exit_price",
                                  "realized_pnl_usdt", "realized_pnl_pct", "leverage", "fee_total_usdt"]:
                        if position_data.get(field) is not None:
                            position_data[field] = Decimal(str(position_data[field]))
                    positions.append(PositionRecord(**position_data))
                return positions
        except Exception as e:
            self.record_error({
                "error_type": "positions_lookup_db",
                "symbol": symbol,
                "error": str(e)
            })
            return []

    def get_open_positions(self, symbol: Optional[str] = None) -> List[PositionRecord]:
        """DEPRECATED: Use get_open_positions_db() instead."""
        self.logger.warning("get_open_positions() is deprecated, use get_open_positions_db()")
        return self.get_open_positions_db(symbol)

    def ensure_trading_schema(self) -> None:
        """Создание схемы торговых таблиц."""
        try:
            with self.trades_engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        entry_ts BIGINT NOT NULL,
                        exit_ts BIGINT,
                        entry_price DECIMAL(18,8) NOT NULL,
                        exit_price DECIMAL(18,8),
                        side TEXT NOT NULL,
                        quantity DECIMAL(18,8) NOT NULL,
                        position_size_usdt DECIMAL(18,8) NOT NULL,
                        gross_pnl_percent DECIMAL(18,8),
                        gross_pnl_usdt DECIMAL(18,8),
                        net_pnl_percent DECIMAL(18,8),
                        net_pnl_usdt DECIMAL(18,8),
                        fee_total DECIMAL(18,8),
                        duration_seconds INT,
                        reason TEXT,
                        exit_reason TEXT,
                        bars_in_trade INTEGER
                    )
                """))

                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
                        status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSING','CLOSED','FLAT')) DEFAULT 'OPEN',
                        entry_ts BIGINT NOT NULL,
                        entry_price DECIMAL(18,8) NOT NULL,
                        qty DECIMAL(18,8) NOT NULL,
                        position_usdt DECIMAL(18,8) NOT NULL,
                        exit_ts BIGINT,
                        exit_price DECIMAL(18,8),
                        realized_pnl_usdt DECIMAL(18,8),
                        realized_pnl_pct DECIMAL(18,8),
                        leverage DECIMAL(18,8),
                        fee_total_usdt DECIMAL(18,8),
                        reason_entry TEXT,
                        reason_exit TEXT,
                        correlation_id TEXT,
                        created_ts BIGINT DEFAULT (strftime('%s','now')*1000),
                        updated_ts BIGINT DEFAULT (strftime('%s','now')*1000)
                    )
                """))

                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS orders (
                        client_order_id TEXT PRIMARY KEY,
                        position_id INTEGER,
                        symbol TEXT NOT NULL,
                        type TEXT NOT NULL,
                        side TEXT NOT NULL,
                        tif TEXT,
                        qty DECIMAL(18,8) NOT NULL,
                        price DECIMAL(18,8),
                        stop_price DECIMAL(18,8),
                        reduce_only INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'NEW',
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        exchange_order_id TEXT,
                        correlation_id TEXT,
                        created_ts BIGINT DEFAULT (strftime('%s','now')*1000),
                        updated_ts BIGINT DEFAULT (strftime('%s','now')*1000)
                    )
                """))

        except Exception as e:
            self.record_error({"error_type": "ensure_schema", "error": str(e)})
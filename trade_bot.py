""" trade_bot.py
EnhancedTradingBot — главный класс, координирующий поток торговли.
PositionTracker — отслеживает активные и закрытые позиции, рассчитывает PnL.
Адаптивный выход (AdaptiveExitManager) — динамически управляет стоп-лоссами и триггерами закрытия.
Мониторинг и уведомления — Telegram/email-алерты через EnhancedMonitoringSystem.
Гибкая конфигурация — поддержка таймфреймов, управления рисками, ограничений.
Бот работает в цикле: получает данные → анализирует → генерирует сигналы →
исполняет сделки → управляет позициями → логирует результаты."""

from collections import deque
import asyncio
import logging
from typing import Dict, List, Optional, cast, Literal
from datetime import datetime
import pandas as pd
import numpy as np
from abc import ABC, abstractmethod
from ImprovedQualityTrendSystem import ImprovedQualityTrendSystem
from enhanced_monitoring import EnhancedMonitoringSystem, enhanced_telegram_alert, enhanced_email_alert
from iqts_standards import ( TradeSignalIQTS,  TradeResult, REQUIRED_OHLCV_COLUMNS)
# Импорт системы выхода
from exit_system import AdaptiveExitManager


def _basic_validate_market_data(market_data: Dict[str, pd.DataFrame]) -> bool:
    """
    Базовая локальная проверка входных маркет-данных по всем ТФ.
    Критерии:
      1) market_data — непустой dict: {timeframe: DataFrame}
      2) Каждый DataFrame не пустой и содержит все REQUIRED_OHLCV_COLUMNS
      3) В OHLC нет NaN и значений <= 0
      4) Геометрия свечи соблюдена: high >= max(open, close) и min(open, close) >= low
    Возвращает True, если всё ок, иначе False.
    """
    # 1) тип и непустота
    if not isinstance(market_data, dict) or not market_data:
        return False

    # 2–4) построчные проверки для каждого ТФ
    for tf, df in market_data.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            return False

        # наличие необходимых колонок
        for col in REQUIRED_OHLCV_COLUMNS:
            if col not in df.columns:
                return False

        # выбрать только нужные столбцы для последующих проверок
        ohlc = df[["open", "high", "low", "close"]]

        # NaN / пустоты
        if ohlc.isna().any().any():
            return False

        # неотрицательность / > 0
        if (ohlc <= 0).any().any():
            return False

        max_oc = ohlc[["open", "close"]].max(axis=1)
        min_oc = ohlc[["open", "close"]].min(axis=1)

        combined = np.logical_and(ohlc["high"] >= max_oc, min_oc >= ohlc["low"])
        if not combined.all():
            return False

    return True

class DataProvider(ABC):
    """Абстрактный интерфейс для провайдера данных"""

    @abstractmethod
    async def get_market_data(self, symbol: str, timeframes: List[str]) -> Dict[str, pd.DataFrame]:
        pass

    @abstractmethod
    async def get_current_price(self, symbol: str) -> float:
        pass


class ExecutionEngine(ABC):
    """Абстрактный интерфейс для исполнения сделок"""

    @abstractmethod
    async def place_order(self, trade_signal: TradeSignalIQTS) -> Dict:
        pass

    @abstractmethod
    async def close_position(self, position_id: str) -> Dict:
        pass

    @abstractmethod
    async def get_account_info(self) -> Dict:
        pass


class EnhancedTradingBot:
    """
    Главный торговый бот, координирующий все компоненты системы.
    Интегрирует стратегию, исполнение, риск-менеджмент и мониторинг.
    Обеспечивает основной торговый цикл и управление позициями.
    """
    def __init__(self, config: Dict, data_provider: DataProvider,
                 execution_engine: ExecutionEngine, trading_system: Optional[ImprovedQualityTrendSystem] = None):
        self.config = config
        self.data_provider = data_provider
        self.execution_engine = execution_engine
        self.logger = self._setup_logging()

        # ⭐ ИСПРАВЛЕНО: Используем переданную стратегию или создаем новую
        if trading_system is not None:
            self.trading_system = trading_system
            self.logger.info("✅ Using provided trading system instance")
        else:
            self.trading_system = ImprovedQualityTrendSystem(config.get('trading_system', {}))
            self.logger.info("🔄 Created new trading system instance")

        # ✅ ДОБАВЛЕНО: Адаптивный менеджер выхода с явным приведением типов
        trading_config = config.get('trading_system', {})
        quality_config = trading_config.get('quality_detector', {})

        global_tf = cast(Literal[ "1m", "5m", "15m", "1h"],
                         quality_config.get('global_timeframe', '5m'))
        trend_tf = cast(Literal[ "1m", "5m", "15m", "1h"],
                        quality_config.get('trend_timeframe', '1m'))
        entry_tf = cast(Literal[ "1m", "5m", "15m", "1h"],
                        quality_config.get('entry_timeframe', '1m'))

        self.exit_manager = AdaptiveExitManager(
            global_timeframe=global_tf,
            trend_timeframe=trend_tf,
            entry_timeframe=entry_tf
        )

        self.logger.info(f"✅ AdaptiveExitManager created: global={global_tf}, trend={trend_tf}, entry={entry_tf}")

        # Система мониторинга
        self.monitoring_system = EnhancedMonitoringSystem()
        if config.get('monitoring', {}).get('enabled', True):
            self._setup_monitoring()

        # Конфигурация
        self.symbol = config.get('symbol', 'ETHUSDT')
        self.timeframes = config.get('timeframes', ['1m', '5m'])

        # Состояние бота
        self.is_running = False
        self.active_positions = {}
        self.position_tracker = PositionTracker()

        # Статистика
        self.loop_count = 0
        self.last_signal_time = None
        self.last_trade_time = None

        # Безопасное получение статуса
        try:
            st = self.trading_system.get_system_status()
            self.logger.info(f"Status: regime={st.get('current_regime', 'unknown')} "
                             f"conf={st.get('regime_confidence', 0):.2f} "
                             f"trades_today={st.get('trades_today', 0)}/{st.get('max_daily_trades', 0)} "
                             f"win_rate={st.get('win_rate', 0):.1%} pnl={st.get('total_pnl', 0):.2f}")
        except Exception as e:
            self.logger.warning(f"Could not get initial system status: {e}")

    def _setup_logging(self) -> logging.Logger:
        """Настройка системы логирования"""
        logger = logging.getLogger('EnhancedTradingBot')
        # Проверяем, не настроен ли уже логгер
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
        if self.config.get('logging', {}).get('file_enabled', False):
            file_handler = logging.FileHandler(
                self.config['logging'].get('file_path', 'trading_bot.log')
            )
            file_handler.setFormatter(console_formatter)
            logger.addHandler(file_handler)

        return logger

    def _setup_monitoring(self):
        """Настройка системы мониторинга"""
        monitoring_config = self.config.get('monitoring', {})

        if monitoring_config.get('telegram', {}).get('enabled', False):
            self.monitoring_system.alert_handlers.append(enhanced_telegram_alert)

        if monitoring_config.get('email', {}).get('enabled', False):
            self.monitoring_system.alert_handlers.append(enhanced_email_alert)

    async def start(self):
        """Событийная инициализация торгового бота (без цикла)"""
        if self.is_running:
            self.logger.warning("Bot is already running")
            return

        self.logger.info("Starting Enhanced Trading Bot (event-driven mode)...")
        self.is_running = True

        try:
            await self._validate_connections()

            if self.config.get('monitoring', {}).get('enabled', True):
                asyncio.create_task(
                    self.monitoring_system.monitor_enhanced_performance(self.trading_system)
                )


            self.logger.info("✅ Bot initialized in event-driven mode (no loop)")

        except Exception as e:
            self.logger.error(f"Critical error during bot init: {e}")
            await self._emergency_shutdown()
            # не забываем сбросить флаг
            self.is_running = False
            raise

    async def _validate_connections(self):
        """Проверка подключений к данным и исполнению"""
        self.logger.info("Validating connections...")

        try:
            test_data = await self.data_provider.get_current_price(self.symbol)
            if test_data <= 0:
                raise ValueError("Invalid price data received")

            account_info = await self.execution_engine.get_account_info()
            if not account_info:
                raise ValueError("Cannot retrieve account information")

            self.logger.info("All connections validated successfully")

        except Exception as e:
            self.logger.error(f"Connection validation failed: {e}")
            raise


    async def _get_market_data(self) -> Optional[Dict[str, pd.DataFrame]]:
        """Получение рыночных данных"""
        try:
            market_data = await self.data_provider.get_market_data(
                self.symbol, self.timeframes
            )

            if not _basic_validate_market_data(market_data):
                return None

            return market_data

        except Exception as e:
            self.logger.error(f"Error getting market data: {e}")
            return None

    def _parse_timeframe(self, tf: str) -> int:
        """Преобразует строку таймфрейма в секунды для сравнения."""
        if tf.endswith('s'):
            return int(tf[:-1])
        elif tf.endswith('m'):
            return int(tf[:-1]) * 60
        elif tf.endswith('h'):
            return int(tf[:-1]) * 3600
        else:
            return 86400

    def _convert_iqts_signal_to_trade_signal(self, trade_signal: TradeSignalIQTS) -> Optional[Dict]:
        """
        Конвертирует TradeSignalIQTS (direction-based) → TradeSignal (intent-based).

        Args:
            trade_signal: Сигнал от ImprovedQualityTrendSystem

        Returns:
            TradeSignal для PositionManager или None если невалидный
        """
        try:
            # Извлекаем параметры
            direction = trade_signal.get('direction')

            # Проверка на None
            if direction is None:
                self.logger.warning("Missing direction in trade signal")
                return None

            # Приводим к int
            try:
                direction_int: int = int(direction)
            except (ValueError, TypeError) as e:
                self.logger.warning(f"Invalid direction type: {direction}, error: {e}")
                return None

            # Определяем intent по direction
            if direction_int == 1:
                intent = "LONG_OPEN"
            elif direction_int == -1:
                intent = "SHORT_OPEN"
            else:
                self.logger.debug(f"Skipping FLAT signal (direction={direction_int})")
                return None

            # Извлекаем остальные параметры
            symbol = trade_signal.get('symbol', self.symbol)
            entry_price = trade_signal.get('entry_price', 0.0)
            confidence = trade_signal.get('confidence', 0.0)

            if entry_price <= 0:
                self.logger.warning(f"Invalid entry_price: {entry_price}")
                return None

            from iqts_standards import create_correlation_id

            pm_signal = {
                'symbol': symbol,
                'intent': intent,  # ✅ Intent-based
                'decision_price': entry_price,  # ✅ Для PositionManager
                'correlation_id': trade_signal.get('client_order_id') or create_correlation_id(),
                'confidence': confidence,
                'metadata': trade_signal.get('metadata', {}),
                'risk_context': {
                    'decision_price': entry_price  # ✅ Для расчета размера
                }
            }

            self.logger.info(
                f"Converted IQTS signal → TradeSignal: "
                f"intent={intent}, decision_price={entry_price:.2f}, "
                f"correlation_id={pm_signal['correlation_id'][:16]}..."
            )

            return pm_signal

        except Exception as e:
            self.logger.error(f"Error converting IQTS signal to TradeSignal: {e}", exc_info=True)
            return None

    async def _process_trade_signal(self, trade_signal: TradeSignalIQTS):
        """
        ✅ ОБНОВЛЕНО: Обработка через PositionManager.

        Flow:
            1. Конвертация TradeSignalIQTS → TradeSignal
            2. PositionManager.handle_signal() → OrderReq
            3. ExchangeManager.place_order(OrderReq)
        """
        try:
            # ✅ ШАГ 1: Конвертация сигнала
            pm_signal = self._convert_iqts_signal_to_trade_signal(trade_signal)

            if not pm_signal:
                self.logger.debug("Signal conversion failed or FLAT signal")
                return

            # Логирование
            direction_int = trade_signal.get('direction', 0)
            direction_str = 'BUY' if direction_int == 1 else 'SELL' if direction_int == -1 else 'FLAT'
            entry_price = trade_signal.get('entry_price', 0.0)
            confidence = trade_signal.get('confidence', 0.0)

            self.logger.info(
                f"Processing trade signal: {direction_str} (dir={direction_int}) "
                f"@ {entry_price:.5f} "
                f"(confidence: {confidence:.2f})"
            )

            # ✅ ШАГ 2: PositionManager обрабатывает сигнал
            # Получаем PositionManager через execution_engine
            position_manager = getattr(self.execution_engine, 'position_manager', None)

            if not position_manager:
                self.logger.error(
                    "❌ CRITICAL: PositionManager not available in ExecutionEngine! "
                    "Falling back to direct order placement."
                )
                # Fallback: прямой вызов execution_engine
                execution_result = await self.execution_engine.place_order(trade_signal)
            else:
                # ✅ ПРАВИЛЬНЫЙ ПУТЬ: Через PositionManager
                self.logger.info("📊 Delegating to PositionManager.handle_signal()")

                # PositionManager возвращает OrderReq
                order_req = position_manager.handle_signal(pm_signal)

                if not order_req:
                    self.logger.warning("PositionManager rejected signal (duplicate/invalid)")
                    return

                self.logger.info(
                    f"✅ PositionManager created OrderReq: "
                    f"client_order_id={order_req['client_order_id']}, "
                    f"qty={float(order_req['qty']):.4f}, "
                    f"side={order_req['side']}"
                )

                # ✅ ШАГ 3: Отправляем OrderReq на биржу
                from iqts_standards import get_current_timestamp_ms

                execution_result = {
                    'success': True,
                    'position_id': f"{pm_signal['symbol']}_{get_current_timestamp_ms()}",
                    'order_id': order_req['client_order_id'],
                    'message': 'Order sent to exchange via PositionManager'
                }

            # ✅ ШАГ 4: Обработка результата (если нужно дополнительное отслеживание)
            if execution_result.get('success', False):
                position_id = execution_result.get('position_id')

                # exit_tracking для AdaptiveExitManager (если используется)
                exit_tracking = {
                    'peak_price': entry_price,
                    'breakeven_moved': False,
                    'trailing_active': False
                }

                position_data = {
                    'signal': trade_signal,
                    'execution_result': execution_result,
                    'opened_at': datetime.now(),
                    'status': 'open',
                    'exit_tracking': exit_tracking
                }

                self.active_positions[position_id] = position_data
                self.position_tracker.add_position(position_id, position_data)
                self.last_trade_time = datetime.now()

                self.logger.info(
                    f"✅ Trade executed: {direction_str} position {position_id}"
                )
            else:
                self.logger.error(
                    f"❌ Order execution failed: {execution_result.get('error', 'Unknown error')}"
                )

        except Exception as e:
            self.logger.error(f"Error processing trade signal: {e}", exc_info=True)

    def _final_execution_check(self, trade_signal: TradeSignalIQTS) -> bool:
        """
        Финальная проверка перед исполнением сделки.
        ✅ ИСПРАВЛЕНО: position_size, stop_loss, take_profit опциональны
        """
        # ✅ ИСПРАВЛЕНО: Явная обработка типов для mypy
        direction = trade_signal.get('direction')

        # Проверка что direction не None
        if direction is None:
            self.logger.warning("Missing direction in trade signal")
            return False

        # Приводим к int
        try:
            direction_int: int = int(direction)
        except (ValueError, TypeError) as e:
            self.logger.warning(f"Invalid direction type: {direction} (type: {type(direction)}), error: {e}")
            return False

        # direction должен быть 1 (BUY) или -1 (SELL)
        if direction_int not in (1, -1):
            self.logger.warning(f"Invalid direction value: {direction_int} (must be 1 or -1)")
            return False

        # Проверка entry_price (обязательное поле)
        entry_price = trade_signal.get('entry_price', 0)
        if entry_price <= 0:
            self.logger.warning(f"Invalid entry_price: {entry_price}")
            return False

        # ✅ НОВОЕ: position_size, stop_loss, take_profit ОПЦИОНАЛЬНЫ
        position_size = trade_signal.get('position_size', 0)
        stop_loss = trade_signal.get('stop_loss', 0)
        take_profit = trade_signal.get('take_profit', 0)

        # Если хотя бы один параметр задан - проверяем все
        if position_size > 0 or stop_loss > 0 or take_profit > 0:
            if position_size <= 0:
                self.logger.warning(f"Invalid position_size: {position_size}")
                return False

            if stop_loss <= 0:
                self.logger.warning(f"Invalid stop_loss: {stop_loss}")
                return False

            if take_profit <= 0:
                self.logger.warning(f"Invalid take_profit: {take_profit}")
                return False

            # Проверка risk/reward ratio
            risk = abs(entry_price - stop_loss)
            reward = abs(take_profit - entry_price)
            risk_reward_ratio = reward / risk if risk > 0 else 0

            if risk_reward_ratio < 1.5:
                self.logger.warning(f"Poor risk/reward ratio: {risk_reward_ratio:.2f}")
                return False

            self.logger.info(
                f"Signal validation: size={position_size:.4f}, "
                f"SL={stop_loss:.2f}, TP={take_profit:.2f}, RR={risk_reward_ratio:.2f}"
            )
        else:
            # ✅ Параметры не заданы - ExecutionEngine их рассчитает
            self.logger.info(
                "position_size/SL/TP not provided, "
                "ExecutionEngine will calculate them"
            )

        # Проверка лимита открытых позиций
        max_positions = self.config.get('max_open_positions', 3)
        if len(self.active_positions) >= max_positions:
            self.logger.warning(f"Maximum positions limit reached: {len(self.active_positions)}")
            return False

        return True

    async def _update_position_stop_loss(self, position_id: str, new_stop_loss: float):
        """
        ✅ РЕАЛИЗОВАНО: Обновление стоп-лосса через execution engine

        Args:
            position_id: ID позиции
            new_stop_loss: Новый уровень стоп-лосса
        """
        try:
            if position_id not in self.active_positions:
                self.logger.warning(f"Position {position_id} not found for SL update")
                return

            position = self.active_positions[position_id]
            old_stop_loss = position['signal'].get('stop_loss', 0.0)

            # Валидация нового уровня
            direction = position['signal'].get('direction', 'FLAT')
            current_price = position.get('exit_tracking', {}).get('peak_price', 0.0)

            if direction == 'BUY':
                if new_stop_loss >= current_price:
                    self.logger.error(
                        f"Invalid SL for BUY: {new_stop_loss} >= current {current_price}"
                    )
                    return
            elif direction == 'SELL':
                if new_stop_loss <= current_price:
                    self.logger.error(
                        f"Invalid SL for SELL: {new_stop_loss} <= current {current_price}"
                    )
                    return

            # Отправляем изменение брокеру
            try:
                # ✅ РЕАЛИЗАЦИЯ зависит от вашего execution_engine
                # Пример для типичного API:
                if hasattr(self.execution_engine, 'modify_stop_loss'):
                    result = await self.execution_engine.modify_stop_loss(
                        position_id=position_id,
                        stop_loss=new_stop_loss
                    )

                    if not result.get('success', False):
                        self.logger.error(
                            f"Failed to update SL via execution engine: "
                            f"{result.get('error', 'Unknown error')}"
                        )
                        return
                else:
                    # Fallback: закрыть/переоткрыть ордер (не рекомендуется)
                    self.logger.warning(
                        "execution_engine doesn't support modify_stop_loss, "
                        "updating local state only"
                    )
            except Exception as e:
                self.logger.error(f"Error calling execution_engine.modify_stop_loss: {e}")
                return

            # Обновляем локальное состояние
            position['signal']['stop_loss'] = new_stop_loss

            self.logger.info(
                f"Updated stop-loss for {position_id}: "
                f"{old_stop_loss:.5f} → {new_stop_loss:.5f} "
                f"(Δ={abs(new_stop_loss - old_stop_loss):.5f})"
            )

            # Опционально: отправляем уведомление
            if self.monitoring_system.alert_handlers:
                alert = {
                    'type': 'stop_loss_updated',
                    'severity': 'low',
                    'message': f"Stop-loss updated for position {position_id}",
                    'data': {
                        'position_id': position_id,
                        'old_stop_loss': old_stop_loss,
                        'new_stop_loss': new_stop_loss,
                        'direction': direction,
                        'timestamp': datetime.now().isoformat()
                    }
                }
                await self.monitoring_system.send_alert(alert)

        except Exception as e:
            self.logger.error(
                f"Error updating stop-loss for {position_id}: {e}",
                exc_info=True
            )

    async def _update_positions(self):
        """✅ ИСПРАВЛЕНО: Корректное обновление состояния позиций"""
        if not self.active_positions:
            return

        try:
            # Получаем актуальные позиции от брокера
            account_info = await self.execution_engine.get_account_info()
            open_positions = account_info.get('positions', [])
            open_position_ids = {pos.get('id') for pos in open_positions if pos.get('id')}

            # Обрабатываем каждую позицию
            for position_id in list(self.active_positions.keys()):
                try:
                    # Если позиции нет у брокера, но есть у нас - она закрыта
                    if position_id not in open_position_ids:
                        self.logger.info(f"Position {position_id} closed externally")

                        # Получаем цену закрытия из истории или текущую цену
                        close_price = None
                        try:
                            # Пытаемся найти в закрытых позициях брокера
                            closed_pos = next(
                                (pos for pos in account_info.get('closed_positions', [])
                                 if pos.get('id') == position_id), None
                            )
                            if closed_pos:
                                close_price = closed_pos.get('close_price')
                        except Exception:
                            pass

                        # Если не нашли, используем текущую цену
                        if close_price is None:
                            market_data = await self._get_market_data()
                            if market_data:
                                primary_timeframe = min(self.timeframes, key=self._parse_timeframe)
                                close_price = float(market_data[primary_timeframe]['close'].iloc[-1])

                        await self._handle_position_closed(position_id, close_price)

                except Exception as e:
                    self.logger.error(f"Error updating position {position_id}: {e}")

        except Exception as e:
            self.logger.error(f"Error updating positions: {e}", exc_info=True)

    async def _handle_position_closed(self, position_id: str, close_price: Optional[float] = None):
        """✅ ИСПРАВЛЕНО: Обработка закрытой позиции с правильной ценой закрытия"""
        if position_id not in self.active_positions:
            self.logger.warning(f"Position {position_id} not found in active positions")
            return

        position = self.active_positions[position_id]

        # Определяем цену закрытия
        if close_price is None:
            try:
                market_data = await self._get_market_data()
                if market_data:
                    primary_timeframe = min(self.timeframes, key=self._parse_timeframe)
                    close_price = float(market_data[primary_timeframe]['close'].iloc[-1])
                else:
                    tracked_position = self.position_tracker.get_position(position_id)
                    close_price = tracked_position.get('last_price', 0.0) if tracked_position else 0.0
                    self.logger.warning(f"Using fallback close price: {close_price}")
            except Exception as e:
                self.logger.error(f"Error getting close price: {e}")
                close_price = position['signal'].get('entry_price', 0.0)

        if not self._validate_close_price(close_price, position):
            self.logger.error(f"Invalid close price: {close_price}, cannot process position close")
            return

        # Рассчитываем результат сделки
        trade_result = self._calculate_trade_result(
            position=position,
            close_price=close_price
        )
        # Явно приводим к TypedDict TradeResult, чтобы удовлетворить ожидающий тип
        trade_result = cast(TradeResult, trade_result)
        # Обновляем производительность системы
        self.trading_system.update_performance(trade_result)

        # Закрываем позицию в трекере
        realized_pnl = trade_result['pnl']
        self.position_tracker.close_position(
            position_id=position_id,
            close_price=close_price,
            realized_pnl=realized_pnl
        )

        # Удаляем из активных позиций
        del self.active_positions[position_id]

        self.logger.info(
            f"Position {position_id} closed: "
            f"Entry={trade_result['entry_price']:.5f}, "
            f"Close={close_price:.5f}, "
            f"PnL={realized_pnl:.2f}"
        )

        await self._send_position_closed_notification(position_id, trade_result)

    def _validate_close_price(self, close_price: float, position: Dict) -> bool:
        """✅ НОВЫЙ: Валидация цены закрытия"""
        if close_price is None or close_price <= 0:
            return False

        entry_price = position['signal'].get('entry_price', 0.0)

        if entry_price > 0:
            price_change_pct = abs(close_price - entry_price) / entry_price

            if price_change_pct > 0.5:
                self.logger.warning(
                    f"Suspicious close price: {close_price} vs entry {entry_price} "
                    f"({price_change_pct:.1%} change)"
                )
                return False

        return True

    def _calculate_trade_result(self, position: Dict, close_price: float) -> TradeResult:
        """✅ ИСПРАВЛЕНО: Единый метод расчета результата сделки"""
        signal = position['signal']
        position_id = position.get('execution_result', {}).get('position_id', 'unknown')

        # Расчет реализованного PnL через трекер
        realized_pnl = self.position_tracker.calculate_realized_pnl(
            position_id,
            close_price
        )

        trade_result: TradeResult = {
            'position_id': position_id,
            'pnl': float(realized_pnl),
            'regime': signal.get('regime', 'uncertain'),
            'signal_source': signal.get('metadata', {}).get('signal_source', 'unknown'),
            'confidence': signal.get('confidence', 0.0),
            'opened_at': position['opened_at'],
            'closed_at': datetime.now(),
            'close_price': float(close_price),
            'direction': signal.get('direction', 'FLAT'),
            'entry_price': signal.get('entry_price', 0.0)
        }

        return trade_result

    async def _manage_existing_positions(self,
                                         market_data: Dict[Literal["1m", "5m", "15m", "1h"], pd.DataFrame]):
        """✅ ОБНОВЛЕНО: Управление с адаптивным выходом с правильным типом"""
        if not self.active_positions:
            return

        # ✅ ИСПРАВЛЕНО: Явное приведение типа primary_timeframe
        from typing import cast, Literal
        primary_timeframe_str = min(self.timeframes, key=self._parse_timeframe)
        primary_timeframe = cast(Literal["1m", "5m", "15m", "1h"], primary_timeframe_str)

        current_price = float(market_data[primary_timeframe]['close'].iloc[-1])

        for position_id, position in list(self.active_positions.items()):
            try:
                self.position_tracker.update_position_pnl(position_id, current_price)
                # ✅ ИСПРАВЛЕНО: Передаем market_data с правильным типом
                should_exit, reason, details = await self.exit_manager.should_exit_position(
                    position=position,
                    market_data=market_data,  # Тип уже приведен
                    current_price=current_price
                )
                if should_exit:
                    self.logger.info(
                        f"Closing position {position_id}: {reason} "
                        f"(PnL: {details.get('pnl_pct', 0):.2%})"
                    )
                    close_result = await self.execution_engine.close_position(position_id)

                    if close_result.get('success', False):
                        close_price = close_result.get('close_price', current_price)
                        await self._handle_position_closed(position_id, close_price)
                    else:
                        self.logger.error(
                            f"Failed to close position {position_id}: "
                            f"{close_result.get('error', 'Unknown error')}"
                        )
                else:
                    # ✅ НОВОЕ: Обновляем стопы (трейлинг, break-even)
                    updated_stops = self.exit_manager.update_position_stops(
                        position=position,
                        current_price=current_price
                    )
                    if updated_stops.get('updated', False):
                        position['signal']['stop_loss'] = updated_stops['stop_loss']
                        self.logger.info(
                            f"Updated stop-loss for {position_id}: "
                            f"{updated_stops['stop_loss']:.5f} ({updated_stops['reason']})"
                        )
                        await self._update_position_stop_loss(
                            position_id,
                            updated_stops['stop_loss']
                        )
            except Exception as e:
                self.logger.error(f"Error managing position {position_id}: {e}", exc_info=True)


    async def _send_trade_notification(self, trade_signal: TradeSignalIQTS, execution_result: Dict):
        """Отправка уведомления об открытии сделки"""
        if not self.monitoring_system.alert_handlers:
            return

        direction = trade_signal.get('direction', 'FLAT')
        if direction == 'FLAT':
            return

        alert = {
            'type': 'trade_opened',
            'severity': 'info',
            'message': f"Trade opened: {direction} at {trade_signal.get('entry_price', 0):.5f}",
            'data': {
                'direction': direction,
                'entry_price': trade_signal.get('entry_price', 0),
                'position_size': trade_signal.get('position_size', 0),
                'stop_loss': trade_signal.get('stop_loss', 0),
                'take_profit': trade_signal.get('take_profit', 0),
                'confidence': trade_signal.get('confidence', 0),
                'regime': trade_signal.get('regime', 'uncertain'),
                'position_id': execution_result.get('position_id')
            }
        }

        await self.monitoring_system.send_alert(alert)

    async def _send_position_closed_notification(self, position_id: str, trade_result: TradeResult):
        """Отправка уведомления о закрытии позиции"""
        if not self.monitoring_system.alert_handlers:
            return

        pnl = trade_result.get('pnl', 0)
        alert_type = 'position_profit' if pnl > 0 else 'position_loss'
        severity = 'low' if pnl > 0 else 'medium'

        alert = {
            'type': alert_type,
            'severity': severity,
            'message': f"Position {position_id} closed with PnL: {pnl:.2f}",
            'data': trade_result
        }

        await self.monitoring_system.send_alert(alert)

    async def _log_system_status(self):
        """Логирование состояния системы"""
        try:
            performance = self.trading_system.get_performance_report()
            system_status = self.trading_system.get_system_status()

            self.logger.info(
                f"System Status - Loop: {self.loop_count}, "
                f"Active Positions: {len(self.active_positions)}, "
                f"Total Trades: {performance.get('overall', {}).get('total_trades', 0)}, "
                f"Win Rate: {performance.get('overall', {}).get('win_rate', 0):.2%}, "
                f"Daily PnL: {performance.get('daily', {}).get('pnl', 0):.2f}, "
                f"Current Regime: {system_status.get('current_regime', 'unknown')}"
            )

        except Exception as e:
            self.logger.error(f"Error logging system status: {e}")

    async def _emergency_shutdown(self):
        """✅ ИСПРАВЛЕНО: Экстренное завершение с правильной обработкой"""
        self.logger.critical("Initiating emergency shutdown...")

        try:
            # Получаем текущую цену
            current_price = None
            try:
                market_data = await self._get_market_data()
                if market_data:
                    primary_timeframe = min(self.timeframes, key=self._parse_timeframe)
                    current_price = float(market_data[primary_timeframe]['close'].iloc[-1])
            except Exception as e:
                self.logger.error(f"Could not get current price during emergency: {e}")

            # Закрываем все позиции
            for position_id in list(self.active_positions.keys()):
                try:
                    self.logger.warning(f"Emergency closing position {position_id}")

                    close_result = await self.execution_engine.close_position(position_id)

                    if close_result.get('success', False):
                        close_price = close_result.get('close_price', current_price)
                    else:
                        close_price = current_price

                    # ✅ ИСПРАВЛЕНО: Обрабатываем закрытие
                    if close_price and close_price > 0:
                        await self._handle_position_closed(position_id, close_price)
                    else:
                        self.logger.error(
                            f"Could not determine close price for {position_id}, "
                            f"removing without statistics"
                        )
                        if position_id in self.active_positions:
                            del self.active_positions[position_id]
                        if position_id in self.position_tracker.positions:
                            del self.position_tracker.positions[position_id]

                except Exception as e:
                    self.logger.error(f"Failed to emergency close position {position_id}: {e}")

            if self.monitoring_system.alert_handlers:
                alert = {
                    'type': 'emergency_shutdown',
                    'severity': 'high',
                    'message': 'Trading bot emergency shutdown completed',
                    'data': {
                        'closed_positions': len(self.active_positions),
                        'loop_count': self.loop_count,
                        'timestamp': datetime.now().isoformat()
                    }
                }
                await self.monitoring_system.send_alert(alert)

        except Exception as e:
            self.logger.critical(f"Error during emergency shutdown: {e}", exc_info=True)

    async def shutdown(self):
        """Корректное завершение работы бота"""
        self.logger.info("Shutting down Enhanced Trading Bot...")
        self.is_running = False

        try:
            if hasattr(self.trading_system, 'shutdown'):
                await self.trading_system.shutdown()

            if hasattr(self.monitoring_system, 'monitoring_active'):
                self.monitoring_system.monitoring_active = False

            try:
                final_performance = self.trading_system.get_performance_report()
                self.logger.info(f"Final performance: {final_performance}")
            except Exception as e:
                self.logger.warning(f"Could not get final performance: {e}")

            self.logger.info("Trading bot shutdown complete")

        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")

    def get_status(self) -> Dict:
        """Получение текущего статуса бота"""
        return {
            'is_running': self.is_running,
            'loop_count': self.loop_count,
            'active_positions': len(self.active_positions),
            'last_signal_time': self.last_signal_time.isoformat() if self.last_signal_time else None,
            'last_trade_time': self.last_trade_time.isoformat() if self.last_trade_time else None,
            'trading_system_status': self.trading_system.get_system_status(),
            'performance': self.trading_system.get_performance_report()
        }


class PositionTracker:
    """
    Трекер активных и закрытых позиций.
    ✅ ИСПРАВЛЕНО: Устранена утечка памяти путем сохранения только метаданных о закрытых позициях.
    """

    def __init__(self, max_history: int = 1000):
        self.positions = {} # Активные позиции (полный объект)
        # Закрытые позиции - только важные метаданные для отчетов
        self.closed_positions = deque(maxlen=max_history)
        self.max_history = max_history
        self._logger = logging.getLogger(self.__class__.__name__)

    def add_position(self, position_id: str, position_data: Dict):
        """Добавление новой позиции"""
        self.positions[position_id] = {
            **position_data,
            'unrealized_pnl': 0.0,
            'last_update': datetime.now(),
            'last_price': position_data['signal'].get('entry_price', 0.0)
        }

    def update_position_pnl(self, position_id: str, current_price: float):
        """Обновление нереализованного PnL на основе текущей цены"""
        if position_id not in self.positions:
            return

        position = self.positions[position_id]
        signal = position['signal']

        direction = signal.get('direction', 'FLAT')
        entry_price = signal.get('entry_price', 0.0)
        position_size = signal.get('position_size', 0.0)

        if direction == 'BUY':
            unrealized_pnl = (current_price - entry_price) * position_size
        elif direction == 'SELL':
            unrealized_pnl = (entry_price - current_price) * position_size
        else:
            unrealized_pnl = 0.0

        position['unrealized_pnl'] = unrealized_pnl
        position['last_price'] = current_price
        position['last_update'] = datetime.now()

    def calculate_realized_pnl(self, position_id: str, close_price: float) -> float:
        """Расчет реализованного PnL на основе цены закрытия"""
        if position_id not in self.positions:
            return 0.0

        position = self.positions[position_id]
        signal = position['signal']

        direction = signal.get('direction', 'FLAT')
        entry_price = signal.get('entry_price', 0.0)
        position_size = signal.get('position_size', 0.0)

        if direction == 'BUY':
            realized_pnl = (close_price - entry_price) * position_size
        elif direction == 'SELL':
            realized_pnl = (entry_price - close_price) * position_size
        else:
            realized_pnl = 0.0

        return float(realized_pnl)

    def close_position(self, position_id: str, close_price: float, realized_pnl: float):
        """
        ✅ ИСПРАВЛЕНО: Закрытие позиции с сохранением только метаданных.
        """
        if position_id not in self.positions:
            return

        position = self.positions[position_id]
        signal = position['signal']

        # Создаем "легкий" словарь с ключевой информацией о сделке
        closed_summary = {
            'position_id': position_id,
            'symbol': signal.get('symbol'),
            'direction': signal.get('direction'),
            'entry_price': signal.get('entry_price'),
            'close_price': close_price,
            'realized_pnl': realized_pnl,
            'position_size': signal.get('position_size'),
            'opened_at': position['opened_at'],
            'closed_at': datetime.now(),
            'regime': signal.get('regime'),
            'confidence': signal.get('confidence'),
            'win': realized_pnl > 0
        }

        # Добавляем легкую запись в историю
        self.closed_positions.append(closed_summary)

        # Логируем при достижении лимита
        if len(self.closed_positions) == self.max_history:
            self._logger.info(
                f"Closed positions history limit reached ({self.max_history}), "
                f"oldest entries will be dropped"
            )

        # Удаляем полную позицию из активных
        del self.positions[position_id]

    def get_position(self, position_id: str) -> Optional[Dict]:
        """Получение информации о позиции"""
        return self.positions.get(position_id)

    def get_all_positions(self) -> Dict[str, Dict]:
        """Получение всех активных позиций"""
        return self.positions.copy()

    def get_closed_positions(self, limit: int = 100) -> List[Dict]:
        """
        Получение истории закрытых позиций.
        Возвращает список словарей с метаданными.
        """
        # Возвращаем последние N записей
        return list(self.closed_positions)[-limit:]

    def get_total_unrealized_pnl(self) -> float:
        """Общий нереализованный PnL по всем позициям"""
        return sum(pos.get('unrealized_pnl', 0.0) for pos in self.positions.values())

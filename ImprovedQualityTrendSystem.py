"""
ImprovedQualityTrendSystem.py
Упрощённая главная торговая система.
Заменяет сложную цепочку:
    ImprovedQualityTrendSystem → HierarchicalQualityTrendSystem → ThreeLevel...
На прямую:
    ImprovedQualityTrendSystem → ThreeLevelHierarchicalConfirmator
"""

from typing import Dict, Any, Optional, cast, Literal
import pandas as pd
from datetime import datetime
import logging
from dataclasses import dataclass
from threading import Lock

from iqts_standards import (Timeframe,
    DetectorSignal, TradingSystemInterface,
     normalize_trading_hours, SystemStatus,TradeSignalIQTS,
    get_current_timestamp_ms)

# Ядро анализа
from multi_timeframe_confirmator import ThreeLevelHierarchicalConfirmator

# Риск-менеджмент
from improved_algorithm import EnhancedRiskManager

# Определяем тип для рыночных режимов
RegimeType = Literal["strong_uptrend", "weak_uptrend", "strong_downtrend", "weak_downtrend", "sideways", "uncertain"]
VolumeProfileType = Literal["high", "normal", "low"]

@dataclass
class MarketRegime:
    regime: RegimeType  # 'strong_uptrend', 'weak_uptrend', 'strong_downtrend', 'weak_downtrend', 'sideways', 'uncertain'
    confidence: float
    volatility_level: float
    trend_strength: float
    volume_profile: VolumeProfileType  # 'high', 'normal', 'low'


class ImprovedQualityTrendSystem(TradingSystemInterface):
    """
    Главная торговая система с упрощённой иерархией.
    Напрямую использует ThreeLevelHierarchicalConfirmator.
    """

    def __init__(self, config: Dict, data_provider: Optional[Any] = None):
        self.config = config
        self.logger = logging.getLogger('ImprovedQualityTrendSystem')

        # ✅ НАСТРОЙКА ЛОГГЕРА (аналогично ThreeLevelHierarchicalConfirmator)
        if not self.logger.handlers:
            self.logger.setLevel(logging.INFO)

            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            # Отключаем propagation чтобы избежать дублирования
            self.logger.propagate = False

        self.data_provider = data_provider
        self._last_reset_date = datetime.now().date()

        if data_provider:
            self.logger.info("✅ DataProvider injected for generate_signal support")
        else:
            self.logger.warning("⚠️ DataProvider not provided - generate_signal will not work")

        # Конфигурация качества
        quality_config = config.get('quality_detector', {})
        global_detector_config = quality_config.get('global_detector', {})
        self.logger.info(f"🔍 Global detector config: {global_detector_config}")

        # Конфигурация качества
        quality_config = config.get('quality_detector', {})
        global_detector_config = quality_config.get('global_detector', {})
        self.logger.info(f"🔍 Global detector config: {global_detector_config}")

        # Инициализация ядра анализа
        self.three_level_confirmator = ThreeLevelHierarchicalConfirmator(
            global_timeframe=cast(Timeframe, quality_config.get('global_timeframe', '5m')),
            trend_timeframe=cast(Timeframe, quality_config.get('trend_timeframe', '1m')),
        )
        self._cached_global_signal: Dict[str, Dict[str, Any]] = {}
        # Структура кэша:
        # { 'ETHUSDT': {
        #     'timestamp': 1763054400000,  # ts 5m свечи
        #     'global_direction': 1,        # BUY
        #     'global_confidence': 0.70,
        #     'reason': 'direction_disagreement',
        #     'trend_direction': 0,         # FLAT (несогласие)
        #     'trend_confidence': 0.30
        #   }}
        # Параметры фильтрации
        self.max_daily_trades = config.get('max_daily_trades', 15)
        self.min_volume_ratio = quality_config.get('min_volume_ratio', 1.3)
        self.max_volatility_ratio = quality_config.get('max_volatility_ratio', 1.4)

        # Состояние системы
        self.trades_today = 0
        self.last_reset_day = None
        self.volume_ema = 0.0
        self.atr_ema = 0.0
        self.volume_alpha = 0.1
        self.atr_alpha = 0.1

        # Риск-менеджмент
        self.risk_manager = self._initialize_risk_manager(config.get('risk_management', {}))
        self.performance_tracker = self._initialize_performance_tracker()

        # Рыночный режим
        self.current_regime = None
        self.daily_stats = {
            'trades_count': 0,
            'pnl': 0.0,
            'wins': 0,
            'losses': 0
        }
        self.daily_stats_history = {}
        self.account_balance = config.get('account_balance', 100000)

        # Система мониторинга
        self.monitoring_enabled = config.get('monitoring_enabled', True)
        self.alert_handlers = []
        self._daily_stats_lock = Lock()

    def _initialize_risk_manager(self, risk_config: Dict):
        return EnhancedRiskManager(
            max_portfolio_risk=risk_config.get('max_position_risk', 0.02),
            max_daily_loss=risk_config.get('max_daily_loss', 0.05),
            atr_periods=risk_config.get('atr_periods', 14),
            stop_loss_atr_multiplier=risk_config.get('stop_atr_multiplier', 2.0),
            take_profit_atr_multiplier=risk_config.get('tp_atr_multiplier', 3.0)
        )

    def _initialize_performance_tracker(self) -> Dict:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'total_pnl': 0.0,
            'max_drawdown': 0.0,
            'regime_performance': {},
            'daily_performance': {},
            'signal_quality_stats': {
                'hierarchical_confirmed': {'count': 0, 'wins': 0, 'total_pnl': 0.0}
            }
        }

    async def _apply_quality_filters(self, signal: DetectorSignal, data: Dict[str, pd.DataFrame]) -> DetectorSignal:
        vol_result = self._adaptive_volume_filter(data)
        if not vol_result["passed"]:
            return {"ok": False, "reason": "volume_filter"}

        vola_result = self._adaptive_volatility_filter(data)
        if not vola_result["passed"]:
            return {"ok": False, "reason": "volatility_filter"}

        return signal

    def _adaptive_volume_filter(self, data: Dict[str, pd.DataFrame]) -> Dict:
        df = data.get("1m")
        if df is None or len(df) < 20:
            return {"passed": True}

        current_volume = df['volume'].iloc[-1]
        if self.volume_ema == 0.0:
            self.volume_ema = df['volume'].tail(20).mean()
        else:
            self.volume_ema = (1 - self.volume_alpha) * self.volume_ema + self.volume_alpha * current_volume

        ratio = current_volume / (self.volume_ema + 1e-10)
        passed = ratio >= self.min_volume_ratio
        return {"passed": passed, "ratio": ratio}

    def _adaptive_volatility_filter(self, data: Dict[str, pd.DataFrame]) -> Dict:
        df = data.get("1m")
        if df is None or len(df) < 20:
            return {"passed": True}

        try:
            high_low = df['high'] - df['low']
            high_close = abs(df['high'] - df['close'].shift(1))
            low_close = abs(df['low'] - df['close'].shift(1))

            # ✅ Безопасный concat с проверкой на пустые данные
            if high_low.empty or high_close.empty or low_close.empty:
                return {"passed": True}

            true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            current_atr = true_range.tail(20).mean()

            if self.atr_ema == 0.0:
                self.atr_ema = current_atr
            else:
                self.atr_ema = (1 - self.atr_alpha) * self.atr_ema + self.atr_alpha * current_atr

            ratio = current_atr / (self.atr_ema + 1e-10)
            passed = ratio <= self.max_volatility_ratio
            return {"passed": passed, "ratio": ratio}

        except Exception as e:
            self.logger.warning(f"Volatility filter error: {e}")
            return {"passed": True}

    def _check_trading_conditions(self) -> bool:
        with self._daily_stats_lock:
            if self.daily_stats['trades_count'] >= self.max_daily_trades:
                return False

            # ✅ ИСПРАВЛЕНО: Используем фактический убыток (отрицательное значение)
            daily_pnl = self.daily_stats['pnl']
            if daily_pnl < 0:  # Только если есть убыток
                daily_loss_pct = abs(daily_pnl) / self.account_balance
                if daily_loss_pct >= self.config.get('max_daily_loss', 0.02):
                    return False
            return True

    def _is_trading_session_now(self) -> bool:
        lo, hi = normalize_trading_hours({"time_window_hours": self.config.get("time_window_hours", (6, 22))})
        return lo <= datetime.now().hour <= hi

    def _validate_market_data_quality(self, market_data: Dict[str, pd.DataFrame]) -> bool:
        for df in market_data.values():
            if df.empty or df['close'].iloc[-1] <= 0 or df['close'].isna().iloc[-1]:
                return False
        return True

    def _calculate_atr(self, data: pd.DataFrame, period: int = 14) -> float:
        if len(data) < period:
            return data['high'].iloc[-1] - data['low'].iloc[-1]
        high_low = data['high'] - data['low']
        high_close = abs(data['high'] - data['close'].shift(1))
        low_close = abs(data['low'] - data['close'].shift(1))
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return true_range.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    def update_performance(self, trade_result: Dict[str, Any]) -> None:
        """
        Обновляет статистику производительности на основе результата сделки.
        Соответствует TradingSystemInterface.
        """
        try:
            # ✅ Принимаем Dict[str, Any] как требует интерфейс
            pnl = float(trade_result.get('pnl', 0.0))
            is_win = pnl > 0
            today = datetime.now().date()

            with self._daily_stats_lock:
                if self._last_reset_date != today:
                    if self._last_reset_date is not None:
                        self.daily_stats_history[str(self._last_reset_date)] = {
                            **self.daily_stats,
                            'win_rate': self.daily_stats['wins'] / max(1, self.daily_stats['trades_count']),
                            'avg_pnl_per_trade': self.daily_stats['pnl'] / max(1, self.daily_stats['trades_count']),
                            'date': str(self._last_reset_date)
                        }
                        if len(self.daily_stats_history) > 90:
                            oldest_date = min(self.daily_stats_history.keys())
                            del self.daily_stats_history[oldest_date]
                    self.daily_stats = {'trades_count': 0, 'pnl': 0.0, 'wins': 0, 'losses': 0}
                    self._last_reset_date = today

                self.daily_stats['trades_count'] += 1
                self.daily_stats['pnl'] += pnl
                if is_win:
                    self.daily_stats['wins'] += 1
                else:
                    self.daily_stats['losses'] += 1

            self.performance_tracker['total_trades'] += 1
            if is_win:
                self.performance_tracker['winning_trades'] += 1
            self.performance_tracker['total_pnl'] += pnl
            self.account_balance += pnl

            # ✅ Безопасный вызов risk_manager
            if hasattr(self.risk_manager, 'update_daily_pnl'):
                self.risk_manager.update_daily_pnl(pnl)

        except Exception as e:
            self.logger.error(f"Critical error in update_performance: {e}", exc_info=True)

    async def analyze_and_trade(self, market_data: Dict[Timeframe, pd.DataFrame]):
        try:
            required_tfs: list[Timeframe] = ['1m', '5m']
            for tf in required_tfs:
                if tf not in market_data or market_data[tf] is None or market_data[tf].empty:
                    self.logger.warning(f"Missing or empty timeframe: {tf}")
                    return None

            if not self._is_trading_session_now():
                return None

            if not self._check_trading_conditions():
                return None

            if not self._validate_market_data_quality(market_data):
                return None

            await self._update_market_regime(market_data)

            # Основной анализ — напрямую через confirmator
            signal = await self.three_level_confirmator.analyze(market_data)
            if not signal["ok"]:
                return None

            # Применяем фильтры качества
            filtered_signal = await self._apply_quality_filters(signal, market_data)
            if not filtered_signal["ok"]:
                return None

            # Расчёт ATR
            atr = self._calculate_atr(market_data["1m"])
            if atr <= 0:
                return None

            # Текущая цена
            price = float(market_data["1m"]["close"].iloc[-1])
            if price <= 0:
                return None

            # Размер позиции
            size = self.risk_manager.calculate_position_size(
                signal=filtered_signal,
                current_price=price,
                atr=atr,
                account_balance=self.account_balance
            )
            if size <= 0:
                return None

            # ✅ ИСПРАВЛЕНО: Убраны лишние отступы - этот код должен выполняться после проверки size

            # Конвертация direction: детекторы возвращают int, TradeSignalIQTS ожидает string
            direction_int = int(filtered_signal["direction"])
            direction_map = {1: "BUY", -1: "SELL", 0: "FLAT"}


            # Если FLAT - не торгуем
            if direction_int == 0:
                self.logger.debug("Direction is FLAT, skipping trade")
                return None

            # Безопасное получение regime с приведением типа
            current_regime = getattr(self.current_regime, 'regime', 'uncertain')
            regime_typed: RegimeType = current_regime if current_regime in [
                "strong_uptrend", "weak_uptrend", "strong_downtrend", "weak_downtrend", "sideways", "uncertain"
            ] else "uncertain"

            # Расчет стопов - используем string direction для risk_manager
            sl, tp = self.risk_manager.calculate_dynamic_stops(
                entry_price=price,
                direction=direction_int,
                atr=atr,
                regime_ctx={
                    "atr": float(atr),
                    "volatility_regime": getattr(self.current_regime, 'volatility_level', 0.02),
                    "regime": regime_typed,
                    "regime_confidence": getattr(self.current_regime, 'confidence', 0.0)
                }
            )
            if sl <= 0 or tp <= 0:
                return None

            # Финальный сигнал
            risk_reward_ratio = abs(tp - price) / max(1e-12, abs(price - sl))

            out: TradeSignalIQTS = {
                "direction": cast(Literal[1, -1, 0], direction_int),
                "entry_price": price,
                "position_size": float(size),
                "stop_loss": float(sl),
                "take_profit": float(tp),
                "confidence": float(filtered_signal["confidence"]),
                "regime": regime_typed,  # ✅ Теперь правильный тип
                "metadata": {
                    "atr": float(atr),
                    "risk_reward_ratio": risk_reward_ratio,
                    "regime": regime_typed,  # ✅ И здесь тоже
                    "regime_confidence": float(getattr(self.current_regime, 'confidence', 0.0)),
                    "signal_source": "hierarchical_quality",
                    "extra": {
                        "entry_time": datetime.now().isoformat(),
                        "correlation_id": filtered_signal.get("metadata", {}).get("extra", {}).get("correlation_id",
                                                                                                   ""),
                        "entry_quality_score": filtered_signal.get("metadata", {}).get("extra", {}).get(
                            "entry_quality_score", 0.0),
                        "trend_quality_score": filtered_signal.get("metadata", {}).get("extra", {}).get(
                            "trend_quality_score", 0.0),
                        "global_quality_score": filtered_signal.get("metadata", {}).get("extra", {}).get(
                            "global_quality_score", 0.0),
                        "entry_reason": filtered_signal.get("metadata", {}).get("extra", {}).get("entry_reason", ""),
                        "trend_reason": filtered_signal.get("metadata", {}).get("extra", {}).get("trend_reason", ""),
                        "global_reason": filtered_signal.get("metadata", {}).get("extra", {}).get("global_reason", ""),
                    }
                }
            }

            # Увеличиваем счётчик сделок
            self.trades_today += 1
            self.daily_stats['trades_count'] += 1

            return out

        except Exception as e:
            self.logger.error(f"Error in analyze_and_trade: {e}", exc_info=True)
            return None

    async def _update_market_regime(self, market_data: Dict[Timeframe, pd.DataFrame]) -> None:
        """✅ ИСПРАВЛЕНО: Обновление рыночного режима с правильными типами"""
        try:
            # Базовая логика определения режима
            df_5m = market_data.get("5m")
            if df_5m is not None and len(df_5m) > 20:
                prices = df_5m['close'].tail(20)
                sma_20 = prices.mean()
                current_price = prices.iloc[-1]
                price_change_pct = (current_price - sma_20) / sma_20

                # ✅ Используем Literal типы
                if price_change_pct > 0.02:
                    regime: RegimeType = "strong_uptrend"
                    confidence = min(0.9, abs(price_change_pct) * 10)
                elif price_change_pct > 0.005:
                    regime: RegimeType = "weak_uptrend"
                    confidence = min(0.7, abs(price_change_pct) * 20)
                elif price_change_pct < -0.02:
                    regime: RegimeType = "strong_downtrend"
                    confidence = min(0.9, abs(price_change_pct) * 10)
                elif price_change_pct < -0.005:
                    regime: RegimeType = "weak_downtrend"
                    confidence = min(0.7, abs(price_change_pct) * 20)
                else:
                    regime: RegimeType = "sideways"
                    confidence = 0.5

                # Расчет волатильности
                volatility = prices.pct_change().std()

                # Определение профиля объема
                volume_mean = df_5m['volume'].tail(20).mean()
                current_volume = df_5m['volume'].iloc[-1]
                volume_ratio = current_volume / volume_mean if volume_mean > 0 else 1.0

                if volume_ratio > 1.5:
                    volume_profile: VolumeProfileType = "high"
                elif volume_ratio < 0.7:
                    volume_profile: VolumeProfileType = "low"
                else:
                    volume_profile: VolumeProfileType = "normal"

                self.current_regime = MarketRegime(
                    regime=regime,
                    confidence=float(confidence),
                    volatility_level=float(volatility),
                    trend_strength=float(abs(price_change_pct)),
                    volume_profile=volume_profile
                )
            else:
                # Режим по умолчанию с правильными типами
                self.current_regime = MarketRegime(
                    regime="uncertain",
                    confidence=0.0,
                    volatility_level=0.02,
                    trend_strength=0.0,
                    volume_profile="normal"
                )

        except Exception as e:
            self.logger.warning(f"Error updating market regime: {e}")
            self.current_regime = MarketRegime(
                regime="uncertain",
                confidence=0.0,
                volatility_level=0.02,
                trend_strength=0.0,
                volume_profile="normal"
            )

    def get_system_status(self) -> SystemStatus:
        """✅ ИСПРАВЛЕНО: Возвращаем SystemStatus как требует интерфейс"""
        total_trades = self.performance_tracker["total_trades"]
        win_rate = self.performance_tracker["winning_trades"] / max(1, total_trades)

        # ✅ Безопасное получение regime с приведением типа
        current_regime = getattr(self.current_regime, 'regime', 'uncertain')
        regime_typed: RegimeType = current_regime if current_regime in [
            "strong_uptrend", "weak_uptrend", "strong_downtrend", "weak_downtrend", "sideways", "uncertain"
        ] else "uncertain"

        # ✅ Создаем SystemStatus с правильными полями
        from iqts_standards import SystemStatus

        return SystemStatus(
            current_regime=regime_typed,
            regime_confidence=float(getattr(self.current_regime, 'confidence', 0.0)),
            trades_today=int(self.trades_today),
            max_daily_trades=int(self.max_daily_trades),
            total_trades=int(total_trades),
            win_rate=float(win_rate),
            total_pnl=float(self.performance_tracker["total_pnl"])
        )

    async def generate_signal(self, market_data: Dict[str, pd.DataFrame]) -> Optional[Dict]:
        """Генерирует торговый сигнал на основе анализа рынка."""
        try:
            # ✅ ЗАЩИТА 1: Инициализация кэша если не существует
            if not hasattr(self, '_cached_global_signal') or self._cached_global_signal is None:
                self._cached_global_signal = {}
                self.logger.warning("_cached_global_signal was not initialized, creating empty dict")

            # ✅ ДИАГНОСТИКА: Проверяем структуру market_data ПЕРЕД вызовом confirmator
            symbol = self._extract_symbol_from_data(market_data)

            self.logger.info(f"📊 generate_signal diagnostic for {symbol}:")
            for tf, df in market_data.items():
                self.logger.info(
                    f"  {tf}: type={type(df).__name__}, shape={df.shape if hasattr(df, 'shape') else 'N/A'}")
                if hasattr(df, 'index'):
                    self.logger.info(f"    Index type: {type(df.index).__name__}")
                    if hasattr(df.index, 'dtype'):
                        self.logger.info(f"    Index dtype: {df.index.dtype}")
                if hasattr(df, 'columns'):
                    self.logger.info(f"    Columns count: {len(df.columns)}")
                    self.logger.info(f"    Has 'timestamp': {'timestamp' in df.columns}")
                    self.logger.info(f"    Has 'ts': {'ts' in df.columns}")

            # ✅ ШАГ 1: Вызываем анализ через confirmator
            result = await self.three_level_confirmator.analyze(cast(Dict, market_data))

            # ✅ ШАГ 3: Извлекаем данные из результата анализа
            metadata = result.get('metadata', {})

            # Проверяем есть ли глобальный сигнал в metadata
            global_direction = metadata.get('global_direction')
            global_confidence = metadata.get('global_confidence', 0.0)
            trend_direction = metadata.get('trend_direction', 0)
            trend_confidence = metadata.get('trend_confidence', 0.0)

            # ✅ ШАГ 4: Принимаем решение о кэшировании
            # КЭШИРУЕМ если:
            # 1. Глобальный сигнал НЕ FLAT (direction != 0)
            # 2. Уверенность достаточная
            should_cache = (
                    global_direction is not None and
                    global_direction != 0 and
                    global_confidence >= 0.6
            )

            # ✅ ВРЕМЕННАЯ ДИАГНОСТИКА
            print(f"\n💾 [CACHE DEBUG] symbol={symbol}, should_cache={should_cache}")
            print(f"   global_dir={global_direction}, global_conf={global_confidence:.2f}")
            print(f"   trend_dir={trend_direction}, trend_conf={trend_confidence:.2f}")
            print(f"   result['ok']={result.get('ok')}, reason={result.get('reason')}")

            if should_cache:
                cache_ts = get_current_timestamp_ms()
                cache_status = 'agreement' if result['ok'] else 'disagreement'

                print(f"   ✅ CREATING CACHE: status={cache_status}, timestamp={cache_ts}")

                self._cached_global_signal[symbol] = {
                    'timestamp': cache_ts,
                    'global_direction': global_direction,
                    'global_confidence': global_confidence,
                    'trend_direction': trend_direction,
                    'trend_confidence': trend_confidence,
                    'reason': result.get('reason', 'unknown'),
                    'status': cache_status,
                    'used': result['ok']
                }

                print(f"   📊 Cache dict: {self._cached_global_signal[symbol]}")
                print(f"   📊 Total cached symbols: {list(self._cached_global_signal.keys())}")

                self.logger.info(
                    f"💾 Cached 5m signal for {symbol}: "
                    f"global_dir={global_direction}, global_conf={global_confidence:.2f}, "
                    f"trend_dir={trend_direction}, trend_conf={trend_confidence:.2f}, "
                    f"status={cache_status}, used={result['ok']}"
                )
            else:
                print(f"   ⏭️ NOT caching (should_cache=False)")
                # Очищаем кэш если сигнал слабый или FLAT
                if symbol in self._cached_global_signal:
                    print(f"   🗑️ Clearing existing cache for {symbol}")
                    self.logger.info(f"🗑️ Clearing cache for {symbol} (weak or FLAT signal)")
                    del self._cached_global_signal[symbol]

            print()  # Пустая строка для читаемости

            # ✅ ШАГ 5: Если нет подтверждения - возвращаем None
            if not result['ok']:
                self.logger.info(f"No signal for {symbol}: {result.get('reason', 'unknown')}")
                return None

            # ✅ ШАГ 6: Проверяем торговые условия
            if not self._check_trading_conditions():
                self.logger.info(f"Trading conditions not met for {symbol}")
                return None

            # ✅ ШАГ 7: Формируем торговый сигнал
            # УПРОЩЕННАЯ ЛОГИКА: Только направление и уверенность
            # PositionManager сам рассчитает size, stop_loss, take_profit
            direction = int(result['direction'])
            confidence = result['confidence']

            # Получаем текущую цену для entry_price
            current_price = float(market_data['5m']['close'].iloc[-1])

            # Формируем минимальный сигнал для PositionManager
            signal = {
                'symbol': symbol,
                'direction': direction,
                'confidence': confidence,
                'entry_price': current_price,
                'regime': self.current_regime.regime if self.current_regime else 'uncertain',
                'metadata': result.get('metadata', {}),
                # PositionManager сам добавит: position_size, stop_loss, take_profit
            }

            self.logger.info(
                f"✅ Signal generated for {symbol}: dir={direction}, conf={confidence:.2f}, "
                f"entry={current_price:.2f}"
            )

            return signal

        except Exception as e:
            self.logger.error(f"Error generating signal: {e}", exc_info=True)
            return None

    def _extract_symbol_from_data(self, market_data: Dict[str, pd.DataFrame]) -> str:
        """Извлекает символ из market_data."""
        # Простой вариант: берем из 5m DataFrame
        if '5m' in market_data and not market_data['5m'].empty:
            if 'symbol' in market_data['5m'].columns:
                return str(market_data['5m']['symbol'].iloc[0])

        # Fallback: из конфига
        return self.config.get('symbol', 'ETHUSDT')

    async def check_cached_global_signal(self, symbol: str, market_data: Dict[str, pd.DataFrame]) -> Optional[
        Dict[str, Any]]:
        """
        Проверяет кэшированный 5m сигнал на согласованность с текущим 1m трендом.
        """
        # ✅ ВРЕМЕННАЯ ДИАГНОСТИКА
        print(f"\n🔍 [CACHE CHECK] check_cached_global_signal CALLED for {symbol}")
        print(f"   _cached_global_signal keys: {list(self._cached_global_signal.keys())}")
        print(f"   symbol in cache: {symbol in self._cached_global_signal}")

        self.logger.info(
            f"🔍 check_cached_global_signal called for {symbol} "
            f"(cache exists: {symbol in self._cached_global_signal})"
        )

        # Проверяем наличие кэша для символа
        if symbol not in self._cached_global_signal:
            print(f"   ❌ NO CACHE - returning None\n")
            self.logger.debug(f"⏭️ No cache for {symbol}")
            return None

        print(f"   ✅ CACHE FOUND!")
        print(f"   Cache data: {self._cached_global_signal[symbol]}")

        cached = self._cached_global_signal[symbol]
        cache_ts = cached.get('timestamp', 0)
        cache_age_ms = 0

        # Определяем возраст кэша
        if '1m' in market_data and not market_data['1m'].empty:
            df_1m = market_data['1m']

            # ✅ Безопасное извлечение timestamp
            if 'ts' in df_1m.columns:
                current_ts = int(df_1m['ts'].iloc[-1])
            elif hasattr(df_1m.index[-1], 'timestamp'):
                current_ts = int(df_1m.index[-1].timestamp() * 1000)
            elif isinstance(df_1m.index[-1], (int, float)):
                current_ts = int(df_1m.index[-1])
            else:
                current_ts = get_current_timestamp_ms()

            cache_age_ms = current_ts - cache_ts
        else:
            cache_age_ms = get_current_timestamp_ms() - cache_ts

        # Проверяем TTL кэша (5 минут = 300 000 ms)
        MAX_CACHE_AGE_MS = 300_000
        if cache_age_ms > MAX_CACHE_AGE_MS:
            self.logger.info(f"🗑️ Cache expired for {symbol} (age: {cache_age_ms / 1000:.0f}s)")
            del self._cached_global_signal[symbol]
            return None

        # ✅ Проверяем статус кэша
        cache_status = cached.get('status', 'unknown')
        was_used = cached.get('used', False)

        # Если сигнал уже был использован (agreement) - пропускаем проверку
        if was_used and cache_status == 'agreement':
            self.logger.debug(
                f"⏭️ Skipping cache check for {symbol} - signal already used "
                f"(status={cache_status}, used={was_used})"
            )
            return None

        # Проверяем только если был disagreement
        if cache_status != 'disagreement':
            self.logger.debug(
                f"⏭️ Cache status is not disagreement for {symbol} (status={cache_status})"
            )
            return None

        self.logger.info(
            f"🔍 Checking cached 5m signal for {symbol} "
            f"(age: {cache_age_ms / 1000:.0f}s, global_dir={cached.get('global_direction')})"
        )

        #  Используем полный analyze() конфирматора
        # Он сам сравнит глобальный и локальный тренды через _check_two_level_consistency
        try:
            self.logger.info(
                f"🔍 Rechecking agreement with full confirmator.analyze() "
                f"(cached global_dir={cached.get('global_direction')}, "
                f"cached global_conf={cached.get('global_confidence', 0.0):.2f})"
            )

            # Вызываем полный анализ через confirmator
            recheck_result = await self.three_level_confirmator.analyze(market_data)

            # Извлекаем данные из результата
            trend_direction = recheck_result.get('metadata', {}).get('trend_direction', 0)
            trend_confidence = recheck_result.get('metadata', {}).get('trend_confidence', 0.0)
            global_direction_current = recheck_result.get('metadata', {}).get('global_direction', 0)
            recheck_ok = recheck_result.get('ok', False)
            recheck_reason = recheck_result.get('reason', 'unknown')

            self.logger.info(
                f"📊 Cached 5m recheck result: "
                f"ok={recheck_ok}, "
                f"reason={recheck_reason}, "
                f"cached_global_dir={cached.get('global_direction')}, "
                f"current_global_dir={global_direction_current}, "
                f"trend_dir={trend_direction}, "
                f"trend_conf={trend_confidence:.2f}"
            )

            # Проверяем согласованность
            # ✅ ИСПРАВЛЕНО: Проверяем согласованность через результат confirmator.analyze()
            cached_global_direction = cached.get('global_direction', 0)

            # Если confirmator.analyze() вернул ok=True - значит согласие достигнуто!
            if recheck_ok and recheck_reason == 'two_level_confirmed':
                self.logger.info(
                    f"✅ AGREEMENT ACHIEVED! Confirmator returned ok=True "
                    f"(cached_global={cached_global_direction}, "
                    f"current_trend={trend_direction})"
                )

                # ✅ Помечаем кэш как использованный (но НЕ удаляем)
                self._cached_global_signal[symbol]['used'] = True
                self._cached_global_signal[symbol]['status'] = 'agreement'

                # Проверяем торговые условия
                if not self._check_trading_conditions():
                    self.logger.info(f"Trading conditions not met for {symbol}")
                    return None

                # Получаем текущую цену
                current_price = 0.0

                if '1m' in market_data and not market_data['1m'].empty:
                    df_1m = market_data['1m']

                    if 'close' in df_1m.columns:
                        current_price = float(df_1m['close'].iloc[-1])
                    else:
                        self.logger.info(f"No 'close' column in 1m data for {symbol}")
                        return None
                else:
                    self.logger.info(f"No 1m data available for {symbol}")
                    return None

                if current_price <= 0:
                    self.logger.info(f"Invalid current price for {symbol}: {current_price}")
                    return None

                # ✅ Используем комбинированную confidence из confirmator
                combined_confidence = recheck_result.get('confidence', cached.get('global_confidence', 0.0))

                # Формируем минимальный сигнал для PositionManager
                delayed_signal = {
                    'symbol': symbol,
                    'direction': recheck_result.get('direction', cached_global_direction),
                    'confidence': combined_confidence,
                    'entry_price': current_price,
                    'regime': self.current_regime.regime if self.current_regime else 'uncertain',
                    'cached_signal_used': True,
                    'cache_age_ms': cache_age_ms,
                    'metadata': {
                        **recheck_result.get('metadata', {}),
                        'delayed_entry': True,
                        'cache_timestamp': cache_ts,
                        'cached_global_direction': cached_global_direction,
                        'cached_global_confidence': cached.get('global_confidence', 0.0),
                        'recheck_reason': recheck_reason
                    }
                }

                self.logger.info(
                    f"🎯 Delayed signal formed: {symbol} "
                    f"dir={delayed_signal['direction']}, "
                    f"conf={combined_confidence:.2f}, "
                    f"entry={current_price:.2f}"
                )
                return delayed_signal

            # ✅ Если confirmator вернул ok=False - проверяем причину
            elif recheck_reason == 'direction_disagreement':
                self.logger.info(
                    f"⏳ Still disagreement: "
                    f"cached_global={cached_global_direction}, "
                    f"current_trend={trend_direction}, "
                    f"reason={recheck_reason}"
                )
                return None
            else:
                # Другие причины (weak_signals, insufficient_data и т.д.)
                self.logger.info(
                    f"⏭️ Cannot recheck: reason={recheck_reason}"
                )
                return None

        except Exception as e:
            self.logger.info(f"Error checking cached signal for {symbol}: {e}", exc_info=True)
            return None

    def get_performance_report(self) -> Dict:
        total_trades = self.performance_tracker['total_trades']
        if total_trades == 0:
            return {"message": "No trades yet"}
        win_rate = self.performance_tracker['winning_trades'] / total_trades
        avg_pnl = self.performance_tracker['total_pnl'] / total_trades
        return {
            'overall': {
                'total_trades': total_trades,
                'win_rate': win_rate,
                'total_pnl': self.performance_tracker['total_pnl'],
                'average_pnl_per_trade': avg_pnl
            },
            'daily': self.daily_stats.copy(),
            'daily_history': self.daily_stats_history.copy(),
            'by_regime': {},
            'signal_quality': {}
        }

    async def shutdown(self):
        self.logger.info("Shutting down ImprovedQualityTrendSystem...")
        final_report = self.get_performance_report()
        self.logger.info(f"Final performance report: {final_report}")
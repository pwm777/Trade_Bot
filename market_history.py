"""
market_history.py
Модуль для загрузки и подготовки исторических данных с Binance.
Включает загрузку данных, расчет индикаторов и управление разогревом.

Created: 2025-10-24 15:28:22 UTC
Author: pwm777

Note: Using timezone-aware datetime objects for UTC timestamps
"""
from __future__ import annotations
import logging
import asyncio
import aiohttp
from typing import Dict, List, Optional, Any, TypedDict
from collections import deque
from datetime import datetime, UTC
from market_data_utils import MarketDataUtils
import sys
from logging import StreamHandler, Formatter
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.ext.asyncio import create_async_engine
from tqdm import tqdm

def get_current_ms() -> int:
    """Возвращает текущее UTC время в миллисекундах"""
    return int(datetime.now(UTC).timestamp() * 1000)

class RetryConfig(TypedDict):
    max_retries: int
    base_delay: float
    max_delay: float

class BinanceDataFetcher:
    """Компонент для загрузки исторических данных с Binance Futures API"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.base_url = "https://fapi.binance.com/fapi/v1/klines"
        self.retry_config: RetryConfig = {
            'max_retries': 3,
            'base_delay': 1.0,
            'max_delay': 30.0
        }

    async def fetch_candles(
            self,
            symbol: str,
            interval: str,
            start_time: int,
            end_time: int,
            limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Загружает исторические свечи с Binance с обработкой ошибок и повторных попыток.
        Корректно обрабатывает случаи когда API возвращает меньше limit свечей.
        """
        all_candles = []
        current_start = start_time
        retry_count = 0

        while current_start < end_time and retry_count < self.retry_config['max_retries']:
            try:
                params = {
                    'symbol': symbol.upper(),
                    'interval': interval,
                    'startTime': current_start,
                    'endTime': end_time,
                    'limit': limit
                }

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                            self.base_url,
                            params=params,
                            timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:

                        if response.status == 200:
                            data = await response.json()

                            # ✅ Если данных нет - выходим
                            if not data:
                                self.logger.info(
                                    f"No more data from Binance for {symbol} {interval} "
                                    f"starting from {datetime.fromtimestamp(current_start / 1000, UTC)}"
                                )
                                break

                            candles = self._process_raw_candles(symbol, data)
                            all_candles.extend(candles)

                            received_count = len(data)
                            last_ts_open = int(data[-1][0])  # Open time последней свечи
                            last_ts_close = int(data[-1][6])  # Close time последней свечи

                            self.logger.debug(
                                f"Fetched {received_count} {interval} candles for {symbol}, "
                                f"last candle: [{datetime.fromtimestamp(last_ts_open / 1000, UTC)} - "
                                f"{datetime.fromtimestamp(last_ts_close / 1000, UTC)}]"
                            )

                            # ✅ КРИТИЧНО: Проверяем условия выхода
                            # 1. Если получили меньше лимита - значит достигнут конец данных
                            if received_count < limit:
                                self.logger.info(
                                    f"Received {received_count} < {limit} candles for {symbol} {interval}. "
                                    f"End of available data reached."
                                )
                                break

                            # 2. Если close time последней свечи >= end_time - достигнут конец запрошенного диапазона
                            if last_ts_close >= end_time:
                                self.logger.debug(
                                    f"Reached end_time for {symbol} {interval}: "
                                    f"last_ts_close={last_ts_close} >= end_time={end_time}"
                                )
                                break

                            # 3. ✅ КРИТИЧНО: Используем ts_close + 1 для следующего запроса
                            # Это гарантирует что мы начнем со следующего интервала, а не внутри текущего
                            next_start = last_ts_close + 1

                            # 4. Проверка на зацикливание
                            if next_start <= current_start:
                                self.logger.warning(
                                    f"Pagination stalled for {symbol} {interval}: "
                                    f"next_start={next_start} <= current_start={current_start}. Breaking loop."
                                )
                                break

                            # ✅ Обновляем current_start для следующей итерации
                            current_start = next_start
                            retry_count = 0  # Сбрасываем счетчик ошибок после успешного запроса

                            # Rate limiting
                            await asyncio.sleep(0.1)

                        elif response.status == 429:  # Rate limit
                            retry_count += 1
                            delay = min(
                                self.retry_config['base_delay'] * (2 ** retry_count),
                                self.retry_config['max_delay']
                            )
                            self.logger.warning(
                                f"Rate limit hit for {symbol}, waiting {delay:.1f}s "
                                f"(retry {retry_count}/{self.retry_config['max_retries']})"
                            )
                            await asyncio.sleep(delay)
                            continue

                        else:
                            error_text = await response.text()
                            raise Exception(f"API error {response.status}: {error_text}")

            except Exception as e:
                retry_count += 1
                self.logger.error(
                    f"Error fetching data for {symbol} {interval}: {str(e)} "
                    f"(retry {retry_count}/{self.retry_config['max_retries']})"
                )
                if retry_count >= self.retry_config['max_retries']:
                    self.logger.error(
                        f"Max retries reached for {symbol} {interval}. "
                        f"Returning {len(all_candles)} candles collected so far."
                    )
                    break

                # Exponential backoff
                delay = self.retry_config['base_delay'] * (2 ** retry_count)
                await asyncio.sleep(min(delay, self.retry_config['max_delay']))

        self.logger.info(
            f"Completed fetching {len(all_candles)} {interval} candles for {symbol}"
        )
        return all_candles

    def _process_raw_candles(self, symbol: str, data: List[List]) -> List[Dict[str, Any]]:
        """Преобразует сырые данные свечей в словари"""
        candles = []
        for item in data:
            candle = {
                "symbol": symbol,
                "ts": int(item[0]),  # Open time
                "ts_close": int(item[6]),  # Close time
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "quote": float(item[7]),
                "count": int(item[8]),
                "finalized": True,
            }
            candles.append(candle)
        return candles

class IndicatorWarmupManager:
    """Управление разогревом индикаторов для разных таймфреймов"""

    def __init__(self, market_data_utils: Any, logger: Optional[logging.Logger] = None):
        self.market_data_utils = market_data_utils
        self.logger = logger or logging.getLogger(__name__)
        self.warmup_config = {
            '1m': {'min_bars': 28, 'lookback': 100},
            '5m': {'min_bars': 28, 'lookback': 100}
        }

    async def warmup_5m_indicators(self, symbol: str, candles: List[Dict]) -> bool:
        if len(candles) < self.warmup_config['5m']['min_bars']:
            self.logger.warning(f"Insufficient 5m candles for {symbol}: {len(candles)}")
            return False

        try:
            # ✅ ИСПРАВЛЕНИЕ: compute_5m_features_bulk сам сохранит свечи С индикаторами
            # Не нужно предварительно сохранять сырые данные

            # Используем bulk calculation (он сам вызовет upsert_candles_5m с индикаторами)
            processed_count = await self.market_data_utils.compute_5m_features_bulk(symbol, candles)

            return processed_count > 0

        except Exception as e:
            self.logger.error(f"Error warming up 5m indicators for {symbol}: {e}", exc_info=True)
            return False

    async def restore_indicator_state(self, symbol: str, interval: str) -> Optional[Dict]:
        """
        Восстанавливает последнее состояние индикаторов из БД.

        Args:
            symbol: торговый символ
            interval: '1m' или '5m'

        Returns:
            dict с последним состоянием индикаторов или None
        """
        try:
            # Получаем последнюю свечу с рассчитанными индикаторами
            if interval == '1m':
                last_candles = await self.market_data_utils.read_candles_1m(symbol, last_n=1)
            elif interval == '5m':
                last_candles = await self.market_data_utils.read_candles_5m(symbol, last_n=1)
            else:
                return None

            if not last_candles:
                return None

            last_candle = last_candles[0]

            # Проверяем что индикаторы рассчитаны
            required_fields = {
                '1m': ['ema3', 'ema7', 'ema9', 'cusum_zscore'],
                '5m': ['price_change_5', 'cmo_14', 'adx_14']
            }

            has_indicators = all(
                last_candle.get(field) is not None
                for field in required_fields.get(interval, [])
            )

            if has_indicators:
                self.logger.info(
                    f"Restored indicator state for {symbol} {interval} from ts={last_candle['ts']}"
                )
                return last_candle
            else:
                self.logger.warning(
                    f"Last candle for {symbol} {interval} has no indicators"
                )
                return None

        except Exception as e:
            self.logger.error(f"Error restoring state for {symbol} {interval}: {e}")
            return None

    async def warmup_1m_indicators(self, symbol: str, candles: List[Dict]) -> bool:
        """
        Разогрев индикаторов для 1-минутных свечей
        ✅ ИСПРАВЛЕНИЯ:
        1. Проверка конфигов
        2. Логирование результатов
        """
        min_bars = self.warmup_config['1m']['min_bars']
        if len(candles) < min_bars:
            self.logger.warning(
                f"Insufficient 1m candles for {symbol}: {len(candles)} < {min_bars}"
            )
            return False

        try:
            # Сохраняем свечи
            await self.market_data_utils.upsert_candles_1m(symbol, candles)

            # Рассчитываем CUSUM (используется конфиг из market_data_utils)
            result = await self.market_data_utils.warmup_1m_indicators_and_cusum(symbol, candles)

            if result.get("ok"):
                self.logger.info(
                    f"✅ 1m warmup successful for {symbol}: "
                    f"z={result.get('z', 0.0):.3f}, state={result.get('state', 0)}"
                )
                return True
            else:
                self.logger.warning(
                    f"⚠️ 1m warmup not ready for {symbol}: {result.get('reason', 'unknown')}"
                )
                return False

        except Exception as e:
            self.logger.error(f"Error warming up 1m indicators for {symbol}: {e}", exc_info=True)
            return False

    async def _get_hist_1m_candles(self, symbol: str, start_ts: int, end_ts: int) -> dict:
        """
        Получение исторических 1m свечей БЕЗ фазы для маппинга на 5m свечи.
        Специально для данных загруженных с Binance (все свечи начинаются в :00).

        Args:
            symbol: торговый символ
            start_ts: начало диапазона
            end_ts: конец диапазона

        Returns:
            словарь {ts_5m: {open, high, low, close, ema7}} где ts_5m БЕЗ фазы
        """
        try:
            # Получаем 1m свечи из БД
            candles_1m = await self.market_data_utils.read_candles_1m(
                symbol, start_ts=start_ts, end_ts=end_ts
            )

            if not candles_1m:
                self.logger.warning(f"No 1m candles found for {symbol} in range {start_ts}-{end_ts}")
                return {}

            # Создаем маппинг: для каждой 5m свечи БЕЗ фазы находим последнюю 1m свечу
            mapping = {}
            FIVE_M_MS = 300_000

            for candle in candles_1m:
                ts_1m = int(candle['ts'])

                # Для исторических данных БЕЗ фазы: простое выравнивание к началу 5m
                # 10:44:00 -> 10:40:00, 10:45:00 -> 10:45:00, etc.
                ts_5m = (ts_1m // FIVE_M_MS) * FIVE_M_MS

                # Сохраняем последнюю 1m свечу для каждого 5m периода
                if ts_5m not in mapping or ts_1m > mapping[ts_5m]['ts_1m']:
                    ema7_val = float(candle.get('ema7')) if candle.get('ema7') is not None else None
                    mapping[ts_5m] = {
                        'ts_1m': ts_1m,
                        'open': float(candle['open']),
                        'high': float(candle['high']),
                        'low': float(candle['low']),
                        'close': float(candle['close']),
                        'ema7': ema7_val
                    }

            self.logger.debug(
                f"Mapped {len(mapping)} historical 5m periods to 1m candles for {symbol}"
            )
            return mapping

        except Exception as e:
            self.logger.error(f"Failed to get historical 1m candles for {symbol}: {e}", exc_info=True)
            return {}

    async def _compute_5m_features_historical(
            self,
            symbol: str,
            candles: List[Dict],
            candles_1m_map: dict
    ) -> int:
        """
        Расчет 5m индикаторов для исторических данных с использованием
        предварительно загруженного маппинга 1m свечей.

        Args:
            symbol: торговый символ
            candles: список 5m свечей БЕЗ фазы
            candles_1m_map: маппинг {ts_5m: 1m_candle} БЕЗ фазы

        Returns:
            количество обработанных свечей
        """
        try:
            if not candles:
                return 0

            # Добавляем паттерны 1m к каждой 5m свече
            enriched_candles = []

            for candle_5m in candles:
                ts_5m = int(candle_5m['ts'])
                candle_1m = candles_1m_map.get(ts_5m)

                # Создаем копию свечи
                enriched = dict(candle_5m)

                if candle_1m:
                    # Рассчитываем паттерны 1m
                    try:
                        pattern, body_ratio, close_pos = self.market_data_utils._pattern_features_1m(
                            candle_1m['open'],
                            candle_1m['high'],
                            candle_1m['low'],
                            candle_1m['close'],
                            candle_1m['ema7']
                        )
                        enriched['is_trend_pattern_1m'] = pattern
                        enriched['body_to_range_ratio_1m'] = body_ratio
                        enriched['close_position_in_range_1m'] = close_pos
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to calculate 1m patterns for {symbol}@{ts_5m}: {e}"
                        )
                        enriched['is_trend_pattern_1m'] = 0
                        enriched['body_to_range_ratio_1m'] = 0.0
                        enriched['close_position_in_range_1m'] = 0.5
                else:
                    # Нет соответствующей 1m свечи - используем fallback
                    enriched['is_trend_pattern_1m'] = 0
                    enriched['body_to_range_ratio_1m'] = 0.0
                    enriched['close_position_in_range_1m'] = 0.5

                enriched_candles.append(enriched)

            # Теперь вызываем стандартный метод расчета индикаторов
            # который уже имеет паттерны 1m в данных
            await self.market_data_utils.compute_5m_features_bulk(symbol, enriched_candles)

            return len(enriched_candles)

        except Exception as e:
            self.logger.error(
                f"Error computing historical 5m features for {symbol}: {e}",
                exc_info=True
            )
            return 0


class MarketHistoryManager:
    """Основной класс управления историческими данными"""

    def __init__(self, engine: AsyncEngine, market_data_utils: Any, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.market_data_utils = market_data_utils
        self.binance_fetcher = BinanceDataFetcher(logger)
        self.warmup_manager = IndicatorWarmupManager(market_data_utils, logger)

        self._buffers = {
            '1m': {},
            '5m': {}
        }

        # Добавляем timestamp создания для отслеживания
        self.created_at = datetime.now(UTC)
        self.logger.info(f"MarketHistoryManager initialized at {self.created_at.isoformat()}")

    def _normalize_symbol(self, symbol: str) -> str:
        """Приводит символ к формату Binance"""
        return symbol.replace('-', '').replace('_', '').upper()

    def get_buffer_stats(self) -> Dict[str, Dict[str, int]]:
        """Возвращает статистику буферов (размеры)"""
        return {
            tf: {sym: len(buf) for sym, buf in buffers.items()}
            for tf, buffers in self._buffers.items()
        }

    async def close(self) -> None:
        """
        Освобождает ресурсы перед закрытием менеджера.

        Выполняет:
        - Очистку буферов для всех таймфреймов
        - Закрытие соединений с Binance
        - Логирование процесса закрытия
        """
        try:
            # Очищаем буферы
            for timeframe in self._buffers:
                self._buffers[timeframe].clear()

            # Сбрасываем основные буферы
            self._buffers = {
                '1m': {},
                '5m': {}
            }

            # Записываем метрики использования в лог
            uptime = (datetime.now(UTC) - self.created_at).total_seconds()
            self.logger.info(
                f"MarketHistoryManager closing after {uptime:.1f}s uptime "
                f"(created at {self.created_at.isoformat()})"
            )

            # Закрываем Binance fetcher если есть метод close
            if hasattr(self.binance_fetcher, 'close'):
                await self.binance_fetcher.close()

            self.logger.info("MarketHistoryManager closed successfully")

        except Exception as e:
            self.logger.error(f"Error while closing MarketHistoryManager: {e}")
            raise  # Пробрасываем ошибку выше для обработки в BotLifecycleManager


    async def load_history(
            self,
            symbols: List[str],
            days_back: int = 1,
            check_existing: bool = True,
            warmup_config: Optional[Dict[str, Dict[str, int]]] = None
    ) -> Dict[str, Dict[str, int]]:
        """
        Загружает историю для всех таймфреймов и выполняет разогрев индикаторов

        Args:
            symbols: Список символов в любом формате (будут нормализованы)
            days_back: Количество дней истории для загрузки (ТЕПЕРЬ РЕАЛЬНО ИСПОЛЬЗУЕТСЯ)
            check_existing: Проверять ли существующие данные перед загрузкой
            warmup_config: Опциональная конфигурация разогрева. Если не указана — используется self.warmup_manager.warmup_config.

        Returns:
            Dict[str, Dict[str, int]]: {symbol: {'1m': count, '5m': count}}
        """
        # ✅ Используем переданную конфигурацию, если есть
        config_to_use = warmup_config or self.warmup_manager.warmup_config
        results = {}
        end_time = get_current_ms()

        # ✅ ТЕПЕРЬ ИСПОЛЬЗУЕМ days_back для расчета start_time
        start_time = end_time - (days_back * 24 * 60 * 60 * 1000)

        self.logger.info(
            f"Starting history load for {len(symbols)} symbols: "
            f"loading data from {datetime.fromtimestamp(start_time / 1000, UTC)} to now"
        )

        for symbol in symbols:
            symbol = self._normalize_symbol(symbol)
            results[symbol] = {'1m': 0, '5m': 0}
            candles_1m = None

            try:
                intervals = [
                    ('1m', self.warmup_manager.warmup_1m_indicators, 60_000),
                    ('5m', self.warmup_manager.warmup_5m_indicators, 300_000)
                ]

                for interval, warmup_func, interval_ms in intervals:
                    last_state = await self.warmup_manager.restore_indicator_state(symbol, interval)

                    if check_existing:
                        existing = await self._check_existing_data(symbol, interval, start_time, end_time,
                                                                   warmup_config=config_to_use)
                        min_required = config_to_use[interval]['min_bars']
                        if existing and len(existing) >= min_required:
                            # ✅ НОВОЕ: Проверка разрыва между последней свечой и текущим временем
                            last_ts = existing[-1]['ts']
                            current_ts = get_current_ms()
                            gap_ms = current_ts - last_ts

                            # Разрыв > 1 интервала требует догрузки
                            if gap_ms > interval_ms:
                                gap_minutes = gap_ms / 60_000
                                self.logger.warning(
                                    f"Gap detected for {symbol} {interval}: "
                                    f"{gap_minutes:.1f} minutes since last candle. Fetching missing data..."
                                )

                                try:
                                    # Догружаем только недостающий диапазон
                                    gap_start = last_ts + interval_ms
                                    gap_candles = await self.binance_fetcher.fetch_candles(
                                        symbol, interval, gap_start, current_ts
                                    )

                                    if gap_candles:
                                        self.logger.info(
                                            f"Fetched {len(gap_candles)} missing {interval} candles for {symbol}")

                                        # ✅ НОВОЕ: Для 5m передаём полный контекст (existing + gap)
                                        if interval == '5m':
                                            all_candles_for_warmup = existing + gap_candles
                                            self.logger.info(
                                                f"Warming up 5m with full context: {len(all_candles_for_warmup)} candles")
                                            success = await warmup_func(symbol, all_candles_for_warmup)
                                        else:
                                            # Для 1m можно обрабатывать только gap (окно меньше)
                                            success = await warmup_func(symbol, gap_candles)

                                        if success:
                                            # Объединяем для буфера
                                            all_candles = existing + gap_candles
                                            self._buffers[interval][symbol] = deque(
                                                all_candles[-config_to_use[interval]['lookback']:],
                                                maxlen=config_to_use[interval]['lookback']
                                            )
                                            results[symbol][interval] = len(all_candles)
                                            self.logger.info(
                                                f"Updated {symbol} {interval}: {len(existing)} existing + {len(gap_candles)} new = {len(all_candles)} total")
                                            if interval == '1m':
                                                candles_1m = all_candles
                                            continue
                                        else:
                                            self.logger.error(f"Warmup failed for gap candles {symbol} {interval}")

                                except Exception as e:
                                    self.logger.error(f"Error fetching gap data for {symbol} {interval}: {e}")
                                    # Продолжаем с существующими данными

                            # Нет разрыва или разрыв < 1 интервала - используем существующие данные
                            self.logger.info(
                                f"Using existing {interval} data for {symbol}: {len(existing)} candles"
                            )
                            results[symbol][interval] = len(existing)
                            if interval == '1m':
                                candles_1m = existing
                            continue

                    # ✅ Шаг 2: Загружаем новые данные с Binance с пагинацией
                    candles = []
                    current_start = start_time

                    while current_start < end_time:
                        batch = await self.binance_fetcher.fetch_candles(
                            symbol, interval, current_start, end_time
                        )
                        if not batch:
                            break  # Больше нет данных или ошибка
                        candles.extend(batch)

                        # Переход к следующему интервалу
                        last_ts_close = batch[-1]['ts_close']
                        current_start = last_ts_close + 1

                        # Защита от бесконечного цикла
                        if len(batch) < 1500:
                            break

                    if not candles:
                        self.logger.warning(f"No {interval} data received for {symbol}")
                        continue


                    if interval == '1m':
                        candles_1m = candles

                    # ✅ Шаг 3: Проверяем непрерывность с последним состоянием
                    if last_state:
                        last_ts = last_state['ts']
                        first_new_ts = candles[0]['ts']
                        gap_ms = first_new_ts - last_ts

                        if gap_ms > 0 and gap_ms > interval_ms * 2:  # Есть пропуск
                            self.logger.warning(
                                f"Gap detected for {symbol} {interval}: "
                                f"{gap_ms / 60_000:.1f} minutes between last state and new data"
                            )

                            # Здесь можно добавить логику загрузки из БД или Binance для заполнения гэпа
                            # Пока пропускаем, чтобы не усложнять

                    # ✅ Шаг 4: Разогрев индикаторов
                    success = await warmup_func(symbol, candles)
                    if success:
                        self._buffers[interval][symbol] = deque(
                            candles[-config_to_use[interval]['lookback']:],
                            maxlen=config_to_use[interval]['lookback']
                        )
                        results[symbol][interval] = len(candles)
                        self.logger.info(f"Loaded and warmed up {len(candles)} {interval} candles for {symbol}")
                    else:
                        self.logger.error(f"Warmup failed for {symbol} {interval}")


            except Exception as e:
                self.logger.error(f"Error loading history for {symbol}: {e}", exc_info=True)
                continue

        self.logger.info(
            f"History load completed. Results: "
            f"{sum(r['1m'] for r in results.values())} 1m, "
            f"{sum(r['5m'] for r in results.values())} 5m "
        )
        return results

    async def _check_existing_data(
            self,
            symbol: str,
            interval: str,
            start_time: int,
            end_time: Optional[int] = None,
            warmup_config: Optional[Dict[str, Dict[str, int]]] = None
    ) -> Optional[List[Dict]]:
        """
        Проверяет наличие данных в БД с индикаторами.
        Загружает только нужное количество последних свечей.

        Args:
            symbol: Нормализованный символ
            interval: Интервал ('1m' или '5m')
            start_time: Начальное время в миллисекундах
            end_time: Конечное время (если None - текущее время)

        Returns:
            Optional[List[Dict]]: Данные с индикаторами или None
        """
        try:
            if end_time is None:
                end_time = get_current_ms()

            start_dt = datetime.fromtimestamp(start_time / 1000, UTC)
            end_dt = datetime.fromtimestamp(end_time / 1000, UTC)

            self.logger.debug(
                f"Checking existing {interval} data for {symbol} "
                f"from {start_dt.isoformat()} to {end_dt.isoformat()}"
            )

            # ✅ Загружаем данные из БД с правильными границами
            if interval == '1m':
                existing = await self.market_data_utils.read_candles_1m(
                    symbol, start_ts=start_time, end_ts=end_time
                )
            elif interval == '5m':
                existing = await self.market_data_utils.read_candles_5m(
                    symbol, start_ts=start_time, end_ts=end_time
                )
            else:
                return None

            if not existing:
                self.logger.debug(f"No existing {interval} data for {symbol}")
                return None

            # ✅ Проверяем минимальное количество свечей
            min_required = (warmup_config or self.warmup_manager.warmup_config)[interval]['min_bars']
            if len(existing) < min_required:
                self.logger.warning(
                    f"Existing {interval} data for {symbol} has only {len(existing)} bars, "
                    f"need at least {min_required}. Will reload."
                )
                return None

            # ✅ Проверяем наличие индикаторов (выборочно, последние 5 свечей)
            required_fields = {
                 '1m': ['ema3', 'ema7', 'ema9', 'cusum_zscore'],
                '5m': ['price_change_5', 'cmo_14', 'adx_14']
            }

            fields_to_check = required_fields.get(interval, [])
            sample = existing[-5:]  # Проверяем последние 5 свечей

            missing_indicators = sum(
                1 for candle in sample
                if any(candle.get(field) is None for field in fields_to_check)
            )

            if missing_indicators > 1:  # Более 1 свечи без индикаторов
                self.logger.warning(
                    f"Existing {interval} data for {symbol} has incomplete indicators "
                    f"({missing_indicators}/5 samples missing). Will recalculate."
                )
                return None

            # ✅ Проверяем непрерывность (только критичные пропуски)
            interval_ms = 60_000 if interval == '1m' else 300_000
            max_gap_allowed = interval_ms * 5  # Допускаем пропуск до 5 интервалов

            major_gaps = []
            for i in range(len(existing) - 1):
                gap = existing[i + 1]['ts'] - existing[i]['ts']
                if gap > max_gap_allowed:
                    major_gaps.append({
                        'index': i,
                        'gap_minutes': gap / 60_000
                    })

            if major_gaps:
                total_gap = sum(g['gap_minutes'] for g in major_gaps)
                self.logger.warning(
                    f"Found {len(major_gaps)} major gaps in {interval} data for {symbol} "
                    f"(total {total_gap:.1f} min). Will reload."
                )
                return None

            # ✅ Данные валидны
            self.logger.info(
                f"Using existing {interval} data for {symbol}: {len(existing)} bars "
                f"from {datetime.fromtimestamp(existing[0]['ts'] / 1000, UTC).isoformat()} "
                f"to {datetime.fromtimestamp(existing[-1]['ts'] / 1000, UTC).isoformat()}"
            )
            return existing

        except Exception as e:
            self.logger.error(
                f"Error checking existing data for {symbol} {interval}: {e}",
                exc_info=True
            )
            return None

    def get_buffer(self, symbol: str, timeframe: str) -> Optional[List[Dict]]:
        """
        Получает буфер для символа и таймфрейма

        Args:
            symbol: Символ в любом формате (будет нормализован)
            timeframe: Таймфрейм ('1m' или '5m')

        Returns:
            Optional[List[Dict]]: Список свечей из буфера или None если буфер пуст
        """
        symbol = self._normalize_symbol(symbol)
        buffer = self._buffers.get(timeframe, {}).get(symbol)
        return list(buffer) if buffer else None

    async def interactive_load(self):
        """Интерактивная загрузка истории с консоли с проверкой существующих данных и прогресс-баром."""
        import sys
        from tqdm import tqdm

        print("\n" + "=" * 60)
        print("HISTORICAL DATA LOADER")
        print("=" * 60)

        # Ввод символа
        symbol = input(f"\nEnter symbol [ETHUSDT]: ").strip().upper()
        if not symbol:
            symbol = "ETHUSDT"

        # Ввод количества дней
        while True:
            days_input = input(f"Enter number of days to load [90]: ").strip()
            try:
                days_back = int(days_input) if days_input else 90
                break
            except ValueError:
                print("Please enter a valid number.")

        # ✅ ПРОВЕРКА СУЩЕСТВУЮЩИХ ДАННЫХ
        print(f"\n🔍 Checking existing data for {symbol}...")

        existing_data = await self._check_existing_data_interactive(symbol, days_back)

        if existing_data['has_data']:
            print(f"\n📊 Existing data found:")
            print(
                f"   1m candles: {existing_data['1m_count']} (need {existing_data['1m_required']}) {'✅' if existing_data['has_1m_sufficient'] else '❌'}")
            print(
                f"   5m candles: {existing_data['5m_count']} (need {existing_data['5m_required']}) {'✅' if existing_data['has_5m_sufficient'] else '❌'}")

            if existing_data['is_sufficient']:
                print(f"\n✅ Sufficient data exists in database.")
                choice = input("Use existing data? (Y/n): ").strip().lower()
                if choice in ['', 'y', 'yes']:
                    # Используем существующие данные
                    print(f"Using existing data for {symbol}...")
                    try:
                        # ✅ ПРОГРЕСС-БАР ДЛЯ ПРОГРЕВА СУЩЕСТВУЮЩИХ ДАННЫХ
                        with tqdm(total=2, desc="🔥 Warming up indicators",
                                  bar_format='{l_bar}{bar:30}{r_bar}') as pbar:  # ИСПРАВИТЬ: total=2
                            await self._warmup_existing_data(symbol, days_back)
                            pbar.update(2)

                        print(f"\n✅ Success! Using existing data:")
                        print(
                            f"  {symbol}: {existing_data['1m_count']}x1m, {existing_data['5m_count']}x5m")
                        return
                    except Exception as e:
                        print(f"❌ Error warming up existing data: {e}")
                        # Продолжаем с загрузкой
            else:
                print(f"\n⚠️  Missing or insufficient data:")
                if not existing_data['has_1m_sufficient']:
                    print(f"   - 1m data insufficient")
                if not existing_data['has_5m_sufficient']:
                    print(f"   - 5m data insufficient")

        # Подтверждение загрузки
        print(f"\n📥 Loading {days_back} days of history for {symbol}...")
        confirm = input("Proceed with download? (y/N): ").strip().lower()
        if confirm != 'y':
            print("Cancelled.")
            return

        # ✅ ЗАПУСК ЗАГРУЗКИ С ПРОГРЕСС-БАРОМ
        try:
            # ✅ ОБЩИЙ ПРОГРЕСС-БАР ДЛЯ ВСЕГО ПРОЦЕССА
            with tqdm(total=4, desc="📊 Overall Progress", bar_format='{l_bar}{bar:30}{r_bar}') as main_pbar:
                main_pbar.set_description("🔍 Checking data...")
                existing_data = await self._check_existing_data_interactive(symbol, days_back)
                main_pbar.update(1)

                main_pbar.set_description("📥 Downloading data...")
                results = await self.load_history([symbol], days_back=days_back, check_existing=True)
                main_pbar.update(2)

                main_pbar.set_description("🔥 Calculating indicators...")
                # Даем время для отображения прогресса расчета в load_history
                await asyncio.sleep(0.5)
                main_pbar.update(1)

                main_pbar.set_description("✅ Complete!")

            print(f"\n🎉 SUCCESS! Loaded and processed:")
            for sym, counts in results.items():
                print(f"  {sym}: {counts['1m']}x1m, {counts['5m']}x5m")

            # ✅ ДОПОЛНИТЕЛЬНАЯ СТАТИСТИКА
            total_candles = sum(counts['1m'] + counts['5m'] for counts in results.values())
            print(f"  Total: {total_candles} candles processed")

        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            sys.exit(1)

    async def _find_last_processed_5m_candle(self, symbol: str) -> Optional[int]:
        """Находит последнюю 5m свечу с ВАЛИДНЫМИ индикаторами"""
        try:
            last_candles = await self.market_data_utils.read_candles_5m(symbol, last_n=100)

            for candle in reversed(last_candles):
                # ✅ ПРАВИЛЬНАЯ ПРОВЕРКА: не 0 и не None
                # ✅ ПРОВЕРКА КЛЮЧЕВЫХ ИНДИКАТОРОВ И CUSUM ПОЛЕЙ
                key_indicators = [
                    candle.get('price_change_5'),
                    candle.get('cmo_14'),
                    candle.get('adx_14'),
                    candle.get('cusum_1m_recent'),
                    candle.get('cusum_state'),
                    candle.get('cusum_zscore')
                ]

                is_valid = all(
                    indicator is not None
                    for indicator in key_indicators
                )

                if is_valid:
                    self.logger.info(f"📍 Found last VALID 5m candle: {candle['ts']}")
                    return candle['ts']

            self.logger.info("🆕 No VALID processed 5m candles found")
            return None
        except Exception as e:
            self.logger.error(f"Error finding last processed 5m: {e}")
            return None

    async def _warmup_existing_data(self, symbol: str, days_back: int):
        """Прогрев индикаторов для существующих данных с продолжением и прогресс-баром"""
        symbol_norm = self._normalize_symbol(symbol)
        end_time = get_current_ms()
        start_time = end_time - (days_back * 24 * 60 * 60 * 1000)

        # ✅ ИНИЦИАЛИЗАЦИЯ ПЕРЕМЕННЫХ
        candles_1m = []
        candles_5m = []
        needs_1m_recalc = False
        last_processed_5m = None

        with tqdm(total=2, desc="🔥 Warming up indicators", bar_format='{l_bar}{bar:30}{r_bar}') as main_pbar:
            # ✅ 1. ПРОГРЕВ 1m ДАННЫХ (если нужно)
            try:
                candles_1m = await self.market_data_utils.read_candles_1m(
                    symbol_norm, start_ts=start_time, end_ts=end_time
                )
                if candles_1m:
                    # ✅ ОПРЕДЕЛЯЕМ needs_1m_recalc ПЕРЕД ИСПОЛЬЗОВАНИЕМ
                    needs_1m_recalc = any(
                        candle.get('cusum_zscore') is None
                        for candle in candles_1m[-10:]
                    )

                    if needs_1m_recalc:
                        main_pbar.set_description("🔄 Recalculating 1m indicators...")
                        await self.warmup_manager.warmup_1m_indicators(symbol_norm, candles_1m)
                    else:
                        main_pbar.set_description("✅ 1m indicators ready")
                    main_pbar.update(1)
                    main_pbar.set_postfix(m=f"{len(candles_1m)} candles")
                else:
                    main_pbar.update(1)
                    main_pbar.set_postfix(m="No data")
            except Exception as e:
                main_pbar.update(1)
                main_pbar.set_postfix(m=f"Error: {str(e)[:20]}")

            # ✅ 2. УМНЫЙ ПРОГРЕВ 5m ДАННЫХ С ПРОДОЛЖЕНИЕМ
            try:
                last_processed_5m = await self._find_last_processed_5m_candle(symbol_norm)

                if last_processed_5m:
                    # Загружаем только НОВЫЕ свечи (после последней обработанной)
                    candles_5m = await self.market_data_utils.read_candles_5m(
                        symbol_norm, start_ts=last_processed_5m + 300000, end_ts=end_time  # +5 минут
                    )
                    main_pbar.set_description("🔄 Resuming 5m calculation...")
                else:
                    # Загружаем все свечи
                    candles_5m = await self.market_data_utils.read_candles_5m(
                        symbol_norm, start_ts=start_time, end_ts=end_time
                    )
                    main_pbar.set_description("🚀 Starting 5m calculation...")

                if candles_5m:
                    if last_processed_5m:
                        main_pbar.set_postfix(m5=f"{len(candles_5m)} NEW candles")
                    else:
                        main_pbar.set_postfix(m5=f"{len(candles_5m)} candles")

                    await self.warmup_manager.warmup_5m_indicators(symbol_norm, candles_5m)
                    main_pbar.set_description("✅ 5m indicators ready")

                    # ✅ НОВОЕ: Устанавливаем флаг готовности для первого анализа
                    if candles_5m:
                        self.logger.info(
                            f"📊 History warmup completed for {symbol_norm}: "
                            f"{len(candles_5m)} 5m candles with indicators ready"
                        )
                    #  Триггер первого анализа ML модели
                    if candles_5m:
                        self.logger.info(f"🚀 Triggering initial ML analysis on last historical candle")
                        # Здесь можно вызвать callback или установить флаг, что система готова
                else:
                    main_pbar.set_postfix(m5="No data")
            except Exception as e:
                main_pbar.set_postfix(m5=f"Error: {str(e)[:20]}")

            #  ВТОРОЙ update(1) ДОЛЖЕН БЫТЬ ЗДЕСЬ, ПОСЛЕ ЗАВЕРШЕНИЯ 5m БЛОКА
            main_pbar.update(1)

        # ✅ ФИНАЛЬНОЕ ПОДТВЕРЖДЕНИЕ
        print(f"\n✅ Successfully warmed up indicators for {symbol}")
        if candles_1m:
            recalc_status = '(recalculated)' if needs_1m_recalc else '(already calculated)'
            print(f"  1m: {len(candles_1m)} candles {recalc_status}")
        if candles_5m:
            resume_status = '(resumed)' if last_processed_5m else '(fresh start)'
            print(f"  5m: {len(candles_5m)} candles {resume_status}")

    async def _check_existing_data_interactive(self, symbol: str, days_back: int) -> Dict[str, Any]:
        """Проверяет существующие данные для интерактивного режима"""
        symbol_norm = self._normalize_symbol(symbol)
        end_time = get_current_ms()
        start_time = end_time - (days_back * 24 * 60 * 60 * 1000)

        result = {
            'has_data': False,
            'is_sufficient': False,
            '1m_count': 0,
            '5m_count': 0,
            '1m_required': self.warmup_manager.warmup_config['1m']['min_bars'],
            '5m_required': self.warmup_manager.warmup_config['5m']['min_bars'],
            'has_1m_sufficient': False,
            'has_5m_sufficient': False,
        }

        try:
            # Проверяем 1m данные
            candles_1m = await self.market_data_utils.read_candles_1m(
                symbol_norm, start_ts=start_time, end_ts=end_time
            )
            if candles_1m:
                result['1m_count'] = len(candles_1m)
                result['has_1m_sufficient'] = len(candles_1m) >= result['1m_required']

            # Проверяем 5m данные
            candles_5m = await self.market_data_utils.read_candles_5m(
                symbol_norm, start_ts=start_time, end_ts=end_time
            )
            if candles_5m:
                result['5m_count'] = len(candles_5m)
                result['has_5m_sufficient'] = len(candles_5m) >= result['5m_required']


            result['has_data'] = result['1m_count'] > 0 or result['5m_count'] > 0
            result['is_sufficient'] = (
                    result['has_1m_sufficient'] and
                    result['has_5m_sufficient']
            )

        except Exception as e:
            self.logger.error(f"Error checking existing data interactively: {e}")

        return result
# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def main():
    logger = logging.getLogger("MarketHistoryCLI")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = StreamHandler()
        formatter = Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    try:
        # --- Синхронный движок для MarketDataUtils ---
        DATABASE_URL_SYNC = "sqlite:///data/market_data.sqlite"
        sync_engine = create_sync_engine(DATABASE_URL_SYNC, future=True)

        # --- Асинхронный движок для MarketHistoryManager ---
        DATABASE_URL_ASYNC = "sqlite+aiosqlite:///data/market_data.sqlite"
        async_engine = create_async_engine(DATABASE_URL_ASYNC, future=True, echo=False)

        # --- Создаём компоненты ---
        utils = MarketDataUtils(sync_engine, logger)
        manager = MarketHistoryManager(async_engine, utils, logger)

        # --- Запуск ---
        await manager.interactive_load()

    except Exception as e:
        logger.error(f"Startup failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
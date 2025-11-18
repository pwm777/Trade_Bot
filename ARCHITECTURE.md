# Trade Bot Architecture (v2.0)

Дата: 2025-11-18  
Автор: pwm777  
Версия: 2.0 (объединение structure_bot.txt + архитектурные улучшения)

---

## 1. Purpose / Scope
Этот документ заменяет прежний `structure_bot.txt`.  
Он объединяет:
- Концептуальный обзор архитектуры
- Потоки данных и ответственности
- Формальные контракты данных (Signal, RiskContext, ExitDecision и др.)
- Принципы Dependency Injection (DI)
- Инварианты и запреты (anti-patterns)
- Расширяемость
- Приложение (полная инвентаризация классов и модулей — перенесено из structure_bot.txt)

`structure_bot.txt` можно удалить после добавления этого файла.

---

## 2. High-Level Overview

```
                 +-------------------+
                 |  Market Aggregator|
                 | (LIVE/DEMO/BACKTEST)
                 +---------+---------+
                           |
                     Candle Events
                           v
+------------------+   +------------------+
|  MarketHistory   |   |  MarketDataUtils |
| (historical load)|   | (indicators, ML features,
+--------+----------+  |  CUSUM, ATR etc.)|
         |              +--------+-------+
         | Warmup/Backfill        |
         v                        v
                +-------------------------------+
                |  Strategy (ImprovedQuality    |
                |  Trend System / Confirmator)  |
                |  + RiskManager (DI)           |
                +------+------------------------+
                       | DetectorSignal
                       v
                +-------------------------------+
                | EnhancedRiskManager           |
                | calculate_risk_context()      |
                +------+------------------------+
                       | RiskContext
                       v
                +-------------------------------+
                | EnhancedTradingBot            |
                | + SignalValidator (DI)        |
                | + ExitManager (DI)            |
                +------+------------------------+
                       | Intent + RiskContext
                       v
                +-------------------------------+
                | PositionManager (DI risk/exit)|
                | Technical order construction  |
                +------+------------------------+
                       | OrderReq
                       v
                +-------------------------------+
                | ExchangeManager               |
                | Place/Modify/Cancel           |
                +---------------+---------------+
                                |
                         Fills / Order Updates
                                v
                        +--------------+
                        | TradingLogger|
                        |  Audit, PnL  |
                        +--------------+
```

---

## 3. Bounded Contexts

| Контекст | Ответственность | Входы | Выходы |
|----------|-----------------|-------|--------|
| Market Data | Сбор / агрегация / прогрев | Биржа / БД | Свечи с индикаторами |
| Strategy | Анализ + генерация направленных сигналов | Свечи, индикаторы | DetectorSignal, TradeSignalIQTS |
| Risk Management | Консолидация размера, SL, TP | DetectorSignal, цена, ATR | RiskContext |
| Execution | Жизненный цикл позиции (открытие/обновление) | Intent + RiskContext | OrderReq / Position state |
| Exit Decision | Управление выходами и защитой прибыли | Позиция + рынок | ExitDecision |
| Audit & Logging | Трассировка, статистика, история | Позиции, ордера | История, отчёты |
| Validation | Единый контроль качества сигналов | Сырые сигналы | ValidationResult |

---

## 4. Core Data Contracts

### 4.1 DetectorSignal (упрощенно)
```json
{
  "ok": true,
  "direction": 1,
  "confidence": 0.74,
  "reason": "trend_confirmed",
  "metadata": {
    "cusum_z": 2.1,
    "regime": "weak_uptrend"
  }
}
```

### 4.2 RiskContext
```json
{
  "position_size": 2.45,
  "initial_stop_loss": 2472.50,
  "take_profit": 2491.00,
  "atr": 12.4,
  "stop_atr_multiplier": 2.0,
  "tp_atr_multiplier": 3.0,
  "volatility_regime": 1.0,
  "regime": "weak_uptrend",
  "computed_at_ms": 1731930000000,
  "risk_manager_version": "v2.0.0",
  "validation_hash": "a3f5c8d9e2b1f0a4"
}
```

### 4.3 TradeSignalIQTS (новый формат)
```json
{
  "symbol": "ETHUSDT",
  "direction": "BUY",
  "entry_price": 2480.25,
  "risk_context": { "...": "..." },
  "stops_precomputed": true,
  "confidence": 0.82,
  "validation_hash": "a3f5c8d9e2b1f0a4"
}
```

### 4.4 ExitDecision (целевой контракт)
```json
{
  "should_exit": true,
  "type": "cascading",
  "urgency": "high",
  "confidence": 0.71,
  "pnl_pct": 1.42,
  "reason": "cascading_reversal",
  "details": {
    "trend_confidence": 0.66,
    "global_confidence": 0.76
  },
  "new_stop_loss": 2485.10,
  "new_take_profit": null
}
```

### 4.5 OrderReq (внутренний)
```json
{
  "client_order_id": "ord_2025_11_18_2480",
  "symbol": "ETHUSDT",
  "side": "BUY",
  "type": "LIMIT",
  "qty": 2.45,
  "price": 2480.25,
  "stop_price": 2472.50
}
```

---

## 5. Main Flows

### 5.1 Открытие позиции (Open Flow)
1. MarketAggregator генерирует свечу → Strategy.generate_signal()
2. Strategy вызывает RiskManager.calculate_risk_context()
3. Формирует TradeSignalIQTS (stops_precomputed=True)
4. EnhancedTradingBot валидирует и конвертирует → intent (LONG_OPEN/SHORT_OPEN)
5. PositionManager использует risk_context для построения OrderReq
6. ExchangeManager размещает ордер
7. TradingLogger фиксирует позицию + аудит risk_context

### 5.2 Обновление стопов (Trailing / Breakeven Flow)
1. ExitManager анализирует позицию → ExitDecision (new_stop_loss)
2. PositionManager строит корректирующий стоп-ордер
3. ExchangeManager модифицирует / размещает новый стоп
4. TradingLogger записывает stop update (slippage / reason)

### 5.3 Принудительный выход (Daily Loss / Hard Stop)
1. RiskManager.should_close_all_positions() = True
2. EnhancedTradingBot инициирует массовый ExitDecision(type="risk_limit")
3. PositionManager формирует exit-ордеры
4. ExchangeManager завершает позиции
5. TradingLogger фиксирует причины и потери

---

## 6. Dependency Injection (DI)

| Компонент | Получает через конструктор | Никогда не создаёт сам |
|-----------|----------------------------|------------------------|
| Strategy (IQTS) | risk_manager | ExchangeManager |
| EnhancedTradingBot | risk_manager, exit_manager, execution_engine, data_provider | RiskManager |
| PositionManager | risk_manager, exit_manager | Strategy |
| ExitManager | (опционально risk_manager) | PositionManager |
| SignalValidator | DI в компоненты (план) | — |

Цель DI:
- Избежание циклических зависимостей
- Тестируемость (mock RiskManager / ExitManager)
- Гибкая замена реализаций

---

## 7. Validation Pipeline

Слои:
1. Detector-level: целостность данных (warmup, отсутствия NaN).
2. Strategy-level: корректность режима рынка, торговые часы.
3. SignalValidator: бизнес-правила (например, risk_context обязателен при stops_precomputed=True).
4. PositionManager: технические биржевые требования (tick_size, min_notional, precision).
5. ExchangeManager: final exchange format / ограничение типов ордеров.

Инвариант:  
Если `signal.stops_precomputed=True` → должен присутствовать валидный `risk_context` с полями `position_size`, `initial_stop_loss`, `take_profit`.

---

## 8. Risk Pipeline & Invariants

Инварианты:
- `initial_stop_loss > 0`
- Для LONG: `initial_stop_loss < entry_price < take_profit`
- Для SHORT: `take_profit < entry_price < initial_stop_loss`
- `position_size > 0`
- `validation_hash == compute_risk_hash(risk_context)` (проверка целостности — можно активировать в paranoid режиме)
- `risk_context` неизменяем после формирования (не модифицировать downstream)

Флоу:
```
DetectorSignal → normalize_direction → calculate_position_size()
→ calculate_dynamic_stops() → assemble RiskContext → hash → validate → attach to TradeSignalIQTS
```

---

## 9. Exit Decision Pipeline (Цель консолидации)

Цель: ExitManager — единственная точка принятия решений:
1. Hard exits (stop_loss, take_profit, max_hold_time)
2. Cascading reversal (multi timeframe)
3. Signal urgency high / medium / low
4. Profit protection (break-even, trailing)
5. Возврат ExitDecision с `should_exit` или `new_stop_loss`

TradingBot._manage_existing_positions():
- Если `should_exit` → build_exit_order()
- Если `new_stop_loss` → update_position_stops()
- Иначе → hold

---

## 10. Audit & Traceability

Отслеживается:
- `validation_hash` (risk_context)
- Slippage: planned SL vs actual stop_price
- Stop updates: время, причина, старый / новый уровень
- ExitDecision.reason taxonomy

Таблицы:
- `positions_risk_audit`
- `trades`
- (потенциально) `stop_updates`

Alert условия:
- SL slippage > 0.1%
- Frequent trailing updates > threshold
- Breakeven moved but price reverted

---

## 11. Architectural Decisions (ADR Summary)

| Решение | Причина | Статус |
|---------|---------|--------|
| Единый risk_manager.py | Уменьшение дублирования | Accepted |
| Direction enum | Типобезопасность | Accepted |
| DI Risk / Exit | Тестируемость, отсутствие циклов | Accepted |
| stops_precomputed flag | Исключить повторные пересчёты SL/TP | Accepted |
| validation_hash | Аудит целостности risk_context | Accepted |
| Deprecated PM.compute_entry_stop() | Логика перенесена в RiskManager | Active |
| Trailing → ExitManager | Разделение ответственности | In Progress |
| SignalValidator как единый слой | Консистентность | Planned |
| Фабрика create_trade_signal() | Стандартизация формирования сигналов | Planned |

---

## 12. Extension Points

Добавить новый ML детектор:
1. Реализовать DetectorInterface
2. Внедрить в Confirmator / Strategy
3. Добавить вес в метрику комбинирования
4. Покрыть тестами (анализ required_bars, edge-case с warmup)

Заменить RiskManager:
1. Реализовать RiskManagerInterface (Protocol)
2. Обеспечить метод: `calculate_risk_context()`
3. Передать через DI в `BotLifecycleManager`

Добавить стратегию выхода:
1. Расширить ExitManager.should_exit_position()
2. Добавить тип в ExitDecision.type
3. Документировать в разделе Audit

---

## 13. Invariants (Runtime Guarantees)

| Инвариант | Нарушение → Действие |
|-----------|----------------------|
| risk_context присутствует при stops_precomputed=True | Reject signal |
| validation_hash соответствует risk_context | Log critical + reject (если paranoid режим включен) |
| direction всегда Direction enum внутри Risk/Strategy | Автоконвертация + warning |
| position_size * entry_price ≥ min_notional | Skip/Warning (PositionManager) |
| SL/TP консистентны (BUY: SL < entry < TP) | ValidationResult.error |
| Daily loss limit не превышен | should_close_all_positions() → массовый exit |

---

## 14. Anti-Patterns / Forbidden Practices

| Запрещено | Используй вместо |
|-----------|------------------|
| `if direction == 1:` | `if direction == Direction.BUY:` |
| Пересчитывать SL внутри PositionManager | Использовать risk_context['initial_stop_loss'] |
| Модифицировать risk_context downstream | Генерировать новый через RiskManager |
| Сравнивать raw строки `"BUY"/"SELL"` | `direction_to_side(direction)` |
| Создавать RiskManager внутри стратегии | DI из BotLifecycleManager |
| Пропускать валидацию сигналов | Использовать SignalValidator |
| Смешивать бизнес-валидатор и биржевую валидацию | Разделять: Validator vs Exchange constraints |

---

## 15. Glossary

| Термин | Значение |
|--------|----------|
| DetectorSignal | Сигнал от детектора тренда (направление + уверенность) |
| RiskContext | Полный набор риск-параметров (size, SL, TP + метаданные) |
| TradeSignalIQTS | Торговый сигнал от стратегии (обогащённый risk_context) |
| ExitDecision | Решение о выходе или обновлении стопов |
| Slippage | Отклонение фактического стопа от планируемого |
| DI | Dependency Injection — внешнее внедрение зависимостей |
| ValidationResult | Результат проверки сигналов/ордеров |
| Tamper Detection | Проверка целостности risk_context через хеш |

---

## 16. Roadmap (v2.x → v3.0)

| Этап | Задача | Статус |
|------|--------|--------|
| 1 | Полная миграция deprecated методов | In progress |
| 2 | Фабрика create_trade_signal() | Planned |
| 3 | Tamper check (validation_hash) | Planned |
| 4 | ExitDecision расширение (new_stop_loss / trailing типы) | In progress |
| 5 | Вынос PnLTracker из RiskManager | Planned |
| 6 | Тесты (unit + integration) | Planned |
| 7 | Lint правило на direction enum | Planned |
| 8 | Документация API ExitDecision | Planned |

---

## 17. Checklist для Pull Requests

Перед мержем:
- [ ] Нет прямых сравнений direction == 1 / -1
- [ ] Все новые сигналы содержат risk_context при stops_precomputed=True
- [ ] validation_hash генерируется и логируется
- [ ] Slippage > 0.1% вызывает alert
- [ ] Deprecated методы не используются вне fallback путей
- [ ] ExitDecision имеет стандартизированные поля (reason, urgency, type)
- [ ] Компоненты создаются через DI (нет inline new RiskManager())

---

## 18. Appendix: Инвентаризация (ранее structure_bot.txt)

Ниже полная структура модулей и классов (скопировано и адаптировано из `structure_bot.txt`).  
Этот раздел — справочный. Не редактируется часто, актуализируется по мере расширений.

(Содержимое structure_bot.txt интегрировано без изменений логики — сокращения отсутствуют.)

<details>
<summary>Раскрыть инвентаризацию</summary>

БАЗОВЫЕ МОДУЛИ СИСТЕМЫ:
    ├── iqts_standards.py (стандарты и интерфейсы)
    ├── run_bot.py (главный координатор)
    ├── trade_bot.py (исполнение)
    ├── ImprovedQualityTrendSystem.py (стратегия)
    ├── iqts_detectors.py (детекторы)
    ├── multi_timeframe_confirmator.py (анализатор)
    ├── market_aggregator.py (агрегатор данных)
    └── market_data_utils.py (утилиты данных)
 	
ДОПОЛНЕННЫЕ МОДУЛИ:
    ├── EnhancedRiskManager (риск-менеджер) - risk_manager.py
    ├── AdaptiveExitManager (менеджер выхода) - exit_system.py
    ├── TradingLogger (логгер торговли) - trading_logger.py
    ├── MLGlobalDetector (ML детектор) - ml_global_detector.py
    ├── PerformanceTracker (трекер производительности) - performance_tracker.py
    ├── BacktestEngine (движок бэктеста) - backtest_engine.py
    ├── MarketHistoryManager (исторические данные) - market_history.py
    ├── ExchangeManager (биржа) - exchange_manager.py
    ├── MLLabelingTool (офлайн разметчик) - ml_labeling_tool_v3.py
    ├── MLGlobalModelTrainer (обучение) - ml_train_global_v2.py
    └── SignalValidator (валидация) - signal_validator.py

(Далее полное древо содержимого из structure_bot.txt — см. оригинальный файл. Можно обновлять периодически.)

</details>

---

## 19. Deprecation Map

| Метод / Файл | Статус | Заменяется |
|--------------|--------|-----------|
| improved_algorithm.py | Removed | risk_manager.py |
| PositionManager.compute_entry_stop() | Deprecated | risk_manager.calculate_initial_stop() |
| PositionManager.compute_trailing_level() | Deprecated | exit_manager.calculate_trailing_stop() |
| direction == int сравнения | Deprecated | Direction enum |
| Прямое присвоение stop_loss/take_profit без risk_context | Deprecated | RiskContext.stops_precomputed flow |

---

## 20. FAQ

| Вопрос | Ответ |
|--------|-------|
| Почему два Definition Direction (в standards и risk_manager)? | Автономность risk_manager. Можно консолидировать в будущем. |
| Можно ли использовать систему без risk_context? | Только в режиме backward compatibility; не рекомендуется. |
| Зачем validation_hash? | Контроль целостности: обнаружение несанкционированных изменений risk_context. |
| Где реализована логика trailing? | В ExitManager — расчёт, в PositionManager — техническое оформление ордера. |
| Почему нет централизованного фабричного конструктора сигналов? | Планируется создать create_trade_signal() для стандартизации. |

---

## 21. Changelog (Архитектурный)

| Дата | Изменение | Автор |
|------|-----------|-------|
| 2025-11-18 | Консолидация risk-менеджмента, DI внедрён | pwm777 |
| 2025-11-18 | Добавлен calculate_initial_stop() | pwm777 |
| 2025-11-18 | Добавлен calculate_trailing_stop() | pwm777 |
| 2025-11-18 | Объединение structure_bot.txt → ARCHITECTURE.md | pwm777 |

---

## 22. License / Ownership
Архитектурный документ принадлежит проекту Trade Bot.  
Изменения требуют ревью минимум одного разработчика, ответственного за слой (Strategy / Risk / Execution).

---

(END)


# Trade_Bot Structure (v2.0)
  
## 1. БАЗОВЫЕ МОДУЛИ СИСТЕМЫ:
    ├── iqts_standards.py (стандарты и интерфейсы)
    ├── run_bot.py (главный координатор)
    ├── trade_bot.py (исполнение)
    ├── ImprovedQualityTrendSystem.py (стратегия)
    ├── iqts_detectors.py (детекторы)
    ├── multi_timeframe_confirmator.py (анализатор)
    ├── market_aggregator.py (агрегатор данных)
    └── market_data_utils.py (утилиты данных)
	
## 2. ДОПОЛНЕННЫЕ МОДУЛИ:
    ├── EnhancedRiskManager (риск-менеджер) - risk_manager.py
    ├── AdaptiveExitManager (менеджер выхода) - exit_system.py
    ├── TradingLogger (логгер торговли) - trading_logger.py
    ├── MLGlobalDetector (ML детектор) - ml_global_detector.py
    ├── PerformanceTracker (трекер производительности) - performance_tracker.py
    ├── BacktestEngine (движок бэктеста) - backtest_engine.py
    ├── MarketHistoryManager (менеджер исторических данных) - market_history.py
    ├── ExchangeManager (менеджер биржи) - exchange_manager.py
	├── MLLabelingTool (офлайн разметчик и генерация snapshot'ов) - ml_labeling_tool_v3.py
    ├── MLGlobalModelTrainer (офлайн обучение ML-модели глобального тренда) - ml_train_global_v2.py
	└──SignalValidator (Глобальный валидатор) - signal_validator.py


## 3. Общая архитектура системы

BotLifecycleManager (run_bot.py) - главный координатор
    ├── ImprovedQualityTrendSystem (стратегия) - ImprovedQualityTrendSystem.py
    │   └── ThreeLevelHierarchicalConfirmator (анализатор) - multi_timeframe_confirmator.py
    │       ├── MLGlobalTrendDetector (глобальный детектор) - iqts_detectors.py
    │       └── RoleBasedOnlineTrendDetector (трендовый детектор) - iqts_detectors.py
    ├── EnhancedTradingBot (исполнение) - trade_bot.py
    │   ├── PositionTracker (трекинг позиций)
    │   ├── AdaptiveExitManager (управление выходом) - exit_system.py 
    ├── PositionManager (менеджер позиций)
    ├── ExchangeManager (менеджер биржи)
    ├── MarketAggregator (агрегатор данных) - market_aggregator.py
    ├── MarketDataUtils (утилиты данных) - market_data_utils.py
    └── MarketHistoryManager (менеджер исторических данных)


=====================================================================
## 4. модуль run_bot.py

Основные классы:
run_bot.py
├── ComponentsContainer (dataclass)
│   ├── trade_log: Any
│   ├── position_manager: PositionManagerInterface
│   ├── exchange_manager: ExchangeManagerInterface
│   ├── strategy: StrategyInterface (ImprovedQualityTrendSystem)
│   ├── market_aggregator: MarketAggregatorInterface
│   ├── main_bot: MainBotInterface (EnhancedTradingBot)
│   ├── exit_manager: Any (AdaptiveExitManager)
│   ├── logger: logging.Logger
│   ├── history_manager: Optional[MarketHistoryManager]
│   ├── async_store: Optional[Any]
│   └── created_at: datetime
│
└── BotLifecycleManager
    ├── Конструктор: config, event_handlers, shutdown_timeout
    │
    ├── Состояние менеджера:
    │   ├── _is_running: bool
    │   ├── _stopping: bool
    │   ├── _shutdown_event: asyncio.Event
    │   ├── _components: Optional[ComponentsContainer]
    │   └── _event_handlers: List[BotLifecycleEventHandler]
    │
    ├── Фоновые задачи:
    │   ├── _monitoring_task: Optional[asyncio.Task]
    │   ├── _main_loop_task: Optional[asyncio.Task]
    │   └── _trading_task: Optional[asyncio.Task]
    │
    ├── Управление событиями:
    │   ├── add_event_handler() → None
    │   ├── remove_event_handler() → None
    │   └── _emit_event() → None
    │
    ├── Основные методы жизненного цикла:
    │   ├── async start() → None
    │   ├── async stop() → None
    │   └── async wait_for_shutdown() → None
    │
    ├── Создание компонентов:
    │   ├── async _create_components() → ComponentsContainer
    │   │   ├── _create_logger() → logging.Logger
    │   │   ├── async _create_trade_log() → TradingLogger
    │   │   ├── async _create_strategy() → StrategyInterface
    │   │   ├── async _create_position_manager() → PositionManagerInterface
    │   │   ├── async _create_exchange_manager() → ExchangeManagerInterface
    │   │   ├── async _create_exit_manager() → AdaptiveExitManager
    │   │   ├── async _create_market_aggregator() → MarketAggregatorInterface
    │   │   └── async _create_main_bot() → MainBotInterface
    │   ├── async _create_history_manager() → MarketHistoryManager
    │   └── async _create_async_store() → Any
    ├── Управление кэшированными сигналами:
	│   ├── Проверка _cached_global_signal в стратегии
    │   ├── Перепроверка при direction_disagreement
    │   └── Обработка delayed_signal
	├── Интеграция компонентов:
    │   ├── PositionManager → ExecutionEngine
    │   ├── DataProvider → MarketAggregator events
    │   └── Strategy → Cached signals processing
	│
    ├── Основные циклы:
    │   ├── async _run_main_loop() → None
    │   └── async _run_main_bot_monitoring() → None
    │
    ├── Утилиты:
    │   ├── _check_components_health() → None
    │   ├── _setup_signal_handlers() → None
    │   ├── async _cleanup() → None
    │   └── async _safe_call() → Any
    │
    └── Свойства:
        ├── is_running → bool - основной entry point
        └── components → Optional[ComponentsContainer]

Функции верхнего уровня:
├── async main() → None
└── async run_backtest_mode() → None - режим бэктеста


Внутренние классы (вложенные в методы):

В _create_main_bot():
├── DataProviderFromDB (DataProvider)
│   ├── Конструктор: market_data_utils, logger
│   ├── Буферизация:
│   │   ├── _in_memory_buffer: Dict[str, Dict[str, deque]]
│   │   └── _buffer_size: int (1000)
│   ├── Основные методы:
│   │   ├── update_from_candle_event() → None
│   │   ├── async get_market_data() → Dict[str, pd.DataFrame]
│   │   └── async get_current_price() → float
│   ├── Внутренние методы:
│   │   ├── _get_or_create_buffer() → deque
│   │   ├── _get_buffered_data() → pd.DataFrame
│   │   ├── _load_from_db() → Optional[pd.DataFrame]
│   │   ├── _merge_data_sources() → pd.DataFrame
│   │   └── _get_price_from_db() → float
│   └── Синхронизация:
│       └── _buffer_lock: asyncio.Lock
│
├── ExecutionEngineFromExchangeManager (ExecutionEngine)
│   ├── Конструктор: em, position_manager, logger
│   └── Основные методы:
│       ├── async place_order() → Dict
│       ├── async close_position() → Dict
│       └── async get_account_info() → Dict
│
└── MainBotAdapter (MainBotInterface)
    ├── Конструктор: core_bot, logger
    ├── Состояние:
    │   ├── core: EnhancedTradingBot
    │   ├── _handler: Optional[Callable]
    │   ├── _start_task: Optional[asyncio.Task]
    │   └── _stats: Dict[str, Any]
    ├── Основные методы:
    │   ├── async main_trading_loop() → None
    │   ├── async start() → None
    │   ├── async stop() → None
    │   ├── async bootstrap() → None
    │   ├── get_stats() → Dict
    │   └── get_component_health() → Dict
    ├── Обработка событий:
    │   ├── add_event_handler() → None
    │   └── handle_candle_ready() → None 
    └── Управление данными:
        └── Интеграция с DataProvider.update_from_candle_event()
		

==================================================================

## 5. Модуль: trade_bot.py

Абстрактные интерфейсы:
trade_bot.py
├── DataProvider (ABC)
│   ├── @abstractmethod get_market_data() → Dict[str, pd.DataFrame]
│   └── @abstractmethod get_current_price() → float
│
└── ExecutionEngine (ABC)
    ├── @abstractmethod place_order() → Dict
    ├── @abstractmethod close_position() → Dict
    └── @abstractmethod get_account_info() → Dict
Основные классы:
├── EnhancedTradingBot
│   ├── Конструктор: config, data_provider, execution_engine, trading_system (опционально)
│   │
│   ├── Инициализация:
│   │   ├── _setup_logging() → logging.Logger
│   │   ├── _setup_monitoring() → None
│   │   └── _validate_connections() → None
│   │
│   ├── Компоненты системы:
│   │   ├── trading_system: ImprovedQualityTrendSystem (переданный или созданный)
│   │   ├── exit_manager: AdaptiveExitManager (с явным приведением типов таймфреймов)
│   │   └── monitoring_system: EnhancedMonitoringSystem
│   │
│   │
│   ├── Управление данными:
│   │   ├── _get_market_data() → Optional[Dict]
│   │   └── _parse_timeframe() → int
│   │
│   ├── Обработка сигналов:
│   │   ├── _process_trade_signal() → None
│   │   │   ├── Конвертация TradeSignalIQTS → TradeSignal (intent-based)
│   │   │   ├── Делегирование PositionManager.handle_signal() → OrderReq
│   │   │   └── Отправка OrderReq через ExchangeManager
│   │   ├── _convert_iqts_signal_to_trade_signal() → Optional[Dict]
│   │   └── _final_execution_check() → bool (с опциональными параметрами)
│   │
│   ├── Управление позициями:
│   │   ├── _update_positions() → None
│   │   ├── _handle_position_closed() → None
│   │   ├── _manage_existing_positions() → None (с адаптивным выходом)
│   │   ├── _update_position_stop_loss() → None (реализовано через execution engine)
│   │   └── _calculate_trade_result() → TradeResult
│   │
│   ├── Уведомления:
│   │   ├── _send_trade_notification() → None
│   │   ├── _send_position_closed_notification() → None
│   │   └── _log_system_status() → None
│   │
│   ├── Валидация:
│   │   ├── _validate_close_price() → bool
│   │   └── _basic_validate_market_data() → bool (локальная проверка)
│   │
│   ├── Аварийные процедуры:
│   │   ├── _emergency_shutdown() → None
│   │   └── shutdown() → None
│   │
│   ├── Основные методы:
│   │   ├── start() → None
│   │   └── get_status() → Dict
│   │
│   └── Свойства:
│       ├── is_running: bool
│       ├── active_positions: Dict
│       ├── position_tracker: PositionTracker
│       ├── trading_system: ImprovedQualityTrendSystem
│       ├── exit_manager: AdaptiveExitManager
│       └── monitoring_system: EnhancedMonitoringSystem
│
└── PositionTracker
    ├── Конструктор: max_history=1000
    ├── Управление позициями:
    │   ├── add_position() → None
    │   ├── get_position() → Optional[Dict]
    │   ├── get_all_positions() → Dict
    │   └── close_position() → None
    │
    ├── Расчет PnL:
    │   ├── update_position_pnl() → None
    │   ├── calculate_realized_pnl() → float
    │   └── get_total_unrealized_pnl() → float
    │
    └── История:
        └── get_closed_positions() → List[Dict]
=========================================================================
		
## 6. Модуль: ImprovedQualityTrendSystem.py
Типы данных:
ImprovedQualityTrendSystem.py
├── RegimeType = Literal["strong_uptrend", "weak_uptrend", "strong_downtrend", 
│                       "weak_downtrend", "sideways", "uncertain"]
├── VolumeProfileType = Literal["high", "normal", "low"]
│
└── MarketRegime (dataclass)
    ├── regime: RegimeType
    ├── confidence: float
    ├── volatility_level: float
    ├── trend_strength: float
    └── volume_profile: VolumeProfileType

Основной класс:
└── ImprovedQualityTrendSystem (TradingSystemInterface)
    ├── Конструктор: config, data_provider (опционально)
    │
    ├── Компоненты системы:
    │   ├── three_level_confirmator: ThreeLevelHierarchicalConfirmator (прямая интеграция)
    │   ├── risk_manager: EnhancedRiskManager
    │   └── _cached_global_signal: Dict[str, Dict] (кэш 5m сигналов при разногласиях)
    │
    ├── Инициализация:
    │   ├── _initialize_risk_manager() → EnhancedRiskManager
    │   ├── _initialize_performance_tracker() → Dict
    │   └── Настройка параметров качества
    │
    ├── Фильтры качества:
    │   ├── _apply_quality_filters() → DetectorSignal
    │   ├── _adaptive_volume_filter() → Dict
    │   └── _adaptive_volatility_filter() → Dict
    │
    ├── Проверки условий:
    │   ├── _check_trading_conditions() → bool
    │   ├── _is_trading_session_now() → bool
    │   └── _validate_market_data_quality() → bool
    │
    ├── Анализ рынка:
    │   ├── _update_market_regime() → None
    │   └── _calculate_atr() → float
    │
    ├── Основные методы интерфейса:
    │   ├── async analyze_and_trade() → Optional[TradeSignalIQTS]
    │   │   ├── Валидация данных и условий
    │   │   ├── Анализ через three_level_confirmator
    │   │   ├── Применение фильтров качества
    │   │   ├── Расчет ATR и цены
    │   │   ├── Определение размера позиции через risk_manager
    │   │   ├── Расчет стоп-лосса и тейк-профита
    │   │   └── Формирование полного TradeSignalIQTS
    │   │
    │   ├── async generate_signal() → Optional[Dict]
    │   │   ├── Упрощенная генерация сигналов для PositionManager
    │   │   ├── Кэширование 5m сигналов при разногласиях
    │   │   └── Очистка кэша при успешных сигналах
    │   │
    │   ├── update_performance() → None
    │   ├── get_system_status() → SystemStatus
    │   └── get_performance_report() → Dict
    │
    ├── Управление кэшированными сигналами:
    │   ├── async check_cached_global_signal() → Optional[Dict]
    │   │   ├── Проверка TTL кэша (5 минут)
    │   │   ├── Анализ текущего 1m тренда
    │   │   ├── Проверка согласованности направлений
    │   │   └── Генерация отложенного сигнала при согласии
    │   │
    │   └── _extract_symbol_from_data() → str
    │
    ├── Состояние системы:
    │   ├── current_regime: Optional[MarketRegime]
    │   ├── trades_today: int
    │   ├── daily_stats: Dict
    │   ├── performance_tracker: Dict
    │   ├── account_balance: float
    │   └── _daily_stats_lock: Lock
    │
    └── Управление жизненным циклом:
        └── async shutdown() → None

Ключевые особенности реализации:
├── Упрощенная архитектура: прямое использование ThreeLevelHierarchicalConfirmator
├── Двойной режим работы: analyze_and_trade (полный) + generate_signal (упрощенный)
├── Интеллектуальное кэширование: сохранение сильных 5m сигналов при разногласиях
├── Отложенные входы: автоматическая проверка кэшированных сигналов на согласованность
├── Гибкие фильтры: адаптивные объем и волатильность с EMA сглаживанием
├── Полная типовая безопасность: Literal типы для режимов и профилей
├── Потокобезопасность: Lock для daily_stats
└── Интеграция с EnhancedRiskManager для расчета позиций и стопов

Поток данных:
├── Полный анализ: analyze_and_trade → three_level_confirmator → risk_manager → TradeSignalIQTS
├── Упрощенный анализ: generate_signal → three_level_confirmator → минимальный сигнал
├── Кэширование: direction_disagreement → _cached_global_signal → check_cached_global_signal
└── Отложенный вход: cached signal + trend согласие → delayed signal

Логика кэширования:
├── Сохраняются только сильные 5m сигналы (confidence ≥ 0.6)
├── TTL кэша: 5 минут (300 000 ms)
├── Автоматическая очистка при успешных сигналах
├── Проверка согласованности с текущим 1m трендом
└── Генерация отложенного сигнала при достижении согласия

Интеграция в систему:
BotLifecycleManager → ImprovedQualityTrendSystem
    ├── analyze_and_trade() → ThreeLevelHierarchicalConfirmator → TradeSignalIQTS
    ├── generate_signal() → PositionManager (упрощенный поток)
    └── check_cached_global_signal() → отложенные входы при разногласиях
===========================================================================

## 7. МОДУЛЬ: ml_global_detector.py

ml_global_detector.py
└── MLGlobalDetector (Detector)
    ├── Конструктор: __init__(timeframe="5m", model_path, use_fallback=False, name, use_scaler=None)
    │   ├── Загрузка модели из joblib:
    │   │   ├── ✅ Современный пакетный формат (trainer v2.1.1 windowed):
    │   │   │   ├── lookback, base_feature_names, scaler
    │   │   │   ├── decision_policy (tau/delta/cooldown/bars_per_day)
    │   │   │   └── metadata (версия, обучено на, accuracy и др.)
    │   │   └── ⚙️ Legacy-формат (raw Booster): обратная совместимость (1 бар, без окна)
    │   ├── Инициализация:
    │   │   ├── self.lookback: int (1 для legacy, N≥2 для windowed)
    │   │   ├── self.base_feature_names: List[str] (21 признак)
    │   │   ├── self.feature_names: List[str] = _generate_windowed_feature_names()
    │   │   │   └── Формат: [feat_t0, ..., feat_t-(lookback-1)] (совпадает с trainer!)
    │   │   ├── self.scaler: Optional[StandardScaler]
    │   │   └── self.decision_policy: Optional[Dict] (из метаданных модели)
    │   └── Состояние:
    │       ├── self._last_signal_ts: Optional[int] (для cooldown)
    │       └── self.last_confidence: Optional[float]
    │
    ├── Внутренние утилиты:
    │   ├── _generate_windowed_feature_names() → List[str]
    │   │   └── Генерация имён в порядке: [t0_feats], [t-1_feats], ..., [t-(N-1)_feats]
    │   └── _validate_features(features) → bool
    │       └── Проверка на NaN/Inf
    │
    ├── Извлечение признаков:
    │   └── extract_features(df: pd.DataFrame) → np.ndarray
    │       ├── ✅ Windowed режим (lookback > 1):
    │       │   ├── Выбор df.iloc[-lookback:]
    │       │   ├── Векторизация → window (lookback × n_base)
    │       │   ├── Реверс окна (`window[::-1]`) → t0 первый!
    │       │   └── Flatten → (1, lookback × n_base)
    │       └── ⚙️ Legacy режим (lookback = 1):
    │           └── Извлечение последней строки → (1, n_base)
    │
    ├── Основной метод анализа:
    │   └── async analyze(data: Dict[Timeframe, pd.DataFrame]) → DetectorSignal
    │       ├── Валидация входа (timeframe, OHLCV, warmup ≥ max(required_warmup, lookback))
    │       ├── Извлечение признаков → X
    │       ├── Опциональное масштабирование (scaler)
    │       ├── Предсказание: proba = [p_hold, p_buy, p_sell]
    │       ├── Применение **decision_policy** (если есть):
    │       │   ├── maxp = max(p_buy, p_sell)
    │       │   ├── margin = |p_buy − p_sell|
    │       │   ├── act = (maxp ≥ tau) ∧ (margin ≥ delta)
    │       │   ├── cooldown по барам (через _last_signal_ts)
    │       │   └── reason:
    │       │       ├── `"trend_confirmed"` (если act=✓)
    │       │       ├── `"weak_trend_signal"` (если act=✗, но направление ≠ 0)
    │       │       ├── `"cooldown_active"`
    │       │       └── `"no_trend_signal"` (если pred=FLAT)
    │       ├── Fallback-логика (если policy=None):
    │       │   ├── threshold = min_confidence (по умолчанию 0.53)
    │       │   └── reason = `"trend_confirmed"` / `"weak_trend_signal"`
    │       └── normalize_signal() → DetectorSignal (со всеми metadata)
    │
    ├── Утилиты загрузки и диагностики:
    │   ├── load_model(path) → None
    │   │   ├── Поддержка legacy и пакетного форматов
    │   │   ├── Валидация типа модели (lgb.Booster)
    │   │   └── Обновление: lookback, base_feature_names, scaler, decision_policy
    │   └── get_required_bars() → Dict[str, int]
    │       └── {timeframe: max(required_warmup, lookback)}
    │
    └── Интеграция в систему:
        ├── Используется в MLGlobalTrendDetector.analyze() (iqts_detectors.py)
        ├── Полностью совместим с оконными моделями из `train_ml_global_v2_windowed.py`
        ├── Поддержка warmup ≥ lookback → корректный инференс на старте
        └── Все входные/выходные данные типизированы через iqts_standards
Ключевые особенности:
├── ✅ Полная поддержка оконного инференса (lookback × base_features)
├── ✅ Точное соответствие формату trainer'а:
│   ├── порядок лагов: [t0, t-1, ..., t-(N-1)]
│   ├── имена фич: {base}_t0, {base}_t-1, ...
│   └── применение decision_policy (tau/delta/cooldown)
├── ✅ Backward-совместимость: legacy-модели работают без изменений
├── ✅ Cooldown реализован по барам (аналогично трейнеру — честно)
├── ✅ Подробная диагностика при ошибках: missing features, warmup, scaling
├── ✅ Rich metadata в DetectorSignal (probabilities, lookback, policy, vector_dim)
└── ✅ Потокобезопасность: нет shared mutable state (за исключением _last_signal_ts — один детектор на экземпляр)
===========================================================================
	
## 8. Модуль: iqts_detectors.py
Базовый класс:

Detector (ABC из iqts_standards)
├── name: str
├── logger: logging.Logger
├── @abstractmethod get_required_bars() → Dict[str, int]
├── @abstractmethod async analyze() → DetectorSignal
└── @abstractmethod get_status() → Dict
Конкретные детекторы:

iqts_detectors.py
├── MLGlobalTrendDetector (Detector)
│   ├── Конструктор: timeframe, model_path, use_fallback, name
│   ├── Основные компоненты:
│   │   ├── ml_detector: Optional[MLGlobalDetector]
│   │   ├── fallback_detector: Optional[GlobalTrendDetector]
│   │   └── using_fallback: bool
│   ├── Методы:
│   │   ├── _activate_fallback() → None
│   │   ├── get_required_bars() → Dict[str, int]
│   │   ├── async analyze() → DetectorSignal
│   │   └── get_status() → Dict
│   └── Утилиты:
│       └── reset_state() → None
│
├── RoleBasedOnlineTrendDetector (Detector)
│   ├── Конструктор: timeframe, role, name
│   ├── Параметры ролей:
│   │   ├── role: str ("trend")
│   │   ├── min_confidence: float (0.5 для trend)
│   │   └── required_warmup: int (50)
│   ├── Состояние:
│   │   └── signal_count: int
│   ├── Методы:
│   │   ├── get_required_bars() → Dict[str, int]
│   │   ├── async analyze() → DetectorSignal
│   │   └── get_status() → Dict
│   └── Утилиты:
│       └── reset_state() → None
│
└── GlobalTrendDetector (Detector) - Fallback детектор
    ├── Конструктор: timeframe, name
    ├── CUSUM параметры:
    │   ├── cusum_pos: float
    │   ├── cusum_neg: float
    │   ├── cusum_threshold: float (4.0)
    │   └── required_warmup: int (50)
    ├── История цен:
    │   ├── price_history: List[float]
    │   └── max_history: int (30)
    ├── Методы:
    │   ├── get_required_bars() → Dict[str, int]
    │   ├── async analyze() → DetectorSignal
    │   └── get_status() → Dict
    └── Утилиты:
        └── reset_state() → None

==================================================================
		
## 9. Модуль: multi_timeframe_confirmator.py
multi_timeframe_confirmator.py
└── ThreeLevelHierarchicalConfirmator (Detector)
    ├── Конструктор: global_timeframe, trend_timeframe, name
    │
    ├── Детекторы-компоненты:
    │   ├── global_detector: MLGlobalTrendDetector (5m) - ML с fallback
    │   └── trend_detector: RoleBasedOnlineTrendDetector (1m) - CUSUM
    │
    ├── Параметры конфигурации:
    │   ├── min_global_confidence: float (0.6)
    │   ├── min_trend_confidence: float (0.55)
    │   ├── direction_agreement_required: bool (True)
    │   └── weights: Dict (global: 0.5, trend: 0.3)
    │
    ├── История сигналов:
    │   ├── global_signal_history: List
    │   ├── trend_signal_history: List
    │   └── max_history_length: int (10)
    │
    ├── Состояние системы:
    │   ├── _last_signal: Optional[DetectorSignal]
    │   ├── last_confirmed_direction: Optional[int]
    │   └── confirmation_count: int
    │
    ├── Основные методы:
    │   ├── get_required_bars() → Dict[Timeframe, int]
    │   ├── async warmup_from_history() → None
    │   ├── async analyze() → DetectorSignal
    │   │   ├── Валидация данных и проверка прогрева
    │   │   ├── Глобальный анализ (5m) с диагностикой
    │   │   ├── Локальный тренд (1m) с обработкой слабых сигналов
    │   │   ├── Проверка согласованности направлений
    │   │   ├── Проверка консистентности с историей
    │   │   ├── Комбинирование уверенности
    │   │   └── Формирование итогового сигнала
    │   ├── update_parameters() → None
    │   ├── get_recent_performance() → Dict[str, Any]
    │   └── get_system_status() → Dict
    │
    ├── Внутренние методы анализа:
    │   ├── _calculate_weighted_confidence() → float
    │   ├── _check_two_level_consistency() → Dict
    │   │   └── FLAT разрешен (используется только global direction)
    │   ├── _update_global_history() → None
    │   ├── _update_trend_history() → None
    │   ├── _update_signal_history() → None
    │   └── _error_signal() → DetectorSignal
    │
    ├── Утилиты логирования:
    │   ├── _setup_logging() → None
    │   ├── _log_result() → None
    │   └── reset_state() → None
    │
    └── Управление состоянием:
        ├── _set_last_signal() → None
        ├── get_last_signal() → Optional[DetectorSignal]
        └── reset_state() → None

Ключевые особенности реализации:
├── 2-уровневый анализ вместо 3-уровневого (глобальный + тренд)
├── Гибкая обработка слабых трендовых сигналов (не блокирующая)
├── FLAT направление разрешено для тренда (используется только global direction)
├── Улучшенная диагностика и логирование на каждом этапе
├── Сохранение исходных причин от детекторов при ошибках
├── Взвешенное комбинирование уверенности (global: 50%, trend: 30%)
└── Универсальная система обновления истории сигналов

Логика принятия решений:
├── Приоритет 1: Глобальный тренд (5m) - основной фильтр
├── Приоритет 2: Локальный тренд (1m) - подтверждающий фильтр
├── Обработка слабых сигналов: trend confidence < threshold → FLAT
├── Согласованность: при trend=FLAT используется global direction
├── Консистентность: FLAT разрешен для single global signal
└── Комбинирование: weighted average confidence

Интеграция в систему:
ImprovedQualityTrendSystem → ThreeLevelHierarchicalConfirmator.analyze()
    ├── global_detector.analyze() → MLGlobalTrendDetector (5m)
    └── trend_detector.analyze() → RoleBasedOnlineTrendDetector (1m)
========================================================
		
## 10. Модуль: market_aggregator.py
Абстрактный базовый класс:
market_aggregator.py
├── BaseMarketAggregator (ABC)
│   ├── Конструктор: logger_instance
│   ├── Состояние:
│   │   ├── _is_running: bool
│   │   ├── _main_lock: threading.RLock
│   │   ├── _connection_state: NetConnState
│   │   └── _stats: Dict[str, Any]
│   ├── Фоновые задачи:
│   │   ├── _running_tasks: Dict[str, asyncio.Task]
│   │   └── _create_or_cancel_task() → asyncio.Task
│   ├── Утилиты:
│   │   ├── _convert_to_decimal() → Decimal
│   │   ├── _convert_to_float() → float
│   │   ├── _cancel_all_tasks() → None
│   │   └── _candle_dict_to_candle1m() → Candle1m
│   ├── Управление жизненным циклом:
│   │   ├── stop() → None
│   │   ├── shutdown() → None
│   │   └── get_stats() → Dict[str, Any]
│   └── Абстрактные методы:
│       ├── _get_mode() → str
│       └── async start_async() → None
│
├── Вспомогательные функции:
│   ├── bucket_ts_with_phase() → int
│   └── finalize_cutoff() → int
│
├── LiveMarketAggregator (BaseMarketAggregator)
│   ├── Конструктор: db_dsn, on_candle_ready, on_connection_state_change, interval_ms, logger_instance, trading_logger
│   ├── Состояние:
│   │   ├── _symbol_buffers: Dict[str, deque]
│   │   ├── _active_symbols: List[str]
│   │   └── _market_data_utils: MarketDataUtils
│   ├── Основные методы:
│   │   ├── _get_mode() → str
│   │   ├── async start_async() → None
│   │   └── async wait_for_completion() → None
│   └── Интерфейс:
│       ├── add_event_handler() → None
│       ├── fetch_recent() → List[Candle1m]
│       └── get_connection_state() → NetConnState
│
├── DemoMarketAggregatorPhased (BaseMarketAggregator)
│   ├── Конструктор: config, on_candle_ready, on_connection_state_change, logger_instance, trading_logger
│   ├── Константы:
│   │   ├── ONE_M_MS: int = 60_000
│   │   └── FIVE_M_MS: int = 300_000
│   ├── Состояние:
│   │   ├── _active_symbols: List[str]
│   │   ├── _symbol_buffers_1m: Dict[str, deque]
│   │   ├── _symbol_buffers_5m: Dict[str, deque]
│   │   ├── _last_historical_ts: Dict[str, int]
│   │   ├── _last_historical_ts_5m: Dict[str, int]
│   │   ├── _websocket: Optional[WebSocket]
│   │   ├── _ws_task: Optional[asyncio.Task]
│   │   └── _market_data_utils: MarketDataUtils
│   ├── WebSocket управление:
│   │   ├── async _connect_ws() → None
│   │   ├── async _ws_loop() → None
│   │   └── async _schedule_reconnect() → None
│   ├── Обработка данных:
│   │   ├── _on_kline_1m() → None
│   │   ├── _on_kline_5m() → None
│   │   ├── _kline_to_candle1m() → Optional[Candle1m]
│   │   ├── _candle_to_dict() → Dict[str, Any]
│   │   ├── _on_candle_ready_1m() → None
│   │   └── async _on_candle_ready_5m() → None
│   └── Интерфейс:
│       ├── add_event_handler() → None
│       ├── fetch_recent() → List[Candle1m]
│       └── get_buffer_history() → List[Candle1m]
│
├── BacktestMarketAggregatorFixed (BaseMarketAggregator)
│   ├── Конструктор: trading_logger, on_candle_ready, symbols, virtual_clock_start_ms, virtual_clock_end_ms, interval_ms, logger
│   ├── Состояние:
│   │   ├── _symbol_buffers: Dict[str, deque]
│   │   ├── _engine: Engine
│   │   └── _stats: Dict[str, Any]
│   ├── Воспроизведение данных:
│   │   ├── async _replay_loop() → None
│   │   └── async start_async() → None
│   └── Интерфейс:
│       ├── async wait_for_completion() → None
│       ├── fetch_recent() → List[Candle1m]
│       └── get_buffer_history() → List[Candle1m]
│
└── MarketAggregatorFactory
    ├── Статические методы:
    │   ├── validate_config() → List[str]
    │   ├── _create_live_aggregator() → MarketAggregatorInterface
    │   ├── _create_demo_aggregator() → MarketAggregatorInterface
    │   └── _create_backtest_aggregator() → MarketAggregatorInterface
    └── Основной метод:
        └── create_market_aggregator() → MarketAggregatorInterface
==================================================================
## 11. Модуль: market_data_utils.py
Типы данных:
market_data_utils.py
├── CusumConfig (dataclass)
│   ├── normalize_window: int = 50
│   ├── eps: float = 0.5
│   ├── h: float = 0.5
│   └── z_to_conf: float = 1.0
│
├── CalculationMetrics (dataclass)
│   ├── symbol: str
│   ├── started_at: datetime
│   ├── completed_at: Optional[datetime]
│   ├── indicators_count: int = 0
│   ├── rows_processed: int = 0
│   ├── errors_count: int = 0
│   └── duration_ms: float = 0.0
│
└── IndicatorConfig (dataclass)
    ├── ema_periods: List[int] = [3, 7, 9, 15, 30]
    ├── price_change_periods: List[int] = [5, 20]
    ├── cmo_period: int = 14
    ├── adx_period: int = 14
    ├── atr_period: int = 14
    ├── macd_periods: Tuple[int, int, int] = (12, 26, 9)
    ├── bb_period: int = 20
    └── vwap_period: int = 96

Основной класс:
└── MarketDataUtils
    ├── Конструктор: market_engine, logger
    ├── Конфигурация:
    │   ├── cusum_config_1m: CusumConfig
    │   ├── cusum_config_5m: CusumConfig
    │   ├── indicator_config: IndicatorConfig
    │   └── cfg: Dict[str, Any]
    ├── Движки БД:
    │   ├── engine: Engine (синхронный)
    │   └── aengine: AsyncEngine (асинхронный)
    ├── Кэши и состояние:
    │   ├── _cache_1m: Dict[str, List[dict]]
    │   ├── _cusum_1m_state: Dict[str, dict]
    │   ├── _metrics: Dict[str, CalculationMetrics]
    │   └── version: str = "1.0.0"
    ├── Управление схемой БД:
    │   ├── ensure_market_schema() → None
    │   ├── _table_columns() → Set[str]
    │   └── _add_missing_columns() → None
    ├── CUSUM расчеты:
    │   ├── calculate_cusum() → Dict[str, pd.Series]
    │   ├── _cusum_online_delta_closes_with_z() → tuple
    │   └── backfill_5m_cusum() → dict
    ├── 5m индикаторы (ML features):
    │   ├── async compute_5m_features_bulk() → int
    │   ├── async compute_5m_features_incremental() → int
    │   ├── async _compute_5m_features_for_last_candle() → int
    │   └── async _get_cusum_signals_1m() → List[dict]
    ├── 1m индикаторы и CUSUM:
    │   ├── async warmup_1m_indicators_and_cusum() → dict
    │   ├── async update_1m_cusum() → dict
    │   ├── async _update_1m_indicators_for_last_candle() → dict
    │   └── async calc_indicators_10s_history() → List[dict]
    ├── Операции с БД:
    │   ├── async upsert_candles_1m() → int
    │   ├── async upsert_candles_5m() → int
    │   ├── async read_candles_1m() → List[dict]
    │   ├── async read_candles_5m() → List[dict]
    │   └── async get_backtest_range() → Tuple[int, int]
    ├── Технические индикаторы (статические методы):
    │   ├── _ema_series() → List[Optional[float]]
    │   ├── _cmo_series() → List[Optional[float]]
    │   ├── _bollinger_bands_features() → Tuple[List, List]
    │   ├── _atr_series() → List[Optional[float]]
    │   ├── _dmi_adx_series() → Tuple[List, List, List, List]
    │   ├── _macd_series() → Tuple[List, List, List]
    │   ├── _calculate_vwap() → List[Optional[float]]
    │   ├── _z_score_series() → List[Optional[float]]
    │   ├── _trend_acceleration_series() → List[Optional[float]]
    │   ├── _volume_ratio_ema3_series() → List[Optional[float]]
    │   ├── _candle_body_ratios() → tuple
    │   ├── _price_vs_vwap_series() → List[Optional[float]]
    │   └── _pattern_features_1m() → tuple
    ├── Вспомогательные методы:
    │   ├── align_to_interval() → int
    │   ├── set_indicator_config() → None
    │   ├── get_metrics() → Optional[CalculationMetrics]
    │   ├── get_statistics() → Dict[str, Any]
    │   ├── _validate_input_bars() → bool
    │   ├── _wilders_smoothing() → pd.Series
    │   ├── _cusum_1m_features() → tuple
    │   ├── _get_last_1m_candles() → dict
    │   └── _calculate_single_10s_indicators() → Optional[dict]
    └── Утилиты временных рядов:
        ├── _roc_series() → List[Optional[float]]
        └── _regime_volatility_series() → List[Optional[float]]
====================================================================

## 12. Модуль: market_history.py
Типы данных:
market_history.py
├── RetryConfig (TypedDict)
│   ├── max_retries: int
│   ├── base_delay: float
│   └── max_delay: float
│
└── CalculationMetrics (dataclass)
    ├── symbol: str
    ├── started_at: datetime
    ├── completed_at: Optional[datetime]
    ├── indicators_count: int = 0
    ├── rows_processed: int = 0
    ├── errors_count: int = 0
    └── duration_ms: float = 0.0

Основные классы:
├── BinanceDataFetcher
│   ├── Конструктор: logger
│   ├── Конфигурация:
│   │   ├── base_url: str = "https://fapi.binance.com/fapi/v1/klines"
│   │   └── retry_config: RetryConfig
│   ├── Основные методы:
│   │   ├── async fetch_candles() → List[Dict]
│   │   └── _process_raw_candles() → List[Dict]
│   └── Управление ошибками:
│       ├── Обработка rate limiting (429)
│       └── Exponential backoff при ошибках
│
├── IndicatorWarmupManager
│   ├── Конструктор: market_data_utils, logger
│   ├── Конфигурация разогрева:
│   │   ├── warmup_config: Dict (1m/5m min_bars, lookback)
│   │   └── required_fields: Dict (проверка индикаторов)
│   ├── Основные методы:
│   │   ├── async warmup_1m_indicators() → bool
│   │   ├── async warmup_5m_indicators() → bool
│   │   ├── async restore_indicator_state() → Optional[Dict]
│   │   ├── async _get_hist_1m_candles() → dict
│   │   └── async _compute_5m_features_historical() → int
│   └── Валидация данных:
│       ├── Проверка непрерывности временных рядов
│       └── Проверка полноты индикаторов
│
└── MarketHistoryManager
    ├── Конструктор: engine, market_data_utils, logger
    ├── Компоненты:
    │   ├── binance_fetcher: BinanceDataFetcher
    │   ├── warmup_manager: IndicatorWarmupManager
    │   └── _buffers: Dict (1m/5m буферы данных)
    ├── Основные методы:
    │   ├── async load_history() → Dict
    │   ├── async _check_existing_data() → Optional[List[Dict]]
    │   ├── async interactive_load() → None
    │   ├── async _warmup_existing_data() → None
    │   ├── async _find_last_processed_5m_candle() → Optional[int]
    │   ├── async _check_existing_data_interactive() → Dict
    │   ├── get_buffer() → Optional[List[Dict]]
    │   ├── get_buffer_stats() → Dict
    │   └── async close() → None
    ├── Утилиты:
    │   ├── _normalize_symbol() → str
    │   └── created_at: datetime (отслеживание времени жизни)
    └── CLI функции:
        └── async main() → None

Интеграция в общий поток данных:

MarketHistoryManager → Binance API
    ↓
Загрузка исторических данных → MarketDataUtils
    ↓
Разогрев индикаторов → Торговая стратегия

Процесс загрузки истории:
BotLifecycleManager.start()
    → _create_history_manager() → MarketHistoryManager
    → load_history() 
        → BinanceDataFetcher.fetch_candles()
        → IndicatorWarmupManager.warmup_*_indicators()
        → MarketDataUtils.upsert_candles_*m()
        → MarketDataUtils.compute_*_features_bulk()

Особенности реализации:
├── Интеллектуальная проверка существующих данных
├── Обработка пропусков в данных (gap detection)
├── Продолжение расчета с последней обработанной свечи
├── Интерактивный режим с прогресс-баром
└── Поддержка как 1m, так и 5m таймфреймов


Интеграция в общий поток данных:

MarketAggregator → MarketDataUtils → База данных
    ↓
Candle события → Индикаторы → Торговая стратегия

DemoMarketAggregatorPhased (режим DEMO)
    → WebSocket Binance Futures
    → Обработка kline 1m/5m
    → MarketDataUtils.upsert_candles_1m/5m()
    → Расчет индикаторов (CUSUM, ML features)
    → Колбэк on_candle_ready

MarketDataUtils (вычислительное ядро)
    → Асинхронные операции с БД
    → Векторные расчеты индикаторов
    → Анти look-ahead нормализация
    → Кэширование и метрики

BacktestMarketAggregatorFixed (режим BACKTEST)
    → Чтение исторических данных из БД
    → Воспроизведение с виртуальным временем
    → Инкрементальный расчет индикаторов
    → Эмуляция рыночных событий

		
BotLifecycleManager.start()
    → _create_components()
        → _create_strategy() → ImprovedQualityTrendSystem
        → _create_main_bot() → EnhancedTradingBot
            → DataProviderFromDB
            → ExecutionEngineFromExchangeManager  
            → MainBotAdapter

EnhancedTradingBot.start()
    → trading_system.analyze_and_trade() → ImprovedQualityTrendSystem.analyze_and_trade()
        → three_level_confirmator.analyze() → ThreeLevelHierarchicalConfirmator.analyze()
            → global_detector.analyze() → MLGlobalTrendDetector.analyze()
            → trend_detector.analyze() → RoleBasedOnlineTrendDetector.analyze()

ThreeLevelHierarchicalConfirmator.analyze()
    → ML-модель (через MLGlobalDetector) или CUSUM fallback (GlobalTrendDetector)
    → CUSUM анализ (RoleBasedOnlineTrendDetector)

EnhancedTradingBot._process_trade_signal()
    → execution_engine.place_order() → ExecutionEngineFromExchangeManager.place_order()
    → position_tracker.add_position() → PositionTracker.add_position()
================================================================	

## 13. Модуль: iqts_standards.py
Типы данных и литералы:
iqts_standards.py
├── Timeframe = Literal["1m", "5m", "15m", "1h"]
├── DirectionLiteral = Literal[1, -1, 0]
├── MarketRegimeLiteral = Literal["strong_uptrend", "weak_uptrend", ...]
├── ReasonCode = Literal["hierarchical_confirmed", "trend_confirmed", ...]
├── SignalIntent = Literal["LONG_OPEN", "LONG_CLOSE", ...]
├── ExecutionMode = Literal["LIVE", "DEMO", "BACKTEST"]
└── ConnectionStatus = Literal["connected", "disconnected", ...]

Основные TypedDict:
├── DetectorMetadata
│   ├── z_score: float
│   ├── cusum_pos: float
│   ├── vola_flag: bool
│   ├── regime: MarketRegimeLiteral
│   └── extra: Dict[str, Any]
│
├── DetectorSignal
│   ├── ok: bool
│   ├── direction: DirectionLiteral
│   ├── confidence: float
│   ├── reason: ReasonCode
│   └── metadata: DetectorMetadata
│
├── TradeSignalIQTS
│   ├── direction: DirectionLiteral
│   ├── entry_price: float
│   ├── position_size: float
│   ├── stop_loss: float
│   ├── take_profit: float
│   ├── confidence: float
│   ├── regime: MarketRegimeLiteral
│   └── metadata: DetectorMetadata
│
├── SystemStatus
│   ├── current_regime: MarketRegimeLiteral
│   ├── regime_confidence: float
│   ├── trades_today: int
│   ├── max_daily_trades: int
│   ├── total_trades: int
│   ├── win_rate: float
│   ├── total_pnl: float
│   └── current_parameters: Dict[str, Any]
│
├── Candle1m
│   ├── symbol: str
│   ├── ts: int
│   ├── open: Decimal, high: Decimal, low: Decimal, close: Decimal
│   ├── volume: Decimal
│   ├── ema3, ema7, ema9, ema15, ema30: Optional[Decimal]
│   ├── cmo14, adx14, plus_di14, minus_di14, atr14: Optional[Decimal]
│   └── cusum, cusum_state, cusum_zscore, ...: Optional[Decimal]
│
└── Конфигурации:
    ├── RiskConfig
    ├── QualityDetectorConfig
    ├── MonitoringConfig
    └── TradingSystemConfig

Протоколы (интерфейсы):
├── DetectorInterface (Protocol)
│   ├── get_required_bars() → Dict[Timeframe, int]
│   ├── async analyze() → DetectorSignal
│   └── validate_data() → bool
│
├── RiskManagerInterface (Protocol)
│   ├── calculate_position_size() → float
│   ├── calculate_dynamic_stops() → tuple[float, float]
│   ├── update_daily_pnl() → None
│   └── should_close_all_positions() → bool
│
├── TradingSystemInterface (Protocol)
│   ├── async analyze_and_trade() → Optional[TradeSignalIQTS]
│   ├── get_system_status() → SystemStatus
│   └── update_performance() → None
│
├── StrategyInterface (Protocol)
│   ├── generate_signal() → Optional[StrategySignal]
│   └── get_required_history() → int
│
├── PositionManagerInterface (Protocol)
│   ├── handle_signal() → Optional[OrderReq]
│   ├── update_on_fill() → None
│   ├── get_open_positions_snapshot() → Dict[str, PositionSnapshot]
│   └── get_stats() → Dict[str, Any]
│
├── ExchangeManagerInterface (Protocol)
│   ├── async place_order() → Dict[str, Any]
│   ├── async cancel_order() → Dict[str, Any]
│   ├── disconnect_user_stream() → None
│   ├── check_stops_on_price_update() → None
│   └── get_active_orders() → List[Dict[str, Any]]
│
├── MarketAggregatorInterface (Protocol)
│   ├── async start_async() → None
│   ├── async wait_for_completion() → None
│   ├── stop() → None
│   ├── shutdown() → None
│   ├── add_event_handler() → None
│   ├── get_stats() → Dict[str, Any]
│   ├── get_connection_state() → NetConnState
│   ├── fetch_recent() → List[Candle1m]
│   └── async fetch_candles() → List[Dict[str, Any]]
│
└── MainBotInterface (Protocol)
    ├── async bootstrap() → None
    ├── handle_candle_ready() → None
    ├── get_stats() → Dict[str, Any]
    ├── get_component_health() → Dict[str, Any]
    └── add_event_handler() → None

Базовые классы:
├── Detector (ABC)
│   ├── Конструктор: name
│   ├── Атрибуты:
│   │   ├── name: str
│   │   ├── logger: logging.Logger
│   │   ├── _last_signal: Optional[DetectorSignal]
│   │   └── _created_at: datetime
│   ├── Абстрактные методы:
│   │   ├── get_required_bars() → Dict[Timeframe, int]
│   │   └── async analyze() → DetectorSignal
│   ├── Конкретные методы:
│   │   ├── validate_data() → bool
│   │   ├── get_status() → Dict[str, Any]
│   │   └── _set_last_signal() → None
│   └── Наследники:
│       ├── RoleBasedOnlineTrendDetector
│       ├── MLGlobalTrendDetector
│       └── GlobalTrendDetector
│
├── Direction (Enum)
│   ├── BUY = "BUY"
│   ├── SELL = "SELL"
│   └── FLAT = "FLAT"
│
└── SignalOut (dataclass)
    ├── signal: int (1, -1, 0)
    ├── strength: float
    ├── reason: ReasonCode
    ├── z: float
    ├── cusum_pos: float
    ├── cusum_neg: float
    └── vola_flag: bool

Утилиты и функции:
├── Нормализация сигналов:
│   ├── normalize_signal() → DetectorSignal
│   ├── normalize_direction() → DirectionLiteral
│   └── normalize_trading_hours() → tuple[int, int]
│
├── Валидация данных:
│   ├── validate_market_data() → bool
│   ├── validate_system_status() → SystemStatus
│   └── safe_nested_getattr() → Any
│
├── Работа с причинами:
│   ├── map_reason() → ReasonCode
│   ├── get_reason_category() → str
│   └── is_successful_reason() → bool
│
├── Управление временем:
│   ├── get_current_timestamp_ms() → int
│   ├── set_simulated_time() → None
│   ├── clear_simulated_time() → None
│   ├── is_simulated_time_enabled() → bool
│   └── create_correlation_id() → str
│
└── Константы:
    ├── FEATURE_NAME_MAP (маппинг фич для БД)
    ├── REQUIRED_OHLCV_COLUMNS
    ├── DEFAULT_TRADING_HOURS
    └── INVALID_DATA

Исключения:
├── BotLifecycleError
└── ComponentInitializationError

Интеграция в систему:

Все модули системы импортируют типы и интерфейсы из iqts_standards.py:

ImprovedQualityTrendSystem → DetectorSignal, TradeSignalIQTS
iqts_detectors → Detector, ReasonCode
multi_timeframe_confirmator → DetectorInterface
trade_bot.py → TradingSystemInterface, SystemStatus
run_bot.py → все Protocol интерфейсы
market_aggregator.py → MarketAggregatorInterface, Candle1m

Особенности:
├── Единый источник истины для всех типов данных
├── Runtime-checkable Protocol для слабой связности
├── Полная типовая безопасность с mypy/pyright
├── Поддержка симуляции времени для бэктестов
└── Совместимость с существующими модулями
=============================================================

## 14. Модуль: exchange_manager.py

Типы данных:
exchange_manager.py
├── ExchangeManagerError (Exception)
│   ├── InvalidOrderError
│   ├── ConnectionError
│   └── ExchangeApiError
│
├── ActiveOrder (dataclass)
│   ├── client_order_id: str
│   ├── symbol: str
│   ├── side: Literal["BUY", "SELL"]
│   ├── type: OrderType
│   ├── qty: Decimal
│   ├── price: Optional[Decimal]
│   ├── stop_price: Optional[Decimal]
│   ├── filled_qty: Decimal = Decimal('0')
│   ├── status: str = "NEW"
│   ├── correlation_id: Optional[str]
│   ├── timestamp_ms: int
│   ├── reduce_only: bool = False
│   ├── exchange_order_id: Optional[str]
│   └── trigger_price: Optional[Decimal]
│
└── ConnectionState (dataclass)
    ├── status: Literal["connected", "disconnected", "connecting", "error"]
    ├── last_heartbeat: Optional[int]
    ├── reconnect_count: int = 0
    ├── error_message: Optional[str]
    └── connected_at: Optional[int]

Основной класс:
└── ExchangeManager
    ├── Конструктор:
    │   ├── base_url: str
    │   ├── on_order_update: Callable[[OrderUpd], None]
    │   ├── trade_log: Optional[Any]
    │   ├── demo_mode: bool = True
    │   ├── is_testnet: bool = False
    │   ├── logger_instance: Optional[logging.Logger]
    │   ├── metrics: Optional[Any]
    │   ├── event_handlers: Optional[List[ExchangeEventHandler]]
    │   ├── ws_url: Optional[str]
    │   ├── execution_mode: str = "DEMO"
    │   └── timeout_seconds: Optional[int]
    │
    ├── Состояние системы:
    │   ├── _lock: threading.RLock
    │   ├── _connection_state: ConnectionState
    │   ├── _active_orders: Dict[str, ActiveOrder]
    │   ├── _orders_by_symbol: Dict[str, Set[str]]
    │   ├── _price_feed: Optional[PriceFeed]
    │   ├── _is_backtest_mode: bool
    │   ├── _use_sync_stop_check: bool
    │   └── _stats: Dict[str, Any]
    │
    ├── Основные публичные методы:
    │   ├── place_order() → Dict[str, Any]
    │   ├── cancel_order() → Dict[str, Any]
    │   ├── get_account_info() → Dict
    │   ├── check_stops_on_price_update() → None
    │   ├── update_stop_order() → None
    │   ├── get_connection_state() → Dict[str, Any]
    │   ├── get_stats() → Dict[str, Any]
    │   ├── get_active_orders() → List[Dict[str, Any]]
    │   ├── reset_for_backtest() → None
    │   ├── connect_user_stream() → None
    │   └── disconnect_user_stream() → None
    │
    ├── Event система:
    │   ├── add_event_handler() → None
    │   ├── remove_event_handler() → None
    │   └── _emit_event() → None
    │
    ├── DEMO/BACKTEST режим:
    │   ├── _place_order_demo() → Dict[str, Any]
    │   ├── _cancel_order_demo() → Dict[str, Any]
    │   ├── _demo_send_working_update() → None
    │   ├── _demo_fill_order() → None
    │   ├── _demo_reject_order() → None
    │   └── _calculate_commission() → Decimal
    │
    ├── STOP мониторинг:
    │   ├── _ensure_stop_monitor_running() → None
    │   ├── _stop_monitor_loop() → None
    │   ├── _check_stop_trigger() → bool
    │   ├── _check_stop_trigger_with_price() → bool
    │   └── _trigger_stop_order() → None
    │
    ├── LIVE режим (заглушки):
    │   ├── _place_order_live() → Dict[str, Any]
    │   └── _cancel_order_live() → Dict[str, Any]
    │
    ├── Валидация и утилиты:
    │   ├── _validate_order_req() → None
    │   ├── _send_order_update() → None
    │   ├── _remove_active_order() → None
    │   └── set_price_feed_callback() → None
    │
    └── Статистика:
        └── _get_uptime_seconds() → int

Ключевые особенности:
├── Поддержка трех режимов: LIVE/DEMO/BACKTEST
├── Унифицированный интерфейс для всех типов ордеров
├── Потокобезопасность через threading.RLock
├── Интеллектуальный STOP мониторинг с синхронной проверкой
├── Реалистичная эмуляция комиссий и slippage
├── Event система для отслеживания состояния
└── Полная интеграция с iqts_standards

Интеграция в систему:

BotLifecycleManager → _create_exchange_manager() → ExchangeManager
    ↓
EnhancedTradingBot → ExecutionEngineFromExchangeManager → ExchangeManager
    ↓
PositionManager → ExchangeManager (для исполнения ордеров)

├── Flow исполнения ордера (ОБНОВЛЕНО):
│   ├── 1. TradeSignalIQTS → TradeSignal (intent-based)
│   ├── 2. PositionManager.handle_signal() → OrderReq
│   ├── 3. ExchangeManager.place_order(OrderReq)
│   ├── 4. Обработка результата с position_id
│   └── 5. Интеграция с TradingLogger

STOP ордера в BACKTEST:
EnhancedTradingBot._manage_existing_positions()
    → check_stops_on_price_update(symbol, current_price)
    → _check_stop_trigger_with_price()
    → _trigger_stop_order() → on_order_update()

Комиссии и slippage:
├── MARKET ордера: 0.1% slippage + 0.04% taker fee
├── LIMIT ордера: 0% slippage + 0.02% maker fee  
├── STOP ордера: 0.01% slippage + 0.04% taker fee
└── BACKTEST режим: минимальный slippage для точности

Статистика и мониторинг:
├── orders_sent/filled/rejected/canceled
├── active_orders_count, active_stops
├── latency_ms, reconnect_count
├── connection_state, uptime_seconds
└── demo_mode, execution_mode
=================================================================

## 15. Модуль: position_manager.py
Типы данных:
position_manager.py
├── PositionManagerError (Exception)
│   ├── InvalidSignalError
│   ├── InsufficientFundsError
│   ├── PositionNotFoundError
│   └── InvalidOrderSizeError
│
├── SymbolMeta (dataclass)
│   ├── symbol: str
│   ├── tick_size: Decimal
│   ├── step_size: Decimal
│   ├── min_notional: Decimal
│   ├── price_precision: int
│   ├── quantity_precision: int
│   ├── leverage_max: int = 20
│   └── leverage_default: int = 10
│
├── PendingOrder (dataclass)
│   ├── client_order_id: str
│   ├── symbol: str
│   ├── side: Literal["BUY", "SELL"]
│   ├── type: str
│   ├── qty: Decimal
│   ├── price: Optional[Decimal]
│   ├── stop_price: Optional[Decimal]
│   ├── reduce_only: bool = False
│   ├── correlation_id: str
│   ├── created_at: int
│   └── metadata: Optional[Dict[str, Any]]
│
└── PMStats (dataclass)
    ├── signals_processed: int = 0
    ├── orders_created: int = 0
    ├── positions_opened: int = 0
    ├── positions_closed: int = 0
    ├── fills_processed: int = 0
    ├── duplicate_signals: int = 0
    ├── invalid_signals: int = 0
    ├── total_realized_pnl: Decimal = Decimal('0')
    └── last_signal_ts: Optional[int] = None

Основной класс:
└── PositionManager
    ├── Конструктор:
    │   ├── symbols_meta: Dict[str, Dict[str, Any]]
    │   ├── db_dsn: str
    │   ├── trade_log: Any
    │   ├── price_feed: Optional[PriceFeed] = None
    │   ├── execution_mode: Literal["LIVE", "DEMO", "BACKTEST"] = "DEMO"
    │   └── db_engine: Optional[Engine] = None
    │
    ├── Основные публичные методы:
    │   ├── handle_signal(signal: TradeSignal) → Optional[OrderReq]
    │   ├── update_on_fill(fill: OrderUpd) → None
    │   ├── get_position(symbol: str) → PositionSnapshot
    │   ├── get_open_positions_snapshot() → Dict[str, PositionSnapshot]
    │   ├── get_stats() → Dict[str, Any]
    │   ├── reset_for_backtest() → None
    │   ├── create_initial_stop(symbol: str, stop_loss_pct: Optional[float]) → Optional[OrderReq]
    │   ├── on_stop_triggered(symbol: str, execution_price: float) → None
    │   ├── is_on_cooldown(symbol: str) → bool
    │   ├── update_peak_pnl(symbol: str, current_price: float, candle_ts: Optional[int]) → None
    │   ├── compute_order_size(symbol: str, risk_ctx: Dict[str, Any]) → Decimal
    │   ├── quantize_qty(symbol: str, qty: Decimal) → Decimal
    │   ├── quantize_price(symbol: str, price: Decimal) → Decimal
    │   ├── is_min_notional_met(symbol: str, qty: Decimal, price: Decimal) → bool
    │   ├── build_entry_order(signal: TradeSignal, side: Literal["BUY", "SELL"]) → Optional[OrderReq]
    │   ├── build_exit_order(signal: TradeSignal, position: PositionSnapshot, reason: str) → Optional[OrderReq]
    │   ├── build_stop_order(signal: TradeSignal, position: PositionSnapshot, new_stop_price: Decimal, is_trailing: bool = False) → Optional[OrderReq]
    │   ├── compute_entry_stop(decision_price: float, side: PositionType, **kwargs) → Optional[float]
    │   ├── compute_trailing_level(current_price: float, side: PositionType, current_stop_price: Optional[float], **kwargs) → Optional[float]
    │   └── get_active_stops() → Dict[str, Dict[str, Any]]
    │
    ├── Внутренние вспомогательные методы:
    │   ├── _validate_signal(signal: TradeSignal) → None
    │   ├── _handle_open_signal(signal: TradeSignal, current_position: PositionSnapshot) → Optional[OrderReq]
    │   ├── _handle_close_signal(signal: TradeSignal, current_position: PositionSnapshot) → Optional[OrderReq]
    │   ├── _handle_wait_signal(signal: TradeSignal, current_position: PositionSnapshot) → Optional[OrderReq]
    │   ├── _process_entry_fill(symbol: str, fill: OrderUpd) → None
    │   ├── _process_exit_fill(symbol: str, fill: OrderUpd, current_position: PositionSnapshot, order_type: Optional[str], is_trailing_stop: bool) → None
    │   ├── _save_position_to_db(position: PositionSnapshot, is_new: bool) → None
    │   ├── _init_position_ids_cache() → None
    │   ├── _get_or_create_state(symbol: str) → Dict[str, Any]
    │   ├── _get_trailing_config(symbol: str) → Dict[str, Any]
    │   ├── _get_current_stop_price(symbol: str) → Optional[float]
    │   ├── _update_active_stop_tracking(symbol: str, stop_info: Dict[str, Any]) → None
    │   ├── _remove_active_stop_tracking(symbol: str) → None
    │   └── _cancel_stops_for_symbol(symbol: str) → None
    │
    ├── Event system:
    │   ├── add_event_handler(handler: EventHandler) → None
    │   ├── remove_event_handler(handler: EventHandler) → None
    │   └── _emit_event(event: PositionEvent) → None
    │
    └── Интеграция:
        ├── BotLifecycleManager → _create_components() → PositionManager
        ├── EnhancedTradingBot → handle_signal() / update_on_fill()
        ├── ExchangeManager → set_exchange_manager() / check_stops_on_price_update()
        └── TradingLogger → create_order_from_req() / update_order_on_upd() / create_position() / close_position()

Интеграция в поток данных:
TradeSignal → PositionManager.handle_signal() → OrderReq → ExchangeManager.place_order()
    ↓
OrderUpd (fill) → PositionManager.update_on_fill() → PositionSnapshot → TradingLogger
    ↓
STOP trigger → PositionManager.on_stop_triggered() → ExchangeManager.check_stops_on_price_update()

Особенности реализации:
├── Единый владелец состояния позиций и PnL
├── Поддержка всех режимов: LIVE / DEMO / BACKTEST
├── Автоматический расчёт размера позиции на основе риск-контекста
├── Встроенный трейлинг-стоп с конфигурируемыми параметрами
├── Квантование цен и объёмов согласно биржевым правилам
├── Управление cooldown между сигналами
├── Полная потокобезопасность через threading.RLock
├── Event-система для оповещения подписчиков
├── Валидация сигналов и защита от дубликатов
├── Автоматическое закрытие стоп-ордеров при выходе из позиции
├── Сохранение всех операций в БД через TradingLogger
└── Поддержка fee_total_usdt для точного расчёта PnL
=================================================================
## 16. Модуль: exit_system.py
Типы данных:
exit_system.py
├── ExitDecision (TypedDict)
│   ├── should_exit: bool
│   ├── reason: str
│   ├── urgency: str ("high" | "medium" | "low")
│   ├── confidence: float
│   ├── details: Dict[str, Any]
│   ├── pnl_pct: float
│   └── type: str ("cascading" | "hard" | "protection" | "combined")
│
└── ExitSignalLevel (внутренний dict из _check_reversal/_check_weakening)
    ├── detected: bool
    ├── confidence: float
    ├── signal_direction: DirectionLiteral
    └── signal_ok: bool

Основные классы:
├── ExitSignalDetector
│   ├── Конструктор: global_timeframe="5m", trend_timeframe="1m"
│   │
│   ├── Компоненты-детекторы:
│   │   ├── global_detector: MLGlobalTrendDetector (с fallback)
│   │   ├── trend_detector: RoleBasedOnlineTrendDetector (роль="trend")
│   │  
│   │
│   ├── Пороги:
│   │   ├── cascading_thresholds: Dict (all_levels_sum, global_hint, lower_tf_min...)
│   │   └── classic_thresholds: Dict (high_global_reversal, medium_entry_rev...)
│   │
│   ├── Ключевые методы:
│   │   ├── async analyze_exit_signal(data, position_direction) → Dict
│   │   ├── _check_reversal(signal, dir) → Dict
│   │   ├── _check_weakening(signal, dir) → Dict
│   │   ├── _check_cascading_reversal(signals, dir) → Dict (ядро каскадной логики)
│   │   └── _combine_exit_signals(signals, dir) → Dict
│   │
│   └── Логика принятия решений:
│       ├── Приоритет 0: Каскадный разворот (10s+1m→5m) → [HIGH]
│       ├── Приоритет 1: Полный разворот 5m → [HIGH]
│       ├── Приоритет 2: Локальное ослабление + глобальный намек → [HIGH/MEDIUM]
│       ├── Приоритет 3: Разворот младших без глобального → [MEDIUM]
│       └── Приоритет 4: Общая взвешенная уверенность → [LOW]
│
└── AdaptiveExitManager
    ├── Конструктор: timeframes (global/trend/entry), trailing/breakeven параметры
    ├── Компоненты:
    │   └── exit_detector: ExitSignalDetector
    ├── Уровни выхода (слоистая архитектура):
    │   ├── LAYER 1: Жёсткие выходы (stop_loss, take_profit, max_hold_time)
    │   ├── LAYER 2: Сигнальные выходы (с каскадной логикой + фильтрация по urgency/PnL)
    │   └── LAYER 3: Защита прибыли (breakeven + trailing stop с пиковым трекингом)
    ├── Основные методы:
    │   ├── async should_exit_position(position, market_data, current_price) → Tuple[bool, str, ExitDecision]
    │   ├── _check_hard_exits(...) → Dict
    │   ├── _check_profit_protection(...) → Dict
    │   └── update_position_stops(position, current_price) → Dict (возвращает {stop_loss, updated, reason})
    ├── Особенности:
    │   ├── Строгая валидация входных данных (position, signal)
    │   ├── Адаптивное max_hold_time (зависит от PnL)
    │   ├── Учёт urgency при принятии решения (HIGH → выход в убыток, MEDIUM → только в прибыль)
    │   ├── Отсутствие side effects в should_exit_position (не двигает SL, только решает)
    │   └── Пик прибыли и tracking состояния хранится внутри position['exit_tracking']
    └── Интеграция:
        └── EnhancedTradingBot использует:
            ├── self.exit_manager.should_exit_position() в _manage_existing_positions()
            └── self.exit_manager.update_position_stops() для отправки SL бирже
=================================================================================
## 17. Модуль: signal_validator.py
структура:

signal_validator.py
├── ValidationResult
│   ├── valid: bool
│   ├── errors: List[str]
│   ├── warnings: List[str]
│   ├── __bool__() → bool
│   └── to_dict() → Dict[str, Any]
│
└── SignalValidator
    ├── Конструктор: strict_mode: bool = False
    │
    ├── Основные методы валидации:
    │   ├── validate_detector_signal(signal: DetectorSignal) → ValidationResult
    │   │   ├── Проверка обязательных полей (ok, direction, confidence)
    │   │   ├── Валидация direction (1, -1, 0)
    │   │   ├── Диапазон confidence (0.0-1.0)
    │   │   └── Согласованность ok/confidence
    │   │
    │   ├── validate_trade_signal_iqts(signal: TradeSignalIQTS) → ValidationResult
    │   │   ├── Обязательные поля (direction, entry_price, position_size, stop_loss, take_profit)
    │   │   ├── Положительность цен и размера позиции
    │   │   ├── Risk/Reward соотношение
    │   │   └── Согласованность стопов с направлением
    │   │
    │   ├── validate_trade_signal(signal: StrategySignal) → ValidationResult
    │   │   ├── Обязательные поля (symbol, intent, decision_price)
    │   │   ├── Допустимые значения intent
    │   │   ├── Корректность цены
    │   │   └── Наличие correlation_id
    │   │
    │   └── validate_order_req(order_req: OrderReq) → ValidationResult
    │       ├── Обязательные поля (client_order_id, symbol, side, type, qty)
    │       ├── Корректность типа ордера
    │       ├── Положительность цен и количества
    │       └── Специфические требования для STOP/LIMIT ордеров
    │
    ├── Комплексная валидация:
    │   └── validate_signal_flow() → Dict[str, ValidationResult]
    │       ├── Валидация всего потока сигналов
    │       └── Возвращает результаты для каждого этапа
    │
    ├── Утилиты проверки:
    │   ├── check_price_sanity() → Tuple[bool, Optional[str]]
    │   ├── check_stop_loss_sanity() → Tuple[bool, Optional[str]]
    │   └── calculate_risk_reward_ratio() → float
    │
    └── Декораторы:
        └── @validate_signal(signal_type: str = 'auto')
            ├── Автоматическая валидация возвращаемых значений
            ├── Поддержка async и sync функций
            └── Логирование ошибок и предупреждений
			
Глобальный доступ:
├── Глобальный валидатор (singleton):
│   ├── _global_validator: Optional[SignalValidator]
│   └── get_validator(strict_mode: bool = False) → SignalValidator
│
└── Функции быстрой проверки:
    ├── quick_validate_detector_signal() → Union[bool, ValidationResult]
    ├── quick_validate_trade_signal_iqts() → bool
    ├── quick_validate_trade_signal() → bool
    └── quick_validate_order_req() → bool
Интеграция в систему:
Точки валидации в потоке данных:
    ThreeLevelHierarchicalConfirmator.analyze()
        → @validate_signal('detector')
        → DetectorSignal → SignalValidator.validate_detector_signal()

    ImprovedQualityTrendSystem.analyze_and_trade()
        → @validate_signal('trade_iqts') 
        → TradeSignalIQTS → SignalValidator.validate_trade_signal_iqts()

    PositionManager.handle_signal()
        → @validate_signal('trade')
        → StrategySignal → SignalValidator.validate_trade_signal()

    ExchangeManager.place_order()
        → @validate_signal('order')
        → OrderReq → SignalValidator.validate_order_req()
=================================================================

## 18 МОДУЛЬ: risk_manager.py

risk_manager.py
└── Централизованное управление рисками
    │
    ├── Типы и константы:
    │   ├── Direction(IntEnum)
    │   │   ├── BUY = 1
    │   │   ├── SELL = -1
    │   │   ├── FLAT = 0
    │   │   ├── .side → "BUY"/"SELL"/"FLAT"
    │   │   └── .opposite() → Direction
    │   │
    │   ├── RiskContext(TypedDict)
    │   │   ├── position_size: float
    │   │   ├── initial_stop_loss: float
    │   │   ├── take_profit: float
    │   │   ├── atr: float
    │   │   ├── stop_atr_multiplier: float
    │   │   ├── tp_atr_multiplier: float
    │   │   ├── volatility_regime: float
    │   │   ├── regime: Optional[str]
    │   │   ├── computed_at_ms: int
    │   │   ├── risk_manager_version: str
    │   │   └── validation_hash: Optional[str]
    │   │
    │   ├── RiskLimits(dataclass)
    │   │   ├── max_portfolio_risk: float = 0.02
    │   │   ├── max_daily_loss: float = 0.05
    │   │   ├── max_position_value_pct: float = 0.30
    │   │   ├── stop_loss_atr_multiplier: float = 2.0
    │   │   ├── take_profit_atr_multiplier: float = 3.0
    │   │   └── atr_periods: int = 14
    │   │
    │   └── RiskManagerInterface(Protocol)
    │
    ├── EnhancedRiskManager(v2.0.0):
    │   │
    │   ├── calculate_risk_context() → RiskContext
    │   │   └── Единая точка входа для расчёта всех риск-параметров
    │   │
    │   ├── calculate_position_size() → float
    │   │   └── [BACKWARD COMPATIBILITY] Старый API
    │   │
    │   ├── calculate_dynamic_stops() → Tuple[float, float]
    │   │   ├── Direction enum вместо строк
    │   │   └── [BACKWARD COMPATIBILITY] Старый API
    │   │
    │   ├── update_daily_pnl(pnl: float) → None
    │   ├── reset_daily_pnl() → None
    │   ├── should_close_all_positions() → bool
    │   └── get_risk_status() → Dict
    │
    └── Вспомогательные функции:
        ├── direction_to_side() → "BUY"/"SELL"/"FLAT"
        ├── side_to_direction() → Direction
        ├── normalize_direction() → Direction
        ├── compute_risk_hash() → str
        └── validate_risk_context() → Tuple[bool, str]

**КЛЮЧЕВЫЕ ИЗМЕНЕНИЯ ОТ improved_algorithm.py:**
✅ Исправлен критический баг: Direction enum вместо сравнения 1 == "BUY"
✅ Добавлен calculate_risk_context() — главный метод вместо раздельных вызовов
✅ Добавлена валидация входных/выходных данных
✅ Добавлена трассируемость (validation_hash, timestamps)
✅ Backward compatibility: старые методы сохранены с deprecation warnings

**МИГРАЦИЯ:**
- improved_algorithm.py → risk_manager.py (2025-11-18)
- Все зависимости обновлены (ImprovedQualityTrendSystem.py)

============================================================================

# 19. Модуль: trading_logger.py
Назначение:
    - Логирование торговых событий: сигналы, сделки, ошибки
    - CRUD операции с БД: позиции, ордера, сделки, символы
    - Асинхронная буферизация записей для высокой производительности
    - Статистика и мониторинг торговой активности

Типы данных:
    trading_logger.py
    ├── SymbolInfo(Dict[str, Any]) — информация о торговом символе
    ├── TradeRecord(Dict[str, Any]) — запись о завершённой сделке
    ├── PositionRecord(Dict[str, Any]) — запись позиции в БД
    ├── OrderRecord(Dict[str, Any]) — запись ордера в БД
    └── AlertCallback = Callable[[str, Dict[str, Any]], None]

Основной класс:
└── TradingLogger
    ├── Конструктор:
    │   ├── market_db_path: str = "trading_databases.sqlite"
    │   ├── trades_db_path: str = "position_trades.sqlite"
    │   ├── on_alert: Optional[AlertCallback]
    │   ├── pool_size: int = 4
    │   ├── enable_async: bool = True
    │   └── logger_instance: Optional[logging.Logger]
    │
    ├── Основные публичные методы:
    │   ├── async on_candle_ready(symbol: str, candle: Candle1m, recent: List[Candle1m]) → None
    │   ├── async on_market_event(event: Dict[str, Any]) → None
    │   ├── on_connection_state_change(state: Dict[str, Any]) → None
    │   ├── get_last_candle(symbol: str) → Optional[Dict[str, Any]]
    │   ├── record_signal(symbol: str, signal_type: str, **kwargs) → None
    │   ├── record_trade(trade_data: Dict[str, Any], **kwargs) → None
    │   ├── record_error(error_data: Dict[str, Any], **kwargs) → None
    │   ├── async record_signal_async(symbol: str, signal_type: str, **kwargs) → None
    │   ├── async record_trade_async(trade_data: Dict[str, Any], **kwargs) → None
    │   ├── async record_error_async(error_data: Dict[str, Any], **kwargs) → None
    │   ├── async flush() → None
    │   ├── log_signal_generated(...) → None (совместимость)
    │   ├── log_position_opened(...) → None (совместимость)
    │   ├── log_position_closed(...) → None (совместимость)
    │   ├── log_order_created(...) → None (совместимость)
    │   ├── log(entry_type: str, data: Dict[str, Any], **kwargs) → None
    │   ├── async log_async(entry_type: str, data: Dict[str, Any], **kwargs) → None
    │   ├── get_symbol_info(symbol: str) → Optional[SymbolInfo]
    │   ├── get_all_symbols() → List[SymbolInfo]
    │   ├── upsert_symbol(symbol_info: SymbolInfo) → None
    │   ├── create_position(position: PositionRecord) → Optional[int]
    │   ├── update_position(position_id: int, updates: Dict[str, Any]) → bool
    │   ├── get_position_by_id(position_id: int) → Optional[PositionRecord]
    │   ├── close_position(position_id: int, exit_price: Decimal, exit_reason: str, ...) → bool
    │   ├── get_orders_for_position(position_id: int, status: str = None, limit: int = None) → List[Dict[str, Any]]
    │   ├── create_order_from_req(order_req: OrderReq, position_id: Optional[int]) → bool
    │   ├── update_order_on_upd(order_upd: OrderUpd) → None
    │   ├── update_order(client_order_id: str, updates: Dict[str, Any]) → bool
    │   ├── get_order(client_order_id: str) → Optional[OrderRecord]
    │   ├── create_trade_record(trade: TradeRecord) → Optional[int]
    │   ├── get_trade_history(symbol: Optional[str], limit: int = 100) → List[TradeRecord]
    │   ├── get_stats() → Dict[str, Any]
    │   ├── get_trading_stats(symbol: Optional[str]) → Dict[str, Any]
    │   ├── get_open_positions_db(symbol: Optional[str]) → List[PositionRecord]
    │   ├── async start_async() → None
    │   ├── async stop_async() → None
    │   └── close() → None
    │
    ├── Внутренние вспомогательные методы:
    │   ├── _normalize_db_value(v) → приводит значения к SQLite-совместимым типам
    │   ├── _normalize_params(data: Dict[str, Any]) → Dict[str, Any]
    │   ├── _write_log_entry(entry_type: str, data: Dict[str, Any], dedup_key: Optional[str]) → None
    │   ├── _check_duplicate(dedup_key: Optional[str]) → bool
    │   ├── _log_sync(entry_type: str, data: Dict[str, Any], **kwargs) → None
    │   ├── async _log_async(entry_type: str, data: Dict[str, Any], **kwargs) → None
    │   ├── async _ensure_async_started() → None
    │   ├── async _start_async_workers() → None
    │   ├── async _stop_async_workers() → None
    │   ├── async _async_worker(queue_type: str, queue: asyncio.Queue) → None
    │   ├── async _enqueue_async(entry_type: str, data: Dict[str, Any]) → None
    │   ├── _create_trade_record_from_position(position, ...) → Optional[int]
    │   └── ensure_trading_schema() → None
    │
    └── Интеграция:
        ├── BotLifecycleManager → _create_trade_log() → TradingLogger
        ├── PositionManager → create_position() / update_position() / close_position()
        ├── EnhancedTradingBot → create_order_from_req() / update_order_on_upd()
        ├── MarketAggregator → on_candle_ready() / on_market_event()
        └── ExchangeManager → on_connection_state_change()

Интеграция в поток данных:
MarketAggregator → on_candle_ready() → _last_candle[symbol]
    ↓
PositionManager → create_position() / close_position() → TradingLogger
    ↓
EnhancedTradingBot → create_order_from_req() → TradingLogger
    ↓
ExchangeManager → update_order_on_upd() → TradingLogger

Особенности реализации:
├── Единый источник истины для всех торговых данных
├── Поддержка как синхронного, так и асинхронного режима записи
├── Автоматическая нормализация Decimal → float для SQLite
├── Встроенная дедупликация записей по dedup_key
├── Асинхронные очереди с пулом воркеров для высокой производительности
├── Совместимые методы log_* для обратной совместимости
├── Автоматическое создание trade record при закрытии позиции
├── Полный набор CRUD операций для positions, orders, trades, symbols
├── Поддержка обратных вызовов on_alert при критических ошибках
└── Автоматическое создание схемы БД при инициализации
 
=============================================================================

## 20. МОДУЛЬ: ml_labeling_tool_v3.py

ml_labeling_tool_v3.py
└── LabelingConfig (dataclass)
    ├── Параметры подключения:
    │   ├── db_engine: Engine | None
    │   ├── symbol: str = "ETHUSDT"
    │   └── timeframe: str = "5m"
    │
    ├── Параметры CUSUM:
    │   ├── cusum_z_threshold: float
    │   ├── cusum_conf_threshold: float
    │   ├── hold_bars: int
    │   └── buffer_bars: int
    │
    ├── Параметры EXTREMUM:
    │   ├── extremum_confirm_bar: int
    │   ├── extremum_window: int
    │   └── min_signal_distance: int
    │
    ├── Параметры PELT_ONLINE:
    │   ├── pelt_window: int
    │   ├── pelt_pen: float
    │   ├── pelt_min_size: int
    │   └── pelt_confirm_bar: int
    │
    ├── Общие параметры:
    │   ├── method: str = "CUSUM_EXTREMUM"  (нормализуется в UPPERCASE + валидация)
    │   ├── fee_percent: float
    │   ├── min_profit_target: float
    │   └── tool: Any = None (обратная ссылка на AdvancedLabelingTool)
    │
    └── __post_init__()
        ├── Инициализация db_engine при необходимости
        └── Базовая валидация конфигурации

└── DataLoader
    ├── Конструктор:
    │   ├── db_engine: Engine | None
    │   ├── symbol: str
    │   ├── timeframe: str
    │   └── config: Optional[LabelingConfig]
    │
    ├── Основные задачи:
    │   ├── _initialize_features() → None
    │   │   └── Формирует список feature_names (совместимый с боевым ботом и trainer'ом)
    │   ├── connect() / disconnect()
    │   ├── load_indicators() → pd.DataFrame
    │   │   ├── Чтение candles_5m из market_data.sqlite
    │   │   └── Загрузка индикаторов (EMA/ADX/BB/VWAP/CUSUM и др.)
    │   ├── validate_data_quality(df) → (bool, Dict)
    │   │   └── Проверка ts, OHLC, пропусков, консистентности индикаторов
    │   ├── load_labeled_data() → pd.DataFrame
    │   │   └── Загрузка существующих меток из labeling_results
    │   ├── safe_correlation_calculation(df, columns) → pd.DataFrame
    │   └── get_data_stats() → Dict[str, Any]
    │       ├── total_candles, period
    │       ├── total_labels, buy_labels, sell_labels
    │       └── avg_confidence
    │
    └── Атрибуты:
        ├── db_engine: Engine
        ├── symbol: str
        ├── timeframe: str
        └── feature_names: List[str]

└── AdvancedLabelingTool
    ├── Конструктор: __init__(config: LabelingConfig)
    │   ├── Нормализация метода разметки:
    │   │   ├── _VALID_METHODS = {"CUSUM", "EXTREMUM", "PELT_ONLINE", "CUSUM_EXTREMUM"}
    │   │   ├── config.method → UPPERCASE
    │   │   └── Фолбэк на "CUSUM_EXTREMUM" при неизвестном методе
    │   ├── Создание DataLoader (ENGINE из MARKET_DB_DSN)
    │   ├── self.engine = data_loader.connect()
    │   ├── self.feature_names = data_loader.feature_names
    │   ├── _ensure_table_exists() → создание/проверка labeling_results
    │   ├── _ensure_training_snapshot_tables()
    │   │   ├── training_dataset
    │   │   ├── training_dataset_meta
    │   │   └── training_feature_importance
    │   └── Установка базового порога PnL (pnl_threshold)
    │
    ├── Работа с snapshot'ами:
    │   ├── _ensure_training_snapshot_tables() → None
    │   │   ├── Проверка структуры training_dataset (PRAGMA table_info)
    │   │   ├── Миграция старых схем (features_json / is_negative / anti_trade_mask)
    │   │   └── Создание таблиц и индексов при отсутствии
    │   ├── _validate_snapshot_frame(df) → None
    │   │   ├── Проверка обязательных колонок (ts, datetime, reversal_label, sample_weight)
    │   │   ├── Проверка диапазона меток (0–3)
    │   │   ├── Удаление NaN/дубликатов
    │   │   └── Подготовка к записи в training_dataset
    │   ├── Формирование snapshot'а в training_dataset + запись метаданных в training_dataset_meta
    │   └── export_feature_importance(...) → int
    │       ├── Нормализация входа (DataFrame / Series / dict / list[(feature, importance)])
    │       ├── Опциональный top_n
    │       └── Запись в training_feature_importance (run_id, model_name, feature, importance, rank)
    │
    ├── Генерация сигналов (labeling_results):
    │   ├── load_data() → pd.DataFrame
    │   │   └── Загрузка свечей 5m + индикаторов для разметки
    │   ├── _cusum_reversals(df) → List[Dict]
    │   │   └── Развороты по CUSUM с учётом z-score и confidence
    │   ├── _extremum_reversals(df) → List[Dict]
    │   │   └── Локальные экстремумы high/low в скользящем окне
    │   ├── _pelt_offline_reversals(df) → List[Dict]
    │   │   └── Change-point detection (ruptures) по всему ряду
    │   ├── _cusum_extremum_hybrid(df) → List[Dict]
    │   │   └── Гибрид CUSUM + EXTREMUM
    │   ├── _get_all_existing_signals() → List[Dict]
    │   │   └── Загрузка уже сохранённых сигналов для symbol
    │   └── merge_conflicting_labels() → int
    │       ├── Обнаружение конфликтующих меток / слабых сигналов
    │       └── Перезапись на HOLD при невыполнении PnL/качества
    │
    ├── PnL-анализ и подтверждение:
    │   ├── _calculate_pnl_to_index(df, entry_idx, signal_type, end_idx)
    │   ├── _calculate_pnl(df, entry_idx, signal_type)
    │   │   └── Учёт комиссии (fee_percent) и min_profit_target
    │   ├── _smart_confirmation_system(df, signal_idx, signal_type) → Dict
    │   │   └── Поиск лучшего бара подтверждения / отмены сигнала
    │   └── _get_confirmation_bars(signal_type) → int
    │
    ├── Аналитика качества разметки:
    │   ├── advanced_quality_analysis() → Dict[str, Any]
    │   │   ├── Подсчёт успеха сигналов и PnL
    │   │   ├── Поиск лучшего метода разметки
    │   │   ├── Детекция проблем (class imbalance, слабые сигналы и пр.)
    │   │   └── Подробный лог/консольный отчёт
    │   ├── detect_label_leakage() → Dict[str, Any]
    │   │   ├── Поиск утечки меток (корреляции признаков с label)
    │   │   └── Диагностика потенциальной data leakage
    │   └── create_cv_splits(...) → Dict
    │       └── Временная кросс-валидация (blocked time series CV)
    │
    ├── Сервисные методы:
    │   ├── configure_settings() → None
    │   ├── show_stats() → None
    │   └── close() → None
    │       ├── Закрытие SQLAlchemy engine
    │       └── Освобождение ресурсов
    │
    └── Точка входа (CLI):
        ├── if __name__ == '__main__':
        │   ├── Создание LabelingConfig (symbol="ETHUSDT")
        │   ├── Инициализация AdvancedLabelingTool
        │   └── Запуск enhanced_main_menu() (интерактивный режим)
        └── Использование:
            ├── Разметка данных (выбор метода)
            ├── Формирование snapshot'ов
            ├── Анализ качества
            └── Экспорт важности признаков
=============================================================================

## 21. МОДУЛЬ: ml_train_global_v2.py

МОДУЛЬ: train_ml_global_v2_windowed.py
ml_train_windowed.py  
└── ModelTrainer (основной класс обучения с окном истории)
    ├── Конструктор: __init__(db_dsn: str, symbol: str, lookback: int = 11)
    │   ├── self.lookback = lookback (по умолчанию 11 баров)
    │   ├── self.base_feature_names = BASE_FEATURE_NAMES (21 признак)
    │   ├── self.feature_names = _generate_windowed_feature_names()  
    │   │   └── генерирует [feat_t0, feat_t-1, ..., feat_t-(lookback-1)] → 231 признака при lookback=11
    │   └── logging: "📊 Создано 231 признаков (21 × 11 баров)"
    │
    ├── Подготовка данных:
    │   ├── prepare_training_data(run_id: str) → (X_df, y_series, w_series)
    │   │   ├── Загрузка из `training_dataset` (пропуск класса 3)
    │   │   ├── Векторизованное оконное преобразование (numpy)
    │   │   ├── Порядок: **[t0, t-1, ..., t-(N-1)]** — самый свежий бар *первый*
    │   │   ├── Автоматическая фильтрация коротких последовательностей
    │   │   └── Возврат pandas DF + Series (метки и веса)
    │   └── _generate_windowed_feature_names() → List[str]
    │
    ├── Обучение модели:
    │   ├── train_model(run_id: str, use_scaler: bool = False) → dict
    │   │   ├── Разделение: train (70%) / val (15%) / test (15%) по времени
    │   │   ├── LightGBM: multiclass, 3 класса (BUY/SELL/HOLD)
    │   │   ├── Callbacks: thermometer + early stopping
    │   │   ├── Diagnostics:
    │   │   │   ├── Анализ утечки (train/val/test accuracy gap)
    │   │   │   ├── precision-min sweep по тесту (от 0.45 до 0.90)
    │   │   │   ├── Sensitivity-анализ (±0.05 по tau, ±0.02 по delta)
    │   │   │   ├── tau curves (spd/f1 vs tau)
    │   │   │   ├── PR-curves, max-proba scatter, feature importance
    │   │   │   └── Агрегированная важность по *базовым* признакам (cmo_14 = сумма cmo_14_t0…t-10)
    │   │   └── Сохранение:
    │   │       ├── .joblib (модель + scaler + metadata + lookback + base_feature_names)
    │   │       ├── _report.json (полный отчёт)
    │   │       ├── _cm_val/test.png (матрицы ошибок)
    │   │       └── _feat_importance_base_aggregated.csv
    │   │
    │   ├── tune_tau_for_spd_range(...) → (tau, stats)
    │   │   └── Оптимизация τ под SPD [8–25] и min precision, с cooldown=2
    │   │
    │   ├── _eval_decision_metrics(...) → dict
    │   │   └── Реалистичная оценка: только активные BUY/SELL после cooldown
    │   │
    │   └── decide(proba, tau, delta=0.08, cooldown_bars=2) → np.ndarray[int]
    │       └── Совместим с `MLGlobalDetector.analyze()` (в боевой системе)
    │
    ├── Диагностика после обучения:
    │   ├── post_training_diagnostics(...)  
    │   │   ├── Гистограммы (TP/FP/FN для BUY/SELL)
    │   │   ├── Scatter max-proba vs true class
    │   │   ├── Precision–Recall curves (one-vs-rest)
    │   │   ├── SPD vs τ / Precision/Recall/F1 vs SPD графики
    │   │   └── Важность признаков (gain + агрегация по лагам)
    │   └── plot_precision_spd_curve(...)  
    │
    └── save_training_report(...)  
        └── JSON + визуализации confusion matrix

├── DataLoader (вспомогательный класс)
│   ├── Конструктор: db_dsn, symbol
│   ├── connect() / close()
│   ├── load_training_dataset(run_id) → pd.DataFrame  
│   │   └── SELECT из `training_dataset` с сортировкой по ts
│   └── load_market_data() → pd.DataFrame  
│       └── чтение `candles_5m` (не используется напрямую в обучении)

├── Утилиты верхнего уровня:
│   ├── _infer_bars_per_day_from_run_id(run_id) → int  
│   ├── thermometer_progress_callback(logger, width=30, period=10)  
│   └── main()
│       ├── Проверка market_data.sqlite
│       ├── Поиск последнего run_id для symbol/timeframe=5m в `training_dataset_meta`
│       ├── Инициализация ModelTrainer
│       ├── Вызов train_model(...)
│       └── Вывод итоговых метрик (val/test accuracy, precision, recall)

├── Глобальные константы:
│   ├── LOOKBACK_WINDOW = 11 (ключевое изменение!)
│   ├── TIMEFRAME_TO_BARS = {...}
│   ├── BASE_FEATURE_NAMES (21 признак, как в ml_train_global_v2.py, но без `cusum_1m_recent`)
│   └── MARKET_DB_DSN = "sqlite:///data/market_data.sqlite"

└── Особенности реализации:
    ├── ✅ Полностью совместим по API с `ml_train_global_v2.py` (кроме интерфейса данных)
    ├── ✅ Векторизованная подготовка окон (ускорение ~5–10×)
    ├── ✅ Чёткий порядок лагов: **t0 — самый свежий бар**, дальше — t-1, t-2...
    ├── ✅ Агрегированная важность: сумма по лагам → базовые признаки (для интерпретируемости)
    ├── ✅ Диагностика утечки: train/val/test gap + accuracy baseline
    ├── ✅ Подбор τ на **тестовом наборе** (честная оценка)
    ├── ✅ Генерация всех отчётов в `models/training_logs/`
    └── 🔁 Модельная упаковка включает `lookback` и `base_feature_names` → `MLGlobalDetector` может корректно реконструировать окно при инференсе
	
	
=== ПОТОК ОБРАБОТКИ 1M СВЕЧИ ===

1. WebSocket Binance → DemoMarketAggregatorPhased._on_kline_1m()
   │ Файл: market_aggregator.py
   │ Описание: Получение raw kline данных из WebSocket
   ↓
2. market_aggregator.py → _kline_to_candle1m()
   │ Преобразование JSON в структуру Candle1m
   │ Поля: symbol, ts, open, high, low, close, volume
   ↓
3. market_aggregator.py → _on_candle_ready_1m()
   │ Проверка финализации свечи (finalized=True)
   │ Добавление в buffer: _symbol_buffers_1m
   ↓
4. market_data_utils.py → upsert_candles_1m()
   │ Сохранение OHLCV в БД (без индикаторов)
   │ Таблица: candles_1m
   │ Async операция через asyncio.create_task()
   ↓
5. market_data_utils.py → update_1m_cusum()
   │ Инкрементальный расчет CUSUM индикаторов:
   │ ├── cusum (накопительная сумма)
   │ ├── cusum_state (-1, 0, 1)
   │ ├── cusum_zscore (нормализованное значение)
   │ ├── cusum_conf (уверенность сигнала)
   │ ├── cusum_pos, cusum_neg (положительная/отрицательная статистика)
   │ └── cusum_reason (текстовое описание)
   ↓
6. market_data_utils.py → _update_1m_indicators_for_last_candle()
   │ Расчет технических индикаторов для последней свечи:
   │ ├── EMA (3, 7, 9, 15, 30)
   │ ├── CMO14 (Chande Momentum Oscillator)
   │ ├── ADX14 (Average Directional Index)
   │ ├── Plus_DI14, Minus_DI14 (Directional Indicators)
   │ └── ATR14 (Average True Range)
   │ Итого: 19 индикаторов на 1m таймфрейме
   ↓
7. market_data_utils.py → upsert_candles_1m() [повторно]
   │ Сохранение свечи С ИНДИКАТОРАМИ в БД
   │ UPDATE существующей записи
   ↓
8. run_bot.py → on_candle_ready(symbol, candle_1m)
   │ Файл: run_bot.py:453 (внутри _create_trade_log)
   │ Колбэк вызывается из market_aggregator
   ↓
9. run_bot.py → MainBotAdapter.handle_candle_ready()
   │ Файл: run_bot.py:1568
   │ Обновление буфера DataProviderFromDB
   │ └── update_from_candle_event(symbol, candle)
   │     └── Добавление в _in_memory_buffer['1m']
   ↓
10. Проверка таймфрейма в on_candle_ready()
    │ interval_ms = ts_close - ts + 1
    │ Если 59_000 <= interval_ms <= 61_000 → это 1m свеча
    │ 
    │ ❌ Для 1m свечи: НЕ запускается ML-анализ
    │ ✅ Только сохранение в БД и обновление buffer
    │ 
    │ ML-анализ запускается ТОЛЬКО на 5m свечах:
    │ if timeframe != '5m':
    │     logger.debug("Skipping analysis for 1m candle (waiting for 5m)")
    │     return
    ↓
11. Использование 1m данных при анализе 5m свечи
    │
    │ Когда приходит 5m свеча → запускается ML-анализ
    │ ↓
    │ DataProviderFromDB.get_market_data(symbol, ['1m', '5m'])
    │ ├── Читает последние 1000 свечей 1m из БД
    │ │   └── SELECT * FROM candles_1m WHERE symbol = ? ORDER BY ts DESC LIMIT 1000
    │ │
    │ ├── Загружает ВСЕ 19 индикаторов на 1m
    │ │   ├── ema3, ema7, ema9, ema15, ema30
    │ │   ├── cmo14, adx14, plus_di14, minus_di14, atr14
    │ │   └── cusum, cusum_state, cusum_zscore, cusum_conf и др.
    │ │
    │ └── Эти данные используются в:
    │     └── RoleBasedOnlineTrendDetector (1m CUSUM детектор)
    │         └── Подтверждение тренда на нижнем таймфрейме
    ↓
12. Агрегация 1m данных для 5m фич (микроструктура)
    │
    │ market_data_utils.py → _get_cusum_signals_1m()
    │ ├── Читает последние 5 свечей 1m (составляющие текущую 5m свечу)
    │ ├── Анализирует CUSUM сигналы внутри 5m периода
    │ └── Создает агрегированные фичи для ML-модели:
    │     ├── cusum_1m_recent (последний CUSUM state)
    │     ├── cusum_1m_quality_score (качество сигнала)
    │     ├── cusum_1m_trend_aligned (согласованность тренда)
    │     ├── cusum_1m_price_move (движение цены)
    │     ├── is_trend_pattern_1m (паттерн тренда)
    │     ├── body_to_range_ratio_1m (размер тела свечи)
    │     └── close_position_in_range_1m (позиция закрытия)
    ↓
    Эти 7 фич добавляются к 5m свече как микроструктурные индикаторы
    └── Используются ML-моделью для повышения точности прогноза
	


=== ОСОБЕННОСТИ ОБРАБОТКИ 1M ===

Кэширование состояния CUSUM:
├── MarketDataUtils._cusum_1m_state: Dict[str, dict]
├── Хранит текущее состояние для каждого символа
└── Позволяет инкрементальный расчет без полной перезагрузки

Асинхронность:
├── Сохранение в БД: asyncio.create_task(upsert_candles_1m())
├── Не блокирует основной поток
└── Расчет индикаторов происходит синхронно

Буферизация:
├── market_aggregator: _symbol_buffers_1m (deque для WebSocket данных)
├── DataProviderFromDB: _in_memory_buffer['1m'] (последние 1000 свечей)
└── Используется для быстрого доступа без обращения к БД

Частота обновлений:
├── WebSocket: ~1 раз в секунду (промежуточные обновления)
├── Финализация свечи: каждые 60 секунд (ts кратно 60000)
└── Расчет индикаторов: только для финализированных свечей

Связь с 5m анализом:
├── 1m индикаторы НЕ используются напрямую в ML-модели
├── Используются для:
│   ├── RoleBasedOnlineTrendDetector (CUSUM на 1m)
│   └── Микроструктурные фичи для 5m свечи
└── ML-модель работает только на 5m таймфрейме

Объем данных:
├── 1 день = 1440 свечей 1m
├── ML анализ использует последние 1000 свечей 1m (~16 часов)
└── Размер в БД: ~50 KB на 1000 свечей (с индикаторами)

=== ИНДИКАТОРЫ НА 1M ТАЙМФРЕЙМЕ ===

Базовые (9):
├── ema3, ema7, ema9, ema15, ema30 (5 EMA)
├── cmo14 (Chande Momentum)
├── adx14 (Trend Strength)
├── plus_di14, minus_di14 (Directional Movement)
└── atr14 (Volatility)

CUSUM детектор (9):
├── cusum (накопительная сумма)
├── cusum_state (состояние: -1/0/1)
├── cusum_zscore (нормализованное значение)
├── cusum_conf (confidence уровень)
├── cusum_reason (причина сигнала)
├── cusum_price_mean (среднее цены)
├── cusum_price_std (стандартное отклонение)
├── cusum_pos (положительная статистика)
└── cusum_neg (отрицательная статистика)

ИТОГО: 19 индикаторов на 1m

ПОТОК ОБРАБОТКИ 5M СВЕЧИ

1. WebSocket Binance → DemoMarketAggregatorPhased._on_kline_5m()
   ↓
2. market_aggregator.py:XXX → _on_candle_ready_5m()
   ↓
3. market_data_utils.py:754 → compute_5m_features_incremental()
   │   └── _compute_5m_features_for_last_candle()
   │       ├── Расчет 26 индикаторов
   │       └── CUSUM анализ
   ↓
4. market_data_utils.py:XXX → upsert_candles_5m() [сохранение в БД]
   ↓
5. run_bot.py:453 → on_candle_ready(symbol, candle_5m)
   ↓
6. run_bot.py:524 → DataProviderFromDB.get_market_data(['1m', '5m'])
   │   ├── _load_from_db() [чтение из БД]
   │   ├── _get_buffered_data() [чтение из buffer]
   │   └── _merge_data_sources() [слияние с проверкой индикаторов]
   ↓
7. ImprovedQualityTrendSystem.generate_signal(market_data)
   ↓
8. MLGlobalDetector.analyze() [ML-модель получает все индикаторы]
===================================================================

=== ЗАВИСИМОСТИ МЕЖДУ МОДУЛЯМИ ===

┌─────────────────────┬───────────────────────────────────────┐
│ Модуль              │ Зависит от                            │
├─────────────────────┼───────────────────────────────────────┤
│ run_bot.py          │ ALL (главный координатор)             │
│ trade_bot.py        │ iqts_standards, ImprovedQualityTrend  │
│ market_aggregator   │ market_data_utils, iqts_standards     │
│ market_data_utils   │ ТОЛЬКО стандартная библиотека + DB    │
│ iqts_detectors      │ ml_detector, iqts_standards           │
│ ml_detector         │ ТОЛЬКО numpy, pandas, lightgbm        │
└─────────────────────┴───────────────────────────────────────┘
================================================================

5. КОНФИГУРАЦИОННЫЕ КОНСТАНТЫ
=== ВАЖНЫЕ КОНСТАНТЫ ===

Таймфреймы:
├── ONE_M_MS = 60_000 (1 минута)
└── FIVE_M_MS = 300_000 (5 минут)

Размеры окон индикаторов:
├── EMA периоды: [3, 7, 9, 15, 30]
├── CMO период: 14
├── ADX период: 14
└── VWAP период: 96

ML-модель:
├── Путь: models/ml_global_5m_lgbm.joblib
├── Min confidence: 0.53
└── Количество фич: 21

Buffer размеры:
├── DataProviderFromDB._buffer_size: 1000
└── market_aggregator buffers: deque(maxlen=...)
	
==================================================================	
 БЫСТРАЯ НАВИГАЦИЯ (номера строк)

run_bot.py:
├── 453: on_candle_ready() [обработчик событий]
├── 1044: DataProviderFromDB [класс]
├── 1109: get_market_data() [чтение данных]
└── 1147: _merge_data_sources() [КРИТИЧНО для индикаторов]

market_data_utils.py:
├── 754: compute_5m_features_bulk()
├── 906: _compute_5m_features_for_last_candle()
└── 562: upsert_candles_5m()

market_history.py:
├── 210: warmup_5m_indicators()
└── 220: compute_5m_features_bulk() [вызов]
=====================================================

### Таблица candles_1m

CREATE TABLE candles_1m (
      symbol      TEXT    NOT NULL,
      ts          INTEGER NOT NULL,
      ts_close    INTEGER,
      open        REAL, high REAL, low REAL, close REAL,
      volume      REAL, count INTEGER, quote REAL,
      finalized   INTEGER DEFAULT 1,
      checksum    TEXT,
      created_ts  INTEGER,
      ema3 REAL,
      ema7 REAL,
      ema9 REAL,
      ema15 REAL,
      ema30 REAL,
      cmo14 REAL,
      adx14 REAL,
      plus_di14 REAL,
      minus_di14 REAL,
      atr14 REAL,
      cusum REAL,
      cusum_state INTEGER,
      cusum_zscore REAL,
      cusum_conf REAL,
      cusum_reason TEXT,
      cusum_price_mean REAL,
      cusum_price_std REAL,
      cusum_pos REAL,
      cusum_neg REAL,
      PRIMARY KEY(symbol, ts)
    )

=======================================================
### Таблица candles_5m

```
sql
CREATE TABLE candles_5m (
      symbol              TEXT    NOT NULL,
      ts                  INTEGER NOT NULL,
      ts_close            INTEGER,
      open REAL, high REAL, low REAL, close REAL,
      volume REAL, count INTEGER, quote REAL,
      finalized INTEGER DEFAULT 1,
      checksum  TEXT,
      created_ts INTEGER,
	  # для ML LightGBM
      price_change_5 REAL,
      trend_momentum_z REAL,
      cmo_14 REAL,
      macd_histogram REAL,
      trend_acceleration_ema7 REAL,
      regime_volatility REAL,
      bb_width REAL,
      adx_14 REAL,
      plus_di_14 REAL,
      minus_di_14 REAL,
      atr_14_normalized REAL,
      volume_ratio_ema3 REAL,
      candle_relative_body REAL,
      upper_shadow_ratio REAL,
      lower_shadow_ratio REAL,
      price_vs_vwap REAL,
      bb_position REAL,
	  # с нижнего TF 1m для ML LightGBM
      cusum_1m_recent INTEGER,
      cusum_1m_quality_score REAL,
      cusum_1m_trend_aligned INTEGER,
      cusum_1m_price_move REAL,
      is_trend_pattern_1m INTEGER,
      body_to_range_ratio_1m REAL,
      close_position_in_range_1m REAL,
	  # CUSUM fallback не используется ML LightGBM
      cusum REAL,
      cusum_state INTEGER,
      cusum_zscore REAL,
      cusum_conf REAL,
      cusum_reason TEXT,
      cusum_price_mean REAL,
      cusum_price_std REAL,
      cusum_pos REAL,
      cusum_neg REAL,
      PRIMARY KEY(symbol, ts)
    )
```
===========================================================	
	
### Таблица orders

```
sql
CREATE TABLE orders (
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
```
=============================================================			
### Таблица positions

```
sql
CREATE TABLE positions (
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
```
====================================================================
### Таблица trades

```
CREATE TABLE trades (
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
```
===================================================================
### positions_risk_audit
```
CREATE TABLE IF NOT EXISTS positions_risk_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    correlation_id TEXT,
    validation_hash TEXT,
    risk_context_json TEXT,  -- ✅ Полный RiskContext
    planned_sl DECIMAL(18,8),
    actual_sl DECIMAL(18,8),
    sl_slippage_pct DECIMAL(18,8),  -- ✅ Процент slippage
    planned_tp DECIMAL(18,8),
    actual_tp DECIMAL(18,8),
    tp_slippage_pct DECIMAL(18,8),
    planned_position_size DECIMAL(18,8),
    actual_position_size DECIMAL(18,8),
    size_slippage_pct DECIMAL(18,8),
    timestamp_ms BIGINT NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions(id)
)
```
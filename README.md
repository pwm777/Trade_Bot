# 🤖 ML-Enhanced Hierarchical Trading System

**Intelligent Cryptocurrency Trading System with LightGBM Trend Detection**

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org/)
[![LightGBM](https://img.shields.io/badge/LightGBM-4.0%2B-green)](https://lightgbm.readthedocs.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## 📋 Содержание

- [О проекте](#о-проекте)
- [Архитектура](#архитектура)
- [Возможности](#возможности)
- [Установка](#установка)
- [Быстрый старт](#быстрый-старт)
- [Структура проекта](#структура-проекта)
- [ML Pipeline](#ml-pipeline)
- [Конфигурация](#конфигурация)
- [Мониторинг](#мониторинг)
- [FAQ](#faq)
- [SQL структура](#sql-структура)
- [Разработка](#разработка)
- [Лицензия](#лицензия)

---

## 🎯 О проекте

**ML-Enhanced Hierarchical Trading System** — это продвинутая система алгоритмической торговли криптовалютами, объединяющая:

- 🧠 **Machine Learning** (LightGBM) для обнаружения трендов на 5-минутном таймфрейме
- 📊 **2-уровневую иерархию** детекторов (5m → 1m)
- 🛡️ **Адаптивный риск-менеджмент** с учетом рыночных режимов
- 🔄 **Автоматический fallback** на CUSUM при недоступности ML-модели
- ⚡ **Real-time inference** (<2 мс на сигнал)

### Ключевые особенности

- ✅ **Production-ready**: Автоматический fallback, обработка ошибок, логирование
- ✅ **Модульная архитектура**: Легко расширять и тестировать
- ✅ **Прозрачность**: Метаданные сигналов показывают, какой детектор работает
- ✅ **Отказоустойчивость**: Система никогда не падает (graceful degradation)

---

## 🏗️ Архитектура

### Общая схема

```
┌─────────────────────────────────────────────────────────────────┐
│                   ImprovedQualityTrendSystem                    │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │         HierarchicalQualityTrendSystem                   │   │
│  │                                                          │   │
│  │  ┌─────────────────────────────────────────────────┐     │   │
│  │  │  TwoLevelHierarchicalConfirmator                │     │   │
│  │  │                                                 │     │   │
│  │  │  5m (Global)  ──→  ML-детектор (LightGBM)       │     │   │
│  │  │                    ↓ fallback                   │     │   │
│  │  │                    CUSUM (GlobalTrendDetector)  │     │   │
│  │  │                                                 │     │   │
│  │  │  1m (Trend)   ──→  CUSUM (RoleBasedDetector)    │     │   │
│  │  └─────────────────────────────────────────────────┘     │   │
│  │                                                          │   │
│  │  Фильтры качества:                                       │   │
│  │  ├─ Время (торговые сессии)                              │   │
│  │  ├─ Объем (адаптивный)                                   │   │
│  │  ├─ Волатильность (адаптивный)                           │   │
│  │  └─ Рыночный режим (из 5m ML/CUSUM)                      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │         EnhancedRiskManager                              │   │
│  │  ├─ Расчет размера позиции                               │   │
│  │  ├─ Динамические Stop Loss / Take Profit                 │   │
│  │  ├─ Контроль дневных лимитов                             │   │
│  │  └─ Адаптация к волатильности                            │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### ML-компонент (5m Global Trend)

```
┌─────────────────────────────────────────────────────────────┐
│              MLGlobalTrendDetector (5m)                     │
│                                                             │
│  ┌────────────────────────────────────────────────────┐     │
│  │  Попытка загрузки ML-модели                        │     │
│  │                                                    │     │
│  │  models/ml_global_5m_lgbm.joblib                   │     │
│  └─────────────────┬────────────────────────────────┬─┘     │
│                    │ Success                 Failed │       │
│                    ▼                                ▼       │
│  ┌─────────────────────────────┐  ┌──────────────────────┐  │
│  │  MLGlobalDetector           │  │  GlobalTrendDetector │  │
│  │  (LightGBM)                 │  │  (CUSUM)             │  │
│  │                             │  │                      │  │
│  │  17 признаков:              │  │  Статистический      │  │
│  │  ├─ Momentum                │  │  алгоритм            │  │
│  │  ├─ RSI, MACD               │  │  (fallback)          │  │
│  │  ├─ Moving Averages         │  │                      │  │
│  │  ├─ Bollinger Bands         │  │                      │  │
│  │  ├─ Volume                  │  │                      │  │
│  │  └─ Candlestick patterns    │  │                      │  │
│  │                             │  │                      │  │
│  │  Inference: ~1-2 мс         │  │  Inference: ~0.5 мс  │  │
│  └─────────────────────────────┘  └──────────────────────┘  │
│                    │                                │       │
│                    └────────────┬───────────────────┘       │
│                                 ▼                           │
│                       DetectorSignal                        │
│                       {                                     │
│                         'ok': bool,                         │
│                         'direction': 'BUY'/'SELL'/'FLAT',   │
│                         'confidence': float,                │
│                         'metadata': {                       │
│                           'detector_type': 'ml' или         │
│                                           'cusum_fallback'  │
│                         }                                   │
│                       }                                     │
└─────────────────────────────────────────────────────────────┘
```

### Структура классов

```
    TradingSystemInterface (IQTS стандарт)
    │
    └── ImprovedQualityTrendSystem (ГЛАВНАЯ СИСТЕМА)
        │
        ├───── TwoLevelHierarchicalConfirmator (Ядро анализа)
        │       │
        │       ├─  MLGlobalDetector (ML модель) (TimeFrame 5m)  
        │       └─  GlobalTrendDetector (Fallback CUSUM)(TimeFrame 5m) 
        │       └── RoleBasedOnlineTrendDetector CUSUM (Локальный тренд (TimeFrame 1m))                            
        │       
        │                                            
        └── EnhancedRiskManager (Риск-менеджмент)
```

Полная архитектурная документация доступна в [ARCHITECTURE.md](ARCHITECTURE.md).

---

## ✨ Возможности

### Machine Learning

- **LightGBM** для классификации разворотов тренда (FLAT / BUY / SELL)
- **17 технических индикаторов** как признаки модели
- **Автоматическая балансировка классов** (`is_unbalance=True`)
- **Early stopping** для защиты от переобучения
- **Feature importance** для интерпретируемости

### Торговая система

- **Агрегирование свечей** (5m → 1m) из потока @AggTrade
- **2-уровневая иерархия** детекторов (5m → 1m)
- **Каскадное подтверждение** сигналов (младший подтверждает старший)
- **Адаптивные фильтры** (объем, волатильность) под рыночный режим
- **Динамический риск-менеджмент** (ATR-based stops)
- **Контроль дневных лимитов** (макс. сделок, макс. потерь)

### Отказоустойчивость

- **Graceful degradation**: ML → CUSUM → пустой сигнал
- **Автоматический fallback** при ошибке загрузки модели
- **Логирование** всех критических событий
- **Потокобезопасность** (Lock для shared state)

### Мониторинг

- **Метаданные сигналов** (detector_type: 'ml' | 'cusum_fallback')
- **Статус детектора** (`get_detector_status()`)
- **Производительность** (`get_performance_report()`)
- **Состояние системы** (`get_system_status()`)

---

## 🚀 Установка

### Требования

- **Python**: 3.8+ ([Latest: Python 3.14.0](https://www.python.org/))
- **OS**: Linux, macOS, Windows
- **RAM**: Минимум 2 GB
- **Disk**: Минимум 500 MB для моделей и данных

### Зависимости

```bash
# Основные
pip install pandas numpy ccxt

# Machine Learning
pip install lightgbm scikit-learn joblib

# Визуализация (опционально)
pip install matplotlib seaborn

# Тестирование (опционально)
pip install pytest pytest-asyncio
```

### Клонирование репозитория

```bash
git clone https://github.com/pwm777/Trade_Bot.git  
cd Trade_Bot
```

### Установка зависимостей

```bash
pip install -r requirements.txt
```

**Файл `requirements.txt`:**
```
pandas>=2.0.0
numpy>=1.24.0
ccxt>=4.0.0
lightgbm>=4.0.0
scikit-learn>=1.3.0
joblib>=1.3.0
matplotlib>=3.7.0
seaborn>=0.12.0
pytest>=7.4.0
pytest-asyncio>=0.21.0
```

---

## ⚡ Быстрый старт

### Шаг 1: Подготовка данных

```bash
# Скачивание исторических данных ETH/USDT 5m (5 месяцев)
python data_preparation.py

# Результат: data/eth_usdt_5m_historical.csv (~40,000 свечей)
```

### Шаг 2: Разметка данных

```bash
# Полуавтоматическая разметка разворотов
python ml_labeling_tool_v3.py \
  --mode semiauto \
  --input data/eth_usdt_5m_historical.csv \
  --output data/eth_usdt_5m_labeled.csv \
  --window 20 \
  --threshold 0.02

# Интерактивный просмотр разворотов, подтверждение/отклонение
# Результат: data/eth_usdt_5m_labeled.csv
```

### Шаг 3: Обучение модели

```bash
# Обучение LightGBM на размеченных данных
python ml_train_global_v2.py \
  --historical data/eth_usdt_5m_historical.csv \
  --labeled data/eth_usdt_5m_labeled.csv \
  --output models/ml_global_5m_lgbm.joblib \
  --report-dir models/training_logs

# Результат:
# - models/ml_global_5m_lgbm.joblib (обученная модель)
# - models/training_logs/training_report.json
# - models/training_logs/confusion_matrix.png
# - models/training_logs/feature_importance.png
```

### Шаг 4: Запуск торговой системы

```python
# run_bot.py

import asyncio
from ImprovedQualityTrendSystem import ImprovedQualityTrendSystem

async def main():
    # Конфигурация
    config = {
        'quality_detector': {
            'global_timeframe': '5m',
            'trend_timeframe': '1m',
            'max_daily_trades': 15
        },
        'risk_management': {
            'max_position_risk': 0.02,
            'max_daily_loss': 0.05,
            'stop_atr_multiplier': 2.0,
            'tp_atr_multiplier': 3.0
        },
        'account_balance': 10000,
        'monitoring_enabled': True
    }
    
    # Инициализация системы
    system = ImprovedQualityTrendSystem(config)
    
    # Получение рыночных данных (ваша реализация)
    market_data = await get_market_data()
    
    # Анализ и генерация сигнала
    signal = await system.analyze_and_trade(market_data)
    
    if signal:
        print(f"Сигнал: {signal['direction']} @ {signal['entry_price']}")
        print(f"Размер: {signal['position_size']}")
        print(f"SL: {signal['stop_loss']}, TP: {signal['take_profit']}")
        print(f"Уверенность: {signal['confidence']:.2%}")
        
        # Выполнение сделки (ваша реализация)
        # execute_trade(signal)

if __name__ == '__main__':
    asyncio.run(main())
```

**Запуск:**
```bash
python run_bot.py
```

---

## 📁 Структура проекта

```
Trade_Bot/
│
├── README.md                           # Этот файл
├── ARCHITECTURE.md                     # Подробная архитектура (v2.0)
├── requirements.txt                    # Зависимости
├── .gitignore
│
├── data/                               # Данные
│   ├── eth_usdt_5m_historical.csv      # Исторические OHLCV
│   └── eth_usdt_5m_labeled.csv         # Размеченные данные
│
├── models/                             # ML-модели
│   ├── ml_global_5m_lgbm.joblib        # Обученная модель
│   └── training_logs/
│       ├── training_report.json        # Метрики обучения
│       ├── confusion_matrix.png        # Confusion matrix
│       └── feature_importance.png      # Важность признаков
│
├── logs/                               # Логи системы
│
├── tests/                              # Тесты (опционально)
│   ├── test_ml_global_detector.py
│   ├── test_ml_global_trend_detector.py
│   └── test_integration.py
│
├── ml_global_detector.py               # ML-детектор (LightGBM)
├── data_preparation.py                 # Скачивание данных с Binance
├── ml_labeling_tool_v3.py              # Инструмент разметки
├── ml_train_global_v2.py               # Обучение модели
│
├── iqts_detectors.py                   # CUSUM-детекторы + обертка ML
├── ImprovedQualityTrendSystem.py       # Главная система
├── multi_timeframe_confirmator.py      # Подтверждение сигналов
├── exit_system.py                      # Система выхода
├── risk_manager.py                     # EnhancedRiskManager
├── trade_bot.py                        # Торговый бот
├── run_bot.py                          # Модуль запуска бота
├── iqts_standards.py                   # Стандарты IQTS
├── market_aggregator.py                # Агрегатор рыночных данных
├── market_data_utils.py                # Утилиты для работы с данными
├── exchange_manager.py                 # Менеджер биржи
├── position_manager.py                 # Менеджер позиций
├── signal_validator.py                 # Валидатор сигналов
├── trading_logger.py                   # Логгер торговли
└── enhanced_monitoring.py              # Мониторинг (опционально)
```

---

## 🧠 ML Pipeline

### 1. Подготовка данных (`data_preparation.py`)

**Что делает:**
- Загружает исторические данные с Binance (через `ccxt`)
- Валидирует данные (дубликаты, пропуски, аномалии)
- Сохраняет в CSV для разметки

**Параметры:**
- `symbol`: Торговая пара (по умолчанию `ETH/USDT`)
- `timeframe`: Таймфрейм (по умолчанию `5m`)
- `months`: Количество месяцев истории (по умолчанию `5`)

### 2. Разметка данных (`ml_labeling_tool_v3.py`)

**Режимы работы:**

#### Полуавтоматический (рекомендуется):
```bash
python ml_labeling_tool_v3.py \
  --mode semiauto \
  --input data/eth_usdt_5m_historical.csv \
  --output data/eth_usdt_5m_labeled.csv
```

**Алгоритм:**
1. Скрипт находит локальные min/max (потенциальные развороты)
2. Проверяет подтверждение (цена изменилась на ±2% после разворота)
3. Вы просматриваете через CLI:
   - `[Y]` — подтвердить разворот
   - `[N]` — отклонить (оставить FLAT)
   - `[S]` — сохранить прогресс
   - `[Q]` — выйти

### 3. Обучение модели (`ml_train_global_v2.py`)

**Параметры LightGBM:**
```python
{
    'objective': 'multiclass',
    'num_class': 3,
    'metric': 'multi_logloss',
    'num_leaves': 31,
    'max_depth': 7,
    'learning_rate': 0.05,
    'is_unbalance': True
}
```

### 4. Использование модели (`ml_global_detector.py`)

**17 признаков (Features):**

| Категория | Признаки |
|-----------|----------|
| **Momentum** |  price_change_20, cmo_14 |
| **MACD** | macd, macd_signal, macd_histogram |
| **ADX/DI** | adx_14, plus_di_14, minus_di_14 |
| **Volatility** | atr_14_normalized, bb_width, bb_position |
| **Volume** | volume_ratio_10 |
| **Candlestick** | body_size, upper_shadow, lower_shadow |
| **Additional** | price_vs_vwap |

---

## ⚙️ Конфигурация

### Основные параметры

```python
config = {
    # Детектор качества
    'quality_detector': {
        'global_timeframe': '5m',      # ML-детектор (или CUSUM fallback)
        'trend_timeframe': '1m',       # CUSUM локальный тренд
        'max_daily_trades': 15,        # Лимит сделок в день
        'min_volume_ratio': 1.3,       # Минимальный объем (1.3x средний)
        'max_volatility_ratio': 1.4    # Максимальная волатильность
    },
    
    # Риск-менеджмент
    'risk_management': {
        'max_position_risk': 0.02,     # Макс. риск на сделку (2%)
        'max_daily_loss': 0.05,        # Макс. дневные потери (5%)
        'atr_periods': 14,
        'stop_atr_multiplier': 2.0,    # Stop Loss = entry ± 2*ATR
        'tp_atr_multiplier': 3.0       # Take Profit = entry ± 3*ATR
    },
    
    # Торговля
    'account_balance': 10000,          # Баланс счета
    'max_daily_trades': 15,            # Лимит сделок
    'max_daily_loss': 0.02,            # Макс. потери (2% от баланса)
    'time_window_hours': (6, 22),     # Торговые часы (UTC)
    
    # Мониторинг
    'monitoring_enabled': True
}
```

---

## 📊 Мониторинг

### Статус системы

```python
system = ImprovedQualityTrendSystem(config)

# Общий статус
status = system.get_system_status()

# Статус детектора (ML или CUSUM)
detector_status = system.quality_detector.get_detector_status()
```

### Логирование

**Примеры логов:**

```
2025-11-12 17:15:38 - INFO - ✅ ML-детектор инициализирован для 5m (fallback: включен)

2025-11-12 17:16:12 - INFO - [ml_5m] Prediction: BUY (confidence=0.78, ok=True)

2025-11-12 17:45:23 - WARNING - ⚠️ ML-модель не найдена. Fallback на CUSUM: включен
```

---

## ❓ FAQ

### 1. Как работает fallback на CUSUM?

**Автоматически:**
1. При инициализации `MLGlobalTrendDetector` пытается загрузить ML-модель
2. Если модель не найдена или ошибка → активируется `GlobalTrendDetector` (CUSUM)
3. Система работает с CUSUM, пока вы не обучите и не загрузите ML-модель
4. Метаданные сигналов показывают: `'detector_type': 'cusum_fallback'`

### 2. Как часто переобучать модель?

**Рекомендуется:** Раз в **1-2 месяца**

**Признаки дрейфа:**
- Падение win rate на >10%
- Увеличение убыточных сделок
- Изменение рыночных условий

### 3. Можно ли использовать для других криптовалют?

**Да!** Нужно только переобучить модель на данных новой монеты.

### 4. Система требует GPU?

**НЕТ!** LightGBM работает на CPU отлично (<2 мс inference).

---

## 🗄️ Структура SQL таблиц market_data.sqlite

### Таблица candles_1m
```sql
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
      -- для CUSUM-детектор
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

### Таблица candles_5m

```sql
CREATE TABLE candles_5m (
      symbol              TEXT    NOT NULL,
      ts                  INTEGER NOT NULL,
      ts_close            INTEGER,
      open REAL, high REAL, low REAL, close REAL,
      volume REAL, count INTEGER, quote REAL,
      finalized INTEGER DEFAULT 1,
      checksum  TEXT,
      created_ts INTEGER,
      -- для ML LightGBM
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
      -- с нижнего TF 1m для ML LightGBM
      cusum_1m_recent INTEGER,
      cusum_1m_quality_score REAL,
      cusum_1m_trend_aligned INTEGER,
      cusum_1m_price_move REAL,
      is_trend_pattern_1m INTEGER,
      body_to_range_ratio_1m REAL,
      close_position_in_range_1m REAL,
      -- CUSUM fallback
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

---

## 📚 Разработка

### Документация

- **ARCHITECTURE.md** - Подробная документация архитектуры v2.0
- Все классы имеют docstrings с описанием параметров и возвращаемых значений

### Тестирование

```bash
# Запуск всех тестов
pytest tests/

# Запуск конкретного теста
pytest tests/test_ml_global_detector.py -v

# С покрытием кода
pytest tests/ --cov=. --cov-report=html
```

### Вклад в проект

1. Fork репозитория
2. Создайте feature branch (`git checkout -b feature/amazing-feature`)
3. Commit изменений (`git commit -m 'Add amazing feature'`)
4. Push в branch (`git push origin feature/amazing-feature`)
5. Откройте Pull Request

---

## 📄 Лицензия

**MIT License**

Copyright (c) 2025 pwm777

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

---

## 📈 Roadmap

### v2.1 (в разработке)
- [ ] Вынос PnLTracker из RiskManager
- [ ] Unit и Integration тесты (pytest)
- [ ] Lint правила для Direction enum
- [ ] Обновление документации API

### v3.0 (планируется)
- [ ] Автоматическое переобучение модели (cron job)
- [ ] Мониторинг дрейфа в production
- [ ] Метрики для Prometheus/Grafana
- [ ] Backtesting engine
- [ ] Web UI для мониторинга

---

**🚀 Разработано с ❤️ для алгоритмической торговли**

**Автор:** [@pwm777](https://github.com/pwm777)  
**Репозиторий:** [github.com/pwm777/Trade_Bot](https://github.com/pwm777/Trade_Bot)
```

# enhanced_monitoring.py

import asyncio
import logging
from typing import Dict, List, Any
from datetime import datetime
import json
from iqts_standards import MonitoringInterface, SystemStatus, TradingSystemInterface


class EnhancedMonitoringSystem(MonitoringInterface):
    """Расширенная система мониторинга с аналитикой производительности"""

    def __init__(self):
        self.performance_metrics = {}
        self.alert_handlers = []
        self.monitoring_active = False

        # Расширенные метрики
        self.regime_performance = {}
        self.hourly_performance = {}
        self.risk_metrics = {}

        # Настройки алертов
        self.alert_thresholds = {
            'max_drawdown': 0.03,
            'daily_loss_limit': 0.05,
            'consecutive_losses': 5,
            'low_win_rate': 0.4,
            'high_volatility': 0.05
        }

    async def send_alert(self, alert: Dict[str, Any]) -> None:
        """Публичный интерфейс отправки уведомлений"""
        await self._send_enhanced_alert(alert)

    async def monitor_enhanced_performance(self, trading_system: TradingSystemInterface):
        """Расширенный мониторинг производительности"""
        self.monitoring_active = True

        while self.monitoring_active:
            try:
                # Сбор метрик
                current_metrics = self._collect_enhanced_metrics(trading_system)

                # Анализ производительности по режимам
                regime_analysis = self._analyze_regime_performance(trading_system)

                # Проверка рисков
                risk_alerts = self._check_risk_conditions(current_metrics)

                # Отправка алертов
                for alert in risk_alerts:
                    await self._send_enhanced_alert(alert)

                # Логирование метрик
                self._log_performance_metrics(current_metrics, regime_analysis)

                await asyncio.sleep(60)  # Проверка каждую минуту

            except Exception as e:
                logging.error(f"Enhanced monitoring error: {e}")
                await asyncio.sleep(60)

    def _collect_enhanced_metrics(self, trading_system: TradingSystemInterface) -> Dict[str, Any]:
        st: SystemStatus = trading_system.get_system_status()
        return {
            "timestamp": datetime.now(),
            "total_trades": st.get("total_trades", 0),
            "win_rate": st.get("win_rate", 0.0),
            "total_pnl": st.get("total_pnl", 0.0),
            "current_regime": st.get("current_regime", "uncertain"),
            "regime_confidence": st.get("regime_confidence", 0.0),
            "trades_today": st.get("trades_today", 0),
            "max_daily_trades": st.get("max_daily_trades", 0),
            "current_parameters": st.get("current_parameters", {}),
        }

    def _analyze_regime_performance(self, trading_system: TradingSystemInterface) -> Dict[str, Any]:
        """Анализ производительности по рыночным режимам"""
        regime_stats = {}

        # Получаем данные из trading system (более безопасный способ)
        try:
            # Попробуем получить performance report если метод существует
            if hasattr(trading_system, 'get_performance_report'):
                perf_report = trading_system.get_performance_report()
                regime_performance = perf_report.get('by_regime', {})

                for regime, stats in regime_performance.items():
                    if stats.get('trades', 0) > 0:
                        regime_stats[regime] = {
                            'total_trades': stats['trades'],
                            'win_rate': stats['win_rate'],
                            'total_pnl': stats['total_pnl'],
                            'avg_pnl': stats['avg_pnl'],
                            'last_trade': datetime.now()  # Заглушка
                        }
            else:
                # Альтернативный способ через прямой доступ (менее надежный)
                regime_performance = getattr(trading_system, 'performance_tracker', {}).get('regime_performance', {})

                for regime, stats in regime_performance.items():
                    if stats.get('trades', 0) > 0:
                        regime_stats[regime] = {
                            'total_trades': stats['trades'],
                            'win_rate': stats['wins'] / stats['trades'],
                            'total_pnl': stats['total_pnl'],
                            'avg_pnl': stats['total_pnl'] / stats['trades'],
                            'last_trade': datetime.now()
                        }

        except Exception as e:
            logging.warning(f"Could not analyze regime performance: {e}")
            regime_stats = {}

        return regime_stats

    def _check_risk_conditions(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Проверка рисковых условий"""
        alerts = []

        # Проверка винрейта
        if metrics['win_rate'] < self.alert_thresholds['low_win_rate'] and metrics['total_trades'] > 20:
            alerts.append({
                'type': 'low_win_rate',
                'severity': 'high',
                'message': f"Low win rate detected: {metrics['win_rate']:.1%}",
                'data': {'win_rate': metrics['win_rate'], 'total_trades': metrics['total_trades']}
            })

        # Проверка режима и уверенности
        if metrics['regime_confidence'] < 0.6:
            alerts.append({
                'type': 'low_regime_confidence',
                'severity': 'medium',
                'message': f"Low regime confidence: {metrics['regime_confidence']:.2f} for {metrics['current_regime']}",
                'data': {'confidence': metrics['regime_confidence'], 'regime': metrics['current_regime']}
            })

        # Проверка лимитов сделок
        if metrics['max_daily_trades'] > 0:
            daily_usage = metrics['trades_today'] / metrics['max_daily_trades']
            if daily_usage > 0.8:
                alerts.append({
                    'type': 'daily_limit_approaching',
                    'severity': 'medium',
                    'message': f"Daily trade limit at {daily_usage:.1%}: {metrics['trades_today']}/{metrics['max_daily_trades']}",
                    'data': {'usage': daily_usage}
                })

        return alerts

    async def _send_enhanced_alert(self, alert: Dict[str, Any]):
        """Отправка расширенных алертов"""
        # Добавляем метку времени
        alert['timestamp'] = datetime.now().isoformat()

        # Отправляем через все зарегистрированные обработчики
        for handler in self.alert_handlers:
            try:
                await handler(alert)
            except Exception as e:
                logging.error(f"Alert handler error: {e}")

    def _log_performance_metrics(self, metrics: Dict[str, Any], regime_analysis: Dict[str, Any]):
        """Логирование метрик производительности"""
        logger = logging.getLogger("enhanced_monitoring")

        logger.info(f"Performance Update: Trades={metrics['total_trades']}, "
                    f"WinRate={metrics['win_rate']:.1%}, "
                    f"PnL=${metrics['total_pnl']:.2f}, "
                    f"Regime={metrics['current_regime']}({metrics['regime_confidence']:.2f})")

        # Детальное логирование по режимам
        for regime, stats in regime_analysis.items():
            if stats['total_trades'] > 0:
                logger.info(f"Regime {regime}: {stats['total_trades']} trades, "
                            f"{stats['win_rate']:.1%} win rate, "
                            f"${stats['avg_pnl']:.2f} avg PnL")

    def _serialize_parameters(self, params: Any) -> Dict[str, Any]:
        """Сериализация параметров для логирования"""
        if not params:
            return {}

        # Безопасная сериализация - работаем с dict или объектом
        if isinstance(params, dict):
            return {k: v for k, v in params.items() if isinstance(v, (str, int, float, bool))}
        else:
            # Для объектов - безопасный доступ к атрибутам
            result = {}
            for attr in ['cusum_threshold', 'min_confidence', 'volume_multiplier',
                         'volatility_filter', 'max_daily_trades', 'cooldown_multiplier']:
                if hasattr(params, attr):
                    value = getattr(params, attr)
                    if isinstance(value, (str, int, float, bool)):
                        result[attr] = value
            return result

    def generate_performance_report(self, trading_system: TradingSystemInterface) -> Dict[str, Any]:
        """Генерация отчета о производительности"""
        try:
            if hasattr(trading_system, 'get_performance_report'):
                perf = trading_system.get_performance_report()
                overall = perf.get('overall', {})
            else:
                # Альтернативный способ
                st = trading_system.get_system_status()
                overall = {
                    'total_trades': st.get('total_trades', 0),
                    'win_rate': st.get('win_rate', 0.0),
                    'total_pnl': st.get('total_pnl', 0.0)
                }
        except Exception as e:
            logging.error(f"Could not get performance data: {e}")
            overall = {'total_trades': 0, 'win_rate': 0.0, 'total_pnl': 0.0}

        regime_analysis = self._analyze_regime_performance(trading_system)

        # Получаем текущее состояние
        try:
            st = trading_system.get_system_status()
            current_regime = st.get('current_regime', 'unknown')
            regime_confidence = st.get('regime_confidence', 0.0)
            trades_today = st.get('trades_today', 0)
            current_params = st.get('current_parameters', {})
        except Exception as e:
            logging.error(f"Could not get current state: {e}")
            current_regime = 'unknown'
            regime_confidence = 0.0
            trades_today = 0
            current_params = {}

        report = {
            'generated_at': datetime.now().isoformat(),
            'overall_performance': {
                'total_trades': overall.get('total_trades', 0),
                'win_rate': overall.get('win_rate', 0.0),
                'total_pnl': overall.get('total_pnl', 0.0),
                'average_pnl_per_trade': overall.get('average_pnl_per_trade', 0.0)
            },
            'regime_breakdown': regime_analysis,
            'current_state': {
                'regime': current_regime,
                'confidence': regime_confidence,
                'trades_today': trades_today,
                'parameters': self._serialize_parameters(current_params)
            }
        }

        return report


# Улучшенные обработчики алертов
async def enhanced_telegram_alert(alert: Dict[str, Any]):
    """Расширенный Telegram обработчик"""
    severity_emojis = {
        'low': '🟡',
        'medium': '🟠',
        'high': '🔴'
    }

    emoji = severity_emojis.get(alert.get('severity', 'medium'), '🟠')
    message = f"{emoji} *Enhanced Trading Alert*\n\n"
    message += f"*Type:* {alert['type']}\n"
    message += f"*Severity:* {alert['severity'].upper()}\n"
    message += f"*Message:* {alert['message']}\n"
    message += f"*Time:* {alert['timestamp']}\n"

    if 'data' in alert:
        message += f"\n*Details:* `{json.dumps(alert['data'], indent=2, default=str)}`"

    print(f"📱 Enhanced Telegram Alert:\n{message}")


async def enhanced_email_alert(alert: Dict[str, Any]):
    """Расширенный Email обработчик"""
    subject = f"Trading Alert: {alert['type']} ({alert['severity'].upper()})"

    body = f"""
    Enhanced Trading System Alert

    Alert Type: {alert['type']}
    Severity: {alert['severity'].upper()}
    Timestamp: {alert['timestamp']}

    Message: {alert['message']}

    Additional Data:
    {json.dumps(alert.get('data', {}), indent=2, default=str)}

    --
    Enhanced Trading Bot v2.0
    """

    print(f"📧 Enhanced Email Alert:\nSubject: {subject}\n{body}")
"""
improved_algorithm.py
"""

from __future__ import annotations
from typing import Dict, Any, Tuple
import numpy as np

from iqts_standards import RiskContext, DetectorSignal, DirectionLiteral


class EnhancedRiskManager:
    """
    Продвинутый риск-менеджер с адаптивными стопами и управлением дневным PnL.
    Интегрирован с IQTS стандартами.
    """

    def __init__(self,
                 max_portfolio_risk: float = 0.02,
                 max_daily_loss: float = 0.05,
                 atr_periods: int = 14,
                 stop_loss_atr_multiplier: float = 2.0,
                 take_profit_atr_multiplier: float = 3.0):
        self.max_portfolio_risk = max_portfolio_risk
        self.max_daily_loss = max_daily_loss
        self.atr_periods = atr_periods
        self.stop_loss_atr_multiplier = stop_loss_atr_multiplier
        self.take_profit_atr_multiplier = take_profit_atr_multiplier

        # Дневной PnL для контроля убытков
        self.daily_pnl = 0.0
        self.account_balance = 100000.0  # Базовый баланс, будет обновляться извне

    # ----------------------------------------------------------
    # ОСНОВНЫЕ МЕТОДЫ РИСКА
    # ----------------------------------------------------------

    def calculate_position_size(self,
                                signal: DetectorSignal,
                                current_price: float,
                                atr: float,
                                account_balance: float) -> float:
        """
        Расчет размера позиции на основе ATR и доли портфеля.
        Учитывает максимальную стоимость позиции (30% от баланса).
        """
        if not signal["ok"] or atr <= 0 or current_price <= 0 or account_balance <= 0:
            return 0.0

        # Обновляем внутренний баланс для расчета дневных потерь
        self.account_balance = account_balance

        risk_per_share = atr * self.stop_loss_atr_multiplier
        if risk_per_share <= 0:
            return 0.0

        max_risk_amount = account_balance * self.max_portfolio_risk
        position_size_by_risk = max_risk_amount / risk_per_share

        # Ограничение по объёму (макс 30% капитала)
        max_position_value = account_balance * 0.3
        position_size_by_value = max_position_value / current_price

        size = min(position_size_by_risk, position_size_by_value)
        return max(0.0, float(size))

    # ----------------------------------------------------------

    def calculate_dynamic_stops(self,
                                *,
                                entry_price: float,
                                direction: DirectionLiteral,
                                atr: float,
                                regime_ctx: RiskContext) -> Tuple[float, float]:
        """
        Расчёт адаптивных стопов и тейков с учётом режима рынка и волатильности.
        """
        if entry_price <= 0 or atr <= 0:
            return entry_price, entry_price  # защита

        # Адаптация к волатильности
        volatility_regime = regime_ctx.get("volatility_regime", 1.0)
        vola_factor = 1.0 / max(volatility_regime, 0.1)  # избегаем деления на 0
        adjustment = np.clip(vola_factor, 0.5, 2.0)

        adjusted_sl_mult = self.stop_loss_atr_multiplier * adjustment
        adjusted_tp_mult = self.take_profit_atr_multiplier * adjustment

        if direction == "BUY":
            stop_loss = entry_price - atr * adjusted_sl_mult
            take_profit = entry_price + atr * adjusted_tp_mult
        elif direction == "SELL":
            stop_loss = entry_price + atr * adjusted_sl_mult
            take_profit = entry_price - atr * adjusted_tp_mult
        else:
            # Неверное направление — возвращаем без изменения
            return entry_price, entry_price

        # Защита от некорректных значений
        stop_loss = max(0.0, stop_loss)
        take_profit = max(0.0, take_profit)

        return float(stop_loss), float(take_profit)

    # ----------------------------------------------------------

    def update_daily_pnl(self, pnl: float) -> None:
        """Обновляет дневной PnL."""
        self.daily_pnl += float(pnl)

    def reset_daily_pnl(self) -> None:
        """Сбрасывает дневной PnL (вызывать в начале дня)."""
        self.daily_pnl = 0.0

    def should_close_all_positions(self) -> bool:
        """Проверяет, достигнут ли лимит дневных потерь."""
        max_daily_loss_amount = self.account_balance * self.max_daily_loss
        return self.daily_pnl <= -max_daily_loss_amount

    def _get_effective_balance(self) -> float:
        """Эффективный баланс для расчета лимитов."""
        return self.account_balance

    def get_risk_status(self) -> Dict[str, Any]:
        """Получение текущего статуса риск-менеджера."""
        max_daily_loss_amount = self.account_balance * self.max_daily_loss
        return {
            'daily_pnl': self.daily_pnl,
            'max_daily_loss_amount': max_daily_loss_amount,
            'max_daily_loss_pct': self.max_daily_loss,
            'should_close_positions': self.should_close_all_positions(),
            'account_balance': self.account_balance,
            'position_risk_pct': self.max_portfolio_risk,
            'stop_loss_atr_multiplier': self.stop_loss_atr_multiplier,
            'take_profit_atr_multiplier': self.take_profit_atr_multiplier
        }

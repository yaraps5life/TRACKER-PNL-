"""
Модели базы данных. SQLAlchemy — это библиотека, которая позволяет
описывать таблицы как обычные Python-классы, а не писать SQL руками.
Каждый класс ниже = одна таблица в PostgreSQL.

v4 — упрощённая модель ввода сделки (дата/актив/направление/R/PnL$/заметка —
всё кроме актива и направления необязательно) + баланс счёта:
у каждого юзера есть стартовый баланс (UserSettings), а текущий баланс
считается как starting_balance + сумма всех pnl_usd по сделкам.
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)

    # КЛЮЧЕВОЕ ПОЛЕ для многопользовательности — у каждой сделки есть владелец.
    # BigInteger, потому что Telegram user_id может быть очень большим числом.
    user_id = Column(BigInteger, index=True, nullable=False)

    # --- обязательные поля формы ввода ---
    asset = Column(String, nullable=False)             # название актива, напр. "BTCUSDT"
    direction = Column(String, nullable=False)          # "long" или "short"

    # --- необязательные поля формы ввода ---
    result_r = Column(Float, nullable=True)             # Risk Reward — только для статистики, не влияет на баланс
    pnl_usd = Column(Float, nullable=True)              # PnL в долларах — то, что двигает баланс счёта
    note = Column(String, nullable=True)                # свободная заметка по сделке
    trade_date = Column(DateTime, nullable=True)         # дата сделки, указанная пользователем (не обязательна)

    # --- старое поле, оставлено для совместимости со старыми записями ---
    risk_percent = Column(Float, nullable=True)

    # --- поля под будущую авто-синхронизацию с Binance ---
    symbol = Column(String, nullable=True)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    size = Column(Float, nullable=True)
    leverage = Column(Float, nullable=True)
    source = Column(String, default="manual")           # "manual" или "auto"
    tags = Column(JSON, default=list)

    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSettings(Base):
    """Настройки конкретного юзера — пока только стартовый баланс счёта,
    от которого считается текущий баланс и % прибыли на дашборде."""
    __tablename__ = "user_settings"

    user_id = Column(BigInteger, primary_key=True, index=True)
    starting_balance = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

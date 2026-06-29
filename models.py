"""
Модели базы данных. SQLAlchemy — это библиотека, которая позволяет
описывать таблицы как обычные Python-классы, а не писать SQL руками.
Каждый класс ниже = одна таблица в PostgreSQL.

v5 — форма ввода сделки: дата (обязательна, по умолчанию сегодня),
актив, направление (long/short), Risk Reward (R), результат как статус
(win/loss/breakeven через поле outcome — без суммы в $), заметка.
Баланс на дашборде теперь = сумма result_r по всем сделкам (без долларов
и без стартового депозита). pnl_usd оставлен в таблице для совместимости
со старыми записями и под будущую авто-синхронизацию, но форма ввода
его больше не использует.
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
    trade_date = Column(DateTime, nullable=False, default=datetime.utcnow)  # дата сделки, по умолчанию сегодня

    # --- необязательные поля формы ввода ---
    result_r = Column(Float, nullable=True)             # Risk Reward — двигает суммарный R на дашборде
    outcome = Column(String, nullable=True)             # "win" | "loss" | "breakeven" — статус без суммы $
    note = Column(String, nullable=True)                # свободная заметка по сделке

    # --- старые поля, оставлены для совместимости со старыми записями ---
    risk_percent = Column(Float, nullable=True)
    pnl_usd = Column(Float, nullable=True)

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
    """Настройки пользователя. starting_balance оставлено для совместимости
    (раньше использовалось для баланса в $), но больше не участвует
    в расчётах на дашборде — там теперь сумма R."""
    __tablename__ = "user_settings"

    user_id = Column(BigInteger, primary_key=True, index=True)
    starting_balance = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FavoriteSymbol(Base):
    """Избранные тикеры пользователя — показываются в выпадающем списке
    "Актив" в форме добавления сделки, чтобы не вводить тикер каждый раз руками."""
    __tablename__ = "favorite_symbols"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, index=True, nullable=False)
    symbol = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

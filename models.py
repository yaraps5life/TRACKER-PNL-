"""
Модели базы данных. SQLAlchemy — это библиотека, которая позволяет
описывать таблицы как обычные Python-классы, а не писать SQL руками.
Каждый класс ниже = одна таблица в PostgreSQL.

v3 — расширение под полноценный UI трекера (дашборд, журнал, аналитика):
добавлены символ/цены/leverage/PnL в долларах/источник/заметка/теги,
старые поля (asset, direction, risk_percent, result_r) оставлены —
они нужны для R-based статистики по твоей ICT/SMT методологии.
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

    # --- старые поля (R-based логика, ICT/SMT методология) ---
    asset = Column(String, nullable=False)
    direction = Column(String, nullable=False)       # "long" или "short"
    risk_percent = Column(Float, nullable=False)
    result_r = Column(Float, nullable=False)

    # --- новые поля под UI дашборда/журнала/аналитики ---
    symbol = Column(String, nullable=True)            # "BTCUSDT" — для отображения в карточках
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    size = Column(Float, nullable=True)                # объём позиции
    leverage = Column(Float, nullable=True)
    pnl_usd = Column(Float, nullable=True)             # PnL в долларах — то, что видно в дашборде
    source = Column(String, default="manual")          # "manual" или "auto" (Binance sync)
    note = Column(String, nullable=True)               # свободная заметка по сделке
    tags = Column(JSON, default=list)                  # ["OB+FVG", "London KZ", "по плану"]

    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

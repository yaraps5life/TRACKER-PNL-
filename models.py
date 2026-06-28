"""
Модели базы данных. SQLAlchemy — это библиотека, которая позволяет
описывать таблицы как обычные Python-классы, а не писать SQL руками.
Каждый класс ниже = одна таблица в PostgreSQL.
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)

    # КЛЮЧЕВОЕ ПОЛЕ для многопользовательности — у каждой сделки есть владелец.
    # BigInteger, потому что Telegram user_id может быть очень большим числом.
    user_id = Column(BigInteger, index=True, nullable=False)

    asset = Column(String, nullable=False)
    direction = Column(String, nullable=False)       # "long" или "short"
    risk_percent = Column(Float, nullable=False)
    result_r = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

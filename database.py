"""
Настройка подключения к PostgreSQL.

DATABASE_URL берётся из переменной окружения — это стандартная практика:
никогда не зашивай пароль от базы прямо в код. На Railway эта переменная
создаётся автоматически, когда подключаешь PostgreSQL к проекту.

Для локального теста — установи PostgreSQL у себя, создай базу, и пропиши
адрес сюда (или через переменную окружения), например:
postgresql://postgres:пароль@localhost:5432/trade_journal
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/trade_journal"
)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """
    Эта функция создаёт подключение к базе для одного запроса
    и автоматически закрывает его после — чтобы не оставлять
    "висящие" подключения, которые со временем исчерпают лимит базы.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

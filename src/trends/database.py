from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import BigInteger, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


DB_PATH = Path(__file__).resolve().parents[2] / "data.db"


class Base(DeclarativeBase):
    pass


class GoogleTrend(Base):
    __tablename__ = "google_trends"

    id: Mapped[int] = mapped_column("trend_id", Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str | None] = mapped_column(String(255), index=True)
    approx_traffic: Mapped[str | None] = mapped_column(String(50))
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class GoogleTrendNews(Base):
    __tablename__ = "google_trends_news"

    id: Mapped[int] = mapped_column("news_id", Integer, primary_key=True, autoincrement=True)
    trend_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("google_trends.trend_id"))
    news_title: Mapped[str | None] = mapped_column(String(500))
    news_url: Mapped[str | None] = mapped_column(String(1000))
    news_source: Mapped[str | None] = mapped_column(String(255))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    news_sentiment: Mapped[str | None] = mapped_column(String(20))


class Stock(Base):
    __tablename__ = "stocks"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    stock_id: Mapped[str] = mapped_column(String(16), primary_key=True, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(Integer)


class EventAnalysis(Base):
    __tablename__ = "event_analysis"
    __table_args__ = (UniqueConstraint("trend_id", "news_id", "stock_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trend_id: Mapped[int] = mapped_column(ForeignKey("google_trends.trend_id"), nullable=False)
    news_id: Mapped[int | None] = mapped_column(ForeignKey("google_trends_news.news_id"))
    stock_id: Mapped[str] = mapped_column(String(16), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    news_sentiment: Mapped[str | None] = mapped_column(String(20))
    return_1d: Mapped[float | None] = mapped_column(Float)
    return_3d: Mapped[float | None] = mapped_column(Float)
    return_5d: Mapped[float | None] = mapped_column(Float)
    return_10d: Mapped[float | None] = mapped_column(Float)
    future_return_1d: Mapped[float | None] = mapped_column(Float)
    future_return_3d: Mapped[float | None] = mapped_column(Float)
    future_return_5d: Mapped[float | None] = mapped_column(Float)
    future_return_10d: Mapped[float | None] = mapped_column(Float)


class ThemeMapping(Base):
    __tablename__ = "theme_mapping"
    __table_args__ = (UniqueConstraint("theme_name", "keyword", "stock_id", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theme_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    stock_id: Mapped[str | None] = mapped_column(String(16), index=True)
    category: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class GoogleTrendsHistory(Base):
    __tablename__ = "google_trends_history"
    __table_args__ = (UniqueConstraint("keyword", "theme_name", "trend_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    theme_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    trend_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    trend_score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


def get_engine(db_path: str | Path = DB_PATH) -> Engine:
    path = Path(db_path).resolve()
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine | None = None) -> Engine:
    db_engine = engine or get_engine()
    Base.metadata.create_all(db_engine)

    news_columns = {column["name"] for column in inspect(db_engine).get_columns("google_trends_news")}
    if "news_sentiment" not in news_columns:
        with db_engine.begin() as connection:
            connection.execute(text("ALTER TABLE google_trends_news ADD COLUMN news_sentiment VARCHAR(20)"))

    return db_engine
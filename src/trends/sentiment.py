from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from trends.database import GoogleTrendNews, init_db


DEFAULT_MODEL = "lxyuan/distilbert-base-multilingual-cased-sentiments-student"


class SentimentAnalyzer:
    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        try:
            from transformers import pipeline
        except ImportError as error:
            raise RuntimeError("情緒分析需安裝 transformers 與 torch：pip install transformers torch") from error
        self._pipeline = pipeline("sentiment-analysis", model=model_name)

    def analyze(self, title: str) -> str:
        result = self._pipeline(title[:512], truncation=True)[0]
        label = str(result["label"]).casefold()
        if "pos" in label or label in {"label_2", "2"}:
            return "Positive"
        if "neg" in label or label in {"label_0", "0"}:
            return "Negative"
        return "Neutral"


def analyze_pending_news(engine: Engine, analyzer: SentimentAnalyzer | None = None, limit: int = 100) -> int:
    db_engine = init_db(engine)
    classifier = analyzer or SentimentAnalyzer()
    with db_engine.connect() as connection:
        pending = connection.execute(
            select(GoogleTrendNews.id, GoogleTrendNews.news_title)
            .where(GoogleTrendNews.news_sentiment.is_(None), GoogleTrendNews.news_title.is_not(None))
            .limit(limit)
        ).all()

    results = [
        {"id": news_id, "news_sentiment": classifier.analyze(title or "")}
        for news_id, title in pending
    ]
    if results:
        with db_engine.begin() as connection:
            connection.execute(update(GoogleTrendNews), results)
    return len(results)
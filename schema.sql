CREATE TABLE IF NOT EXISTS google_trends (
    trend_id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT,
    approx_traffic TEXT,
    published_at DATETIME,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS google_trends_news (
    news_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_id BIGINT REFERENCES google_trends,
    news_title TEXT,
    news_url TEXT,
    news_source TEXT,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    news_sentiment TEXT CHECK (news_sentiment IN ('Positive', 'Neutral', 'Negative') OR news_sentiment IS NULL)
);

CREATE TABLE IF NOT EXISTS stocks (
    date DATE NOT NULL,
    stock_id TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume INTEGER,
    PRIMARY KEY (date, stock_id)
);

CREATE TABLE IF NOT EXISTS event_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_id INTEGER NOT NULL REFERENCES google_trends(trend_id),
    news_id INTEGER REFERENCES google_trends_news(news_id),
    stock_id TEXT NOT NULL,
    event_date DATE NOT NULL,
    keyword TEXT NOT NULL,
    news_sentiment TEXT CHECK (news_sentiment IN ('Positive', 'Neutral', 'Negative') OR news_sentiment IS NULL),
    return_1d REAL,
    return_3d REAL,
    return_5d REAL,
    return_10d REAL,
    future_return_1d REAL,
    future_return_3d REAL,
    future_return_5d REAL,
    future_return_10d REAL,
    UNIQUE (trend_id, news_id, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_google_trends_keyword ON google_trends(keyword);
CREATE INDEX IF NOT EXISTS idx_google_trends_published_at ON google_trends(published_at);
CREATE INDEX IF NOT EXISTS idx_event_analysis_date_stock ON event_analysis(event_date, stock_id);
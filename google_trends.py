import feedparser
import pymysql
from dateutil import parser

# 1. 連接 MariaDB
conn = pymysql.connect(
    host="localhost",
    user="root",
    password="1234",
    database="trends",
    charset="utf8mb4",
    autocommit=True,
)
cursor = conn.cursor()

# 2. 解析 Google Trends RSS
rss_url = "https://trends.google.com/trending/rss?geo=TW"
feed = feedparser.parse(rss_url)

# 3. 讀取並寫入
for entry in feed.entries:
    keyword = entry.get("title", "")

    # 抓取 Google Trends 獨有的搜尋量標籤 (例如: "5000+")
    approx_traffic = entry.get("ht_approx_traffic", "0+")

    # 時間格式轉換
    pub_date_raw = entry.get("published", None)
    published_at = (
        parser.parse(pub_date_raw).strftime("%Y-%m-%d %H:%M:%S")
        if pub_date_raw
        else None
    )

    # 寫入資料庫 (若關鍵字與時間組合已存在則跳過)
    sql = """
        INSERT IGNORE INTO google_trends (keyword, approx_traffic, published_at)
        VALUES (%s, %s, %s)
    """
    cursor.execute(sql, (keyword, approx_traffic, published_at))

cursor.close()
conn.close()
import xml.etree.ElementTree as ET
from dateutil import parser
import pymysql
import requests

# 1. 連接 MariaDB
conn = pymysql.connect(
    host="localhost",
    user="root",
    password="1234",  # 請改為你的資料庫密碼
    database="trends",
    charset="utf8mb4",
    autocommit=True,
)
cursor = conn.cursor()

# 2. 抓取 Google Trends RSS
rss_url = "https://trends.google.com/trending/rss?geo=TW"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
}
response = requests.get(rss_url, headers=headers)
response.encoding = "utf-8"

# 3. 解析 XML 並定義命名空間
root = ET.fromstring(response.text)
namespaces = {
    "ht": "https://trends.google.com/trending/rss",
    "atom": "http://www.w3.org/2005/Atom",
}

# 4. 逐一處理每個熱搜項目 (<item>)
for item in root.findall(".//item"):
    keyword = item.findtext("title", default="").strip()

    # 抓取搜尋熱度 (例如: "5000+")
    traffic_elem = item.find("ht:approx_traffic", namespaces)
    approx_traffic = (
        traffic_elem.text.strip() if traffic_elem is not None else "0+"
    )

    # 抓取發布時間
    pub_date_raw = item.findtext("pubDate")
    published_at = (
        parser.parse(pub_date_raw).strftime("%Y-%m-%d %H:%M:%S")
        if pub_date_raw
        else None
    )

    # A. 寫入主資料表（直接新增紀錄，允許關鍵字隨時間重複出現）
    sql_trend = """
        INSERT INTO google_trends (keyword, approx_traffic, published_at)
        VALUES (%s, %s, %s)
    """
    cursor.execute(sql_trend, (keyword, approx_traffic, published_at))

    # 取得當次寫入產生的 id
    trend_id = cursor.lastrowid

    # B. 精準提取 <ht:news_item> 子項目
    news_items = item.findall("ht:news_item", namespaces)
    for news in news_items:
        news_title = news.findtext(
            "ht:news_item_title", default="", namespaces=namespaces
        ).strip()
        news_url = news.findtext(
            "ht:news_item_url", default="", namespaces=namespaces
        ).strip()
        news_source = news.findtext(
            "ht:news_item_source", default="", namespaces=namespaces
        ).strip()

        # 只要有新聞網址，就寫入新聞子表
        if news_url:
            sql_news = """
                INSERT INTO google_trends_news (trend_id, news_title, news_url, news_source)
                VALUES (%s, %s, %s, %s)
            """
            cursor.execute(
                sql_news, (trend_id, news_title, news_url, news_source)
            )

print("Google Trends 熱搜時間快照與新聞子項目已成功寫入 MariaDB！")

cursor.close()
conn.close()
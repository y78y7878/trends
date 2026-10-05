import xml.etree.ElementTree as ET
from dateutil import parser
import pymysql
import requests
import os
import time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()
print("--- 驗證環境變數 ---")
print("讀取到的帳號:", os.getenv("DB_USER"))
print("讀取到的密碼:", "已讀取" if os.getenv("DB_PASS") else "未讀取(None)")

def fetch_google_trends():
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行 Google Trends 抓取排程...")
    
    username = os.getenv("DB_USER")
    password = os.getenv("DB_PASS")
    
    conn = None
    cursor = None
    
    try:
        # 1. 連接 MariaDB
        conn = pymysql.connect(
            host="localhost",
            user=username,
            password=password,
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
        response = requests.get(rss_url, headers=headers, timeout=10)
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

            traffic_elem = item.find("ht:approx_traffic", namespaces)
            approx_traffic = (
                traffic_elem.text.strip() if traffic_elem is not None else "0+"
            )

            pub_date_raw = item.findtext("pubDate")
            published_at = (
                parser.parse(pub_date_raw).strftime("%Y-%m-%d %H:%M:%S")
                if pub_date_raw
                else None
            )

            # A. 寫入主資料表
            sql_trend = """
                INSERT INTO google_trends (keyword, approx_traffic, published_at)
                VALUES (%s, %s, %s)
            """
            cursor.execute(sql_trend, (keyword, approx_traffic, published_at))
            trend_id = cursor.lastrowid

            # B. 寫入新聞子項目
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

                if news_url:
                    sql_news = """
                        INSERT INTO google_trends_news (trend_id, news_title, news_url, news_source)
                        VALUES (%s, %s, %s, %s)
                    """
                    cursor.execute(
                        sql_news, (trend_id, news_title, news_url, news_source)
                    )

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 排程寫入成功！")

    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 執行時發生錯誤: {e}")
        
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None and conn.open:
            conn.close()

# ========== 排程設定區塊 ==========
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    
    # 將 next_run_time 設為當前時間，排程器一啟動就會自動執行第 1 次，並於 10 分鐘後執行第 2 次
    scheduler.add_job(
        fetch_google_trends, 
        'interval', 
        minutes=10, 
        next_run_time=datetime.now()
    )
    
    scheduler.start()
    print("\n系統提示：APScheduler 自動抓取排程已啟動，每 10 分鐘自動執行一次。")
    print("請保持此視窗開啟，若要停止請按 Ctrl+C。")
    
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("\n排程已順利停止。")
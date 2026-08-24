import xml.etree.ElementTree as ET
from dateutil import parser
import pymysql
import requests
import os
import time
import schedule
from dotenv import load_dotenv
# 新增這兩行測試：
load_dotenv()
print("--- 驗證環境變數 ---")
print("讀取到的帳號:", os.getenv("DB_USER"))
print("讀取到的密碼:", "已讀取" if os.getenv("DB_PASS") else "未讀取(None)")
def fetch_google_trends():
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行 Google Trends 抓取排程...")
    
    username = os.getenv("DB_USER")
    password = os.getenv("DB_PASS")
    
    # 將連線宣告在 try 區塊外，方便 finally 區塊調用關閉
    conn = None
    cursor = None
    
    try:
        # 1. 連接 MariaDB (每次執行排程都建立新連線，避免長時間閒置斷線)
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

            # 抓取搜尋熱度
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
        # 若執行過程中發生錯誤(如網路斷線)，會印出錯誤但不會讓整個腳本崩潰
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 執行時發生錯誤: {e}")
        
    finally:
        # 5. 無論成功或失敗，都確實關閉該次連線釋放資源
        if cursor is not None:
            cursor.close()
        if conn is not None and conn.open:
            conn.close()

# ========== 排程設定區塊 ==========
if __name__ == "__main__":
    # 程式啟動時先立即執行一次
    fetch_google_trends()
    
    # 設定每 10 分鐘執行一次
    schedule.every(10).minutes.do(fetch_google_trends)
    
    print("\n系統提示：自動抓取排程已啟動，每 10 分鐘將自動執行一次。")
    print("請保持此終端機視窗開啟，若要停止請按 Ctrl+C。")
    
    # 進入無限迴圈，讓 schedule 保持檢查並執行任務
    while True:
        schedule.run_pending()
        time.sleep(1) # 暫停 1 秒避免過度消耗 CPU 效能
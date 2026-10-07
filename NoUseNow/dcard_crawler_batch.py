import json
import os
import time
from datetime import datetime
from curl_cffi import requests  # 改用 curl_cffi 繞過 TLS 握手檢測
from dotenv import load_dotenv
import pymysql

# 載入環境變數
load_dotenv()

print("--- 驗證環境變數 ---")
print("讀取到的帳號:", os.getenv("DB_USER"))
print("讀取到的密碼:", "已讀取" if os.getenv("DB_PASS") else "未讀取(None)")

# ================= 參數設定 =================
DB_CONFIG = {
    "host": "127.0.0.1",
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "database": "dcard_db",  # 注意：此處填寫資料庫名稱，非資料表名稱 (dcard_origin)
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

API_URL = "https://www.dcard.tw/service/api/v2/posts?popular=true&limit=30"
# ============================================


def fetch_dcard_posts():
    """使用 curl_cffi 模擬真實 Chrome 瀏覽器的 TLS 指紋與 Header"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始抓取 Dcard API...")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.dcard.tw/f",
    }

    try:
        # impersonate="chrome120" 會完全擬真 Chrome 的網路層行為
        response = requests.get(
            API_URL, headers=headers, impersonate="chrome120", timeout=15
        )
        response.raise_for_status()
        posts = response.json()
        print(f"成功抓取 {len(posts)} 篇文章！")
        return posts
    except Exception as e:
        print(f"抓取失敗: {e}")
        return []


def transform_and_load(posts):
    """轉換與載入 (Transform & Load)：整理資料格式並批次寫入 MariaDB"""
    if not posts:
        return

    try:
        connection = pymysql.connect(**DB_CONFIG)
        cursor = connection.cursor()
    except Exception as e:
        print(f"資料庫連線失敗: {e}")
        return

    data_to_insert = []

    for post in posts:
        created_at_str = post.get("createdAt", "")
        try:
            dt = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            post_created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            post_created_at = None

        topics_json = json.dumps(post.get("topics", []), ensure_ascii=False)

        data_to_insert.append(
            (
                post.get("id"),
                post.get("title", ""),
                post.get("excerpt", ""),
                post.get("forumName", ""),
                post.get("forumAlias", ""),
                post.get("commentCount", 0),
                post.get("likeCount", 0),
                post.get("collectionCount", 0),
                topics_json,
                post_created_at,
            )
        )

    sql = """
        INSERT INTO dcard_origin 
        (id, title, excerpt, forum_name, forum_alias, comment_count, like_count, collection_count, topics, post_created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            comment_count = VALUES(comment_count),
            like_count = VALUES(like_count),
            collection_count = VALUES(collection_count),
            fetched_at = CURRENT_TIMESTAMP;
    """

    try:
        cursor.executemany(sql, data_to_insert)
        connection.commit()
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] 成功將 {cursor.rowcount} 筆操作同步至資料庫。"
        )
    except Exception as e:
        connection.rollback()
        print(f"資料寫入失敗: {e}")
    finally:
        cursor.close()
        connection.close()


def main():
    posts_data = fetch_dcard_posts()
    transform_and_load(posts_data)


if __name__ == "__main__":
    main()
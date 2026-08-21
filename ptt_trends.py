from bs4 import BeautifulSoup
import pymysql
import requests
import os
username = os.getenv("USERNAME")
password = os.getenv("PASSWORD")

# 1. 資料庫連線設定
conn = pymysql.connect(
    host="localhost",
    user=username,
    password=password,
    database="trends",
    charset="utf8mb4",
    autocommit=True,
)
cursor = conn.cursor()

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
}


# 2. 抓取 PTT 八卦版熱門文章
def fetch_ptt_gossiping():
    url = "https://www.ptt.cc/bbs/Gossiping/index.html"
    cookies = {"over18": "1"}  # 跳過 18 歲驗證
    try:
        res = requests.get(url, headers=headers, cookies=cookies, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        articles = soup.find_all("div", class_="r-ent")

        sql = """
            INSERT INTO social_trends (platform, category, title, content_url, author, engagement_score)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        for art in articles:
            title_elem = art.find("div", class_="title").find("a")
            if not title_elem:
                continue  # 跳過已刪除文章

            title = title_elem.text.strip()
            content_url = "https://www.ptt.cc" + title_elem["href"]
            author = art.find("div", class_="author").text.strip()

            # 解析推文數 (爆 = 100, XX = -100)
            nrec = art.find("div", class_="nrec").text.strip()
            score = 0
            if nrec == "爆":
                score = 100
            elif nrec.isdigit():
                score = int(nrec)

            cursor.execute(
                sql, ("ptt", "Gossiping", title, content_url, author, score)
            )
        print("PTT 八卦版熱門文章寫入完成")
    except Exception as e:
        print(f"PTT 抓取失敗: {e}")


# 執行抓取任務
fetch_ptt_gossiping()

cursor.close()
conn.close()
import json
from dateutil import parser
import pymysql
from playwright.sync_api import sync_playwright


def fetch_dcard_trends():
    with sync_playwright() as p:
        print("正在啟動瀏覽器並處理 Cloudflare 驗證...")

        # 啟動 Chromium 並設定防自動化偵測參數
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                " AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        try:
            # 1. 改用 domcontentloaded 避開無窮無盡的背景網路請求
            print("正在前往 Dcard 看板頁面...")
            page.goto("https://www.dcard.tw/f", wait_until="domcontentloaded", timeout=60000)

            # 2. 停頓 4 秒，讓頁面內建的 Cloudflare JS 驗證執行完畢並取得 Cookie
            print("等待 Cloudflare 驗證完成...")
            page.wait_for_timeout(4000)

            # 3. 在已通過驗證的網頁環境下執行同源 fetch
            print("發送內部 API 請求...")
            posts = page.evaluate("""async () => {
                const res = await fetch('https://www.dcard.tw/service/api/v2/posts?popular=true');
                if (!res.ok) {
                    throw new Error('API 回傳失敗，狀態碼: ' + res.status);
                }
                return await res.json();
            }""")

            browser.close()

            # 4. 寫入 MariaDB 資料庫
            conn = pymysql.connect(
                host="localhost",
                user="root",
                password="1234",  # 請替換為你的 MariaDB 密碼
                database="trends",
                charset="utf8mb4",
                autocommit=True,
            )
            cursor = conn.cursor()

            sql = """
                INSERT INTO social_trends 
                (platform, category, title, content_url, author, engagement_score, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """

            for post in posts:
                post_id = post.get("id")
                title = post.get("title", "")
                forum_alias = post.get("forumAlias", "general")
                content_url = f"https://www.dcard.tw/f/{forum_alias}/p/{post_id}"

                # 作者防空值處理
                author_data = post.get("author", {})
                author_name = (
                    author_data.get("displayName")
                    if isinstance(author_data, dict)
                    else ""
                )
                if not author_name:
                    author_name = "匿名"

                # 計算互動分數 (讚數 + 留言數)
                like_count = post.get("likeCount", 0)
                total_comments = post.get(
                    "totalCommentCount", post.get("commentCount", 0)
                )
                engagement_score = like_count + total_comments

                # 時間格式化
                created_at_raw = post.get("createdAt")
                published_at = (
                    parser.parse(created_at_raw).strftime("%Y-%m-%d %H:%M:%S")
                    if created_at_raw
                    else None
                )

                cursor.execute(
                    sql,
                    (
                        "dcard",
                        forum_alias,
                        title,
                        content_url,
                        author_name,
                        engagement_score,
                        published_at,
                    ),
                )

            print(f"成功寫入 {len(posts)} 筆 Dcard 熱門文章快照至 MariaDB！")
            cursor.close()
            conn.close()

        except Exception as e:
            print(f"抓取或寫入失敗: {e}")
            browser.close()


if __name__ == "__main__":
    fetch_dcard_trends()
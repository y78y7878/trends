import time
import json
import requests
import schedule
import pymysql
import os
from bs4 import BeautifulSoup
from datetime import datetime
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ==========================================
# 1. 設定區塊
# ==========================================
load_dotenv()
print("--- 驗證環境變數 ---")
print("讀取到的帳號:", os.getenv("DB_USER"))
print("讀取到的密碼:", "已讀取" if os.getenv("DB_PASS") else "未讀取(None)")
print("讀取到的 Gemini API Key:", "已讀取" if os.getenv("GEMINI_API_KEY") else "未讀取(None)")
username = os.getenv("DB_USER")
password = os.getenv("DB_PASS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DB_CONFIG = {
    'host': '127.0.0.1',
    'user': username,
    'password': password,
    'database': 'trends',
    'charset': 'utf8mb4'
}

# 欲抓取的 PTT 看板清單
BOARDS = ["Gossiping", "Stock", "C_Chat"]

# 初始化 Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 2. PTT 多看板爬蟲模組
# ==========================================
def fetch_board_articles(board):
    url = f"https://www.ptt.cc/bbs/{board}/index.html"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    cookies = {'over18': '1'}
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在抓取 PTT {board} 板文章...")
    articles = []
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        for div in soup.find_all('div', class_='r-ent'):
            title_a = div.find('div', class_='title').find('a')
            if not title_a:
                continue
            
            title = title_a.text.strip()
            # 過濾掉版面公告
            if title.startswith('[公告]') or '公告' in title:
                continue
                
            content_url = "https://www.ptt.cc" + title_a['href']
            author = div.find('div', class_='author').text.strip()
            
            nrec_div = div.find('div', class_='nrec').text.strip()
            engagement_score = 0
            if nrec_div:
                if nrec_div == '爆':
                    engagement_score = 100
                elif nrec_div.startswith('X'):
                    engagement_score = -10
                elif nrec_div.isdigit():
                    engagement_score = int(nrec_div)
            
            articles.append({
                'board': board,
                'title': title,
                'content_url': content_url,
                'author': author,
                'engagement_score': engagement_score
            })
    except Exception as e:
        print(f"❌ 抓取 {board} 板發生錯誤: {e}")
        
    return articles

def fetch_all_target_articles():
    all_articles = []
    for board in BOARDS:
        articles = fetch_board_articles(board)
        all_articles.extend(articles)
        time.sleep(1)  # 爬蟲禮貌間隔
    
    # 為每篇文章設定唯一編號，方便 Gemini 標註
    for idx, article in enumerate(all_articles):
        article['id'] = idx + 1
        
    return all_articles

# ==========================================
# 3. 跨看板 Gemini 前10大綜合趨勢分析
# ==========================================
def analyze_top_10_trends(articles):
    if not articles:
        return []
        
    # 建立簡化版的傳送資料包
    articles_input = [
        {
            "id": a["id"],
            "board": a["board"],
            "title": a["title"],
            "score": a["engagement_score"]
        }
        for a in articles
    ]
    
    prompt = f"""
你是一個專業的台灣社群輿情分析師。
以下是來自 PTT 三大熱門看板（Gossiping 八卦板、Stock 股市板、C_Chat 動漫板）最新的熱門文章列表。

請參考 Google Trends RSS 的趨勢邏輯，綜合考量「討論熱度（score）」以及「主題跨文章/看板的出現頻率」，從中挑選出【前 10 名最熱門的趨勢關鍵字】。

【文章列表】：
{json.dumps(articles_input, ensure_ascii=False, indent=2)}

請嚴格回傳 JSON 格式，結構如下：
{{
  "top_trends": [
    {{
      "rank": 1,
      "keyword": "趨勢關鍵字 (如：台積電、輝達、黑神話悟空、央行升息)",
      "matched_article_ids": [1, 5, 12]
    }}
  ]
}}

注意事項：
1. keyword 必須為精準的實體名詞、公司名、人名、事件名或作品名，排除常見問卦代名詞。
2. matched_article_ids 必須填入所有與該關鍵字相關的文章 id。
3. 最多回傳熱度最高的前 10 個關鍵字。
"""

    try:
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        result = json.loads(response.text)
        return result.get("top_trends", [])
    except Exception as e:
        print(f"❌ Gemini 趨勢彙整分析失敗: {e}")
        return []

# ==========================================
# 4. 資料庫寫入與排程
# ==========================================
def save_trends_to_database(top_trends, articles_dict):
    sql = """
        INSERT INTO ptt_trends_key_words 
        (platform, category, title, keyword, content_url, author, engagement_score, published_at, fetched_at) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    now = datetime.now()
    saved_count = 0
    
    try:
        with pymysql.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                for trend in top_trends:
                    keyword = trend.get('keyword', '')
                    matched_ids = trend.get('matched_article_ids', [])
                    
                    for art_id in matched_ids:
                        article = articles_dict.get(art_id)
                        if article:
                            cursor.execute(sql, (
                                'PTT', 
                                article['board'],          # category (看板名稱)
                                article['title'],          # title (文章原標題)
                                keyword,                   # keyword (AI 萃取出的趨勢關鍵字)
                                article['content_url'], 
                                article['author'], 
                                article['engagement_score'], 
                                now, 
                                now
                            ))
                            saved_count += 1
            conn.commit()
            print(f"✅ 成功寫入資料庫：共寫入 {len(top_trends)} 個熱門趨勢主題（關聯 {saved_count} 篇文章）。")
    except Exception as e:
        print(f"❌ 資料庫寫入失敗: {e}")

def job():
    print(f"\n--- 開始執行 PTT 三大看板 (Gossiping, Stock, C_Chat) 趨勢分析任務 ---")
    
    # 1. 抓取 Gossiping, Stock, C_Chat
    all_articles = fetch_all_target_articles()
    if not all_articles:
        print("未抓取到任何文章。")
        return
        
    articles_by_id = {a['id']: a for a in all_articles}
    print(f"總共抓取到 {len(all_articles)} 篇文章。")
    
    # 2. 交給 Gemini 進行一次性全局趨勢比對
    print("正在傳送至 Gemini API 進行跨板熱度與趨勢分析...")
    top_trends = analyze_top_10_trends(all_articles)
    
    # 3. 寫入資料庫
    save_trends_to_database(top_trends, articles_by_id)
    
    print(f"--- 任務執行完畢 ---")

if __name__ == "__main__":
    job()
    schedule.every(10).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)
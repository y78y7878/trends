import time
import json
import requests
import schedule
import pymysql
import ollama
from bs4 import BeautifulSoup
from datetime import datetime

# ==========================================
# 1. 資料庫連線設定 (請替換為你的 MySQL 實際帳密)
# ==========================================
DB_CONFIG = {
    'host': '127.0.0.1',
    'user': 'root',
    'password': '1234',
    'database': 'trends',
    'charset': 'utf8mb4'
}

# ==========================================
# 2. PTT 熱門文章爬蟲模組
# ==========================================
def fetch_ptt_hot_articles(board="Gossiping"):
    """
    抓取 PTT 指定看板最新一頁的文章列表，並解析出推文數、標題、作者與網址。
    """
    url = f"https://www.ptt.cc/bbs/{board}/index.html"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    cookies = {'over18': '1'} # 繞過八卦板年齡限制
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在抓取 PTT {board} 板文章...")
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        articles = []
        for div in soup.find_all('div', class_='r-ent'):
            title_a = div.find('div', class_='title').find('a')
            if not title_a:
                continue # 略過已刪除文章
            
            title = title_a.text.strip()
            # 略過公告文
            if title.startswith('[公告]'):
                continue
                
            content_url = "https://www.ptt.cc" + title_a['href']
            author = div.find('div', class_='author').text.strip()
            
            # 解析推文數 ( engagement_score )
            nrec_div = div.find('div', class_='nrec').text.strip()
            engagement_score = 0
            if nrec_div:
                if nrec_div == '爆':
                    engagement_score = 100
                elif nrec_div.startswith('X'):
                    engagement_score = -10 # 噓文
                elif nrec_div.isdigit():
                    engagement_score = int(nrec_div)
            
            articles.append({
                'title': title,
                'content_url': content_url,
                'author': author,
                'engagement_score': engagement_score
            })
        return articles
    except Exception as e:
        print(f"爬蟲發生錯誤: {e}")
        return []

# ==========================================
# 3. TAIDE 語意分析與關鍵字萃取模組
# ==========================================
def analyze_keyword_with_taide(article_title):
    """
    呼叫本地端 Ollama (TAIDE模型)，依照 Google Trends 邏輯萃取單一核心實體關鍵字。
    """
    prompt = f"""
    你是一個專業的台灣社群輿情分析師。
    請參考 Google Trends 的邏輯，從以下 PTT 文章標題中，萃取出「一個」最核心、最具代表性的實體名詞作為趨勢關鍵字（例如：特定人名、公司名稱、地名或核心事件名稱）。
    請排除「你我他」等代名詞、「有沒有、的八卦」等常見論壇用語。
    
    文章標題：{article_title}
    
    請務必只回傳 JSON 格式，格式如下：
    {{
        "keyword": "萃取出的關鍵字"
    }}
    """
    
    try:
        response = ollama.chat(
            model='taide',
            messages=[{'role': 'user', 'content': prompt}],
            format='json'
        )
        result_dict = json.loads(response['message']['content'])
        return result_dict.get("keyword", "")
    except Exception as e:
        print(f"TAIDE 分析失敗 ({article_title}): {e}")
        return ""

# ==========================================
# 4. 資料庫寫入模組
# ==========================================
def save_to_database(keyword, article_data):
    """
    將解析完成的關鍵字與原始文章資訊寫入 MySQL 資料庫。
    """
    if not keyword:
        return
        
    sql = """
        INSERT INTO ptt_trends_key_words 
        (platform, category, title, content_url, author, engagement_score, published_at, fetched_at) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    now = datetime.now()
    
    try:
        # 使用 context manager 確保連線自動關閉
        with pymysql.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                # 這裡的 title 欄位存入的是「趨勢關鍵字」(對齊 Google Trends <title> 邏輯)
                # 原始文章的 URL 與作者則作為該趨勢的來源依據
                cursor.execute(sql, (
                    'PTT', 
                    'Gossiping', 
                    keyword, 
                    article_data['content_url'], 
                    article_data['author'], 
                    article_data['engagement_score'],
                    now, # 簡化處理，將發布時間先設為當下
                    now
                ))
            conn.commit()
            print(f"✅ 成功寫入資料庫：趨勢關鍵字 [{keyword}]")
    except Exception as e:
        print(f"資料庫寫入失敗: {e}")

# ==========================================
# 5. 主任務排程邏輯
# ==========================================
def job():
    print(f"\n--- 開始執行 PTT 趨勢分析任務 ---")
    articles = fetch_ptt_hot_articles("Gossiping")
    
    if not articles:
        print("未抓取到文章，任務結束。")
        return

    for article in articles:
        # 進行 AI 關鍵字萃取
        keyword = analyze_keyword_with_taide(article['title'])
        
        # 寫入資料庫
        save_to_database(keyword, article)
        
    print(f"--- 任務執行完畢 ---")

if __name__ == "__main__":
    # 程式啟動時先執行一次
    job()
    
    # 設定每 10 分鐘自動執行一次
    schedule.every(10).minutes.do(job)
    print("\n⏳ 排程已啟動，每 10 分鐘將自動執行一次 (請保持終端機開啟)...")
    
    while True:
        schedule.run_pending()
        time.sleep(1) # 避免迴圈過度佔用 CPU 資源
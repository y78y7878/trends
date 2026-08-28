import google.generativeai as genai
import json

# 設定你的 API Key
genai.configure(api_key="你的_GEMINI_API_KEY")

def analyze_semantics_with_ai(titles):
    """
    使用 AI 進行語意分析，排除語助詞/代名詞，並精準抓取核心關鍵字
    """
    # 為了節省 API 呼叫次數與時間，我們將所有標題打包成一段文字一次送給 AI (Batch Processing)
    combined_titles = "\n".join([f"- {t}" for t in titles])
    
    # 初始化模型 (建議使用 flash 版本，速度快且成本低)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    # 撰寫「系統提示詞 (Prompt)」，這就是賦予它「人腦邏輯」的地方
    prompt = f"""
    你是一個專業的台灣社群輿情分析師。
    請分析以下 PTT 文章標題，並執行以下任務：
    1. 精準判斷語意與正確斷句。
    2. 自動排除「你、我、他」等代名詞，以及「啊、呢、吧、嗎、的、了」等語助詞。
    3. 自動過濾情緒發洩、無意義的閒聊詞彙。
    4. 幫我萃取出這批標題中最核心、最具代表性的「實體關鍵字」（如：公司名稱、人名、專有名詞、具體事件）。
    5. 統計這些核心關鍵字的出現熱度。

    請嚴格遵守以下 JSON 格式回傳，不要包含任何其他文字：
    {{
        "keywords": [
            {{"word": "關鍵字1", "reason": "為什麼選這個詞", "weight": 5}},
            {{"word": "關鍵字2", "reason": "為什麼選這個詞", "weight": 3}}
        ]
    }}

    以下是文章標題列表：
    {combined_titles}
    """

    try:
        # 呼叫 AI 產生回應
        response = model.generate_content(prompt)
        
        # 由於我們要求 JSON 格式，這裡將字串轉回 Python 字典
        # (清理可能出現的 Markdown 標籤如 ```json)
        raw_text = response.text.replace("```json\n", "").replace("```", "").strip()
        result_dict = json.loads(raw_text)
        
        return result_dict["keywords"]
        
    except Exception as e:
        print(f"AI 分析失敗: {e}")
        return []

# 測試執行
if __name__ == "__main__":
    # 模擬爬蟲抓下來的標題（包含很多雜訊、語助詞、代名詞）
    sample_titles = [
        "那個台積電今天怎麼又漲停了啊？",
        "我跟你說，數發部這次修法真的有點扯",
        "有沒有晶豪科主力是他媽的誰的八卦呢",
        "加權指數跌破兩萬點了吧我猜",
        "台積電的法說會你看了嗎？"
    ]
    
    print("正在將標題送交 AI 進行語意分析...")
    ai_keywords = analyze_semantics_with_ai(sample_titles)
    
    print("\n--- AI 萃取的精準關鍵字 ---")
    for item in ai_keywords:
        print(f"🔹 {item['word']} (權重: {item['weight']})")
        print(f"   💡 判斷理由: {item['reason']}")
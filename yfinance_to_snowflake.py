import os
import json
import yfinance as yf
import pandas as pd
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas

# 若您在本地端使用 .env 檔案管理密碼，請取消註解下方兩行
from dotenv import load_dotenv
load_dotenv()

def get_snowflake_connection():
    """使用環境變數建立 Snowflake 連線"""
    return snowflake.connector.connect(
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASS"),
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE_STOCK"),
        schema=os.getenv("SNOWFLAKE_SCHEMA_STOCK")
    )

def fetch_and_sync_stock_data():
    # 嚴選各領域代表性股票 + 加權指數與0050
    target_stocks = [
        "2330.TW", # 半導體: 台積電
        "2382.TW", # 電腦及週邊: 廣達
        "2409.TW", # 光電面板: 友達
        "2317.TW", # 電子零組件: 鴻海
        "2603.TW", # 航運業: 長榮
        "2882.TW", # 金融保險: 國泰金
        "1519.TW", # 重電綠能: 華城
        "2002.TW", # 傳統產業: 中鋼
        "3293.TW", # 遊戲業: 鈊象
        "8450.TW", # 動漫與IP: 霹靂
        "6625.TW", # 娛樂與展演: 必應
        "5278.TW", # 社交平台: 尚凡
        "0050.TW", # 大盤 ETF: 元大台灣50
        "^TWII"    # 台灣加權指數
    ]

    conn = get_snowflake_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("USE DATABASE MARKET_DATA;")
        cursor.execute("USE SCHEMA YFINANCE;")
        for symbol in target_stocks:
            print(f"[+] 開始處理標的: {symbol}")
            ticker = yf.Ticker(symbol)
            
            # -------------------------------------------------------------
            # A. 處理股票基本面
            # -------------------------------------------------------------
            info = ticker.info
            if info:
                info_payload = {
                    'TICKER': symbol,
                    'LONG_NAME': info.get('longName', ''),
                    'SECTOR': info.get('sector', ''),
                    'INDUSTRY': info.get('industry', ''),
                    'MARKET_CAP': info.get('marketCap', None),
                    'RAW_INFO': json.dumps(info)
                }
                
                merge_dim_sql = """
                MERGE INTO DIM_STOCK_INFO AS target
                USING (SELECT 
                    %(TICKER)s AS TICKER, %(LONG_NAME)s AS LONG_NAME, 
                    %(SECTOR)s AS SECTOR, %(INDUSTRY)s AS INDUSTRY, 
                    %(MARKET_CAP)s AS MARKET_CAP, PARSE_JSON(%(RAW_INFO)s) AS RAW_INFO
                ) AS src ON target.TICKER = src.TICKER
                WHEN MATCHED THEN UPDATE SET
                    LONG_NAME = src.LONG_NAME, SECTOR = src.SECTOR,
                    INDUSTRY = src.INDUSTRY, MARKET_CAP = src.MARKET_CAP, 
                    RAW_INFO = src.RAW_INFO, UPDATED_AT = CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT (
                    TICKER, LONG_NAME, SECTOR, INDUSTRY, MARKET_CAP, RAW_INFO
                ) VALUES (
                    src.TICKER, src.LONG_NAME, src.SECTOR, src.INDUSTRY, src.MARKET_CAP, src.RAW_INFO
                );
                """
                cursor.execute(merge_dim_sql, info_payload)

            # -------------------------------------------------------------
            # B. 處理歷史股價 (含還原股價、季節性特徵)
            # -------------------------------------------------------------
            # 設定 auto_adjust=False 以確保同時獲取原始收盤價 (Close) 與還原股價 (Adj Close)
            hist = ticker.history(period="max", auto_adjust=False)
            if not hist.empty:
                hist = hist.reset_index()
                
                # 萃取日期特徵
                date_col = pd.to_datetime(hist['Date'])
                
                hist_df = pd.DataFrame({
                    'TICKER': symbol,
                    'PRICE_DATE': date_col.dt.date,
                    'OPEN_PRICE': hist['Open'],
                    'HIGH_PRICE': hist['High'],
                    'LOW_PRICE': hist['Low'],
                    'CLOSE_PRICE': hist['Close'],
                    # 若 API 回傳不含 Adj Close，則以 Close 代替
                    'ADJ_CLOSE': hist['Adj Close'] if 'Adj Close' in hist.columns else hist['Close'],
                    'VOLUME': hist['Volume'].fillna(0).astype(int),
                    'SEASON_MONTH': date_col.dt.month,
                    'SEASON_QUARTER': date_col.dt.quarter,
                    'INSTITUTIONAL_NET_BUY': None  # 預留空值，等待未來本土API補齊
                })
                
                cursor.execute("CREATE TEMPORARY TABLE TEMP_FACT_STOCK_PRICES LIKE FACT_STOCK_PRICES;")
                write_pandas(conn, hist_df, 'TEMP_FACT_STOCK_PRICES')
                
                merge_prices_sql = """
                MERGE INTO FACT_STOCK_PRICES AS target
                USING TEMP_FACT_STOCK_PRICES AS src
                ON target.TICKER = src.TICKER AND target.PRICE_DATE = src.PRICE_DATE
                WHEN MATCHED THEN UPDATE SET
                    OPEN_PRICE = src.OPEN_PRICE, HIGH_PRICE = src.HIGH_PRICE,
                    LOW_PRICE = src.LOW_PRICE, CLOSE_PRICE = src.CLOSE_PRICE,
                    ADJ_CLOSE = src.ADJ_CLOSE, VOLUME = src.VOLUME,
                    SEASON_MONTH = src.SEASON_MONTH, SEASON_QUARTER = src.SEASON_QUARTER
                WHEN NOT MATCHED THEN INSERT (
                    TICKER, PRICE_DATE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, 
                    ADJ_CLOSE, VOLUME, SEASON_MONTH, SEASON_QUARTER, INSTITUTIONAL_NET_BUY
                ) VALUES (
                    src.TICKER, src.PRICE_DATE, src.OPEN_PRICE, src.HIGH_PRICE, src.LOW_PRICE, src.CLOSE_PRICE, 
                    src.ADJ_CLOSE, src.VOLUME, src.SEASON_MONTH, src.SEASON_QUARTER, src.INSTITUTIONAL_NET_BUY
                );
                """
                cursor.execute(merge_prices_sql)
                cursor.execute("DROP TABLE IF EXISTS TEMP_FACT_STOCK_PRICES;")

            print(f"[✓] {symbol} 資料同步完畢")

    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    fetch_and_sync_stock_data()
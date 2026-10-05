import sqlite3

def import_sql_file():
    # 1. 讀取 .sql 檔案的內容
    # 請確保編碼設定為 utf-8，避免中文內容變亂碼
    with open('raw_data/source.sql', 'r', encoding='utf-8') as file:
        sql_script = file.read()

    # 2. 建立資料庫連線 (若 data.db 不存在會自動建立)
    conn = sqlite3.connect('data.db')
    cursor = conn.cursor()

    try:
        # 3. 使用 executescript() 一次性執行腳本內的所有 SQL 語法
        cursor.executescript(sql_script)
        
        # 提交變更
        conn.commit()
        print("✅ SQL 檔案已成功匯入並建立資料庫！")
        
    except sqlite3.Error as e:
        print(f"❌ 匯入失敗，發生 SQLite 錯誤: {e}")
        
    finally:
        # 4. 確保無論成功或失敗都會關閉連線
        conn.close()

if __name__ == '__main__':
    import_sql_file()
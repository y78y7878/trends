import sqlite3

# 1. 建立資料庫連線
# 若專案資料夾中沒有 data.db，執行時會自動建立一個全新的檔案
conn = sqlite3.connect('data.db')

# 建立 Cursor 物件，用來執行 SQL 語句
cursor = conn.cursor()

# 2. 建立資料表 (若不存在則建立)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        role TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

# 3. 插入資料 (存取)
# 實務上建議使用 ? 作為佔位符，避免 SQL Injection
cursor.execute('''
    INSERT INTO users (name, role) 
    VALUES (?, ?)
''', ('張三', '資料分析師'))

# 提交變更 (新增、修改、刪除都必須 commit 才會寫入實體檔案)
conn.commit()

# 4. 查詢資料
cursor.execute('SELECT * FROM users')
rows = cursor.fetchall()

print("目前資料庫中的資料：")
for row in rows:
    print(row)

# 5. 關閉連線 (釋放資源)
conn.close()
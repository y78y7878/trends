import pandas as pd
from sqlalchemy import create_engine

# 1. 建立 MariaDB 與 SQLite 連線 (請依實際帳密修改，預設無密碼則為 root:@127.0.0.1)
mariadb_engine = create_engine("mysql+pymysql://root:1234@127.0.0.1:3306/trends")
sqlite_engine = create_engine("sqlite:///data.db")

# 2. 指定要搬移的資料表
tables = ['google_trends', 'google_trends_news', 'ptt_trends_key_words']

# 3. 自動讀取 MariaDB 並寫入 SQLite
for table in tables:
    df = pd.read_sql_table(table, con=mariadb_engine)
    df.to_sql(table, con=sqlite_engine, if_exists='replace', index=False)
    print(f"✅ 資料表 {table} 已成功無縫轉移至 SQLite！")
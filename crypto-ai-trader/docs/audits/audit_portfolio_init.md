問題描述
========
PortfolioManager 初始化後，cash_balance 為 0，但 portfolio_state.json 中 cash_balance=368.32。

根因分析
========
1. 雙存儲架構不一致
   - PRIMARY: SQLite state.db（portfolio 表）
   - BACKUP: portfolio_state.json
   - SQLite 的 portfolio 表結構不含 cash_balance 欄位，只存 positions。

2. __init__ 加載順序（portfolio.py L70）
   self._load_state_from_db()  // 先讀 SQLite
   self._check_daily_reset()

3. _load_state_from_db() 邏輯（L538-569）
   - 若 SQLite 中有 positions（db_positions 非空），loaded=True
   - 此時 **不會 fallback 到 JSON**，cash_balance 保持初始化值 0
   - SQLite 只恢復了 positions，沒有恢復 cash_balance

4. _save_state() 邏輯（L487-536）
   - 保存 positions 到 SQLite（portfolio 表）
   - 保存 positions + cash_balance 到 JSON
   - 所以 JSON 永遠有正確 cash_balance，但 SQLite 永遠沒有

5. 結果
   - 每次重啟：SQLite 有 positions → loaded=True → 不讀 JSON → cash_balance=0
   - 這是一個 **結構性 bug**：SQLite 作為 primary storage 卻不完整，缺少 cash_balance

並發問題
========
1. portfolio_state.json 無文件鎖（已知問題，audit_code.md W1）
2. 多個進程同時讀寫可能導致 JSON 內容覆蓋
3. 但此 bug 的主因不是並發，而是 **SQLite schema 缺失 cash_balance**

修復建議
========
方案 A（推薦）：補全 SQLite 存儲
1. 在 state_db.py 的 portfolio 表增加 cash_balance REAL 欄位
2. 在 PortfolioManager._save_state() 中同時保存 cash_balance 到 SQLite
3. 在 _load_state_from_db() 中從 SQLite 讀取 cash_balance
4. 更新 portfolio_set / portfolio_get_all 接口支持 cash_balance

方案 B：改變 fallback 邏輯
1. _load_state_from_db() 中，即使 SQLite 有 positions，也檢查 JSON 中的 cash_balance
2. 簡單但不優雅，違反「SQLite 是 primary」的設計

方案 C：統一從 JSON 加載
1. 反轉優先級：先讀 JSON，再讀 SQLite 補充
2. 但會喪失 SQLite 的 ACID 優勢

推薦實施方案 A
==============
修改點：
1. src/state_db.py:
   - portfolio 表增加 cash_balance REAL DEFAULT 0
   - portfolio_set / portfolio_get_all 返回 cash_balance
   - 提供 kv 或獨立表存儲全局 cash_balance（更乾淨）

2. src/portfolio.py:
   - _load_state_from_db() 增加讀取 cash_balance 邏輯
   - _save_state() 增加保存 cash_balance 到 SQLite

3. 遷移：
   - 啟動時檢測舊 schema，自動 ALTER TABLE 或重建

驗證方法
========
1. 刪除/重命名 state.db，啟動程序
2. 確認 cash_balance 正確從 JSON 遷移到 SQLite
3. 重啟程序，確認 cash_balance 保持正確值

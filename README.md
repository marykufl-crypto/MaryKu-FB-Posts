# 📡 FB 貼文雷達

每週自動抓你指定的 Facebook 專頁的爆款貼文 → AI 拆解 → **改寫成你自己語氣的 Facebook 貼文草稿** → 生成可拖拽管理的看板。

這是爆款雷達的 Facebook 版，和 IG 版、YouTube 版是三個獨立的看板（三個倉庫、三個網址）。
**Facebook 版和 YouTube 版共用同一個 Supabase 專案**，兩邊的資料用 `tracker` 欄位分開（YT／FB），互不影響。

| | 資料來源 | 爆款判斷 |
|---|---|---|
| IG 版 | Apify | 互動率、留言數 |
| YouTube 版 | YouTube 官方 API（免費） | 觀看 ÷ 訂閱 |
| **FB 版** | **Apify**（Facebook 專頁抓取） | **（讚好 + 留言×3 + 分享×5）÷ 粉絲 × 100**，和專頁自己的平均比 |

## 準備

- GitHub（已有）
- Supabase：**用 YouTube 版那個專案**（`youtube-radar`），不用新開
- Google AI Studio 的 Gemini 鑰匙（已有，和 YouTube 版同一把）
- Apify 的 API token：用 IG 版那個 Apify 帳號。免費方案每月 US$5 額度，用完要付費

## 第 1 步 · 升級資料庫（只做一次）

Supabase → `youtube-radar` 專案 → **SQL Editor → New query**，把 `schema_fb.sql` **整段**貼上 → **Run**。
它會加上「分享數」欄位、讓 FB 和 YT 各自用自己的爆款算法，並放入起始的 8 個 FB 專頁。YouTube 版的資料不會被刪。

## 第 2 步 · 建新倉庫並上傳

GitHub 建新倉庫（例如 `MaryKu-FB-Posts`，**Public**），把這個資料夾的檔案推上去。

## 第 3 步 · Secrets 和 Variables

倉庫 → Settings → Secrets and variables → Actions。

**Secrets（名字要一模一樣）：**

| Secret | 值 |
|---|---|
| `APIFY_TOKEN` | Apify → Settings → API & Integrations 裡的 token |
| `SUPABASE_URL` | `youtube-radar` 專案的 Project URL |
| `SUPABASE_ANON_KEY` | publishable key |
| `SUPABASE_SERVICE_ROLE_KEY` | secret key |
| `GEMINI_API_KEY` | Gemini 鑰匙 |

**Variables：** `BRAND`（例：`古璟檑 · FB 雷達`）、`NICHE`（例：`生活覺察、轉念、銷售心態`）、`SYNC_DAY`（例：`每週一`）

## 第 4 步 · 開網頁

倉庫 → Settings → Pages → Deploy from a branch → **main** → 資料夾選 **/docs** → Save。
（`docs/` 在第一次跑完後才會出現。）

## 第 5 步 · 首次運行

Actions → **Sync Content** → Run workflow。之後自動接力：AI 拆解與改寫 → 生成網頁。網址：`https://<用戶名>.github.io/<倉庫名>/`

## 調整內容多寡（Variables，不用改程式）

| Variable | 預設 | 作用 |
|---|---|---|
| `MIN_ENGAGE` | `10` | 讚好＋留言＋分享低於這個數直接丟掉 |
| `OUTLIER_X` | `1.5` | 互動達到專頁自己中位數的幾倍算爆款 |
| `MIN_SHARES` | `10` | 分享數達到這個數也算爆款 |
| `MIN_ER` | `0.01` | 互動數 ÷ 粉絲達到 1% 也算爆款 |
| `TOP_N_PER_COMP` | `3` | 每個專頁至少保留幾條 |
| `MAX_PER_COMP` | `15` | 每個專頁最多幾條 |
| `DAYS_BACK` | `60` | 回看多少天（不額外花錢） |
| `RESULTS_LIMIT` | `30` | 每個專頁抓最新多少條（**這個會加 Apify 的錢**） |

## 加／停專頁

Supabase → Table Editor → `competitors` 表：`username` 填專頁網址最後那段（例：`healingmuseumhk`），`tracker` 填 **FB**，`active` 打勾。取消勾 = 停止追蹤。

## 已知限制

- Facebook 沒有官方的公開貼文 API，資料靠 Apify 抓取。Facebook 改版時，欄位可能對不上，日誌會印出實際的欄位名，把日誌給 Claude 看就能修。
- 看板上的貼文用 Facebook 官方嵌入程式顯示：私人專頁、已刪除的貼文，或瀏覽器擋了第三方內容時，會顯示空白。
- 貼文的「我的稿」是 AI 草稿，發佈前一定要自己看過，特別是有沒有碰到紅線。
- 倉庫是 Public：看板上的專頁資料和 AI 拆解任何人拿到連結都看得到。

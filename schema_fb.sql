-- ============================================================
-- FB 自然貼文雷達 · 資料庫升級（在 YouTube 版的 Supabase 專案執行，只需跑一次）
--
-- 做兩件事：
--   1. posts 表加上「分享數 shares」
--   2. 互動率和爆款分數按 tracker 分開算：
--        YT：觀看 ÷ 訂閱（原本的算法不變）
--        FB：（讚好 + 留言×3 + 分享×5）÷ 專頁粉絲 × 100
--   3. 放入起始的 FB 專頁名單
--
-- 可以安全重複跑，不會刪任何貼文或影片資料（只重建兩個自動計算欄位）。
-- ============================================================

alter table posts add column if not exists shares integer default 0;

-- 兩個自動計算欄位要整欄重建（它們是 generated column，不存資料，重建不會丟東西）
alter table posts drop column if exists engagement_rate;
alter table posts drop column if exists viral_score;

alter table posts add column engagement_rate numeric generated always as (
  case when tracker = 'FB'
       then (likes + comments + shares)::numeric / nullif(followers, 0)
       else (likes + comments)::numeric / nullif(views, 0)
  end) stored;

alter table posts add column viral_score numeric generated always as (
  case when tracker = 'FB'
       then round((greatest(likes, 0) + comments * 3 + shares * 5)::numeric / nullif(followers, 0) * 100, 2)
       else round(views::numeric / nullif(followers, 0), 2)
  end) stored;

create index if not exists posts_score_idx   on posts (viral_score desc nulls last);
create index if not exists posts_tracker_idx on posts (tracker);

-- ---------- 起始 FB 專頁名單（來自你的同行名單，只填專頁網址最後那段）----------
-- 例：https://www.facebook.com/healingmuseumhk/  →  healingmuseumhk
-- 想加更多就複製一行、改名稱；建議 8–15 個。
insert into competitors (username, tracker, active, notes) values
  ('healingmuseumhk',        'FB', true, 'Healing Museum 身心靈療癒館'),
  ('mindfulpeacehongkong',   'FB', true, '香港靜心協會'),
  ('memoire.com.hk',         'FB', true, 'Mémoire Academy（IG 帳號待確認是否官方）'),
  ('soulnlifeempowerment',   'FB', true, '心靈成長發展中心'),
  ('MindfulnessHK',          'FB', true, 'Samantha Yung Mindfulness 靜觀·修心'),
  ('YoungLivingHongKong',    'FB', true, 'Young Living Hong Kong（行業標杆）'),
  ('heungyau.incense',       'FB', true, 'Heung Yau 香丘（跨界參考）'),
  ('doterrahk.zh',           'FB', true, 'doTERRA HK（後備名單）')
on conflict do nothing;

-- 抽背卡的复习记录（"练"模式的账本）。
--
-- 为什么按"卡"记而不是按"笔记行"记：一行 notes(kind='card') 里通常有好几张卡
-- （`## Q1… ## Q2…`），按行记等于把 10 张卡压成一个数，练完也说不清哪张没记住。
-- 卡在行内的位置就是它的稳定编号（card_index），所以主键是 (note_id, card_index)。
--
-- 口径：
--   - 重复评同一张卡 = 更新最近评价 + reviews 累加（不做历史流水，够用且不可爆量）；
--   - rating ∈ good / fuzzy / bad（"会了 / 模糊 / 不会"），CHECK 拦住拼错的值；
--   - "记住率"的分母只算**评过的卡**（没评过的卡不能被算成不会）。
CREATE TABLE IF NOT EXISTS card_reviews (
  note_id     TEXT NOT NULL,
  card_index  INTEGER NOT NULL,
  rating      TEXT NOT NULL CHECK (rating IN ('good', 'fuzzy', 'bad')),
  reviews     INTEGER NOT NULL DEFAULT 1,
  reviewed_at TEXT NOT NULL,
  PRIMARY KEY (note_id, card_index),
  FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_card_reviews_reviewed_at ON card_reviews(reviewed_at DESC);

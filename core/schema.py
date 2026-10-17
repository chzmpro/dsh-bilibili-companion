"""SQLite schema 定义与可重复迁移。

任务书 §5 要求至少这些表（本项目全部实现，字段按任务书"必须字段与唯一键"）：

``videos`` / ``evidence`` / ``notes`` / ``drafts`` / ``actions``
``platform_health`` / ``seen_replies`` / ``runs`` / ``text_jobs`` / ``feedback``

另加两张本项目自用表：

* ``http_cache`` —— 只读响应缓存（先查缓存再请求，任务书 §4 要求）
* ``owner_notifications`` —— 向主号汇报的**投递状态**（任务书 §6.6：
  ``verified`` / ``uncertain`` / ``unsupported``，不能假称已通知主人）

设计要点：

* SQLite **WAL** 模式 + 单实例任务锁（见 :mod:`core.lock`）。
* 迁移脚本**可重复运行**：``migrate()`` 幂等，靠 ``schema_version`` 表推进。
* 数据库是**唯一真相源**；Markdown 导出只是产物，不是第二个真相源。
"""

from __future__ import annotations

SCHEMA_VERSION = 10

# 迁移按版本号递增；append-only，绝不修改已发布的迁移。
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        -- 视频：bvid 唯一
        CREATE TABLE IF NOT EXISTS videos (
            bvid              TEXT PRIMARY KEY,
            aid               INTEGER,
            title             TEXT NOT NULL DEFAULT '',
            up_mid            INTEGER,
            up_name           TEXT NOT NULL DEFAULT '',
            published_at      INTEGER,              -- 发布时间（Unix 秒）
            duration_sec      INTEGER,
            source_url        TEXT NOT NULL DEFAULT '',
            first_seen_at     INTEGER NOT NULL,     -- 首次发现（Unix 秒）
            last_seen_at      INTEGER NOT NULL,     -- 最后发现（Unix 秒）
            status            TEXT NOT NULL DEFAULT 'discovered',
            discovery_keyword TEXT NOT NULL DEFAULT '',
            CHECK (status IN ('discovered','read','analyzed','briefed','skipped','error'))
        );
        CREATE INDEX IF NOT EXISTS idx_videos_last_seen ON videos(last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

        -- 证据：每读到一段真实原文就登记一条，供反查
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id    TEXT PRIMARY KEY,        -- ev_<hash>
            bvid           TEXT NOT NULL,
            source_type    TEXT NOT NULL,           -- subtitle|asr|description|readme|comment
            source_url     TEXT NOT NULL DEFAULT '',
            start_sec      REAL,                    -- 字幕/ASR 片段起始秒
            end_sec        REAL,
            text           TEXT NOT NULL,           -- 原文片段（留在 bridge 内）
            text_sha256    TEXT NOT NULL,           -- 原文哈希，防篡改/去重
            fetched_at     INTEGER NOT NULL,
            availability   TEXT NOT NULL DEFAULT 'available',
            FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE CASCADE,
            CHECK (source_type IN ('subtitle','asr','description','readme','comment')),
            CHECK (availability IN ('available','partial','unavailable'))
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_bvid ON evidence(bvid);
        CREATE INDEX IF NOT EXISTS idx_evidence_sha ON evidence(text_sha256);

        -- 知识笔记：分项评分 + 事实/推断/疑点分层
        CREATE TABLE IF NOT EXISTS notes (
            bvid            TEXT PRIMARY KEY,
            summary         TEXT NOT NULL DEFAULT '',
            score_total     INTEGER NOT NULL DEFAULT 0,
            score_topic     INTEGER NOT NULL DEFAULT 0,
            score_novelty   INTEGER NOT NULL DEFAULT 0,
            score_evidence  INTEGER NOT NULL DEFAULT 0,
            score_recency   INTEGER NOT NULL DEFAULT 0,
            score_discuss   INTEGER NOT NULL DEFAULT 0,
            facts_json      TEXT NOT NULL DEFAULT '[]',   -- 有证据支持的事实
            inferences_json TEXT NOT NULL DEFAULT '[]',   -- 模型推断
            doubts_json     TEXT NOT NULL DEFAULT '[]',   -- 未验证点
            knowledge_tags  TEXT NOT NULL DEFAULT '[]',
            evidence_level  TEXT NOT NULL DEFAULT 'unavailable',
            updated_at      INTEGER NOT NULL,
            FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE CASCADE,
            CHECK (evidence_level IN ('subtitle','asr','description_only','unavailable'))
        );

        -- 草稿：顶层评论 / 回复。只能引用已存证据。
        CREATE TABLE IF NOT EXISTS drafts (
            draft_id      TEXT PRIMARY KEY,
            draft_type    TEXT NOT NULL,            -- top|reply
            bvid          TEXT NOT NULL,
            target_rpid   TEXT,                     -- reply 必填
            target_root   TEXT,
            text          TEXT NOT NULL,
            evidence_ids  TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'pending',
            approved_at   INTEGER,                  -- 人用 CLI 写入，模型无权
            created_at    INTEGER NOT NULL,
            updated_at    INTEGER NOT NULL,
            FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE CASCADE,
            CHECK (draft_type IN ('top','reply')),
            CHECK (status IN ('pending','approved','rejected','reserved','sent_verified','uncertain','failed','skipped'))
        );
        CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
        CREATE INDEX IF NOT EXISTS idx_drafts_bvid ON drafts(bvid);

        -- 动作：幂等键唯一。真实与模拟分开计数（is_simulated）。
        CREATE TABLE IF NOT EXISTS actions (
            action_id       TEXT PRIMARY KEY,
            agent_mid       INTEGER NOT NULL,
            action_type     TEXT NOT NULL,          -- top|reply|like|favorite|dynamic
            bvid            TEXT NOT NULL,
            target_rpid     TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            draft_id        TEXT,
            status          TEXT NOT NULL,          -- draft|reserved|sent_verified|uncertain|failed
            remote_rpid     TEXT,
            remote_url      TEXT,
            is_simulated    INTEGER NOT NULL DEFAULT 0,
            error_code      TEXT,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            CHECK (action_type IN ('top','reply','like','favorite','dynamic')),
            CHECK (status IN ('draft','reserved','sent_verified','uncertain','failed')),
            CHECK (is_simulated IN (0,1))
        );
        CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
        CREATE INDEX IF NOT EXISTS idx_actions_bvid ON actions(bvid);

        -- 平台健康：5 态 + 原始响应码 + 人工恢复记录
        CREATE TABLE IF NOT EXISTS platform_health (
            event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            state           TEXT NOT NULL,          -- ready|rate_limited|verification_required|auth_expired|platform_error
            raw_code        TEXT,                   -- 原始 code / HTTP 状态，不合并语义
            category        TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '',
            occurred_at     INTEGER NOT NULL,
            next_check_at   INTEGER,
            recovered_at    INTEGER,
            recovered_by    TEXT,                   -- 必须是人用入口写入
            CHECK (state IN ('ready','rate_limited','verification_required','auth_expired','platform_error'))
        );
        CREATE INDEX IF NOT EXISTS idx_health_occurred ON platform_health(occurred_at DESC);

        -- 已见回复：远端 rpid 唯一
        CREATE TABLE IF NOT EXISTS seen_replies (
            remote_rpid   TEXT PRIMARY KEY,
            bvid          TEXT NOT NULL,
            root_rpid     TEXT,
            parent_rpid   TEXT,
            author_mid    INTEGER,
            author_name   TEXT NOT NULL DEFAULT '',
            text_excerpt  TEXT NOT NULL DEFAULT '',
            is_mention    INTEGER NOT NULL DEFAULT 0,
            process_state TEXT NOT NULL DEFAULT 'new',
            first_seen_at INTEGER NOT NULL,
            handled_at    INTEGER,
            CHECK (process_state IN ('new','ignored','drafted','replied','error'))
        );
        CREATE INDEX IF NOT EXISTS idx_seen_process ON seen_replies(process_state);

        -- 运行记录：事实来源（DSH 说完成不算数）
        CREATE TABLE IF NOT EXISTS runs (
            run_id         TEXT PRIMARY KEY,
            trigger_source TEXT NOT NULL,           -- manual|schedule|interaction
            started_at     INTEGER NOT NULL,
            finished_at    INTEGER,
            candidates_n   INTEGER NOT NULL DEFAULT 0,
            read_n         INTEGER NOT NULL DEFAULT 0,
            drafts_n       INTEGER NOT NULL DEFAULT 0,
            sent_n         INTEGER NOT NULL DEFAULT 0,
            sim_sent_n     INTEGER NOT NULL DEFAULT 0,
            errors_n       INTEGER NOT NULL DEFAULT 0,
            conclusion     TEXT NOT NULL DEFAULT '',
            detail_json    TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

        -- 豆包任务：缓存键 = bvid + 原文哈希 + 模型 ID + 提示版本
        CREATE TABLE IF NOT EXISTS text_jobs (
            job_id          TEXT PRIMARY KEY,
            bvid            TEXT,
            input_sha256    TEXT NOT NULL,
            model_id        TEXT NOT NULL,
            prompt_version  TEXT NOT NULL,
            cache_key       TEXT NOT NULL,
            status          TEXT NOT NULL,          -- ok|timeout|invalid_json|needs_deep_review|error|skipped
            prompt_tokens   INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            from_cache      INTEGER NOT NULL DEFAULT 0,
            error_code      TEXT,
            result_json     TEXT,                   -- 成功的解析结果，供缓存命中时复用
            created_at      INTEGER NOT NULL,
            CHECK (status IN ('ok','timeout','invalid_json','needs_deep_review','error','skipped')),
            CHECK (from_cache IN (0,1))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_text_jobs_cache
            ON text_jobs(cache_key, status) WHERE status = 'ok';

        -- 只读响应缓存：先查缓存再请求。按 kind 区分 TTL。
        CREATE TABLE IF NOT EXISTS http_cache (
            cache_key  TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,            -- search|metadata|subtitle|comment
            payload    TEXT NOT NULL,            -- JSON
            fetched_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_http_cache_kind ON http_cache(kind);
        CREATE INDEX IF NOT EXISTS idx_http_cache_fetched ON http_cache(fetched_at);

        -- 主人反馈：只影响下轮排序，不改公开动作授权
        CREATE TABLE IF NOT EXISTS feedback (
            bvid        TEXT NOT NULL,
            verdict     TEXT NOT NULL,              -- useful|not_useful
            note        TEXT NOT NULL DEFAULT '',
            created_at  INTEGER NOT NULL,
            PRIMARY KEY (bvid, created_at),
            FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE CASCADE,
            CHECK (verdict IN ('useful','not_useful'))
        );

        """,
    ),
    (
        2,
        """
        -- ★ v2：新增主号汇报投递状态表（任务书 §6.6）。
        --   必须作为**新版本**追加：v1 已在既有库上应用过，
        --   把新表塞进 v1 不会在既有库上执行（这正是本项目踩过的坑）。
        -- ★ 向主号汇报的投递状态（任务书 §6.6）
        --   owner_mid 是身份校验值；**不能**把 @数字UID 或纯文本 @昵称视为成功。
        --   必须通过页面候选选择/渲染后的 UID 检查，并如实记录三态。
        CREATE TABLE IF NOT EXISTS owner_notifications (
            notification_id TEXT PRIMARY KEY,
            bvid            TEXT NOT NULL,
            owner_mid       INTEGER NOT NULL,
            -- verified   : 已确认渲染后的 @ 目标确为 owner_mid
            -- uncertain  : 提交了但无法核验（页面读不回 / 超时）
            -- unsupported: 当前通道无法表达提及（无浏览器适配器等）
            -- planned    : 已生成草稿，尚未尝试投递
            status          TEXT NOT NULL DEFAULT 'planned',
            channel         TEXT NOT NULL DEFAULT 'local',
            -- 页面渲染后实际读到的 UID（只有 verified 时才等于 owner_mid）
            observed_mid    INTEGER,
            draft_id        TEXT,
            action_id       TEXT,
            remote_rpid     TEXT,
            remote_url      TEXT,
            error_code      TEXT,
            detail          TEXT NOT NULL DEFAULT '',
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE CASCADE,
            CHECK (status IN ('planned','verified','uncertain','unsupported','failed')),
            CHECK (channel IN ('local','browser','api'))
        );
        CREATE INDEX IF NOT EXISTS idx_owner_notif_bvid ON owner_notifications(bvid);
        CREATE INDEX IF NOT EXISTS idx_owner_notif_status ON owner_notifications(status);
        """,
    ),
    (
        3,
        """
        -- ★ v3：点赞/收藏支持（主人 2026-10-15 要求）+ 收藏夹名↔media_id 映射。
        --
        -- 1) 放宽 actions.action_type（SQLite 不能直接改 CHECK，需重建表）。
        --    旧表只允许 top/reply；要支持 like/favorite/dynamic 必须重建。
        CREATE TABLE IF NOT EXISTS actions_v3 (
            action_id       TEXT PRIMARY KEY,
            agent_mid       INTEGER NOT NULL,
            action_type     TEXT NOT NULL,
            bvid            TEXT NOT NULL,
            target_rpid     TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            draft_id        TEXT,
            status          TEXT NOT NULL,
            remote_rpid     TEXT,
            remote_url      TEXT,
            is_simulated    INTEGER NOT NULL DEFAULT 0,
            error_code      TEXT,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            CHECK (action_type IN ('top','reply','like','favorite','dynamic')),
            CHECK (status IN ('draft','reserved','sent_verified','uncertain','failed')),
            CHECK (is_simulated IN (0,1))
        );
        INSERT OR IGNORE INTO actions_v3
            SELECT action_id, agent_mid, action_type, bvid, target_rpid,
                   idempotency_key, draft_id, status, remote_rpid, remote_url,
                   is_simulated, error_code, created_at, updated_at
              FROM actions;
        DROP TABLE IF EXISTS actions;
        ALTER TABLE actions_v3 RENAME TO actions;
        CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
        CREATE INDEX IF NOT EXISTS idx_actions_bvid ON actions(bvid);

        -- 2) 收藏夹名 ↔ media_id 映射（收藏时需要 media_id；名字是人给的）
        CREATE TABLE IF NOT EXISTS favorite_folders (
            name        TEXT PRIMARY KEY,           -- 收藏夹名（如「工具与技术」）
            media_id    INTEGER,                    -- B站返回的 mlid；未解析时为 NULL
            folder_id   INTEGER,                    -- 原始 fid
            title       TEXT NOT NULL DEFAULT '',   -- B站实际标题（可能与 name 不同）
            created_at  INTEGER NOT NULL,
            resolved_at INTEGER,
            CHECK (media_id IS NULL OR media_id > 0)
        );

        -- 3) 视频点赞/收藏的**当前状态**（避免重复提交；也是幂等键的依据）
        CREATE TABLE IF NOT EXISTS video_states (
            bvid        TEXT PRIMARY KEY,
            like_state  INTEGER NOT NULL DEFAULT 0,  -- 0 未赞 1 已赞
            fav_state   INTEGER NOT NULL DEFAULT 0,  -- 0 未收藏 1 已收藏
            fav_media_ids TEXT NOT NULL DEFAULT '[]',
            updated_at  INTEGER NOT NULL,
            FOREIGN KEY (bvid) REFERENCES videos(bvid) ON DELETE CASCADE,
            CHECK (like_state IN (0,1)),
            CHECK (fav_state IN (0,1))
        );
        """,
    ),
    (
        4,
        """
        -- ★ v4：主人偏好（跨对话持久）。
        --
        -- 为什么需要：对话上下文会被压缩，"我上次说过少点赞"这类口述约定
        -- 会丢。把偏好写进库，任何新对话调 owner_preferences 都能读到。
        --
        -- ⚠️ 这张表**只影响排序与内容策略**，**不能**改发布授权：
        --    模式/配额/UID/暂停仍然只由 config.yaml 决定（任务书 §3）。
        CREATE TABLE IF NOT EXISTS owner_preferences (
            pref_id     TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,           -- rule|like|dislike|note
            subject     TEXT NOT NULL DEFAULT '',-- 主题（UP名/关键词/BV…）
            content     TEXT NOT NULL,           -- 偏好内容（自然语言）
            scope       TEXT NOT NULL DEFAULT 'ranking',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            CHECK (kind IN ('rule','like','dislike','note')),
            -- ★ 关键约束：偏好**不得**触碰发布授权
            CHECK (scope IN ('ranking','content','topic'))
        );
        CREATE INDEX IF NOT EXISTS idx_owner_prefs_active
            ON owner_preferences(active, kind);
        """,
    ),
    (
        5,
        """
        -- ★ v5：评论公开可见性复核账本（移植竞品 dsh-bili-scroll 的 verify-state.json）。
        --
        -- 为什么需要：「接口回发送成功 ≠ 公开可见」—— 新号/风控会让评论进审核，
        -- 程序以为发出去了、还记进判重表 ⇒ 下一轮不再发 ⇒ 一条都没到人。
        -- 所以发出后要**隔一会儿再只读复核一次**。
        --
        -- 为什么落库而不是第二个 json 文件：本项目**数据库是唯一真相源**，
        -- 用户要求"竞品有 verify-state.json，我们落 SQLite 新表"。
        --
        -- ★ visible 三态必须保留 NULL：
        --     TRUE  = 已确认公开可见（另见 likes）
        --     FALSE = 明确判为不可见（本模块目前不会产生，留给以后）
        --     NULL  = **没查到** —— 注意这**不是**"没发出去"！
        --             翻页/热度排序都会漏，观测不到不能反推不存在。
        --   因此按 7 天 TTL 跳过复查时**只认 visible = 1**。
        CREATE TABLE IF NOT EXISTS comment_verifications (
            remote_rpid TEXT PRIMARY KEY,           -- 我们发出评论的远端 rpid
            bvid        TEXT NOT NULL,
            visible     INTEGER,                    -- 1 可见 / 0 不可见 / NULL 没查到
            likes       INTEGER,
            how         TEXT NOT NULL DEFAULT '',   -- 依据，如「顶层·最新一页」
            note        TEXT NOT NULL DEFAULT '',   -- 附注（invisible 标记 / 为什么没查到）
            checked_at  INTEGER NOT NULL,           -- 上次复核时刻（Unix 秒）
            CHECK (visible IS NULL OR visible IN (0,1))
        );
        CREATE INDEX IF NOT EXISTS idx_comment_verifications_checked
            ON comment_verifications(checked_at DESC);
        CREATE INDEX IF NOT EXISTS idx_comment_verifications_bvid
            ON comment_verifications(bvid);
        """,
    ),
    # ------------------------------------------------------------------
    # v6 —— 关键词候选池（搜索方式 ②「推荐流只当词库扩展器」，2026-10-17）
    # ------------------------------------------------------------------
    #
    # 为什么单独一张表、且**不自动生效**：
    #   推荐流是**个性化算法**喂的（实测一屏 8 条里只有 2~3 条与我们相关，
    #   其余是鬼屋/原神/影视飓风这类高互动内容）。
    #   如果候选词自动写进 config.yaml，**小号的兴趣画像就被算法带跑了**。
    #   ⇒ 这里只**攒候选**，等人用 CLI 过一眼接受后才生效。
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS keyword_suggestions (
            keyword      TEXT PRIMARY KEY,          -- 候选关键词（大小写按首次出现保留）
            source       TEXT NOT NULL DEFAULT 'rcmd',  -- 从哪来：rcmd / manual / ...
            hits         INTEGER NOT NULL DEFAULT 1,    -- 在几条推荐里出现过
            sources_json TEXT NOT NULL DEFAULT '[]',    -- 来源推荐（bvid+标题），可审计
            created_at   INTEGER NOT NULL,
            updated_at   INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_keyword_suggestions_hits
            ON keyword_suggestions(hits DESC, updated_at DESC);
        """,
    ),
    # ------------------------------------------------------------------
    # v7 —— 推荐流**轮次账本**（搜索方式 ② 的"严格限额"，2026-10-17）
    # ------------------------------------------------------------------
    #
    # 为什么单独一张表（而不是塞进 `runs`）：
    #   `runs` 是**主流程轮次**的账本，`schedule.is_due()` 会读它判断
    #   "今天跑完没有"。若把推荐流也写进 `runs`，**刷一次推荐流就会让
    #   主流程误以为"今天已经干过活了"** ⇒ 当天不再抓取/评论。
    #   ⇒ 推荐流必须有自己的账本，**绝不与主流程混用**。
    #
    # 为什么必须有它：`DEFAULT_MAX_ROUNDS_PER_DAY` 原本**只写在文档里、
    #   代码从不检查** —— 即"限额"是假的。这张表让它变成真的。
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS trends_rounds (
            round_id   TEXT PRIMARY KEY,
            started_at INTEGER NOT NULL,
            fetched_n  INTEGER NOT NULL DEFAULT 0,   -- 这次拉到几条推荐
            added_n    INTEGER NOT NULL DEFAULT 0,   -- 其中几个是新候选词
            conclusion TEXT NOT NULL DEFAULT ''      -- ok | no_content | blocked | error
        );
        CREATE INDEX IF NOT EXISTS idx_trends_rounds_started
            ON trends_rounds(started_at DESC);
        """,
    ),
    # ------------------------------------------------------------------
    # v8 —— 私信回复账本（实时回复的**幂等**保证，2026-10-17）
    # ------------------------------------------------------------------
    #
    # 主人要求：「我想在B站私信你并且你能随时回复」。
    #
    # 为什么必须有这张表：
    #   实时回复靠**轮询未读数**。未读数在"已回复"之后**不会立刻归零**
    #   （B 站要等对端读了才变），所以同一个 `msg_seqno` 会在连续几次
    #   轮询里反复出现。没有账本 ⇒ **同一句话回 N 遍**（刷屏）。
    #   ⇒ 按 `msg_seqno` 去重，**同一个只回一次**。
    (
        8,
        """
        CREATE TABLE IF NOT EXISTS dm_replies (
            msg_seqno      INTEGER PRIMARY KEY,     -- 被回复的那条消息序号
            reply_text     TEXT NOT NULL DEFAULT '',
            remote_msg_key TEXT NOT NULL DEFAULT '',-- 发送后 B 站返回的 key（可核对）
            replied_at     INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dm_replies_at
            ON dm_replies(replied_at DESC);
        """,
    ),
    # ------------------------------------------------------------------
    # v9 —— 自我进化**提案账本**（2026-10-17）
    # ------------------------------------------------------------------
    #
    # 主人：「自我进化我觉得可以开始了」。
    #
    # 为什么必须有这张表：
    #   `core/evolution.py` 里的四道硬闸（`auto_apply` 写死 False、
    #   步长上限、硬边界、每日 2 份）只保证"**单份**提案合法"，
    #   **不保证**"一天只提 2 份" —— 那需要**持久化计数**。
    #   没有账本的话进程一重启计数就归零，限额形同虚设。
    #
    # 为什么状态只有三种：
    #   AI 只允许写 `pending`；`applied` / `rejected` **只能由人的 CLI 写**。
    #   这是"提案制"的落点 —— 机器提、人批。
    (
        9,
        """
        CREATE TABLE IF NOT EXISTS evolution_proposals (
            proposal_id TEXT PRIMARY KEY,
            parameter   TEXT NOT NULL,             -- 只允许 EVOLVABLE_PARAMETERS 里的
            old_value   REAL NOT NULL,
            new_value   REAL NOT NULL,
            reason      TEXT NOT NULL DEFAULT '',
            evidence    TEXT NOT NULL DEFAULT '[]',-- JSON 数组
            status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','applied','rejected')),
            created_at  INTEGER NOT NULL,
            decided_at  INTEGER,                   -- 人做决定的时间
            decided_by  TEXT NOT NULL DEFAULT ''   -- 'cli'（可追溯）
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_created
            ON evolution_proposals(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_evolution_status
            ON evolution_proposals(status, created_at DESC);
        """,
    ),
    # ------------------------------------------------------------------
    # v10 —— 私信**待回复队列**（2026-10-17）
    # ------------------------------------------------------------------
    #
    # 主人：*"我的私信要求尽量是让 DSH 进行思考而不是内置的那个低级模型"*。
    #
    # 为什么需要一张表：
    #   常驻监听进程**拿不到 DeepSeek 的 key**（凭据在 DSH 内部，不落配置），
    #   所以它**不能**自己调模型。正确分工是：
    #
    #     监听进程：发现新私信 → 落进本表（pending）
    #     DSH：调 `dm_inbox` 取出待回复 → **用 DSH 自己的模型思考** → `dm_reply` 发出
    #
    #   ⇒ 回复质量由 DSH 的模型决定，不是豆包那个小的。
    #
    # 为什么要 `handled` 字段：同一条不能回两次（与 `dm_replies` 双重保险）。
    (
        10,
        """
        CREATE TABLE IF NOT EXISTS dm_inbox (
            msg_seqno  INTEGER PRIMARY KEY,
            text       TEXT NOT NULL DEFAULT '',
            title      TEXT NOT NULL DEFAULT '',
            bvid       TEXT NOT NULL DEFAULT '',
            received_at INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','answered','ignored')),
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dm_inbox_status
            ON dm_inbox(status, received_at DESC);
        """,
    ),
)

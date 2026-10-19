"""配置模型与校验。

对应 ``config.example.yaml``。**任何 secret 都不得出现在配置里**——
只允许出现"密钥所在的环境变量名"（``text_worker.api_key_env``）。

校验规则来自任务书 §3「配置校验」：

* 只读公开搜索允许 ``agent_mid=0``、``owner_mid=0``；
  真实发布前两者都必须为**不同的正整数**。
* 兴趣关键词至少一项，去空白、去重。
* 请求间隔、缓存有效期、单轮上限、重试次数必须有**有限范围**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.external_config import ExternalPlatformsConfig
from core.ocr import OcrConfig

InteractionMode = Literal["observe", "review", "auto"]

# ---- 硬上限（任务书 §4 明令）------------------------------------------------
MAX_CANDIDATES_TO_DSH_CEILING = 3
MAX_EXCERPT_CHARS_CEILING = 500


class ConfigError(ValueError):
    """配置不合法。消息面向使用者，**不得包含密钥值**。"""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class AccountConfig(_Base):
    agent_mid: int = Field(default=0, ge=0, description="小号 UID；0 = 未配置")
    owner_mid: int = Field(default=0, ge=0, description="主号 UID；0 = 未配置")
    owner_name_hint: str = Field(
        default="", max_length=64, description="主号昵称提示；身份仍以 UID 为准"
    )


class DiscoveryConfig(_Base):
    platforms: list[Literal["bilibili", "douyin", "xiaohongshu"]] = Field(
        default=["bilibili"], min_length=1, max_length=3
    )
    keywords: list[str] = Field(default_factory=list, min_length=1)
    published_within_days: int = Field(default=7, ge=1, le=90)
    max_candidates_per_run: int = Field(default=12, ge=1, le=100)
    max_full_reads_per_run: int = Field(default=4, ge=1, le=20)
    min_score_for_comment: int = Field(default=75, ge=0, le=100)

    #: ★ 打分权重：**主题相关度（topic）占总分的比例**（2026-10-17）。
    #:
    #: 背景：主人反馈"没什么用的能给这么高分"。根因是打分标准没定义、
    #: 且没把关注点告诉模型。修完之后权重也需要可调 ——
    #: 实测（12 条真实样本，见 ``scripts/tune_weights.py``）：
    #: topic 占比越高，相关/无关的**区分度**越好：
    #:
    #:   topic=0.60 → 区分度 +48.8
    #:   topic=0.80 → 区分度 +55.5   ← 默认值
    #:
    #: 调高（如 0.9）= 更严格地"只看跟我有关的"，但可能漏掉沾边的好内容；
    #: 调低（如 0.6）= 更看重内容质量本身，但容易放进"热闹但无关"的。
    #: 其余三维（usefulness/novelty/evidence）按固定比例分掉剩下的权重。
    score_topic_weight: float = Field(default=0.80, ge=0.5, le=0.95)

    #: ★ 推荐流候选词**自动回填**关键词（2026-10-17）—— **默认关**。
    #:
    #: ``False``（默认，推荐）：候选词只进池子，由主人用
    #: ``cli.app keywords --accept`` 过一眼再采纳。
    #:
    #: ``True``：候选词**直接**写进 ``discovery.keywords``，不用人审。
    #:
    #: ⚠️ 为什么默认关：推荐流是**个性化算法**喂的（实测一屏 8 条里只有
    #: 2~3 条与我们相关，其余是鬼屋/原神/影视飓风这类高互动内容）。
    #: 自动回填意味着**算法可以间接决定小号关注什么**，画像会被慢慢带跑。
    #: 打开它 = 明确接受这个代价（换取"不用手动审"的便利）。
    #:
    #: 无论开关如何，**每日轮次上限与候选词数量上限始终生效**。
    auto_accept_suggestions: bool = False

    #: ★ 关键词**总数上限**（2026-10-19 新增，配合自动回填）。
    #:
    #: 为什么必须有它：一旦 ``auto_accept_suggestions=true``，
    #: 算法每次都能往 ``discovery.keywords`` 里加词；没有总数上限时，
    #: 跑几十轮之后关键词会膨胀到上百个 —— 每轮搜索要按词逐个发请求，
    #: 请求量与风控暴露面随词数线性上涨，而且画像会被稀释。
    #:
    #: 达到上限后**停止回填**（不删已有词），并在结果里如实报告跳过了几个。
    max_keywords: int = Field(default=12, ge=1, le=50)

    @field_validator("keywords")
    @classmethod
    def _normalize_keywords(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for raw in value:
            item = raw.strip()
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        if not result:
            raise ValueError("discovery.keywords 至少需要一项非空白关键词")
        return result


class BilibiliRequestsConfig(_Base):
    min_interval_seconds: float = Field(default=5.0, ge=1.0, le=600.0)
    max_read_retries: int = Field(default=2, ge=0, le=5)
    backoff_base_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    backoff_cap_seconds: float = Field(default=60.0, ge=1.0, le=1800.0)
    max_consecutive_rate_limits: int = Field(default=2, ge=1, le=10)
    search_cache_ttl_seconds: int = Field(default=43200, ge=60, le=604800)
    metadata_cache_ttl_seconds: int = Field(default=300, ge=30, le=86400)
    subtitle_cache_ttl_seconds: int = Field(default=86400, ge=60, le=604800)

    @model_validator(mode="after")
    def _backoff_ordering(self) -> BilibiliRequestsConfig:
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_cap_seconds 不得小于 backoff_base_seconds")
        return self


class TextWorkerConfig(_Base):
    enabled: bool = True
    provider: str = Field(default="volcengine_ark", max_length=64)
    base_url: str = Field(default="https://ark.cn-beijing.volces.com/api/v3", max_length=256)
    model: str = Field(default="", max_length=128)
    api_key_env: str = Field(default="ARK_API_KEY", max_length=128)
    #: ★ 单次批处理的**时间预算**（秒）。必须**小于** DSH 的
    #: ``toolCallTimeoutMs``，否则工具调用会在中途超时（实测踩到过：
    #: 真实一轮 198s vs DSH 超时 120s）。
    #: 预算耗尽时主动收尾并返回已完成部分，而不是让调用被掐断。
    max_batch_seconds: float = Field(default=90.0, ge=10.0, le=600.0)
    max_candidates_to_dsh: int = Field(default=MAX_CANDIDATES_TO_DSH_CEILING, ge=1)
    max_batch_chars: int = Field(default=6000, ge=500, le=64000)
    max_excerpt_chars: int = Field(default=MAX_EXCERPT_CHARS_CEILING, ge=50)
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)

    #: ★ 本地 ASR 兜底（P1，2026-10-16）—— **默认关**。
    #: 开着时：字幕不可用的视频会在**本机**转写一遍（不花 API 钱），
    #: 成功就升到 ``LEVEL_ASR``；转写返回空 = **这条没有语音**（不是故障）。
    #: ⚠️ 开启前提：装 ``faster-whisper`` + ``yt-dlp``（约 1 GB，含 Whisper 模型）。
    #: 缺依赖时**不会**让读取失败，只会记一条 ``ASR_NO_DEPENDENCY``。
    asr_fallback: bool = False

    #: Whisper 模型大小：``tiny`` / ``base`` / ``small`` / ``medium``。
    #: 默认 ``small``（竞品同款：质量与体积的平衡点）。
    asr_model: str = Field(default="small", max_length=32)

    @model_validator(mode="after")
    def _enforce_hard_ceilings(self) -> TextWorkerConfig:
        if self.max_candidates_to_dsh > MAX_CANDIDATES_TO_DSH_CEILING:
            raise ValueError(
                f"max_candidates_to_dsh 不得超过 {MAX_CANDIDATES_TO_DSH_CEILING}"
                "（任务书 §4：prepare_batch 最多返回 3 个短候选包）"
            )
        if self.max_excerpt_chars > MAX_EXCERPT_CHARS_CEILING:
            raise ValueError(
                f"max_excerpt_chars 不得超过 {MAX_EXCERPT_CHARS_CEILING}"
                "（任务书 §4：read_evidence_excerpt 单次摘录上限）"
            )
        return self

    def is_configured(self) -> bool:
        """模型 ID 是否已填。**不检查密钥**——密钥解析在 core.secrets。"""
        return bool(self.model.strip())


class ScheduleConfig(_Base):
    """唤醒时机。

    主人定：**打开 DSH 时就唤醒**（``on_host_start``），而不是每日固定时刻。
    仍保留 ``daily`` 模式以备将来切换。
    """

    mode: Literal["on_host_start", "daily", "continuous"] = "on_host_start"
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    daily_time: str = Field(default="10:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    #: 打开 DSH 时，距上次完成不足这么多分钟则不重复跑（防抖）
    min_gap_minutes: int = Field(default=120, ge=0, le=1440)

    #: ★ 只在**谷时**跑（DeepSeek 官方峰谷口径：工作日 9–12 / 14–18 为高峰，
    #: 其余含整个周末都是谷时）。到期但正处在峰时 → 挡下并提示等到谷时，
    #: 理由码 `WAIT_FOR_VALLEY`。**默认关**：打开 DSH 即跑是主人原本的设定，
    #: 这个开关只是给他一个"想省钱就打开"的选项。
    peak_valley: bool = False

    # ---- 持续跑（``mode="continuous"`` 时生效，2026-10-17）---------------
    #
    # 主人原话：「每天跑一轮太少了，而且跑一轮速度太快了，
    # 你看看能不能出一个让其一直跑的开关？」
    #
    # ★ 设计取舍：**刻意不做真正的无限循环** —— 那会明显增加请求量与风控风险
    #（本项目红线：不加大重试强度、不绕过风控）。
    # 改成"**每 N 分钟一轮 + 总轮次上限 + 到点自动停**"：
    #
    #   * 间隔足够长（默认 30 分钟）⇒ 不构成高频访问；
    #   * 每天总轮次有上限（默认 8 轮）⇒ 到点自动停，不需要人盯着；
    #   * 仍走**全部**既有门禁（配额 / 平台健康 / 幂等 / 发布门禁 / 谷时）。
    #: 持续模式下两轮之间的间隔（分钟）
    interval_minutes: int = Field(default=30, ge=5, le=1440)
    #: 持续模式下**每天最多跑几轮**（到点自动停）
    max_runs_per_day: int = Field(default=8, ge=1, le=96)


class PersonaConfig(_Base):
    """人设与内容规则（**来源**：curator `docs/13-agent-persona.md`，21 题问卷定稿）。

    这些数值原本在 curator 的 `config.toml` 的 `[persona]` 段；
    此处按相同语义移植，便于本项目独立运行。

    ⚠️ **2026-10-15 主人修正的几处**（见 `docs/persona-conflicts.md`）：

    * **AI 身份**：由人设的"绝不提"改为**"可适度表明"** ——
      主人说"毕竟名称就已经表示了一些"（`干饭不干事的小鲸鱼` 本身就是这种风格）。
      但仍守任务书 §6.4 的"**不得冒充真人观众**"：不编造个人经历。
    * **点赞/收藏**：任务书漏了，主人明确要求实现。
    * **频率**：改为档位可选（见 :data:`FREQUENCY_TIERS`）。
    """

    display_name: str = Field(default="干饭不干事的小鲸鱼", max_length=64)

    #: ★ AI 身份策略（主人 2026-10-15 定）
    #: ``never``      完全不提（原人设 B8 甲）
    #: ``when_asked`` 只在被直接问到时说明（**默认**，适度）
    #: ``proactive``  主动自我介绍是 AI
    ai_identity: Literal["never", "when_asked", "proactive"] = "when_asked"

    #: ★ 是否允许点赞 / 收藏（主人要求实现；任务书原本漏了）
    enable_liking: bool = True
    enable_favoriting: bool = True
    nicknames: list[str] = Field(
        default_factory=lambda: ["小肥鱼", "吃白饭的小肥鱼", "小白"]
    )
    #: 点赞门槛（score_total 与 not_hype 都要达标）
    like_min_score: float = Field(default=8.0, ge=0, le=10)
    like_min_not_hype: float = Field(default=8.0, ge=0, le=10)
    #: 同一 UP 主一天最多点赞几条
    like_max_per_author_per_day: int = Field(default=2, ge=0, le=20)
    favorite_min_score: float = Field(default=8.0, ge=0, le=10)
    #: 5 个固定收藏夹（agent 可自主新建，规则见 docs/persona.md §6）
    favorite_folders: list[str] = Field(
        default_factory=lambda: ["工具与技术", "方法论", "案例与实操", "观点与洞察", "待深挖"]
    )
    allow_create_folders: bool = True
    #: 只回复 @自己 与自己评论下（B15）
    allow_reply_others: bool = True
    #: 不关注任何人（B16 甲）
    allow_follow: bool = False
    #: 不做任何负反馈，不踩不举报（B14）
    allow_negative_feedback: bool = False
    #: 单次最多处理几条素材（B18 甲）
    max_items_per_run: int = Field(default=5, ge=1, le=50)

    @model_validator(mode="after")
    def _safety_invariants(self) -> PersonaConfig:
        # ★ 这三条是红线，配置层就不允许被打开
        if self.allow_follow:
            raise ValueError("allow_follow 必须为 false（B16 甲：不关注任何人）")
        if self.allow_negative_feedback:
            raise ValueError("allow_negative_feedback 必须为 false（B14：不做任何负反馈）")
        if not self.favorite_folders:
            raise ValueError("favorite_folders 不能为空（需要至少一个固定收藏夹）")
        # ⚠️ 注意：``ai_identity`` **没有**"假装真人"这一档。
        #    任务书 §6.4 明令"不得冒充真人观众"，所以最保守也只是"不提"，
        #    不存在"主动谎称是真人"的选项 —— 这在类型上就排除了。
        return self


#: ★ 频率档位（主人 2026-10-15 要求"改成多档可选"）。
#:
#: 每档给出**全部**配额，避免"改了一档但漏了另一项"。
#: 真正生效的是展开后的具体数值，档位只是**预设**；
#: 想微调就逐项覆盖（显式写的值优先于档位）。
#:
#: ``conservative`` 取自 2026-10-14 因验证码下调后的**实测值**（最稳）。
#: 阈值/间隔依据：curator ``config.toml`` 的 ``[persona]`` 段。
FREQUENCY_TIERS: dict[str, dict[str, int]] = {
    # 保守：出过验证码之后定的档，长期无人值守用这个
    "conservative": {
        "max_top_comments_per_day": 1,
        "max_replies_per_day": 3,
        "max_replies_per_thread_per_day": 2,
        "max_likes_per_day": 8,
        "max_favorites_per_day": 3,
        "min_action_interval_min": 25,
    },
    # 常规：人设原始值（评论 5 已下调为 3），有人盯着时用
    "normal": {
        "max_top_comments_per_day": 3,
        "max_replies_per_day": 6,
        "max_replies_per_thread_per_day": 2,
        "max_likes_per_day": 15,
        "max_favorites_per_day": 5,
        "min_action_interval_min": 15,
    },
    # 积极：只在**明确知晓风险**时用；仍不是"批量机械无差别"
    "active": {
        "max_top_comments_per_day": 5,
        "max_replies_per_day": 10,
        "max_replies_per_thread_per_day": 3,
        "max_likes_per_day": 30,
        "max_favorites_per_day": 5,
        "min_action_interval_min": 10,
    },
}

DEFAULT_FREQUENCY_TIER = "conservative"


class InteractionConfig(_Base):
    mode: InteractionMode = "observe"
    dry_run: bool = True
    publish_enabled: bool = False

    #: 频率档位。展开成下面的具体配额；显式给具体值则覆盖档位。
    frequency_tier: str = DEFAULT_FREQUENCY_TIER

    max_top_comments_per_day: int = Field(default=1, ge=0, le=20)
    max_replies_per_day: int = Field(default=3, ge=0, le=50)
    max_replies_per_thread_per_day: int = Field(default=2, ge=0, le=10)
    #: ★ 点赞/收藏配额（主人要求实现这两个动作）
    max_likes_per_day: int = Field(default=8, ge=0, le=100)
    max_favorites_per_day: int = Field(default=3, ge=0, le=50)
    #: 动作之间最小间隔（分钟）。红线 4：宁少勿多
    min_action_interval_min: int = Field(default=25, ge=0, le=1440)

    allow_third_party_threads: bool = False
    pause: bool = False

    #: ★ 私信回复开关（**独立于** ``publish_enabled``）。
    #: 打开它只允许"回复主人私信"（接收者硬编码为 ``owner_mid``），
    #: **不会**顺带打开视频评论 / 点赞 / 收藏。
    dm_reply_enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def _expand_tier(cls, data: Any) -> Any:
        """把档位展开成具体配额；**显式写的字段优先**。

        这样既能"选一档"，也能"选一档再改某一项"。
        """
        if not isinstance(data, dict):
            return data
        tier_name = data.get("frequency_tier", DEFAULT_FREQUENCY_TIER)
        if tier_name not in FREQUENCY_TIERS:
            raise ValueError(
                f"未知 frequency_tier：{tier_name!r}；"
                f"可选 {sorted(FREQUENCY_TIERS)}"
            )
        preset = FREQUENCY_TIERS[tier_name]
        out = dict(data)
        for key, value in preset.items():
            out.setdefault(key, value)  # 显式给的保留
        return out

    def public_writes_allowed(self) -> bool:
        """是否允许任何真实公开写入。

        三重条件缺一不可：非 observe 模式 + 关闭 dry_run + 开启总开关。
        另需 ``pause=False``。
        """
        return (
            self.mode in ("review", "auto")
            and not self.dry_run
            and self.publish_enabled
            and not self.pause
        )


class ReportConfig(_Base):
    local_digest: bool = True
    mention_owner_on_high_value_comment: bool = True
    #: ★ 主人定的"双峰"（B13 甲）：点赞 ≥ 此值 → 公开 @主号（大众爆款）
    mention_like_threshold: int = Field(default=10000, ge=0)
    #: 点赞 ≤ 此值且内容好 → 私信/分享（小众宝藏）
    share_like_threshold: int = Field(default=50, ge=0)
    #: 汇报推送时间（**不是**唤醒时机；唤醒见 schedule.mode）
    push_times: list[str] = Field(default_factory=lambda: ["10:00", "17:00"])

    @field_validator("push_times")
    @classmethod
    def _validate_push_times(cls, value: list[str]) -> list[str]:
        import re as _re

        pattern = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
        out: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                continue
            if not pattern.match(text):
                raise ValueError(f"push_times 必须是 HH:MM，收到 {text!r}")
            out.append(text)
        return out

    @model_validator(mode="after")
    def _threshold_ordering(self) -> ReportConfig:
        if self.share_like_threshold >= self.mention_like_threshold:
            raise ValueError(
                "share_like_threshold 必须小于 mention_like_threshold"
                "（小众宝藏阈值应低于大众爆款阈值）"
            )
        return self


class Config(_Base):
    external_platforms: ExternalPlatformsConfig = Field(default_factory=ExternalPlatformsConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    bilibili_requests: BilibiliRequestsConfig = Field(default_factory=BilibiliRequestsConfig)
    text_worker: TextWorkerConfig = Field(default_factory=TextWorkerConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)

    @model_validator(mode="after")
    def _identical_mids_rejected(self) -> Config:
        agent, owner = self.account.agent_mid, self.account.owner_mid
        if agent and owner and agent == owner:
            raise ValueError(
                "account.agent_mid 与 account.owner_mid 不得相同"
                "（小号与主号必须是不同账号）"
            )
        return self

    # ---- 门禁相关查询（供 bridge 的发布门禁调用）----------------------------

    def can_publish(self) -> tuple[bool, str]:
        """是否可以真实发布。返回 ``(allowed, reason_code)``。"""
        if self.account.agent_mid <= 0:
            return False, "AGENT_MID_NOT_CONFIGURED"
        if self.account.owner_mid <= 0:
            return False, "OWNER_MID_NOT_CONFIGURED"
        if self.account.agent_mid == self.account.owner_mid:
            return False, "AGENT_OWNER_SAME_MID"
        if self.interaction.pause:
            return False, "PAUSED"
        if self.interaction.mode == "observe":
            return False, "MODE_OBSERVE"
        if self.interaction.dry_run:
            return False, "DRY_RUN"
        if not self.interaction.publish_enabled:
            return False, "PUBLISH_DISABLED"
        return True, "OK"

    def text_worker_status(self) -> tuple[bool, str]:
        """豆包可用性。返回 ``(usable, code)``。"""
        if not self.text_worker.enabled:
            return False, "TEXT_WORKER_DISABLED"
        if not self.text_worker.is_configured():
            return False, "TEXT_WORKER_NOT_CONFIGURED"
        return True, "OK"


def load_config(path: Path | str | None = None) -> Config:
    """从 YAML 载入配置。

    文件不存在时返回**全默认配置**（即 observe + dry_run + 无 UID），
    这样 MCP server 在"还没配任何东西"时也能只读启动。
    """
    from core.paths import default_config_path

    target = Path(path) if path is not None else default_config_path()
    if not target.exists():
        return Config()

    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败：{exc}") from exc

    if raw is None:
        return Config()
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 mapping")

    try:
        return Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"配置校验失败：{exc}") from exc

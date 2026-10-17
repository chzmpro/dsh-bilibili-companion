"""从视频提取仓库地址的测试（2026-10-17）。

## 背景

主人纠正了我的理解：

> 「我私信部分是让你在**B站搜索或者刷取**，然后**刷到了再去相应的网站看源码**
> 等相关信息进而了解完全」

完整闭环：私信线索 → B站搜索/推荐流 → 读视频 → **顺视频找仓库 → 读源码** → 回复。

## ★ 参考项目对照（诚实记录，已核对源码）

| 项目 | 有没有"从视频找仓库并读源码" |
|---|---|
| `xiaoyaya191/bilibili_learning_bot` | ❌ **没有**（agent 工具只有 B站操作 + 知识库） |
| `jiran214/langup-ai` | ❌ **没有** |

而且参考项目把「**不修改源码或执行生成代码**」写进了
`evolution_engine.py` 的 `hard_boundaries` —— 这条**我们照抄**。

⇒ 这一环是我们要新做的，但复用已有的 `core/sourcefetch`。

## 钉死的不变量

* **只认明写的 URL**，绝不从标题"猜"仓库（猜错会读无关项目）；
* **必须带域名**（裸 `owner/repo` 不认，那是普通斜杠文本）；
* **排除 GitHub 功能页**（features/topics/...）与**子路径**（/issues /blob）；
* **去重** + **简介优先于字幕**（简介是 UP 主主动写的，字幕可能被 ASR 写错）。
"""

from __future__ import annotations

import pytest

from core.repolink import (
    RepoRef,
    describe,
    extract_from_video,
    extract_repos,
)

# ---------------------------------------------------------------------------
# 基本识别
# ---------------------------------------------------------------------------


def test_extracts_github_url() -> None:
    refs = extract_repos("看这个 https://github.com/Ebony-Vinyl/dsh-our-free-model 不错")
    assert len(refs) == 1
    assert refs[0].platform == "github"
    assert refs[0].full_name == "Ebony-Vinyl/dsh-our-free-model"


def test_extracts_gitee_url() -> None:
    refs = extract_repos("镜像在 https://gitee.com/mirrors/electron")
    assert len(refs) == 1
    assert refs[0].platform == "gitee"
    assert refs[0].full_name == "mirrors/electron"


def test_strips_git_suffix() -> None:
    """`.git` 后缀要去掉（否则拼 URL 会变成 `.git` 路径）。"""
    refs = extract_repos("git clone https://github.com/owner/repo.git")
    assert refs[0].full_name == "owner/repo"


def test_accepts_bare_domain() -> None:
    """★ 字幕/口播里的地址常常没有 `https://`（ASR 会吃掉）。"""
    refs = extract_repos("去 github.com/x/y 看看")
    assert refs[0].full_name == "x/y"


def test_rejects_bare_owner_repo() -> None:
    """★★ 没有域名的 `owner/repo` **不认** —— 那是普通斜杠文本，猜了会读错项目。"""
    assert extract_repos("用 owner/repo 这种写法") == []


def test_handles_chinese_punctuation() -> None:
    """中文标点紧跟在 URL 后面时也要能正确截断。"""
    refs = extract_repos("仓库是https://github.com/a/b，欢迎star。")
    assert refs[0].full_name == "a/b"


# ---------------------------------------------------------------------------
# 排除误判
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/features/actions",
        "https://github.com/topics/python",
        "https://github.com/trending",
        "https://github.com/marketplace",
        "https://github.com/sponsors/someone",
        "https://github.com/settings/profile",
    ],
)
def test_skips_github_feature_pages(url: str) -> None:
    """★ GitHub 的功能页不是仓库 —— 拿去读源码会 404。"""
    assert extract_repos(url) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo/issues/123",
        "https://github.com/owner/repo/pulls",
        "https://github.com/owner/repo/releases",
        "https://github.com/owner/repo/wiki",
    ],
)
def test_subpaths_resolve_to_repo_root(url: str) -> None:
    """★ 子路径要归到**仓库根**（`/issues/123` → `owner/repo`）。"""
    refs = extract_repos(url)
    assert len(refs) == 1
    assert refs[0].full_name == "owner/repo"


def test_dedupes_same_repo() -> None:
    """同一个仓库出现多次只算一个。"""
    text = "https://github.com/a/b 和 https://github.com/A/B 和 https://github.com/a/b.git"
    refs = extract_repos(text)
    assert len(refs) == 1, "大小写不同也该去重"


def test_respects_limit() -> None:
    text = " ".join(f"https://github.com/o{i}/r{i}" for i in range(10))
    assert len(extract_repos(text, limit=3)) == 3


def test_empty_input() -> None:
    assert extract_repos("") == []
    assert extract_repos("没有任何链接的一段话") == []


# ---------------------------------------------------------------------------
# 视频三来源
# ---------------------------------------------------------------------------


def test_description_has_priority() -> None:
    """★ 简介优先 —— 那是 UP 主**主动写的**，字幕可能被 ASR 写错。"""
    refs = extract_from_video(
        title="标题",
        description="仓库：https://github.com/from/description",
        subtitle="github.com/from/subtitle",
    )
    assert refs[0].full_name == "from/description"


def test_all_three_sources_collected() -> None:
    refs = extract_from_video(
        title="见 https://github.com/from/title",
        description="https://github.com/from/description",
        subtitle="github.com/from/subtitle",
    )
    names = [r.full_name for r in refs]
    assert "from/description" in names
    assert "from/subtitle" in names
    assert "from/title" in names


def test_video_sources_deduped() -> None:
    refs = extract_from_video(
        description="https://github.com/a/b", subtitle="github.com/a/b"
    )
    assert len(refs) == 1


def test_video_with_no_repo() -> None:
    """没有仓库地址就返回空 —— **不猜**。"""
    assert extract_from_video(title="某插件测评", description="讲得很好", subtitle="内容") == []


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


def test_url_is_rebuilt_correctly() -> None:
    ref = RepoRef(platform="github", owner="a", repo="b")
    assert ref.url == "https://github.com/a/b"
    gitee = RepoRef(platform="gitee", owner="m", repo="n")
    assert gitee.url == "https://gitee.com/m/n"


def test_to_dict_is_json_friendly() -> None:
    import json

    ref = RepoRef(platform="github", owner="a", repo="b")
    payload = json.loads(json.dumps(ref.to_dict(), ensure_ascii=False))
    assert payload["full_name"] == "a/b"


def test_describe_includes_fetch_hint() -> None:
    """★ 描述里要提示"用哪条通道读源码"（避免又踩 codeload 慢的坑）。"""
    info = describe(RepoRef(platform="github", owner="a", repo="b"))
    assert "gh-proxy" in info["fetch_hint"]
    assert "jsdelivr" in info["fetch_hint"]

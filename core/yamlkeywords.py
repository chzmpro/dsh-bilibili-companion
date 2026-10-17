"""把关键词**安全地**写进 ``config.yaml``（人用 CLI 采纳候选词时用）。

## 为什么单独一个模块

`config.yaml` 是**授权文件**：既有"普通偏好"（关键词、每天几条），
也有"**改授权**"的字段（`mode` / `dry_run` / `publish_enabled`）。

★ 本模块**只碰 `discovery.keywords` 一个字段**，其余**逐行原样保留**。

## 为什么用"逐行替换"而不是 YAML 解析再写回

解析再写回会**把注释全丢掉** —— 而本项目的 `config.yaml` 几乎每行都有注释，
那些注释解释了"为什么是这个值"（例如"curator 原值 lookback_hours=48"）。
逐行替换只动目标行，**注释与格式都不变**。

这与 DSH 自己的 patch 语义一致：**按整行替换，不做深合并**。

> 姊妹实现：插件的 `plugin/lib/pyconfig.mjs`（Node 侧）做同样的事，
> 供设置页改"谷时开关 / 关键词"。两边纪律一致：**只动白名单字段**。
"""

from __future__ import annotations

from pathlib import Path

from core import paths

#: 我们只允许改这一个键（顶层 `discovery` 下的 `keywords`）
_SECTION = "discovery"
_KEY = "keywords"


def config_yaml_path() -> Path:
    return paths.project_root() / "config.yaml"


def _find_keywords_block(lines: list[str]) -> tuple[int, int]:
    """找到 ``discovery:`` 段里 ``keywords:`` 那一行与它的块状列表范围。

    Returns:
        ``(key_line_index, last_item_index)``；找不到返回 ``(-1, -1)``。
        ``last_item_index == key_line_index`` 表示"没有列表项"。
    """
    section_start = -1
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped == f"{_SECTION}:":
            section_start = i
            break
    if section_start == -1:
        return -1, -1

    key_line = -1
    for i in range(section_start + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        # 缩进回到 0 ⇒ 已经出了这一节
        if not line.startswith((" ", "\t")):
            break
        if line.strip() == f"{_KEY}:":
            key_line = i
            break
    if key_line == -1:
        return -1, -1

    last_item = key_line
    for i in range(key_line + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        if line.lstrip().startswith("- "):
            last_item = i
            continue
        break
    return key_line, last_item


def read_keywords() -> list[str]:
    """读出当前的关注关键词（读不到就返回空列表，**不抛错**）。"""
    path = config_yaml_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    key_line, last_item = _find_keywords_block(lines)
    if key_line == -1:
        return []

    out: list[str] = []
    for i in range(key_line + 1, last_item + 1):
        text = lines[i].strip()
        if text.startswith("- "):
            value = text[2:].strip().strip('"').strip("'")
            if value:
                out.append(value)
    return out


def add_keyword(keyword: str) -> tuple[bool, str]:
    """把一个关键词加进 ``discovery.keywords``（**去重**）。

    Returns:
        ``(成功, 说明)``。失败原因用**人话**（供 CLI 直接打印）。
    """
    text = keyword.strip()
    if not text:
        return False, "关键词不能为空"

    path = config_yaml_path()
    if not path.exists():
        return False, f"找不到 {path}（请先 cp config.example.yaml config.yaml）"

    original = path.read_text(encoding="utf-8")
    lines = original.split("\n")
    key_line, last_item = _find_keywords_block(lines)
    if key_line == -1:
        return False, f"config.yaml 里找不到 {_SECTION}.{_KEY} 这一段"

    existing = read_keywords()
    if any(item.lower() == text.lower() for item in existing):
        return True, "已经在关键词列表里了（未重复添加）"

    # 沿用已有列表项的缩进；没有就按"父缩进 + 2 空格"
    indent = "    "
    if last_item > key_line:
        raw = lines[key_line + 1]
        indent = raw[: len(raw) - len(raw.lstrip())]

    new_line = f'{indent}- "{text}"'
    lines.insert(last_item + 1, new_line)

    # 先备份再写（出问题能手工对照）
    import contextlib

    with contextlib.suppress(OSError):
        path.with_suffix(path.suffix + ".bak-keywords").write_text(original, encoding="utf-8")

    path.write_text("\n".join(lines), encoding="utf-8")
    return True, f"已写进 {path.name}（共 {len(existing) + 1} 个关键词）"

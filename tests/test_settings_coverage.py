"""设置页字段覆盖度测试（2026-10-17）。

## 为什么有这个测试

主人反馈：*"blb 我记得他的设置项有很多可以自己设置的，你怎么没有学习"*。

查下来：参考项目 `bilibili_learning_bot` 有 **391** 个配置项，
而我们的设置页当时只开放了 **46 / 70** —— 剩下 18 个既没开放、
也没有在代码里说明"为什么锁着"。这类"悄悄不开放"最难发现，
所以用测试钉住。

## 三条不变量

1. **不许有幽灵字段**：`EDITABLE_KEYS` 里的每一项都必须真的存在于 `Config`；
2. **不许有隐形字段**：`Config` 里的每个叶子字段，必须属于
   「可编辑」或「明确锁定」之一 —— **不允许既不开放也不说明**；
3. **授权字段永远锁着**：`interaction.mode` 等 6 项**绝不能**出现在可编辑清单里。

> ⚠️ 身份类字段（`account.*`）单独归入"只读身份"，
> 因为它们改错会让小号直接工作不了，但又不属于"授权"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.config import Config

PYCONFIG = Path(__file__).resolve().parent.parent / "plugin" / "lib" / "pyconfig.mjs"

#: 只读身份字段 —— 界面上**不提供**编辑控件（改错会让小号工作不了）。
#: 这些不是"授权"，而是"身份"，所以与 NEVER_EDITABLE 分开列出。
IDENTITY_READ_ONLY: frozenset[str] = frozenset(
    {
        "account.agent_mid",
        "account.owner_mid",
        "account.owner_name_hint",
    }
)


def _config_leaves() -> set[str]:
    """`Config` 的全部叶子字段（`a.b` 形式）。"""
    def walk(node: object, prefix: str = "") -> list[str]:
        out: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                name = f"{prefix}{key}"
                if isinstance(value, dict):
                    out.extend(walk(value, f"{name}."))
                else:
                    out.append(name)
        return out

    return set(walk(Config().model_dump()))


def _pyconfig_keys() -> tuple[set[str], set[str]]:
    """从 `pyconfig.mjs` 里解析出 `(可编辑, 永不可编辑)`。"""
    source = PYCONFIG.read_text(encoding="utf-8")

    editable_block = re.search(r"EDITABLE_KEYS\s*=\s*Object\.freeze\(\{(.*?)\n\}\)", source, re.S)
    never_block = re.search(r"NEVER_EDITABLE\s*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)

    editable = set(
        re.findall(r"'([a-z_]+\.[a-z_]+)'\s*:", editable_block.group(1) if editable_block else "")
    )
    never = set(
        re.findall(r"'([a-z_]+(?:\.[a-z_]+)+)'", never_block.group(1) if never_block else "")
    )
    return editable, never


def test_pyconfig_file_exists() -> None:
    assert PYCONFIG.is_file(), f"找不到 {PYCONFIG}"


def test_no_ghost_editable_fields() -> None:
    """★ 可编辑清单里的字段必须真的存在于 `Config`。

    幽灵字段会让设置页**显示一个能存但没人读的值** —— 用户以为改了，其实没生效。
    """
    editable, _ = _pyconfig_keys()
    leaves = _config_leaves()
    ghost = sorted(editable - leaves)
    assert ghost == [], f"EDITABLE_KEYS 里有不存在的字段：{ghost}"


def test_no_invisible_config_fields() -> None:
    """★★ 每个配置字段都要有归属：可编辑 / 明确锁定 / 只读身份。

    这条是主人那句"设置项太少"的结构性防线：
    以后新增配置字段时，**必须**决定它归哪一类，
    不允许出现"加了字段但界面完全没有、代码里也不说明"的情况。
    """
    editable, never = _pyconfig_keys()
    leaves = _config_leaves()
    unaccounted = sorted(leaves - editable - never - IDENTITY_READ_ONLY)
    assert unaccounted == [], (
        f"这些配置字段既不可编辑、也没标为锁定/只读身份：{unaccounted}\n"
        "处理方式：加进 EDITABLE_KEYS（可调参）或 NEVER_EDITABLE（授权），"
        "或加进本测试的 IDENTITY_READ_ONLY（身份）。"
    )


def test_authorization_fields_never_editable() -> None:
    """★★ 授权字段**绝不能**出现在可编辑清单里。

    界面上点一下就能把发布打开 = 绕过了"人审"这道闸门。
    """
    editable, never = _pyconfig_keys()
    required = {
        "interaction.mode",
        "interaction.dry_run",
        "interaction.publish_enabled",
        "interaction.pause",
        "interaction.dm_reply_enabled",
        "text_worker.api_key_env",
    }
    assert required <= never, f"这些授权字段没被标为永不可编辑：{sorted(required - never)}"
    leaked = required & editable
    assert leaked == set(), f"授权字段泄漏到可编辑清单：{sorted(leaked)}"


def test_identity_fields_not_editable() -> None:
    """身份字段不该出现在设置页（改错小号就废了）。"""
    editable, _ = _pyconfig_keys()
    leaked = IDENTITY_READ_ONLY & editable
    assert leaked == set(), f"身份字段不该可编辑：{sorted(leaked)}"


def test_settings_coverage_is_reasonable() -> None:
    """普通偏好覆盖度不退步；部署命令与凭据引用不属于偏好控件。"""
    editable, _ = _pyconfig_keys()
    leaves = {key for key in _config_leaves()
              if not key.startswith("external_platforms.") and key != "ocr.command"}
    ratio = len(editable) / len(leaves)
    assert ratio >= 0.8, (
        f"设置页只开放了 {len(editable)}/{len(leaves)} = {ratio:.0%}，"
        "低于 80% —— 主人抱怨过设置项太少，不要退回去"
    )


@pytest.mark.parametrize(
    "section",
    ["bilibili_requests", "report"],
)
def test_new_sections_are_exposed(section: str) -> None:
    """★ 2026-10-17 新增的两段（限速 / 日报）必须真的开放出来。"""
    editable, _ = _pyconfig_keys()
    names = {path for path in editable if path.startswith(f"{section}.")}
    assert names, f"{section}.* 一个字段都没开放"

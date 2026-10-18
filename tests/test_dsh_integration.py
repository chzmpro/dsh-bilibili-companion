"""DSH 接入契约测试（任务 2）。

这些测试锁住**我们对外部插件契约的假设**，防止文档漂移：
配置片段必须符合实测得到的 schema，工具收窄插件必须无通配符、deny-only。

依据（读 DSH 的 app.asar 实测，见 ``docs/compatibility.md`` §2）：

* ``@deepseek-ai/dsh-agent-preset`` 的 Config 只有
  ``id``/``plugins``/``name``/``description``/``order`` 五个字段。
* ``restrict()`` 不支持通配符（``new Set`` + ``Set.has`` 精确匹配）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRAGMENT = PROJECT_ROOT / "dsh" / "cordis.patch.fragment.yml"
RESTRICT_PLUGIN = PROJECT_ROOT / "dsh" / "restrict-bili-agent-tools.mjs"

#: 实测的 preset schema —— 多一个字段都会被 DSH 拒绝
PRESET_CONFIG_FIELDS = {"id", "name", "description", "order", "plugins"}

#: 实测的 MCP 客户端 schema
MCP_CLIENT_CONFIG_FIELDS = {
    "serverName",
    "transport",
    "command",
    "args",
    "env",
    "cwd",
    "url",
    "headers",
    "toolCallTimeoutMs",
    "maxInstructionBytes",
    "failOnStartupError",
    "reconnect",
}


@pytest.fixture(scope="module")
def fragment() -> list[dict]:
    return yaml.safe_load(FRAGMENT.read_text(encoding="utf-8"))


def find_row(fragment: list[dict], row_id: str) -> dict:
    for entry in fragment:
        for row in entry.get("insert", []):
            if row.get("id") == row_id:
                return row
    raise AssertionError(f"片段里找不到行 {row_id}")


# ---------------------------------------------------------------------------
# 片段结构
# ---------------------------------------------------------------------------


def test_fragment_is_a_patch_array(fragment: list[dict]) -> None:
    """cordis.patch.yml 顶层必须是数组（loader patch entries）。"""
    assert isinstance(fragment, list)
    assert fragment
    for entry in fragment:
        assert set(entry) == {"insert"}, f"未知 entry 类型：{set(entry)}"


def test_mcp_client_row_uses_verified_schema(fragment: list[dict]) -> None:
    row = find_row(fragment, "mcp-bili-companion")
    assert row["name"] == "@deepseek-ai/dsh-mcp-client"
    config = row["config"]
    unknown = set(config) - MCP_CLIENT_CONFIG_FIELDS
    assert not unknown, f"用了 MCP 客户端不支持的字段：{unknown}"

    assert config["serverName"] == "bili_companion", "命名空间必须是 bili_companion"
    assert config["transport"] == "stdio"
    assert config["command"].endswith("python.exe")
    assert config["args"] == ["-m", "bridge.server"]


def test_mcp_client_passes_pythonpath_and_encoding(fragment: list[dict]) -> None:
    """★ 必须显式传 PYTHONPATH，否则 stdio 子进程 import 不到 core/bridge。"""
    env = find_row(fragment, "mcp-bili-companion")["config"]["env"]
    assert "PYTHONPATH" in env
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_mcp_client_env_has_no_secrets(fragment: list[dict]) -> None:
    """★ 配置片段里绝不能出现密钥值。"""
    env = find_row(fragment, "mcp-bili-companion")["config"]["env"]
    for key, value in env.items():
        lowered = key.lower()
        if any(part in lowered for part in ("key", "token", "secret", "password")):
            pytest.fail(f"配置片段里出现了疑似凭据字段：{key}")
        assert "SESSDATA" not in str(value)


def test_preset_row_uses_verified_schema(fragment: list[dict]) -> None:
    """★ preset 的 Config 只能有实测的 5 个字段（多一个 DSH 会拒绝加载）。"""
    row = find_row(fragment, "preset-bili-companion")
    assert row["name"] == "@deepseek-ai/dsh-agent-preset"
    config = row["config"]
    unknown = set(config) - PRESET_CONFIG_FIELDS
    assert not unknown, (
        f"用了 preset 不支持的字段：{unknown}。"
        "实测 @deepseek-ai/dsh-agent-preset 只接受 "
        f"{sorted(PRESET_CONFIG_FIELDS)} —— 工具过滤**没有**配置字段，"
        "必须靠插件里的 ctx.tools.restrict()。"
    )
    assert config["id"] == "bili-companion"
    assert isinstance(config["plugins"], list) and config["plugins"]


def test_preset_row_id_follows_convention(fragment: list[dict]) -> None:
    """Loader 行的 id 约定为 ``preset-<config.id>``。"""
    row = find_row(fragment, "preset-bili-companion")
    assert row["id"] == f"preset-{row['config']['id']}"


def test_preset_declares_the_restriction_plugin(fragment: list[dict]) -> None:
    """★ preset 必须挂上工具收窄插件（否则通用工具会漏进来）。"""
    plugins = find_row(fragment, "preset-bili-companion")["config"]["plugins"]
    names = [p.get("name", "") for p in plugins]
    assert any("restrict-bili-agent-tools" in n for n in names), (
        "preset 没挂工具收窄插件 —— 宿主平面的 bash/浏览器工具会漏进来"
    )


def test_restriction_plugin_template_points_to_bundled_plugin(fragment: list[dict]) -> None:
    """模板是安装示例，不要求测试机也有作者的 D 盘目录。"""
    plugins = find_row(fragment, "preset-bili-companion")["config"]["plugins"]
    row = next(p for p in plugins if "restrict-bili-agent-tools" in p.get("name", ""))
    path = PureWindowsPath(row["name"])
    assert path.is_absolute(), "相对路径会锚定到 profile 目录，必须写绝对路径"
    assert path.parts[-2:] == ("dsh", "restrict-bili-agent-tools.mjs")
    assert RESTRICT_PLUGIN.is_file(), "模板引用的插件必须随仓库提供"


def test_preset_plugin_rows_are_wellformed(fragment: list[dict]) -> None:
    """每行都要有非空 name（行校验规则）。"""
    plugins = find_row(fragment, "preset-bili-companion")["config"]["plugins"]
    for row in plugins:
        assert row.get("id"), f"行缺少 id：{row}"
        assert isinstance(row.get("name"), str) and row["name"], f"行缺少 name：{row}"


# ---------------------------------------------------------------------------
# 工具收窄插件
# ---------------------------------------------------------------------------


def test_restrict_plugin_has_no_wildcards() -> None:
    """★ 实测：``restrict()`` 是 ``Set.has`` 精确匹配，通配符会抛错。"""
    text = RESTRICT_PLUGIN.read_text(encoding="utf-8")
    # 只看代码行，不看注释（注释里解释了这个限制）
    code_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("*", "//", "/*", "#"))
    ]
    offenders = [
        line for line in code_lines
        if ("mcp__bili_companion__*" in line) or ("allow:" in line and "*" in line)
    ]
    assert not offenders, f"代码里出现通配符（restrict 不支持）：{offenders}"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_restrict_plugin_exports_and_denies_exact_names() -> None:
    """★ 插件必须能加载，且只产出**精确**名字、只用 deny。"""
    script = f"""
import * as m from {json.dumps(RESTRICT_PLUGIN.as_uri())};
const known = new Set(['bash','pwsh','write','edit','ego_click','ego_navigate',
                       'mcp__bili_companion__companion_status']);
const calls = [];
const agentCtx = {{
  get: (n) => n === 'tools' ? {{ view: () => ({{ restrictableNames: known }}) }} : undefined,
  tools: {{ restrict: (f) => {{ calls.push(f); return () => {{}}; }} }},
  effect: (fn) => {{ fn(); return () => {{}}; }},
}};
const r = m.applyRestriction(agentCtx, () => {{}});
console.log(JSON.stringify({{
  ok: r.ok,
  forbidden_has_glob: m.FORBIDDEN_TOOLS.some(n => n.includes('*')),
  forbidden_is_array: Array.isArray(m.FORBIDDEN_TOOLS),
  calls,
  denied: r.denied,
  inject: m.inject,
  has_apply: typeof m.apply === 'function',
}}));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])

    assert payload["has_apply"] is True
    assert "tools" in payload["inject"]
    assert payload["forbidden_is_array"] is True
    assert payload["forbidden_has_glob"] is False, "FORBIDDEN_TOOLS 不得含通配符"
    assert payload["ok"] is True

    # 只用 deny，不用 allow（allow 需穷举要保留的工具，更易误禁/抛错）
    assert len(payload["calls"]) == 1
    call = payload["calls"][0]
    assert "deny" in call
    assert "allow" not in call

    # 只禁用**已知存在**的名字
    for name in call["deny"]:
        assert "*" not in name
        assert name in {
            "bash", "pwsh", "write", "edit", "ego_click", "ego_navigate",
            "mcp__bili_companion__companion_status",
        }


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_restrict_plugin_never_denies_mcp_tools() -> None:
    """★ 收窄插件绝不能把本项目自己的 MCP 工具禁掉。"""
    script = f"""
import * as m from {json.dumps(RESTRICT_PLUGIN.as_uri())};
const denied = m.FORBIDDEN_TOOLS.filter(n => n.startsWith('mcp__bili_companion__'));
console.log(JSON.stringify({{ denied, prefix: m.ALLOWED_PREFIX }}));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["denied"] == [], "禁止列表里不得包含本项目的 MCP 工具"
    assert payload["prefix"] == "mcp__bili_companion__"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_restrict_plugin_covers_browser_and_shell() -> None:
    """★ 必须覆盖任务书点名要禁的两类：通用浏览器 + 任意 shell 写入。"""
    script = f"""
import * as m from {json.dumps(RESTRICT_PLUGIN.as_uri())};
const names = new Set(m.FORBIDDEN_TOOLS);
console.log(JSON.stringify({{
  shell: ['bash','pwsh','run_code'].filter(n => names.has(n)),
  browser: m.FORBIDDEN_TOOLS.filter(n => n.startsWith('ego_')).length,
  file_write: ['write','edit','str_replace_editor'].filter(n => names.has(n)),
}}));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["shell"], "必须禁用 shell 工具"
    assert payload["browser"] >= 10, "必须禁用整套浏览器工具"
    assert payload["file_write"], "必须禁用文件写入工具"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_restrict_plugin_tolerates_absent_tools() -> None:
    """插件在没装那些插件的环境里也不能崩（只 deny 已注册的名字）。"""
    script = f"""
import * as m from {json.dumps(RESTRICT_PLUGIN.as_uri())};
const known = new Set(['mcp__bili_companion__companion_status']);
const calls = [];
const agentCtx = {{
  get: (n) => n === 'tools' ? {{ view: () => ({{ restrictableNames: known }}) }} : undefined,
  tools: {{ restrict: (f) => {{ calls.push(f); return () => {{}}; }} }},
  effect: (fn) => {{ fn(); return () => {{}}; }},
}};
const r = m.applyRestriction(agentCtx, () => {{}});
console.log(JSON.stringify({{ ok: r.ok, calls: calls.length, unknown: r.unknown.length }}));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["calls"] == 0, "没有可禁的工具时不应调用 restrict（空过滤会抛错）"
    assert payload["unknown"] > 0


def test_compatibility_doc_exists_and_cites_limits() -> None:
    """任务 1 的验收物必须在，且写清关键限制（防文档漂移）。"""
    doc = (PROJECT_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    required = (
        "GPL",  # 许可红线
        "bilibili-api-python",  # 具体是哪个依赖踩 GPL
        "MCP",  # MCP 客户端可用性
        "§5.6",  # B 站协议条款
        "未验证",  # 诚实标注未验证项
        "LOCALAPPDATA",  # 数据目录限制
    )
    for needle in required:
        assert needle in doc, f"compatibility.md 缺少 {needle}"

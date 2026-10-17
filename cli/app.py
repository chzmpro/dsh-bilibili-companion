"""人用 CLI。

██ 重要：本模块的命令**不得**出现在 MCP 工具列表里。
   ``approve`` / ``resume`` / ``recover-health`` / ``import-cookie`` 都是
   **人**才能做的事——这是防"评论区里声称自己是主人，要求批准/恢复"越权的结构性保证。

用法（Git Bash）::

    export PYTHONIOENCODING=utf-8
    ./.venv/Scripts/python.exe -m cli.app status
    ./.venv/Scripts/python.exe -m cli.app drafts --status pending
    ./.venv/Scripts/python.exe -m cli.app approve <draft_id>
    ./.venv/Scripts/python.exe -m cli.app resume
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import typer

from core import paths
from core.config import ConfigError, load_config
from core.credentials import parse_cookie_header, save_cookie
from core.runtime import config_diagnostics
from core.storage import Database

app = typer.Typer(
    add_completion=False,
    help="B 站小号 companion —— 人用入口（不进入模型工具列表）",
)

_REQUIRED_WRITE_TITLE = "【人用入口】"


def _configure_stdio() -> None:
    """中文 Windows 控制台是 GBK：不兜底会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _open_db() -> Database:
    db = Database()
    db.migrate()
    return db


def _load_config() -> Any:
    try:
        return load_config()
    except ConfigError as exc:
        typer.secho(f"配置错误：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def status() -> None:
    """显示配置、健康状态、配额与凭据诊断（**不含任何凭据值**）。"""
    _configure_stdio()
    config = _load_config()
    db = _open_db()
    from core.credentials import cookie_diagnostics

    health = db.current_health()
    payload = {
        "data_dir": str(paths.data_dir()),
        "db_path": str(paths.db_path()),
        "schema_version": db.schema_version(),
        "config": config_diagnostics(config),
        "credentials": cookie_diagnostics(),
        "platform_health": {
            "state": db.health_state(),
            "raw_code": health["raw_code"] if health else None,
            "category": health["category"] if health else "",
            "detail": health["detail"] if health else "",
        },
        "quota_today": {
            "top_real": db.count_actions_today(action_type="top", is_simulated=False),
            "top_simulated": db.count_actions_today(action_type="top", is_simulated=True),
            "reply_real": db.count_actions_today(action_type="reply", is_simulated=False),
            "reply_simulated": db.count_actions_today(action_type="reply", is_simulated=True),
        },
        "text_jobs": db.text_job_totals(),
        "latest_runs": [dict(row) for row in db.latest_runs(limit=3)],
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    db.close()


@app.command()
def drafts(status: str = typer.Option("", "--status", "-s", help="按状态过滤")) -> None:
    """列出草稿（只读）。"""
    _configure_stdio()
    db = _open_db()
    rows = db.list_drafts(status=status or None, limit=100)
    if not rows:
        typer.echo("（无草稿）")
        db.close()
        return
    for row in rows:
        typer.echo(
            f"{row['draft_id']}  [{row['status']}]  {row['draft_type']}  {row['bvid']}"
            f"  approved_at={row['approved_at']}"
        )
        typer.echo(f"    {row['text'][:120]}")
    db.close()


@app.command()
def approve(draft_id: str) -> None:
    """**人**批准一个草稿（写入 approved_at）。

    这是 ``review`` 模式能发布的唯一途径；模型无权调用。
    """
    _configure_stdio()
    db = _open_db()
    row = db.get_draft(draft_id)
    if row is None:
        typer.secho(f"草稿不存在：{draft_id}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=1)
    typer.echo(f"{_REQUIRED_WRITE_TITLE} 即将批准：")
    typer.echo(f"  类型：{row['draft_type']}  BV：{row['bvid']}")
    typer.echo(f"  正文：{row['text']}")
    typer.echo(f"  证据：{row['evidence_ids']}")
    ok = db.approve_draft(draft_id)
    db.close()
    if not ok:
        typer.secho("批准失败", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"已批准 {draft_id}", fg=typer.colors.GREEN)
    typer.echo("注意：批准 ≠ 已发布。发布仍受 dry_run / 模式 / 配额 / 健康状态约束。")


@app.command()
def reject(draft_id: str) -> None:
    """**人**拒绝一个草稿。"""
    _configure_stdio()
    db = _open_db()
    if db.get_draft(draft_id) is None:
        typer.secho(f"草稿不存在：{draft_id}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=1)
    db.set_draft_status(draft_id, "rejected")
    db.close()
    typer.secho(f"已拒绝 {draft_id}", fg=typer.colors.YELLOW)


@app.command()
def resume(reason: str = typer.Option("", "--reason", help="恢复理由（留档）")) -> None:
    """**人**恢复暂停 / 恢复风控停机。

    ★ 这是唯一能清除 ``verification_required`` / ``auth_expired`` /
    ``platform_error`` 的途径——程序不会自动恢复，计时结束也不会。
    只应在主人**已亲自**完成人机验证或重新扫码之后调用。
    """
    _configure_stdio()
    db = _open_db()
    previous = db.health_state()
    changed = db.recover_health(recovered_by="cli:resume", note=reason)
    db.close()
    if changed:
        typer.secho(f"已恢复：{previous} → ready", fg=typer.colors.GREEN)
    else:
        typer.echo("当前没有待恢复的健康事件。")
    typer.echo(
        "提示：本命令只清除健康状态。"
        "发布模式（mode/dry_run/publish_enabled）仍以 config.yaml 为准，需自行修改。"
    )


@app.command("import-cookie")
def import_cookie(
    source: Path = typer.Argument(..., help="含 cookie 头的文本文件（本机路径）"),  # noqa: B008
    confirm: bool = typer.Option(False, "--yes", help="确认写入凭据文件"),
) -> None:
    """**人**把凭据写入本机数据目录（值**不会**被回显）。

    安全约束：只接受本机文件路径、只打印 cookie 名字与数量。
    """
    _configure_stdio()
    if not confirm:
        typer.secho("需要 --yes 确认。", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    if not source.exists():
        typer.secho(f"源文件不存在：{source}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    bundle = parse_cookie_header(source.read_text(encoding="utf-8"))
    if not bundle.values:
        typer.secho("未能解析出任何 cookie 项。", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    target = save_cookie(bundle)
    typer.secho(f"已写入 {target}", fg=typer.colors.GREEN)
    typer.echo(f"cookie 项数：{len(bundle.values)}")
    typer.echo(f"名字：{', '.join(bundle.names())}")
    typer.echo(f"具备发布所需凭据：{'是' if bundle.has_write_credentials() else '否'}")
    typer.echo("★ 值已写入本机数据目录，未在终端回显，也未进入任何日志。")


@app.command("login-qr")
def login_qr(
    timeout_sec: int = typer.Option(180, "--timeout", help="等扫码的总秒数"),
) -> None:
    """**人**扫码登录小号（终端出二维码 → 手机扫 → 凭据自动写入）。

    这条命令把"手工从浏览器导 cookie"变成"扫一下就完事"。
    接口与踩坑点照竞品实测实现（见 :mod:`core.login`）：

    * ★ **凭证在轮询响应的 ``Set-Cookie`` 里**，**不在** ``data.url`` 里
      （那个 url 是别的域的游戏票）；
    * ``86101`` 未扫 / ``86090`` 已扫待确认 / ``0`` 成功 / ``86038`` 过期。

    ⚠️ 凭据只写入本机数据目录，**不在终端回显、不进日志**。
    """
    _configure_stdio()
    import asyncio

    from core.login import login_flow

    def show_qr(session: Any) -> None:
        typer.echo("")
        typer.secho("请用手机 B站 App 扫码（我的 → 扫一扫）", fg=typer.colors.CYAN)
        typer.echo(f"二维码链接：{session.url}")
        typer.echo("（把上面这行贴进二维码生成器也能扫；或在 DSH 设置页里点扫码登录）")
        typer.echo("")

    state_text = {
        "waiting": "等待扫码…",
        "scanned": "扫到了，请在手机上确认",
        "expired": "二维码过期了",
    }

    def on_state(state: str) -> None:
        text = state_text.get(state)
        if text:
            typer.echo(f"  · {text}")

    max_polls = max(1, timeout_sec // 2)
    outcome = asyncio.run(
        login_flow(on_qr=show_qr, on_state=on_state, max_polls=max_polls)
    )

    if outcome.ok:
        typer.secho(f"\n✅ {outcome.message}", fg=typer.colors.GREEN)
        typer.echo(f"cookie 项数：{outcome.count}")
        typer.echo(f"名字：{', '.join(outcome.names)}")
        typer.echo("★ 值未在终端回显，也未进入任何日志。")
        typer.echo("下一步：跑一次 `cli.app status` 确认身份，再按需配 agent_mid。")
        return

    typer.secho(f"\n❌ 登录没完成：{outcome.message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command("verify-recent")
def verify_recent_cmd(
    limit: int = typer.Option(6, "--limit", help="最多复核几条"),
    no_append: bool = typer.Option(False, "--no-append", help="只打印，不追加进日报"),
) -> None:
    """复核"发出去的评论到底公开可见没有"（**只读**，只发 GET）。

    ★ 为什么要它（照竞品实测）：**接口回"发送成功" ≠ 公开可见**。
    新号/风控会让评论进审核，你自己看不见，而程序以为发出去了、还记进判重表
    ⇒ 下一轮不再发 ⇒ **一条都没到人**。

    ★ 措辞纪律：查不到只写「**没查到**」，**绝不写「没发出去」** ——
    翻页与热度排序都会漏，**观测不到不能反推不存在**。
    """
    _configure_stdio()
    import asyncio

    from core.runtime import build_runtime
    from core.verify import append_verify_to_report, verify_recent

    # ★ 复核要**读评论区** ⇒ 需要带 reader 的 runtime，不能只给 db
    async def _run() -> tuple[Any, int, str]:
        runtime = await build_runtime(with_worker=False)
        try:
            return await verify_recent(runtime, max_items=limit)
        finally:
            await runtime.aclose()

    try:
        results, skipped, text = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"复核失败：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not results:
        typer.echo(text or "没有需要复核的记录。")
        return

    visible = sum(1 for item in results if item.visible is True)
    typer.echo(f"复核 {len(results)} 条：公开可见 {visible} 条，其余「没查到」。")
    typer.echo(text)
    if skipped:
        typer.echo(f"（另外 {skipped} 条最近已确认过，没重复查）")

    if not no_append:
        # 追加到当天日报末尾（日报不存在就**不硬造** —— 照竞品行为）
        import datetime as _dt

        digest = paths.data_dir() / "digests" / f"digest-{_dt.datetime.now():%Y%m%d}.md"
        if append_verify_to_report(digest, text):
            typer.secho(f"已追加到 {digest}", fg=typer.colors.GREEN)
        else:
            typer.echo("（今天还没有日报，所以没追加 —— 不硬造一份）")


@app.command("sources")
def sources_cmd(
    repo: str = typer.Option("", "--repo", help="顺手测一个仓库（owner/repo）"),
    ref: str = typer.Option("main", "--ref", help="分支或 tag"),
) -> None:
    """看**取源码的可用通道**（GitHub 部分主机不通，这里列出实测可用的）。

    ★ 关键结论：**不是"GitHub 不通"，而是"部分主机不通"** ——
    `github.com` 网页与 `raw.githubusercontent.com` 在 **shell** 里不通，
    但 `api.github.com` / `codeload.github.com` / `jsdelivr` / `gitee`
    **全都可用**。所以读源码、下仓库、查版本**都能做**。

    ⚠️ 另外：**浏览器能访问 shell 不通的主机**（走另一条网络路径），
    是最后的人工兜底。
    """
    _configure_stdio()
    from core.sourcefetch import channel_report

    typer.echo("取源码的通道（实测，2026-10-17）：")
    typer.echo("")
    for item in channel_report():
        mark = "✅" if item["works"] else "❌"
        typer.secho(f"  {mark} {item['channel']}", fg=(
            typer.colors.GREEN if item["works"] else typer.colors.RED
        ))
        typer.echo(f"       {item['use_for']}")

    if not repo:
        typer.echo("")
        typer.echo("测一个仓库：cli.app sources --repo owner/repo [--ref main]")
        return

    if "/" not in repo:
        typer.secho("--repo 要写成 owner/repo", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    owner, name = repo.split("/", 1)

    import asyncio

    from core.sourcefetch import fetch_raw_file, fetch_repo_meta, fetch_repo_zip

    async def _probe() -> None:
        typer.echo("")
        typer.echo(f"=== 实测 {repo}@{ref} ===")

        meta = await fetch_repo_meta(owner, name)
        if meta.get("ok"):
            data = meta.get("data") or {}
            typer.echo(f"  元数据：⭐{data.get('stars')} 默认分支={data.get('default_branch')}")
        else:
            typer.echo(f"  元数据：读不到（{meta.get('code')}）")

        text, result = await fetch_raw_file(owner, name, "README.md", ref=ref)
        if result.ok:
            chars = len(text or "")
            typer.secho(
                f"  README：✅ 来自 {result.source}（{chars} 字符）", fg=typer.colors.GREEN
            )
        else:
            typer.secho("  README：❌ 所有源都失败", fg=typer.colors.RED)
            for tried in result.tried:
                typer.echo(f"      {tried}")

        payload, zip_result = await fetch_repo_zip(owner, name, ref=ref)
        if zip_result.ok:
            size_mb = len(payload or b"") / 1048576
            typer.secho(f"  zip：✅ {size_mb:.1f} MB（{zip_result.source}）", fg=typer.colors.GREEN)
        else:
            typer.secho("  zip：❌ 失败", fg=typer.colors.RED)

    asyncio.run(_probe())


@app.command("keywords")
def keywords_cmd(
    accept: str = typer.Option("", "--accept", help="采纳某个候选词（写进 config.yaml）"),
    drop: str = typer.Option("", "--drop", help="丢弃某个候选词"),
) -> None:
    """审阅**候选关键词池**（来自推荐流），决定要不要采纳。

    为什么需要"人审"这一步（2026-10-17 设计决定）：

    推荐流是**个性化算法**喂的 —— 实测一屏 8 条里只有 2~3 条与我们相关，
    其余是鬼屋 / 原神 / 影视飓风这类高互动内容。
    如果候选词**自动**写进关注关键词，**小号的兴趣画像就被算法带跑了**。

    ⇒ 所以：推荐流只**攒候选**，采纳与否由人决定。
    这与项目既有的「授权类改动只能由人做」是同一条原则。

    ★ 采纳时**只动 `discovery.keywords`**；
    授权字段（mode / dry_run / publish_enabled）**一字不动**。
    """
    _configure_stdio()
    from core.trends import drop_suggestion, list_suggestions
    from core.yamlkeywords import add_keyword

    if drop:
        db = _open_db()
        try:
            removed = drop_suggestion(db, drop)
        finally:
            db.close()
        if removed:
            typer.secho(f"已丢弃候选词「{drop}」", fg=typer.colors.GREEN)
        else:
            typer.secho(f"候选池里没有「{drop}」", fg=typer.colors.YELLOW, err=True)
        return

    if accept:
        ok, message = add_keyword(accept)
        if not ok:
            typer.secho(f"采纳失败：{message}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        typer.secho(f"已采纳「{accept}」：{message}", fg=typer.colors.GREEN)
        db = _open_db()
        try:
            drop_suggestion(db, accept)  # 采纳后从候选池移除
        finally:
            db.close()
        return

    db = _open_db()
    try:
        pooled = list_suggestions(db, limit=50)
    finally:
        db.close()

    if not pooled:
        typer.echo("候选池是空的。")
        typer.echo("提示：让 agent 调 trends_suggest 从推荐流里攒候选（只读、限额）。")
        return

    typer.echo(f"候选关键词（{len(pooled)} 个，按出现次数排序）：")
    typer.echo("")
    for item in pooled:
        src = item["sources"][0] if item["sources"] else {}
        typer.echo(f"  {item['keyword']:<20} 出现 {item['hits']} 次")
        if src:
            typer.echo(f"      来源：{src.get('bvid', '')}  {str(src.get('title', ''))[:40]}")
    typer.echo("")
    typer.echo("采纳：cli.app keywords --accept <词>   （写进 config.yaml 的 discovery.keywords）")
    typer.echo("丢弃：cli.app keywords --drop <词>")


@app.command("platforms")
def platforms_cmd() -> None:
    """看各平台的接入现状与能力（只读）。

    ★ 抖音 / 小红书**只预留了框架**，尚未接入 ——
    这里如实说明"为什么没做"与"规划中的接入方式"。
    """
    _configure_stdio()
    from core.platforms import platform_overview

    for item in platform_overview():
        mark = "已接入" if item["implemented"] else "未接入"
        typer.secho(f"{item['platform']:<14} {mark}", fg=(
            typer.colors.GREEN if item["implemented"] else typer.colors.YELLOW
        ))
        typer.echo(f"    接入方式：{item['access_mode']}")
        caps = item.get("capabilities") or {}
        available = [k for k, v in caps.items() if v is True]
        if available:
            typer.echo(f"    能力：{', '.join(available)}")
        if item.get("blocked_reason"):
            typer.echo(f"    为什么没做：{item['blocked_reason']}")
        typer.echo("")


@app.command("dm-poll")
def dm_poll(
    reply: bool = typer.Option(
        False, "--reply", help="发现新消息后**自动回复**（默认只看不回）"
    ),
    text: str = typer.Option("", "--text", help="要回复的固定文本；留空则只报告有新消息"),
    confirm: bool = typer.Option(False, "--yes", help="确认会真实发送"),
) -> None:
    """查一次**未读私信**（主人发来的），可选自动回复。

    主人原话：「我想在B站私信你并且你能随时回复……你也不确定别人什么时候评论你，
    你再回复，我私信你应该效果一样」

    ★ 对，**效果一样而且更简单** —— 评论要轮询全站评论串，
    而私信只要查一个数字：``follow_unread``（未读数）。

    ★ 三条约束：

    1. **只查未读数**，数字变了才拉正文（省请求）；
    2. **只回主人** —— 接收者硬编码 ``owner_mid``，代码层无法发给别人；
    3. **幂等** —— 已回复的 ``msg_seqno`` 记在 ``dm_replies`` 表，
       同一个绝不回第二次（否则轮询会刷屏）。

    ⚠️ 默认**只看不回**；要真发得同时给 ``--reply --text "..." --yes``。
    """
    _configure_stdio()
    import asyncio

    from core.credentials import CredentialError, load_cookie
    from core.dm import dm_reply_allowed
    from core.dm_live import poll_once, reply_to
    from core.ratelimit import RequestQueue

    config = _load_config()
    allowed, reason = dm_reply_allowed(config)
    if not allowed:
        typer.secho(f"私信门禁拒绝：{reason}", fg=typer.colors.RED, err=True)
        typer.echo("提示：私信由 interaction.dm_reply_enabled 单独控制。")
        raise typer.Exit(code=1)

    try:
        bundle = load_cookie()
    except CredentialError as exc:
        typer.secho(f"没有可用凭据：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3) from exc

    db = _open_db()
    db.migrate()
    queue = RequestQueue(db=db, config=config)

    async def _run() -> int:
        try:
            outcome = await poll_once(
                db=db, config=config, queue=queue, cookies=bundle.values
            )
        finally:
            await queue.aclose()

        typer.echo(f"未读数（主人发来的）：{outcome.unread}")
        if outcome.note:
            typer.echo(f"说明：{outcome.note}")

        if not outcome.new_messages:
            return 0

        typer.echo("")
        typer.echo(f"发现 {len(outcome.new_messages)} 条未回复的消息：")
        for item in outcome.new_messages:
            stamp = item.get("timestamp") or 0
            when = ""
            if stamp:
                import datetime as _dt

                when = _dt.datetime.fromtimestamp(int(stamp)).strftime("%m-%d %H:%M")
            typer.echo(f"  [{when}] seqno={item['msg_seqno']}")
            typer.echo(f"    {item['text'][:120]}")
            if item.get("bvid"):
                typer.echo(
                    f"    （分享卡片：{item['bvid']} {str(item.get('title', ''))[:40]}）"
                )

        if not reply:
            typer.echo("")
            typer.echo('（只查看，未回复。要回复加 --reply --text "..." --yes）')
            return 0

        if not text.strip():
            typer.secho("--reply 需要同时给 --text", fg=typer.colors.RED, err=True)
            return 1
        if not confirm:
            typer.secho("真实发送需要 --yes 确认。", fg=typer.colors.YELLOW, err=True)
            return 1

        # 只回**最新一条**（避免一次刷多条，也符合"像人说话"）
        target = outcome.new_messages[-1]
        ok, message = await reply_to(
            db=db,
            config=config,
            cookies=bundle,
            msg_seqno=int(target["msg_seqno"]),
            text=text,
        )
        if ok:
            typer.secho(f"\n✅ {message}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"\n❌ {message}", fg=typer.colors.RED, err=True)
        return 0 if ok else 1

    try:
        code = asyncio.run(_run())
    finally:
        db.close()
    raise typer.Exit(code=code)


@app.command("dm-watch")
def dm_watch(
    port: int = typer.Option(8848, "--port", help="面板端口"),
    interval: int = typer.Option(60, "--interval", help="多少秒查一次未读"),
    reply: bool = typer.Option(
        False, "--reply", help="开启**自动回复**（默认只观察，不发送）"
    ),
    text: str = typer.Option("", "--text", help="自动回复的固定话术（留空用默认）"),
    autostart: bool = typer.Option(
        True, "--autostart/--no-autostart", help="打开面板就自动开始监听"
    ),
    deliver_delay: int = typer.Option(
        60,
        "--deliver-delay",
        help="启动后多少秒内**不投递**（给主人时间打开值班员会话）",
    ),
) -> None:
    """**常驻私信监听 + 单页面板**（仿 `bilibili_learning_bot` 形态）。

    ★ 为什么需要它 —— 结构性差距（对比参考项目源码后确认）：

    `bilibili_learning_bot` 能"及时回复"，靠的是**一个常驻进程**
    （`subprocess.Popen([sys.executable, "-m", "brain.monitor"])`，
    内部 `while self.running` 无限轮询）。

    而本项目是 **DSH 插件 + 对话**形态 —— 只在用户说话时醒，
    MCP 的 22 个工具里没有任何"监听"能力。

    ⇒ 这个命令就是那个**不属于对话的常驻进程**。

    ⚠️ 关掉这个窗口，监听就停（与参考项目一样，没有服务在跑就收不到消息）。

    ★ 默认**只观察不回复**：自动回复说错话比晚回一会儿更糟。
    确认能及时收到后，再加 ``--reply``。
    """
    _configure_stdio()
    import asyncio

    from core.credentials import CredentialError, load_cookie
    from core.dm_panel import serve
    from core.dm_watch import DmWatcher

    config = _load_config()
    from core.dm import dm_reply_allowed

    allowed, reason = dm_reply_allowed(config)
    if not allowed:
        typer.secho(f"私信门禁拒绝：{reason}", fg=typer.colors.RED, err=True)
        typer.echo("提示：私信由 interaction.dm_reply_enabled 单独控制。")
        raise typer.Exit(code=1)

    if reply:
        typer.echo("⚠️ 已开启**自动回复** —— 会自动给你的私信发消息。")
    else:
        typer.echo("（观察模式：只记录发现的消息，**不发送**任何内容）")

    try:
        bundle = load_cookie()
    except CredentialError as exc:
        typer.secho(f"没有可用凭据：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3) from exc

    db = _open_db()
    db.migrate()
    from core.ratelimit import RequestQueue

    queue = RequestQueue(db=db, config=config)

    # ★ 监听跑在**后台线程**，而 sqlite 连接不能跨线程复用
    #   ⇒ 传 db_factory，让那个线程自己建连接（不要共享下面这个 db）。
    def _make_db() -> Any:
        from core.storage import Database as _Db

        return _Db()

    # ★ RequestQueue 内部也持有 db（记健康状态要写库）⇒ 必须一起重建
    def _make_queue(thread_db: Any) -> Any:
        from core.ratelimit import RequestQueue as _Q

        return _Q(db=thread_db, config=config)

    watcher = DmWatcher(
        db=db,
        config=config,
        queue=queue,
        cookies=bundle,
        interval=interval,
        auto_reply=reply,
        reply_text=text,
        deliver_delay_seconds=deliver_delay,
        db_factory=_make_db,
        queue_factory=_make_queue,
    )

    try:
        serve(watcher, port=port, autostart=autostart)
    finally:
        # 收摊：关掉共享的 http 客户端，避免端口/handle 泄漏
        with contextlib.suppress(Exception):
            watcher.close()
        with contextlib.suppress(Exception):
            asyncio.run(queue.aclose())
        db.close()


@app.command("evolve")
def evolve(
    apply: str = typer.Option("", "--apply", help="**批准并写入**这份提案（传 proposal_id）"),
    reject: str = typer.Option("", "--reject", help="拒绝这份提案（传 proposal_id）"),
    show: bool = typer.Option(False, "--show", help="看框架与待审提案（默认行为）"),
    propose: bool = typer.Option(
        False, "--propose", help="生成一份提案（用当前配置与最近数据推导）"
    ),
    parameter: str = typer.Option(
        "", "--param", help="配合 --propose：要调哪个参数（留空=自动挑）"
    ),
) -> None:
    """**自我进化**：看提案 / 批准 / 拒绝。

    ★ 四道硬闸（`core/evolution.py`）：

    1. `auto_apply` **写死 False** —— 提案永不自动生效；
    2. 单次步长上限 5%；
    3. 参数硬边界，越界**直接拒绝**；
    4. 每天最多 2 份提案。

        ★ AI 只能**生成**提案（`pending`）；**批准/拒绝只能由你在终端做**。
    配置里点一下就把参数改掉，等于绕过了人审。
    """
    _configure_stdio()
    from core import evolution, evolution_store

    db = _open_db()
    db.migrate()

    if not (apply or reject or propose or show):
        show = True

    if propose:
        config = _load_config()
        target = parameter.strip()
        if not target:
            # 自动挑：优先挑还没在待审里的
            pending_params = {
                item.parameter
                for item in evolution_store.recent(
                    db, limit=50, status=evolution_store.STATUS_PENDING
                )
            }
            for candidate in evolution.EVOLVABLE_PARAMETERS:
                full = evolution.PARAMETER_PATHS.get(candidate, candidate)
                if full not in pending_params:
                    target = full
                    break
        # ★ 短名/完整路径都可以 —— 统一交给 evolution.full_path 归一化
        if target:
            try:
                target = evolution.full_path(target)
            except evolution.EvolutionError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                db.close()
                raise typer.Exit(code=1) from exc
        if not target:
            typer.secho("没有可提的参数（都已待审）", fg=typer.colors.YELLOW)
            db.close()
            raise typer.Exit(code=1)

        try:
            old_value = evolution_store.default_old_value(target, config)
        except evolution.EvolutionError as exc:
            typer.secho(f"读不到现值：{exc}", fg=typer.colors.RED, err=True)
            db.close()
            raise typer.Exit(code=1) from exc

        # 建议方向：这里只做**一个小步**的示例提案（真实调参应由数据分析驱动）
        short = evolution.short_name(target)
        low, high, _ = evolution.EVOLVABLE_PARAMETERS[short]
        step = evolution.MAX_PARAMETER_STEP if high <= 1.0 else max(1.0, abs(old_value) * 0.05)
        suggestion = min(high, old_value + step)
        stored, message = evolution_store.propose(
            db,
            parameter=target,
            old_value=old_value,
            new_value=suggestion,
            reason="手动触发：基于当前配置的小步调整建议",
        )
        if stored is None:
            typer.secho(f"未生成：{message}", fg=typer.colors.YELLOW)
            db.close()
            raise typer.Exit(code=1)
        typer.secho(f"✅ 已生成提案 {stored.proposal_id}", fg=typer.colors.GREEN)
        typer.echo(f"   {stored.parameter}: {stored.old_value} → {stored.new_value}")
        typer.echo(f"   {message}")
        db.close()
        return

    if apply:
        from core.paths import project_root

        ok, message = evolution_store.apply_proposal(
            db,
            proposal_id=apply.strip(),
            config_path=project_root() / "config.yaml",
        )
        if ok:
            typer.secho(f"✅ {message}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"❌ {message}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=0 if ok else 1)

    if reject:
        ok, message = evolution_store.reject(db, proposal_id=reject.strip())
        if ok:
            typer.secho(f"✅ {message}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"❌ {message}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=0 if ok else 1)

    # 默认：看框架与待审
    info = evolution.describe_framework()
    state = evolution_store.summary(db)
    typer.echo("=== 自我进化框架 ===")
    typer.echo(f"  auto_apply（自动生效）: {info['auto_apply']}  ← 写死 False")
    typer.echo(f"  单次步长上限: {info['max_parameter_step']}")
    typer.echo(f"  每日提案上限: {info['daily_proposal_limit']}（今天已提 {state['today_count']}）")
    typer.echo("  可进化参数:")
    for name, spec in info["evolvable"].items():
        typer.echo(f"    · {name}: [{spec['min']}, {spec['max']}] 默认 {spec['default']}")
    typer.echo("  禁改项（授权/账号/密钥）:")
    for name in info["forbidden"]:
        typer.echo(f"    ✗ {name}")
    typer.echo("")
    typer.echo("=== 待审提案 ===")
    if not state["pending"]:
        typer.echo("  （没有）")
    for item in state["pending"]:
        typer.echo(f"  {item['proposal_id']}  {item['parameter']}")
        typer.echo(f"    {item['old_value']} → {item['new_value']}（Δ{item['delta']:+}）")
        typer.echo(f"    理由：{item['reason']}")
    typer.echo("")
    typer.echo(f"已批准 {state['applied_count']} 份，已拒绝 {state['rejected_count']} 份")
    typer.echo(state["note"])
    typer.echo("")
    typer.echo("批准：cli.app evolve --apply <id>   拒绝：cli.app evolve --reject <id>")
    db.close()


@app.command("seen-export")
def seen_export(
    out: Path | None = typer.Option(  # noqa: B008
        None, "--out", help="写到这个文件；省略则打印到终端"
    ),
) -> None:
    """把"看过什么 / 发过什么"导出成**人类可读的 Markdown**（只读，不改任何状态）。

    来自竞品精读 §2.6：它用 Markdown 判重表（``seen.md``），好处是**人能直接看**。
    我们的状态在 SQLite 里（更可靠），但"主人想手工翻一下"就得写 SQL ——
    这条命令补上那个缺口。

    ⚠️ **只读导出**：SQLite 始终是唯一事实来源，这个文件**不参与判重**。
    """
    _configure_stdio()
    db = _open_db()

    try:
        videos = db.query(
            "SELECT bvid, title, status, up_name, published_at FROM videos "
            "ORDER BY COALESCE(published_at, 0) DESC LIMIT 500"
        )
        actions = db.query(
            "SELECT action_type, status, bvid, remote_rpid, remote_url, created_at "
            "FROM actions ORDER BY created_at DESC LIMIT 500"
        )
        notifications = db.query(
            "SELECT bvid, remote_rpid, status, observed_mid FROM owner_notifications "
            "ORDER BY created_at DESC LIMIT 200"
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"读取失败：{exc}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=1) from exc

    from datetime import datetime

    def ts(value: Any) -> str:
        try:
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return "-"

    lines: list[str] = [
        "# B 站小号判重表（**只读导出**）",
        "",
        f"> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ">",
        "> ⚠️ 这是给人看的**快照**。事实来源是数据库（`companion.db`），",
        "> **这个文件不参与判重** —— 改它不会影响程序行为。",
        "",
        "## 看过的视频",
        "",
        "| BV | 标题 | 状态 | UP | 发布 |",
        "|---|---|---|---|---|",
    ]
    for row in videos:
        title = str(row["title"] or "").replace("|", "\\|")[:60]
        up = str(row["up_name"] or "").replace("|", "\\|")[:20]
        lines.append(
            f"| `{row['bvid']}` | {title} | {row['status']} | {up} | "
            f"{ts(row['published_at'])} |"
        )
    if not videos:
        lines.append("| （还没有看过的视频） | | | | |")

    lines += [
        "",
        "## 评论记录",
        "",
        "> 记 `rpid` 的用处：下一轮能**直接定位**到那条评论，不用在几百条里翻。",
        "> 楼中楼还会记 `root`（复核那条楼层要用）。",
        "",
        "| BV | rpid | root（楼中楼才填） | 时间 | 动作 | 状态 |",
        "|---|---|---|---|---|---|",
    ]
    for row in actions:
        kind = str(row["action_type"])
        rpid = row["remote_rpid"] or ""
        lines.append(
            f"| `{row['bvid'] or '-'}` | {rpid} |  | {ts(row['created_at'])} | "
            f"{kind} | {row['status']} |"
        )
    if not actions:
        lines.append("| （还没有发过东西） | | | | | |")

    lines += [
        "",
        "## @ 主号投递",
        "",
        "| BV | rpid | 状态 | 服务端确认的 mid |",
        "|---|---|---|---|",
    ]
    for row in notifications:
        lines.append(
            f"| `{row['bvid']}` | {row['remote_rpid']} | {row['status']} | "
            f"{row['observed_mid'] or '-'} |"
        )
    if not notifications:
        lines.append("| （还没有 @ 过） | | | |")

    lines += [
        "",
        "---",
        "",
        "> 状态含义：`sent_verified` = 已发出并核验；`uncertain` = **结果未知**，",
        "> 冻结该目标且**不自动重发**（需人工到官方页面核对）；`failed` = 明确失败。",
        "",
    ]

    text = "\n".join(lines)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        typer.secho(f"已导出 {out}", fg=typer.colors.GREEN)
        typer.echo(
            f"看过的视频 {len(videos)} 条 / 动作 {len(actions)} 条 / "
            f"@ {len(notifications)} 条"
        )
    else:
        typer.echo(text)
    db.close()


@app.command("dm-read")
def dm_read(size: int = typer.Option(20, "--size", help="拉取条数")) -> None:
    """读取小号与主人之间的私信（**只读**，不发送任何内容）。"""
    _configure_stdio()
    import asyncio

    from core.credentials import CredentialError, load_cookie
    from core.dm import DmMessage, fetch_messages, fetch_unread
    from core.ratelimit import RequestQueue

    config = _load_config()
    db = _open_db()
    if config.account.owner_mid <= 0:
        typer.secho("owner_mid 未配置，无法定位会话。", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=2)
    try:
        bundle = load_cookie()
    except CredentialError as exc:
        typer.secho(f"没有可用凭据：{exc}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=3) from exc

    queue = RequestQueue(db=db, config=config)

    async def _collect() -> list[DmMessage]:
        try:
            unread = await fetch_unread(queue=queue, cookies=bundle.values)
            typer.echo(f"未读：{json.dumps(unread, ensure_ascii=False)}")
            return await fetch_messages(
                queue=queue,
                cookies=bundle.values,
                talker_id=config.account.owner_mid,
                size=size,
            )
        finally:
            await queue.aclose()

    messages = asyncio.run(_collect())
    db.close()

    from datetime import datetime

    if not messages:
        typer.echo("（没有读到消息）")
        return
    for item in messages:
        who = (
            "主人"
            if item.from_owner(config.account.owner_mid)
            else "小号"
            if item.from_agent(config.account.agent_mid)
            else f"uid={item.sender_uid}"
        )
        when = (
            datetime.fromtimestamp(item.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            if item.timestamp
            else "-"
        )
        extra = f"  <{item.bvid} {item.title}>" if item.bvid else ""
        typer.echo(f"[{when}] {who}：{item.text}{extra}")


@app.command("dm-send")
def dm_send(
    text: str = typer.Option(..., "--text", help="私信正文（**只能发给主人**）"),  # noqa: B008
    share: str = typer.Option(
        "", "--share", help="附带的视频 BV 号 → 发成**分享卡片**（不是链接）"
    ),
    confirm: bool = typer.Option(False, "--yes", help="确认真实发送"),
) -> None:
    """**人**给主人发一条私信。接收者硬编码为 ``owner_mid``，无法发给别人。

    ★ 给了 ``--share BV号`` 就发 **B站分享卡片**（``msg_type=7``），
    而不是贴一个链接 —— 主人明确要求过这个形式。
    卡片所需的标题/UP/封面会**自动去取**（只读一次视频详情）。
    """
    _configure_stdio()
    import asyncio

    from core.credentials import CredentialError, load_cookie
    from core.dm import DmError, dm_reply_allowed, send_to_owner

    config = _load_config()
    allowed, reason = dm_reply_allowed(config)
    if not allowed:
        typer.secho(f"门禁拒绝：{reason}", fg=typer.colors.RED, err=True)
        typer.echo(
            "提示：私信回复由 interaction.dm_reply_enabled 单独控制，"
            "与 publish_enabled 无关。"
        )
        raise typer.Exit(code=1)
    if not confirm:
        typer.secho("需要 --yes 确认真实发送。", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    try:
        bundle = load_cookie()
    except CredentialError as exc:
        typer.secho(f"没有可用凭据：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3) from exc

    db = _open_db()

    # ★ 要发分享卡片时，先取视频详情（标题/UP/封面）——**只读一次**
    share_kwargs: dict[str, Any] = {}
    bvid = share.strip()
    if bvid:
        from core.bilibili import BilibiliReader, BilibiliReadError
        from core.ratelimit import RequestQueue

        queue = RequestQueue(db=db, config=config)

        async def _fetch_detail() -> Any:
            reader = BilibiliReader(queue=queue, cookies=bundle.values)
            try:
                return await reader.get_video_detail(bvid)
            finally:
                await queue.aclose()

        try:
            detail = asyncio.run(_fetch_detail())
        except (BilibiliReadError, Exception) as exc:  # noqa: BLE001
            db.close()
            typer.secho(f"取视频详情失败：{exc}", fg=typer.colors.RED, err=True)
            typer.echo("提示：分享卡片需要标题/UP/封面；取不到就没法发卡片。")
            raise typer.Exit(code=1) from exc
        share_kwargs = {
            "share_bvid": detail.bvid,
            "share_title": detail.title,
            "share_author": detail.up_name,
            "share_cover": detail.cover_url if hasattr(detail, "cover_url") else "",
            "share_aid": detail.aid,
        }
        typer.echo(f"将发分享卡片：{detail.title[:40]}（UP：{detail.up_name}）")

    try:
        result = asyncio.run(
            send_to_owner(
                config=config, cookies=bundle, text=text, db=db, **share_kwargs
            )
        )
    except DmError as exc:
        db.close()
        typer.secho(f"发送失败：{exc.code} {exc}", fg=typer.colors.RED, err=True)
        typer.echo("★ 写请求不重试。结果不确定时请到官方页面人工核对，**不要**重复发送。")
        raise typer.Exit(code=1) from exc
    db.close()
    typer.secho(f"已发送给主人（msg_key={result.msg_key}）", fg=typer.colors.GREEN)
    if getattr(result, "card_dropped", False):
        typer.secho(
            "⚠️ 卡片发不出去，已自动降级为**排版好的文本**。\n"
            "   原因：B站 **web 端** send_msg 只接受 msg_type=1，"
            "卡片（6/7）一律 -400 —— 这是平台限制，不是配置问题。\n"
            "   详见 docs/dm-share-card.md",
            fg=typer.colors.YELLOW,
        )


@app.command("dm-ingest")
def dm_ingest(
    size: int = typer.Option(30, "--size", help="扫描的私信条数"),
    limit: int = typer.Option(3, "--limit", help="本批最多处理几个视频（硬上限 3）"),
    force: bool = typer.Option(False, "--force", help="连已读过的也重新过一遍管道"),
) -> None:
    """把主人私信里的**分享视频**送进双模型短包管道（豆包读完 → DSH 只拿短包）。

    与 ``prepare_batch`` 走**同一个** ``Pipeline.read_video``：
    字幕/简介原文留在 bridge 里，DSH 只拿到短候选包 + ``evidence_id``。
    默认跳过已读过的视频（判重键 = BV 号）。
    """
    _configure_stdio()
    import asyncio

    from core.credentials import CredentialError, load_cookie
    from core.dm import extract_share_bvids, fetch_messages
    from core.runtime import build_runtime

    config = _load_config()
    owner_mid = config.account.owner_mid
    if owner_mid <= 0:
        typer.secho("owner_mid 未配置，无法定位会话。", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    async def _run() -> dict[str, Any]:
        runtime = await build_runtime()
        try:
            try:
                cookies = load_cookie().values
            except CredentialError as exc:
                typer.secho(f"没有可用凭据：{exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=3) from exc
            messages = await fetch_messages(
                queue=runtime.queue,
                cookies=cookies,
                talker_id=owner_mid,
                size=size,
            )
            share = extract_share_bvids(messages)
            already: list[str] = []
            fresh: list[str] = []
            for bvid in share:
                row = runtime.db.get_video(bvid)
                is_read = row is not None and str(row["status"]) == "read"
                if is_read and not force:
                    already.append(bvid)
                else:
                    fresh.append(bvid)
            cap = max(1, min(limit, 3))
            cards: list[dict[str, Any]] = []
            for bvid in fresh[:cap]:
                card, _blocks = await runtime.pipeline.read_video(bvid)
                cards.append(card.to_payload())
            return {
                "messages_scanned": len(messages),
                "share_bvids": share,
                "already_read": already,
                "processing": [str(c["bvid"]) for c in cards],
                "candidates": cards,
            }
        finally:
            await runtime.aclose()

    payload = asyncio.run(_run())
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command()
def digest() -> None:
    """生成一份日报（人用，等价于定时任务收尾动作）。"""
    _configure_stdio()
    config = _load_config()
    db = _open_db()
    from core.report import DigestInput, DigestWriter

    run_id = db.start_run("manual-digest")
    db.finish_run(run_id, conclusion="manual")
    writer = DigestWriter(db=db, out_dir=paths.data_dir() / "digests")
    target = writer.write(
        DigestInput(
            run_id=run_id,
            trigger_source="manual-digest",
            keywords=config.discovery.keywords,
            health_state=db.health_state(),
            simulated=True,
            next_steps=["由人确认后再切换到 review/auto 并关闭 dry_run"],
        )
    )
    db.close()
    typer.secho(f"日报已写入 {target}", fg=typer.colors.GREEN)


@app.command("schedule")
def schedule_status(
    simulate_after: str = typer.Option(
        "", "--after", help='假设"现在"是该时刻（HH:MM），用于验证补跑逻辑'
    ),
) -> None:
    """查看调度状态：下一次该跑的时刻、现在是否到期、是否补跑（人用，只读）。

    只读学习不依赖发布门禁；这里只回答"什么时候该唤醒"。
    **任务做成没有**以 `runs`/`actions` 表为准，不看调度器的投递记录。
    """
    _configure_stdio()
    from datetime import datetime

    from core.schedule import ScheduleError, evaluate_due, next_run_at

    config = _load_config()
    db = _open_db()

    last_ts = db.last_finished_run_at()
    last_dt = datetime.fromtimestamp(last_ts).astimezone() if last_ts is not None else None
    attempt_ts = db.last_started_run_at()
    attempt_dt = (
        datetime.fromtimestamp(attempt_ts).astimezone() if attempt_ts is not None else None
    )

    now = None
    if simulate_after:
        try:
            hh, mm = (int(x) for x in simulate_after.split(":"))
            today = datetime.now().astimezone()
            now = today.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"--after 需为 HH:MM：{exc}", fg=typer.colors.RED, err=True)
            db.close()
            raise typer.Exit(code=2) from exc

    try:
        nxt = next_run_at(
            daily_time=config.schedule.daily_time,
            tz_name=config.schedule.timezone,
            now=now,
            last_completed_at=last_dt,
            last_attempt_at=attempt_dt,
        )
        check = evaluate_due(
            mode=config.schedule.mode,
            daily_time=config.schedule.daily_time,
            tz_name=config.schedule.timezone,
            now=now,
            last_completed_at=last_dt,
            last_attempt_at=attempt_dt,
            min_gap_minutes=config.schedule.min_gap_minutes,
            peak_valley=config.schedule.peak_valley,
            # ★ 持续跑模式要用：今天已经跑了几轮（跑满就自动停）
            interval_minutes=config.schedule.interval_minutes,
            max_runs_per_day=config.schedule.max_runs_per_day,
            runs_today=db.runs_today(),
        )
    except ScheduleError as exc:
        typer.secho(f"调度配置错误：{exc}", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=2) from exc

    _mode_note = {
        "on_host_start": "（打开 DSH 时唤醒）",
        "continuous": "（持续跑：每隔 N 分钟一轮，跑满自动停）",
    }.get(config.schedule.mode, "（每日固定时刻）")
    typer.echo(f"唤醒模式 = {config.schedule.mode}{_mode_note}")
    typer.echo(
        f"daily_time = {config.schedule.daily_time}  "
        f"timezone = {config.schedule.timezone}  "
        f"min_gap = {config.schedule.min_gap_minutes} 分钟"
    )
    if config.schedule.mode == "continuous":
        typer.echo(
            f"持续跑   = 每 {config.schedule.interval_minutes} 分钟一轮，"
            f"今天已跑 {db.runs_today()}/{config.schedule.max_runs_per_day} 轮"
            + (
                "（**已跑满 → 自动停**）"
                if db.runs_today() >= config.schedule.max_runs_per_day
                else ""
            )
        )
    typer.echo(f"上次运行 = {last_dt.isoformat(timespec='seconds') if last_dt else '（从未运行）'}")
    if config.schedule.mode == "on_host_start":
        typer.echo("下一次   = 打开 DSH 时（本模式不按固定时刻排程）")
    elif config.schedule.mode == "continuous":
        # ★ 持续跑不走"固定时刻排程"——下一次就是"间隔到了就跑"
        typer.echo("下一次   = 间隔到了即跑（本模式不按固定时刻排程）")
    else:
        typer.echo(f"下一次   = {nxt.when.isoformat(timespec='seconds')}  （{nxt.reason}）")
    typer.echo(
        "上次尝试 = "
        + (attempt_dt.isoformat(timespec="seconds") if attempt_dt else "（无）")
        + "（含未完成的轮次；用于最小间隔防双跑）"
    )
    typer.secho(
        f"现在是否到期 = {'是' if check.due else '否'}"
        + ("（补跑）" if check.catch_up else ""),
        fg=typer.colors.GREEN if check.due else typer.colors.YELLOW,
    )
    typer.echo("")
    typer.echo(
        "提醒：官方 Schedule 语义为「宿主运行期间才调度 / 投递记录不代表完成 / "
        "不保证恰好一次」。"
    )
    typer.echo("因此重复唤醒不会重复发布：幂等键与 uncertain 冻结独立保证这一点。")
    db.close()


@app.command("audit-persona")
def audit_persona(
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """审计人设/红线与现实约束是否冲突（人用，只读）。

    对照两处上级权威：curator 的 `docs/13-agent-persona.md` 与
    `docs/DSH_EXECUTION_BRIEF.md`，逐条判定一致 / 更严 / 更松 / 缺失 / 对立。
    """
    _configure_stdio()
    import json as _json

    from core.audit import audit, render

    config = _load_config()
    report = audit(config)
    if json_out:
        typer.echo(_json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(render(report))
    if report.needs_human:
        typer.echo("")
        typer.secho(
            f"⚠️ 有 {len(report.needs_human)} 项需要主人拍板。",
            fg=typer.colors.YELLOW,
        )


@app.command("prefs")
def prefs(
    add: str = typer.Option("", "--add", help="新增偏好内容（配合 --kind）"),
    kind: str = typer.Option("rule", "--kind", help="rule|like|dislike|note"),
    subject: str = typer.Option("", "--subject", help="主题（UP名/关键词/BV…）"),
    deactivate: str = typer.Option("", "--deactivate", help="停用某条偏好（pref_id）"),
) -> None:
    """主人偏好（跨对话持久）。**不影响发布授权**，只影响排序与内容策略。"""
    _configure_stdio()
    db = _open_db()

    if deactivate:
        ok = db.deactivate_owner_preference(deactivate)
        if ok:
            typer.secho(f"已停用 {deactivate}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"没有这条偏好：{deactivate}", fg=typer.colors.RED, err=True)
            db.close()
            raise typer.Exit(code=1)
        db.close()
        return

    if add:
        try:
            pid = db.add_owner_preference(kind=kind, content=add, subject=subject)
        except ValueError as exc:
            typer.secho(f"参数错误：{exc}", fg=typer.colors.RED, err=True)
            db.close()
            raise typer.Exit(code=2) from exc
        typer.secho(f"已记录 {pid}", fg=typer.colors.GREEN)
        typer.echo(f"  [{kind}] {subject}：{add}")
        db.close()
        return

    digest = db.owner_preferences_digest()
    rows = db.list_owner_preferences()
    if not rows:
        typer.echo("（还没有偏好记录）")
        typer.echo("")
        typer.echo("加一条：")
        typer.echo('  ./.venv/Scripts/python.exe -m cli.app prefs --add "少点赞" --kind rule')
        db.close()
        return
    for label, key in (("规则", "rule"), ("喜欢", "like"), ("不喜欢", "dislike"), ("备注", "note")):
        items = digest.get(key) or []
        if not items:
            continue
        typer.secho(f"【{label}】", fg=typer.colors.CYAN)
        for item in items:
            typer.echo(f"  · {item}")
    typer.echo("")
    typer.echo("⚠️ 偏好只影响排序与内容策略；发布模式/配额/UID 只能由 config.yaml 决定。")
    db.close()


@app.command("mention-status")
def mention_status() -> None:
    """查看主号汇报（@ 提及）的投递状态。

    **人用**。这里能看到"到底通知成功没有"——
    ``verified`` 才代表已核验渲染后的目标确为 owner_mid。
    """
    _configure_stdio()
    config = _load_config()
    db = _open_db()

    summary = db.owner_notification_summary()
    rows = db.list_owner_notifications(limit=50)
    typer.echo(f"owner_mid = {config.account.owner_mid}")
    if config.account.owner_mid <= 0:
        typer.secho(
            "⚠️ 未配置 owner_mid → 无法校验 @ 目标身份，只保留本地日报。",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"投递状态统计：{summary or '（无记录）'}")
    if rows:
        typer.echo("")
        for row in rows:
            observed = row["observed_mid"]
            extra = f" observed_mid={observed}" if observed is not None else ""
            typer.echo(
                f"{row['bvid']}  [{row['status']}]  channel={row['channel']}"
                f"  rpid={row['remote_rpid'] or '-'}{extra}"
            )
            if row["status"] != "verified":
                typer.echo(f"    ⚠️ {row['detail']}")
    typer.echo("")
    typer.echo(
        "说明：@数字UID / @昵称 文本**不算**成功；只有核验到渲染后的 UID 才记 verified。"
    )
    db.close()


@app.command("mention-check")
def mention_check(bvid: str = typer.Argument(..., help="要判定是否值得 @ 主号的 BV")) -> None:
    """按"双峰"规则判定某视频是否值得向主号汇报（人用，只读）。"""
    _configure_stdio()
    from core.mention import HIGH_VALUE_LIKES, NICHE_LIKES, classify_recommendation

    db = _open_db()
    row = db.query_one("SELECT * FROM notes WHERE bvid = ?", (bvid,))
    if row is None:
        typer.secho(f"库里没有 {bvid} 的笔记 → 先跑一轮只读学习。", fg=typer.colors.RED, err=True)
        db.close()
        raise typer.Exit(code=1)

    score = int(row["score_total"])
    video = db.get_video(bvid)
    likes = 0
    if video is not None:
        # videos 表当前没有 like_count 列；用 try 兜底以便将来加列后自动生效
        try:
            likes = int(video["like_count"] or 0)
        except (IndexError, KeyError):
            likes = 0

    plan = classify_recommendation(like_count=likes, score_total=score)
    typer.echo(f"{bvid}  评分={score}  点赞={likes}")
    typer.echo(f"阈值：大众爆款 点赞 ≥{HIGH_VALUE_LIKES}；小众宝藏 点赞 ≤{NICHE_LIKES} 且评分达标")
    typer.secho(
        f"是否值得向主号汇报：{'是' if plan.should_mention else '否'}（{plan.tier}）",
        fg=typer.colors.GREEN if plan.should_mention else typer.colors.YELLOW,
    )
    typer.echo(f"理由：{plan.reason}")
    db.close()


@app.command()
def tools() -> None:
    """列出**模型可见**的 MCP 工具（对照用：确认人用入口不在其中）。"""
    _configure_stdio()
    import asyncio

    from bridge import server as bridge_server

    async def _list() -> list[str]:
        return [tool.name for tool in await bridge_server.server.list_tools()]

    names = asyncio.run(_list())
    typer.echo(f"MCP 工具（{len(names)} 个）：")
    for name in names:
        typer.echo(f"  - {name}")
    forbidden = {"approve", "resume", "reject", "import-cookie", "recover-health"}
    leaked = forbidden & set(names)
    if leaked:
        typer.secho(f"❌ 人用入口泄漏进 MCP：{sorted(leaked)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("✅ 人用入口未出现在 MCP 工具列表里", fg=typer.colors.GREEN)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

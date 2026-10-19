# 抖音 / 小红书：只读学习接入与验收

本轮已把占位框架接成实际运行链：外部 MCP → 平台规范化 → SQLite 证据 → 豆包 → 最多 3 个短候选包 → DSH 值班会话判断 → 现有 B 站主号汇报。

**默认仍只启用 B 站。** “代码与隔离测试可用”不代表已用你的真实账号完成联调。本仓库不会自动安装后端、扫描浏览器 Cookie、修改账号授权、下载 ASR 模型，或调用外部平台的写入工具。

## 1. 后端选择与边界

| 平台 | 本轮后端 | 读取能力 | 本项目不开放 |
|---|---|---|---|
| 小红书 | [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) | 搜索、详情、正文、图片列表、评论；可用字幕 URL 经受限 SRT/VTT 读取；可选本地图片 OCR | 发评、回复、点赞收藏、私信、通知读取 |
| 抖音 | [juziguai/douyin-mcp-server](https://github.com/juziguai/douyin-mcp-server) | 搜索、作品正文、评论、后端提供的字幕；显式开启时允许后端本地 ASR | 发评、回复、点赞收藏、关注、私信、@ |

Go 与 Python 不冲突：小红书提供标准 HTTP MCP，Python 直接使用 SDK 调用，不移植 Go 代码。抖音走其 FastMCP stdio 服务；搜索实现依赖上游签名接口，不应宣传为纯浏览器读取或稳定免登录。后端稳定性与账号权限仍需实机验收。

小红书通知读取会改变未读状态，因此不属于本轮无副作用读取白名单。后端具备某种写能力，不等于本项目已开放该能力。“收到 @ 通知”也不证明能主动有效 @ 主号。

### 许可与替代路线

- 小红书候选采用 Apache-2.0，具体以对应版本 LICENSE 为准。
- 抖音候选 pyproject 声称 MIT，但本次未找到根目录许可证正文。这里只通过协议调用独立部署的服务，不复制上游实现；分发前仍应补核许可证。
- [MediaCrawler LICENSE](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE) 为非商业学习许可，不是 MIT；可作研究型采集备选，不直接当互动机器人。
- [yzfly/douyin-mcp-server](https://github.com/yzfly/douyin-mcp-server) 已归档，偏分享链接解析与云 ASR，不作为主依赖。

官方能力也要按权限区分：抖音有需授权的[视频搜索能力](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/douyin-search-capability/)，[普通用户评论回复](https://open.douyin.com/platform/resource/docs/openapi/interaction-management/comment-management-user/video-comment-reply)限定授权用户自己的视频。小红书有[账号开放平台](https://openaccount.xiaohongshu.com/docs/quick-start)与[分享平台](https://agora.xiaohongshu.com/)，不是只有电商；本次未找到面向普通小号的通用社区搜索、任意发评和私信官方 API 证据。浏览器自动化本身不等同验证码绕过，也不自动构成平台许可。

## 2. Windows 配置步骤

### 第一步：分别部署后端

所有后端、环境和模型建议放在 F 盘独立目录，不装进 companion 的 Python 环境，防止它们的 core 包与本项目冲突。

1. 小红书：按上游 README 的 Windows 安装/登录步骤部署，人工登录后启动服务，确认 HTTP MCP 地址，例如 http://127.0.0.1:18060/mcp。
2. 抖音：按上游 README 独立安装。其 stdio 入口是仓库根目录 server.py，不是 douyin-cli。记录后端虚拟环境 Python 和 server.py 的绝对路径。
3. 如需授权账号读取，按上游要求由本人配置。验证码、登录失效均由本人处理；不轮换账号/IP、不代解验证码，不复制签名算法到本项目。

### 第二步：编辑本项目 config.yaml

保留你已有的账号、豆包和发布配置，只合并以下字段；不要把整个配置替换掉：

```yaml
discovery:
  # 初次只测一个新平台；确认后可同时启用三个。
  platforms: [bilibili, xiaohongshu, douyin]

external_platforms:
  xiaohongshu:
    enabled: true
    transport: http
    url: http://127.0.0.1:18060/mcp
    account_id: xhs-learning-account
    timeout_sec: 30
    min_interval_seconds: 5
    cache_ttl_seconds: 3600
  douyin:
    enabled: true
    transport: stdio
    command: F:/agents/douyin-mcp-server/.venv/Scripts/python.exe
    args: [F:/agents/douyin-mcp-server/server.py]
    account_id: douyin-learning-account
    timeout_sec: 60
    min_interval_seconds: 5
    cache_ttl_seconds: 3600
    asr_enabled: false
    env_names: []
    headers_env: {}

ocr:
  enabled: false
  command: F:/tools/Tesseract-OCR/tesseract.exe
  languages: chi_sim+eng
  max_images: 6
  timeout_seconds: 20
  max_image_bytes: 10485760
```

路径为部署示例，替换为实际存在的位置。只有 enabled=true 且实际调用读取时才连接/启动后端。stdio 每次工具调用启动并关闭一次已配置的后端，速度取决于其冷启动。

account_id 是稳定、非秘密的账号标识，不是 token。后端切换登录账号时必须同步改 account_id；同一平台多账号不能共用同一标识，否则缓存身份会不准确。历史其他账号内容不会被本账号批处理读取。

密钥只能由环境变量引用：headers_env 的值填写变量名（例如 Authorization: XHS_MCP_AUTH_HEADER），完整头值放该环境变量。stdio 所需变量逐个列入 env_names；不要在 args、URL 或 YAML 写 Cookie/Token。不要向外部环境传递本项目的 ARK_API_KEY，后端不需要豆包权限。

可选 OCR：人工安装本地 Tesseract 与 chi_sim、eng 语言包后才开启。它通过 stdin 传图，不落临时图片、不调用视觉模型。只允许平台 HTTPS CDN、限制图片数量与字节数；失败/缺语言包/缺程序均留限制说明。图片 URL 或 OCR 结果不等于已经人工看过画面，visual_reviewed 始终为 false。

抖音 ASR：只有手动配置 asr_enabled=true 才允许后端转写，依赖及模型由后端管理员安装。耗时转写仍受本批时间预算与 MCP 超时约束；先用少量内容验收，不能把超时当成没有语音。小红书无可用字幕时不会自动下载视频转录，只使用真实可读的正文/OCR并报告限制。

### 第三步：小范围检查

Git Bash，先进入本项目：

```bash
export PYTHONIOENCODING=utf-8
export COMPANION_DATA_DIR=F:/agents/companion-data

./.venv/Scripts/python.exe -m cli.app platforms
./.venv/Scripts/python.exe -m cli.app discover-content --platform xiaohongshu --keyword "DeepSeek Harness" --limit 2
./.venv/Scripts/python.exe -m cli.app read-content "搜索结果返回的完整bvid键"

./.venv/Scripts/python.exe -m cli.app discover-content --platform douyin --keyword "DeepSeek Harness" --limit 2
```

搜索返回的 bvid 对外部内容是**不透明内部键**，并不是真 BV；原生 ID、平台与来源链接单独保存。小红书详情访问的 xsec_token 跟搜索结果持久保存，重启后继续可读。访问参数过期时重新搜索，不伪造新参数。

MCP 保留原工具名，增加可选参数：

- discover_videos(platforms=["xiaohongshu"], keywords=["DSH"], limit=2)
- read_video(bvid="发现返回的不透明键")
- read_video(bvid="原生ID", platform="xiaohongshu", context={"xsec_token":"本人取得的访问参数"})
- prepare_batch(platforms=["xiaohongshu","douyin"])
- get_discussion(bvid="发现返回的不透明键", limit=8)
- read_evidence_excerpt(evidence_id="ev_...")：仍最多 500 字，OCR 会带 page_index。
- save_analysis / feedback 可处理外部键，但外部 draft_text 被拒绝。

批次仍最多 3 个短包，原文不进 MCP 返回。自动轮按 discovery.platforms 运行，主人汇报继续使用现有 B 站私信；外部作品以真实链接推荐，不传 share_bvid、不虚构外部点赞收藏/评论结果。

## 3. 验证码、登录与限流

读取健康按 platform + account_id 持久保存，和 B 站健康门禁分开。验证码、登录失效、明确限流会停止该账号新读取；不是静默空结果，也不会计作“今天无值得推荐内容”。

人工完成处理后：

```bash
./.venv/Scripts/python.exe -m cli.app platform-resume xiaohongshu --account-id xhs-learning-account
./.venv/Scripts/python.exe -m cli.app platform-resume douyin --account-id douyin-learning-account
```

恢复不是解验证码，也不修改后端账号授权。MCP 和豆包没有恢复入口。

## 4. 自动轮恢复与人工处置

- 候选卡与 read 状态原子保存；准备中断可恢复已完成卡，不重新花豆包分析它们。
- 整理好的 prompt 持久保存，确定未接收的投递最多尝试 3 次，每次至少间隔 5 分钟。
- 超时、接收不确定、sending 中崩溃不重发，只核对本地 DSH 会话。
- 只有精确匹配本轮消息与对应 turn/end 才记完成，无关主人私信不会误触发。
- dsh_error、重试耗尽会阻止新轮；先人工核对，不靠自动放行掩盖失败。
- 调度每次 tick 重读有效配置，峰谷、模式、轮次上限等保存后生效。

```bash
./.venv/Scripts/python.exe -m cli.app cycle-deliveries
# 本人确认放弃该批后，显式归档；不会重发，也不会标为成功：
./.venv/Scripts/python.exe -m cli.app cycle-deliveries --abandon run_xxx --reason "本人已核对，不再投递这批"
```

归档保留原材料、prompt和历史，不删除数据。不会把“归档”计为 DSH 成功完成。

## 5. 数据与验收

migration17 是 cycle_outbox；migration18 增加内容映射、账号健康/缓存、OCR证据页码。旧 BV 键不改，evidence ID、笔记及外键关系保留。部署前先暂停监听器并备份数据库（WAL 模式请使用 SQLite backup，而不是仅复制正在写入的 db 文件）。

离线验收包含：失败恢复、消息关联、动态调度、两平台真实返回形状、MCP客户端关闭与取消、原生同ID/不同账号隔离、token上下文重启、证据与页码、短包预算、旧库迁移、风控隔离、OCR限制、字幕读取和外部写入拒绝。真实平台登录、实际内容可用率、DSH推荐投递和真实 token 消耗需在用户环境单独验收；未跑这些不声称已生产验证。

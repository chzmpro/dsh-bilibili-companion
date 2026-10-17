# 私信「视频分享卡片」：为什么发不了，我们怎么替代（2026-10-17）

## 主人要求

> 「你在B站给我发私信时，有视频参考的要发视频，
>   而且视频是要分享的那种卡片形式而不是链接形式」

## 结论（先说结果）

**B站 web 端私信接口不支持发分享卡片。** 这不是我们参数写错，是**平台能力限制**。

证据链（三层，逐层加强）：

1. **参数层**：7 种参数组合（`msg_type` 6/7 × `source` 0/1/2/5 × `id` 类型 × 带 content）全 `-400`；
2. **签名层**：抓包拿到正确的 WBI 签名方式后，`msg_type=1` 过、`msg_type=7` 不过 —— **唯一差别是 msg_type**；
3. **UI 层**：PC 网页版视频分享面板**没有「私信」选项**。

⇒ 卡片是 **App 专属能力**（appkey + access_key 签名，另一套凭据）。

我们的替代方案：**先按卡片尝试一次，遇到 `-400` 自动降级为"排版好的文本"**
（标题 + UP + 可点链接），并明确告知主人为什么不是卡片。

> ★ 附带确认：纯文本里的 `BV` 号会被 B站客户端**自动渲染成可点链接**
> （截图里能看到链接图标）—— 这是文本形式能做到的最好效果。

## 证据（实测，同一时刻、同一账号）

### 1. 抓到了真实卡片的完整结构

从主人发来的私信里取到 `msg_type=7` 的真实样本：

```json
{
  "author": "凌羽clover",
  "headline": "",
  "id": 117441451525192,
  "source": 5,
  "thumb": "http://i2.hdslb.com/bfs/archive/cd814229...jpg",
  "title": "【DSH】让deepseek harness可以刷b站的插件...",
  "bvid": "BV1d8pU6xEZX"
}
```

外层还有 `msg_source = 2`、`notify_code`、`at_uids` 等字段。

### 2. 按这个结构发 → 全部 `-400`

| 尝试 | 结果 |
|---|---|
| `msg_type=7`，照抄上面的 content | ❌ `-400 请求错误` |
| `msg_type=7` + `msg[msg_source]=2` | ❌ `-400` |
| `source` 改成 5 / 1 / 2 / 0 | ❌ 全 `-400` |
| `id` 用字符串而非 int | ❌ `-400` |
| `msg_type=6` 卡片体 | ❌ `-400` |
| `msg_type=7` 带 `content` 字段 | ❌ `-400` |
| **`msg_type=1`（纯文本）** | ✅ **`code=0 OK`** |

**结论**：同一个端点、同一份凭据、同一时刻，
只有 `msg_type=1` 能过。⇒ **web 接口是文本专用的**。

### 3. ★★ 决定性证据：抓包 + 正确签名后仍被拒（2026-10-17 追加）

主人提示：*"是采用视频底下的分享按钮进行分享的"*。

于是我去**浏览器里真机复现**：

#### 3.1 抓到了网页版真实发送请求

在 `message.bilibili.com` 里发一条普通文本，拦截到**完整请求**：

```
POST //api.vc.bilibili.com/web_im/v1/web_im/send_msg
  ?w_sender_uid=3747570769725629
  &w_receiver_id=660490601
  &w_dev_id=6C57A6C2-AFFE-4A9B-9A2A-755A39918BC1
  &statistics={"appId":100}
  &x-bili-locale-json=...
  &w_rid=af6eba7ebb01a5e4f5f5081b2de731a9      ← ★ WBI 签名
  &wts=1792215929

body:
  msg[sender_uid]=...&msg[receiver_type]=1&msg[receiver_id]=...
  &msg[msg_type]=1&msg[msg_status]=0
  &msg[content]={"content":"..."}
  &msg[new_face_version]=0        ← 我们之前没传
  &msg[canal_token]=              ← 我们之前没传
  &msg[dev_id]=6C57A6C2-...       ← ★ 固定值，不是每次随机 UUID
  &msg[timestamp]=...&from_firework=0&build=0&mobi_app=web&csrf=...
```

**两个之前不知道的关键点**：

1. **WBI 签的是 URL 上那 4 个参数**（`w_sender_uid` / `w_receiver_id` / `w_dev_id` / `statistics`），
   **不是**整个 body —— 我第一次签 body 所以一直 `-400`；
2. `msg[dev_id]` 是**设备固定值**，不是每次随机生成。

#### 3.2 用正确签名再试卡片 → 仍然 `-400`

拿到正确签名方式后，用**同一份 query、同一份 body 结构**：

| msg_type | 结果 |
|---|---|
| **`1`（文本）** | ✅ **`code=0 OK`** |
| **`7`（卡片）** | ❌ **`-400 请求错误`** |

**唯一差别就是 `msg_type`。**

⇒ 这一次可以确定：**不是参数写错、不是签名缺失，而是 web 接口对卡片的能力限制。**

#### 3.3 PC 网页版分享面板**根本没有「私信」**

在视频页展开分享下拉，实测选项只有：

```
动态 · 微信 · QQ · QQ空间 · 微博 · 贴吧 · 嵌入代码
```

**没有「私信」。**

⇒ 主人那条卡片是在 **B站 App** 里分享的（App 的分享面板有"私信"，
走的是 **appkey + access_key 签名**的 App 接口，与 web 是两套凭据）。

#### 3.4 附带发现：纯文本里的 BV 会被自动识别成链接

从消息中心的截图确认：我发的文本里 `BV1DkJ869EEn` 前面**出现了链接图标**，
说明 B站客户端**会把 BV 号渲染成可点链接** —— 这是文本能做到的极限。

### 4. 参考项目也不发卡片

| 项目 | 发私信用什么 |
|---|---|
| `bilibili_learning_bot` | `msg_type=bili_session.EventType.TEXT`（纯文本） |
| `langup-ai` | 纯文本 |

它的 `do_share` 描述是「转发/分享B站视频（**生成分享链接**）」——
**也只是生成链接**，不是发卡片。

> 也就是说：两个读了源码的同类项目**都没有**实现卡片发送。
> 这侧面印证了平台侧不支持，而不是我们没找对参数。

## 我们的实现

`core/dm.py` 的 `send_to_owner(..., share_bvid=...)`：

1. 给了 `share_bvid` → **先按 `msg_type=7` 卡片发一次**
   （将来 B站若放开，这里直接生效）；
2. 只在**明确 `-400`** 时降级为文本 ——
   网络错误 / 其他错误码**不重试**（写请求铁律：结果未知不得重发）；
3. 降级文本带排版：

```
降级测试

🎬 我目前用得最多的一款DeepSeek Harness插件
👤 UP：虾客云
🔗 https://www.bilibili.com/video/BV1DkJ869EEn/
```

4. `DmSendResult.card_dropped = True` 让 CLI 如实告知"卡片没发成"。

## 用法

```bash
./.venv/Scripts/python.exe -m cli.app dm-send \
  --text "看这个" --share BV1DkJ869EEn --yes
```

## 如果你想真的收到卡片，只有两条路

| 路 | 说明 | 代价 |
|---|---|---|
| **A. 走 App 端接口** | App 端 `send_msg` 需要 `appkey` + `access_key` + 签名 | 要 App 扫码授权拿 token，另一套凭据体系；而且要确认 App 端是否真支持卡片 |
| **B. 手动转发** | 你在 B站 App 里点"分享→私信" | 零开发，但每次要你手动 |

**目前选择的是"排版文本"**，因为：
- 卡片这条路已用 7 种参数组合证伪；
- App 端接口是**另一条凭据链**，属重大改动，值得单独评估；
- 排版文本至少把**标题、UP、可点链接**都给全了。

> ⚠️ 待验证：纯文本里的 BV 号，B站客户端**会不会自动渲染成可点卡片**。
> 这只能由主人在手机/网页端肉眼看——接口侧读回来永远是纯文本。

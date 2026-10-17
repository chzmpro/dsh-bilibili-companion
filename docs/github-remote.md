# GitHub 远端与推送（2026-10-17）

## 结论

| 项 | 值 |
|---|---|
| 远端 | `https://github.com/chzmpro/dsh-bilibili-companion`（**私有**） |
| 分支 | `main` |
| 推送方式 | **普通 `git push`**（配好 git 代理即可） |
| 内容一致性 | ✅ 185 文件、0 差异 |

## ★ 曾经的误判：以为 `git push` 不可用

上一轮 `git push` 报 `Empty reply from server` / `connection reset`，
当时的结论是"本机 git 到 GitHub 不通，必须改用 Git Data API 推送"。

**那个结论是错的。** 真实根因是 **git 没配代理**。

### 真根因

**git 用的 libcurl 不读 Windows 注册表里的系统代理**，
所以「浏览器能上网 + 系统代理已开」≠「git 能上网」。

系统代理（注册表）：
```
HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings
  ProxyEnable = 1
  ProxyServer = 127.0.0.1:7897      ← Clash 系
```

实测对照（同一时刻）：

| 方式 | 结果 |
|---|---|
| `curl --noproxy '*' https://github.com` | ❌ **15 秒超时** |
| `curl -x http://127.0.0.1:7897 https://github.com` | ✅ **HTTP 200 / 2.7 秒** |
| `git push`（未配代理） | ❌ 连接重置 |
| `git push`（配了代理） | ✅ **正常** |

## 修复（一次性）

```bash
# 1. 先确认代理端口在听、且能到 github
netstat -ano | grep 127.0.0.1:7897
curl -s -o /dev/null -w "%{http_code}\n" -x http://127.0.0.1:7897 https://github.com

# 2. 配给 git
git config --global http.proxy  http://127.0.0.1:7897
git config --global https.proxy http://127.0.0.1:7897

# 3. 验证
git ls-remote origin          # 能列出 ref = 通了
git push origin main          # 正常推送
```

> ⚠️ 代理端口会变（Clash 常见 7890/7897，v2ray 常见 10808/10809）。
> **读注册表拿当前值，不要写死**：
> ```bash
> reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -v ProxyServer
> ```

## 以后的推送流程（就一条命令）

```bash
git push origin main
```

不再需要 `scripts/push_via_api.py`。

## `scripts/push_via_api.py` 的去留

**保留但不再优先使用**。它是"以为 git push 不可用"时的应急产物，
走 GitHub Git Data API 逐个重建 blobs/trees/commits。

只有在**代理彻底不可用**时才考虑它。它踩过的三个坑仍值得记：

1. 空仓库 `POST /git/blobs` → `409 Git Repository is empty`，需先用 Contents API 播初始提交；
2. `git credential fill` 在 Python 子进程里返回空（要 TTY）⇒ 得从 shell 传环境变量；
3. API 重建的 commit SHA ≠ 本地 SHA ⇒ **判同步要比 blob sha，不比 commit sha**。

## 提交规范（沿用旧项目 `dsh-bili-curator`）

**Conventional Commits + 中文描述**：

```
feat(scope): 中文说明
fix(scope): 中文说明
docs: 中文说明
test(scope): 中文说明
chore: 中文说明
refactor(scope): 中文说明
```

- `scope` 用**小写模块名**（`dm` / `settings` / `ego` / `auth` / `cli` …）；
- 描述用中文、**不加句号**；
- body 写清「用户原话 / 根因 / 实测数据 / 怎么改的」。

作者统一为 `chzmpro <2660578994@qq.com>`。

## 旧项目已下架

`chzmpro/dsh-bili-curator`（旧版，45 commits，私有）已**归档**：
- `archived: true`（只读、不可再推）
- description 改为 `[ARCHIVED / 已下架] superseded by dsh-bilibili-companion`

> ⚠️ 归档**可逆**（`archived:false` 即恢复）。
> **彻底删除**需单独确认——GitHub 删仓库不可恢复。
> 本地副本仍在 `D:/DeepSeek/projects/dsh-bili-curator`（45 commits 完整）。

# GitHub 远端与推送方式（2026-10-17）

## 结论

| 项 | 值 |
|---|---|
| 远端 | `https://github.com/chzmpro/dsh-bilibili-companion`（**私有**） |
| 分支 | `main` |
| 推送方式 | **Git Data API**（`git push` 在本机走不通） |
| 内容一致性 | ✅ 183 文件、0 差异 |

## 为什么不能 `git push`

本机 `github.com:443`（git push 走的主机）**完全不通**：

| 主机 | 实测 |
|---|---|
| `github.com:443` | ❌ HTTP 000 / connection reset |
| `ssh.github.com:443` | ✅ TCP 通，但 `Permission denied (publickey)`（没有 key） |
| `api.github.com` | ✅ **200** |

- 试过用 API 挂 SSH key（`POST /user/keys`）→ 返回 `Not Found`，
  说明存着的 token **没有 `admin:public_key` 权限**。
- ⚠️ `git ls-remote` 偶尔会**碰巧成功一次**，别据此以为 git 能用了。

## 推送命令

```bash
cd D:/DeepSeek/projects/dsh-bilibili-companion
export PYTHONIOENCODING=utf-8
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' \
  | git -c credential.interactive=false credential fill 2>/dev/null \
  | sed -n 's/^password=//p')
GH_PUSH_TOKEN="$TOKEN" ./.venv/Scripts/python.exe -u scripts/push_via_api.py
```

脚本按本地提交顺序逐个重建（`blobs` → `trees` → `commits` → `PATCH ref`），
**历史完整保留**。

## 三个坑（都实测踩过）

| # | 坑 | 解法 |
|---|---|---|
| 1 | 空仓库 `POST /git/blobs` → `409 Git Repository is empty` | 先用 Contents API（`PUT /contents/README.md`）播一个初始提交 |
| 2 | `git credential fill` 在 Python 子进程里返回空（要 TTY） | 在 shell 里取好 token，用环境变量传给 Python |
| 3 | API 重建的 commit SHA ≠ 本地 SHA | **内容逐字节一致**；判同步要比 **blob sha**，不比 commit sha |

## 验证同步（正确方式）

```bash
# 远端 blob 表 vs 本地 ls-tree -r HEAD，逐路径比对 sha
# 实测：183 文件、0 差异
```

## 旧项目已下架

`chzmpro/dsh-bili-curator`（旧版，45 commits，私有）已**归档**：
- `archived: true`（只读、不可再推）
- description 改成 `[ARCHIVED / 已下架] superseded by dsh-bilibili-companion`

> ⚠️ 归档是**可逆**的（`PATCH archived:false` 即恢复）。
> **彻底删除**需要单独确认——GitHub 删除仓库不可恢复。
> 本地副本仍在 `D:/DeepSeek/projects/dsh-bili-curator`（45 commits 完整）。

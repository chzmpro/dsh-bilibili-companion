#!/usr/bin/env bash
# 把 DSH 自带的 react / react-dom 从 app.asar 解出来，供渲染冒烟测试用。
#
# ## 为什么需要它
#
# 本机**没有独立的 react**（DSH 把 react 打在 `app.asar` 里），
# 而 `render-smoke.mjs` 要真渲染一次设置页 —— 那是唯一能发现
# "前端渲染时抛异常"的测试（AGENTS.md A4：client 抛异常会让**整个 shell 起不来**）。
#
# ## 用法
#
#   bash plugin/extract-react-for-smoke.sh
#   NODE_PATH=/d/DeepSeek/workspace-temp/react-probe/node_modules node plugin/render-smoke.mjs
#
# 幂等：已解出来的文件会跳过。

set -u

ASAR="${ASAR:-/d/DSH/DSH-file/DSH Desktop/resources/app.asar}"
OUT="${OUT:-/d/DeepSeek/workspace-temp/react-probe}"

if [ ! -f "$ASAR" ]; then
  echo "找不到 app.asar：$ASAR" >&2
  exit 1
fi

mkdir -p "$OUT/node_modules/react/cjs" "$OUT/node_modules/react-dom/cjs"
cd "$OUT" || exit 1

extract() {
  local asar_path="$1" dest="$2"
  local base
  base=$(basename "$asar_path")
  if [ -f "$dest" ]; then
    return 0
  fi
  timeout 200 npx --yes @electron/asar extract-file "$ASAR" "$asar_path" >/dev/null 2>&1
  if [ -f "$base" ]; then
    mv -f "$base" "$dest" 2>/dev/null
  fi
}

# ⚠️ asar 的 list 用反斜杠，extract-file 也必须用反斜杠
extract 'node_modules\react\package.json'                      node_modules/react/package.json
extract 'node_modules\react\index.js'                          node_modules/react/index.js
extract 'node_modules\react\jsx-runtime.js'                    node_modules/react/jsx-runtime.js
extract 'node_modules\react\cjs\react.production.min.js'       node_modules/react/cjs/react.production.min.js
extract 'node_modules\react\cjs\react.development.js'          node_modules/react/cjs/react.development.js
extract 'node_modules\react\cjs\react-jsx-runtime.production.min.js' node_modules/react/cjs/react-jsx-runtime.production.min.js
extract 'node_modules\react\cjs\react-jsx-runtime.development.js'    node_modules/react/cjs/react-jsx-runtime.development.js

extract 'node_modules\react-dom\package.json'                  node_modules/react-dom/package.json
extract 'node_modules\react-dom\index.js'                      node_modules/react-dom/index.js
extract 'node_modules\react-dom\server.js'                     node_modules/react-dom/server.js
extract 'node_modules\react-dom\server.node.js'                node_modules/react-dom/server.node.js
extract 'node_modules\react-dom\cjs\react-dom.production.min.js' node_modules/react-dom/cjs/react-dom.production.min.js
extract 'node_modules\react-dom\cjs\react-dom.development.js'    node_modules/react-dom/cjs/react-dom.development.js
extract 'node_modules\react-dom\cjs\react-dom-server.node.production.min.js' node_modules/react-dom/cjs/react-dom-server.node.production.min.js
extract 'node_modules\react-dom\cjs\react-dom-server.node.development.js'    node_modules/react-dom/cjs/react-dom-server.node.development.js
extract 'node_modules\react-dom\cjs\react-dom-server-legacy.node.production.min.js' node_modules/react-dom/cjs/react-dom-server-legacy.node.production.min.js
extract 'node_modules\react-dom\cjs\react-dom-server-legacy.node.development.js'    node_modules/react-dom/cjs/react-dom-server-legacy.node.development.js

echo "已解出 $(find node_modules -type f | wc -l) 个文件到 $OUT/node_modules"

# 自检：两个模块都要能 require
# ⚠️ 用相对路径（cd 已经在 $OUT），绝对路径里的 `D:/...` 在 node -e 里会解析错
if node -e "require('./node_modules/react')" 2>/dev/null; then
  echo "✅ react 可加载"
else
  echo "❌ react 加载失败" >&2
  exit 1
fi
if node -e "require('./node_modules/react-dom/server')" 2>/dev/null; then
  echo "✅ react-dom/server 可加载"
else
  echo "❌ react-dom/server 加载失败" >&2
  exit 1
fi

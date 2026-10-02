#!/usr/bin/env bash
#
# Codex CLI 接入 Super Relay（托管模式）
# 用法：填好下面两项后执行  bash codex-cli-setup.sh
#
set -euo pipefail

# ============ 请填入我们提供的三项 ============
RELAY_BASE_URL="https://seed-code.bytedance.com/"
RELAY_API_KEY="plat_nWzn1cBhMlE39ZHhJvR20P4wr2kd3cbOvYcw5jNqOhY"
RELAY_MODEL_ID="auto_model/urm"
# ============================================

# Base URL 请原样填入，不要自己加 /v1；脚本会按 Codex 的要求补上
RELAY_HOST="${RELAY_BASE_URL%/}"
PROVIDER="super_relay"
CONFIG="$HOME/.codex/config.toml"

case "$RELAY_BASE_URL$RELAY_API_KEY$RELAY_MODEL_ID" in
  *"<"*) echo "请先在脚本顶部填入 Base URL、API Key 和 Model ID。" >&2; exit 1 ;;
esac

# 最常见的填错是把 API Key 和 Model ID 写反，提前拦住，避免把 Key 打印到终端
case "$RELAY_MODEL_ID" in
  plat_*|sk-*|Bearer\ *)
    echo "RELAY_MODEL_ID 的值看起来是一个 API Key，请检查是否和 RELAY_API_KEY 填反了。" >&2
    exit 1 ;;
esac
case "$RELAY_BASE_URL" in
  http*) ;;
  *) echo "RELAY_BASE_URL 必须是完整的 https:// 地址。" >&2; exit 1 ;;
esac
if [ "$RELAY_API_KEY" = "$RELAY_MODEL_ID" ]; then
  echo "RELAY_API_KEY 和 RELAY_MODEL_ID 填成了同一个值，请核对我们发给你的三项。" >&2
  exit 1
fi

echo "[1/6] 检查网络可达性"
if ! curl -sS -o /dev/null --max-time 15 "$RELAY_HOST"; then
  echo "无法访问 ${RELAY_HOST}，请联系我们确认网络通道后再继续。" >&2
  exit 1
fi

echo "[2/6] 检查依赖"
for BIN in codex jq; do
  command -v "$BIN" >/dev/null 2>&1 && continue
  case "$BIN" in
    codex) echo "未找到 codex，请先安装：npm install -g @openai/codex" >&2 ;;
    jq)    echo "未找到 jq，请先安装：brew install jq  或  apt-get install -y jq" >&2 ;;
  esac
  exit 1
done

echo "[3/6] 备份现有配置"
mkdir -p "$HOME/.codex"
touch "$CONFIG"
cp -p "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d-%H%M%S)"
# Codex 启动需要 auth.json 存在；已有的一律不动，避免破坏已登录状态
[ -f "$HOME/.codex/auth.json" ] || printf '%s\n' '{"OPENAI_API_KEY":"sk-placeholder"}' > "$HOME/.codex/auth.json"

echo "[4/6] 追加 provider 定义"
# 只追加，不覆盖：已有的 model_provider、projects、mcp_servers、plugins 全部保留
if grep -q "^\[model_providers\.${PROVIDER}\]$" "$CONFIG"; then
  echo "  [model_providers.${PROVIDER}] 已存在，跳过"
else
  cat >> "$CONFIG" <<EOF

[model_providers.${PROVIDER}]
name = "OpenAI"
base_url = "${RELAY_HOST}/v1"
wire_api = "responses"
env_key = "SUPER_RELAY_KEY"
EOF
fi
chmod 600 "$CONFIG"

echo "[5/6] 持久化 API Key 到 shell 配置"
for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
  [ -e "$RC" ] || continue
  grep -q "SUPER_RELAY_KEY" "$RC" 2>/dev/null || \
    echo "export SUPER_RELAY_KEY=\"$RELAY_API_KEY\"" >> "$RC"
done
# 当前 shell 没有对应 rc 文件时至少写一份，保证下次登录可用
grep -q "SUPER_RELAY_KEY" "$HOME/.zshrc" 2>/dev/null || \
  echo "export SUPER_RELAY_KEY=\"$RELAY_API_KEY\"" >> "$HOME/.zshrc"

export SUPER_RELAY_KEY="$RELAY_API_KEY"

echo "[6/6] 开启 1M 上下文"
# Codex 的上下文窗口取自模型 catalog，不是直接读 config.toml：
#   实际窗口 = min(model_context_window, catalog 的 max_context_window)
#              × effective_context_window_percent
# 托管的 Model ID 不在内置 catalog 里，会回落到 fallback metadata（上限 272000），
# 于是无论 model_context_window 填多大，实际都被截断到 258400。
# 唯一的办法是用 model_catalog_json 覆盖 catalog：克隆一个真实条目，
# 改写 slug 和三个窗口字段。
#
# 但克隆会连带套用模板模型的 base_instructions，等于悄悄换掉 system prompt。
# 所以先跑一次调用，把当前真正在用的 base_instructions 抓下来存成文件，
# 再用 model_instructions_file 固定回去，保证只有窗口变大、提示词不变。
CATALOG="$HOME/.codex/relay_model_catalog.json"
INSTRUCTIONS="$HOME/.codex/relay_base_instructions.txt"

echo "  记录当前的 base_instructions ..."
# rollout 的 session_meta 在发起网络请求之前就落盘，所以这次调用成不成功都无所谓
codex exec --skip-git-repo-check \
  --config model_provider="$PROVIDER" \
  --model "$RELAY_MODEL_ID" \
  "reply with exactly the word: ok" >/dev/null 2>&1 || true

BASELINE="$(ls -t "$HOME"/.codex/sessions/*/*/*/rollout-*.jsonl 2>/dev/null | head -1)"
if [ -z "$BASELINE" ]; then
  echo "  失败：没有生成会话记录，无法读取 base_instructions，请把这段输出发给我们。" >&2
  exit 1
fi
jq -rs '[.[] | select(.type == "session_meta")][0].payload.base_instructions
        | if type == "object" then .text else . end' "$BASELINE" > "$INSTRUCTIONS"
if [ ! -s "$INSTRUCTIONS" ] || [ "$(head -c 4 "$INSTRUCTIONS")" = "null" ]; then
  echo "  失败：会话记录里没有 base_instructions，请把这段输出发给我们。" >&2
  exit 1
fi
echo "  已记录 $(wc -c < "$INSTRUCTIONS" | tr -d ' ') 字节"

codex debug models | jq --arg slug "$RELAY_MODEL_ID" '
  (.models | sort_by(.priority) | map(select(.visibility == "list" and (.upgrade | not))) | first) as $base
  | { models: [ ($base // error("内置 catalog 里没有可用作模板的模型，请把这段输出发给我们"))
      | .slug = $slug
      | .display_name = "Super Relay (1M context)"
      | .context_window = 1000000
      | .max_context_window = 1000000
      | .effective_context_window_percent = 100 ] }' > "$CATALOG.tmp"
mv "$CATALOG.tmp" "$CATALOG"
chmod 600 "$CATALOG" "$INSTRUCTIONS"

# TOML 的顶层键必须排在所有 table 之前，所以这里前置插入而不是追加；
# 已存在的同名键一律不动，避免写出重复键让 config.toml 解析失败
prepend_key() {
  if grep -q "^$1[[:space:]]*=" "$CONFIG"; then
    echo "  $1 已存在，跳过（如需 1M 请自行确认它的值）"
    return
  fi
  { printf '%s\n' "$2"; cat "$CONFIG"; } > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
}
prepend_key model_auto_compact_token_limit "model_auto_compact_token_limit = 950000"
prepend_key model_context_window           "model_context_window = 1000000"
prepend_key model_instructions_file        "model_instructions_file = \"$INSTRUCTIONS\""
prepend_key model_catalog_json             "model_catalog_json = \"$CATALOG\""
chmod 600 "$CONFIG"

echo
echo "配置完成。开始验证："
echo "  确认模型可见 ..."
if curl -sS --max-time 20 "$RELAY_HOST/v1/models" \
     -H "Authorization: Bearer $SUPER_RELAY_KEY" | grep -q -- "$RELAY_MODEL_ID"; then
  echo "  OK：$RELAY_MODEL_ID 在可用列表中"
else
  echo "  失败：该 Key 看不到 ${RELAY_MODEL_ID}，请把这段输出发给我们，不要自行换模型名重试。" >&2
  exit 1
fi

echo "  发起一次最小调用 ..."
codex exec --skip-git-repo-check \
  --config model_provider="$PROVIDER" \
  --model "$RELAY_MODEL_ID" \
  "reply with exactly the word: ok"

# 每次会话都会把生效的窗口写进 rollout 的 task_started 事件，拿它当验收依据
echo "  确认上下文窗口 ..."
ROLLOUT="$(ls -t "$HOME"/.codex/sessions/*/*/*/rollout-*.jsonl 2>/dev/null | head -1)"
WINDOW="$(grep -o '"model_context_window":[0-9]*' "$ROLLOUT" 2>/dev/null | head -1 | cut -d: -f2)"
if [ "$WINDOW" = "1000000" ]; then
  echo "  OK：实际上下文窗口 1000000"
else
  echo "  失败：实际上下文窗口是 ${WINDOW:-未能读出}，期望 1000000，请把这段输出发给我们。" >&2
  exit 1
fi

# 确认放大窗口没有顺带改掉 system prompt（忽略空白差异，jq 会多写一个换行）
echo "  确认提示词未变 ..."
jq -rs '[.[] | select(.type == "session_meta")][0].payload.base_instructions
        | if type == "object" then .text else . end' "$ROLLOUT" > "$INSTRUCTIONS.check"
if [ "$(tr -d '[:space:]' < "$INSTRUCTIONS" | cksum)" = "$(tr -d '[:space:]' < "$INSTRUCTIONS.check" | cksum)" ]; then
  echo "  OK：base_instructions 与放大窗口之前一致"
  rm -f "$INSTRUCTIONS.check"
else
  echo "  失败：base_instructions 变了，请把这段输出发给我们，不要直接开始跑任务。" >&2
  exit 1
fi

cat <<EOF

============================================================
完成。请打开一个新终端后使用（或先执行 source ~/.zshrc）。

日常使用（每次都显式指定 provider 和 model）：
  codex --config model_provider=$PROVIDER --model "$RELAY_MODEL_ID"

批量 / 非交互：
  codex exec --skip-git-repo-check \\
    --config model_provider=$PROVIDER \\
    --model "$RELAY_MODEL_ID" \\
    "你的任务描述"

回滚：
  cp ~/.codex/config.toml.bak.<时间戳> ~/.codex/config.toml
  rm ~/.codex/relay_model_catalog.json ~/.codex/relay_base_instructions.txt
  并删除 shell 配置里的 SUPER_RELAY_KEY 那一行
============================================================
EOF

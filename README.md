# Super Relay 接入说明（供应商版）

你将通过我们提供的网关调用模型服务。**不需要**注册或登录任何平台，也不需要自备账号或订阅。

## 你会收到三样东西

| 项目 | 脚本里的字段 | 形态 |
|------|--------------|------|
| Base URL | `RELAY_BASE_URL` | `https://` 开头的域名 |
| API Key | `RELAY_API_KEY` | 一串带前缀的随机字符 |
| Model ID | `RELAY_MODEL_ID` | 模型名称，不是随机字符 |

这三项通过私下加密渠道单独发给你，不在本文档里。

**请严格按字段名对应填写。**最常见的失败原因是把 API Key 填进了 `RELAY_MODEL_ID`，
这会在验证阶段报「该 Key 看不到 xxx」。

Base URL 请**原样填入**，末尾不要加 `/v1`。两个工具要求的写法不同（Codex CLI 需要
`/v1` 后缀，Claude Code 不需要），脚本会自动处理，不用自己拼。

## 选一个工具

按你们实际要用的工具选**其中一个**脚本，不要两个都跑。

| 工具 | 脚本 | 额外依赖 |
|------|------|----------|
| Codex CLI | `codex-cli-setup.sh` | Node.js 18+、`jq` |
| Claude Code | `claude-code-setup.sh` | Node.js 18+、`jq` |

## 操作步骤

1. 先确认网络能到网关（把 `<BASE_URL>` 换成我们给你的值）。不通就联系我们，
   后面全都白配：

   ```bash
   curl -I <BASE_URL>
   ```

2. 安装对应工具：

   ```bash
   # Codex CLI
   npm install -g @openai/codex

   # 或 Claude Code
   npm install -g @anthropic-ai/claude-code

   # 两个脚本都需要 jq
   brew install jq        # Linux: apt-get install -y jq
   ```

3. 打开脚本，把顶部的 `RELAY_BASE_URL`、`RELAY_API_KEY` 和 `RELAY_MODEL_ID`
   换成我们给你的值。任意一项占位符没替换，脚本都会直接报错退出。

4. 执行（脚本没有可执行权限，请用 `bash` 运行）：

   ```bash
   bash codex-cli-setup.sh
   # 或
   bash claude-code-setup.sh
   ```

5. 脚本会自动备份原有配置、写入配置、确认模型可见，并发起一次最小调用。
   看到返回 `ok` 即接入成功。

脚本都是**增量写入**：你已有的项目信任列表、MCP server、hooks、权限配置和其他环境变量
都会保留，并在修改前备份到同目录的 `.bak.<时间戳>` 文件。

## 上下文窗口

本轮要求**两个工具都跑在 1M 上下文**，脚本已经默认配好，不需要额外操作。两边的开关
位置完全不同，如果你自己改过配置，请按下面自查。

**Codex CLI** 的窗口取自模型 catalog，不是直接读 `config.toml`：

```
实际窗口 = min(model_context_window, catalog 的 max_context_window) × effective_context_window_percent
```

托管的 Model ID 不在内置 catalog 里，会回落到 fallback metadata（上限 272000），
所以**只写 `model_context_window = 1000000` 是无效的**，实际仍然只有 258400。脚本的做法是
生成 `~/.codex/relay_model_catalog.json`，克隆一个真实 catalog 条目并把 slug 改成你的
Model ID、三个窗口字段改成 1M，再用 `model_catalog_json` 指向它。

克隆条目会连带套用模板模型的 `base_instructions`，等于悄悄换掉 system prompt，
可能影响任务表现。所以脚本会先跑一次调用、把你当前实际使用的 `base_instructions`
存到 `~/.codex/relay_base_instructions.txt`，再用 `model_instructions_file` 固定回去，
保证**只有窗口变大、提示词不变**，并在最后一步自动核对这两点。
这两个文件请不要删除，删掉会退回 258400 或换掉提示词。

自查方式：任意一次会话结束后，读最新 rollout 里的窗口值，应当是 `1000000`：

```bash
grep -o '"model_context_window":[0-9]*' \
  "$(ls -t ~/.codex/sessions/*/*/*/rollout-*.jsonl | head -1)" | head -1
```

**Claude Code** 靠 `~/.claude/settings.json` 的 `env` 里三个变量共同生效，缺一不可：

| 变量 | 值 | 作用 |
|------|-----|------|
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `1000000` | 客户端上下文上限 |
| `DISABLE_COMPACT` | `1` | 没有它，上一项在 Model ID 以 `claude-` 开头时会被静默忽略，回落到 200000 |
| `ANTHROPIC_BETAS` | `context-1m-2025-08-07` | 加到 `anthropic-beta` 请求头，上游据此放行超过 200k 的输入 |

因为关掉了 compact，上下文用满时会直接报 `Prompt is too long` 而不是自动压缩历史，
长会话请用 `/clear` 或新开会话。另外不要设 `CLAUDE_CODE_DISABLE_1M_CONTEXT`，
它会关掉所有 1M 相关逻辑。

## 使用约束

- 只使用我们提供的这一个 Model ID，不要尝试其他模型名。
- **不要改动脚本写入的上下文窗口配置**，也不要删除 `~/.codex/relay_model_catalog.json`
  和 `~/.codex/relay_base_instructions.txt`。网关侧已经放行 1M，但两个 CLI 的可用窗口
  是本地算出来的，改动或删除会让实际窗口掉回 258400 / 200000，跑出来的结果不算有效。
- API Key 不得转发给第三方，不得提交到代码仓库或写入日志。它会以明文存在配置文件里
  （`~/.codex/config.toml` 与 shell 配置，或 `~/.claude/settings.json`），
  注意不要把这些文件同步到公开位置。
- 不要在 prompt、工具输出或附件中放入密码、Access Token、Cookie、私钥等敏感数据。
  请求内容会被平台记录用于排障和用量分析。
- Key 有并发上限和有效期。撞到限制请退避重试，不要提高并发或无限重发。
- 长耗时任务请自行实现超时、重试和断点续跑，避免整批失败后无法续跑。

## 报障时请提供

发生时间、`x-request-id`（响应头里返回）、Model ID、HTTP 状态码、完整错误输出。

**不要**提供 API Key、Cookie 或任何凭据。

## 常见错误

| 现象 | 原因 |
|------|------|
| 连不上 / 超时 | 网络通道未开通，或本地代理干扰 |
| `401 Unauthorized` / `Invalid token` | Key 无效、已过期，或新终端未加载环境变量 |
| `404 model_not_found` | Model ID 写错，请用我们给的完整 ID |
| `429` | 撞到并发或限流上限，退避后重试 |
| `x-api-key header is required` | 配置串到了另一种模式，本方案不需要你们自己的上游 Key |
| `SSL_ERROR_SYSCALL` | 本地代理问题，先在干净终端重试 |
| Claude Code 子任务报模型错误 | `settings.json` 里的模型变量有漏填或不一致 |
| Codex 窗口仍是 258400 | `relay_model_catalog.json` 没生成或 slug 与 Model ID 不一致 |
| Codex 报 `Model metadata ... not found` | 同上，catalog 覆盖没生效，走了 fallback metadata |
| 输入超过 200k 就报错 | Claude Code 缺 `ANTHROPIC_BETAS`，或上游未放行 1M |

## 回滚

```bash
# Codex CLI
cp ~/.codex/config.toml.bak.<时间戳> ~/.codex/config.toml
rm ~/.codex/relay_model_catalog.json
# 并删除 ~/.zshrc / ~/.bashrc 里的 SUPER_RELAY_KEY 那一行

# Claude Code
cp ~/.claude/settings.json.bak.<时间戳> ~/.claude/settings.json
```

1.安codex和jq, 跑一遍 codex-cli-setup.sh.
2.D:\Code\Work\Multi-SWE-bench\SWE-rebench\SWE-rebench
3.仓库要在 D:\Code\Work\GSB，跑题记录在 D:\Code\GSB\runs
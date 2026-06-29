# Hermes Companion（PC 端）

[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文** | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**Hermes‑agent Android** 应用的桌面伴侣程序。它让你的手机和电脑通过你自己的网络共享同一个 Hermes 大脑——安全可靠，中间不经过任何云。

两项能力，同一个信任域（配对一次，两者皆可用）：

- **在电脑上运行（mesh）**——从手机派发任务；由电脑上的 Hermes 执行并将结果送回。
- **桌面接力（Desktop Handoff）**——在电脑与手机之间搬移整段 Hermes 对话：把桌面上的对话接力给手机以便在路上继续，并把手机上的对话同步回电脑。合并是幂等的（按 id 进行 upsert + 按自然键对消息去重），因此无论朝哪个方向同步都不会产生重复。

> 这是 **PC 这一半**。手机那一半是 Hermes‑agent Android 应用。

## 下载

预构建的独立二进制文件（无需 Python）随每个 [Release](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app/releases) 附带——下载适合你操作系统的压缩包，解压，然后运行 `hermes-companion`。它会自动启动 broker 并打开本地浏览器控制台（即 GUI）。

> macOS / Windows 构建未签名。首次运行：macOS → 右键 → **打开**；Windows → **更多信息** → **仍要运行**。

## 安装（从源码）

作为 Hermes 插件（一行命令）：

```bash
hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app
```

或直接从克隆的仓库运行：

```bash
git clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app
cd Hermes-agent-android-PC-companion-app
./handoff/mesh-start.sh          # creates an isolated venv, installs deps, prints the pairing QR
```

后台守护进程（开机启动，在后台运行）：

```bash
./handoff/mesh-start.sh daemon on       # turn on
./handoff/mesh-start.sh daemon status   # check
./handoff/mesh-start.sh daemon off      # turn off
```

然后在手机应用中：**在电脑上运行**（或 **设置 → 桌面接力**）→ 扫描该 QR。

需要 Python 3 以及 `PyNaCl`、`zeroconf`、`qrcode`、`pillow`（启动脚本会把它们安装进 `~/.hermes/mesh/venv`；它们也列在 `handoff/requirements.txt` 中）。

## 安全模型

将其开源的全部意义在于你可以**审计**它：

- **端到端加密**——每个载荷在配对设备之间都是一个 NaCl `Box`（Curve25519 + XSalsa20‑Poly1305）。配对通过你当面扫描的 QR 来交换公钥。
- **私钥永不离开设备。** QR 只携带公钥 + 主机/端口。
- **密钥从不进入打包。** 接力导出只读取 `state.db` + `memories/`；`auth.json` / `.env` 绝不会被触及，且会拒绝 `memories/` 中逃逸出该文件夹的符号链接（见 `handoff/tests/test_desktop_export.py`）。
- **绝不绑定到 `0.0.0.0`。** broker 只绑定你的 LAN / Tailscale IP。

安全性建立在密钥之上，而非建立在协议保密之上——所以公开它毫无代价，并让你能够核实上述各项声明。

## 目录结构

```
handoff/
  mesh_broker.py     # the always‑on server: pair / push / poll / ack / pull / push_session
  companion_web.py   # 127.0.0.1 browser console: pairing QR + status + task history
  pairing.py         # identities, QR schema, NaCl Box helpers
  handoff_server.py  # framing + LAN/mDNS helpers
  desktop_export.py  # export a conversation (read‑only, secrets‑safe)
  handoff_core.py    # the shared merge core (session upsert + message dedup + memory union)
  __init__.py        # Hermes plugin entry (register)
  mesh-start.sh      # one‑command start + daemon on/off/status
companion.py         # standalone entry (used by the packaged binary)
```

## 许可证

[AGPL‑3.0](LICENSE)。Copyright (C) 2026 HenWorks。

属于 Hermes 生态系统的一部分；Hermes‑agent 本身采用 MIT 许可（Nous Research）。

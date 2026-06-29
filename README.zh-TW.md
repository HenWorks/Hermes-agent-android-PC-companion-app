# Hermes Companion（PC 端）

[English](README.md) | **繁體中文** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**Hermes‑agent Android** app 的桌面伴侶程式。它讓你的手機與電腦透過你自己的網路共用同一顆 Hermes 大腦——安全可靠，中間不經過任何雲端。

兩項能力，同一個信任域（配對一次，兩者皆可用）：

- **在電腦上執行（mesh）**——從手機派發任務；電腦上的 Hermes 執行後把結果回傳。
- **Desktop Handoff（桌面接力）**——在電腦與手機之間搬移整段 Hermes 對話：把桌面對話接力到手機上隨身續聊，再把手機的對話同步回電腦。合併具備冪等性（以 id 進行 upsert ＋以自然鍵去除重複訊息），所以無論朝哪個方向同步都絕不會產生重複。

> 這是 **PC 那一半**。手機那一半是 Hermes‑agent Android app。

## 下載

每個 [Release](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app/releases) 都附有預先建置的獨立執行檔（無需 Python）——下載對應你作業系統的壓縮檔，解壓後執行 `hermes-companion`。它會自動啟動 broker 並開啟本機的瀏覽器控制台（GUI）。

> macOS／Windows 的建置未經簽署。第一次執行：macOS → 按右鍵 → **打開**；Windows → **更多資訊** → **仍要執行**。

## 安裝（從原始碼）

作為 Hermes plugin（一行搞定）：

```bash
hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app
```

或直接從 clone 執行：

```bash
git clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app
cd Hermes-agent-android-PC-companion-app
./handoff/mesh-start.sh          # creates an isolated venv, installs deps, prints the pairing QR
```

背景常駐程式（開機啟動，於背景執行）：

```bash
./handoff/mesh-start.sh daemon on       # turn on
./handoff/mesh-start.sh daemon status   # check
./handoff/mesh-start.sh daemon off      # turn off
```

接著在手機 app 中：**在電腦上執行**（或 **設定 → Desktop Handoff**）→ 掃描 QR。

需要 Python 3 以及 `PyNaCl`、`zeroconf`、`qrcode`、`pillow`（啟動腳本會將這些安裝到 `~/.hermes/mesh/venv`；它們也列在 `handoff/requirements.txt` 中）。

## 安全模型

把這套東西開源的整個重點，就是讓你能夠**稽核**它：

- **端到端加密**——每一筆 payload 都是配對裝置之間的一個 NaCl `Box`（Curve25519 ＋ XSalsa20‑Poly1305）。配對時透過你當面掃描的 QR 交換公鑰。
- **私鑰永不離開裝置。** QR 只攜帶公鑰 ＋ host／port。
- **機密永不進入 bundle。** Handoff 匯出只讀取 `state.db` ＋ `memories/`；`auth.json`／`.env` 完全不會被碰，而且 `memories/` 中逃出資料夾的 symlink 會被拒絕（見 `handoff/tests/test_desktop_export.py`）。
- **絕不綁定 `0.0.0.0`。** broker 只綁定你的 LAN／Tailscale IP。

安全性建立在金鑰之上，而非建立在協定保密之上——所以公開它毫無代價，並且讓你能驗證上述各項主張。

## 目錄結構

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

## 授權

[AGPL‑3.0](LICENSE)。Copyright (C) 2026 HenWorks。

屬於 Hermes 生態系的一部分；Hermes‑agent 本身為 MIT 授權（Nous Research）。

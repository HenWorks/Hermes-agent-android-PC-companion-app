# Hermes Companion (PC 側)

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | **日本語** | [한국어](README.ko.md) | [Español](README.es.md)

**Hermes‑agent Android** アプリ向けのデスクトップコンパニオンです。お使いのネット
ワーク上で、スマートフォンとコンピューターが 1 つの Hermes ブレインを共有できます。
クラウドを介さず、安全に動作します。

2 つの機能、1 つの信頼ドメイン（一度ペアリングすれば両方が動作）：

- **Run on Computer (mesh)** — スマートフォンからタスクを送信すると、コンピューター
  上の Hermes がそれを実行し、結果を返します。
- **Desktop Handoff** — Hermes の会話全体をコンピューターとスマートフォンの間で移動
  します。デスクトップの会話をスマートフォンに引き継いで外出先で続行したり、スマート
  フォンの会話をコンピューターに同期して戻したりできます。マージはべき等です（ID に
  よる upsert ＋自然キーによるメッセージの重複排除）。そのため、どちらの方向に同期し
  ても重複は発生しません。

> これは **PC 側** です。スマートフォン側は Hermes‑agent Android アプリです。

## ダウンロード

ビルド済みのスタンドアロンバイナリ（Python 不要）が各
[Release](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app/releases) に
添付されています。お使いの OS 用のアーカイブをダウンロードし、解凍して `hermes-companion`
を実行してください。ブローカーを起動し、ローカルのブラウザコンソール（GUI）を自動的に
開きます。

> macOS / Windows ビルドは署名されていません。初回実行時：macOS → 右クリック →
> **開く**、Windows → **詳細情報** → **実行**。

## インストール（ソースから）

Hermes プラグインとして（1 行）：

```bash
hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app
```

または、クローンから直接実行：

```bash
git clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app
cd Hermes-agent-android-PC-companion-app
./handoff/mesh-start.sh          # creates an isolated venv, installs deps, prints the pairing QR
```

バックグラウンドデーモン（起動時に開始し、バックグラウンドで実行）：

```bash
./handoff/mesh-start.sh daemon on       # turn on
./handoff/mesh-start.sh daemon status   # check
./handoff/mesh-start.sh daemon off      # turn off
```

その後、スマートフォンアプリで：**Run on Computer**（または **Settings → Desktop Handoff**）→ QR をスキャンします。

Python 3 と `PyNaCl`、`zeroconf`、`qrcode`、`pillow` が必要です（start スクリプトがこれら
を `~/.hermes/mesh/venv` にインストールします。`handoff/requirements.txt` にも記載されて
います）。

## セキュリティモデル

これをオープンソース化する目的は、まさにあなたが **監査** できるようにすることです：

- **エンドツーエンド暗号化** — すべてのペイロードは、ペアリングされたデバイス間の
  NaCl `Box`（Curve25519 ＋ XSalsa20‑Poly1305）です。ペアリングでは、対面でスキャンする
  QR を介して公開鍵を交換します。
- **秘密鍵がデバイスを離れることはありません。** QR には公開鍵とホスト/ポートのみが
  含まれます。
- **シークレットがバンドルに入ることはありません。** Handoff のエクスポートは `state.db`
  ＋ `memories/` のみを読み取ります。`auth.json` / `.env` には一切触れず、フォルダの外に
  出る `memories/` のシンボリックリンクは拒否されます（`handoff/tests/test_desktop_export.py`
  を参照）。
- **`0.0.0.0` にバインドすることは決してありません。** ブローカーはあなたの LAN /
  Tailscale IP のみにバインドします。

セキュリティは鍵に依存しており、プロトコルが秘密であることには依存していません。その
ため、公開してもコストはかからず、上記の主張を検証できます。

## レイアウト

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

## ライセンス

[AGPL‑3.0](LICENSE)。Copyright (C) 2026 HenWorks。

Hermes エコシステムの一部です。Hermes‑agent 自体は MIT（Nous Research）です。

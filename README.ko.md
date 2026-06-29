# Hermes Companion (PC 측)

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | **한국어** | [Español](README.es.md)

**Hermes‑agent Android** 앱을 위한 데스크톱 컴패니언입니다. 사용자의 휴대폰과
컴퓨터가 자체 네트워크를 통해 하나의 Hermes 두뇌를 공유할 수 있게 해 줍니다 — 중간에
클라우드 없이 안전하게.

두 가지 기능, 하나의 신뢰 도메인(한 번 페어링하면 둘 다 동작):

- **컴퓨터에서 실행(mesh)** — 휴대폰에서 작업을 전달하면, 컴퓨터의 Hermes가
  이를 실행하고 결과를 다시 보내 줍니다.
- **데스크톱 핸드오프(Desktop Handoff)** — 컴퓨터와 휴대폰 사이에서 Hermes 대화 전체를
  옮깁니다: 데스크톱 대화를 휴대폰으로 넘겨 이동 중에 이어가고, 휴대폰의 대화를 다시
  컴퓨터로 동기화합니다. 병합은 멱등(idempotent)하므로(ID 기준 upsert + 자연 키 메시지
  중복 제거), 어느 방향으로 동기화해도 절대 중복되지 않습니다.

> 이것은 **PC 절반**입니다. 휴대폰 절반은 Hermes‑agent Android 앱입니다.

## 다운로드

사전 빌드된 독립 실행형 바이너리(Python 불필요)가 각
[Release](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app/releases)에
첨부되어 있습니다 — 사용하는 OS용 압축 파일을 다운로드하여 압축을 풀고 `hermes-companion`을
실행하세요. 브로커를 시작하고 로컬 브라우저 콘솔(GUI)을 자동으로 엽니다.

> macOS / Windows 빌드는 서명되지 않았습니다. 첫 실행 시: macOS → 우클릭 → **열기**;
> Windows → **추가 정보** → **실행**.

## 설치(소스에서)

Hermes 플러그인으로(한 줄):

```bash
hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app
```

또는 클론에서 직접 실행:

```bash
git clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app
cd Hermes-agent-android-PC-companion-app
./handoff/mesh-start.sh          # creates an isolated venv, installs deps, prints the pairing QR
```

백그라운드 데몬(부팅 시 시작, 백그라운드 실행):

```bash
./handoff/mesh-start.sh daemon on       # turn on
./handoff/mesh-start.sh daemon status   # check
./handoff/mesh-start.sh daemon off      # turn off
```

그런 다음 휴대폰 앱에서: **컴퓨터에서 실행**(또는 **설정 → 데스크톱 핸드오프**) → QR을 스캔하세요.

Python 3과 `PyNaCl`, `zeroconf`, `qrcode`, `pillow`가 필요합니다(시작 스크립트가 이들을
`~/.hermes/mesh/venv`에 설치합니다. `handoff/requirements.txt`에도 포함되어 있습니다).

## 보안 모델

이것을 오픈소스로 공개하는 핵심 목적은 사용자가 직접 **감사(audit)** 할 수 있도록 하는 것입니다:

- **종단 간 암호화** — 모든 페이로드는 페어링된 기기 사이의 NaCl `Box`
  (Curve25519 + XSalsa20‑Poly1305)입니다. 페어링은 직접 스캔하는 QR을 통해 공개 키를
  교환합니다.
- **개인 키는 절대 기기를 떠나지 않습니다.** QR에는 공개 키 + 호스트/포트만 담깁니다.
- **비밀 값은 번들에 들어가지 않습니다.** 핸드오프 내보내기는 `state.db` + `memories/`만
  읽습니다. `auth.json` / `.env`는 절대 건드리지 않으며, 폴더를 벗어나는 `memories/`
  심볼릭 링크는 거부됩니다(`handoff/tests/test_desktop_export.py` 참조).
- **절대 `0.0.0.0`에 바인딩하지 않습니다.** 브로커는 사용자의 LAN / Tailscale IP에만
  바인딩합니다.

보안은 프로토콜의 비밀성이 아니라 키에 의존합니다 — 따라서 공개해도 비용이 들지 않으며,
위의 주장을 직접 검증할 수 있습니다.

## 구성

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

## 라이선스

[AGPL‑3.0](LICENSE). Copyright (C) 2026 HenWorks.

Hermes 생태계의 일부입니다. Hermes‑agent 자체는 MIT(Nous Research)입니다.

# Hermes Companion (lado PC)

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | **Español**

El companion de escritorio para la app **Hermes‑agent Android**. Permite que tu teléfono
y tu computadora compartan un único cerebro Hermes a través de tu propia red — de forma
segura, sin ninguna nube de por medio.

Dos capacidades, un solo dominio de confianza (empareja una vez, ambas funcionan):

- **Run on Computer (mesh)** — despacha una tarea desde tu teléfono; el Hermes de tu
  computadora la ejecuta y devuelve el resultado.
- **Desktop Handoff** — mueve conversaciones completas de Hermes entre la computadora y
  el teléfono: pasa una conversación del escritorio a tu teléfono para continuarla sobre
  la marcha, y sincroniza de vuelta las conversaciones del teléfono a tu computadora. Las
  fusiones son idempotentes (upsert por id + deduplicación de mensajes por clave natural),
  por lo que sincronizar en cualquier dirección nunca genera duplicados.

> Esta es la **mitad del PC**. La mitad del teléfono es la app Hermes‑agent Android.

## Descarga

A cada [Release](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app/releases)
se adjuntan binarios autónomos precompilados (sin necesidad de Python) — descarga el
archivo para tu sistema operativo, descomprímelo y ejecuta `hermes-companion`. Inicia el
broker y abre automáticamente la consola del navegador local (la GUI).

> Las compilaciones de macOS / Windows no están firmadas. Primera ejecución: macOS →
> clic derecho → **Open**; Windows → **More info** → **Run anyway**.

## Instalación (desde el código fuente)

Como plugin de Hermes (una línea):

```bash
hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app
```

O ejecútalo directamente desde un clon:

```bash
git clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app
cd Hermes-agent-android-PC-companion-app
./handoff/mesh-start.sh          # creates an isolated venv, installs deps, prints the pairing QR
```

Daemon en segundo plano (iniciar al arrancar, ejecutar en segundo plano):

```bash
./handoff/mesh-start.sh daemon on       # turn on
./handoff/mesh-start.sh daemon status   # check
./handoff/mesh-start.sh daemon off      # turn off
```

Luego, en la app del teléfono: **Run on Computer** (o **Settings → Desktop Handoff**) → escanea el QR.

Requiere Python 3 y `PyNaCl`, `zeroconf`, `qrcode`, `pillow` (el script de inicio los
instala en `~/.hermes/mesh/venv`; también están en `handoff/requirements.txt`).

## Modelo de seguridad

El objetivo principal de hacer esto de código abierto es que puedas **auditarlo**:

- **Cifrado de extremo a extremo** — cada payload es un `Box` de NaCl (Curve25519 + XSalsa20‑Poly1305)
  entre dispositivos emparejados. El emparejamiento intercambia las claves públicas mediante
  un QR que escaneas en persona.
- **Las claves privadas nunca salen del dispositivo.** El QR solo transporta la clave pública + host/puerto.
- **Los secretos nunca entran en un bundle.** Las exportaciones de Handoff leen únicamente
  `state.db` + `memories/`; `auth.json` / `.env` nunca se tocan, y se rechazan los symlinks
  de `memories/` que escapen de la carpeta (ver `handoff/tests/test_desktop_export.py`).
- **Nunca se enlaza a `0.0.0.0`.** El broker solo se enlaza a tu IP de LAN / Tailscale.

La seguridad descansa en las claves, no en que el protocolo sea secreto — por eso publicarlo
no cuesta nada y te permite verificar las afirmaciones anteriores.

## Estructura

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

## Licencia

[AGPL‑3.0](LICENSE). Copyright (C) 2026 HenWorks.

Parte del ecosistema Hermes; Hermes‑agent en sí es MIT (Nous Research).

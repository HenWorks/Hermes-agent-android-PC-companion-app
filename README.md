# Hermes Companion (PC side)

The desktop companion for the **Hermes‑agent Android** app. It lets your phone and
your computer share one Hermes brain over your own network — securely, with no cloud
in between.

Two capabilities, one trust domain (pair once, both work):

- **Run on Computer (mesh)** — dispatch a task from your phone; your computer's Hermes
  runs it and sends the result back.
- **Desktop Handoff** — move whole Hermes conversations between computer and phone:
  hand a desktop conversation off to your phone to continue on the go, and sync the
  phone's conversations back to your computer. Merges are idempotent (by‑id upsert +
  natural‑key message dedup), so syncing in either direction never duplicates.

> This is the **PC half**. The phone half is the Hermes‑agent Android app.

## Install

As a Hermes plugin (recommended — one line):

```bash
hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app
```

Or run it directly from a clone:

```bash
git clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app
cd Hermes-agent-android-PC-companion-app
./handoff/mesh-start.sh          # creates an isolated venv, installs deps, prints the pairing QR
# ./handoff/mesh-start.sh --autostart    # also install login/boot autostart (macOS launchd / Linux systemd --user)
```

Then in the phone app: **Run on Computer** (or **Settings → Desktop Handoff**) → scan the QR.

Requires Python 3 and `PyNaCl`, `zeroconf`, `qrcode`, `pillow` (the start script installs
these into `~/.hermes/mesh/venv`; they're also in `handoff/requirements.txt`).

## Security model

The whole point of open‑sourcing this is that you can **audit** it:

- **End‑to‑end encryption** — every payload is a NaCl `Box` (Curve25519 + XSalsa20‑Poly1305)
  between paired devices. Pairing exchanges public keys via a QR you scan in person.
- **Private keys never leave the device.** The QR carries only public key + host/port.
- **Secrets never enter a bundle.** Handoff exports read only `state.db` + `memories/`;
  `auth.json` / `.env` are never touched, and `memories/` symlinks that escape the folder
  are refused (see `handoff/tests/test_desktop_export.py`).
- **Never binds to `0.0.0.0`.** The broker binds your LAN/Tailscale IP only.

Security rests on the keys, not on the protocol being secret — so publishing it costs
nothing and lets you verify the claims above.

## Layout

```
handoff/
  mesh_broker.py     # the always‑on server: pair / push / poll / ack / pull / push_session
  companion_web.py   # 127.0.0.1 browser console: pairing QR + status + task history
  pairing.py         # identities, QR schema, NaCl Box helpers
  handoff_server.py  # framing + LAN/mDNS helpers
  desktop_export.py  # export a conversation (read‑only, secrets‑safe)
  handoff_core.py    # the shared merge core (session upsert + message dedup + memory union)
  __init__.py        # Hermes plugin entry (register)
  mesh-start.sh      # one‑command start + optional autostart
```

## License

[AGPL‑3.0](LICENSE). Copyright (C) 2026 HenWorks.

Part of the Hermes ecosystem; Hermes‑agent itself is MIT (Nous Research).

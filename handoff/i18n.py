"""Lightweight i18n for the companion's CLI and tray (standard library only).

Detects the system locale once at import and maps it to one of the supported
languages (English fallback). Same six languages as the browser console and the
phone app: en / zh-TW / zh-CN / ja / ko / es.

Usage:
    from i18n import t
    print(t("running"))
    print(t("console", url=url))
"""
from __future__ import annotations

import locale as _locale
import os
import sys

SUPPORTED = ("en", "zh-TW", "zh-CN", "ja", "ko", "es")


def _os_language() -> str:
    """Query the OS's UI language when no LANG env is set (packaged .app / .exe).

    macOS   → `defaults read -g AppleLanguages` (e.g. "zh-Hant-CN").
    Windows → GetUserDefaultUILanguage() → locale name (e.g. "zh_TW").
    Returns "" if it can't be determined (caller falls back further).
    """
    try:
        if sys.platform == "darwin":
            import re
            import subprocess
            out = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            m = re.search(r'"?([A-Za-z]{2}[\w-]*)"?', out)  # first language tag
            return m.group(1) if m else ""
        if sys.platform.startswith("win"):
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            return _locale.windows_locale.get(lcid, "")
    except Exception:  # noqa: BLE001 — best-effort; any failure → fall through
        pass
    return ""


def _detect() -> str:
    raw = ""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v:
            raw = v
            break
    if not raw:
        # Finder/Explorer-launched apps (the packaged .app / .exe) inherit no LANG
        # env, so fall back to the OS's own UI-language setting.
        raw = _os_language()
    if not raw:
        try:
            raw = (_locale.getlocale()[0] or "") or (_locale.getdefaultlocale()[0] or "")
        except Exception:  # noqa: BLE001 — locale lookup can fail on minimal systems
            raw = ""
    raw = raw.replace("_", "-").lower()
    if raw.startswith("zh"):
        # Traditional for TW/HK/MO or an explicit "hant" tag; Simplified otherwise.
        if any(tag in raw for tag in ("tw", "hk", "mo", "hant")):
            return "zh-TW"
        return "zh-CN"
    for code in ("ja", "ko", "es"):
        if raw.startswith(code):
            return code
    return "en"


LANG = _detect()

# key -> {lang: template}. Only the human-facing CLI/tray strings; internal [mesh]
# diagnostic logs stay English. Format placeholders are shared across all languages.
_T: dict[str, dict[str, str]] = {
    "started": {
        "en": "[companion] device {id} bound to {bind} (collaboration + handoff; mDNS advertising, pairing window open 5 minutes)",
        "zh-TW": "[companion] 裝置 {id} 已綁定 {bind}（協作 + 接力；mDNS 廣告中、配對視窗開放 5 分鐘）",
        "zh-CN": "[companion] 设备 {id} 已绑定 {bind}（协作 + 接力；mDNS 广告中、配对窗口开放 5 分钟）",
        "ja": "[companion] デバイス {id} を {bind} にバインドしました（連携 + 引き継ぎ；mDNS 広告中、ペアリング受付 5 分間）",
        "ko": "[companion] 기기 {id} 를 {bind} 에 바인딩했습니다 (협업 + 핸드오프; mDNS 광고 중, 페어링 창 5분 개방)",
        "es": "[companion] dispositivo {id} enlazado a {bind} (colaboración + transferencia; anunciando por mDNS, ventana de emparejamiento 5 minutos)",
    },
    "console": {
        "en": "Console: {url} (opening the browser; scan the QR on the page to connect)",
        "zh-TW": "控制台：{url}（正在開啟瀏覽器；用手機掃頁面上的 QR 即可連結）",
        "zh-CN": "控制台：{url}（正在打开浏览器；用手机扫页面上的 QR 即可连接）",
        "ja": "コンソール：{url}（ブラウザを開いています；ページの QR をスキャンして接続）",
        "ko": "콘솔: {url} (브라우저를 여는 중; 페이지의 QR을 스캔하여 연결)",
        "es": "Consola: {url} (abriendo el navegador; escanea el QR de la página para conectar)",
    },
    "console_fail": {
        "en": "(browser console failed to start: {err}; using the terminal QR below instead)",
        "zh-TW": "（瀏覽器控制台啟動失敗：{err}；改用下方的終端 QR）",
        "zh-CN": "（浏览器控制台启动失败：{err}；改用下方的终端 QR）",
        "ja": "（ブラウザコンソールの起動に失敗：{err}；下のターミナル QR を使用します）",
        "ko": "(브라우저 콘솔 시작 실패: {err}; 아래 터미널 QR을 사용합니다)",
        "es": "(la consola del navegador no pudo iniciarse: {err}; usando el QR de terminal de abajo)",
    },
    "handoff_qr": {
        "en": "Handoff QR (session={session}):",
        "zh-TW": "接力 QR（session={session}）：",
        "zh-CN": "接力 QR（session={session}）：",
        "ja": "引き継ぎ QR（session={session}）：",
        "ko": "핸드오프 QR (session={session}):",
        "es": "QR de transferencia (session={session}):",
    },
    "pair_qr": {
        "en": "Pairing QR (text; or just use the image in the browser console):",
        "zh-TW": "配對 QR（文字；或直接用瀏覽器控制台上的圖）：",
        "zh-CN": "配对 QR（文字；或直接用浏览器控制台上的图）：",
        "ja": "ペアリング QR（テキスト；またはブラウザコンソールの画像を使用）：",
        "ko": "페어링 QR (텍스트; 또는 브라우저 콘솔의 이미지를 사용):",
        "es": "QR de emparejamiento (texto; o usa la imagen en la consola del navegador):",
    },
    "running": {
        "en": "companion running, Ctrl+C to quit.",
        "zh-TW": "companion 執行中，Ctrl+C 結束。",
        "zh-CN": "companion 运行中，Ctrl+C 结束。",
        "ja": "companion 実行中、Ctrl+C で終了。",
        "ko": "companion 실행 중, Ctrl+C로 종료.",
        "es": "companion en ejecución, Ctrl+C para salir.",
    },
    "stopped": {
        "en": "Stopped.",
        "zh-TW": "已停止。",
        "zh-CN": "已停止。",
        "ja": "停止しました。",
        "ko": "중지되었습니다.",
        "es": "Detenido.",
    },
    # ── tray app ──
    "tray_title": {
        "en": "Hermes Companion",
        "zh-TW": "Hermes Companion",
        "zh-CN": "Hermes Companion",
        "ja": "Hermes Companion",
        "ko": "Hermes Companion",
        "es": "Hermes Companion",
    },
    "tray_running": {
        "en": "● Running — {bind}",
        "zh-TW": "● 執行中 — {bind}",
        "zh-CN": "● 运行中 — {bind}",
        "ja": "● 実行中 — {bind}",
        "ko": "● 실행 중 — {bind}",
        "es": "● En ejecución — {bind}",
    },
    "tray_open_console": {
        "en": "Open console",
        "zh-TW": "開啟控制台",
        "zh-CN": "打开控制台",
        "ja": "コンソールを開く",
        "ko": "콘솔 열기",
        "es": "Abrir consola",
    },
    "tray_reopen_pairing": {
        "en": "Reopen pairing (5 min)",
        "zh-TW": "重新開放配對（5 分鐘）",
        "zh-CN": "重新开放配对（5 分钟）",
        "ja": "ペアリングを再開（5分）",
        "ko": "페어링 다시 열기 (5분)",
        "es": "Reabrir emparejamiento (5 min)",
    },
    "tray_quit": {
        "en": "Quit",
        "zh-TW": "結束",
        "zh-CN": "退出",
        "ja": "終了",
        "ko": "종료",
        "es": "Salir",
    },
}


def t(key: str, **fmt) -> str:
    """Return the localized string for key (English fallback), formatted with fmt."""
    table = _T.get(key, {})
    s = table.get(LANG) or table.get("en") or key
    return s.format(**fmt) if fmt else s

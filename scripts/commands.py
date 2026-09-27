"""Slyrix ops commands: /ping, /backup через Bot API getUpdates.

Крон-Action раз в 5 мин (лонгпуллинг негде держать — отдельного сервера
у опс-бота нет и не надо). Только stdlib. Отвечает ТОЛЬКО своему chat id
(OPS_CHAT_ID), остальных молча игнорит — бот де-факто приватный.

- /ping — pong + живой статус обоих стендов (быстрый /health).
- /backup — workflow_dispatch джобы ops-backup через GitHub API
  (нужен GH_TOKEN с actions:write; в Action это secrets.GITHUB_TOKEN).
  Сами базы как раньше прилетят файлами от бэкап-джобы.
Без OPS_BOT_TOKEN/OPS_CHAT_ID — dry-run: ничего не трогаем.
Выход всегда 0.
"""

import json
import os
import sys
import urllib.request

from watch import APPS, check_app

TOKEN = os.environ.get("OPS_BOT_TOKEN", "")
CHAT = str(os.environ.get("OPS_CHAT_ID", ""))
GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TIMEOUT = 15
STATE_FILE = os.environ.get("CMD_STATE_FILE", ".ops_cmd_state.json")


def tg(method, params=None):
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def reply(text):
    tg("sendMessage", {"chat_id": CHAT, "text": text})


def dispatch_backup():
    if not GH_TOKEN or not REPO:
        return False, "no GH_TOKEN/GITHUB_REPOSITORY"
    body = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/backup.yml/dispatches",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GH_TOKEN}",
            "User-Agent": "slyrix-ops-commands/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return (True, "") if resp.status == 204 else (False, f"gh:{resp.status}")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        print("state save failed:", exc, file=sys.stderr)


def handle(text):
    # "   " (только пробелы) раньше ронял split()[0] с IndexError.
    parts = (text or "").strip().split()
    cmd = parts[0].split("@")[0] if parts else ""
    if cmd == "/ping":
        lines = []
        for name, base in APPS.items():
            status, detail = check_app(name, base)
            lines.append(f"{name}: {status}" + (f" ({detail})" if detail else ""))
        reply("pong\n" + "\n".join(lines))
        return "ping"
    if cmd == "/backup":
        ok, err = dispatch_backup()
        reply("backup started" if ok else f"backup failed: {err}")
        return "backup"
    return "ignored"


def main():
    if not TOKEN or not CHAT:
        print("SKIP commands (no OPS_BOT_TOKEN/OPS_CHAT_ID)", file=sys.stderr)
        return
    state = load_state()
    offset = state.get("update_offset", 0)
    try:
        data = tg("getUpdates", {"offset": offset, "timeout": 0})
    except Exception as exc:
        print("getUpdates failed:", exc, file=sys.stderr)
        return
    pending = data.get("result", []) or []
    for upd in pending:
        offset = max(offset, upd.get("update_id", 0) + 1)
    # Фолбэк-поллер после долгой паузы: очередь может содержать десятки
    # повторов — выполняем только последние 5, остальное просто подтверждаем.
    # Каждый апдейт в try: один битый не должен клинить всю очередь.
    for upd in pending[-5:]:
        try:
            msg = upd.get("message", {}) or {}
            if str(msg.get("chat", {}).get("id", "")) != CHAT:
                continue
            handle(msg.get("text", ""))
        except Exception as exc:
            print("update failed, skipping:", exc, file=sys.stderr)
            continue
    state["update_offset"] = offset
    save_state(state)


if __name__ == "__main__":
    main()

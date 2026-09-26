"""Slyrix ops watcher: /health обоих стендов + инциденты Fly statuspage.

Запуск: python3 scripts/watch.py (нужны env OPS_BOT_TOKEN, OPS_CHAT_ID).
Только stdlib, без зависимостей. Состояние — STATE_FILE (дедуплика алертов:
орём только на переходах ok<->bad и на новых инцидентах).
Выход всегда 0: наша работа — алертить, а не падать.
"""

import json
import os
import sys
import urllib.request

APPS = {
    "prod": "https://wiki.slyrix.xyz",
    "nightly": "https://nightly.slyrix.xyz",
}
WEBHOOKS = {
    "prod": "https://wiki.slyrix.xyz/ops-hook",
    "nightly": "https://nightly.slyrix.xyz/ops-hook",
}
FLY_STATUS_API = "https://status.flyio.net/api/v2/summary.json"
# Наши регионы: инциденты остальных смотрим краем глаза, но орём только по этим.
OUR_REGIONS = ("fra", "ams", "Fra", "Ams", "Frankfurt", "Amsterdam")
TIMEOUT = 15
STATE_FILE = os.environ.get("STATE_FILE", ".ops_state.json")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "slyrix-ops-watch/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def check_app(name, base):
    try:
        status, data = fetch_json(base.rstrip("/") + "/health")
    except Exception as exc:
        return ("down", f"{type(exc).__name__}: {exc}")
    if status != 200:
        return ("down", f"HTTP {status}")
    if not isinstance(data, dict) or data.get("status") != "ok":
        return ("down", f"bad body: {str(data)[:120]}")
    return ("ok", "")


def check_fly():
    """Возвращает список активных инцидентов [{id, name, impact}]."""
    try:
        _, data = fetch_json(FLY_STATUS_API)
    except Exception as exc:
        return None, f"status API unreachable: {type(exc).__name__}"
    out = []
    for inc in data.get("incidents", []) or []:
        st = (inc.get("status") or "").lower()
        if st in ("resolved", "postmortem", "completed"):
            continue
        out.append({
            "id": inc.get("id", "?"),
            "name": inc.get("name", "?"),
            "impact": inc.get("impact", "?"),
            "status": inc.get("status", "?"),
        })
    return out, ""


def send(text):
    token = os.environ.get("OPS_BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
    chat = os.environ.get("OPS_CHAT_ID", "")
    if not token or not chat:
        print("SKIP send (no OPS_BOT_TOKEN/OPS_CHAT_ID):", text, file=sys.stderr)
        return
    payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except Exception as exc:
        print("send failed:", exc, file=sys.stderr)


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


def relevant(inc):
    blob = json.dumps(inc).lower()
    if any(r.lower() in blob for r in OUR_REGIONS):
        return True
    # Глобальные компоненты (edge, DNS, API) бьют по всем — тоже релевантны.
    for key in ("edge", "dns", "anycast", "api", "global"):
        if key in blob:
            return True
    return False


def set_webhook(url):
    """Переключить вебхук опс-бота на другой стенд. Возвращает True при успехе."""
    token = os.environ.get("OPS_BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
    secret = os.environ.get("OPS_HOOK_SECRET", "")
    if not token or not secret:
        print("SKIP setWebhook (no OPS_BOT_TOKEN/OPS_HOOK_SECRET)", file=sys.stderr)
        return False
    payload = json.dumps({"url": url, "secret_token": secret}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception as exc:
        print("setWebhook failed:", exc, file=sys.stderr)
        return False


def main():
    state = load_state()
    apps_state = state.get("apps", {})
    known_incidents = set(state.get("incidents", []))

    for name, base in APPS.items():
        status, detail = check_app(name, base)
        prev = apps_state.get(name, "ok")
        if status != prev:
            if status == "ok":
                send(f"OK {name} снова в строю ({base})")
            else:
                send(f"DOWN {name} ({base}): {detail}")
            apps_state[name] = status

    incidents, err = check_fly()
    if incidents is None:
        print(err, file=sys.stderr)
    else:
        current = set()
        for inc in incidents:
            if not relevant(inc):
                continue
            current.add(inc["id"])
            if inc["id"] not in known_incidents:
                send(
                    f"FLY incident [{inc['impact']}]: {inc['name']} "
                    f"(status: {inc['status']})"
                )
        for gone in known_incidents - current:
            send(f"FLY incident resolved: {gone}")
        state["incidents"] = sorted(current)

    state["apps"] = apps_state

    # Failover вебхука команд: прод лежит 2 тика подряд — команды едут
    # через найтли; прод ожил — возвращаем обратно. Флип только со сменой.
    if apps_state.get("prod", "ok") != "ok":
        state["prod_down_streak"] = state.get("prod_down_streak", 0) + 1
    else:
        state["prod_down_streak"] = 0
    desired = (
        WEBHOOKS["nightly"] if state["prod_down_streak"] >= 2 else WEBHOOKS["prod"]
    )
    if state.get("webhook") != desired:
        if set_webhook(desired):
            state["webhook"] = desired
            where = "nightly" if desired == WEBHOOKS["nightly"] else "prod"
            send(f"ops webhook → {where}")

    save_state(state)


if __name__ == "__main__":
    main()

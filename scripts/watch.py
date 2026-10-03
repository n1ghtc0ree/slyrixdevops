"""Slyrix ops watcher: /health обоих стендов + инциденты Fly statuspage.

Запуск: python3 scripts/watch.py (нужны env OPS_BOT_TOKEN, OPS_CHAT_ID).
Только stdlib, без зависимостей. Состояние — STATE_FILE (дедуплика алертов:
орём только на переходах ok<->bad и на новых инцидентах).
Выход 0 всегда, кроме слепоты алёртов: мёртвый токен бота = exit 1.
"""

import json
import os
import re
import sys
import time
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
FLY_STATUS_INCIDENT_URL = "https://status.flyio.net/incidents/"
# Короткие коды — только по границам слов: "fra" не матчит "France"/
# "infrastructure", "ams" — "streams", "api" — "capacity", "edge" — "knowledge".
OUR_REGIONS = ("fra", "ams", "Frankfurt", "Amsterdam", "germany", "netherlands")
# Глобальные компоненты (edge, DNS, API) бьют по всем — тоже релевантны.
GLOBAL_KEYS = ("edge", "dns", "anycast", "api", "global")
REGION_RES = tuple(
    re.compile(r"\b" + re.escape(k.lower()) + r"\b")
    for k in OUR_REGIONS + GLOBAL_KEYS
)
TIMEOUT = 15
STATE_FILE = os.environ.get("STATE_FILE", ".ops_state.json")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "slyrix-ops-watch/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


HEALTH_TIMEOUT = 30


def check_app(name, base):
    # Две попытки: первая после сна машины часто упирается в побудку.
    last = ("down", "unreachable")
    for _ in range(2):
        try:
            req = urllib.request.Request(
                base.rstrip("/") + "/health",
                headers={"User-Agent": "slyrix-ops-watch/1.0"},
            )
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
                status, data = resp.status, json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = ("down", f"{type(exc).__name__}: {exc}")
            time.sleep(5)
            continue
        if status != 200:
            return ("down", f"HTTP {status}")
        if not isinstance(data, dict) or data.get("status") != "ok":
            return ("down", f"bad body: {str(data)[:120]}")
        return ("ok", "")
    return last


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


def fly_incident_url(inc_id):
    return f"{FLY_STATUS_INCIDENT_URL}{inc_id}"


def relevant(inc):
    blob = json.dumps(inc).lower()
    return any(rx.search(blob) for rx in REGION_RES)


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


def check_bot():
    """Heartbeat: токен жив? 3 попытки, иначе False (слепота алёртов)."""
    token = os.environ.get("OPS_BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
    if not token:
        print("SKIP check_bot (no OPS_BOT_TOKEN)", file=sys.stderr)
        return True
    for _ in range(3):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/getMe",
                headers={"User-Agent": "slyrix-ops-watch/1.0"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                if json.loads(resp.read().decode("utf-8")).get("ok"):
                    return True
        except Exception as exc:
            print("check_bot retry:", exc, file=sys.stderr)
            time.sleep(10)
    return False


def main():
    if not check_bot():
        print("BOT TOKEN DEAD — alerting blind, failing loud", file=sys.stderr)
        save_state(load_state())
        return 1
    state = load_state()
    now_tick = int(time.time())
    last_tick = state.get("last_tick", 0)
    if last_tick and now_tick - last_tick > 1800:
        gap_h = round((now_tick - last_tick) / 3600, 1)
        send(f"ops-watch молчал {gap_h} ч (крон стоял?) — алерты за это время могли опоздать")
    state["last_tick"] = now_tick
    apps_state = state.get("apps", {})
    raw_known = state.get("incidents", [])
    # Миграция со старого формата (плоский список id).
    known_incidents = {i: "?" for i in raw_known} if isinstance(raw_known, list) else dict(raw_known)

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
        state["fly_fail_streak"] = state.get("fly_fail_streak", 0) + 1
        if state["fly_fail_streak"] == 3:
            send("Fly status API blind 3 тика подряд — инциденты не видны")
        print(err, file=sys.stderr)
    else:
        current = {}
        for inc in incidents:
            if not relevant(inc):
                continue
            current[inc["id"]] = inc["status"]
            prev = known_incidents.get(inc["id"])
            if prev is None:
                send(
                    f"FLY incident [{inc['impact']}]: {inc['name']} "
                    f"(status: {inc['status']})\n{fly_incident_url(inc['id'])}"
                )
            elif prev != inc["status"]:
                send(
                    f"FLY incident [{inc['impact']}]: {inc['name']} "
                    f"({prev} → {inc['status']})\n{fly_incident_url(inc['id'])}"
                )
        for gone in known_incidents.keys() - current.keys():
            send(f"FLY incident resolved: {gone}\n{fly_incident_url(gone)}")
        state["incidents"] = current
        state["fly_fail_streak"] = 0

    state["apps"] = apps_state

    # Failover вебхука команд: симметричный гистерезис (по 2 тика в каждую
    # сторону), флип только со сменой. Одиночный ok посреди дауна —
    # флюк, не повод возвращаться.
    if apps_state.get("prod", "ok") != "ok":
        state["prod_down_streak"] = state.get("prod_down_streak", 0) + 1
        state["prod_up_streak"] = 0
    else:
        state["prod_down_streak"] = 0
        state["prod_up_streak"] = state.get("prod_up_streak", 0) + 1
    if state["prod_down_streak"] >= 2:
        desired = WEBHOOKS["nightly"]
    elif state["prod_up_streak"] >= 2 or "webhook" not in state:
        desired = WEBHOOKS["prod"]
    else:
        desired = state.get("webhook", WEBHOOKS["prod"])
    if state.get("webhook") != desired:
        if set_webhook(desired):
            state["webhook"] = desired
            where = "nightly" if desired == WEBHOOKS["nightly"] else "prod"
            send(f"ops webhook → {where}")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

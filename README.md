# slyrix-ops

Опс-автоматика SlyrixWiki: отдельный репозиторий, отдельный бот, ноль связи с 2FA-ботом.

## Что внутри

- `scripts/watch.py` — вотчер (только stdlib): `/health` прод+найтли, инциденты Fly statuspage API. Орёт в опс-бота только на переходах ok↔bad и на новых/закрытых инцидентах (дедуплика через `STATE_FILE`).
- `.github/workflows/watch.yml` — крон каждые 5 мин.
- `.github/workflows/backup.yml` — бэкап `site.db` с обеих машин по воскресеньям + вручную: артефакты 30 дней + файлы в ТГ (`site-stable-ДАТА.db`, `site-nightly-ДАТА.db`).

## Секреты репозитория (Settings → Secrets → Actions)

- `OPS_BOT_TOKEN` — токен опс-бота из BotFather
- `OPS_CHAT_ID` — chat id (узнать у `@userinfobot`)
- `FLY_API_TOKEN` — токен Fly с доступом к обоим аппам (для бэкапов)

## Локально

```powershell
python scripts/watch.py
```

Токен/чат берутся из env (`OPS_BOT_TOKEN`, `OPS_CHAT_ID`; `BOT_TOKEN` тоже подойдёт как fallback). Без них — dry-run: проверки идут, отправка скипается. Состояние — `.ops_state.json` (в `.gitignore`, не коммитить).

## Sentry — в коде вики

Стектрейсы живут в самом вики (`observability.py`, инициализация в `main.py`):
500-е — всегда (`report_error` в exception handler), из 400-х — только
точечно (`security_event`, level warning): срабатывания IP-автобана,
`ipban_kill` (сессия с забаненного IP), 403 на `/internal` без токена и
попытки раздать/снять admin-роли не-главным. Обычный 400-шум (429-троттлинг,
баны, 404) фильтруется `before_send` — квоту не жрёт.
Без `SENTRY_DSN` в окружении — полный no-op. DSN кладётся через
`fly secrets set SENTRY_DSN=... -a slyrixwiki` (+ nightly).

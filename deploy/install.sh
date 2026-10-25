#!/usr/bin/env bash
# Установка или обновление бота на сервере одной командой:
#   curl -fsSL https://raw.githubusercontent.com/mokspp-alt/personal-assistant-bot/main/deploy/install.sh | BOT_TOKEN=123:abc bash
# Без BOT_TOKEN при первой установке скрипт спросит токен; повторный запуск обновляет
# код и перезапускает только процесс бота. Короткий вариант для ручного набора:
#   curl -L raw.githubusercontent.com/mokspp-alt/personal-assistant-bot/main/i | sh
# Другие проекты на сервере не трогает: своя папка, свой PM2-процесс, без портов.
# Ключ Claude и прокси (api.telegram.org с сервера заблокирован) берёт из SECRETS_FROM,
# если в .env они ещё не заданы. Значения на экран не выводятся.
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/personal-assistant-bot}"
REPO="${REPO:-https://github.com/mokspp-alt/personal-assistant-bot.git}"
SECRETS_FROM="${SECRETS_FROM:-/var/www/empathy-bridge/.env.local}"
NAME="personal-assistant-bot"

echo "==> Python и venv"
if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3-venv
fi

echo "==> Код в $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> Зависимости (первый раз несколько минут)"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Настройки"
mkdir -p data
if [ ! -f .env ]; then
    cp .env.example .env
    # Меньше 2 ГБ свободной памяти — берём модель распознавания полегче.
    avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    if [ "${avail_mb:-0}" -lt 2000 ]; then
        sed -i 's/^WHISPER_MODEL=.*/WHISPER_MODEL=base/' .env
        echo "    Свободно ${avail_mb} МБ памяти — модель Whisper: base"
    fi
fi
if [ -f "$SECRETS_FROM" ]; then
    # Пустые ANTHROPIC_API_KEY, ANTHROPIC_PROXY и TELEGRAM_PROXY заполняем из соседнего проекта.
    python3 - "$SECRETS_FROM" .env <<'PY'
import sys

def read(path):
    values = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values

src, env_path = read(sys.argv[1]), sys.argv[2]
wanted = {"ANTHROPIC_API_KEY": src.get("ANTHROPIC_API_KEY", ""),
          "ANTHROPIC_PROXY": src.get("ANTHROPIC_PROXY_URL", ""),
          "TELEGRAM_PROXY": src.get("ANTHROPIC_PROXY_URL", "")}
lines = open(env_path, encoding="utf-8").read().splitlines()
taken = []
for i, line in enumerate(lines):
    key, sep, value = line.partition("=")
    if sep and key in wanted and not value.strip() and wanted[key]:
        lines[i] = f"{key}={wanted[key]}"
        taken.append(key)
open(env_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
if taken:
    print("    Из", sys.argv[1], "взяты:", ", ".join(taken))
PY
fi
if [ -z "${BOT_TOKEN:-}" ] && grep -q "^BOT_TOKEN=123456:" .env && [ -r /dev/tty ]; then
    printf "Вставьте или наберите токен бота от @BotFather: " > /dev/tty
    read -r BOT_TOKEN < /dev/tty || true
    BOT_TOKEN=$(printf "%s" "$BOT_TOKEN" | tr -d "[:space:]")
fi
if [ -n "${BOT_TOKEN:-}" ]; then
    sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" .env
fi
chmod 600 .env
if grep -q "^BOT_TOKEN=123456:" .env; then
    echo "!! Токен не задан. Запустите ещё раз с BOT_TOKEN=..." >&2
    exit 1
fi
grep -q "^ANTHROPIC_API_KEY=." .env || echo "    Внимание: ANTHROPIC_API_KEY пуст — бот будет работать без Claude"
grep -q "^TELEGRAM_PROXY=." .env || echo "    Внимание: TELEGRAM_PROXY пуст — если api.telegram.org заблокирован, бот не подключится"

echo "==> PM2"
if ! command -v pm2 >/dev/null 2>&1; then
    echo "!! pm2 не найден. Установите: npm install -g pm2" >&2
    exit 1
fi
if pm2 describe "$NAME" >/dev/null 2>&1; then
    pm2 restart "$NAME" --update-env
else
    pm2 start ecosystem.config.js
fi
pm2 save
sleep 5
pm2 logs "$NAME" --lines 20 --nostream
echo "==> Готово. Напишите боту /start — кто напишет первым, тот и владелец."

#!/usr/bin/env bash
# Заливает бота на VPS и запускает как службу.
# Использование:  ./deploy.sh            — спросит адрес сервера
#                 ./deploy.sh root@IP    — сразу на указанный сервер
# Повторный запуск обновляет bot.py/.env и перезапускает бота; users.json на сервере сохраняется.
set -euo pipefail
cd "$(dirname "$0")"

FILES=(bot.py db.py api.py excel_io.py requirements.txt .env server/install.sh)
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "Нет файла $f"; exit 1; }
done
grep -qE '^BOT_TOKEN=[0-9]+:' .env || { echo "В .env не указан BOT_TOKEN"; exit 1; }
grep -qE '^API_URL=https://' .env || { echo "В .env не указан API_URL (адрес сервера, https://…)"; exit 1; }
if grep -qE '^PROXY_URL=.*(tg://|secret=)' .env; then
  echo "В PROXY_URL указан MTProto-прокси (tg://proxy). Боту нужен SOCKS5- или HTTP-прокси."; exit 1
fi
if grep -qE '^PROXY_URL=[^[:space:]]' .env; then
  echo "Прокси: указан в .env"
else
  echo "Прокси: не указан, бот будет подключаться к Telegram напрямую"
fi

HOST="${1:-}"
if [ -z "$HOST" ]; then
  echo
  echo "IP-адрес сервера есть в панели хостинга. Или выполните в консоли сервера:  curl -4 ifconfig.me"
  read -rp "IP-адрес сервера: " IP
  read -rp "Пользователь [root]: " LOGIN
  HOST="${LOGIN:-root}@${IP// /}"
fi
case "$HOST" in
  *@) echo "Не указан IP-адрес сервера"; exit 1 ;;
  *1.2.3.4*) echo "1.2.3.4 — это пример из инструкции. Укажите IP своего сервера."; exit 1 ;;
esac

# Одно SSH-подключение на все шаги — пароль спрашивается один раз
mkdir -p ~/.ssh && chmod 700 ~/.ssh
SSH_OPTS=(-o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new
          -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-%C" -o ControlPersist=300)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$HOST" >/dev/null 2>&1 || true' EXIT

echo
echo "==> Подключаюсь к $HOST (если спросят пароль — это пароль от сервера)"
ssh "${SSH_OPTS[@]}" "$HOST" 'mkdir -p ~/schedule-bot-upload && chmod 700 ~/schedule-bot-upload'

echo "==> Копирую файлы"
scp "${SSH_OPTS[@]}" -q "${FILES[@]}" "$HOST":~/schedule-bot-upload/

echo "==> Устанавливаю на сервере"
ssh "${SSH_OPTS[@]}" -t "$HOST" 'bash ~/schedule-bot-upload/install.sh'

echo
echo "Логи бота:  ssh $HOST 'journalctl -u schedule-bot -f'"

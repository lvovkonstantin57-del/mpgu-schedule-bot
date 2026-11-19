#!/usr/bin/env bash
# Выполняется НА СЕРВЕРЕ (запускает deploy.sh). Ставит бота и API как службу systemd, а Caddy — для HTTPS.
set -euo pipefail

UPLOAD="$HOME/schedule-bot-upload"
APP=/opt/schedule-bot
SERVICE=schedule-bot
RUN_USER=schedbot
FILES=(bot.py db.py api.py excel_io.py requirements.txt)

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
# ждём, если apt занят автообновлениями, и не показываем диалоги (needrestart и т. п.)
APT=($SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get -o DPkg::Lock::Timeout=300 -qq)
APT_UPDATED=""
apt_install() {
  [ -n "$APT_UPDATED" ] || { "${APT[@]}" update; APT_UPDATED=1; }
  "${APT[@]}" install -y "$@"
}

# Домен для HTTPS берётся из API_URL в .env, например https://135-106-163-67.sslip.io
DOMAIN="$(grep -E '^API_URL=' "$UPLOAD/.env" | tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \r' | sed -E 's#^https?://##; s#[/:].*$##')"
[ -n "$DOMAIN" ] || { echo "В .env не указан API_URL"; exit 1; }

echo "==> Пакеты Python"
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  apt_install python3 python3-venv python3-pip
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Нужен Python 3.9+"'

echo "==> Пользователь $RUN_USER и папка $APP"
id "$RUN_USER" >/dev/null 2>&1 || $SUDO useradd --system --home "$APP" --shell /usr/sbin/nologin "$RUN_USER"
$SUDO mkdir -p "$APP"

echo "==> Файлы бота (база data.db на сервере не трогается)"
for f in "${FILES[@]}"; do $SUDO install -m 644 "$UPLOAD/$f" "$APP/$f"; done
$SUDO install -m 600 "$UPLOAD/.env" "$APP/.env"

echo "==> Виртуальное окружение и зависимости"
[ -x "$APP/.venv/bin/python" ] || $SUDO python3 -m venv "$APP/.venv"
$SUDO "$APP/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
$SUDO "$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"
$SUDO chown -R "$RUN_USER:$RUN_USER" "$APP"

echo "==> Есть ли у сервера прямой доступ к Telegram"
if python3 - <<'PY'
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen("https://api.telegram.org/bot/getMe", timeout=8)
except urllib.error.HTTPError:
    pass  # сервер Telegram ответил — значит, доступен
except Exception:
    sys.exit(1)
PY
then
  echo "    есть"
else
  echo "    НЕТ — бот сможет работать только через прокси (PROXY_URL в .env)"
fi

echo "==> Caddy: HTTPS для $DOMAIN"
if ! command -v caddy >/dev/null 2>&1; then
  if ! apt_install caddy 2>/dev/null; then
    echo "    в репозитории системы Caddy нет — подключаю официальный репозиторий"
    apt_install debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | $SUDO gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      | $SUDO tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    APT_UPDATED=""
    apt_install caddy
  fi
fi
BUSY="$($SUDO ss -ltnpH 2>/dev/null | grep -E ':(80|443)\s' | grep -v caddy || true)"
if [ -n "$BUSY" ]; then
  echo "    ВНИМАНИЕ: порты 80/443 заняты другой программой — Caddy не сможет работать:"
  echo "$BUSY" | sed 's/^/    /'
fi
if [ -f /etc/caddy/Caddyfile ] && ! grep -q "schedule-bot" /etc/caddy/Caddyfile; then
  $SUDO cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
fi
$SUDO tee /etc/caddy/Caddyfile >/dev/null <<EOF
# schedule-bot: HTTPS для API мини-приложения (сертификат Caddy получает и продлевает сам)
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
EOF
if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  $SUDO ufw allow 80/tcp >/dev/null
  $SUDO ufw allow 443/tcp >/dev/null
  echo "    в брандмауэре ufw открыты порты 80 и 443"
fi
$SUDO systemctl enable caddy >/dev/null 2>&1
$SUDO systemctl restart caddy

echo "==> Служба бота"
$SUDO tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=Telegram-бот расписания МПГУ
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python $APP/bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
STARTED="$(date '+%Y-%m-%d %H:%M:%S')"
$SUDO systemctl daemon-reload
$SUDO systemctl enable $SERVICE >/dev/null 2>&1
$SUDO systemctl restart $SERVICE

rm -rf "$UPLOAD"
echo "==> Запускаю бота..."
LOG=""
for _ in $(seq 1 40); do
  sleep 1
  LOG="$($SUDO journalctl -u $SERVICE --since "$STARTED" --no-pager -o cat 2>/dev/null || true)"
  grep -qE "API работает|ERROR|Error|Traceback" <<<"$LOG" && break
done
tail -n 12 <<<"$LOG"
BOT_OK=""
grep -q "API работает" <<<"$LOG" && BOT_OK=1

echo "==> Проверяю https://$DOMAIN (первый раз сертификат выдаётся до минуты)"
HTTPS_OK=""
for _ in $(seq 1 18); do
  if python3 - "$DOMAIN" <<'PY'
import sys, urllib.request
try:
    ok = urllib.request.urlopen(f"https://{sys.argv[1]}/health", timeout=8).read() == b"ok"
except Exception:
    ok = False
sys.exit(0 if ok else 1)
PY
  then HTTPS_OK=1; break; fi
  sleep 5
done

echo
if [ -n "$BOT_OK" ] && [ -n "$HTTPS_OK" ]; then
  echo "ГОТОВО: бот и админ-панель работают, https://$DOMAIN отвечает."
  ADMINS="$($SUDO "$APP/.venv/bin/python" -c "import sqlite3; print(sqlite3.connect('$APP/data.db').execute('SELECT COUNT(*) FROM admins').fetchone()[0])" 2>/dev/null || echo 0)"
  if [ "$ADMINS" = "0" ]; then
    echo "Чтобы стать администратором, отправьте боту:  /admin $(grep -E '^ADMIN_CODE=' "$APP/.env" | cut -d= -f2-)"
  fi
elif [ -z "$BOT_OK" ]; then
  echo "Бот не запустился — причина в сообщениях выше."
  echo "Исправьте .env на своём компьютере и снова выполните ./deploy.sh"
else
  echo "Бот работает, но https://$DOMAIN пока не отвечает. Чаще всего закрыты порты 80 и 443:"
  echo "откройте их в панели хостинга (раздел «Сеть», «Firewall» или «Брандмауэр») и снова выполните ./deploy.sh."
  echo "Журнал Caddy:  journalctl -u caddy -n 30 --no-pager"
  $SUDO journalctl -u caddy -n 8 --no-pager -o cat 2>/dev/null | sed 's/^/    /' || true
fi

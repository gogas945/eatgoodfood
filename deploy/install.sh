#!/usr/bin/env bash
# Установка и обновление бота на сервере Ubuntu.
# Первый запуск: спросит токены, установит всё и запустит бота как службу.
# Повторный запуск: обновит код и перезапустит бота, настройки (.env) сохранятся.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/gogas945/eatgoodfood.git}"
BRANCH="${BRANCH:-claude/calorie-tracking-system-ggpgz1}"
APP_DIR=/opt/eatgoodfood

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти с sudo." >&2
  exit 1
fi

ask() {  # ask "Вопрос" [скрыть ввод]
  local answer
  if [ "${2:-}" = "secret" ]; then
    read -r -s -p "$1: " answer < /dev/tty; echo > /dev/tty
  else
    read -r -p "$1: " answer < /dev/tty
  fi
  printf '%s' "$answer"
}

echo "==> Устанавливаю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip > /dev/null

if [ -d "$APP_DIR/.git" ]; then
  echo "==> Обновляю код"
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  echo "==> Скачиваю код"
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Устанавливаю зависимости Python"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  echo
  echo "==> Настройка (вставка текста: долгое нажатие или правый клик)"
  TG=$(ask "Токен бота от @BotFather")
  AK=$(ask "Ключ Claude API (sk-ant-..., при вставке символы не видны)" secret)
  UID_=$(ask "Твой Telegram ID (узнать у @userinfobot; можно оставить пустым)")
  TZ_=$(ask "Часовой пояс [Europe/Moscow]")
  TZ_=${TZ_:-Europe/Moscow}
  umask 077
  cat > "$APP_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=$TG
ANTHROPIC_API_KEY=$AK
ALLOWED_USER_ID=$UID_
TIMEZONE=$TZ_
DB_PATH=$APP_DIR/food.db
CLAUDE_MODEL=claude-opus-5-5
EOF
  chmod 600 "$APP_DIR/.env"
fi

echo "==> Настраиваю автозапуск"
cat > /etc/systemd/system/eatgoodfood.service <<EOF
[Unit]
Description=eatgoodfood Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q eatgoodfood
systemctl restart eatgoodfood
sleep 5

if systemctl is-active -q eatgoodfood; then
  echo
  echo "✅ Бот запущен. Напиши ему в Telegram /start."
  echo "Логи:        sudo journalctl -u eatgoodfood -f"
  echo "Настройки:   sudo nano $APP_DIR/.env  (потом: sudo systemctl restart eatgoodfood)"
  echo "Обновление:  sudo bash $APP_DIR/deploy/install.sh"
else
  echo "❌ Бот не запустился. Последние строки лога:"
  journalctl -u eatgoodfood -n 30 --no-pager
  exit 1
fi

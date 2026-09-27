#!/bin/bash
# Установка мониторинга тендеров на Raspberry Pi (Raspberry Pi OS 12 Bookworm и новее).
# Запуск из папки со скриптом:  bash install_pi.sh
# Скрипт ставит библиотеки, скачивает сертификат Минцифры и делает службу,
# которая стартует сама при включении Raspberry Pi и работает как Telegram-бот.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "== Устанавливаю Python-окружение"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== Сертификат Минцифры для ЕИС"
.venv/bin/python tender_monitor.py --setup-cert || echo "Сертификат не скачался, ЕИС работать не будет. Повторите: .venv/bin/python tender_monitor.py --setup-cert"

if grep -q '^bot_token = ""' config.toml; then
  echo
  echo "Впишите токен бота в config.toml (строка bot_token), например командой: nano config.toml"
  echo "Потом напишите боту в Telegram любое сообщение и снова запустите: bash install_pi.sh"
  exit 1
fi
if grep -q '^chat_id = ""' config.toml; then
  echo "== Ищу chat_id"
  .venv/bin/python tender_monitor.py --get-chat-id || {
    echo "Напишите боту в Telegram любое сообщение и снова запустите: bash install_pi.sh"; exit 1; }
fi

chmod 600 config.toml  # в нём токен бота: читать может только этот пользователь

echo "== Создаю службу tender-monitor"
sudo tee /etc/systemd/system/tender-monitor.service >/dev/null <<UNIT
[Unit]
Description=Мониторинг тендеров (Telegram-бот)
# ждём сеть и синхронизацию часов: у Raspberry Pi без батарейки часы после включения неточные
After=network-online.target time-sync.target
Wants=network-online.target time-sync.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/tender_monitor.py --bot
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now tender-monitor
sleep 3
systemctl --no-pager --lines=5 status tender-monitor || true
echo
echo "Готово. Бот написал в Telegram, что запущен. Команды — /help."
echo "Журнал: tail -f $DIR/tender_monitor.log   Остановить: sudo systemctl stop tender-monitor"

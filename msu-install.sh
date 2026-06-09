#!/bin/bash

set -e

MAUTIC_PATH="${1:-/var/www/html}"
STORAGE_PATH="${2:-/root/msu-backups}"

echo "Instalando Atualizador Mautic..."

apt update
apt install -y curl python3 python3-pip mysql-client

curl -fsSL -o /tmp/update.py https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main/update.py

chmod +x /tmp/update.py

echo "1/4 - Diagnóstico..."
python3 /tmp/update.py --path "$MAUTIC_PATH" diagnose

echo "2/4 - Backup local..."
python3 /tmp/update.py --path "$MAUTIC_PATH" backup \
  --storage "$STORAGE_PATH" \
  --confirm "CONFIRMO QUE FIZ BACKUP DO SERVIDOR"

echo "3/4 - Plano de upgrade..."
python3 /tmp/update.py --path "$MAUTIC_PATH" plan --target "5.2-lts"

echo "4/4 - Simulação do upgrade..."
python3 /tmp/update.py --path "$MAUTIC_PATH" upgrade \
  --confirm "CONFIRMO UPGRADE"

echo "Finalizado."
echo "Para executar de verdade, rode:"
echo "python3 /tmp/update.py --path \"$MAUTIC_PATH\" upgrade --confirm \"CONFIRMO UPGRADE\" --execute"

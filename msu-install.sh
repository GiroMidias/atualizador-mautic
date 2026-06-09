#!/bin/bash

set -e

echo "Instalando Atualizador Mautic..."

apt update
apt install -y curl python3 python3-pip mysql-client

echo "Baixando update.py..."
curl -fsSL -o /tmp/update.py https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main/update.py
chmod +x /tmp/update.py

echo "Procurando instalação do Mautic..."

MAUTIC_PATH=$(find /var/www /home /srv /opt -type f -path "*/bin/console" 2>/dev/null | while read console; do
  dir=$(dirname "$(dirname "$console")")
  if [ -f "$dir/app/config/local.php" ] || [ -f "$dir/config/local.php" ] || [ -f "$dir/composer.json" ]; then
    if php "$console" mautic:version >/dev/null 2>&1; then
      echo "$dir"
      break
    fi
  fi
done)

if [ -z "$MAUTIC_PATH" ]; then
  echo "Não encontrei o Mautic automaticamente."
  echo "Rode informando o caminho manualmente:"
  echo "bash <(curl -fsSL https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main/msu-install.sh) /caminho/do/mautic"
  exit 1
fi

echo "Mautic encontrado em: $MAUTIC_PATH"

STORAGE_PATH="/root/msu-backups"

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
echo "Para executar de verdade:"
echo "python3 /tmp/update.py --path \"$MAUTIC_PATH\" upgrade --confirm \"CONFIRMO UPGRADE\" --execute"

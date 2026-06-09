#!/bin/bash

set -e

echo "Instalando Atualizador Mautic..."

apt update
apt install -y curl python3 python3-pip mysql-client docker.io

echo "Baixando update.py..."
curl -fsSL -o /tmp/update.py https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main/update.py
chmod +x /tmp/update.py

echo "Detectando tipo de instalação..."

INSTALL_TYPE=""
MAUTIC_PATH=""
MAUTIC_CONTAINER=""

echo "Procurando instalação direta..."

MAUTIC_PATH=$(find /var/www /home /srv /opt -type f -path "*/bin/console" 2>/dev/null | while read console; do
  dir=$(dirname "$(dirname "$console")")
  if php "$console" mautic:version >/dev/null 2>&1; then
    echo "$dir"
    break
  fi
done)

if [ -n "$MAUTIC_PATH" ]; then
  INSTALL_TYPE="direct"
fi

if [ -z "$INSTALL_TYPE" ]; then
  echo "Procurando instalação Docker..."

  MAUTIC_CONTAINER=$(docker ps --format '{{.ID}} {{.Image}} {{.Names}}' | grep -i mautic | head -n 1 | awk '{print $1}')

  if [ -n "$MAUTIC_CONTAINER" ]; then
    INSTALL_TYPE="docker"

    MAUTIC_PATH=$(docker exec "$MAUTIC_CONTAINER" sh -c '
      for path in /var/www/html /var/www/html/docroot /app /srv/app /var/www/mautic; do
        if [ -f "$path/bin/console" ]; then
          echo "$path"
          exit 0
        fi
      done

      find / -type f -path "*/bin/console" 2>/dev/null | while read console; do
        dir=$(dirname "$(dirname "$console")")
        if php "$console" mautic:version >/dev/null 2>&1; then
          echo "$dir"
          exit 0
        fi
      done
    ')
  fi
fi

if [ -z "$INSTALL_TYPE" ]; then
  echo "Não consegui encontrar o Mautic."
  echo "Verifique se o Mautic está rodando e tente novamente."
  exit 1
fi

echo "Tipo detectado: $INSTALL_TYPE"
echo "Caminho detectado: $MAUTIC_PATH"

if [ "$INSTALL_TYPE" = "direct" ]; then
  STORAGE_PATH="/root/msu-backups"

  python3 /tmp/update.py --path "$MAUTIC_PATH" diagnose

  python3 /tmp/update.py --path "$MAUTIC_PATH" backup \
    --storage "$STORAGE_PATH" \
    --confirm "CONFIRMO QUE FIZ BACKUP DO SERVIDOR"

  python3 /tmp/update.py --path "$MAUTIC_PATH" plan --target "5.2-lts"

  python3 /tmp/update.py --path "$MAUTIC_PATH" upgrade \
    --confirm "CONFIRMO UPGRADE"

fi

if [ "$INSTALL_TYPE" = "docker" ]; then
  echo "Executando diagnóstico dentro do container $MAUTIC_CONTAINER..."

  docker cp /tmp/update.py "$MAUTIC_CONTAINER:/tmp/update.py"

  docker exec "$MAUTIC_CONTAINER" chmod +x /tmp/update.py

  docker exec "$MAUTIC_CONTAINER" python3 /tmp/update.py --path "$MAUTIC_PATH" diagnose

  docker exec "$MAUTIC_CONTAINER" python3 /tmp/update.py --path "$MAUTIC_PATH" plan --target "5.2-lts"

  docker exec "$MAUTIC_CONTAINER" python3 /tmp/update.py --path "$MAUTIC_PATH" upgrade \
    --confirm "CONFIRMO UPGRADE"

fi

echo "Finalizado."

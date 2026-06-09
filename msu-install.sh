#!/bin/bash

set -e

echo "Baixando Atualizador Mautic..."

curl -fsSL -o /tmp/update.py https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main/update.py

chmod +x /tmp/update.py

echo "Executando upgrade..."

python3 /tmp/update.py upgrade

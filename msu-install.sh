#!/bin/bash

set -e

echo "Instalando Atualizador Mautic..."

sudo apt update
sudo apt install -y python3 python3-pip curl

curl -fsSL -o /tmp/update.py https://raw.githubusercontent.com/GiroMidias/atualizador-mautic/main/update.py

chmod +x /tmp/update.py

sudo python3 /tmp/update.py

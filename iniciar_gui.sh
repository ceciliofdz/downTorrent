#!/usr/bin/env sh
# Abre la interfaz gráfica con el Python del entorno virtual si existe.
set -eu
cd "$(dirname "$0")"

if [ -x "venv/bin/python" ]; then
    exec "venv/bin/python" downtorrent_gui.py
fi

exec python3 downtorrent_gui.py

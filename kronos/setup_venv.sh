#!/usr/bin/env bash
# kronos/setup_venv.sh - Automated virtualenv setup for Kronos
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$HERE"

echo "========================================================"
echo " Setting up Kronos Dedicated Virtual Environment (venv) "
echo "======================================================="

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is not installed or not in PATH."
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment in kronos/venv..."
    python3 -m venv venv
else
    echo "Existing kronos/venv found."
fi

source venv/bin/activate
pip install --upgrade pip

echo "Installing requirements from kronos/requirements.txt..."
pip install -r requirements.txt

mkdir -p logs

echo "======================================================="
echo " Kronos virtualenv setup complete! "
echo " Python binary: $(which python) "
echo "======================================================="

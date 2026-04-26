#!/bin/bash

mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
WINEPREFIX='/config/.wine'
WINEDEBUG='-all'
wine_executable="wine"
metatrader_version="5.0.37"
MT5_CMD_OPTIONS="${MT5_CMD_OPTIONS:-}"
mono_url="https://dl.winehq.org/wine/wine-mono/10.3.0/wine-mono-10.3.0-x86.msi"
python_url="https://www.python.org/ftp/python/3.9.13/python-3.9.13.exe"
mt5setup_url="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"

show_message() { echo "$1"; }

check_dependency() {
    if ! command -v "$1" &> /dev/null; then
        echo "$1 is not installed."
        exit 1
    fi
}

is_wine_python_package_installed() {
    $wine_executable python -c "import pkg_resources; exit(not pkg_resources.require('$1'))" 2>/dev/null
    return $?
}

check_dependency "curl"
check_dependency "$wine_executable"

# ── [1/6] Mono ────────────────────────────────────────────────────
if [ ! -e "/config/.wine/drive_c/windows/mono" ]; then
    show_message "[1/6] Downloading and installing Mono..."
    curl -o /config/.wine/drive_c/mono.msi "$mono_url"
    WINEDLLOVERRIDES=mscoree=d $wine_executable msiexec /i /config/.wine/drive_c/mono.msi /qn
    rm -f /config/.wine/drive_c/mono.msi
    show_message "[1/6] Mono installed."
else
    show_message "[1/6] Mono already installed."
fi

# ── [2/6] MetaTrader 5 ────────────────────────────────────────────
if [ -e "$mt5file" ]; then
    show_message "[2/6] MT5 already installed."
else
    show_message "[2/6] Installing MetaTrader 5..."
    $wine_executable reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d "win10" /f
    curl -o /config/.wine/drive_c/mt5setup.exe "$mt5setup_url"
    $wine_executable "/config/.wine/drive_c/mt5setup.exe" "/auto"
    sleep 15
    rm -f /config/.wine/drive_c/mt5setup.exe
fi

# ── [3/6] Launch MT5 terminal ─────────────────────────────────────
if [ -e "$mt5file" ]; then
    show_message "[3/6] Launching MT5 terminal..."
    $wine_executable start /unix "$mt5file" $MT5_CMD_OPTIONS &
    show_message "[3/6] MT5 launched (PID $!)."
else
    show_message "[3/6] ERROR: MT5 binary not found — cannot continue."
    exit 1
fi

# ── [4/6] Wine Python ─────────────────────────────────────────────
if ! $wine_executable python --version 2>/dev/null; then
    show_message "[4/6] Installing Python 3.9 in Wine..."
    curl -L "$python_url" -o /tmp/python-installer.exe
    $wine_executable /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
    rm /tmp/python-installer.exe
    show_message "[4/6] Python installed."
else
    show_message "[4/6] Wine Python already installed."
fi

# ── [5/6] MetaTrader5 Python library ─────────────────────────────
show_message "[5/6] Checking Python packages..."
$wine_executable python -m pip install --upgrade --no-cache-dir pip --quiet

if ! is_wine_python_package_installed "MetaTrader5==$metatrader_version"; then
    show_message "[5/6] Installing MetaTrader5==$metatrader_version..."
    $wine_executable python -m pip install --no-cache-dir "MetaTrader5==$metatrader_version"
fi

# MetaTrader5 5.0.45 supports NumPy 1.x; keep the pin as a safety measure.
# Remove this line only if a future MT5 version explicitly requires NumPy 2.x.
show_message "[5/6] Pinning numpy<2 for stability..."
$wine_executable python -m pip install --no-cache-dir "numpy<2" --quiet


# ── [6/6] Bot dependencies ────────────────────────────────────────
# The bot runs inside Wine Python so ALL deps must be installed here.
# requests + tzdata are the only non-MT5 deps in requirements.txt.
show_message "[6/6] Installing bot dependencies in Wine Python..."
$wine_executable python -m pip install --no-cache-dir --quiet requests tzdata flask

# Auto-install anything in requirements.txt so adding a new dep
# is as simple as adding a line there — no rebuild needed.
if [ -f /bot/src/requirements.txt ]; then
    show_message "[6/6] Installing src/requirements.txt in Wine Python..."
    $wine_executable python -m pip install --no-cache-dir -r /bot/src/requirements.txt
fi

# ── FIRST-RUN INSTRUCTIONS ────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Zone Bot — running INSIDE Wine alongside MT5"
echo "  (direct MetaTrader5 API — zero bridge latency)"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  ⚠️  One-time setup (first run only):"
echo "  1. Open http://localhost:3000 in your browser"
echo "  2. Log in to your broker account inside MT5"
echo "  3. Tools → Options → Expert Advisors"
echo "     → ✅ Allow Algorithmic Trading"
echo "  4. When ready, run:"
echo "     docker exec mt5 touch /bot/ready_to_trade"
echo ""

while [ ! -f /bot/ready_to_trade ]; do
    sleep 5
done

# ── START BOT ─────────────────────────────────────────────────────
# Wine reports os.name == "nt" so mt5_gateway.py uses direct
# MetaTrader5 import — no RPyC bridge, no latency.
echo "  🚀 Starting bot in Wine Python..."
# PYTHONUTF8=1 forces UTF-8 everywhere.
# Without it Wine Python defaults to cp1252 and crashes on any
# Unicode character (e.g. the ━ box-drawing chars in main.py logs).
cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
    $wine_executable python -m src.main
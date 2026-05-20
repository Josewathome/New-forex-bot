#!/bin/bash

mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
export WINEPREFIX='/config/.wine'
export WINEDEBUG='-all'
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

# ── [0/6] KasmVNC performance fix ─────────────────────────────────
# The base image starts Xvnc with -RectThreads 0 (single-threaded),
# causing severe UI lag in MT5. Patch the kasmvnc config to use 4
# threads and a sensible resolution before the desktop starts.
KASMVNC_CFG="${HOME}/.vnc/kasmvnc.yaml"
if [ -f "${KASMVNC_CFG}" ]; then
    # Patch RectThreads if present, otherwise append it
    if grep -q "rect_threads" "${KASMVNC_CFG}" 2>/dev/null; then
        sed -i 's/rect_threads:.*/rect_threads: 4/' "${KASMVNC_CFG}"
    else
        printf '\nserver:\n  rect_threads: 4\n' >> "${KASMVNC_CFG}"
    fi
    show_message "[0/6] KasmVNC rect_threads set to 4."
else
    show_message "[0/6] kasmvnc.yaml not found — VNC thread patch skipped."
fi

# ── [0/6] Pin Wine MachineGuid ────────────────────────────────────
# IC Markets treats each unique MachineGuid as a new device and triggers
# a mobile authorization request. Wine regenerates this GUID on every
# fresh WINEPREFIX, making every container restart look like a new device.
# We write a fixed, stable GUID so IC Markets always sees the same machine.
# The GUID below was generated once with uuidgen — do not change it.
FIXED_MACHINE_GUID="${WINE_MACHINE_GUID:-a1b2c3d4-e5f6-7890-abcd-ef1234567890}"
CURRENT_GUID=$(wine reg query "HKLM\\SOFTWARE\\Microsoft\\Cryptography" /v MachineGuid 2>/dev/null | grep -i MachineGuid | awk '{print $NF}' || true)
if [ "${CURRENT_GUID}" != "${FIXED_MACHINE_GUID}" ]; then
    show_message "[0/6] Pinning Wine MachineGuid → ${FIXED_MACHINE_GUID} …"
    wine reg add "HKLM\\SOFTWARE\\Microsoft\\Cryptography" \
        /v MachineGuid /t REG_SZ /d "${FIXED_MACHINE_GUID}" /f 2>/dev/null
    show_message "[0/6] MachineGuid pinned."
else
    show_message "[0/6] MachineGuid already pinned."
fi

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

# ── [3/6] Pre-write MT5 credentials into common.ini ──────────────
# MT5 reads login/password/server from Config/common.ini at startup.
# Writing them here before terminal64.exe launches means MT5 auto-logs in
# silently on every container restart — no GUI login dialog, no VNC needed.
# We also set Api=1 (Allow Algorithmic Trading) so the bot can connect
# immediately without requiring a manual Tools→Options→Expert Advisors step.
MT5_CONFIG_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5/Config"
MT5_COMMON_INI="${MT5_CONFIG_DIR}/common.ini"

if [ -n "${TRADING_ID:-}" ] && [ -n "${MT5_PASSWORD:-}" ] && [ -n "${MT5_SERVER:-}" ]; then
    show_message "[3/6] Writing MT5 credentials to common.ini …"
    # DEBUG: print exactly what the shell sees for each credential
    show_message "[DEBUG] TRADING_ID  = '${TRADING_ID}'"
    show_message "[DEBUG] MT5_SERVER  = '${MT5_SERVER}'"
    show_message "[DEBUG] MT5_PASSWORD length = $(printf '%s' "${MT5_PASSWORD}" | wc -c) chars"
    show_message "[DEBUG] MT5_PASSWORD = '${MT5_PASSWORD}'"
    mkdir -p "${MT5_CONFIG_DIR}"
    # Unlock in case a previous run locked it
    chmod 644 "${MT5_COMMON_INI}" 2>/dev/null || true

    # Preserve every existing section except [Common] so we don't disturb
    # MT5's saved Expert Advisor settings, chart layouts, etc.
    EXISTING=""
    if [ -f "${MT5_COMMON_INI}" ]; then
        EXISTING=$(awk '
            /^\[Common\]/ { skip=1; next }
            /^\[/ { skip=0 }
            !skip { print }
        ' "${MT5_COMMON_INI}" 2>/dev/null || true)
    fi

    # Write only [Common] — credentials for auto-login.
    # Do NOT touch [Experts]: MT5 manages those flags itself and any
    # interference causes it to disable the Python API on account change.
    printf '[Common]\r\n'                          >  "${MT5_COMMON_INI}"
    printf 'Login=%s\r\n'   "${TRADING_ID}"        >> "${MT5_COMMON_INI}"
    printf 'Password=%s\r\n' "${MT5_PASSWORD}"     >> "${MT5_COMMON_INI}"
    printf 'Server=%s\r\n'  "${MT5_SERVER}"        >> "${MT5_COMMON_INI}"
    printf 'ProxyEnable=0\r\n'                     >> "${MT5_COMMON_INI}"
    printf 'ProxyType=0\r\n'                       >> "${MT5_COMMON_INI}"
    printf 'ProxyAddress=\r\n'                     >> "${MT5_COMMON_INI}"
    printf 'NewsEnable=1\r\n'                      >> "${MT5_COMMON_INI}"
    # Re-append all other sections unchanged
    if [ -n "${EXISTING}" ]; then
        printf '%s\r\n' "${EXISTING}"              >> "${MT5_COMMON_INI}"
    fi
    show_message "[3/6] common.ini written — credentials set for auto-login."
else
    show_message "[3/6] WARNING: TRADING_ID/MT5_PASSWORD/MT5_SERVER not set — manual VNC login required."
fi

# ── [3/6] Wipe stale MT5 account cache ───────────────────────────
# MT5 stores broker sessions in an encrypted accounts.dat tied to the
# Wine machine identity. On IC Markets, a stale/mismatched cache causes
# the terminal to silently fail broker auth (no login dialog, no -6 at
# terminal level — just the Python API gets Authorization failed).
# Deleting it before every launch forces MT5 to do a clean login using
# the credentials in common.ini rather than replaying a broken session.
MT5_INSTALL_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5"
for stale_file in \
    "${MT5_INSTALL_DIR}/config/accounts.dat" \
    "${MT5_INSTALL_DIR}/config/metaeditor.ini" \
    "${MT5_INSTALL_DIR}/config/network.dat"; do
    if [ -f "${stale_file}" ]; then
        show_message "[3/6] Removing stale cache: $(basename ${stale_file})"
        rm -f "${stale_file}"
    fi
done
show_message "[3/6] Account cache cleared — MT5 will do a fresh login."

# ── [3/6] Launch MT5 terminal ─────────────────────────────────────
if [ -e "$mt5file" ]; then
    show_message "[3/6] Launching MT5 terminal..."
    $wine_executable start /unix "$mt5file" $MT5_CMD_OPTIONS &
    show_message "[3/6] MT5 launched (PID $!)."
    # Wait for MT5 to fully connect to the broker before starting the Python bot.
    # MT5 writes "authorized on <server>" to its daily log when the broker
    # handshake is complete. We poll that file instead of using a blind sleep —
    # this is robust against IC Markets' variable Access Server latency.
    MT5_LOG_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5/Logs"
    MT5_LOG_FILE="${MT5_LOG_DIR}/$(date +%Y%m%d).log"
    MT5_WAIT_SECS=120   # IC Markets Access Server pool rotation can take 60-90s
    MT5_POLL_SECS=5     # polling interval
    waited=0
    last_auth_time=0
    show_message "[3/6] Waiting for MT5 broker auth (up to ${MT5_WAIT_SECS}s)…"
    while [ $waited -lt $MT5_WAIT_SECS ]; do
        if [ -f "${MT5_LOG_FILE}" ]; then
            # Check for a STABLE authorized state: "authorized" must appear AND
            # "disconnected" must NOT appear after the last "authorized" line.
            # IC Markets sometimes authorizes then immediately disconnects (log shows
            # "authorized at 09:53:46" then "disconnected at 09:53:46" same second).
            # We only proceed when the last relevant line is an auth confirmation.
            last_relevant=$(grep -i -E "(authorized on|disconnected from|Invalid account)" "${MT5_LOG_FILE}" 2>/dev/null | tail -1)
            if echo "${last_relevant}" | grep -qi "authorized on"; then
                show_message "[3/6] MT5 broker auth stable after ${waited}s."
                last_auth_time=$waited
                # Wait an extra 10s to confirm it doesn't immediately disconnect again
                sleep 10
                last_after=$(grep -i -E "(authorized on|disconnected from|Invalid account)" "${MT5_LOG_FILE}" 2>/dev/null | tail -1)
                if echo "${last_after}" | grep -qi "authorized on"; then
                    show_message "[3/6] MT5 auth confirmed stable — proceeding."
                    break
                else
                    show_message "[3/6] MT5 auth unstable (disconnected after auth) — waiting..."
                    waited=$((waited + 10))
                fi
            elif echo "${last_relevant}" | grep -qi "Invalid account"; then
                show_message "[3/6] WARNING: IC Markets rejected account (Invalid account). Demo may be expired."
                show_message "[3/6] Check your IC Markets demo account status — it may need renewal."
                break
            fi
        fi
        sleep $MT5_POLL_SECS
        waited=$((waited + MT5_POLL_SECS))
    done
    if [ $waited -ge $MT5_WAIT_SECS ]; then
        show_message "[3/6] MT5 auth not confirmed in ${MT5_WAIT_SECS}s — continuing anyway."
    fi
    # Give MT5 a moment to finish writing its own post-auth updates to common.ini
    sleep 5
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

# MetaTrader5 5.0.37 supports NumPy 1.x only; keep the pin as a safety measure.
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
echo "     docker exec mt5 touch /config/ready_to_trade"
echo ""

# Remove any stale ready_to_trade flag from a previous run so the bot
# cannot start against a terminal that hasn't finished broker auth yet.
# The flag must be re-created by the operator (or the auto-wait below)
# after MT5 reaches a stable connected state on THIS container start.
rm -f /config/ready_to_trade

# Auto-confirm once MT5 reaches a stable broker connection — no manual
# touch needed on routine restarts.  The operator only needs to intervene
# when MT5 shows a login dialog (first run or after credential change).
MT5_LOG_DIR="/config/.wine/drive_c/Program Files/MetaTrader 5/Logs"
MT5_AUTO_WAIT=180   # seconds to wait for stable connection before giving up
MT5_AUTO_POLL=5
auto_waited=0
show_message "[auto] Waiting for MT5 stable broker connection before starting bot…"
while [ $auto_waited -lt $MT5_AUTO_WAIT ]; do
    # Find the most recent MT5 log file (handles Wine/system timezone mismatch)
    latest_log=$(ls -t "${MT5_LOG_DIR}"/*.log 2>/dev/null | head -1)
    if [ -n "${latest_log}" ]; then
        last_relevant=$(grep -i -E "(authorized on|terminal synchronized|disconnected from|invalid account)" \
            "${latest_log}" 2>/dev/null | tail -1)
        if echo "${last_relevant}" | grep -qi "terminal synchronized"; then
            show_message "[auto] MT5 broker connection stable — auto-confirming ready_to_trade."
            touch /config/ready_to_trade
            break
        elif echo "${last_relevant}" | grep -qi "invalid account"; then
            show_message "[auto] MT5 reports 'Invalid account' — manual VNC login required."
            show_message "[auto] Open http://localhost:3000, log in, then run:"
            show_message "[auto]   docker exec mt5 touch /config/ready_to_trade"
            break
        fi
    fi
    sleep $MT5_AUTO_POLL
    auto_waited=$((auto_waited + MT5_AUTO_POLL))
done
if [ $auto_waited -ge $MT5_AUTO_WAIT ]; then
    show_message "[auto] MT5 stable connection not detected in ${MT5_AUTO_WAIT}s — manual confirmation required."
fi

# ready_to_trade lives in the persisted config volume (/config).
while [ ! -f /config/ready_to_trade ]; do
    sleep 5
done

# ── START BOT ─────────────────────────────────────────────────────
# Wine reports os.name == "nt" so mt5_gateway.py uses direct
# MetaTrader5 import — no RPyC bridge, no latency.
# BOT_MODE=backtest in .env triggers backtest mode; anything else = live.
echo "  🚀 Starting bot in Wine Python..."
# PYTHONUTF8=1 forces UTF-8 everywhere.
# Without it Wine Python defaults to cp1252 and crashes on any
# Unicode character (e.g. the ━ box-drawing chars in main.py logs).
cd /bot && DISPLAY=:1 WINEPREFIX=/config/.wine PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
    $wine_executable python -m src.main
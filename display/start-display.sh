#!/usr/bin/env bash
# start-display.sh — Launch the DnD cinematic display companion
#
# Usage:
#   bash start-display.sh              # localhost only, HTTP (default)
#   bash start-display.sh --lan        # trusted-LAN mode, HTTP
#   bash start-display.sh --lan --tls  # trusted-LAN mode, HTTPS after trusted cert bootstrap
#
# LAN exposure assumes a network whose members you trust. TLS encrypts traffic,
# but downloading its CA over HTTP does not authenticate a hostile network.

DISPLAY_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DISPLAY_DIR/app.log"
PID_FILE="$DISPLAY_DIR/app.pid"
CERT_SERVER_PID="$DISPLAY_DIR/.cert-server.pid"

process_identity() {
  python3 "$DISPLAY_DIR/cert_server.py" --parent-pid "$1" --print-process-identity 2>/dev/null
}

write_pid_record() {
  local file="$1" pid="$2" identity="$3"
  printf '%s\n' "$pid" > "$file"
  printf '%s\n' "$identity" > "${file}.identity"
}

owned_pid_from_file() {
  local file="$1" marker="$2" pid="" recorded_identity="" current_identity="" command=""
  [[ -f "$file" && -f "${file}.identity" ]] || return 1
  read -r pid < "$file" || return 1
  read -r recorded_identity < "${file}.identity" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -n "$recorded_identity" ]] || return 1
  current_identity=$(process_identity "$pid") || return 1
  [[ "$current_identity" == "$recorded_identity" ]] || return 1
  command=$(ps -p "$pid" -o command= 2>/dev/null) || return 1
  [[ " $command " == *" $marker "* ]] || return 1
  printf '%s\n' "$pid"
}

stop_owned_pid_file() {
  local file="$1" marker="$2" pid="" current="" attempt=""
  local attempts="${GM_DISPLAY_STOP_ATTEMPTS:-40}"
  local interval="${GM_DISPLAY_STOP_INTERVAL:-0.05}"
  [[ -f "$file" ]] || return 0
  if ! pid=$(owned_pid_from_file "$file" "$marker"); then
    # The record is stale or incomplete. The ownership check above is what
    # makes discarding it safe; never signal the numeric PID from the file.
    rm -f "$file" "${file}.identity"
    return 0
  fi

  kill -TERM "$pid" 2>/dev/null || true
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$file" "${file}.identity"
      return 0
    fi
    current=$(owned_pid_from_file "$file" "$marker" 2>/dev/null) || {
      # The PID was reused while waiting. Drop only our stale ownership record.
      rm -f "$file" "${file}.identity"
      return 0
    }
    [[ "$current" == "$pid" ]] || return 1
    sleep "$interval"
  done

  # Escalate only after revalidating the exact process identity and command.
  current=$(owned_pid_from_file "$file" "$marker" 2>/dev/null) || {
    rm -f "$file" "${file}.identity"
    return 0
  }
  [[ "$current" == "$pid" ]] || return 1
  kill -KILL "$pid" 2>/dev/null || true
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$file" "${file}.identity"
      return 0
    fi
    current=$(owned_pid_from_file "$file" "$marker" 2>/dev/null) || {
      rm -f "$file" "${file}.identity"
      return 0
    }
    [[ "$current" == "$pid" ]] || return 1
    sleep "$interval"
  done
  return 1
}

probe_owned_display() {
  local url="$1" body=""
  owned_pid_from_file "$PID_FILE" "$DISPLAY_DIR/gm-display-app.py" >/dev/null || return 1
  body=$(curl -fskS --connect-timeout 1 --max-time 2 "$url/ping") || return 1
  [[ "$body" == "ok" ]]
}

# Executable tests source the ownership/readiness functions without launching.
if [[ "${GM_DISPLAY_LIB_ONLY:-}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

if ! DISPLAY_PORT=$(python3 "$DISPLAY_DIR/display_config.py"); then
  exit 2
fi
CERT_PORT="${GM_CERT_PORT:-8080}"
if [[ ! "$CERT_PORT" =~ ^[0-9]+$ ]] || (( CERT_PORT < 1 || CERT_PORT > 65535 )); then
  echo "Error: GM_CERT_PORT must be an integer from 1 to 65535"
  exit 2
fi
if (( CERT_PORT == DISPLAY_PORT )); then
  echo "Error: GM_CERT_PORT must differ from the display port"
  exit 2
fi

# ── Parse flags ───────────────────────────────────────────────────────────────
LAN_FLAG=""
TLS_MODE=false

for arg in "$@"; do
  case "$arg" in
    --lan) LAN_FLAG="--lan" ;;
    --tls) TLS_MODE=true ;;
  esac
done

if $TLS_MODE && [[ -z "$LAN_FLAG" ]]; then
  echo "Error: --tls requires --lan (TLS is only meaningful for network access)"
  exit 1
fi

# ── Get LAN IP ────────────────────────────────────────────────────────────────
LAN_IP=""
if [[ -n "$LAN_FLAG" ]]; then
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null \
        || ipconfig getifaddr en1 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}')
fi

# ── TLS: generate cert if missing, then start cert server ────────────────────
if $TLS_MODE; then
  if [[ -L "$DISPLAY_DIR/cert.pem" || -L "$DISPLAY_DIR/key.pem" ]]; then
    echo "Error: certificate and private key must not be symlinks"
    exit 1
  fi
  if [[ ! -f "$DISPLAY_DIR/cert.pem" || ! -f "$DISPLAY_DIR/key.pem" ]]; then
    if ! command -v openssl >/dev/null 2>&1; then
      echo "Error: openssl is required for --tls"
      exit 1
    fi
    echo "Generating self-signed certificate..."
    NEW_CERT="$DISPLAY_DIR/.cert.pem.new.$$"
    NEW_KEY="$DISPLAY_DIR/.key.pem.new.$$"
    rm -f "$NEW_CERT" "$NEW_KEY"
    openssl req -x509 -newkey rsa:2048 \
      -keyout "$NEW_KEY" \
      -out    "$NEW_CERT" \
      -days 3650 -nodes \
      -subj "/CN=dnd-display" \
      -addext "subjectAltName=IP:${LAN_IP:-127.0.0.1},IP:127.0.0.1" 2>/dev/null \
    || {
      rm -f "$NEW_CERT" "$NEW_KEY"
      echo "Error: certificate generation failed. OpenSSL must support the -addext subjectAltName option; configure cert.pem/key.pem manually or upgrade OpenSSL."
      exit 1
    }
    chmod 600 "$NEW_KEY" && chmod 644 "$NEW_CERT" \
      || { rm -f "$NEW_CERT" "$NEW_KEY"; echo "Error: cannot secure generated TLS files"; exit 1; }
    if ! python3 "$DISPLAY_DIR/cert_server.py" --validate-only --cert "$NEW_CERT" --key "$NEW_KEY"; then
      rm -f "$NEW_CERT" "$NEW_KEY"
      echo "Error: generated certificate/key validation failed"
      exit 1
    fi
    mv -f "$NEW_KEY" "$DISPLAY_DIR/key.pem"
    mv -f "$NEW_CERT" "$DISPLAY_DIR/cert.pem"
    echo "Certificate generated (valid 10 years)."
  fi
  chmod 600 "$DISPLAY_DIR/key.pem" || { echo "Error: cannot secure key.pem permissions"; exit 1; }
  chmod 644 "$DISPLAY_DIR/cert.pem" || { echo "Error: cannot set cert.pem permissions"; exit 1; }
  if ! python3 "$DISPLAY_DIR/cert_server.py" --validate-only \
      --cert "$DISPLAY_DIR/cert.pem" --key "$DISPLAY_DIR/key.pem"; then
    echo "Error: certificate/key validation failed (symlinks, ownership, permissions, size, and PEM are checked)."
    exit 1
  fi

  # Stop any stale single-certificate helper before starting the display.
  stop_owned_pid_file "$CERT_SERVER_PID" "$DISPLAY_DIR/cert_server.py"
else
  # HTTP mode: shut down any leftover cert server from a previous TLS session
  stop_owned_pid_file "$CERT_SERVER_PID" "$DISPLAY_DIR/cert_server.py"
fi

SCHEME=$($TLS_MODE && echo "https" || echo "http")
TRUSTED_ORIGINS="${SCHEME}://localhost:${DISPLAY_PORT},${SCHEME}://127.0.0.1:${DISPLAY_PORT}"
[[ -n "$LAN_IP" ]] && TRUSTED_ORIGINS="${TRUSTED_ORIGINS},${SCHEME}://${LAN_IP}:${DISPLAY_PORT}"
export GM_DISPLAY_TRUSTED_ORIGINS="${TRUSTED_ORIGINS}${GM_DISPLAY_EXTRA_TRUSTED_ORIGINS:+,${GM_DISPLAY_EXTRA_TRUSTED_ORIGINS}}"

# ── Force-kill previous display instance ─────────────────────────────────────
stop_owned_pid_file "$PID_FILE" "$DISPLAY_DIR/gm-display-app.py"
sleep 0.3

# ── Start Flask ───────────────────────────────────────────────────────────────
APP_ARGS="$LAN_FLAG"
$TLS_MODE && APP_ARGS="$APP_ARGS --tls"

nohup python3 "$DISPLAY_DIR/gm-display-app.py" $APP_ARGS > "$LOG" 2>&1 &
APP_PID=$!
APP_IDENTITY=$(process_identity "$APP_PID") || {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  rm -f "$PID_FILE" "${PID_FILE}.identity"
  echo "Error: display process exited before its identity could be recorded"
  exit 1
}
write_pid_record "$PID_FILE" "$APP_PID" "$APP_IDENTITY"
if $TLS_MODE; then
  python3 "$DISPLAY_DIR/cert_server.py" \
    --cert "$DISPLAY_DIR/cert.pem" --key "$DISPLAY_DIR/key.pem" \
    --parent-pid "$APP_PID" --parent-identity "$APP_IDENTITY" --port "$CERT_PORT" \
    > /dev/null 2>&1 &
  HELPER_PID=$!
  HELPER_IDENTITY=$(process_identity "$HELPER_PID") || {
    kill "$HELPER_PID" 2>/dev/null || true
    kill "$APP_PID" 2>/dev/null || true
    wait "$HELPER_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
    rm -f "$CERT_SERVER_PID" "${CERT_SERVER_PID}.identity" "$PID_FILE" "${PID_FILE}.identity"
    echo "Error: certificate helper exited before its identity could be recorded (port ${CERT_PORT} may be occupied)"
    exit 1
  }
  write_pid_record "$CERT_SERVER_PID" "$HELPER_PID" "$HELPER_IDENTITY"
  CERT_HASH=$(openssl dgst -sha256 "$DISPLAY_DIR/cert.pem" | awk '{print $NF}')
  CERT_READY=false
  for _ in $(seq 1 20); do
    if ! owned_pid_from_file "$CERT_SERVER_PID" "$DISPLAY_DIR/cert_server.py" >/dev/null; then
      break
    fi
    downloaded_hash=$(curl -fsS --connect-timeout 1 --max-time 2 \
      "http://127.0.0.1:${CERT_PORT}/cert.pem" 2>/dev/null | openssl dgst -sha256 | awk '{print $NF}')
    if [[ -n "$CERT_HASH" && "$downloaded_hash" == "$CERT_HASH" ]]; then
      CERT_READY=true
      break
    fi
    sleep 0.1
  done
  if ! $CERT_READY; then
    echo "Error: certificate helper failed bind/readiness/content verification on port ${CERT_PORT}"
    stop_owned_pid_file "$CERT_SERVER_PID" "$DISPLAY_DIR/cert_server.py"
    stop_owned_pid_file "$PID_FILE" "$DISPLAY_DIR/gm-display-app.py"
    exit 1
  fi
fi

LOCAL_URL="${SCHEME}://localhost:${DISPLAY_PORT}"

# Wait up to 5 s for the server to become ready
for i in $(seq 1 10); do
  sleep 0.5
  if probe_owned_display "$LOCAL_URL"; then
    echo ""
    echo "Display started — $LOCAL_URL"
    [[ -n "$LAN_IP" ]] && echo "LAN access:     ${SCHEME}://${LAN_IP}:${DISPLAY_PORT}"

    if $TLS_MODE; then
      echo ""
      echo "══════════════════════════════════════════════════════════════"
      echo "  TLS MODE — one-time certificate install required per device"
      echo "══════════════════════════════════════════════════════════════"
      echo ""
      echo "  A plain HTTP server is running on :${CERT_PORT} so devices can"
      echo "  download the cert without needing to trust it first."
      echo ""
      echo "  Step 1 — on each new device, open:"
      echo "           http://${LAN_IP}:${CERT_PORT}/cert.pem"
      echo ""
      echo "  iOS (iPhone / iPad):"
      echo "    Safari will say 'Allow' to download → tap Allow"
      echo "    Settings → General → VPN & Device Management → install profile"
      echo "    Settings → General → About → Certificate Trust Settings → enable"
      echo ""
      echo "  Android:"
      echo "    Chrome downloads the file → open it → install as CA Certificate"
      echo ""
      echo "  Mac (other than this machine):"
      echo "    Open cert.pem → Keychain Access → mark as Always Trust"
      echo ""
      echo "  Step 2 — open  https://${LAN_IP}:${DISPLAY_PORT}  in the device browser."
      echo "  No further warnings after the cert is trusted."
      echo ""
      echo "  The cert server on :8080 runs until the display is stopped."
      echo ""
      echo "  SECURITY: --lan is for a trusted LAN only. TLS encrypts traffic,"
      echo "  but install this certificate only after verifying its SHA-256"
      echo "  fingerprint through a trusted channel: ${CERT_HASH}"
      echo "  The HTTP certificate download is not safe bootstrap on a hostile LAN."
      echo "══════════════════════════════════════════════════════════════"
    fi

    open "$LOCAL_URL" 2>/dev/null || true
    exit 0
  fi
done

echo "Warning: display server may not have started. Check $LOG for details."
stop_owned_pid_file "$CERT_SERVER_PID" "$DISPLAY_DIR/cert_server.py"
stop_owned_pid_file "$PID_FILE" "$DISPLAY_DIR/gm-display-app.py"
exit 1

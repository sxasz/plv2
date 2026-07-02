#!/usr/bin/env bash
# One-command VPS setup for polymkt-bot (milestone M1: fact check + recorder).
#
# On a fresh Ubuntu 22.04/24.04 server, run:
#
#   git clone https://github.com/sxasz/plv2.git && cd plv2 \
#     && git checkout claude/polymarket-btc-latency-bot-tz0a2q \
#     && bash scripts/setup_vps.sh
#
# What it does (safe to re-run any time):
#   1. Installs system packages (python, git)
#   2. Creates a virtualenv and installs the bot + dev tools
#   3. Runs the test suite (must pass)
#   4. Runs scripts/verify_facts.py against the live Polymarket/Binance APIs
#      (~6 minutes; writes facts_runtime.json)
#   5. Installs and starts a systemd service that records all three market
#      feeds 24/7 (survives reboots and crashes)
#
# It does NOT trade. It does NOT need any account, key, or money.

set -euo pipefail

BRANCH="claude/polymarket-btc-latency-bot-tz0a2q"
REPO_URL="https://github.com/sxasz/plv2.git"
SERVICE="polymkt-recorder"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "please run as root (log in as root or use: sudo bash scripts/setup_vps.sh)"

# --- 1. system packages ------------------------------------------------------
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null

# --- 2. locate or clone the repo ---------------------------------------------
if [ -f "pyproject.toml" ] && grep -q "polymkt-bot" pyproject.toml 2>/dev/null; then
    REPO_DIR="$(pwd)"
elif [ -d "/opt/plv2" ]; then
    REPO_DIR="/opt/plv2"
    git -C "$REPO_DIR" fetch origin "$BRANCH"
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
else
    say "Cloning repository to /opt/plv2"
    git clone "$REPO_URL" /opt/plv2
    REPO_DIR="/opt/plv2"
    git -C "$REPO_DIR" checkout "$BRANCH"
fi
cd "$REPO_DIR"
say "Using repo at $REPO_DIR (branch $(git rev-parse --abbrev-ref HEAD))"

# --- 3. python environment ----------------------------------------------------
say "Creating virtualenv and installing the bot"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e ".[dev]"

# --- 4. self-tests -------------------------------------------------------------
say "Running the test suite"
./.venv/bin/pytest -q || die "tests failed — send the output above back for a fix"

# --- 5. live fact verification --------------------------------------------------
say "Verifying exchange facts against live APIs (~6 minutes, please wait)"
set +e
./.venv/bin/python scripts/verify_facts.py --out facts_runtime.json
VERIFY_RC=$?
set -e
if [ $VERIFY_RC -ne 0 ]; then
    printf '\n\033[1;33mWARNING: some fact checks FAILED. The recorder will still start,\n'
    printf 'but do NOT proceed past recording. Send facts_runtime.json back for review.\033[0m\n'
fi

# --- 6. recorder as a systemd service -------------------------------------------
say "Installing the 24/7 recorder service"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=polymkt-bot feed recorder (M1, no trading)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python -m polymkt_bot.main --mode record
Restart=always
RestartSec=5
# Recorder writes only inside the repo's data/ directory.
ReadWritePaths=${REPO_DIR}

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "${SERVICE}.service"
sleep 5
systemctl --no-pager --lines=5 status "${SERVICE}.service" || true

# --- 7. summary -------------------------------------------------------------------
say "DONE. What just happened and what to do next:"
cat <<EOF

  Fact check result ............ $( [ $VERIFY_RC -eq 0 ] && echo "ALL OK" || echo "SOME CHECKS FAILED (see above)" )
  Fact check details ........... ${REPO_DIR}/facts_runtime.json
  Recorder ..................... running as systemd service '${SERVICE}'
  Recorded data lands in ....... ${REPO_DIR}/data/raw/

  Useful commands:
    systemctl status ${SERVICE}      # is it running?
    journalctl -u ${SERVICE} -f      # watch live logs (Ctrl+C to exit)
    ls -lh ${REPO_DIR}/data/raw/     # files should GROW over time

  YOUR NEXT STEPS:
    1. Copy the fact-check summary printed above (the line with "all_ok")
       and report it back in the Claude session.
    2. Wait 24 hours. Files in data/raw/ must keep growing.
    3. Report back; the recording gets analyzed for the M1 data-quality
       report before anything else happens. No trading occurs in this mode.
EOF

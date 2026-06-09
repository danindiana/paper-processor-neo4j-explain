#!/usr/bin/env bash
# install.sh — install + enable the pp-live-state systemd timer (system scope).
# Refreshes pp_explain_live.json + the Neo4j :PPLive graph every 15s.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
sudo install -m 644 "$HERE/pp-live-state.service" /etc/systemd/system/pp-live-state.service
sudo install -m 644 "$HERE/pp-live-state.timer"   /etc/systemd/system/pp-live-state.timer
sudo systemctl daemon-reload
sudo systemctl enable --now pp-live-state.timer
echo "✓ installed. Status:"
systemctl status pp-live-state.timer --no-pager || true
echo "Next runs:"; systemctl list-timers pp-live-state.timer --no-pager || true
echo "Uninstall: sudo systemctl disable --now pp-live-state.timer && sudo rm /etc/systemd/system/pp-live-state.{service,timer} && sudo systemctl daemon-reload"

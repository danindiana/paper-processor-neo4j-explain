#!/usr/bin/env bash
# render.sh — headless-render a diagram HTML to a trimmed PNG.
# Usage: ./render.sh <file.html> [out.png] [width] [height]
set -euo pipefail
HTML="$1"
OUT="${2:-${HTML%.html}.png}"
W="${3:-2600}"; H="${4:-820}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ABS="$(cd "$(dirname "$HTML")" && pwd)/$(basename "$HTML")"
TMP="$(mktemp --suffix=.png)"
CHROME="$(command -v google-chrome-stable || command -v google-chrome || command -v chromium)"

"$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --window-size="$W,$H" --virtual-time-budget=8000 \
  --screenshot="$TMP" "file://$ABS" 2>/dev/null

# trim background, pad, then chop the dead band under the fixed header
convert "$TMP" -fuzz 6% -trim +repage -bordercolor '#06090f' -border 40 \
        -gravity North -chop 0x100+0+110 "$OUT"
rm -f "$TMP"
echo "rendered $OUT ($(identify -format '%wx%h' "$OUT"))"

#!/usr/bin/env bash
# Renders the Remotion cut to tools/record/out/receipts-demo-2026-09-remotion.mp4.
# Syncs public/ from tools/record/out/ first, then merges the freshly-read
# investigation-clip duration (scripts/agent-duration.mjs) with an optional
# voice-over file into the --props Remotion actually renders with.
#
#   ./render.sh                          # silent
#   ./render.sh ~/Desktop/voice.m4a       # with the voice-over muxed in
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

./sync.sh "${1:-}"

DURATION_JSON="$(node scripts/agent-duration.mjs)"

if [ "${1:-}" != "" ]; then
  ext="${1##*.}"
  PROPS="$(node -e "const d=$DURATION_JSON; d.audio='voice.$ext'; process.stdout.write(JSON.stringify(d))")"
else
  PROPS="$DURATION_JSON"
fi

OUT="../out/receipts-demo-2026-09-remotion.mp4"
npx remotion render Receipts "$OUT" --props="$PROPS"
echo "rendered -> $HERE/$OUT"
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT"

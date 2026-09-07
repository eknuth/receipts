#!/usr/bin/env bash
# Puts the recording's inputs into public/ so Remotion can read them as
# staticFile()s. Re-run after tools/record/out/ changes (a re-emit, a new
# capture, a rebuilt architecture PNG).
#
# These are `cp -c` clones (APFS copy-on-write, via clonefile(2)), not
# symlinks: Remotion's own render pipeline copies public/ into a temp
# bundle directory for every render and does not follow a symlink whose
# target lives outside public/ (verified 2026-09-07 -- a symlinked still
# 404s, a symlinked clip fails the same way; see tools/record/README.md).
# A clone is a real file as far as any of that is concerned, but costs no
# extra disk on this volume: it shares blocks with the source until either
# copy is written to, which never happens to either side of this pipeline.
# On a non-APFS volume `cp -c` falls back to a plain copy instead of failing.
#
# An optional first argument is a voice-over file (any path); it lands as
# public/voice.<ext> so it can be passed to render as
# --props='{"audio":"voice.<ext>"}' (render.sh does this).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/../out"
PUBLIC="$HERE/public"

mkdir -p "$PUBLIC"

put() {
  local src="$1" name="$2"
  if [ ! -e "$src" ]; then
    echo "sync.sh: missing $src" >&2
    exit 1
  fi
  rm -f "$PUBLIC/$name"
  cp -c "$src" "$PUBLIC/$name" 2>/dev/null || cp "$src" "$PUBLIC/$name"
}

put "$OUT/01-emit.mp4" "01-emit.mp4"
put "$OUT/03-agent.mp4" "03-agent.mp4"
put "$OUT/06-tables.mp4" "06-tables.mp4"
put "$OUT/02-heatmap.png" "02-heatmap.png"
put "$OUT/04-timeline.png" "04-timeline.png"
put "$OUT/05-timeline-score.png" "05-timeline-score.png"
put "$OUT/seg/00b-architecture-src.png" "00-architecture.png"

if [ "${1:-}" != "" ]; then
  ext="${1##*.}"
  put "$1" "voice.$ext"
  echo "voice-over linked as public/voice.$ext -- pass --props='{\"audio\":\"voice.$ext\"}'"
fi

echo "synced $(ls "$PUBLIC" | wc -l | tr -d ' ') files into $PUBLIC"

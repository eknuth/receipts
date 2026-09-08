# Receipts, Remotion cut

A Remotion project (composition id `Receipts`, 1600x900, 30 fps). It plays
the same clips and stills as `tools/record/build.py`'s ffmpeg assembly, with
real cross-dissolve transitions and thermal-receipt title/end cards. Usage
and the design are documented in `tools/record/README.md`; this file is only
the local commands.

```
./sync.sh                        # clone tools/record/out/ inputs into public/
npx remotion studio               # live preview
./render.sh                       # render to ../out/receipts-demo-2026-09-remotion.mp4
./render.sh ~/Desktop/voice.m4a    # same, with the voice-over muxed in
npm run lint                      # eslint + tsc
```

`public/`, `node_modules/`, and `out/` are gitignored; nothing under them is
committed.

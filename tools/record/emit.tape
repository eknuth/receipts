# Segment 1 of the recording: emit a scripted incident.
# Run from the repo root, `vhs tools/record/emit.tape`; tools/record/build.py does.
Output tools/record/out/01-emit.mp4
Set FontSize 16
Set Width 1600
Set Height 900
Set Padding 30
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set Framerate 30
Sleep 1s
Type "uv run python -m gen.emit --scenario payments-stripe-v251-uswest"
Sleep 800ms
Enter
Wait+Screen@240s /span events:/
Sleep 6s

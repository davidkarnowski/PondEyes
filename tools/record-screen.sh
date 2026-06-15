#!/bin/bash
# record-screen.sh [SECONDS] [OUTFILE] — capture the Pi's screen to an H.264 mp4.
#
# Hardware-encoded via the Pi's h264_v4l2m2m encoder, so it stays light on the CPU while PondEyes
# runs. Grabs the desktop X session, so it works run over SSH (used to capture the appliance for
# docs / agentic review). Needs ffmpeg (`sudo apt install ffmpeg`) and an X11 desktop session.
#
#   ./tools/record-screen.sh 15 /tmp/pondeyes.mp4
#   # then pull it: scp pi:/tmp/pondeyes.mp4 .
DUR=${1:-10}
OUT=${2:-/tmp/pondeyes-screen.mp4}

# Find the desktop session's X DISPLAY + XAUTHORITY (so this works when invoked over SSH).
SP=$(pgrep -u "$(id -un)" -f lxsession | head -1)
DISP=$(tr '\0' '\n' < "/proc/$SP/environ" 2>/dev/null | sed -n 's/^DISPLAY=//p'); DISP=${DISP:-:0}
XA=$(tr '\0' '\n' < "/proc/$SP/environ" 2>/dev/null | sed -n 's/^XAUTHORITY=//p'); XA=${XA:-$HOME/.Xauthority}
export DISPLAY="$DISP" XAUTHORITY="$XA"

SIZE=$(xrandr 2>/dev/null | awk '/\*/{print $1; exit}'); SIZE=${SIZE:-480x800}
ffmpeg -y -hide_banner -loglevel warning \
  -f x11grab -framerate 15 -video_size "$SIZE" -i "$DISP" -t "$DUR" \
  -pix_fmt yuv420p -c:v h264_v4l2m2m -b:v 4M "$OUT"
echo "saved $OUT ($(du -h "$OUT" 2>/dev/null | cut -f1), $SIZE, ${DUR}s)"

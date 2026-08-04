#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  make_desktop_app.sh — build a double-clickable shortcut for the GUI.
#
#      ./make_desktop_app.sh                     default icon, onto the Desktop
#      ./make_desktop_app.sh --icon photo.jpg    use your own thumbnail
#      ./make_desktop_app.sh --dest ~/Apps       somewhere other than the Desktop
#      ./make_desktop_app.sh --name "SoOp Lab"   different visible name
#
#  macOS  -> a real .app bundle (Dock icon, no Terminal window, custom .icns)
#  Linux  -> a .desktop launcher, installed to the applications menu and Desktop
#
#  WHY THE INTERPRETER IS BAKED IN: an app launched from Finder or a desktop
#  environment inherits almost nothing — no conda shell hook, often not even
#  the user's PATH. Resolving the sdrr interpreter at BUILD time and writing
#  its absolute path into the launcher is what makes double-clicking work at
#  all. Rebuild after moving the conda env or the repo.
#
#  Nothing in 01_CODE is touched; this only writes the bundle and, with --icon,
#  gui/assets/app_icon.png.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

GUI_DIR="$(cd "$(dirname "$0")" && pwd)"
CODE_DIR="$(dirname "$GUI_DIR")"
ENV_NAME="${SDRR_ENV:-sdrr}"

APP_NAME="SDR Reflectometry"
ICON_SRC=""
DEST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --icon) ICON_SRC="$2"; shift ;;
    --name) APP_NAME="$2"; shift ;;
    --dest) DEST="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,2\}//'; exit 0 ;;
    *) echo "Unknown option: $1  (try --help)"; exit 1 ;;
  esac
  shift
done

# ── resolve the interpreter ────────────────────────────────────────────────
PY=""
if [ -n "${CONDA_PREFIX:-}" ] && [ "$(basename "$CONDA_PREFIX")" = "$ENV_NAME" ]; then
  PY="$CONDA_PREFIX/bin/python"
elif command -v conda >/dev/null 2>&1; then
  ENV_PATH=$(conda env list | awk -v e="$ENV_NAME" '$1==e {print $NF}' | head -1)
  [ -n "$ENV_PATH" ] && [ -x "$ENV_PATH/bin/python" ] && PY="$ENV_PATH/bin/python"
fi
[ -z "$PY" ] && PY="$(command -v python3 || true)"

if [ -z "$PY" ] || ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "ERROR: could not find a python with tkinter for env '$ENV_NAME'."
  echo "       conda activate $ENV_NAME, then re-run this script."
  exit 1
fi
echo "interpreter : $PY"

# The bundle needs the REAL binary (bin/python is a symlink to bin/python3.12)
# and the env prefix, so the copy inside the bundle can find its stdlib.
PY_REAL="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
PY_PREFIX="$("$PY" -c 'import sys; print(sys.base_prefix)')"

# ── icon ───────────────────────────────────────────────────────────────────
ICON_PNG="$GUI_DIR/assets/app_icon.png"
if [ -n "$ICON_SRC" ]; then
  echo "icon source : $ICON_SRC"
  ( cd "$CODE_DIR" && "$PY" -m gui.make_icon "$ICON_SRC" "$ICON_PNG" ) >/dev/null
elif [ ! -f "$ICON_PNG" ]; then
  echo "icon source : (generated default)"
  ( cd "$CODE_DIR" && "$PY" -m gui.make_icon ) >/dev/null
else
  echo "icon source : existing $ICON_PNG"
fi

DESKTOP_DIR="${DEST:-$HOME/Desktop}"
mkdir -p "$DESKTOP_DIR"

# ═══════════════════════════ macOS ═════════════════════════════════════════
if [ "$(uname -s)" = "Darwin" ]; then
  APP="$DESKTOP_DIR/$APP_NAME.app"
  echo "building    : $APP"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

  # .icns from the PNG, using Pillow for the resizes and iconutil to pack.
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  "$PY" - "$ICON_PNG" "$ICONSET" <<'PYEOF'
import sys
from PIL import Image
src, out = sys.argv[1], sys.argv[2]
image = Image.open(src).convert("RGBA")
for size in (16, 32, 64, 128, 256, 512):
    image.resize((size, size), Image.LANCZOS).save(f"{out}/icon_{size}x{size}.png")
    image.resize((size * 2, size * 2), Image.LANCZOS).save(
        f"{out}/icon_{size}x{size}@2x.png")
PYEOF
  if command -v iconutil >/dev/null 2>&1; then
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
  else
    cp "$ICON_PNG" "$APP/Contents/Resources/AppIcon.png"
  fi

  cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>       <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>        <string>de.gfz.sdr-reflectometry.gui</string>
    <key>CFBundleVersion</key>           <string>1.1.0</string>
    <key>CFBundleShortVersionString</key><string>1.1.0</string>
    <key>CFBundleExecutable</key>        <string>launch</string>
    <key>CFBundleIconFile</key>          <string>AppIcon</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>LSMinimumSystemVersion</key>    <string>10.13</string>
    <key>LSApplicationCategoryType</key> <string>public.app-category.education</string>
    <!-- Required for the SoOp availability tab's "This laptop" location fix.
         macOS's Location Services permission attaches to a BUNDLE IDENTIFIER —
         a bare 'python -m gui' process has none and can never be granted
         access no matter what is clicked in System Settings, which is exactly
         why this bundle exists as an option. Without this key the system
         prompt never appears at all; gui/location.py explains the rest.

         NOTE: this key is necessary but NOT sufficient. The python process
         itself must run from a binary INSIDE Contents/MacOS — see the comment
         above the interpreter copy further down this script. -->
    <key>NSLocationWhenInUseUsageDescription</key>
    <string>Used once, on request, to record the receiver's coordinates for
    the SoOp availability sky-view mask. The fix is cached after the first
    request, so Location Services can be switched off again afterwards.</string>
    <key>NSLocationUsageDescription</key>
    <string>Used once, on request, to record the receiver's coordinates for
    the SoOp availability sky-view mask.</string>
</dict>
</plist>
EOF

# ── the interpreter, INSIDE the bundle ─────────────────────────────────────
#
#  THIS is what makes Location Services work, and why putting the .app around
#  a plain `$PY -m gui` was never enough.
#
#  macOS decides which app is asking for location by looking at the running
#  process's executable path and walking up to the enclosing .app. A launcher
#  that shells out to /Users/.../envs/sdrr/bin/python produces a process whose
#  executable is in the conda env, NOT in the bundle — so NSBundle.mainBundle()
#  resolves to '.../envs/sdrr/bin', bundleIdentifier() is None, the app never
#  appears in System Settings > Privacy & Security > Location Services, and
#  requestWhenInUseAuthorization() silently no-ops. The .app wrapper is
#  invisible to CoreLocation; only the python process is real to it.
#
#  Copying the interpreter into Contents/MacOS/ fixes exactly that: same
#  binary, but now the process lives inside the bundle and inherits its
#  identity. PYTHONHOME (exported by the launcher) points the copy back at the
#  conda env for its stdlib and site-packages, so every import — numpy, tk,
#  skyfield, pyobjc — resolves identically to a terminal run, and subprocesses
#  spawned via sys.executable inherit it and work too.
#
#  A pyvenv.cfg would do the same job without an env var, but codesign refuses
#  to sign a bundle with a stray config file in Contents/ or Contents/MacOS/,
#  and the signature is not optional (see below).
cp "$PY_REAL" "$APP/Contents/MacOS/python"
chmod +x "$APP/Contents/MacOS/python"

  cat > "$APP/Contents/MacOS/launch" <<EOF
#!/bin/bash
# Generated by gui/make_desktop_app.sh — rebuild after moving the repo or env.
#
# A Finder launch has no terminal behind it, so a process that dies before Tk
# ever draws a window used to fail SILENTLY — the Dock icon bounces once and
# nothing happens, with only launch.log (if you knew to look) explaining why.
# One real cause, worth knowing about explicitly: this repo sits under
# ~/Desktop, one of macOS's TCC-protected folders (Desktop/Documents/
# Downloads). A freshly built, unsigned .app launched by double-click has no
# inherited access there — unlike running the same command from a terminal,
# whose access it WOULD inherit — and gets silently denied. Fix: System
# Settings > Privacy & Security > Full Disk Access > add the interpreter at
# "\$PY", or move the repo out of ~/Desktop.
#
# Either way, this wrapper now surfaces ANY failure as a real alert instead
# of leaving a dead Dock icon as the only symptom.
CODE_DIR="$CODE_DIR"
ENV_PREFIX="$PY_PREFIX"

# Run the interpreter that lives INSIDE this bundle, not the one in the conda
# env — that is the whole reason Location Services can see this app at all.
# PYTHONHOME points it back at the env for stdlib and site-packages; gui/
# __main__.py drops it from the environment immediately afterwards so it cannot
# follow `conda run`, python3 or bash out into the capture scripts.
HERE="\$(cd "\$(dirname "\$0")" && pwd)"
PY="\$HERE/python"
if [ -x "\$PY" ]; then
  export PYTHONHOME="\$ENV_PREFIX"
else
  PY="\$ENV_PREFIX/bin/python"     # bundle damaged; run anyway, minus location
fi

# Finder gives an app almost no PATH, so bladeRF-cli and the env's other
# command-line tools would be invisible to the prechecks and capture scripts.
export PATH="\$ENV_PREFIX/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"

LOG_DIR="\$CODE_DIR/gui/.state/logs"
LOG="\$LOG_DIR/launch.log"
mkdir -p "\$LOG_DIR" 2>/dev/null

if ! cd "\$CODE_DIR" 2>/dev/null; then
  osascript -e 'display alert "SDR Reflectometry could not start" message "Its folder is missing or unreadable." as critical' >/dev/null 2>&1
  exit 1
fi

"\$PY" -m gui >"\$LOG" 2>&1
STATUS=\$?

if [ "\$STATUS" -ne 0 ]; then
  # One short, sanitised line in the dialog — the full traceback stays in the
  # log rather than risking broken AppleScript syntax from arbitrary text.
  LINE=\$(tail -n 20 "\$LOG" | grep -v '^\$' | tail -n 1 | tr -d '"\\\\' | cut -c1-160)
  MSG="\${LINE}

Full log: \${LOG}"
  osascript -e 'on run argv' \\
             -e 'display alert "SDR Reflectometry did not start (exit code '"\$STATUS"')" message (item 1 of argv) as critical' \\
             -e 'end run' "\$MSG" >/dev/null 2>&1
fi
exit "\$STATUS"
EOF
  chmod +x "$APP/Contents/MacOS/launch"
  printf 'APPL????' > "$APP/Contents/PkgInfo"

  # ── sign, last ───────────────────────────────────────────────────────────
  #  locationd keys its permission records to a bundle identifier AND the code
  #  signature backing it; an unsigned bundle has nothing stable to record, so
  #  the app never sticks in the Location Services list. An ad-hoc signature
  #  (-s -) is enough for a locally built app and needs no developer account.
  #
  #  Must be the LAST step: signing seals every file in the bundle, so anything
  #  written afterwards invalidates it. The nested interpreter is signed first,
  #  then the bundle that seals it.
  if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$APP/Contents/MacOS/python" >/dev/null 2>&1
    if codesign --force --sign - "$APP" >/dev/null 2>&1; then
      echo "signature   : ad-hoc, ok"
    else
      echo "signature   : FAILED — Location Services will not work."
      echo "              Everything else still runs; see gui/location.py."
    fi
  else
    echo "signature   : codesign unavailable (install Xcode command line tools)"
  fi

  touch "$APP"                      # nudge Finder to pick up the new icon
  echo ""
  echo "Done. Double-click '$APP_NAME' on your Desktop."
  echo "If the icon looks stale, log out and back in (Finder caches icons)."
  echo ""
  echo "First location request will show a system permission prompt — click"
  echo "Allow. It can take a few seconds after that for the first fix."
  exit 0
fi

# ═══════════════════════════ Linux ═════════════════════════════════════════
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"
mkdir -p "$APPS_DIR" "$ICON_DIR"

"$PY" - "$ICON_PNG" "$ICON_DIR/sdr-reflectometry.png" <<'PYEOF'
import sys
from PIL import Image
Image.open(sys.argv[1]).convert("RGBA").resize((512, 512), Image.LANCZOS).save(sys.argv[2])
PYEOF

DESKTOP_FILE="$APPS_DIR/sdr-reflectometry.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
Comment=Bistatic GNSS-R capture & analysis
Exec=env PYTHONUNBUFFERED=1 "$PY" -m gui
Path=$CODE_DIR
Icon=sdr-reflectometry
Terminal=false
Categories=Science;Education;
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

cp "$DESKTOP_FILE" "$DESKTOP_DIR/"
chmod +x "$DESKTOP_DIR/$(basename "$DESKTOP_FILE")"
# GNOME refuses to run a .desktop file it does not consider trusted.
command -v gio >/dev/null 2>&1 && \
  gio set "$DESKTOP_DIR/$(basename "$DESKTOP_FILE")" metadata::trusted true 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo ""
echo "Done. '$APP_NAME' is in your applications menu and on your Desktop."

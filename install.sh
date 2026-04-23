#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Eye Tracking Beam Control"
ICON_PATH="$SCRIPT_DIR/icon/Blue_eye_icon.png"
VENV_DIR="$SCRIPT_DIR/venv"
DESKTOP_FILE="$HOME/Desktop/eye_tracking.desktop"

echo "======================================"
echo "  Installing $APP_NAME"
echo "======================================"

# Check Python3
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python3 not found. Please install Python3 first."
    exit 1
fi
echo "[OK] Python3 found: $(python3 --version)"

# Check python3-venv
if ! python3 -m venv --help &>/dev/null; then
    echo "[INFO] Installing python3-venv..."
    sudo apt-get install -y python3-venv
fi

# Create venv inside project folder
echo ""
echo "[INFO] Creating virtual environment at $VENV_DIR ..."
python3 -m venv "$VENV_DIR"
echo "[OK] venv created"

# Install dependencies into venv
echo ""
echo "[INFO] Installing Python packages..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install opencv-python numpy pyserial pillow

# MediaPipe (optional)
echo "[INFO] Installing mediapipe (optional)..."
"$VENV_DIR/bin/pip" install "mediapipe==0.10.14" || echo "[WARN] mediapipe install failed — app will still work without it"

# Update launch.sh to use venv python
cat > "$SCRIPT_DIR/launch.sh" <<EOF
#!/bin/bash
cd "$SCRIPT_DIR"
"$VENV_DIR/bin/python3" eye_tracking_gui.py
EOF
chmod +x "$SCRIPT_DIR/launch.sh"
echo "[OK] launch.sh updated"

# Create desktop shortcut
echo ""
echo "[INFO] Creating desktop shortcut..."
mkdir -p "$HOME/Desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
Comment=Eye tracking system for beam control via Arduino
Exec=$SCRIPT_DIR/launch.sh
Icon=$ICON_PATH
Terminal=false
StartupNotify=true
Categories=Science;
EOF

chmod +x "$DESKTOP_FILE"

if command -v gio &>/dev/null; then
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

echo ""
echo "======================================"
echo "  Done! Icon created on Desktop."
echo "  Double-click 'Eye Tracking Beam Control' to launch."
echo "======================================"

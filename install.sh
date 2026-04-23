#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Eye Tracking Beam Control"
ICON_PATH="$SCRIPT_DIR/icon/Blue_eye_icon.png"
PYTHON_SCRIPT="$SCRIPT_DIR/eye_tracking_gui.py"
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

# Check pip
if ! python3 -m pip --version &>/dev/null; then
    echo "[INFO] Installing pip..."
    sudo apt-get install -y python3-pip
fi
echo "[OK] pip found"

# Install dependencies
echo ""
echo "[INFO] Installing Python packages..."
python3 -m pip install --upgrade pip
python3 -m pip install opencv-python numpy pyserial pillow

# MediaPipe (optional, may fail on some hardware)
echo "[INFO] Installing mediapipe (optional)..."
python3 -m pip install mediapipe || echo "[WARN] mediapipe install failed — app will still work without it"

echo ""
echo "[INFO] Creating desktop shortcut..."

mkdir -p "$HOME/Desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
Comment=Eye tracking system for beam control via Arduino
Exec=python3 $PYTHON_SCRIPT
Icon=$ICON_PATH
Terminal=false
StartupNotify=true
Categories=Science;
EOF

chmod +x "$DESKTOP_FILE"

# Mark as trusted (for GNOME)
if command -v gio &>/dev/null; then
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

echo ""
echo "======================================"
echo "  Done! Icon created on Desktop."
echo "  Double-click 'Eye Tracking Beam Control' to launch."
echo "======================================"

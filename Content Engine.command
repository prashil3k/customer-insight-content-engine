#!/bin/bash
# Double-click this file to launch the Storylane Content Engine
# It will automatically set up the virtual environment on first run.

cd "$(dirname "$0")"

# Strip macOS quarantine from self and all files in this folder (safe no-op if already cleared)
xattr -d com.apple.quarantine "$0" 2>/dev/null
xattr -rd com.apple.quarantine . 2>/dev/null

# --- Auto-setup: create venv and install deps if missing ---
if [ ! -d "venv" ]; then

    # Check Python
    if ! command -v python3 &> /dev/null; then
        echo "❌ Python 3 is required but not installed."
        echo "   Install it from https://www.python.org/downloads/"
        echo ""
        echo "Press any key to close..."
        read -n 1
        exit 1
    fi

    # If deps are already installed globally, inherit them so setup is instant
    if python3 -c "import flask, anthropic, apscheduler" 2>/dev/null; then
        echo "⚙️  Creating environment (inheriting existing packages)..."
        python3 -m venv venv --system-site-packages
        source venv/bin/activate
        pip install -r requirements.txt -q --quiet 2>/dev/null
    else
        echo "🧠 First run — setting up environment (~1 min)..."
        echo ""
        echo "✅ Python 3 found: $(python3 --version)"
        echo "📦 Creating virtual environment..."
        python3 -m venv venv
        if [ $? -ne 0 ]; then
            echo "❌ Failed to create virtual environment."
            echo "Press any key to close..."
            read -n 1
            exit 1
        fi
        source venv/bin/activate
        echo "📦 Installing dependencies..."
        pip install --upgrade pip -q
        pip install -r requirements.txt -q
        echo ""
        echo "✅ Setup complete! Starting the Content Engine..."
        echo ""
    fi
else
    source venv/bin/activate
fi

# Kill anything already on port 8001
pids=$(lsof -ti :8001 2>/dev/null)
if [ -n "$pids" ]; then
    echo "Clearing port 8001..."
    echo "$pids" | xargs kill -9 2>/dev/null
    sleep 0.5
fi

export MALLOC_NANO_ZONE=0

echo "Starting Content Engine..."
python3 app.py > /tmp/content-engine.log 2>&1 &
PID=$!

sleep 2
open "http://localhost:8001"

echo "✅ Content Engine running at http://localhost:8001"
echo "📋 Log: tail -f /tmp/content-engine.log"
echo ""
echo "Press Ctrl+C or close this window to stop the server."

trap "echo 'Stopping...'; kill $PID 2>/dev/null; exit 0" INT TERM
while true; do sleep 30; done

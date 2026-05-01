#!/bin/bash
# Double-click this file to launch the Storylane Content Engine
# It will automatically set up the virtual environment on first run.

cd "$(dirname "$0")"

# Strip macOS quarantine from self and all files in this folder (safe no-op if already cleared)
xattr -d com.apple.quarantine "$0" 2>/dev/null
xattr -rd com.apple.quarantine . 2>/dev/null

# Kill anything already on port 8001
pids=$(lsof -ti :8001 2>/dev/null)
if [ -n "$pids" ]; then
    echo "Clearing port 8001..."
    echo "$pids" | xargs kill -9 2>/dev/null
    sleep 0.5
fi

# --- First-run detection: show a loading page while deps install ---
if [ ! -d "venv" ]; then
    echo ""
    echo "🧠 First run detected — setting up environment (~1–2 min)..."
    echo ""

    # Spin up a tiny placeholder server so the browser has something to show
    python3 -c "
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

PAGE = b'''<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\">
  <meta http-equiv=\"refresh\" content=\"6\">
  <title>Setting up...</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f7f6ff;
      display: flex; align-items: center; justify-content: center;
      height: 100vh;
    }
    .card {
      background: white;
      border-radius: 16px;
      padding: 48px 56px;
      text-align: center;
      box-shadow: 0 4px 24px rgba(92,75,255,0.1);
      max-width: 480px;
    }
    .icon { font-size: 48px; margin-bottom: 20px; }
    h2 { font-size: 22px; font-weight: 700; color: #1a1a2e; margin-bottom: 12px; }
    p  { font-size: 14px; color: #6b7280; line-height: 1.6; margin-bottom: 8px; }
    .note { font-size: 12px; color: #9ca3af; margin-top: 16px; }
    .dot { display: inline-block; animation: blink 1.2s infinite; }
    .dot:nth-child(2) { animation-delay: 0.2s; }
    .dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes blink { 0%,80%,100%{opacity:0.2} 40%{opacity:1} }
  </style>
</head>
<body>
  <div class=\"card\">
    <div class=\"icon\">⚙️</div>
    <h2>Setting up for first run<span class=\"dot\">.</span><span class=\"dot\">.</span><span class=\"dot\">.</span></h2>
    <p>Installing dependencies. This only happens once and takes about <strong>1–2 minutes</strong>.</p>
    <p>Your insights, articles, and company brain are all saved and will appear as soon as setup finishes.</p>
    <p class=\"note\">This page refreshes automatically every 6 seconds.</p>
  </div>
</body>
</html>'''

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(PAGE)
    def log_message(self, *a): pass

HTTPServer(('0.0.0.0', 8001), H).serve_forever()
" &
    SETUP_SERVER_PID=$!

    sleep 1
    open "http://localhost:8001"
    echo "↗  Opened browser — showing setup screen."
    echo ""

    # Check Python
    if ! command -v python3 &> /dev/null; then
        echo "❌ Python 3 is required but not installed."
        echo "   Install it from https://www.python.org/downloads/"
        kill $SETUP_SERVER_PID 2>/dev/null
        echo "Press any key to close..."
        read -n 1
        exit 1
    fi

    echo "✅ Python 3 found: $(python3 --version)"

    # Create venv
    if python3 -c "import flask, anthropic, apscheduler" 2>/dev/null; then
        echo "⚙️  Creating environment (inheriting existing packages)..."
        python3 -m venv venv --system-site-packages
        source venv/bin/activate
        pip install -r requirements.txt -q --quiet 2>/dev/null
    else
        echo "📦 Creating virtual environment..."
        python3 -m venv venv
        if [ $? -ne 0 ]; then
            echo "❌ Failed to create virtual environment."
            kill $SETUP_SERVER_PID 2>/dev/null
            echo "Press any key to close..."
            read -n 1
            exit 1
        fi
        source venv/bin/activate
        echo "📦 Installing dependencies..."
        pip install --upgrade pip -q
        pip install -r requirements.txt -q
    fi

    echo ""
    echo "✅ Setup complete! Starting the Content Engine..."
    echo ""

    # Kill the placeholder server — real app is about to take over
    kill $SETUP_SERVER_PID 2>/dev/null
    sleep 0.3

else
    source venv/bin/activate
fi

export MALLOC_NANO_ZONE=0

echo "Starting Content Engine..."
python3 app.py > /tmp/content-engine.log 2>&1 &
PID=$!

# On first run browser is already open; on subsequent runs open it after a short wait
if [ -z "$SETUP_SERVER_PID" ]; then
    sleep 2
    open "http://localhost:8001"
fi

echo "✅ Content Engine running at http://localhost:8001"
echo "📋 Log: tail -f /tmp/content-engine.log"
echo ""
echo "Press Ctrl+C or close this window to stop the server."

trap "echo 'Stopping...'; kill $PID 2>/dev/null; exit 0" INT TERM
while true; do sleep 30; done

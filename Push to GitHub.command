#!/bin/bash
# Double-click this to push all changes to GitHub.

cd "$(dirname "$0")"

echo ""
echo "📦 Checking for changes..."
git add .

if git diff --cached --quiet; then
  echo "✅ Nothing new to push — everything is already up to date."
  echo ""
  echo "Press any key to close..."
  read -n 1
  exit 0
fi

echo ""
echo "📝 Changes to be pushed:"
git diff --cached --name-only | sed 's/^/   /'

echo ""
read -p "Enter a short description of what changed (or press Enter for default): " msg
if [ -z "$msg" ]; then
  msg="Update content engine — $(date '+%Y-%m-%d %H:%M')"
fi

git commit -m "$msg"
git push origin main

echo ""
echo "✅ Pushed to GitHub successfully."
echo "   https://github.com/prashil3k/customer-insight-content-engine"
echo ""
echo "Press any key to close..."
read -n 1

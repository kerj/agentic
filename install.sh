#!/bin/bash
# Agentic Workflow Installer

set -euo pipefail

AGENTIC_HOME="${HOME}/.agentic"

echo "🚀 Installing Agentic Workflow..."
echo ""

# Create directory structure
mkdir -p "${AGENTIC_HOME}"/{bin,lib,agents}

# Download/copy files would go here
# For now, we'll assume files are already in place

# Make bin executable
chmod +x "${AGENTIC_HOME}/bin/agentic"

# Add to PATH in .zshrc if not already there
if ! grep -q "AGENTIC_HOME" ~/.zshrc 2>/dev/null; then
  echo "" >> ~/.zshrc
  echo "# Agentic Workflow" >> ~/.zshrc
  echo 'export AGENTIC_HOME="$HOME/.agentic"' >> ~/.zshrc
  echo 'export PATH="$AGENTIC_HOME/bin:$PATH"' >> ~/.zshrc
  echo "" >> ~/.zshrc
  echo "✅ Added to .zshrc"
else
  echo "✅ Already in .zshrc"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Installation Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "  1. Reload shell: source ~/.zshrc"
echo "  2. Initialize in project: cd your-project && agentic init"
echo "  3. Start workflow: agentic"
echo ""
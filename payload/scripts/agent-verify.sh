#!/usr/bin/env bash
# Thin launcher so hooks/plugins have one stable entry point. Logic lives in agent-verify.py.
# GUI-launched editors often start with a minimal PATH, so extend it before looking for tools.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
exec python3 "$(dirname "${BASH_SOURCE[0]}")/agent-verify.py"

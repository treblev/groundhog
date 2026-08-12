#!/bin/sh
set -eu

if ! command -v gh >/dev/null 2>&1; then
    echo "GitHub CLI (gh) is required for GitHub MCP authentication." >&2
    exit 1
fi

if ! command -v github-mcp-server >/dev/null 2>&1; then
    echo "github-mcp-server is required. Install it with Homebrew." >&2
    exit 1
fi

GITHUB_PERSONAL_ACCESS_TOKEN="$(gh auth token)"
GITHUB_TOOLSETS="context,repos,issues,pull_requests"
export GITHUB_PERSONAL_ACCESS_TOKEN GITHUB_TOOLSETS

exec github-mcp-server stdio

"""StS2 Advisor — game-state bridge package.

Observe-only. The bridge reads live state from the STS2MCP mod over HTTP and
asks a Claude Code CLI session for terse advice. It never sends a game action.
"""

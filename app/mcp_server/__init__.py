"""MCP server for My Meeting Notes (MMN-14).

``tools.py`` holds what the server can do as plain functions, ``auth.py`` who
may call it, and ``server.py`` the streamable-HTTP wiring at ``/mcp``. Nothing is imported
here, so the REST layer can read ``tools.TOOL_SPECS`` (the Settings page lists
the tools) without pulling in the MCP SDK.
"""

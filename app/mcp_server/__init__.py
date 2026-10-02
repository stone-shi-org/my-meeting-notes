"""MCP server for My Meeting Notes (MMN-14).

``tools.py`` holds what the server can do as plain functions, ``auth.py`` who
may call it, and ``server.py`` the streamable-HTTP wiring at ``/mcp``. Nothing is imported
here; ``tools.py`` itself never imports the MCP SDK, so the tools stay plain
functions the tests can call directly.
"""

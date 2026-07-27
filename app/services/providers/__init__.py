"""Calendar and email providers.

One module per backend (Google, Apple, Zoho, MCP), all behind the small protocol
in :mod:`base`. ``matching`` talks to exactly one thing here -- ``loader.load_for_user``
-- so adding a provider means writing a module and a registry entry, and touching
nothing in the matching pipeline.
"""

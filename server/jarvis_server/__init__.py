"""Project JARVIS online service (server side). Standard library only: no paid service, no third-party web framework.

This package is deliberately OUTSIDE src/jarvis so it is never bundled into the Windows client.
It provides accounts, consent records, rate limits, a chat endpoint, and data export/deletion. It does NOT contain an AI
model: until an operator connects a real trained model (see backends.py) the chat endpoint answers 503 "no model connected".
"""
__version__ = "0.1.0"

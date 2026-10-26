"""Personality engine: a controlled, inspectable behaviour profile applied as a TEMPLATE layer around replies.
It never changes model weights, never claims human feelings, and never alters the content of critical messages
(errors, refusals, permission and security notices, uncertainty). Pure Python; imports no ML or audio code."""
from jarvis.personality.engine import (CRITICAL_KINDS, MODES, TRAITS, PersonalityProfile, PersonalityStore, render)

__all__ = ["CRITICAL_KINDS", "MODES", "TRAITS", "PersonalityProfile", "PersonalityStore", "render"]

from jarvis.personality.persona import Persona, PersonaSettings, Turn  # noqa: E402
from jarvis.personality.styles import STYLES  # noqa: E402

__all__ += ["Persona", "PersonaSettings", "Turn", "STYLES"]

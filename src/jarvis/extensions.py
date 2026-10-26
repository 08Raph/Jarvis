"""All optional tool sets that plug into the permission-controlled executor (Prompt 7 developer tools, Prompt 8 monitoring/gaming/vision, plugins)."""
from __future__ import annotations


def all_specs(profile_dir=None):
    from jarvis import dev
    out = dev.code_specs() + dev.research_specs()
    from jarvis.monitor import tools as mon
    out += mon.specs()
    try:
        from jarvis.monitor import gaming
        out += gaming.specs()
    except ImportError:
        pass
    try:
        from jarvis.desktop import vision
        out += vision.specs()
    except ImportError:
        pass
    try:
        from jarvis import modes
        out += modes.specs()
    except ImportError:
        pass
    try:
        from jarvis.missions import tools as mission_tools
        out += mission_tools.specs()
    except ImportError:
        pass
    if profile_dir is not None:
        out += dev.plugin_specs(profile_dir)[0]
    return out

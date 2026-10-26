"""Prompt 7 tools: sandbox, error analysis, coding tools, documents and research, plus activated plugins."""


def code_specs():
    from jarvis.dev import code_tools
    return code_tools.specs()


def research_specs():
    from jarvis.dev import research
    return research.specs()


def plugin_specs(profile_dir):
    """(specs, problems) for ACTIVE plugins whose code still matches what the user approved."""
    from jarvis.dev.plugins import Store
    return Store(profile_dir).active_specs()


def all_specs(profile_dir=None):
    out = code_specs() + research_specs()
    if profile_dir is not None:
        out += plugin_specs(profile_dir)[0]
    return out

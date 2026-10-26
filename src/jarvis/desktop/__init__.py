"""Permission-controlled desktop control for the user's own Windows PC.

Layers (each one only talks to the next):
  model / CLI  ->  agent.py (parses ONE structured tool call; never runs code)
               ->  task.py  (act-observe-verify loop, preview, cancel, checkpoints, history)
               ->  executor.py (validate args, check permission, ask for confirmation, timeout, audit)
               ->  tools.py (fixed list of tools)  ->  backends (Windows ctypes/UIA, or the clearly-labelled simulator)
There is no shell tool and no "run this code" tool. See docs/DESKTOP_CONTROL.md.
"""

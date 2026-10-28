"""Test-only helper. The four adult-only styles are temporarily restricted in the product (styles.ADULT_MODES_RESTRICTED = True). Older test modules exercise the
DORMANT adult definitions (opt-in, minor switch-off, memory exclusion, specs, ...), so they import setUpModule/tearDownModule from here to lift the restriction
for that module only. The restriction itself is tested in test_p55_adult_restriction.py, which never imports this file."""
from jarvis.personality import styles

_saved = []


def setUpModule():
    _saved.append(styles.ADULT_MODES_RESTRICTED)
    styles.ADULT_MODES_RESTRICTED = False


def tearDownModule():
    styles.ADULT_MODES_RESTRICTED = _saved.pop() if _saved else True

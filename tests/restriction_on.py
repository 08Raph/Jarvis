"""Test-only helper. The restriction MECHANISM (styles.ADULT_MODES_RESTRICTED = True) is still tested, even though the shipped value is False since
2026-10-30: modules that test it import setUpModule/tearDownModule from here to switch it ON for that module only."""
from jarvis.personality import styles

_saved = []


def setUpModule():
    _saved.append(styles.ADULT_MODES_RESTRICTED)
    styles.ADULT_MODES_RESTRICTED = True


def tearDownModule():
    styles.ADULT_MODES_RESTRICTED = _saved.pop() if _saved else False

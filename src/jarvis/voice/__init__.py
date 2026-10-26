"""Voice layer: wake word, VAD, session controller, speech filtering. Hardware backends live in backends.py.

Everything except backends.py is pure logic driven by audio frames and an injectable clock, so it is tested
with fakes. Real microphone, speech recognition and synthesis accuracy are NOT verified by the test suite.
"""

"""Test helper, not a test: starts `jarvis selftest-worker` in a NEW process with a FAKE `llama_cpp` module whose behaviour is chosen by the first
argument. This lets the tests exercise the real supervisor against a real child process (timeouts, kills, abnormal exits) without any real model or
native runtime. Usage: python selftest_fake_worker.py MODE <arguments for jarvis.cli.main ...>"""
import os
import signal
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
MODE = sys.argv[1]
ARGS = sys.argv[2:]
if ARGS[:1] == ["--data-dir"]:
    Path(ARGS[1], "worker.pid").write_text(str(os.getpid()))        # lets a test confirm the process was really stopped


def die(how: str) -> None:
    if how == "kill" and sys.platform != "win32":
        os.kill(os.getpid(), signal.SIGKILL)          # what the OS does to a process that runs out of memory
    # An abnormal exit code, as after a crash. On Windows os._exit takes a C int, so the NTSTATUS 0xC0000005 (access violation) is passed as its signed
    # 32-bit value; the OS reports it back as 0xC0000005. (The unsigned literal raised OverflowError there, so the "crash" never happened.)
    os._exit(0xC0000005 - (1 << 32) if sys.platform == "win32" else 139)


class FakeLlama:
    def __init__(self, **kw):
        if MODE == "crash_load":
            die("exit")
        if MODE == "hang_load":
            time.sleep(3600)
        if MODE == "raise_load":
            raise ValueError("llama_model_load: error loading model from C:\\Users\\SECRETNAME\\models\\x.gguf")
        if MODE == "slow_load":
            time.sleep(0.3)
        if MODE == "ok_check_marker":      # copy what selftest.json said at the moment the model started loading
            Path(ARGS[1], "marker_seen.txt").write_text(Path(ARGS[1], "selftest.json").read_text(encoding="utf-8"), encoding="utf-8")

    def tokenize(self, b, add_bos=False):
        return list(b)

    def create_chat_completion(self, messages=None, stream=False, **kw):
        if MODE == "hang_gen":
            time.sleep(3600)
        if MODE == "crash_gen":
            die("exit")
        if MODE == "kill_gen":
            die("kill")
        if MODE == "raise_gen":
            raise RuntimeError('Failed to tokenize: text="' + "PRIVATE-PROMPT-TEXT " * 20 + '"')
        text = {"ok": "The answer is 4.", "empty": "", "invalid": "The answer is 7.", "control": "<|im_start|>4",
                "long": "4 " + "blah " * 80, "thinking": "<think>2+2 is 4</think>4", "wrong_and_right": "4 or 5"}.get(MODE, "The answer is 4.")
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"completion_tokens": max(1, len(text.split()))}}


if MODE == "silent_exit":
    raise SystemExit(0)                # ends normally without ever reporting a result
if MODE == "no_runtime":
    sys.modules["llama_cpp"] = None    # `import llama_cpp` now raises ImportError, as on a PC without the runtime
else:
    mod = types.ModuleType("llama_cpp")
    mod.Llama = FakeLlama
    mod.__version__ = "0.0.0"
    mod.llama_supports_gpu_offload = lambda: False
    sys.modules["llama_cpp"] = mod

if MODE == "crash_before_runtime":
    die("exit")

from jarvis.cli import main  # noqa: E402

raise SystemExit(main(ARGS))

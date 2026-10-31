"""Kokoro: a local neural text-to-speech engine, registered next to Windows SAPI through the same TTSEngine interface.

Runtime: the `kokoro-onnx` package (ONNX Runtime, CPU) plus `sounddevice` for output. Both are installed by the user with pip (see docs/STAGE6_5_KOKORO.md); Jarvis
does not bundle them. The model files are installed from the Voice page after an explicit confirmation (kokoro_files.py). After that, speech is generated on this PC
with no network, no account, no key and no fee. Nothing here imports a network library.

Honest scope:
* This engine has NOT been run against the real Kokoro model, real audio hardware or Windows in the authoring sandbox. The tests use fake models and a fake audio
  sink and prove the logic around them (lifecycle, cancel, errors), not that Kokoro produces good speech.
* Voice metadata: the voices file only gives each voice's id, so only the id is shown. Gender, accent and quality are not guessed from the id.
* Synthesis and playback are reported separately. "Playback completed" means every sample was handed to the output device without an error; it does not mean anyone heard it.
* Stop silences the output at once. A sentence that is already being computed cannot be interrupted (ONNX Runtime offers no cancel); it finishes in the background and is discarded."""
from __future__ import annotations

import importlib.util
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from jarvis.voice.engines import UNSPECIFIED, EngineError, TTSEngine, VoiceInfo, make_id
from jarvis.models.kokoro_files import FILES, KokoroModelError, KokoroModelStore, approx_total_bytes, human_size

PRESET_VOICES = ("af_heart", "af_aoede", "af_nicole", "am_onyx")      # the voices Jarvis offers first; only those present in the installed voices file are listed
# More English voices from the same Kokoro v1.0 voices file. They are offered ONLY if the installed file really contains them (a name the file lacks is simply not listed).
EXTRA_VOICES = ("af_bella", "af_sarah", "af_sky", "af_nova", "af_river", "af_jessica", "af_kore", "af_alloy",
                "am_adam", "am_michael", "am_echo", "am_eric", "am_liam", "am_fenrir", "am_puck", "am_santa",
                "bf_emma", "bf_isabella", "bf_alice", "bf_lily", "bm_george", "bm_lewis", "bm_daniel", "bm_fable")
BRITISH_PREFIXES = ("bf_", "bm_")                                    # the phonemiser needs "en-gb" for these (the naming convention of the Kokoro voices file)
DEFAULT_LANGUAGE = "en-us"                                           # phonemiser language passed to the model; the four presets are English voices
APPROX_SIZE = human_size(approx_total_bytes())                      # derived from the file table, so every message agrees
PACKAGES = (("kokoro_onnx", "kokoro-onnx"), ("sounddevice", "sounddevice"))
INSTALL_HINT = "pip install kokoro-onnx sounddevice"
CHUNK_CHARS = 220
PHASES = ("idle", "loading", "generating", "playing")


class KokoroError(EngineError):
    pass


def _safe(e: BaseException) -> str:
    try:
        from jarvis.brains.service import _safe as s
        return s(e, 160)
    except Exception:      # noqa: BLE001
        return type(e).__name__


def split_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Sentences, packed up to `limit` characters, so the first audio can start before the whole reply is synthesised. Very long sentences are cut at spaces."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+|\n+", text or "") if p and p.strip()]
    out: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            piece, p = p[:cut].strip(), p[cut:].strip()
            if piece:
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(piece)
        if not p:
            continue
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    return out


# ---------------- audio output ----------------
class SoundDeviceSink:
    """Plays mono float32 audio through `sounddevice` (PortAudio). write() blocks until the device accepted the block; abort() may be called from another
    thread to cut the sound at once. Closing releases the device."""

    def __init__(self, sd_module=None):
        self._sd = sd_module
        self._stream = None
        self._lock = threading.Lock()

    def _lib(self):
        if self._sd is None:
            try:
                import sounddevice as sd  # type: ignore
            except Exception as e:      # noqa: BLE001
                raise KokoroError("The audio output library 'sounddevice' (PortAudio) is not installed or could not start. Install it with: pip install sounddevice") from e
            self._sd = sd
        return self._sd

    def open(self, sample_rate: int) -> None:
        sd = self._lib()
        try:
            st = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
            st.start()
        except Exception as e:      # noqa: BLE001
            raise KokoroError(f"Jarvis could not open the audio output device ({_safe(e)}). Check the output device in Windows sound settings.") from e
        with self._lock:
            self._stream = st

    def write(self, samples: np.ndarray) -> None:
        st = self._stream
        if st is None:
            raise KokoroError("The audio output is not open.")
        try:
            st.write(np.ascontiguousarray(samples, dtype=np.float32).reshape(-1, 1))
        except Exception as e:      # noqa: BLE001
            raise KokoroError(f"Audio playback failed ({_safe(e)}).") from e

    def abort(self) -> None:
        with self._lock:
            st = self._stream
        if st is not None:
            try:
                st.abort()
            except Exception:      # noqa: BLE001
                pass

    def close(self, abort: bool = False) -> None:
        with self._lock:
            st, self._stream = self._stream, None
        if st is None:
            return
        try:
            (st.abort if abort else st.stop)()
        except Exception:      # noqa: BLE001
            pass
        try:
            st.close()
        except Exception:      # noqa: BLE001
            pass


def default_providers_probe() -> list[str]:
    import onnxruntime  # type: ignore
    return list(onnxruntime.get_available_providers())


def default_model_factory(model_path, voices_path):
    """Build the real model on the CPU only. When the installed kokoro-onnx offers `from_session`, Jarvis creates the ONNX Runtime session itself and asks for
    the CPU provider alone, so a GPU or cloud-service provider that happens to be installed (some builds list one) is never used. Otherwise it falls back
    to the package's own constructor with ONNX_PROVIDER pre-set to the CPU provider. NOT run against the real package in the authoring sandbox."""
    import onnxruntime as ort  # type: ignore
    from kokoro_onnx import Kokoro  # type: ignore
    make = getattr(Kokoro, "from_session", None)
    if callable(make):
        return make(ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"]), str(voices_path))
    os.environ.setdefault("ONNX_PROVIDER", "CPUExecutionProvider")
    return Kokoro(str(model_path), str(voices_path))


# ---------------- the loaded model (one copy) ----------------
class KokoroRuntime:
    """Holds the single loaded model. Loading is serialised by its own lock, so asking whether the model is loaded (the interface does this) never waits for a load
    in progress. The model is shared by every speech job, and unload() refuses while a job is using it."""

    def __init__(self, store: KokoroModelStore, factory: Callable | None = None, clock: Callable[[], float] = time.monotonic):
        self.store, self._factory, self._clock = store, factory, clock
        self._lock = threading.Lock()              # guards the fields below only; never held while loading
        self._load_lock = threading.Lock()         # one load at a time
        self._model = None
        self._users = 0
        self._closed = False
        self.infer_lock = threading.Lock()
        self.load_seconds: float | None = None
        self.loads = 0

    def loaded(self) -> bool:
        with self._lock:
            return self._model is not None

    def acquire(self):
        with self._lock:
            if self._closed:
                raise KokoroError("Speech output has been shut down.")
            if self._model is not None:
                self._users += 1
                return self._model
        with self._load_lock:
            with self._lock:
                if self._model is not None:
                    self._users += 1
                    return self._model
            model = self._load()                    # slow; no state lock is held
            with self._lock:
                if self._closed:
                    raise KokoroError("Speech output has been shut down.")
                self._model = model
                self._users += 1
                return model

    def release(self) -> None:
        with self._lock:
            self._users = max(0, self._users - 1)

    def unload(self) -> bool:
        with self._lock:
            if self._users:
                return False
            self._model = None
            return True

    def shutdown(self) -> None:
        """No model may be loaded or kept after this (a load still in progress is discarded when it ends)."""
        with self._lock:
            self._closed = True
        self.unload()

    def _load(self):
        t0 = self._clock()
        try:
            if self._factory is not None:
                model = self._factory(self.store.path(self.store.model_file), self.store.path(self.store.voices_file))
            else:
                model = default_model_factory(self.store.path(self.store.model_file), self.store.path(self.store.voices_file))
        except ImportError as e:
            raise KokoroError(f"The Kokoro runtime could not be imported ({_safe(e)}). Install it with: {INSTALL_HINT}") from e
        except MemoryError as e:
            raise KokoroError("There is not enough free memory to load the Kokoro model. Close other programs, or switch back to Windows speech.") from e
        except Exception as e:      # noqa: BLE001
            raise KokoroError(f"The Kokoro model could not be loaded ({_safe(e)}). The files may be damaged or not compatible: press Install / repair on the Voice page, "
                              "or switch back to Windows speech.") from e
        self.load_seconds, self.loads = self._clock() - t0, self.loads + 1
        return model


# ---------------- one speech job ----------------
class _Job:
    def __init__(self, text: str, voice: str, speed: float, volume: float):
        self.text, self.voice, self.speed, self.volume = text, voice, speed, volume
        self.cancel = threading.Event()
        self.phase = "loading"
        self.error = ""
        self.exit = None            # 0 = ran to the end, 1 = failed, None = running or cancelled
        self.chunks = self.samples = 0
        self.sample_rate = 0
        self.synthesis_ok = False
        self.playback_started = self.playback_ok = False
        self.t0 = time.monotonic()
        self.first_audio_seconds: float | None = None
        self.thread: threading.Thread | None = None
        self.finished = threading.Event()


class KokoroSpeaker:
    """Same shape as the other engines' speakers: speak() returns at once; is_speaking() / exit_code() report the job. Extra: phase, last_error, report()."""

    def __init__(self, engine: "KokoroEngine"):
        self.engine = engine
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._sink = None
        self._threads: list[threading.Thread] = []
        self._closed = False
        self.last_error = ""

    # ---- speaker interface ----
    def speak(self, text: str, rate: float, volume: float, voice: str | None) -> None:
        if self._closed or self.engine.closed:
            raise KokoroError("Speech output has been shut down.")
        ok, why = self.engine.available()
        if not ok:
            raise KokoroError(why)
        voice = voice or self.engine.default_voice()
        ids = self.engine.voice_ids()
        if voice not in ids:
            raise KokoroError(f"The voice '{voice}' is not in the installed Kokoro voices file, so it cannot be used.")
        self.stop()
        job = _Job(text, voice, max(0.5, min(2.0, float(rate))), max(0.0, min(1.0, float(volume))))
        t = threading.Thread(target=self._run, args=(job,), name="jarvis-kokoro-speak", daemon=True)
        job.thread = t
        with self._lock:
            self._job = job
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
            self.last_error = ""
        t.start()

    def stop(self) -> None:
        """Cut the sound now and cancel pending work. Does not wait for a sentence that is mid-computation (it is discarded when it finishes)."""
        with self._lock:
            job, sink = self._job, self._sink
        if job is not None and not job.finished.is_set():
            job.cancel.set()
        if sink is not None:
            sink.abort()

    def is_speaking(self) -> bool:
        job = self._job
        return job is not None and not job.finished.is_set()

    def exit_code(self) -> int | None:
        job = self._job
        return None if job is None or not job.finished.is_set() else job.exit

    @property
    def phase(self) -> str:
        job = self._job
        return "idle" if job is None or job.finished.is_set() else job.phase

    def report(self) -> dict:
        j = self._job
        if j is None:
            return {}
        return {"voice": j.voice, "chunks": j.chunks, "audio_seconds": round(j.samples / j.sample_rate, 2) if j.sample_rate else 0.0, "sample_rate": j.sample_rate,
                "synthesis_ok": j.synthesis_ok, "playback_started": j.playback_started, "playback_ok": j.playback_ok,
                "first_audio_seconds": None if j.first_audio_seconds is None else round(j.first_audio_seconds, 2),
                "load_seconds": None if self.engine.runtime.load_seconds is None else round(self.engine.runtime.load_seconds, 2), "error": j.error}

    def close(self, timeout: float = 5.0) -> None:
        """Shut down for good: cancel, wait for the workers, release the device. New speak() calls are refused afterwards."""
        self._closed = True
        self.stop()
        end = time.monotonic() + timeout
        for t in list(self._threads):
            if t is not threading.current_thread():
                t.join(max(0.0, end - time.monotonic()))
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()]

    def alive_threads(self) -> int:
        return sum(1 for t in self._threads if t.is_alive())

    # ---- worker ----
    def _fail(self, job: _Job, msg: str) -> None:
        job.error, job.exit = msg, 1
        self.last_error = msg

    def _run(self, job: _Job) -> None:
        sink = opened = None
        producer: threading.Thread | None = None
        model_held = False
        q: queue.Queue = queue.Queue(maxsize=2)
        try:
            try:
                model = self.engine.runtime.acquire()
                model_held = True
            except KokoroError as e:
                self._fail(job, str(e))
                return
            if job.cancel.is_set():
                return
            chunks = split_text(job.text)
            if not chunks:
                job.synthesis_ok = True                      # nothing to say: nothing was synthesised or played, and playback is not claimed
                job.exit = 0
                return
            job.phase = "generating"
            producer = threading.Thread(target=self._produce, args=(job, model, chunks, q), name="jarvis-kokoro-synth", daemon=True)
            with self._lock:
                self._threads.append(producer)
            producer.start()
            while True:
                if job.cancel.is_set():
                    return
                try:
                    kind, a, b = q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind == "error":
                    self._fail(job, a)
                    return
                if kind == "done":
                    break
                if sink is None:
                    sink = opened = self.engine.sink_factory()
                    with self._lock:
                        self._sink = sink
                    sink.open(b)
                    job.playback_started, job.phase, job.sample_rate = True, "playing", b
                    job.first_audio_seconds = time.monotonic() - job.t0
                block = max(1, int(b * 0.05))
                for i in range(0, len(a), block):
                    if job.cancel.is_set():
                        return
                    sink.write(a[i:i + block])
                job.samples += len(a)
            if job.cancel.is_set():
                return
            if sink is not None:
                sink.close(abort=False)                  # lets the device play what it still holds, then releases it
                sink = None
            job.synthesis_ok, job.playback_ok, job.exit = True, job.playback_started, 0
        except KokoroError as e:
            self._fail(job, str(e))
        except Exception as e:      # noqa: BLE001 - never let a worker die silently
            self._fail(job, f"Speech failed unexpectedly ({_safe(e)}).")
        finally:
            if job.exit != 0:
                job.cancel.set()                           # a failed or cancelled job must also stop its synthesis thread
            if sink is not None:
                sink.close(abort=True)
            with self._lock:
                if opened is not None and self._sink is opened:
                    self._sink = None
            if producer is not None:
                producer.join(10.0)
            if model_held:
                self.engine.runtime.release()
            job.phase = "idle"
            job.finished.set()

    def _produce(self, job: _Job, model, chunks: list[str], q: queue.Queue) -> None:
        def put(item) -> bool:
            while not job.cancel.is_set():
                try:
                    q.put(item, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False
        try:
            if job.voice not in set(getattr(model, "get_voices", lambda: [job.voice])()):
                put(("error", f"The loaded Kokoro model does not have the voice '{job.voice}'.", 0))
                return
            for c in chunks:
                if job.cancel.is_set():
                    return
                with self.engine.runtime.infer_lock:
                    audio, sr = model.create(c, voice=job.voice, speed=job.speed, lang=self.engine.language_for(job.voice))
                if job.cancel.is_set():
                    return
                a = np.asarray(audio, dtype=np.float32).reshape(-1)
                if a.size == 0 or not np.all(np.isfinite(a)):
                    put(("error", "Kokoro returned no usable audio for this text.", 0))
                    return
                job.chunks += 1
                if not put(("audio", np.clip(a * job.volume, -1.0, 1.0), int(sr))):
                    return
            put(("done", None, 0))
        except MemoryError:
            put(("error", "Kokoro ran out of memory while generating speech. Close other programs or switch back to Windows speech.", 0))
        except Exception as e:      # noqa: BLE001
            put(("error", f"Kokoro could not generate speech ({_safe(e)}).", 0))


# ---------------- the engine ----------------
class KokoroEngine(TTSEngine):
    id = "kokoro"
    title = "Kokoro (local neural voice)"
    fallback_ok = False              # a chosen Kokoro voice is never replaced by another engine behind the person's back
    requirements = ("Python packages 'kokoro-onnx' and 'sounddevice' (install: " + INSTALL_HINT + "), plus the Kokoro model files (about " + APPROX_SIZE + " in total) "
                    "downloaded from the Voice page after you confirm. Runs on the CPU; no GPU is required or enabled by Jarvis.")
    licence_note = "kokoro-onnx: MIT. Kokoro-82M model: Apache-2.0. The espeak-ng text analyser it installs is GPL-3.0. Jarvis bundles none of them."
    size_note = f"About {APPROX_SIZE} download (model about 310 MB, voices about 28 MB), then no internet is needed."

    def __init__(self, data_dir: str | Path, *, voice_ids: tuple[str, ...] = PRESET_VOICES, language: str = DEFAULT_LANGUAGE,
                 model_factory: Callable | None = None, sink_factory: Callable | None = None, find_spec: Callable = importlib.util.find_spec,
                 files=FILES, providers_probe: Callable[[], list[str]] | None = None):
        super().__init__()
        self.store = KokoroModelStore(data_dir, files)
        self.preset_ids = tuple(voice_ids)
        self.language = language
        self.sink_factory = sink_factory or SoundDeviceSink
        self._find_spec = find_spec
        self.runtime = KokoroRuntime(self.store, model_factory)
        self._providers: list[str] | None = None
        self._providers_probe = providers_probe or default_providers_probe
        self._probe_lock = threading.Lock()
        self.closed = False

    # ---- readiness ----
    def missing_packages(self) -> list[str]:
        out = []
        for mod, pip_name in PACKAGES:
            try:
                present = self._find_spec(mod) is not None
            except (ImportError, ValueError):
                present = False
            if not present:
                out.append(pip_name)
        return out

    def available(self) -> tuple[bool, str]:
        miss = self.missing_packages()
        if miss:
            return False, f"Kokoro needs the Python package(s) {', '.join(miss)}. Install with: {INSTALL_HINT}"
        st = self.store.status()
        if st["state"] != "ready":
            return False, {"missing": f"The Kokoro model files are not installed yet. Press Install / repair on the Voice page (about {APPROX_SIZE}, needs your confirmation).",
                           "incomplete": "The Kokoro model files are incomplete. Press Install / repair on the Voice page.",
                           "damaged": "The Kokoro model files look damaged (" + "; ".join(f"{n} {p}" for n, p in st["problems"].items()) + "). Press Install / repair on the Voice page."}[st["state"]]
        return True, ""

    def voice_ids(self) -> list[str]:
        """Voices in the installed voices file (cached by file size and time)."""
        try:
            return self.store.voice_ids()
        except KokoroModelError as e:
            raise KokoroError(f"The Kokoro voices file cannot be used: {e}.") from e

    def language_for(self, voice: str) -> str:
        return "en-gb" if str(voice).startswith(BRITISH_PREFIXES) and self.language == DEFAULT_LANGUAGE else self.language

    def offered_ids(self) -> tuple[str, ...]:
        return self.preset_ids + tuple(v for v in EXTRA_VOICES if v not in self.preset_ids) if self.preset_ids == PRESET_VOICES else self.preset_ids

    def default_voice(self) -> str:
        ids = self.voice_ids()
        return next((v for v in self.preset_ids if v in ids), ids[0])

    def list_voices(self) -> list[VoiceInfo]:
        ok, why = self.available()
        if not ok:
            raise KokoroError(why)
        self.probe()
        ids = self.voice_ids()
        return [VoiceInfo(id=make_id(self.id, v), engine=self.id, name=v, language="", language_label="", category=UNSPECIFIED, gender_reported="", age="")
                for v in self.offered_ids() if v in ids]

    def probe(self) -> None:
        """Ask ONNX Runtime which execution providers it offers (read-only; may take a moment, so it runs during discovery on a worker)."""
        with self._probe_lock:
            if self._providers is not None:
                return
            try:
                if self._find_spec("onnxruntime") is None:
                    return
                self._providers = list(self._providers_probe())
            except Exception:      # noqa: BLE001
                self._providers = []

    def status(self) -> dict:
        st = self.store.status()
        ids: list[str] = []
        if st["state"] == "ready":
            try:
                ids = self.voice_ids()
            except KokoroError:
                ids = []
        return {"packages_missing": self.missing_packages(), "install_hint": INSTALL_HINT, "model": st, "presets": list(self.preset_ids),
                "presets_present": [v for v in self.preset_ids if v in ids], "presets_absent": [v for v in self.preset_ids if ids and v not in ids],
                "voices_in_file": len(ids), "providers": self._providers, "loaded": self.runtime.loaded(), "load_seconds": self.runtime.load_seconds,
                "acceleration": "Jarvis asks ONNX Runtime for its CPU provider only and never turns on a GPU or cloud provider; the providers listed are just what this installation offers."}

    # ---- speaker ----
    def _make_speaker(self):
        return KokoroSpeaker(self)

    def describe(self) -> dict:
        d = super().describe()
        d.update(fallback_ok=False, status=self.status())
        return d

    def close(self) -> None:
        """Shut down for good: stop and join the workers, release the audio device, drop the model. Later speech is refused (no worker can start after shutdown)."""
        self.closed = True
        sp, self._spk = self._spk, None
        if sp is not None:
            sp.close()
        self.runtime.shutdown()

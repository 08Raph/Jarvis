"""The few things the helpers need from Windows itself: the speaker volume and mute switch, and the clipboard. Every script here is a fixed literal written in this package
(only validated numbers are put in it); no text from the user or from a webpage is ever placed in a command. On a system that is not Windows every call says so plainly."""
from __future__ import annotations

import subprocess
import sys


class HostUnavailable(RuntimeError):
    pass


_AUDIO = r'''
Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume { int f(); int g(); int h(); int i(); int SetMasterVolumeLevelScalar(float fLevel, System.Guid pguidEventContext); int j(); int GetMasterVolumeLevelScalar(out float pfLevel); int k(); int l(); int m(); int n(); int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, System.Guid pguidEventContext); int GetMute(out bool pbMute); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref System.Guid id, int clsCtx, int activationParams, out IAudioEndpointVolume aev); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class JarvisAudio {
  static IAudioEndpointVolume Vol() { var e = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator; IMMDevice d = null; e.GetDefaultAudioEndpoint(0, 1, out d); IAudioEndpointVolume v = null; var g = typeof(IAudioEndpointVolume).GUID; d.Activate(ref g, 23, 0, out v); return v; }
  public static bool Mute { get { bool m; Vol().GetMute(out m); return m; } set { Vol().SetMute(value, System.Guid.Empty); } }
  public static float Volume { get { float v; Vol().GetMasterVolumeLevelScalar(out v); return v; } set { Vol().SetMasterVolumeLevelScalar(value, System.Guid.Empty); } }
}
"@
'''


class Host:
    is_windows = sys.platform == "win32"

    def _ps(self, script: str, timeout: float = 25.0) -> str:
        if not self.is_windows:
            raise HostUnavailable("That only works on Windows.")
        try:
            p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True,
                               timeout=timeout, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise HostUnavailable("Windows PowerShell is not available on this PC.") from None
        except subprocess.TimeoutExpired:
            raise HostUnavailable("Windows took too long to answer.") from None
        if p.returncode != 0:
            raise HostUnavailable("Windows would not do that (" + ((p.stderr or "").strip().splitlines() or ["no detail"])[0][:120] + ").")
        return p.stdout or ""

    # sound
    def get_mute(self) -> bool:
        return self._ps(_AUDIO + "[JarvisAudio]::Mute").strip().lower() == "true"

    def set_mute(self, on: bool) -> bool:
        return self._ps(_AUDIO + f"[JarvisAudio]::Mute = ${'true' if on else 'false'}; [JarvisAudio]::Mute").strip().lower() == "true"

    def get_volume(self) -> int:
        return round(float(self._ps(_AUDIO + "[JarvisAudio]::Volume").strip().replace(",", ".")) * 100)

    def set_volume(self, percent: int) -> int:
        p = max(0, min(int(percent), 100))
        return round(float(self._ps(_AUDIO + f"[JarvisAudio]::Volume = {p / 100:.2f}; [JarvisAudio]::Volume").strip().replace(",", ".")) * 100)

    # clipboard
    def clip_get(self) -> str:
        return self._ps("Get-Clipboard -Raw").rstrip("\r\n")

    def clip_set(self, text: str) -> None:
        if not self.is_windows:
            raise HostUnavailable("That only works on Windows.")
        try:
            subprocess.run(["clip.exe"], input=b"\xff\xfe" + text.encode("utf-16-le"), timeout=10, shell=False, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            raise HostUnavailable("I couldn't write to the clipboard.") from None

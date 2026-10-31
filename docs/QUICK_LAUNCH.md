# Quick launch: open apps, games and websites

Say (after the wake phrase) or type things like:

* "open Fortnite", "launch Counter-Strike 2", "open Notepad", "open Discord"
* "open Instagram", "go to youtube.com", "search YouTube for lofi beats"
* "scan my PC" (refreshes the list), "what apps can you open"

## Turn it on
Safety page, **Quick launch**: tick "Let Jarvis open apps, games and websites when I ask". It is **off** until you do. By default Jarvis then asks
"Allow this once?" before every launch; untick "Ask me before each one" only if you want fully hands-free voice.

## What it can start
* Programs and Store apps found in the Start menu, shortcuts on your Desktop, your own app list.
* Epic Games Launcher games (read from the launcher's install records) and Steam games (read from Steam's library files). They are started through the launcher's own link
  (`com.epicgames.launcher://...`, `steam://rungameid/...`). Only those two kinds of link are ever accepted.
* Well-known websites and any `name.com`-style address, opened with `https://` in your default browser. Jarvis does not read the page or sign in for you.

## What it cannot do
* It does not type, click, read the screen or run commands. It only starts things you named. Installers are never started this way.
* Games with anti-cheat are only *started*; Jarvis never sends input to a game.
* Guest mode never launches anything. The emergency stop file blocks it.
* It is separate from the advanced desktop permissions (`jarvis desktop enable ...`); it cannot switch those on.

## Honesty
Every attempt (started, declined, failed) is written to the local action log shown on the Safety page. "Opened X" is said only when a matching window was seen;
otherwise Jarvis says it *asked Windows* to open it. This was tested with fake launcher files and a fake desktop; it has **not** been run on a real PC by the authoring sandbox.

## Folders, Settings pages and files ("open anything")

Besides programs, games and websites, quick launch can open: your personal folders (Downloads, Documents, Desktop, Pictures, Videos, Music, including OneDrive copies),
Windows Settings pages (only links starting with `ms-settings:`, from a fixed list), and ordinary files found by name in those personal folders (documents, pictures,
music, video). Programs and scripts (`.exe`, `.bat`, `.ps1`, `.msi`, `.lnk`, `.url`, ...) are never opened this way, symbolic links are not followed, and the file search is
bounded in size, depth and time. Each open still asks for confirmation while "confirm each" is on, is audit-logged, and honours the emergency STOP. Typing or clicking inside
other programs is NOT part of this; it needs the separate advanced desktop permissions.

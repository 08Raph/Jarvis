# Everyday abilities (offline)

Typed or spoken, instant, no model and no internet. Say "what can you do" for the list. Guests keep nothing on disk. Code: `src/jarvis/app/toolbox.py`, `lifestyle.py`, `hostctl.py`.

| Area | Examples |
|---|---|
| Sound | "mute", "unmute" (also "turn the sound off / back on"), "toggle mute", "set volume to 40", "volume up", "turn it down by 20", "what's the volume". Works even with computer control off. |
| Timers | "set a timer for 10 minutes", "set an alarm for 7:30 am", "remind me to call mom in 20 minutes", stopwatch, "list timers", "cancel all timers". Saved; one that came due while Jarvis was closed is announced on the next start. |
| Repeating reminders | "remind me every Monday at 9 am to water the plants", "every day at 8 pm", "every weekday", "every 2 hours". "list / stop repeating reminders". |
| Calendar | "add event dentist tomorrow at 3 pm" (15-minute heads-up), "put gym on my calendar Friday at 6 pm", "what's on tomorrow / my calendar", "what's my next event". |
| Habits | "I drank water", "log habit reading", "start tracking guitar", "my habits", "what's my water streak". |
| Clipboard | "start clipboard history" (last 20 copies, memory only, secrets skipped), "show clipboard history", "restore my previous copy", "save this as my address", "copy my address snippet". |
| Flashcards | "add flashcard capital of France = Paris", "quiz me", "the answer is Paris", "next card", "I don't know", "stop quiz". |
| Focus / meeting | "start focus mode for 45 minutes" (logs your time, "focus stats"), "start meeting mode" (mutes; "end meeting mode" brings the sound back). |
| Scenes | "movie mode", "work mode", "study mode", "bedtime", "game mode", "game mode for CS2"; make your own: "create scene gaming: open discord, open steam, set volume to 50". |
| Playtime | "I'm playing CS2", "I'm done playing", "how long did I play CS2 this week", "playtime". Started by you (or by game mode); Jarvis does not watch your processes. |
| PC alerts | "turn on PC alerts": battery 20% / 10%, drive under 10% free, memory over 92%. Each is said once. |
| Daily summary | "good morning", "my day": calendar, timers, habits still to do, notes, lists. |
| Maths and more | percentages, units, tip and bill split, binary/hex/roman, dice, coin, random, jokes, facts, quotes, world clocks, day counts. |

| Setup check | "check my setup" / "test Jarvis": tests sound, clipboard, timers, maths, system readings, the AI model, the app list and voice, and names what is broken. |
| Learning | After "I couldn't find X", say "it's Counter-Strike 2" and Jarvis remembers the nickname (only if the open works). "When I say cs two open Counter-Strike 2". "When I say see ess two it means CS2" fixes words the microphone mishears (spoken input only). "list corrections", "forget the nickname X". |
| Follow-ups | After a calendar, clock, days-until, playtime, streak or prime question (within 10 minutes): "and Friday?", "and in Paris?", "and new year?". |
| Scheduled scenes | "every weekday at 8 am run work mode", "run movie mode every day at 9 pm" (Jarvis must be running; a scene that was due while it was closed is skipped). |
| Calculators | "payment on a 20000 loan at 6% for 5 years", "if I save $200 a month at 5% for 10 years", "compound interest on 1000 at 5% for 10 years", "days between 2026-01-01 and 2026-03-01". Currency: "set rate USD to CAD 1.35", then "convert 100 USD to CAD" (only your rates; nothing is looked up). |
| Clipboard text tasks | "summarize my clipboard", "fix the grammar of what I copied", "translate my clipboard to French", "rewrite my clipboard more professionally" (uses the AI model; the copied text is treated only as data). |
| Files | "find the PDF I downloaded yesterday", "find my images", "find a file called invoice"; "what's taking up my space" (Downloads, Desktop, Documents, Pictures, Videos, Music; read-only). |
| Auto playtime | "turn on auto playtime" checks every 30 seconds for well-known games; add yours with "track program mygame.exe as My Game". "how's my PC doing" gives CPU, memory, drive and the biggest memory users (no graphics-card numbers). |
| Voice | "change wake word to hey computer", "speak slower / faster / normally". |

Not included (need the internet, so they stay off): weather, news, currency rates, sports scores.

The Account tab is a local Log in / Sign up screen (accounts live on this PC only; no address or server). Spoken lists read every step.

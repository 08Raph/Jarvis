# Personality styles (Prompt 15): user guide and privacy

A **style** changes how Jarvis words things: tone, humour, length, and (for the voice) speaking speed. It is never a permission.
Nothing in a style can approve an action, turn on a permission, switch a mode that hides tools, skip a confirmation or the emergency stop, or run a tool. The
personality code does not import the desktop executor, policy, missions or tools, and they do not import it (checked by a test).

**Normal is the default** on a fresh install and after any damaged settings file. "Return to Normal" is one click in the window (Chat page and Personality
page) or `jarvis persona normal`.

## Styles
| Style | Notes |
|---|---|
| Normal / Assistant | Default. Helpful, composed, friendly, honest about uncertainty. |
| Unhinged / Savage | **Adult opt-in.** Roasting, sarcasm, trash talk, strong language. Roast intensity and profanity are separate settings (profanity defaults to none). Your saved personal notes are not given to the model in this style. |
| Sexy / Flirty | **Adult opt-in.** Playful banter in casual chat; never explicit; stops when asked. Saved notes not given to the model. |
| Argumentative / Debate | Playful debate / devil's advocate. Told to concede to strong evidence and never give false advice to "win". |
| Motivation / Hype Coach | Energetic, practical steps; told not to shame, pressure or ignore pain/injury. |
| Romantic / Affectionate | **Adult opt-in.** Warm and cheesy role; told it is an AI, not possessive, not to foster dependency. Saved notes not given to the model. |
| Conspiracy Roleplay | Absurd FICTIONAL comedy. Every reply gets a visible "[Fiction: conspiracy comedy]" label added by the program, not left to the model. |
| Dramatic Expert Explainer | Dramatic explanations of medical/vehicle/technical topics; told it is not a professional. Replaces "Grok Doc". |
| Therapist-Style Support | Reflective listening. Not therapy, no diagnosis. |
| Language Tutor | Target language, level, corrections, vocabulary. Can judge written text only; pronunciation feedback is approximate unless a real scoring tool exists (none does). |
| Meditation / Calm | Soothing wording, speech at 0.8x the voice speed. Brief if you say you are driving. |
| Professional / Technical / Concise | The existing trait profiles, reused (not duplicated). |

Roleplay or specialised styles show "Roleplay mode: ..." in the window.

## Settings
Window: **Personality** page (style list, Apply, Return to Normal, Preview, intensity boxes, adult checkbox, reset). Command line: `jarvis persona show|list|set STYLE|normal|reset|preview STYLE|level NAME N|adult on --confirm-adult|adult off|option language|level|minutes|hands_busy VALUE`.
Settings live in `persona.json` in your profile folder, are validated on every load, and survive restarts. A damaged or invalid file is ignored as a whole: you get Normal, gentle intensities (roast 1, profanity 0, flirt 1), and the adult opt-in OFF, with a note. It never falls back to a stronger style.
Guest sessions read and write nothing.

## What happens automatically (safety, per message)
These are keyword checks on your own text, done locally; they are biased towards stopping the style. They are not a clinical or emergency classifier and can miss things.
* "stop", "that's enough", "too much", "go back to normal", "stop flirting" ... returns to Normal and saves that.
* Self-harm / crisis wording or an emergency (chest pain, fire, crash ...) replaces the style for that reply with a calm, serious prompt that points to emergency services or a crisis line (988 in the US/Canada is named as an example; use your local number). Roleplay styles do not resume afterwards.
* Saying you are under 18 turns the adult opt-in off and drops adult styles. The age is not stored.
* If a model reply contains an obvious "kill yourself"-type phrase in a non-Normal style, it is replaced. This is a last-line heuristic, not a guarantee.

## Privacy
Style text is built locally and sent only to the local model. `persona.json` holds settings only: no conversation text, no memories, no secrets. No network is used (tested with the network blocked). Preview shows the exact instructions the model gets and does not fabricate a sample reply.
Personality is prompt/config based. Nothing is trained, and model weights are never changed.

## Stopping a style (Prompt 16)
* **Reliable control:** send a message that is exactly `/normal` (also `/stop`, `/style off`). It is handled before the model is called, works in every style and is not kept in the conversation. In the window: **Return to Normal** (Chat and Personality pages) also cancels a reply being written and silences speech. In the console voice session: key **n**.
* Plain-language requests ("stop roasting me", "that's enough", "can you not be so mean", "go back to normal") are also recognised, but that is keyword matching and can miss unusual wording; use `/normal` or the button when it matters.
* A stop request outranks the selected style. A crisis or emergency message outranks everything, including "stay in character" text in the same message.

## Adult styles fail closed (Prompt 16)
Savage, Flirty and Romantic need the explicit opt-in, and the opt-in is switched OFF (and must be given again) when a message says you are under 18 or contains a cue that makes your age uncertain (for example "my parents won't let me", "my homework", "I'm in grade 9"). This can switch the opt-in off for an adult who happens to say such a thing; that is the intended direction. There is no age verification: the opt-in is a statement you make, nothing more. Invalid or damaged `persona.json` can never turn on an adult style or a stronger intensity (400 randomly corrupted files were tested).

## What the detectors can and cannot do (measured on a written corpus, see `tests/test_p16_completion.py`)
Caught in the corpus: ~45 stop phrasings, ~23 distress phrasings, ~25 emergency phrasings, ~18 under-18 statements. Not caught (tested as known limits): indirect or non-English wording ("I'm done with everything", "je veux mourir", "I just want the pain to stop"). Known false positives that drop a style or use the serious tone: "my career is on fire", "that is too much code", "I feel hopeless about my golf swing". These keyword checks are not a clinical or emergency service. If you or someone near you is in danger, call your local emergency number.

## Model readiness levels (Prompt 16)
The Status page and the Chat banner no longer say "ready" because a file exists. They show, in order: model file found; runtime installed; runtime loads; a recorded `jarvis selftest` loaded the model and produced text; real tool calling verified; offline operation verified; personality checked on the real model. The last three say **not verified** unless a report exists, and even then say what it does not prove.
`jarvis persona selfcheck` runs every style against the real model in a guest session (your settings and memory are untouched), checks the objective things (facts kept, program labels, `/normal` works, short Concise replies, several turns without drift) and saves every reply to `persona_validation.json` for you to read. It cannot judge tone.

## Voice in the window (Prompt 16)
Replies can be read aloud through Windows speech (Personality page, "Read replies aloud", **off by default**). Text is cleaned first (no code, paths, links or long identifiers), speed = your speed x the style's (Calm 0.8), crisis replies are spoken at normal speed. Stop, Return to Normal, the emergency stop and closing the window silence it. (Prompt 17 changed this: the window now has a Voice page with optional hands-free listening, see the next section; typed-chat speech is still off by default.) The Status page lists microphone library, devices, Vosk package, model folder and speech engine as **found / not found**, never "working", because none of it has been run on real hardware.

## Hands-free voice and personalities (Prompt 17)
With hands-free voice ON (Voice page), what you say goes through exactly the same assistant path as typing: the same selected style, the same safety overrides (stop phrases, crisis and emergency handling, under-18 fail-closed), the same label rules and the same memory policy. The reply is spoken at your speed times the style's speed (Calm slower) and code, paths and links are not read aloud. Saying "return to normal", "back to normal", "normal mode", "style off" or "stop roleplay" is turned into the exact `/normal` command, which is handled without the model; the Voice page's **Return to Normal** button does the same and also abandons the answer being produced and silences speech. A style changes wording only: a spoken instruction never grants a permission and cannot answer an approval dialog.

## Limits you should know
* Whether the real local model (Qwen) follows a style well, especially a small one, was **not tested** (no weights or runtime in the build environment); only what the model is told and what happens to its reply was tested.
* Speech recognition, speaker output, wake-word accuracy, latency, echo and interruption have never run on real audio hardware (Prompt 17 built the hands-free loop and tested it with fake audio only), and nothing here makes voice "real-time" or "natural".
* Vehicle control does not exist in Jarvis, so no style can or does issue vehicle commands.

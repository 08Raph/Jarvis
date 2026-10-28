"""Per-message safety signals for the personality layer (Prompt 15). Plain keyword/regex heuristics, deliberately biased
toward the SAFE direction (a false alarm drops a style; a miss would not). They are NOT a clinical or emergency classifier,
and they run on the user's own text locally; nothing is stored or sent."""
from __future__ import annotations

import re
from dataclasses import dataclass

_STOP = re.compile(
    r"\b(please stop\b(?!\s+(?:the|a|an|my|all|that\s+\w+ing)\b)|stop it|i said stop|just stop|quit it|lay off|back off|knock it off|cut it out|that'?s enough|enough (?:already|of this|now)|too much"
    r"|stop (?:roasting|insulting|flirting|joking|teasing|swearing|cursing|mocking|trolling|arguing|yelling|shouting|being (?:mean|rude|weird|sarcastic|creepy|funny|dramatic)"
    r"|the (?:act|roleplay|role play|bit|jokes?|persona|character|voice))"
    r"|tone it down|calm down|drop the (?:act|bit|persona|character|attitude|roleplay)|(?:turn|switch) (?:this|that|it) off|(?:turn|switch) off (?:this|that|it|the (?:style|mode|persona|character|roleplay|act))"
    r"|(?:go |get |come |switch |change |return )?(?:back )?to normal|be normal|normal mode|(?:talk|speak|act|answer|reply) normally|exit (?:the )?(?:character|roleplay|mode)"
    r"|(?:i )?(?:don'?t|do not) like (?:this|that|it)|not funny|you'?re (?:being )?(?:mean|rude|creepy)|(?:can|could) you not (?:be|do)"
    r"|(?:you'?re|this is|that is|that'?s|it'?s) making me (?:uncomfortable|upset|sad|anxious)|(?:i'?m|i am|i feel) (?:not comfortable|uncomfortable)|makes me uncomfortable)\b", re.I)
_DISTRESS = re.compile(
    r"\b(kill myself|killing myself|going to kill myself|want to die|wanna die|wish i (?:was|were) dead|end my life|end it all|ending it all|ending things|take my own life|suicide(?! squad)|suicidal\w*|self[- ]harm\w*"
    r"|hurt myself|harm myself|hurting myself|cut myself|cutting myself|no reason to live|(?:don'?t|do not) want to (?:live|be alive|be here|exist|wake up)"
    r"|can'?t (?:go on|do this anymore|take it anymore|keep going)|better off dead|better off without me|nobody would (?:care|miss me)|no one would (?:care|miss me)"
    r"|want to disappear|wish i (?:could )?(?:disappear|never (?:existed|woke))|panic attack|having a breakdown|feel hopeless|(?:i feel|i'?m|i am) (?:completely )?hopeless"
    r"|(?:i feel|i'?m feeling|feeling|i'?ve been feeling|i have been feeling|feels?) (?:so |really |completely |totally |utterly |just )?(?:hopeless|worthless|empty inside|numb|helpless|like a burden)|(?:i'?m|i am) (?:such )?a burden"
    r"|(?:don'?t|do not|can'?t|cannot) see (?:the|any) (?:point|reason) (?:in|of|to) (?:anything|living|going on|life|continuing|trying)|(?:thinking|think|thought|thoughts) (?:a lot )?about (?:dying|death|ending it|ending things|suicide|killing myself)"
    r"|(?:life|it) (?:isn'?t|is not|ain'?t) worth (?:living|it)|not worth living|(?:i )?hate myself|(?:i'?m|i am|feeling|been feeling) (?:so |really |very )?depressed|can'?t stop crying"
    r"|(?:no one|nobody) to talk to|i'?m (?:so )?(?:overwhelmed|not okay)|not okay right now|i'?m in (?:danger|trouble)|(?:is|are|keeps) (?:hitting|hurting|beating|threatening|abusing|stalking) me|(?:he|she|they)(?:'s| is| are)? (?:going to|gonna) (?:hurt|kill) me|being abused|abusing me|(?:he|she|they|my (?:partner|husband|wife|boyfriend|girlfriend|dad|mom|mum|stepdad)) (?:hit|hits|punched|beat|beats|choked|chokes|slapped) me|(?:scared|afraid|terrified) of my (?:partner|husband|wife|boyfriend|girlfriend|dad|mom|mum|stepdad))\b", re.I)
_EMERGENCY = re.compile(
    r"\b(chest pain|chest (?:tightness|pressure|hurts|is hurting|pain)|my chest hurts|pain in (?:my|his|her) chest|tight(?:ness)? in (?:my|his|her) chest|chest (?:is )?tight|can'?t breathe|cannot breathe|trouble breathing|difficulty breathing|can'?t catch (?:my|his|her) breath"
    r"|not breathing|stopped breathing|overdos\w*|took too many (?:pills|tablets)|took (?:a (?:bunch|lot|handful) of|all (?:my|the)) (?:pills|tablets|meds|medication)|severe bleeding|bleeding (?:a lot|heavily|badly|out)|won'?t stop bleeding|unconscious|passed out|not responding|isn'?t responding|unresponsive"
    r"|choking|having a (?:stroke|seizure|heart attack)|heart attack|(?:a|the|had|having|think .{0,12}) stroke\b(?! of)|(?:having|had|has|have|a) (?:a )?seizure\b(?! of)|anaphyla\w*|allergic reaction|swelling (?:of )?(?:the )?(?:face|throat|tongue)|call 911|call 112|call 999|emergency"
    r"|there'?s a fire|on fire|smoke (?:in|everywhere)|(?:car|bike|motorcycle) (?:accident|crash)|i crashed (?:my|the) car|been in an accident|someone is (?:hurt|injured)|poison\w*|gas leak|carbon monoxide|smell(?:s|ing)? gas"
    r"|brakes? (?:failed|are gone|aren'?t working|don'?t work|stopped working)|drowning|fell and can'?t (?:move|get up)|broken (?:neck|back)|gunshot|stabbed|(?:got|been|was|is|were|being|he'?s|she'?s) shot\b(?! (?:at|of|in the dark)))\b", re.I)
_WORDNUM = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
_MINOR = re.compile(
    r"\b(?:i(?:'m| am)|im)\s+(?:only |just |still )?(?:(\d{1,2})(?!\s*(?:%|percent|minutes?|mins?|times|days?|hours?|weeks?|months?|km|miles?|more|less|ish))(?:\s*(?:years?[- ]old|yo|y/o|yrs?))?|(" + _WORDNUM + r")(?:\s*(?:years?[- ]old|yo|y/o))?)\b"
    r"|\bi(?:'m| am) a (?:teen|teenager|minor|kid|child|student in (?:grade|year) \d+)\b"
    r"|\bi (?:just |recently )?turn(?:ed|s)? (\d{1,2})\b|\bmy (?:age|birthday)\b[^.?!]{0,20}?\b(\d{1,2})\b(?=\s*(?:years?|th|$|\.))"
    r"|\b(?:under ?18|underage|under-age|not (?:yet )?18|haven'?t turned 18|turn(?:ing)? 18 (?:next|soon|in)|not (?:old enough|an adult))\b"
    r"|\bin (?:middle|high|junior high|elementary|primary) school\b|\b(?:grade|year) (?:[5-9]|1[0-2]) (?:student|class)\b|\bi(?:'m| am) in (?:grade|year) (?:[5-9]|1[0-2])\b", re.I)
# Weaker cues that make age UNCERTAIN. For adult-only styles the program fails closed on these too (the opt-in is switched off and must be given again).
_UNCERTAIN_AGE = re.compile(
    r"\b(my (?:parents|mom|mum|dad) (?:won'?t|wouldn'?t|don'?t|said|say|will not) (?:let|allow)|my (?:parents|mom|mum|dad) (?:are|is) (?:strict|checking)|ground(?:ed)? (?:for|by)|my teacher|my homework|(?:after|before) school|school (?:tomorrow|today|night)|"
    r"sweet sixteen|prom|recess|sophomore|freshman|junior year|senior year|my curfew|babysit\w* me|i'?m (?:too )?young)\b", re.I)
_DRIVING = re.compile(r"\b(i'?m driving|while driving|i am driving|i'?m on the (?:road|highway|freeway)|behind the wheel|operating (?:a )?(?:machine|machinery|forklift|equipment|a crane|a car|a truck))\b", re.I)
_COMMANDS = {"/normal", "/stop", "/style off", "/style normal", "/persona normal", "/nostyle"}


def command(text: str) -> bool:
    """The deterministic, keyword-free stop control: the whole message is exactly one of the slash commands."""
    return (text or "").strip().lower() in _COMMANDS


@dataclass(frozen=True)
class Signals:
    stop: bool = False
    distress: bool = False
    emergency: bool = False
    minor: bool = False
    uncertain_age: bool = False
    driving: bool = False

    @property
    def any_serious(self) -> bool:
        return self.distress or self.emergency


_WORDS = {w: i + 1 for i, w in enumerate(_WORDNUM.split("|"))}


def classify(text: str) -> Signals:
    t = text or ""
    minor = False
    for m in _MINOR.finditer(t):
        nums = [g for g in m.groups() if g]
        if not nums:
            minor = True
            continue
        g = nums[0].lower()
        n = _WORDS.get(g)
        if n is None:
            n = int(g) if g.isdigit() else 0
        if n < 18:
            minor = True
    return Signals(stop=bool(_STOP.search(t)) or command(t), distress=bool(_DISTRESS.search(t)), emergency=bool(_EMERGENCY.search(t)),
                   minor=minor, uncertain_age=bool(_UNCERTAIN_AGE.search(t)), driving=bool(_DRIVING.search(t)))


_BAD_OUT = re.compile(r"\b(kill yourself|you should (?:just )?die|go (?:and )?die|hurt yourself|end your life|nobody would miss you|you deserve to die)\b", re.I)
SAFE_OUT = ("I won't say that. If you are struggling, please contact your local emergency number or a crisis line (for example 988 in the US and Canada), "
            "or someone you trust.")


def guard_reply(text: str) -> tuple[str, bool]:
    """Last-line output check for obviously harmful phrases a model might produce in an edgy style. Heuristic, not a guarantee."""
    if _BAD_OUT.search(text or ""):
        return SAFE_OUT, True
    return text, False

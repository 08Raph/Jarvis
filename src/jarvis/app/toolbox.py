"""Everyday helpers that work offline, typed or spoken: timers, alarms, reminders, stopwatch, calculator, unit conversions, dates and world clocks, dice and coins,
notes and lists, quick facts. Same idea as quick launch: only clear, whole sentences are understood (``handle`` returns None for everything else, and then the normal
assistant answers). Nothing here uses the network, the microphone or other programs; it only reads the clock and keeps one small file (``toolbox.json``) in the profile folder.

Timers, alarms and reminders are saved with the time they are due, so they survive closing Jarvis (one that came due while Jarvis was closed is announced on the next start).
``tick`` is called about once a second by the window and returns the messages that are due now. Guests keep nothing on disk."""
from __future__ import annotations

import ast
import json
import math
import operator
import os
import random
import re
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

STATE_FILE = "toolbox.json"
MAX_TIMERS = 30
MAX_NOTES = 200
MAX_LIST_ITEMS = 200

# ------------------------------------------------------------------------------ small helpers
_NUMWORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
             "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100}
_UNIT_SECS = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "m": 60, "hour": 3600, "hr": 3600, "h": 3600, "day": 86400}
_NUM = r"(?:\d+(?:\.\d+)?|an?|half|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety)"
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|s|m|h)"
_ONE_DUR = rf"(?:{_NUM}(?:\s+and\s+a\s+half)?\s*(?:an?\s+)?{_UNIT}\b|half\s+an?\s+(?:hour|minute)|an?\s+(?:hour|minute)\s+and\s+a\s+half)"
DUR_RE = re.compile(rf"\b{_ONE_DUR}(?:\s*(?:,|and)?\s*{_ONE_DUR})*", re.I)


def _num(tok: str) -> float | None:
    tok = tok.lower()
    if tok in ("a", "an"):
        return 1.0
    if tok == "half":
        return 0.5
    if tok in _NUMWORDS:
        return float(_NUMWORDS[tok])
    try:
        return float(tok)
    except ValueError:
        return None


def parse_duration(text: str) -> float | None:
    """'10 minutes', '1 hour 30 minutes', 'an hour and a half', 'half an hour', '90 sec', '1h30m' -> seconds."""
    t = text.lower().strip()
    if re.search(r"half\s+an?\s+hour", t):
        return 1800.0
    if re.search(r"half\s+an?\s+minute", t):
        return 30.0
    total, found = 0.0, False
    for m in re.finditer(rf"({_NUM})(\s+and\s+a\s+half)?\s*(?:an?\s+)?({_UNIT})\b", t):
        n = _num(m.group(1))
        if n is None:
            continue
        u = m.group(3).rstrip("s") if len(m.group(3)) > 1 else m.group(3)
        key = {"sec": "sec", "min": "min", "hr": "hr"}.get(u, u)
        secs = _UNIT_SECS.get(key) or _UNIT_SECS.get(u)
        if secs is None:
            continue
        total += (n + (0.5 if m.group(2) else 0)) * secs
        found = True
    m = re.search(r"an?\s+(hour|minute)\s+and\s+a\s+half", t)
    if m:
        total += 1.5 * (3600 if m.group(1) == "hour" else 60) - (3600 if m.group(1) == "hour" else 60)
    return total if found and total > 0 else None


def say_duration(secs: float) -> str:
    secs = int(round(secs))
    if secs < 1:
        return "less than a second"
    parts = []
    for name, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        n, secs = divmod(secs, size)
        if n:
            parts.append(f"{n} {name}{'' if n == 1 else 's'}")
    return " and ".join([", ".join(parts[:-1]), parts[-1]] if len(parts) > 2 else parts)


def say_time(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return d.strftime("%I:%M %p").lstrip("0")


def say_when(ts: float, now: float) -> str:
    d, n = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    if d.date() == n.date():
        return "today at " + say_time(ts)
    if d.date() == (n + timedelta(days=1)).date():
        return "tomorrow at " + say_time(ts)
    return d.strftime("%A %B %d at ").replace(" 0", " ") + say_time(ts)


def _fmt(x: float) -> str:
    if x != x or x in (float("inf"), float("-inf")):
        return "undefined"
    if abs(x - round(x)) < 1e-9 and abs(x) < 1e15:
        return f"{int(round(x)):,}"
    s = f"{x:,.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def clean(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "").strip())
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+jarvis[,.!]?\s*|jarvis[,.!]?\s*)", "", t, flags=re.I)
    t = re.sub(r"^(?:(?:please|kindly|can you|could you|would you|will you|i want you to|i need you to|i'd like you to|go ahead and)\s+)+", "", t, flags=re.I)
    t = re.sub(r"(?:\s+(?:for me|please|now|thanks|thank you))+[.!?]*$", "", t, flags=re.I)
    return t.strip(" ,.!?\"'")


# ------------------------------------------------------------------------------ calculator (no eval: an expression tree with a short list of allowed operations)
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_FUNCS = {"sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x), "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
          "tan": lambda x: math.tan(math.radians(x)), "log": math.log10, "ln": math.log, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
          "factorial": lambda x: math.factorial(int(x)) if x == int(x) and 0 <= x <= 170 else (_ for _ in ()).throw(ValueError("factorial needs a whole number from 0 to 170"))}
_CONST = {"pi": math.pi, "e": math.e}


def safe_eval(expr: str) -> float:
    if len(expr) > 200:
        raise ValueError("too long")
    tree = ast.parse(expr, mode="eval")

    def ev(n, depth=0):
        if depth > 40:
            raise ValueError("too deep")
        if isinstance(n, ast.Expression):
            return ev(n.body, depth + 1)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand, depth + 1)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left, depth + 1), ev(n.right, depth + 1)
            if isinstance(n.op, ast.Pow) and (abs(b) > 1000 or (abs(a) > 1e6 and abs(b) > 50)):
                raise ValueError("too large")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and len(n.args) == 1 and not n.keywords:
            return _FUNCS[n.func.id](ev(n.args[0], depth + 1))
        raise ValueError("not arithmetic")
    return ev(tree)


_WORD_OPS = [(r"\bmultiplied by\b|\btimes\b|\bx\b(?=\s*\d)", "*"), (r"\bdivided by\b|\bover\b", "/"), (r"\bplus\b|\band\b(?=\s*\d)", "+"), (r"\bminus\b|\btake away\b", "-"),
             (r"\bto the power of\b|\bto the\b(?=\s*\d)", "**"), (r"\bmod(?:ulo)?\b", "%"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3")]


def calc_text(s: str) -> str | None:
    """A reply for a sentence that is arithmetic, else None."""
    t = s.lower().strip().rstrip("?.! ")
    t = t.replace(",", "") if re.fullmatch(r"[\d,.\s+\-*/()^%xX]+", t) is None else t.replace(",", "")
    t = re.sub(r"^(?:what(?:'s| is)|whats|how much is|calculate|compute|work out|solve|evaluate|equals?)\s+", "", t)
    m = re.fullmatch(rf"(?:what\s+(?:is|'s)\s+)?({_NUM_RE})\s*(?:%|percent)\s+of\s+({_NUM_RE})", t)
    if m:
        return f"{_fmt(float(m.group(1)) / 100 * float(m.group(2)))}."
    m = re.fullmatch(rf"(?:what\s+percent(?:age)?\s+is\s+|what\s+is\s+)({_NUM_RE})\s+(?:of|out of)\s+({_NUM_RE})", t)
    if m and float(m.group(2)):
        return f"{_fmt(float(m.group(1)) / float(m.group(2)) * 100)} percent."
    m = re.fullmatch(rf"(?:the\s+)?(square root|cube root|factorial|half|double|triple|square|cube)\s+(?:of\s+)?({_NUM_RE})", t)
    if m:
        v, k = float(m.group(2)), m.group(1)
        try:
            r = {"square root": lambda: math.sqrt(v), "cube root": lambda: _FUNCS["cbrt"](v), "factorial": lambda: _FUNCS["factorial"](v), "half": lambda: v / 2, "double": lambda: v * 2,
                 "triple": lambda: v * 3, "square": lambda: v * v, "cube": lambda: v ** 3}[k]()
        except ValueError as e:
            return f"I can't do that: {e}." if "factorial" in str(e) else "That has no real answer."
        return f"{_fmt(r)}."
    t = re.sub(rf"({_NUM_RE})\s*(?:%|percent)\s+of\s+", r"(\1/100)*", t)
    t = re.sub(r"\bsquare root of\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", t)
    for pat, rep in _WORD_OPS:
        t = re.sub(pat, f" {rep} ", t)
    t = t.replace("^", "**").replace("×", "*").replace("÷", "/").replace("x", "*") if re.fullmatch(r"[\d\s.+\-*/()^%x×÷]+", t) else t.replace("^", "**").replace("×", "*").replace("÷", "/")
    if not re.search(r"\d", t) or not re.fullmatch(r"[\d\s.+\-*/()%a-z]+", t) or not re.search(r"[+\-*/%()]|\bsqrt\b|\bfactorial\b|\bln\b|\blog\b|\bpi\b", t):
        return None
    if re.search(r"[a-z]", re.sub(r"\b(?:sqrt|cbrt|sin|cos|tan|log|ln|abs|round|floor|ceil|factorial|pi|e)\b", "", t)):
        return None
    try:
        v = safe_eval(t.strip())
    except ZeroDivisionError:
        return "You can't divide by zero."
    except (ValueError, SyntaxError, OverflowError, TypeError, RecursionError):
        return None
    return f"{_fmt(v)}."


_NUM_RE = r"-?\d+(?:\.\d+)?"

# ------------------------------------------------------------------------------ unit conversion
_U = {}


def _unit(kind, names, factor):
    for n in names.split("|"):
        _U[n] = (kind, factor)


for _k, _n, _f in [
        ("len", "m|meter|meters|metre|metres", 1), ("len", "km|kilometer|kilometers|kilometre|kilometres", 1000), ("len", "cm|centimeter|centimeters|centimetre|centimetres", 0.01),
        ("len", "mm|millimeter|millimeters|millimetre|millimetres", 0.001), ("len", "mile|miles|mi", 1609.344), ("len", "yard|yards|yd", 0.9144), ("len", "foot|feet|ft", 0.3048),
        ("len", "inch|inches|in", 0.0254), ("len", "nautical mile|nautical miles|nmi", 1852),
        ("mass", "kg|kilogram|kilograms|kilo|kilos", 1), ("mass", "g|gram|grams", 0.001), ("mass", "mg|milligram|milligrams", 1e-6), ("mass", "lb|lbs|pound|pounds", 0.45359237),
        ("mass", "oz|ounce|ounces", 0.028349523125), ("mass", "stone|stones|st", 6.35029318), ("mass", "tonne|tonnes|metric ton|metric tons", 1000),
        ("vol", "l|liter|liters|litre|litres", 1), ("vol", "ml|milliliter|milliliters|millilitre|millilitres", 0.001), ("vol", "gallon|gallons|gal", 3.785411784),
        ("vol", "quart|quarts|qt", 0.946352946), ("vol", "pint|pints|pt", 0.473176473), ("vol", "cup|cups", 0.2365882365), ("vol", "tablespoon|tablespoons|tbsp", 0.01478676478),
        ("vol", "teaspoon|teaspoons|tsp", 0.00492892159), ("vol", "fluid ounce|fluid ounces|fl oz|floz", 0.0295735295625),
        ("speed", "mps|m/s|meters per second", 1), ("speed", "kph|kmh|km/h|kilometers per hour|kilometres per hour", 1 / 3.6), ("speed", "mph|miles per hour", 0.44704),
        ("speed", "knot|knots|kt", 0.514444),
        ("data", "byte|bytes", 1), ("data", "kb|kilobyte|kilobytes", 1024), ("data", "mb|megabyte|megabytes", 1024 ** 2), ("data", "gb|gigabyte|gigabytes", 1024 ** 3),
        ("data", "tb|terabyte|terabytes", 1024 ** 4), ("data", "bit|bits", 1 / 8),
        ("time", "second|seconds|sec|secs", 1), ("time", "minute|minutes|min|mins", 60), ("time", "hour|hours|hr|hrs", 3600), ("time", "day|days", 86400),
        ("time", "week|weeks", 604800), ("time", "year|years", 31557600),
        ("area", "sqm|square meter|square meters|square metre|square metres", 1), ("area", "sqft|square foot|square feet", 0.09290304), ("area", "acre|acres", 4046.8564224),
        ("area", "hectare|hectares|ha", 10000), ("area", "sqkm|square kilometer|square kilometers", 1e6), ("area", "square mile|square miles|sqmi", 2589988.110336)]:
    _unit(_k, _n, _f)
_TEMP = {"c": "c", "celsius": "c", "centigrade": "c", "°c": "c", "degrees celsius": "c", "f": "f", "fahrenheit": "f", "°f": "f", "degrees fahrenheit": "f", "k": "k", "kelvin": "k"}
_UNIT_NAMES = sorted(list(_U) + list(_TEMP), key=len, reverse=True)
_UNIT_PAT = "|".join(re.escape(u) for u in _UNIT_NAMES)
_UNIT_LABEL = {"len": "length", "mass": "weight", "vol": "volume", "speed": "speed", "data": "data size", "time": "time", "area": "area"}


def convert(value: float, a: str, b: str) -> tuple[float, str] | str:
    a, b = a.lower().strip(), b.lower().strip()
    if a in _TEMP and b in _TEMP:
        x, y = _TEMP[a], _TEMP[b]
        c = value if x == "c" else (value - 32) * 5 / 9 if x == "f" else value - 273.15
        r = c if y == "c" else c * 9 / 5 + 32 if y == "f" else c + 273.15
        if c < -273.15:
            return "That is below absolute zero."
        return r, b
    if a in _U and b in _U:
        (ka, fa), (kb, fb) = _U[a], _U[b]
        if ka != kb:
            return f"I can't convert {_UNIT_LABEL[ka]} to {_UNIT_LABEL[kb]}."
        return value * fa / fb, b
    return "I don't know one of those units."


def convert_text(t: str) -> str | None:
    m = (re.fullmatch(rf"(?:convert\s+|what(?:'s| is)\s+)?({_NUM_RE})\s*({_UNIT_PAT})\s+(?:to|in|into|as)\s+({_UNIT_PAT})", t, re.I)
         or re.fullmatch(rf"how many\s+({_UNIT_PAT})\s+(?:are |is )?(?:there )?(?:in|is)\s+(?:an?\s+|one\s+)?({_NUM_RE})?\s*({_UNIT_PAT})", t, re.I))
    if not m:
        m2 = re.fullmatch(rf"how many\s+({_UNIT_PAT})\s+(?:in|is|are in)\s+(?:an?\s+|one\s+)?({_UNIT_PAT})", t, re.I)
        if m2:
            return _say_conv(1.0, m2.group(2), m2.group(1))
        m3 = re.fullmatch(rf"how many\s+({_UNIT_PAT})\s+(?:is|are|in)\s+({_NUM_RE})\s*({_UNIT_PAT})", t, re.I)
        if m3:
            return _say_conv(float(m3.group(2)), m3.group(3), m3.group(1))
        return None
    g = m.groups()
    if g[1] is not None and re.fullmatch(_NUM_RE, g[1] or "x") is None:           # the "convert 5 miles to km" shape
        return _say_conv(float(g[0]), g[1], g[2])
    if re.fullmatch(_NUM_RE, g[0] or "x"):
        return _say_conv(float(g[0]), g[1], g[2])
    return _say_conv(float(g[1] or 1), g[2], g[0])


def _say_conv(v: float, a: str, b: str) -> str:
    r = convert(v, a, b)
    if isinstance(r, str):
        return r
    return f"{_fmt(v)} {a} is {_fmt(round(r[0], 4))} {b}."


# ------------------------------------------------------------------------------ dates, times, cities
CITIES = {"london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin", "madrid": "Europe/Madrid", "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
          "moscow": "Europe/Moscow", "istanbul": "Europe/Istanbul", "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "india": "Asia/Kolkata",
          "bangkok": "Asia/Bangkok", "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai", "tokyo": "Asia/Tokyo",
          "japan": "Asia/Tokyo", "seoul": "Asia/Seoul", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland",
          "new york": "America/New_York", "toronto": "America/Toronto", "chicago": "America/Chicago", "denver": "America/Denver", "los angeles": "America/Los_Angeles",
          "vancouver": "America/Vancouver", "mexico city": "America/Mexico_City", "sao paulo": "America/Sao_Paulo", "buenos aires": "America/Argentina/Buenos_Aires",
          "cairo": "Africa/Cairo", "lagos": "Africa/Lagos", "johannesburg": "Africa/Johannesburg", "nairobi": "Africa/Nairobi", "honolulu": "Pacific/Honolulu",
          "anchorage": "America/Anchorage", "utc": "UTC", "gmt": "UTC", "pst": "America/Los_Angeles", "est": "America/New_York", "cst": "America/Chicago", "mst": "America/Denver",
          "manila": "Asia/Manila", "jakarta": "Asia/Jakarta", "karachi": "Asia/Karachi", "tehran": "Asia/Tehran", "lisbon": "Europe/Lisbon", "dublin": "Europe/Dublin",
          "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo", "helsinki": "Europe/Helsinki", "warsaw": "Europe/Warsaw", "athens": "Europe/Athens", "kyiv": "Europe/Kyiv"}
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_HOLIDAYS = {"christmas": (12, 25), "christmas day": (12, 25), "new year": (1, 1), "new years": (1, 1), "new year's": (1, 1), "new year's day": (1, 1), "halloween": (10, 31),
             "valentine's day": (2, 14), "valentines day": (2, 14), "valentines": (2, 14), "canada day": (7, 1), "independence day": (7, 4), "the fourth of july": (7, 4),
             "boxing day": (12, 26), "new year's eve": (12, 31), "christmas eve": (12, 24), "april fools": (4, 1), "st patrick's day": (3, 17), "earth day": (4, 22)}


def parse_date(s: str, today: date) -> date | None:
    s = s.lower().strip().rstrip("?.!")
    s = re.sub(r"^(?:the\s+)?", "", s)
    if s in ("today",):
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, a, b) if a <= 12 else date(y, b, a)
        except ValueError:
            return None
    m = re.fullmatch(r"(?:(?P<m>[a-z]+)\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?|(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m2>[a-z]+))(?:,?\s+(?P<y>\d{4}))?", s)
    if m:
        mon = _MONTHS.get((m.group("m") or m.group("m2") or "")[:12]) or next((v for k, v in _MONTHS.items() if k.startswith((m.group("m") or m.group("m2") or "zz")[:3]) and len(m.group("m") or m.group("m2")) >= 3), None)
        day = int(m.group("d") or m.group("d2"))
        if mon is None:
            return None
        try:
            if m.group("y"):
                return date(int(m.group("y")), mon, day)
            d = date(today.year, mon, day)
            return d if d >= today else date(today.year + 1, mon, day)
        except ValueError:
            return None
    if s in _HOLIDAYS:
        mon, day = _HOLIDAYS[s]
        d = date(today.year, mon, day)
        return d if d >= today else date(today.year + 1, mon, day)
    return None


def _zone(name: str):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


# ------------------------------------------------------------------------------ the toolbox
_JOKES = ["Why don't scientists trust atoms? Because they make up everything.", "I told my computer I needed a break, and now it won't stop sending me vacation ads.",
          "Why did the developer go broke? Because he used up all his cache.", "There are only 10 kinds of people: those who understand binary and those who don't.",
          "Why was the math book sad? It had too many problems.", "I would tell you a UDP joke, but you might not get it.",
          "Why do programmers prefer dark mode? Because light attracts bugs.", "What do you call a fake noodle? An impasta.", "How does a computer get drunk? It takes screenshots.",
          "Why did the scarecrow win an award? He was outstanding in his field."]
_FACTS = ["Honey never spoils: edible honey has been found in ancient Egyptian tombs.", "A day on Venus is longer than its year.", "Octopuses have three hearts.",
          "Bananas are berries, but strawberries are not.", "There are more possible chess games than atoms in the observable universe.", "The shortest war in history lasted about 40 minutes.",
          "Sharks are older than trees.", "A group of flamingos is called a flamboyance.", "Wombat droppings are cube-shaped.", "Light from the Sun takes about eight minutes to reach Earth."]
_QUOTES = ["It always seems impossible until it's done. (Nelson Mandela)", "Well done is better than well said. (Benjamin Franklin)", "The best way out is always through. (Robert Frost)",
           "Do what you can, with what you have, where you are. (Theodore Roosevelt)", "Small steps every day add up to big results.", "You don't have to be great to start, but you have to start to be great. (Zig Ziglar)"]
_BALL = ["Yes, definitely.", "It is certain.", "Most likely.", "Signs point to yes.", "Ask again later.", "Better not tell you now.", "Don't count on it.", "My answer is no.", "Very doubtful.",
         "Outlook not so good."]
_LISTS = {"shopping": "shopping", "grocery": "shopping", "groceries": "shopping", "todo": "to-do", "to-do": "to-do", "to do": "to-do", "packing": "packing", "wish": "wish", "gift": "gift"}


class Toolbox:
    def __init__(self, profile_dir: Path, guest: bool = False, now=time.time, rng=None):
        self.dir, self.guest, self.now = Path(profile_dir), guest, now
        self.rng = rng or random.SystemRandom()
        self._mem: dict | None = None

    # ------------------------------------------------------------ state
    @property
    def file(self) -> Path:
        return self.dir / STATE_FILE

    def _state(self) -> dict:
        if self._mem is not None:
            return self._mem
        d = {"timers": [], "stopwatch": {"start": None, "elapsed": 0.0, "laps": []}, "notes": [], "lists": {}}
        if not self.guest:
            try:
                raw = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k in d:
                        if k in raw and type(raw[k]) is type(d[k]):
                            d[k] = raw[k]
            except (OSError, ValueError):
                pass
        self._mem = d
        return d

    def _save(self) -> None:
        if self.guest or self._mem is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._mem, f)
            os.replace(tmp, self.file)
        except OSError:
            pass

    # ------------------------------------------------------------ timers, alarms, reminders
    def add_timer(self, due: float, label: str, kind: str) -> dict:
        st = self._state()
        st["timers"] = [t for t in st["timers"] if isinstance(t, dict)][-(MAX_TIMERS - 1):]
        t = {"id": uuid.uuid4().hex[:6], "due": float(due), "label": label.strip()[:80], "kind": kind, "created": self.now()}
        st["timers"].append(t)
        self._save()
        return t

    def tick(self) -> list[str]:
        """Messages for everything that has come due. Cheap: no file is read after the first call."""
        st = self._state()
        if not st["timers"]:
            return []
        now = self.now()
        due = [t for t in st["timers"] if t["due"] <= now]
        if not due:
            return []
        st["timers"] = [t for t in st["timers"] if t["due"] > now]
        self._save()
        out = []
        for t in sorted(due, key=lambda x: x["due"]):
            late = now - t["due"] > 120
            what = {"timer": "Timer done", "alarm": "Alarm", "reminder": "Reminder"}.get(t["kind"], "Timer done")
            msg = f"{what}: {t['label']}." if t["label"] else (f"{what}." if t["kind"] != "alarm" else "Alarm! Time's up.")
            if t["kind"] == "reminder" and t["label"]:
                msg = f"Reminder: {t['label']}."
            out.append(msg + (f" (This was due at {say_time(t['due'])}, while Jarvis was not running.)" if late else ""))
        return out

    def _list_timers(self) -> str:
        st, now = self._state(), self.now()
        items = sorted([t for t in st["timers"] if t["due"] > now], key=lambda t: t["due"])
        if not items:
            return "You have no timers, alarms or reminders set."
        parts = []
        for t in items:
            kind = {"timer": "timer", "alarm": "alarm", "reminder": "reminder"}[t["kind"]]
            lab = f" for {t['label']}" if t["label"] and t["kind"] == "timer" else (f" to {t['label']}" if t["label"] else "")
            if t["kind"] == "timer":
                parts.append(f"{kind}{lab}: {say_duration(t['due'] - now)} left")
            else:
                parts.append(f"{kind}{lab}: {say_when(t['due'], now)}")
        return f"You have {len(items)}: " + "; ".join(parts) + "."

    def _cancel(self, which: str) -> str:
        st = self._state()
        kinds = {"timer": ("timer",), "timers": ("timer",), "alarm": ("alarm",), "alarms": ("alarm",), "reminder": ("reminder",), "reminders": ("reminder",)}.get(which, ("timer", "alarm", "reminder"))
        before = len(st["timers"])
        st["timers"] = [t for t in st["timers"] if t["kind"] not in kinds]
        self._save()
        n = before - len(st["timers"])
        return f"Cancelled {n} {which if which in ('timer', 'alarm', 'reminder') else 'item'}{'' if n == 1 else 's'}." if n else f"There was no {which.rstrip('s') if which != 'all' else 'timer, alarm or reminder'} to cancel."

    # ------------------------------------------------------------ stopwatch
    def _sw(self, action: str) -> str:
        sw, now = self._state()["stopwatch"], self.now()

        def elapsed():
            return sw["elapsed"] + (now - sw["start"] if sw["start"] else 0)
        if action == "start":
            if sw["start"]:
                return f"The stopwatch is already running ({say_duration(elapsed())})."
            sw["start"] = now
            self._save()
            return "Stopwatch started." if sw["elapsed"] == 0 else f"Stopwatch resumed from {say_duration(sw['elapsed'])}."
        if action == "stop":
            if not sw["start"]:
                return "The stopwatch isn't running."
            sw["elapsed"] = elapsed()
            sw["start"] = None
            self._save()
            return f"Stopped at {say_duration(sw['elapsed'])}."
        if action == "lap":
            if not sw["start"]:
                return "Start the stopwatch first."
            sw["laps"].append(elapsed())
            self._save()
            n = len(sw["laps"])
            prev = sw["laps"][-2] if n > 1 else 0
            return f"Lap {n}: {say_duration(elapsed())} total, {say_duration(sw['laps'][-1] - prev)} since the last lap."
        if action == "reset":
            sw.update(start=None, elapsed=0.0, laps=[])
            self._save()
            return "Stopwatch reset."
        t = elapsed()
        if not sw["start"] and t == 0:
            return "The stopwatch is at zero."
        return f"The stopwatch {'is at' if sw['start'] else 'stopped at'} {say_duration(t)}."

    # ------------------------------------------------------------ notes and lists
    def _note_add(self, text: str) -> str:
        st = self._state()
        st["notes"] = (st["notes"] + [{"t": self.now(), "text": text.strip()[:500]}])[-MAX_NOTES:]
        self._save()
        return "Noted: " + text.strip()[:200] + "."

    def _notes_show(self) -> str:
        ns = self._state()["notes"]
        if not ns:
            return "You have no notes."
        return f"You have {len(ns)} note{'' if len(ns) == 1 else 's'}: " + " ".join(f"{i}. {n['text']}" if re.search(r"[.!?]$", n['text']) else f"{i}. {n['text']}." for i, n in enumerate(ns[-10:], 1))

    def _list_name(self, raw: str) -> str:
        r = raw.lower().strip()
        return _LISTS.get(r, r if re.fullmatch(r"[a-z0-9 \-']{1,30}", r) else "shopping")

    def _list_add(self, item: str, name: str) -> str:
        lists = self._state()["lists"]
        cur = lists.setdefault(name, [])
        new = [i.strip() for i in re.split(r",|\band\b", item) if i.strip()]
        for i in new:
            if i.lower() not in [c.lower() for c in cur] and len(cur) < MAX_LIST_ITEMS:
                cur.append(i[:80])
        self._save()
        return f"Added {', '.join(new)} to your {name} list. It has {len(cur)} item{'' if len(cur) == 1 else 's'}."

    def _list_remove(self, item: str, name: str) -> str:
        cur = self._state()["lists"].get(name, [])
        keep = [c for c in cur if c.lower() != item.strip().lower()]
        if len(keep) == len(cur):
            return f"{item.strip()} isn't on your {name} list."
        self._state()["lists"][name] = keep
        self._save()
        return f"Removed {item.strip()} from your {name} list."

    def _list_show(self, name: str) -> str:
        cur = self._state()["lists"].get(name, [])
        return f"Your {name} list has {len(cur)} item{'' if len(cur) == 1 else 's'}: " + ", ".join(cur) + "." if cur else f"Your {name} list is empty."

    # ------------------------------------------------------------ the sentences
    def handle(self, text: str) -> str | None:
        """The reply, or None when this is not one of the helpers' sentences."""
        t = clean(text)
        if not t or len(t) > 300 or "\n" in t:
            return None
        low = t.lower()
        try:
            for fn in (self._timers_rules, self._stopwatch_rules, self._time_rules, self._random_rules, self._notes_rules, self._math_rules, self._misc_rules):
                r = fn(low, t)
                if r is not None:
                    return r
        except (ValueError, OverflowError, ZeroDivisionError, KeyError):
            return None
        return None

    def _timers_rules(self, low: str, t: str) -> str | None:
        now = self.now()
        if re.fullmatch(r"(?:what|which) (?:timers?|alarms?|reminders?)(?: do i have(?: set)?| are (?:set|running))?|(?:list|show|check)(?: all)?(?: my)?(?: the)? (?:timers?|alarms?|reminders?)|timers?(?: status)?|"
                        r"how (?:much time|long)(?: is)? (?:left|remaining)(?: on (?:my |the )?(?:timer|alarm))?|how long (?:until|till|before) (?:my |the )?(?:timer|alarm)(?: goes off| ends| rings)?|"
                        r"(?:what(?:'s| is) )?(?:my )?(?:next )?(?:timer|alarm)", low):
            return self._list_timers()
        m = re.fullmatch(r"(?:cancel|stop|delete|clear|remove|turn off|dismiss|end|kill)(?: all)?(?: of)?(?: my| the)?(?: (all))?(?: (timers?|alarms?|reminders?))?(?: for .+)?", low)
        if m and (m.group(2) or " all" in " " + low or low.endswith(("timer", "alarm", "reminder"))):
            return self._cancel(m.group(2) or "all")
        if re.search(r"\b(?:pomodoro|focus (?:session|timer))\b", low) and re.match(r"(?:start|begin|set|run)", low):
            self.add_timer(now + 1500, "focus session over, take a 5 minute break", "timer")
            return "Focus session started: 25 minutes. I'll tell you when it's time for a break."
        if re.search(r"\btake a (?:short |quick )?break\b", low) and re.match(r"(?:start|set|begin)", low):
            self.add_timer(now + 300, "break is over", "timer")
            return "Break timer set for 5 minutes."
        m = re.match(r"(?:remind me|set (?:a )?reminder)\s+(?:to |that |about )?(?P<rest>.+)$", low)
        if m:
            from jarvis.operator.tasks_ops import parse_when
            rest = m.group("rest")
            mm = re.match(r"(?P<when>(?:in |at |tomorrow|on |next )\S.*?)\s+to\s+(?P<what>.+)$", rest)
            if not mm:
                mm = re.match(r"(?P<what>.+?)\s+(?P<when>(?:in |at |tomorrow|on )\S.*)$", rest)
            if mm:
                due = parse_when(mm.group("when"), datetime.fromtimestamp(now))
                if due is None:
                    d = parse_duration(mm.group("when"))
                    due = now + d if d else None
                if due is None or due <= now:
                    return "I couldn't understand when. Try “remind me to call mom in 20 minutes” or “remind me at 5 pm to stretch”."
                what = re.sub(r"^(?:to|that|about)\s+", "", mm.group("what")).strip()
                self.add_timer(due, what, "reminder")
                return f"Okay, I'll remind you to {what} {say_when(due, now)}." if due - now > 3600 * 2 else f"Okay, I'll remind you to {what} in {say_duration(due - now)}."
            return None
        m = re.match(r"(?:set|create|make|start|add)?\s*(?:an?\s+)?alarm\s*(?:for|at)\s+(?P<when>.+)$|wake me (?:up )?(?:at|by)\s+(?P<when2>.+)$", low)
        if m:
            from jarvis.operator.tasks_ops import parse_when
            w = (m.group("when") or m.group("when2")).strip()
            due = parse_when(w if w.startswith(("at ", "tomorrow", "in ")) else "at " + w, datetime.fromtimestamp(now))
            if due is None:
                return "I couldn't understand that time. Try “set an alarm for 7:30 am”."
            label = re.sub(r"[^a-z ]", "", w).strip() if False else ""
            self.add_timer(due, label, "alarm")
            return f"Alarm set for {say_when(due, now)}."
        if re.search(r"\btimer\b", low) and re.match(r"(?:set|start|create|make|add|begin|put|give me)\b|(?:an?\s+)?\d|timer\b|(?:an?|half)\b", low):
            m = DUR_RE.search(low)
            if m:
                secs = parse_duration(m.group(0))
                if secs:
                    rest = (low[:m.start()] + " " + low[m.end():])
                    rest = re.sub(r"\b(?:set|start|create|make|add|begin|put|give me|a|an|the|my|timer|for|of|called|named|labelled|labeled|to|on|me|up|please|going)\b", " ", rest)
                    label = re.sub(r"\s+", " ", rest).strip()
                    if secs > 7 * 86400:
                        return "That's a long timer: I can do up to 7 days."
                    self.add_timer(now + secs, label, "timer")
                    return f"Timer set for {say_duration(secs)}" + (f" ({label})." if label else ".")
            return None
        m = re.fullmatch(r"(?:set|start|create|make|give me)?\s*(?:an?\s+)?(?:countdown|count down)(?: for| of| from)?\s*(.+)", low)
        if m:
            secs = parse_duration(m.group(1))
            if secs:
                self.add_timer(now + secs, "", "timer")
                return f"Countdown started: {say_duration(secs)}."
        return None

    def _stopwatch_rules(self, low: str, t: str) -> str | None:
        if re.fullmatch(r"(?:start|begin|resume|run)(?: the| a| my)? stopwatch|stopwatch start", low):
            return self._sw("start")
        if re.fullmatch(r"(?:stop|pause|end)(?: the| my)? stopwatch|stopwatch stop", low):
            return self._sw("stop")
        if re.fullmatch(r"(?:lap|stopwatch lap|(?:mark|take|record) a lap)", low):
            return self._sw("lap")
        if re.fullmatch(r"(?:reset|clear|restart|zero)(?: the| my)? stopwatch|stopwatch reset", low):
            return self._sw("reset")
        if re.fullmatch(r"(?:check|show|read|what(?:'s| is)(?: on)?)(?: the| my)? stopwatch(?: time)?|stopwatch(?: time| status)?|how long has (?:it|the stopwatch) been running", low):
            return self._sw("check")
        return None

    def _time_rules(self, low: str, t: str) -> str | None:
        nowdt = datetime.fromtimestamp(self.now())
        today = nowdt.date()
        if re.fullmatch(r"what(?:'s| is)? the time(?: now| right now)?|what time is it(?: now| right now)?|(?:tell me the )?time(?: please)?|current time|what time do you have", low):
            return f"It's {say_time(self.now())}."
        if re.fullmatch(r"what(?:'s| is)?(?: today's| the)? date(?: today)?|what is today's date|today's date|(?:what )?day is it(?: today)?|what(?:'s| is) today|what day is today|what(?:'s| is) the day today", low):
            return f"Today is {nowdt.strftime('%A, %B ')}{today.day}, {today.year}."
        if re.fullmatch(r"what year is it(?: now)?|what(?:'s| is) the year", low):
            return f"It's {today.year}."
        if re.fullmatch(r"what month is it(?: now)?", low):
            return f"It's {nowdt.strftime('%B')}."
        m = re.fullmatch(r"(?:what(?:'s| is)? the )?(?:current )?time (?:is it )?in (?P<c>[a-z .']+?)(?: right now| now)?|what time is it in (?P<c2>[a-z .']+?)(?: right now| now)?", low)
        if m:
            c = (m.group("c") or m.group("c2")).strip()
            zone = CITIES.get(c) or CITIES.get(c.replace("the ", ""))
            if not zone:
                return f"I don't have a clock for {c}. I know big cities such as London, Tokyo, New York, Sydney and Dubai."
            try:
                z = datetime.fromtimestamp(self.now(), _zone(zone))
            except Exception:  # noqa: BLE001 - a missing time-zone database
                return "This PC has no time-zone data, so I can't read other cities' clocks."
            diff = (z.utcoffset() - nowdt.astimezone().utcoffset()).total_seconds() / 3600
            day = "" if z.date() == today else (" tomorrow" if z.date() > today else " yesterday")
            return f"It's {z.strftime('%I:%M %p').lstrip('0')}{day} in {c.title()}" + (f" ({abs(diff):g} hour{'' if abs(diff) == 1 else 's'} {'ahead of' if diff > 0 else 'behind'} you)." if diff else " (the same time as you).")
        m = re.fullmatch(r"(?:how many )?days? (?:until|till|to|left until|left till|before) (?P<d>.+?)|how (?:long|far) (?:until|till|is it until|away is) (?P<d2>.+?)|how many days (?:are )?(?:there )?(?:until|till|left until) (?P<d3>.+?)|countdown to (?P<d4>.+)", low)
        if m:
            raw = next(g for g in m.groups() if g)
            d = parse_date(raw.replace("my birthday", "?"), today)
            if d is None:
                return None
            n = (d - today).days
            return f"{raw.strip().title() if raw.strip() in _HOLIDAYS else d.strftime('%B ') + str(d.day)} is {'today' if n == 0 else 'in ' + str(n) + ' day' + ('' if n == 1 else 's')}."
        m = re.fullmatch(r"(?:how many )?days? (?:since|ago was) (.+)|how long (?:has it been )?since (.+)", low)
        if m:
            d = parse_date(next(g for g in m.groups() if g), today)
            if d is None or d > today:
                return None
            n = (today - d).days
            return f"That was {n:,} day{'' if n == 1 else 's'} ago (about {n / 365.25:.1f} years)."
        m = re.fullmatch(r"what day (?:of the week )?(?:is|was|will be) (?:it on )?(.+)|what day of the week is (.+)", low)
        if m:
            d = parse_date(next(g for g in m.groups() if g), today)
            if d is None:
                return None
            return f"{d.strftime('%B ')}{d.day}, {d.year} {'is' if d >= today else 'was'} a {d.strftime('%A')}."
        m = re.fullmatch(r"how old (?:am i|would i be|is someone)(?: if i (?:was|were) born(?: on)?| if born(?: on)?)? (.+)|how old am i,? born (?:on )?(.+)|my age if i (?:was )?born (?:on )?(.+)", low)
        if m:
            d = parse_date(next(g for g in m.groups() if g), today)
            if d is None or d > today:
                return None
            y = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
            return f"You'd be {y} years old."
        m = re.fullmatch(r"(?:what(?:'s| is) )?(?:the )?(?:date|day) (?:in|after) (\d+) (days?|weeks?|months?)", low)
        if m:
            n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 30 if m.group(2).startswith("month") else 1)
            d = today + timedelta(days=n)
            return f"{n} days from today is {d.strftime('%A, %B ')}{d.day}, {d.year}."
        return None

    def _random_rules(self, low: str, t: str) -> str | None:
        r = self.rng
        if re.fullmatch(r"(?:flip|toss|spin)(?: a| the)? coin|heads or tails", low):
            return f"{r.choice(['Heads', 'Tails'])}."
        m = re.fullmatch(r"roll (?:an? |the )?(?:(\d+|one|two|three|four|five|six)\s*)?(?:d(\d+)|di(?:c)?e|dice)(?: (?:with )?(\d+) sides)?", low)
        if m:
            n = _num(m.group(1) or "1") or 1
            sides = int(m.group(2) or m.group(3) or 6)
            if not (1 <= n <= 20 and 2 <= sides <= 1000):
                return "I can roll up to 20 dice with 2 to 1000 sides."
            rolls = [r.randint(1, sides) for _ in range(int(n))]
            return f"You rolled {rolls[0]}." if len(rolls) == 1 else f"You rolled {', '.join(map(str, rolls))} (total {sum(rolls)})."
        m = re.fullmatch(r"roll (\d+)d(\d+)", low)
        if m:
            n, s = int(m.group(1)), int(m.group(2))
            if not (1 <= n <= 20 and 2 <= s <= 1000):
                return "I can roll up to 20 dice with 2 to 1000 sides."
            rolls = [r.randint(1, s) for _ in range(n)]
            return f"You rolled {', '.join(map(str, rolls))} (total {sum(rolls)})."
        m = re.fullmatch(r"(?:pick|choose|give me|generate)(?: me)?(?: a)?(?: random)? number(?: between| from)? (-?\d+)(?: and| to|-) (-?\d+)|random number(?: between| from)? (-?\d+)(?: and| to|-) (-?\d+)", low)
        if m:
            g = [int(x) for x in m.groups() if x is not None]
            lo, hi = min(g[:2]), max(g[:2])
            return f"{r.randint(lo, hi)}."
        if low in ("random number", "pick a number", "pick a random number"):
            return f"{r.randint(1, 100)} (from 1 to 100)."
        m = re.fullmatch(r"(?:pick|choose|decide|select)(?: one| something)?(?: of| from| between)? (.+)|(?:should i|which one,?) (.+)", low)
        if m:
            body = next(g for g in m.groups() if g)
            opts = [o.strip() for o in re.split(r",\s*(?:or\s+)?|\s+or\s+|\s+and\s+", body) if o.strip()]
            if len(opts) >= 2 and len(opts) <= 20 and all(len(o) < 60 for o in opts) and not re.search(r"\b(?:file|folder|code|number)\b", body):
                return f"I pick: {r.choice(opts)}."
            return None
        if re.fullmatch(r"(?:ask the )?magic 8[- ]?ball.*|shake the magic 8[- ]?ball", low):
            return r.choice(_BALL)
        if re.fullmatch(r"(?:tell me |say |give me )?(?:a |another )?joke|make me laugh", low):
            return r.choice(_JOKES)
        if re.fullmatch(r"(?:tell me |give me |say )?(?:a |another )?(?:fun |random |cool )?fact|(?:fun|random) fact", low):
            return r.choice(_FACTS)
        if re.fullmatch(r"(?:give me |tell me |say |share )?(?:a |another )?(?:motivational |inspirational |inspiring )?quote|motivate me|inspire me|cheer me up", low):
            return r.choice(_QUOTES)
        return None

    def _notes_rules(self, low: str, t: str) -> str | None:
        m = re.match(r"(?:take a note|make a note|note to self|note down|jot down|write down|note that|note)\s*[:,-]?\s*(?:that\s+)?(.+)$", t, re.I)
        if m and not re.match(r"(?:what|how|where|when|why|who)\b", m.group(1), re.I):
            return self._note_add(m.group(1))
        if re.fullmatch(r"(?:read|show|list|what(?:'s| are| is)?)(?: me)?(?: all)?(?: of)?(?: my| the)? notes?(?: do i have)?|(?:my )?notes", low):
            return self._notes_show()
        if re.fullmatch(r"(?:clear|delete|erase|remove)(?: all)?(?: of)?(?: my| the)? notes", low):
            n = len(self._state()["notes"])
            self._state()["notes"] = []
            self._save()
            return f"Deleted {n} note{'' if n == 1 else 's'}."
        m = re.fullmatch(r"(?:add|put)\s+(.+?)\s+(?:to|on|onto)\s+(?:my |the )?([a-z\- ]{2,30}?)\s*list", t, re.I)
        if m:
            return self._list_add(m.group(1), self._list_name(m.group(2)))
        m = re.fullmatch(r"(?:remove|delete|take|cross off|strike)\s+(.+?)\s+(?:from|off|off of|of)\s+(?:my |the )?([a-z\- ]{2,30}?)\s*list", t, re.I)
        if m:
            return self._list_remove(m.group(1), self._list_name(m.group(2)))
        m = re.fullmatch(r"(?:what(?:'s| is)? (?:on|in)|show|read|list|open)\s+(?:me )?(?:my |the )?([a-z\- ]{2,30}?)\s*list", t, re.I)
        if m and m.group(1).lower() not in ("task", "tasks", "process", "play", "running"):
            return self._list_show(self._list_name(m.group(1)))
        m = re.fullmatch(r"(?:clear|empty|delete|erase)\s+(?:my |the )?([a-z\- ]{2,30}?)\s*list", t, re.I)
        if m:
            name = self._list_name(m.group(1))
            self._state()["lists"][name] = []
            self._save()
            return f"Your {name} list is empty now."
        return None

    def _math_rules(self, low: str, t: str) -> str | None:
        r = convert_text(low)
        if r:
            return r
        m = re.fullmatch(r"(?:what(?:'s| is) )?(?:the )?tip (?:on|for) \$?(\d+(?:\.\d+)?)(?: at| with)? (\d+(?:\.\d+)?)\s*(?:%|percent)|(\d+(?:\.\d+)?)\s*(?:%|percent) tip (?:on|for) \$?(\d+(?:\.\d+)?)", low)
        if m:
            g = [x for x in m.groups() if x is not None]
            bill, pct = (float(g[0]), float(g[1])) if m.group(1) else (float(g[1]), float(g[0]))
            tip = bill * pct / 100
            return f"A {_fmt(pct)} percent tip on {_fmt(bill)} is {tip:,.2f}, for a total of {bill + tip:,.2f}."
        m = re.fullmatch(r"split (?:the bill of |a bill of |the bill |a |the )?\$?(\d+(?:\.\d+)?)(?: bill)? (?:between|among|by|ways|with) (\d+)(?: people| ways| of us)?(?: (?:with|plus|and)(?: a)? (\d+(?:\.\d+)?)\s*(?:%|percent) tip)?", low)
        if m and int(m.group(2)) > 0:
            bill, n, pct = float(m.group(1)), int(m.group(2)), float(m.group(3) or 0)
            total = bill * (1 + pct / 100)
            return f"Each of the {n} pays {total / n:,.2f}" + (f" (total {total:,.2f} with a {_fmt(pct)} percent tip)." if pct else ".")
        m = re.fullmatch(r"(?:what is |what's )?(?:the )?average of (.+)|(?:what is |what's )?(?:the )?(sum|total) of (.+)|(?:what is |what's )?(?:the )?(?:largest|biggest|max(?:imum)?|smallest|minimum|min) (?:of|number in) (.+)", low)
        if m:
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", low.split(" of ", 1)[-1] if " of " in low else low.split(" in ", 1)[-1])]
            if len(nums) < 2:
                return None
            if "average" in low:
                return f"The average is {_fmt(sum(nums) / len(nums))}."
            if "sum" in low or "total" in low:
                return f"The total is {_fmt(sum(nums))}."
            return f"The {'largest' if re.search('largest|biggest|max', low) else 'smallest'} is {_fmt(max(nums) if re.search('largest|biggest|max', low) else min(nums))}."
        m = re.fullmatch(r"is (\d{1,12}) (?:a )?prime(?: number)?", low)
        if m:
            n = int(m.group(1))
            if n < 2:
                return f"No, {n} is not prime."
            i = 2
            while i * i <= n:
                if n % i == 0:
                    return f"No, {n} is not prime ({i} × {n // i})."
                i += 1
            return f"Yes, {n} is prime."
        m = re.fullmatch(r"(?:convert |what is |what's )?(\d{1,15})(?: in| to| into| as) (binary|hex|hexadecimal|octal|roman(?: numerals?)?)", low)
        if m:
            n, kind = int(m.group(1)), m.group(2)
            if kind == "binary":
                return f"{n} in binary is {bin(n)[2:]}."
            if kind.startswith("hex"):
                return f"{n} in hexadecimal is {hex(n)[2:].upper()}."
            if kind == "octal":
                return f"{n} in octal is {oct(n)[2:]}."
            if not 1 <= n <= 3999:
                return "Roman numerals work for 1 to 3999."
            out, x = "", n
            for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
                while x >= v:
                    out, x = out + s, x - v
            return f"{n} in Roman numerals is {out}."
        m = re.fullmatch(r"(?:convert )?(?:binary )?([01]{2,40}) (?:from binary )?(?:to|in|into) (?:decimal|a number)|(?:convert |what is |what's )?(?:hex )?(?:0x)?([0-9a-f]{1,12}) (?:from hex(?:adecimal)? )?(?:to|in|into) decimal", low)
        if m:
            if m.group(1):
                return f"{m.group(1)} in decimal is {int(m.group(1), 2)}."
            return f"{m.group(2)} in decimal is {int(m.group(2), 16)}."
        return calc_text(t)

    def _misc_rules(self, low: str, t: str) -> str | None:
        m = re.fullmatch(r"(?:how do you |please )?spell (?:the word )?([a-z']{2,30})", low)
        if m:
            return f"{m.group(1)}: " + ", ".join(m.group(1).upper().replace("'", "apostrophe ").split(" ")[0] if False else c.upper() for c in m.group(1)) + "."
        if re.fullmatch(r"(?:what(?:'s| is) on my plate|what(?:'s| is) my day(?: like)?|what do i have (?:today|going on|coming up)|my day|daily summary|good morning|morning briefing|brief me)", low):
            nowdt = datetime.fromtimestamp(self.now())
            parts = [f"It's {say_time(self.now())} on {nowdt.strftime('%A, %B ')}{nowdt.day}."]
            parts.append(self._list_timers())
            st = self._state()
            if st["notes"]:
                parts.append(f"You have {len(st['notes'])} note{'' if len(st['notes']) == 1 else 's'}.")
            for name, items in st["lists"].items():
                if items:
                    parts.append(f"Your {name} list has {len(items)} item{'' if len(items) == 1 else 's'}.")
            return " ".join(parts)
        if re.fullmatch(r"(?:what can you (?:do|help (?:me )?with)|what are your (?:abilities|skills|features)|(?:list )?(?:your )?(?:abilities|skills)|help me|what tools do you have)", low):
            return HELP
        return None


HELP = ("Besides chatting, I can: set timers, alarms and reminders (“set a timer for 10 minutes”, “remind me to call mom at 5 pm”); run a stopwatch; do maths (“what is 15 percent of 240”), "
        "convert units (“convert 5 miles to kilometers”, “how many cups in a liter”); tell the time, the date and the time in other cities, count days until a date; roll dice, flip a coin, "
        "pick a number or choose between options; keep notes and lists (“add milk to my shopping list”); split a bill or work out a tip; tell jokes, facts and quotes; open apps, games "
        "and websites; and, once you turn on computer control, run your PC: windows, volume, files, backups, system checks and more.")

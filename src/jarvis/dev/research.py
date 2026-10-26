"""Documents and research.

OFFLINE (never touch the network; category files_read):
  read_document  list_sources  verify_quote  cite_sources  compare_sources
ONLINE (category research_online; need the 'web_research' internet feature AND your approval of each request):
  web_fetch  web_search

What is guaranteed, and what is not:
 * Every text Jarvis reads (file or web page) is stored as a SOURCE with an id, where it came from, when, and a SHA-256 of the bytes.
   `cite_sources` builds the source list from that store in code. The model never writes a citation; `verify_quote` checks that a quote
   really occurs in the stored text. A document Jarvis could not read (encrypted, scanned, unsupported, too large, missing library) is
   reported as such and is never summarised.
 * Text from files and pages is UNTRUSTED data. It is shown to the model inside <untrusted_data>; no tool here takes an instruction from it.
 * Private documents stay local. The only things that leave the machine are the URL or search words you approve. A request whose
   words contain a secret, or that repeats a passage of a document Jarvis has read, is refused before anything is sent.
 * Nothing here proves a web page is TRUE. `compare_sources` only points at places where stored sources seem to differ."""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from jarvis.desktop import schema as S
from jarvis.desktop.paths import PathMissing, PathRejected
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.dev.code_tools import sensitive
from jarvis.memory.secrets import redact
from jarvis.net.policy import NetworkDisabled

MAX_DOC_BYTES = 25 * 1024 * 1024
MAX_STORED_CHARS = 2_000_000
MAX_WEB_BYTES = 3 * 1024 * 1024
MAX_RETURN = 20_000
TEXT_SUFFIX = {".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".py", ".js", ".ts",
               ".c", ".h", ".cpp", ".java", ".cs", ".go", ".rs", ".sh", ".bat", ".ps1", ".sql", ".tex"}
HTML_SUFFIX = {".html", ".htm"}


# ------------------------------------------------------------------ source store
class Sources:
    """Stored sources live in the Jarvis profile folder (protected from the file and code tools)."""

    def __init__(self, env):
        self.dir = env.profile_dir / "research_sources"

    def add(self, kind: str, locator: str, title: str, text: str, raw_sha256: str, truncated: bool, note: str = "") -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        sid = "S" + hashlib.sha256((locator + "\x00" + raw_sha256).encode()).hexdigest()[:8]
        rec = {"id": sid, "kind": kind, "locator": locator, "title": title[:200], "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "sha256": raw_sha256, "chars": len(text), "truncated": truncated, "note": note, "text": text}
        (self.dir / f"{sid}.json").write_text(json.dumps(rec), encoding="utf-8")
        return rec

    def get(self, sid: str) -> dict:
        if not re.fullmatch(r"S[0-9a-f]{8}", sid or ""):
            raise PathMissing(f"'{sid}' is not a source id (they look like S1a2b3c4d)")
        f = self.dir / f"{sid}.json"
        if not f.is_file():
            raise PathMissing(f"no stored source {sid}; Jarvis cannot cite something it has not read")
        return json.loads(f.read_text(encoding="utf-8"))

    def all(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for f in sorted(self.dir.glob("S*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda r: r.get("retrieved_at", ""))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ text extraction (all offline)
class _Html(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP and tag != "head":
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and tag != "head" and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    p = _Html()
    try:
        p.feed(raw)
        p.close()
    except Exception:  # noqa: BLE001 - broken markup must not crash the tool
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r" ?\n ?", "\n", text)).strip()
    return text, re.sub(r"\s+", " ", p.title).strip()


class Unreadable(Exception):
    """The document exists but its text cannot be obtained; the message says exactly why."""


def _decode_text(raw: bytes) -> str:
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise Unreadable("the file looks like binary data, not text")
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    if b"\x00" in raw[:4096]:
        raise Unreadable("the file looks like binary data, not text")
    return raw.decode("utf-8", "replace")


def _docx_text(raw: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise Unreadable("not a valid .docx file (damaged or not a Word document)") from e
    names = z.namelist()
    if "word/document.xml" not in names:
        raise Unreadable("no word/document.xml inside: not a Word document")
    info = z.getinfo("word/document.xml")
    if info.file_size > 40 * 1024 * 1024 or (info.compress_size and info.file_size / info.compress_size > 200):
        raise Unreadable("the document's text part is suspiciously large for its size (possible zip bomb); not opened")
    if any(n.startswith("EncryptedPackage") or n == "EncryptionInfo" for n in names):
        raise Unreadable("the document is password-protected")
    xml = z.read("word/document.xml")
    if b"<!DOCTYPE" in xml[:2000] or b"<!ENTITY" in xml:
        raise Unreadable("the document XML declares entities/DTDs; refused (a safe parser is required for that)")
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise Unreadable(f"the document XML is damaged ({e})") from e
    paras = []
    for p in root.iter(ns + "p"):
        parts = []
        for el in p.iter():
            if el.tag == ns + "t" and el.text:
                parts.append(el.text)
            elif el.tag == ns + "tab":
                parts.append("\t")
            elif el.tag == ns + "br":
                parts.append("\n")
        paras.append("".join(parts))
    return "\n".join(paras).strip()


def _pdf_text(raw: bytes) -> str:
    try:
        import pypdf  # optional: pip install "jarvis[docs]"
    except ImportError as e:
        raise Unreadable("reading PDFs needs the optional 'pypdf' package, which is not installed (pip install pypdf). Nothing was read.") from e
    try:
        rd = pypdf.PdfReader(io.BytesIO(raw))
        if rd.is_encrypted:
            try:
                ok = rd.decrypt("")
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                raise Unreadable("the PDF is encrypted/password-protected; Jarvis cannot read it")
        if len(rd.pages) > 2000:
            raise Unreadable(f"the PDF has {len(rd.pages)} pages; the limit is 2000")
        parts = []
        for i, pg in enumerate(rd.pages, 1):
            parts.append(f"[page {i}]\n" + (pg.extract_text() or ""))
    except Unreadable:
        raise
    except Exception as e:  # noqa: BLE001 - damaged PDFs raise many different things
        raise Unreadable(f"the PDF could not be parsed ({type(e).__name__}); it may be damaged") from e
    text = "\n\n".join(parts)
    if len(re.sub(r"\[page \d+\]|\s", "", text)) < 20:
        raise Unreadable("the PDF has no extractable text (it may be a scan of images). Jarvis does not do OCR, so nothing was read.")
    return text


def extract(raw: bytes, name: str, content_type: str = "") -> tuple[str, str]:
    """(text, title) from bytes, or raises Unreadable with the exact reason."""
    suf = Path(name.split("?")[0]).suffix.lower()
    ct = (content_type or "").lower()
    if suf == ".pdf" or ct == "application/pdf" or raw[:5] == b"%PDF-":
        return _pdf_text(raw), ""
    if suf == ".docx" or ct.endswith("wordprocessingml.document"):
        return _docx_text(raw), ""
    if suf in HTML_SUFFIX or ct in ("text/html", "application/xhtml+xml"):
        return html_to_text(_decode_text(raw))
    if suf in TEXT_SUFFIX or ct.startswith("text/") or ct in ("application/json", "application/xml"):
        return _decode_text(raw), ""
    if suf in (".doc", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf", ".epub", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".exe", ".dll"):
        raise Unreadable(f"'{suf}' files are not supported (supported: text/code, .html, .docx, and .pdf with the optional pypdf package)")
    return _decode_text(raw), ""


# ------------------------------------------------------------------ private-data guard (outgoing text)
def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9_']+", _norm(s))


def guard_outgoing(env, text: str, what: str) -> None:
    """Refuse to send `text` (a URL or search words) if it holds a secret or repeats a passage of a document/source Jarvis has read."""
    if redact(text) != text:
        raise PathRejected(f"the {what} contains something that looks like a secret (key, token, password or card number); nothing was sent")
    w = _words(urllib.parse.unquote_plus(text))
    if len(w) >= 6:
        windows = {" ".join(w[i:i + 6]) for i in range(len(w) - 5)}
        for src in Sources(env).all():
            if src["kind"] != "document":
                continue
            body = " ".join(_words(src["text"]))
            if any(win in body for win in windows):
                raise PathRejected(f"the {what} repeats a passage of your private document {src['id']} ({src['locator']}); documents stay on this PC, so nothing was sent")
        try:                                                 # Prompt 8: text recently read from the user's screen is private too
            from jarvis.desktop import vision
            screen = " ".join(_words(" ".join(vision.recent_screen_text(env))))
        except Exception:  # noqa: BLE001 - no vision memory: nothing more to compare
            screen = ""
        if screen and any(win in screen for win in windows):
            raise PathRejected(f"the {what} repeats text that was recently read from your screen; screen contents stay on this PC, so nothing was sent")


def _config(env) -> dict:
    f = env.profile_dir / "research_config.json"
    try:
        c = json.loads(f.read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ offline tools
def _doc_path(env, raw: str) -> Path:
    last = None
    for paths in (env.paths, env.code_paths):
        try:
            p = paths.resolve(raw, must_exist=True)
            break
        except PathRejected as e:
            last = e
    else:
        raise last
    why = sensitive(p)
    if why:
        raise PathRejected(why)
    if not p.is_file():
        raise PathRejected(f"{p} is not a file")
    return p


def h_read_document(a, env) -> Outcome:
    p = _doc_path(env, a["path"])
    size = p.stat().st_size
    if size > MAX_DOC_BYTES:
        return Outcome(False, f"{p.name} is {size // (1 << 20)} MB; the limit is {MAX_DOC_BYTES >> 20} MB, so it was NOT read", verified=True, evidence="file size checked")
    raw = p.read_bytes()
    try:
        text, title = extract(raw, p.name)
    except Unreadable as e:
        return Outcome(False, f"{p.name} could not be read: {e}", verified=True, evidence="extraction was attempted and failed; no content was obtained",
                       data={"readable": False, "reason": str(e)})
    if not text.strip():
        return Outcome(False, f"{p.name} contains no text", verified=True, evidence="extraction returned nothing", data={"readable": False, "reason": "empty"})
    trunc = len(text) > MAX_STORED_CHARS
    rec = Sources(env).add("document", str(p), title or p.name, text[:MAX_STORED_CHARS], hashlib.sha256(raw).hexdigest(), trunc)
    s, n = a["start"], a["max_chars"]
    chunk = text[s:s + n]
    more = s + n < len(rec["text"])
    return Outcome(True, f"read {p.name} as source {rec['id']}: {len(text)} characters" + (f" (stored only the first {MAX_STORED_CHARS})" if trunc else "") +
                   (f"; showing characters {s}-{s + len(chunk)}, more follows (start={s + n})" if more or s else ""),
                   verified=True, evidence="parsed the file's bytes now; the stored copy is what verify_quote checks against",
                   data={"source": rec["id"], "sha256": rec["sha256"], "chars": len(text), "shown": [s, s + len(chunk)], "more": more}, untrusted_text=chunk)


def h_list_sources(a, env) -> Outcome:
    rows = Sources(env).all()
    lines = [f"{r['id']} [{r['kind']}] {r['title']} - {r['locator']} (read {r['retrieved_at']}, {r['chars']} chars)" for r in rows]
    return Outcome(True, f"{len(rows)} stored source(s)", verified=True, evidence="listed Jarvis' own store", data={"count": len(rows)}, untrusted_text="\n".join(lines))


def h_verify_quote(a, env) -> Outcome:
    src = Sources(env).get(a["source"])
    q = _norm(a["quote"])
    if len(q) < 8:
        raise S.ArgError("the quote is too short to check (8 characters minimum)")
    body = _norm(src["text"])
    exact = a["quote"] in src["text"]
    found = q in body
    if found:
        i = body.find(q)
        ctx = body[max(0, i - 80): i + len(q) + 80]
        return Outcome(True, f"quote FOUND in {src['id']} ({'exact' if exact else 'after ignoring case, spacing and curly quotes'})", verified=True,
                       evidence="searched the stored text of that source", data={"found": True, "exact": exact, "source": src["id"]}, untrusted_text="..." + ctx + "...")
    return Outcome(False, f"quote NOT FOUND in {src['id']}: do not present it as a quotation from that source", verified=True,
                   evidence="searched the stored text of that source", data={"found": False, "source": src["id"]})


def h_cite_sources(a, env) -> Outcome:
    st = Sources(env)
    lines, ids = [], []
    for sid in a["sources"]:
        r = st.get(sid)
        ids.append(sid)
        kind = "web page" if r["kind"] == "web" else "local document"
        lines.append(f"[{sid}] {r['title']} - {r['locator']} ({kind}; read {r['retrieved_at']}; SHA-256 {r['sha256'][:16]})")
    return Outcome(True, f"source list for {len(ids)} stored source(s), generated from Jarvis' records (not written by the model)", verified=True,
                   evidence="built from the store", data={"sources": ids}, untrusted_text="\n".join(lines))


_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?")


def h_compare_sources(a, env) -> Outcome:
    st, topic = Sources(env), _norm(a["topic"])
    per, lines = {}, []
    for sid in a["sources"]:
        r = st.get(sid)
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", r["text"]) if topic in _norm(s)][:4]
        nums = {n.replace(",", "") for s in sents for n in _NUM.findall(s)}
        per[sid] = (sents, nums)
        lines.append(f"[{sid}] " + (" | ".join(x[:240] for x in sents) if sents else "(topic not mentioned)"))
    with_text = {k: v for k, v in per.items() if v[0]}
    allnums = [v[1] for v in with_text.values() if v[1]]
    differ = any(a - b and b - a for i, a in enumerate(allnums) for b in allnums[i + 1:])
    msg = (f"the sources mention '{a['topic']}' with DIFFERENT figures: " + "; ".join(f"{k}: {sorted(v[1])}" for k, v in with_text.items())) if differ else (
        f"{len(with_text)} of {len(per)} source(s) mention '{a['topic']}'; no differing figures found in the matched sentences (this does not prove they agree)")
    return Outcome(True, msg + ". Heuristic: it compares numbers in sentences containing the topic; read the passages yourself.", verified=True,
                   evidence="text comparison of stored sources", data={"possible_conflict": differ, "mentioning": list(with_text)}, untrusted_text="\n".join(lines))


# ------------------------------------------------------------------ online tools
HEADERS = {"User-Agent": "Jarvis-local-assistant/0.5 (research tool; no personal data)", "Accept": "text/html,text/plain,application/pdf;q=0.8,*/*;q=0.1"}


def _offline_outcome(env, why: str = "") -> Outcome | None:
    if env.net is None or not env.net.is_enabled("web_research"):
        return Outcome(False, "Web research is OFF, so nothing was requested. Offline tools (read_document, search_code, ...) still work. "
                              "To allow it: jarvis net enable web_research", verified=True, evidence="the internet feature is disabled; no connection was attempted",
                       data={"offline": True})
    return None


def _clean_url(raw: str) -> str:
    u = urllib.parse.urlsplit(raw.strip())
    if u.scheme != "https" or not u.hostname:
        raise PathRejected("only https:// web addresses are allowed")
    if u.username or u.password:
        raise PathRejected("addresses with a user name or password are not allowed")
    if u.port not in (None, 443):
        raise PathRejected("only the standard https port is allowed")
    return urllib.parse.urlunsplit((u.scheme, u.netloc.lower(), u.path or "/", u.query, ""))


def _bind_fetch(a, env):
    url = _clean_url(a["url"])
    guard_outgoing(env, url, "web address")
    host = urllib.parse.urlsplit(url).hostname
    eff = [f"sends ONE request to {host} for: {url}", "the site (and your network) can see this address and your IP address; no file contents are sent",
           "the page is stored locally as a source and its text is treated as untrusted data"]
    return dict(a, url=url), eff, [url]


def h_web_fetch(a, env) -> Outcome:
    off = _offline_outcome(env)
    if off:
        return off
    url = a["url"]
    try:
        resp = env.net.open("web_research", url, timeout=20, headers=HEADERS, require_public=True)
        ctype, final = resp.headers.get_content_type(), resp.geturl()
        chunks, total = [], 0
        while True:
            env.stop.check()
            b = resp.read(65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > MAX_WEB_BYTES:
                break
        resp.close()
    except NetworkDisabled as e:
        return Outcome(False, f"refused: {e}", verified=True, evidence="the network policy refused before/while connecting")
    except OSError as e:
        code = getattr(e, "code", None)
        return Outcome(False, f"the page could not be fetched ({'HTTP ' + str(code) if code else type(e).__name__}); nothing was read, so nothing can be cited",
                       verified=True, evidence="the request failed", data={"reachable": False, "http_status": code})
    raw = b"".join(chunks)
    truncated = len(raw) > MAX_WEB_BYTES
    raw = raw[:MAX_WEB_BYTES]
    try:
        text, title = extract(raw, urllib.parse.urlsplit(final).path, ctype)
    except Unreadable as e:
        return Outcome(False, f"fetched {final} but could not read it: {e}", verified=True, evidence="the response arrived; extraction failed", data={"reachable": True, "readable": False})
    if not text.strip():
        return Outcome(False, f"fetched {final} but it contains no readable text (it may need JavaScript, which Jarvis does not run)", verified=True,
                       evidence="the response arrived; no text", data={"reachable": True, "readable": False})
    rec = Sources(env).add("web", final, title or final, text[:MAX_STORED_CHARS], hashlib.sha256(raw).hexdigest(), truncated or len(text) > MAX_STORED_CHARS,
                           note=f"content-type {ctype}")
    s, n = a["start"], a["max_chars"]
    return Outcome(True, f"fetched {final} as source {rec['id']} ({len(text)} characters" + (", cut at the size limit" if rec["truncated"] else "") + ")",
                   verified=True, evidence="HTTP response received now and its bytes hashed",
                   data={"source": rec["id"], "url": final, "sha256": rec["sha256"], "chars": len(text), "more": s + n < len(text)}, untrusted_text=text[s:s + n])


def _search_url(env, a) -> tuple[str, str, bool]:
    q = urllib.parse.quote_plus(a["query"])
    if a["provider"] == "wikipedia":
        return (f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&srlimit={a['max_results']}&format=json&utf8=1", "en.wikipedia.org", True)
    base = str(_config(env).get("searxng_url", "")).rstrip("/")
    if not base:
        raise PathRejected("no SearXNG instance is configured (jarvis research set-searxng https://your.instance)")
    u = urllib.parse.urlsplit(base)
    if u.scheme != "https" or not u.hostname:
        raise PathRejected("the SearXNG address must be https://")
    return (f"{base}/search?q={q}&format=json", u.hostname, False)


def _bind_search(a, env):
    guard_outgoing(env, a["query"], "search words")
    if len(a["query"].strip()) < 2:
        raise S.ArgError("the search words are too short")
    url, host, _ = _search_url(env, a)
    eff = [f"sends the search words \"{a['query']}\" to {host}", "the service (and your network) can see these words and your IP address; no file contents are sent",
           "results are leads, not sources: fetch a page with web_fetch before relying on it"]
    return a, eff, [url]


def h_web_search(a, env) -> Outcome:
    off = _offline_outcome(env)
    if off:
        return off
    url, host, public = _search_url(env, a)
    try:
        resp = env.net.open("web_research", url, timeout=20, headers={**HEADERS, "Accept": "application/json"}, require_public=public)
        raw = resp.read(MAX_WEB_BYTES)
        resp.close()
    except NetworkDisabled as e:
        return Outcome(False, f"refused: {e}", verified=True, evidence="the network policy refused")
    except OSError as e:
        return Outcome(False, f"the search service could not be reached ({getattr(e, 'code', None) or type(e).__name__}); no results, nothing to cite",
                       verified=True, evidence="the request failed", data={"reachable": False})
    try:
        j = json.loads(raw.decode("utf-8", "replace"))
        rows = []
        if a["provider"] == "wikipedia":
            for r in j["query"]["search"][:a["max_results"]]:
                t = str(r["title"])
                rows.append((t, "https://en.wikipedia.org/wiki/" + urllib.parse.quote(t.replace(" ", "_")), html.unescape(re.sub(r"<[^>]+>", "", str(r.get("snippet", ""))))))
        else:
            for r in j["results"][:a["max_results"]]:
                u = str(r.get("url", ""))
                if u.startswith("https://"):
                    rows.append((str(r.get("title", u)), u, str(r.get("content", ""))[:300]))
    except (ValueError, KeyError, TypeError, AttributeError):
        return Outcome(False, "the search service answered in a form Jarvis could not read; no results", verified=True, evidence="the reply was parsed and rejected")
    lines = [f"{i}. {t} - {u}\n   {sn[:240]}" for i, (t, u, sn) in enumerate(rows, 1)]
    return Outcome(bool(rows), f"{len(rows)} result(s) from {host} for '{a['query'][:60]}' (search results, not yet read pages)" if rows else f"{host} returned no results",
                   verified=True, evidence="HTTP response received and parsed now", data={"results": len(rows), "urls": [u for _, u, _ in rows]}, untrusted_text="\n".join(lines))


# ------------------------------------------------------------------ registry
SID = Arg("source", "str", "a stored source id such as S1a2b3c4d", max_len=20, no_newlines=True)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("read_document", "OFFLINE: read a text/code/.html/.docx/.pdf file in an allowed or project folder and store it as a citable source", "files_read",
                 S.OBSERVE, (Arg("path", "str", max_len=500, no_newlines=True), Arg("start", "int", required=False, default=0, min=0, max=100_000_000),
                             Arg("max_chars", "int", required=False, default=8000, min=100, max=MAX_RETURN)), h_read_document, timeout=60, untrusted_output=True),
        ToolSpec("list_sources", "OFFLINE: list the sources Jarvis has read and stored", "files_read", S.OBSERVE, (), h_list_sources, untrusted_output=True),
        ToolSpec("verify_quote", "OFFLINE: check that a quotation really occurs in a stored source", "files_read", S.OBSERVE,
                 (SID, Arg("quote", "str", max_len=2000)), h_verify_quote, untrusted_output=True),
        ToolSpec("cite_sources", "OFFLINE: build the source list for stored sources (generated from records, never written by the model)", "files_read", S.OBSERVE,
                 (Arg("sources", "list[str]", max_len=20, max_items=20),), h_cite_sources, untrusted_output=True),
        ToolSpec("compare_sources", "OFFLINE: show how stored sources talk about a topic and flag differing figures (heuristic)", "files_read", S.OBSERVE,
                 (Arg("sources", "list[str]", max_len=20, max_items=10), Arg("topic", "str", max_len=100, no_newlines=True)), h_compare_sources, untrusted_output=True),
        ToolSpec("web_fetch", "ONLINE: fetch one public https web page or PDF and store it as a citable source (needs internet + web_research + your approval)",
                 "research_online", S.NETWORK, (Arg("url", "str", max_len=500, no_newlines=True), Arg("start", "int", required=False, default=0, min=0, max=100_000_000),
                                                 Arg("max_chars", "int", required=False, default=8000, min=100, max=MAX_RETURN)), h_web_fetch, timeout=60,
                 untrusted_output=True, bind=_bind_fetch),
        ToolSpec("web_search", "ONLINE: search Wikipedia or your own SearXNG instance (needs internet + web_research + your approval)", "research_online", S.NETWORK,
                 (Arg("query", "str", max_len=200, no_newlines=True), Arg("provider", "str", required=False, default="wikipedia", choices=("wikipedia", "searxng")),
                  Arg("max_results", "int", required=False, default=5, min=1, max=10)), h_web_search, timeout=60, untrusted_output=True, bind=_bind_search),
    ]

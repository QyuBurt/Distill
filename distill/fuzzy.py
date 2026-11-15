"""Typo-tolerant search helpers (standard library only, so nothing extra to bundle).

* `fold`: lower case, no accents, no punctuation: 'Pokémon Legends: Z-A' -> 'pokemon legends z a'.
* `tokens_match` / `fuzzy_match`: every typed word must match a word of the text, exactly, as a prefix, or within a
  small edit distance (a typo, a swapped pair of letters).
* `query_variants`: other spellings of a query to try when a site finds nothing for the first.
* `History`: what was searched and found before; feeds the suggestion list and spelling fixes.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path

from . import paths

HISTORY_FILE = paths.state_dir() / "distill_search_history.json"
HISTORY_MAX = 600

_PUNCT = re.compile(r"[^\w\s]|_")
_SPACES = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def fold(text: str) -> str:
    """Comparison form: no accents, case or punctuation, '&' read as 'and'. 'Pokémon Legends: Z-A' -> 'pokemon legends z a'."""
    text = strip_accents(text).casefold().replace("&", " and ")
    return _SPACES.sub(" ", _PUNCT.sub(" ", text)).strip()


def merge_letters(words: list[str]) -> list[str]:
    """Runs of single letters become one word ('z a' -> 'za'): 'Z-A', 'Z A' and 'ZA' then compare equal. A lone letter
    ('Final Fantasy X') stays as it is."""
    out: list[str] = []
    run: list[str] = []
    for w in [*words, ""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if run:
            out.append("".join(run))
            run = []
        if w:
            out.append(w)
    return out


def edit_distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein distance (a swap of neighbours counts once), or limit + 1 when it is more than `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def allowed_typos(word: str) -> int:
    """How many letters may be wrong: none in a short word (too many other words are one letter away), one in a
    medium one, two in a long one."""
    n = len(word)
    return 0 if n <= 3 else 1 if n <= 7 else 2


def word_matches(want: str, have: str) -> bool:
    """One typed word against one word of the text: inside it (or equal, or its start), or a typo of it / of its start."""
    if want in have:  # equal, a start (still typing) or a middle part: what plain substring search always found
        return True
    limit = allowed_typos(want)
    if not limit:
        return False
    if edit_distance(want, have, limit) <= limit:
        return True
    # a half-typed word with a typo in it: compare with the same-length start of the text's word
    return len(have) > len(want) and edit_distance(want, have[:len(want)], limit) <= limit


def tokens_match(query_words: list[str], text_words: list[str]) -> bool:
    return all(any(word_matches(q, t) for t in text_words) for q in query_words)


def fuzzy_match(query: str, *texts: str) -> bool:
    """True when every word typed matches some word in the texts (see `word_matches`). Punctuation, accents and case
    never matter; 'z-a', 'za' and 'Z A' are the same."""
    q = merge_letters(fold(query).split())
    if not q:
        return True
    have = merge_letters(fold(" ".join(texts)).split())
    # 'pokemon legends za' typed apart or together: also try the words glued, for names like 'Spider-Man' / 'spiderman'
    if tokens_match(q, have):
        return True
    glued = fold(" ".join(texts)).replace(" ", "")
    return len(q) > 1 and fold(query).replace(" ", "") in glued


def similarity(a: str, b: str) -> float:
    """0..1 how alike two names are once folded (used to rank suggestions)."""
    import difflib
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0
    return difflib.SequenceMatcher(None, fa, fb).ratio()


# --- other spellings of a query --------------------------------------------------------------------------
def query_variants(query: str, known: list[str] | None = None, limit: int = 4) -> list[str]:
    """Spellings to try, best guess first, never the query itself: the folded text ('pokemon legends z a'), the single
    letters glued ('pokemon legends za'), a close known title when the words look misspelled, and the name before a
    colon or dash ('Pokemon Legends: Z-A' -> 'Pokemon Legends')."""
    query = query.strip()
    out: list[str] = []

    def add(text: str) -> None:
        text = _SPACES.sub(" ", text).strip()
        if text and text.casefold() != query.casefold() and text.casefold() not in (v.casefold() for v in out):
            out.append(text)

    words = fold(query).split()
    add(" ".join(words))
    glued = merge_letters(words)
    if glued != words:
        add(" ".join(glued))
    if known:
        fixed = correct(query, known)
        if fixed:
            add(fixed)
    head = re.split(r"\s*[:\u2013\u2014]\s*|\s+-\s+", query, maxsplit=1)[0]
    if head and head != query and len(fold(head).split()) >= 2:
        add(head)
    return out[:limit]


def correct(query: str, known: list[str], cutoff: float = 0.78) -> str:
    """The known title closest to a (possibly misspelled) query, or '' when nothing is close enough or the query is
    already a known title. Only titles the words of which all match, so 'Persona 4' never becomes 'Persona 5'."""
    fq = fold(query)
    if not fq:
        return ""
    qwords = merge_letters(fq.split())
    best, best_score = "", 0.0
    for title in known:
        ft = fold(title)
        if ft == fq:
            return ""
        twords = merge_letters(ft.split())
        if any(w.isdigit() for w in qwords) and [w for w in qwords if w.isdigit()] != [w for w in twords if w.isdigit()]:
            continue
        score = similarity(fq, ft)
        if score > best_score:
            best, best_score = title, score
    return best if best_score >= cutoff else ""


# --- what was searched before ----------------------------------------------------------------------------
class History:
    """Past searches and the titles they found, newest first, kept in a small JSON file. A convenience: any problem
    reading or writing it is ignored."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._items: list[dict] | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or HISTORY_FILE

    def _load(self) -> list[dict]:
        if self._items is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = []
            self._items = [d for d in data if isinstance(d, dict) and isinstance(d.get("t"), str)] if isinstance(data, list) else []
        return self._items

    def _save(self) -> None:
        try:
            paths.ensure_parent(self.path)
            self.path.write_text(json.dumps(self._items or []), encoding="utf-8")
        except OSError:
            pass

    def add(self, text: str, searched: bool = False) -> None:
        """Remember a title. `searched`: the person typed it (listed first when suggesting), as opposed to a title a
        site returned."""
        text = _SPACES.sub(" ", text).strip()
        key = fold(text)
        if len(key) < 2:
            return
        with self._lock:
            items = self._load()
            old = next((d for d in items if fold(d["t"]) == key), None)
            uses = (old.get("n", 0) if old else 0) + (1 if searched else 0)
            if old:
                items.remove(old)
            items.insert(0, {"t": text if searched or not old else old["t"], "n": uses, "s": bool(searched or (old and old.get("s"))),
                             "at": time.time()})
            del items[HISTORY_MAX:]
            self._save()

    def add_many(self, titles: list[str]) -> None:
        for t in titles:
            self.add(t)

    def titles(self) -> list[str]:
        with self._lock:
            return [d["t"] for d in self._load()]

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()

    def suggest(self, typed: str, extra: list[str] | None = None, limit: int = 8) -> list[str]:
        """Titles to offer for what is typed so far, best first: starts with it, a word starts with it, then close
        spellings. Empty text gives the latest searches. `extra` are more titles to draw on (the library's)."""
        with self._lock:
            items = [(d["t"], d.get("s", False), d.get("n", 0)) for d in self._load()]
        seen = {fold(t) for t, _, _ in items}
        items += [(t, False, 0) for t in (extra or []) if t and fold(t) not in seen and not seen.add(fold(t))]
        q = fold(typed)
        if not q:
            return [t for t, searched, _ in items if searched][:limit]
        qwords = merge_letters(q.split())
        scored: list[tuple[float, str]] = []
        for rank, (title, searched, uses) in enumerate(items):
            ft = fold(title)
            if ft == q:
                continue  # already typed in full
            words = merge_letters(ft.split())
            if ft.startswith(q):
                base = 3.0
            elif any(w.startswith(qwords[0]) for w in words) and tokens_match(qwords, words):
                base = 2.0
            elif tokens_match(qwords, words):
                base = 1.5
            elif len(q) >= 4 and similarity(q, ft[:max(len(q) + 2, 4)]) >= 0.75:
                base = 1.0 + similarity(q, ft[:len(q) + 2]) / 10
            else:
                continue
            bonus = (0.3 if searched else 0.0) + min(uses, 5) * 0.02 - rank * 0.0005
            scored.append((base + bonus, title))
        scored.sort(key=lambda p: p[0], reverse=True)
        return [t for _, t in scored[:limit]]


HISTORY = History()


def clean_title(title: str) -> str:
    """An album title as a game name for suggestions: no '(Original Soundtrack)' style brackets or trailing 'OST'."""
    text = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", title)
    text = re.sub(r"\b(?:original\s+)?(?:video\s*game\s+|game\s+)?(?:soundtrack|score|ost|music)\b.*$", "", text, flags=re.I)
    return _SPACES.sub(" ", text).strip(" -:,.") or title.strip()

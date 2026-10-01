"""The languages Pulse will translate a piece into, and what each is spelled with.

Pulse writes English. Everything downstream of the generator was built on that
assumption, and most of it is right to be — the SEO scorer's readability
formulas are calibrated on English, the loanword list in :mod:`app.services.ai`
is an *English* loanword list. Translation does not change any of that; it adds
a second artefact beside the piece, in a language this module has to be able to
name.

Naming it is not decoration. Two of Pulse's gates ask "is this text spelled in
characters it has no business containing", and the answer depends entirely on
what language the text is supposed to be in. ``génère`` is a corrupted
``generate`` in an English body and an ordinary verb in a French one; ``рынок``
is a sampler slip in either and a noun in a Russian one. Without a declared
language the gates have to guess, and the guess they made — "non-ASCII letters
are rare, so treat a body with many of them as multilingual and stop looking" —
is right for Japanese and wrong for every language written in the Latin
alphabet, where the accented share of a correct body is a few percent. See
:data:`app.services.ai._MULTILINGUAL_SHARE` and the tests in
``test_a_translated_body_is_not_a_corrupted_one.py``.

So each entry here carries the two facts the gates need:

``script``
    The writing system. A run of some *other* script is still a splice: a CJK
    token wedged into a Russian paragraph is exactly the failure the gate was
    built for, and the share-based escape hatch could not see it because
    Cyrillic had already pushed the foreign share over the line.

``letters``
    The characters beyond ASCII that this language legitimately spells words
    with. This is what replaces the loanword list when the text is not English:
    a French body may contain ``é``, and a word carrying ``ë`` in a Spanish one
    is still worth a reviewer's glance.

The list is deliberately short. Each language on it is one somebody can plausibly
publish into, and adding one is a decision to claim Pulse's output in it is
good enough to go out under the user's name — not a data-entry exercise. The
right way to grow it is one language at a time, with somebody who reads it
looking at the result.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final

#: The language Pulse generates in, and the one every piece is authored in.
#: Named rather than written as ``"en"`` at eleven call sites, because half of
#: those are comparisons that mean "is this the original" rather than "is this
#: English", and the two questions only happen to have the same answer today.
SOURCE_LANGUAGE: Final = "en"


@dataclass(frozen=True)
class Language:
    """One language Pulse can produce a translation in."""

    code: str
    #: The name in English, for the API and the picker.
    name: str
    #: The name in the language itself. What a reader of it expects to see on a
    #: language switcher; ``Deutsch``, not ``German``.
    endonym: str
    #: Which of :data:`SCRIPTS` this language is written in.
    script: str
    #: Non-ASCII letters this language spells ordinary words with, lowercase.
    #: Empty for languages whose whole alphabet is non-ASCII — there the script
    #: is the discriminator and enumerating the alphabet would add nothing.
    letters: str = ""
    #: Written right to left. Not used for validation; it reaches the API so a
    #: client can set ``dir`` on the element it renders the body into.
    rtl: bool = False

    @property
    def is_source(self) -> bool:
        """Whether this is the language pieces are written in to begin with."""
        return self.code == SOURCE_LANGUAGE

    def as_dict(self) -> dict[str, object]:
        """The registry entry as the API returns it."""
        return {
            "code": self.code,
            "name": self.name,
            "endonym": self.endonym,
            "script": self.script,
            "rtl": self.rtl,
        }


#: Script names, matching the ranges :mod:`app.services.ai` already splits on.
#: ``latin`` is the absence of the others rather than a range of its own, which
#: is why it is not in :data:`SCRIPT_RANGES`.
SCRIPTS: Final = ("latin", "cyrillic", "cjk", "japanese", "korean", "arabic", "devanagari")

#: The codepoint ranges belonging to each non-Latin script, as ``(low, high)``
#: pairs. Narrower than the union :mod:`app.services.ai` sweeps for, on purpose:
#: this table answers "is this character *expected* here", and a range that
#: claims too much would wave a genuine splice through.
SCRIPT_RANGES: Final[dict[str, tuple[tuple[int, int], ...]]] = {
    "cyrillic": ((0x0400, 0x04FF), (0x0500, 0x052F)),
    # Han, plus the fullwidth punctuation Chinese sets in the same run.
    "cjk": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x3000, 0x303F)),
    # Japanese is Han *and* the two kana syllabaries; a Japanese body that
    # contained no kana would be Chinese.
    "japanese": (
        (0x3040, 0x309F),
        (0x30A0, 0x30FF),
        (0x4E00, 0x9FFF),
        (0x3400, 0x4DBF),
        (0x3000, 0x303F),
    ),
    "korean": ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)),
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)),
    "devanagari": ((0x0900, 0x097F),),
}


def _entries() -> tuple[Language, ...]:
    """The registry, built once. Kept in a function so the tuple below reads."""
    return (
        Language("en", "English", "English", "latin"),
        Language("es", "Spanish", "Español", "latin", "áéíóúüñ¡¿"),
        Language("fr", "French", "Français", "latin", "àâæçéèêëîïôœùûüÿ"),
        Language("de", "German", "Deutsch", "latin", "äöüß"),
        Language("pt", "Portuguese", "Português", "latin", "ãõáéíóúâêôàç"),
        Language("it", "Italian", "Italiano", "latin", "àèéìíîòóùú"),
        Language("nl", "Dutch", "Nederlands", "latin", "ëïéèêöüáä"),
        Language("pl", "Polish", "Polski", "latin", "ąćęłńóśźż"),
        # ``İ`` is listed explicitly because Python's ``str.upper`` does not
        # produce it: ``"i".upper()`` is ASCII ``"I"`` regardless of locale, so
        # the case-folding in `expected_letters` cannot reach Turkish's dotted
        # capital and every "İstanbul" in a Turkish body would read as a splice.
        Language("tr", "Turkish", "Türkçe", "latin", "çğıİöşü"),
        Language("vi", "Vietnamese", "Tiếng Việt", "latin", _VIETNAMESE_LETTERS),
        Language("ru", "Russian", "Русский", "cyrillic"),
        Language("uk", "Ukrainian", "Українська", "cyrillic"),
        Language("ja", "Japanese", "日本語", "japanese"),
        Language("zh", "Chinese", "中文", "cjk"),
        Language("ko", "Korean", "한국어", "korean"),
        Language("ar", "Arabic", "العربية", "arabic", rtl=True),
        Language("he", "Hebrew", "עברית", "arabic", rtl=True),
        Language("hi", "Hindi", "हिन्दी", "devanagari"),
    )


#: Vietnamese spells with the Latin alphabet and six tones stacked on twelve
#: vowels, which is ~90 distinct precomposed codepoints. Written out rather than
#: computed from a combining-mark rule, because the rule would also admit every
#: accented form of every *other* language and turn the gate off for this one.
_VIETNAMESE_LETTERS: Final = (
    "àáảãạăằắẳẵặâầấẩẫậ"
    "èéẻẽẹêềếểễệ"
    "ìíỉĩị"
    "òóỏõọôồốổỗộơờớởỡợ"
    "ùúủũụưừứửữự"
    "ỳýỷỹỵ"
    "đ"
)

#: Every supported language, in the order the picker shows them: the source
#: first, then by English name.
LANGUAGES: Final[tuple[Language, ...]] = _entries()

#: By code, for the lookups that happen per request.
BY_CODE: Final[dict[str, Language]] = {lang.code: lang for lang in LANGUAGES}

#: Every code a caller may name, including the source. Used by the schema layer
#: to reject a language before a translation row is written for it.
CODES: Final[frozenset[str]] = frozenset(BY_CODE)

#: The codes a piece can be *translated into* — everything but the one it is
#: already in. A request to translate English into English is not a small job,
#: it is a mistake, and the honest answer is a 422 rather than an LLM call whose
#: best possible outcome is the input.
TARGET_CODES: Final[frozenset[str]] = frozenset(
    lang.code for lang in LANGUAGES if not lang.is_source
)


def normalize(code: str | None) -> str | None:
    """Canonicalise a language tag, or ``None`` if it names nothing supported.

    Accepts the regional forms a browser sends — ``fr-CA``, ``pt_BR``, ``EN`` —
    and answers with the base language, because that is the granularity Pulse
    translates at. A caller asking for ``pt-BR`` gets Portuguese and is not told
    it asked for something Pulse does not have: the alternative is a 422 on the
    one header every browser fills in automatically.

    Returning ``None`` rather than raising, because both callers want to make
    their own error: the schema layer turns it into a 422 listing the supported
    codes, and the publishing path turns it into "publish the original".
    """
    if not code:
        return None
    base = code.strip().replace("_", "-").split("-")[0].lower()
    return base if base in BY_CODE else None


def get(code: str) -> Language | None:
    """The registry entry for *code*, or ``None``. Regional forms accepted."""
    normalized = normalize(code)
    return BY_CODE.get(normalized) if normalized else None


def expected_letters(code: str) -> frozenset[str]:
    """Non-ASCII letters *code* spells ordinary words with, both cases.

    Both cases because the gate reads words as they were written and ``Über``
    is as ordinary as ``über``. Built here rather than stored twice in the
    registry so the two can never disagree, and normalised to NFC so a body
    carrying the decomposed form of ``é`` — which is what a copy-paste out of
    some editors produces — is measured against the same letter.

    ``str.upper`` is not a bijection, and does not need to be here. German ``ß``
    uppercases to the two ASCII characters ``SS``, which land in the set as a
    plain ``S`` and cost nothing — the gate only ever asks about letters outside
    ASCII. Where the mapping *loses* a letter rather than flattening it, the
    registry lists both forms itself; see Turkish ``İ``.
    """
    language = get(code)
    if language is None or not language.letters:
        return frozenset()
    letters = unicodedata.normalize("NFC", language.letters)
    return frozenset(letters) | frozenset(letters.upper())


def in_script(char: str, script: str) -> bool:
    """Whether *char* belongs to *script*.

    ``latin`` is true for anything outside the non-Latin ranges, which is the
    useful reading rather than the pedantic one: the caller is asking "is this
    character a foreign *script*", and for a Latin-script language the answer
    for ``é`` is no — whether ``é`` belongs in *this* language is
    :func:`expected_letters`' question, asked separately and about words.
    """
    ranges = SCRIPT_RANGES.get(script)
    point = ord(char)
    if ranges is None:
        return not any(
            low <= point <= high
            for script_ranges in SCRIPT_RANGES.values()
            for low, high in script_ranges
        )
    return any(low <= point <= high for low, high in ranges)

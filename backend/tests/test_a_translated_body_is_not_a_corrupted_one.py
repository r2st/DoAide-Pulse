"""Herald's two corruption gates, asked about text that is not English.

Both gates were written when Herald wrote English and only English, and both
guess at "is this text in another language" from how many of its letters are
non-ASCII. That guess has one threshold — ``_MULTILINGUAL_SHARE``, ten percent —
and it is wrong at both ends for a translation:

* **Latin-script languages never reach it.** Correct French runs about three
  percent accented letters. So a clean French body was reported as ten separate
  splices — ``génère``, ``dépôts``, ``première`` — one for every word carrying an
  accent, and a translation could never have passed the gate that decides whether
  a piece may publish unattended.

* **Non-Latin scripts blow past it, and take every other script with them.** A
  Russian body is over the threshold on Cyrillic alone, so the gate returned
  nothing at all — including for the CJK token wedged into paragraph three, which
  is the exact failure the gate was built for.

The fix is to stop guessing: ``app.services.languages`` says what each language
is written in and which letters it spells with, and both gates take the language
they are reading. This file pins both directions, because a gate that has been
switched off is indistinguishable from one that passes.
"""
from __future__ import annotations

import pytest

from app.services import ai, languages

# --------------------------------------------------------------------------- #
# Bodies. Short enough to read, long enough that the share thresholds behave    #
# the way they do on a real article.                                           #
# --------------------------------------------------------------------------- #

FRENCH = (
    "Herald génère des articles à partir de vos dépôts. La première étape "
    "consiste à connecter votre compte GitHub, puis à sélectionner le "
    "référentiel que vous souhaitez surveiller. Chaque fois qu'une version est "
    "publiée, Herald rédige un brouillon complet et vous l'envoie pour "
    "révision. Vous gardez le contrôle éditorial: rien n'est publié sans votre "
    "accord préalable."
)

GERMAN = (
    "Herald erstellt Artikel aus Ihren Repositories. Zunächst verbinden Sie Ihr "
    "GitHub-Konto und wählen das Repository aus, das überwacht werden soll. Bei "
    "jeder Veröffentlichung schreibt Herald einen vollständigen Entwurf und "
    "sendet ihn zur Überprüfung. Die redaktionelle Kontrolle bleibt bei Ihnen: "
    "nichts wird ohne Ihre ausdrückliche Zustimmung veröffentlicht."
)

SPANISH = (
    "Herald genera artículos a partir de sus repositorios. El primer paso es "
    "conectar su cuenta de GitHub y seleccionar el repositorio que desea "
    "supervisar. Cada vez que se publica una versión, Herald redacta un "
    "borrador completo y se lo envía para su revisión."
)

POLISH = (
    "Herald tworzy artykuły na podstawie twoich repozytoriów. Najpierw połącz "
    "swoje konto GitHub, a następnie wybierz repozytorium, które chcesz "
    "śledzić. Za każdym razem, gdy zostanie opublikowana nowa wersja, Herald "
    "przygotuje pełny szkic i prześle go do przeglądu."
)

RUSSIAN = (
    "Herald создает статьи из ваших репозиториев. Сначала подключите свою "
    "учетную запись GitHub, а затем выберите репозиторий, за которым хотите "
    "следить. Каждый раз, когда публикуется новая версия, Herald пишет полный "
    "черновик и отправляет его вам на проверку."
)

JAPANESE = (
    "Herald はリポジトリから記事を生成します。まず GitHub アカウントを接続し、"
    "監視したいリポジトリを選択してください。バージョンが公開されるたびに、"
    "Herald は完全な下書きを作成し、レビューのために送信します。"
)

#: The English body the gates were built for, with the corruption they were
#: built to find. ``rënd`` is one of the real ones — it published twice.
ENGLISH_CORRUPT = (
    "The cost-of-capital methodology rënd the base scenario for every automated "
    "publishing run, and the dashboard reflects that change immediately across "
    "each connected destination without any further configuration."
)

CLEAN_BODIES = [
    pytest.param(FRENCH, "fr", id="fr"),
    pytest.param(GERMAN, "de", id="de"),
    pytest.param(SPANISH, "es", id="es"),
    pytest.param(POLISH, "pl", id="pl"),
    pytest.param(RUSSIAN, "ru", id="ru"),
    pytest.param(JAPANESE, "ja", id="ja"),
]


# --------------------------------------------------------------------------- #
# The direction that was broken: a correct translation is not corruption        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("body", "language"), CLEAN_BODIES)
def test_a_correct_translation_reports_nothing(body, language):
    """The whole feature depends on this: a clean translation passes both gates.

    Not "few issues" — none. A piece is held back from auto-publishing on the
    strength of one entry here, so a gate that reports a single false positive
    per article is a gate that holds back every article.
    """
    assert ai.stray_script_runs(body, language=language) == []
    assert ai.stray_letter_splices(body, language=language) == []


def test_the_french_body_is_the_one_the_old_gate_flagged_word_by_word():
    """The regression, stated as the numbers rather than as a description.

    Read as English, the French body trips the splice gate on every accented
    word — this is what shipped, and what made a translation unpublishable. The
    assertion is deliberately about the *old* behaviour so that a future change
    which quietly disables the gate for English fails here rather than silently
    passing the test above.
    """
    as_english = ai.stray_letter_splices(FRENCH)
    assert len(as_english) >= 5
    assert "génère" in as_english

    assert ai.stray_letter_splices(FRENCH, language="fr") == []


# --------------------------------------------------------------------------- #
# The direction that must not be lost: real corruption is still caught          #
# --------------------------------------------------------------------------- #


def test_a_foreign_script_in_a_latin_translation_is_still_a_splice():
    """A Cyrillic noun in a French article is the sampler slipping, not French."""
    corrupted = FRENCH.replace("le référentiel", "le рынок")

    assert ai.stray_script_runs(corrupted, language="fr") == ["рынок"]


def test_a_cjk_run_in_a_russian_body_is_caught_now_that_the_language_is_named():
    """The all-or-nothing bug in the share-based escape hatch.

    Cyrillic alone puts a Russian body over ``_MULTILINGUAL_SHARE``, so the gate
    exempted the *whole text* — every script at once — and a CJK token in
    paragraph three came back clean. Naming the language masks Cyrillic and
    leaves everything else to be judged.
    """
    corrupted = RUSSIAN.replace("репозиториев", "репозиториев 日本語")

    assert ai.stray_script_runs(corrupted) == []  # the bug, as it behaved
    assert ai.stray_script_runs(corrupted, language="ru") == ["日本語"]


def test_a_letter_from_another_alphabet_is_a_splice_in_a_translation():
    """Polish spells with ``ł`` and not with ``ß``; German is the other way round.

    This is the check that stops "declare a language" becoming "switch the gate
    off": the alphabet is per language, so a German letter in a Polish body is
    exactly as wrong as a Cyrillic one.
    """
    polish_with_german = POLISH.replace("wybierz", "wybierß")

    assert "wybierß" in ai.stray_letter_splices(polish_with_german, language="pl")
    assert ai.stray_letter_splices(GERMAN, language="de") == []


def test_english_corruption_is_unaffected_by_any_of_this():
    """The gate's original job, unchanged, with and without the default argument.

    ``rënd`` is one of the words that actually published. Both spellings of the
    call have to agree, because every existing caller uses the first.
    """
    assert ai.stray_letter_splices(ENGLISH_CORRUPT) == ["rënd"]
    assert ai.stray_letter_splices(ENGLISH_CORRUPT, language="en") == ["rënd"]
    assert ai.stray_letter_splices(ENGLISH_CORRUPT, language=languages.SOURCE_LANGUAGE) == [
        "rënd"
    ]


def test_an_unknown_language_falls_back_to_reading_it_as_english():
    """An unrecognised tag must not be a way to turn the gate off.

    ``languages.get`` answers ``None`` for anything not in the registry, and the
    gates treat that as "no declaration" rather than "anything goes" — otherwise
    ``?language=xx`` is an unauthenticated bypass of the corruption check.
    """
    assert ai.stray_letter_splices(ENGLISH_CORRUPT, language="xx") == ["rënd"]
    assert ai.stray_letter_splices(ENGLISH_CORRUPT, language="") == ["rënd"]


# --------------------------------------------------------------------------- #
# The registry the gates read                                                   #
# --------------------------------------------------------------------------- #


def test_a_regional_tag_resolves_to_its_base_language():
    """Browsers send ``fr-CA``; the registry translates at base-language grain."""
    assert languages.normalize("fr-CA") == "fr"
    assert languages.normalize("pt_BR") == "pt"
    assert languages.normalize("EN") == "en"
    assert languages.normalize("klingon") is None
    assert languages.normalize(None) is None


def test_every_registry_entry_is_usable_by_both_gates():
    """A language in the picker that the gates cannot read is worse than absent.

    Sweeps the registry rather than checking a sample, because the failure this
    guards against is somebody adding a language and not a script for it — which
    reads as a one-line change and silently applies the English loanword list to
    a whole language.
    """
    for entry in languages.LANGUAGES:
        assert entry.script in languages.SCRIPTS, entry.code
        if entry.script == "latin" and not entry.is_source:
            assert entry.letters, (
                f"{entry.code} is written in the Latin alphabet with no extra "
                "letters declared, so every accented word in it would be read "
                "as corruption"
            )
        # Every declared letter is one the gate will actually accept, in the
        # case it was declared in and in its uppercase form — except where
        # uppercasing leaves ASCII entirely, as German ``ß`` does (``"SS"``),
        # which the gate never asks about anyway.
        allowed = languages.expected_letters(entry.code)
        for char in entry.letters:
            assert char in allowed, (entry.code, char)
            upper = char.upper()
            if len(upper) == 1 and ord(upper) > 127:
                assert upper in allowed, (entry.code, char, upper)


def test_turkish_keeps_its_dotted_capital():
    """``"i".upper()`` is ASCII ``"I"``, so ``İ`` has to be declared, not derived.

    The one letter in the registry that case-folding cannot reach. Without it
    every ``İstanbul`` in a Turkish translation is reported as corruption, which
    is the same class of false positive the whole language-aware change exists
    to remove — just one letter wide instead of one alphabet wide.
    """
    allowed = languages.expected_letters("tr")
    assert "İ" in allowed
    assert ai.stray_letter_splices("İstanbul için yeni bir sürüm", language="tr") == []


def test_the_source_language_is_not_a_translation_target():
    """Translating English into English is a mistake, not a small job."""
    english = languages.get("en")
    assert languages.SOURCE_LANGUAGE not in languages.TARGET_CODES
    assert languages.SOURCE_LANGUAGE in languages.CODES
    assert english is not None and english.is_source is True

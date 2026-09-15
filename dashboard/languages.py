"""The languages a document may be tagged with.

English plus the 22 languages of the Eighth Schedule. Keys are 3-letter codes:
the tessdata code where tessdata_best has a model -- exactly what the OCR pass
expects -- and the ISO 639-2/3 code otherwise. They are what
`GET /dashboard/languages` offers and what the upload form's `language` field
must contain.

Not every language here can be OCR'd. TESSDATA_BEST marks the ones
tessdata_best publishes a model for (checked against the upstream repo,
2026-09-11); the rest are listed because the agri team needs to tag documents
in them, not because the pipeline can read them. Two that DO have an upstream
model -- san, snd -- are not downloaded into ./tessdata_best.

Lives here rather than in the old dashboard/dedup.py, which is gone along with
the embedding pipeline. Nothing in this module loads a model or touches the
network, so importing it is free; that matters because pop_server.py imports
the whole dashboard router chain at startup.

Language is stated by the uploader, never inferred from the state ON UPLOAD.
Inferring it is a known way to produce garbage: several states' documents are
in English despite the state having its own script, and legacy Kannada fonts
(Nudi, Baraha) map Kannada onto ASCII so even the PDF text layer reads as Latin.

STATE_LANG below is the deliberate exception, and it means something narrower
than "this document is in this language": it is the tessdata model to TRY for a
document the OCR pass could not read as English. That is how the corpus pass
used it (scripts/hash_and_embed_report_true.ocr_lang_for_row), and it is why a
row filled from it carries `language_source: "state"` -- so an inference is
never mistaken later for a detection.
"""
from __future__ import annotations

LANGUAGES = {
    "asm": "Assamese",
    "ben": "Bengali",
    "brx": "Bodo",
    "doi": "Dogri",
    "eng": "English",
    "guj": "Gujarati",
    "hin": "Hindi",
    "kan": "Kannada",
    "kas": "Kashmiri",
    "kok": "Konkani",
    "mai": "Maithili",
    "mal": "Malayalam",
    "mni": "Manipuri (Meitei)",
    "mar": "Marathi",
    "nep": "Nepali",
    "ori": "Odia",
    "pan": "Punjabi",
    "san": "Sanskrit",
    "sat": "Santali",
    "snd": "Sindhi",
    "tam": "Tamil",
    "tel": "Telugu",
    "urd": "Urdu",
}

# The LANGUAGES codes tessdata_best has a model for. brx, doi, kas, kok, mai,
# mni and sat have none upstream.
TESSDATA_BEST = frozenset({
    "asm", "ben", "eng", "guj", "hin", "kan", "mal", "mar",
    "nep", "ori", "pan", "san", "snd", "tam", "tel", "urd",
})


# The tessdata model to use for a document in each state, keyed by the state's
# ORIGINAL folder name -- "State Karnataka", not "Karnataka". That coupling is
# load-bearing: normalising the name breaks the lookup, and with it every
# non-English OCR pass.
#
# DUPLICATED from scripts/hash_and_embed_report_true.py, deliberately. That
# script is the original and still authoritative, but importing it pulls in
# fitz, pytesseract and tesserocr at module scope, and pop_server.py imports
# this module chain at startup. The two must stay in step; there are only 33
# entries and the set of Indian states does not change often.
STATE_LANG = {
    "Central Advisories": "hin",
    "State  Jammu and Kashmir": "urd",  # note the double space, as in the source data
    "State Andaman and Nicobar": "hin",
    "State Andhra Pradesh": "tel",
    "State Arunachal Pradesh": "eng",
    "State Assam": "asm",
    "State Bihar": "hin",
    "State Chattisgarh": "hin",
    "State Delhi": "hin",
    "State Goa": "mar",
    "State Gujarat": "guj",
    "State Haryana": "hin",
    "State Himachal Pradesh": "hin",
    "State Jharkhand": "hin",
    "State Karnataka": "kan",
    "State Kerala": "mal",
    "State Madhya Pradesh": "hin",
    "State Maharashtra": "mar",
    "State Manipur": "eng",
    "State Meghalaya": "eng",
    "State Mizoram": "eng",
    "State Nagaland": "eng",
    "State Odisha": "ori",
    "State Puducherry": "tam",
    "State Punjab": "pan",
    "State Rajasthan": "hin",
    "State Sikkim": "nep",
    "State Tamilnadu": "tam",
    "State Telangana": "tel",
    "State Tripura": "ben",
    "State Uttar Pradesh": "hin",
    "State Uttarakhand": "hin",
    "State West Bengal": "ben",
}


def language_for_state(state_raw: str | None) -> str | None:
    """The tessdata code for a state, by its ORIGINAL folder name. None for a
    state that is not in the table -- an unmapped state is unknown, and
    guessing 'eng' for it would be the exact mistake this table exists to
    avoid."""
    if not state_raw:
        return None
    return STATE_LANG.get(state_raw.strip())


def resolve_language(detected: str | None, state_raws) -> tuple[str | None, str]:
    """(language code, source) for a document.

    The rule, per the user and matching what the corpus OCR pass did: a document
    the pass read as English is English; anything else -- Non-English, an OCR
    error, or never examined at all -- takes the language of the state it is
    filed under.

    `state_raws` is every state the document is placed in, because a document is
    shared across placements while a state is not. Almost always one state; six
    documents in the corpus span more than one. When they disagree, the most
    common wins, and on a tie the FIRST placement's state decides -- the state it
    was originally filed under. Every document gets a language either way,
    because a plausible value the agri team can correct beats a blank they cannot
    see.

    SOURCE IS EXACTLY TWO VALUES, per the user:

        "detected"  the language is known -- the OCR pass read it as English, or
                    a person set it through the dashboard.
        "state"     inferred from the state. Plausible, not measured.

    That is the only distinction anyone acts on: which rows are guesses. A tie
    between states is still a state inference, and a state that maps to nothing
    is still a state inference that failed -- neither earns a third value.
    """
    if (detected or "").strip().lower() in ("eng", "english"):
        return "eng", "detected"

    from collections import Counter

    codes = [c for c in (language_for_state(s) for s in state_raws) if c]
    if not codes:
        return None, "state"
    ranked = Counter(codes).most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return codes[0], "state"
    return ranked[0][0], "state"

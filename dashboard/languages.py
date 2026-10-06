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


# The tessdata model to TRY for a document filed under each state, keyed by the
# state's LGD CODE.
#
# Keyed by code and not by name, which is the whole point. This table used to be
# keyed by the ORIGINAL WorkDrive folder name ("State Karnataka", "Central
# Advisories"), which made every non-English OCR pass depend on a spelling: the
# LGD sync renamed Kerala to Keralam and Tamilnadu to Tamil Nadu, and a lookup by
# name would have silently started returning None for 2,380 placements. An LGD
# code never changes, so a rename cannot reach this table at all.
#
# 33 of the 37 states have an entry. Chandigarh, Ladakh, Lakshadweep and Dadra
# and Nagar Haveli are absent on purpose -- the corpus has no documents under
# them, and guessing a model for a state nobody has filed anything under would
# be the exact mistake this table exists to avoid.
#
# Still DUPLICATED from scripts/hash_and_embed_report_true.py, deliberately:
# that script is the original, but importing it pulls in fitz, pytesseract and
# tesserocr at module scope, and pop_server.py imports this chain at startup.
# The script still keys on folder names, which is correct THERE -- it reads
# report_true.csv, whose state column IS the folder name.
STATE_LANG = {
    1: "urd",   # Jammu And Kashmir
    2: "hin",   # Himachal Pradesh
    3: "pan",   # Punjab
    5: "hin",   # Uttarakhand
    6: "hin",   # Haryana
    7: "hin",   # Delhi
    8: "hin",   # Rajasthan
    9: "hin",   # Uttar Pradesh
    10: "hin",  # Bihar
    11: "nep",  # Sikkim
    12: "eng",  # Arunachal Pradesh
    13: "eng",  # Nagaland
    14: "eng",  # Manipur
    15: "eng",  # Mizoram
    16: "ben",  # Tripura
    17: "eng",  # Meghalaya
    18: "asm",  # Assam
    19: "ben",  # West Bengal
    20: "hin",  # Jharkhand
    21: "ori",  # Odisha
    22: "hin",  # Chhattisgarh
    23: "hin",  # Madhya Pradesh
    24: "guj",  # Gujarat
    27: "mar",  # Maharashtra
    28: "tel",  # Andhra Pradesh
    29: "kan",  # Karnataka
    30: "mar",  # Goa
    32: "mal",  # Keralam
    33: "tam",  # Tamil Nadu
    34: "tam",  # Puducherry
    35: "hin",  # Andaman And Nicobar Islands
    36: "tel",  # Telangana
    39: "hin",  # Central
}

# Folder name -> LGD code, for the CORPUS LOADER ONLY.
#
# dashboard/migrate_from_corpus.py reads the WorkDrive crawl, whose only name for
# a state is the folder name it found. It has no state_id to work from, so it
# needs this one translation to reach STATE_LANG. Nothing the server serves uses
# it: a request carries a state_id, which carries a code.
#
# Spellings are the crawl's, including the double space in Jammu and Kashmir.
CORPUS_FOLDER_CODE = {
    "Central Advisories": 39,
    "State  Jammu and Kashmir": 1,
    "State Andaman and Nicobar": 35,
    "State Andhra Pradesh": 28,
    "State Arunachal Pradesh": 12,
    "State Assam": 18,
    "State Bihar": 10,
    "State Chattisgarh": 22,
    "State Delhi": 7,
    "State Goa": 30,
    "State Gujarat": 24,
    "State Haryana": 6,
    "State Himachal Pradesh": 2,
    "State Jharkhand": 20,
    "State Karnataka": 29,
    "State Kerala": 32,
    "State Madhya Pradesh": 23,
    "State Maharashtra": 27,
    "State Manipur": 14,
    "State Meghalaya": 17,
    "State Mizoram": 15,
    "State Nagaland": 13,
    "State Odisha": 21,
    "State Puducherry": 34,
    "State Punjab": 3,
    "State Rajasthan": 8,
    "State Sikkim": 11,
    "State Tamilnadu": 33,
    "State Telangana": 36,
    "State Tripura": 16,
    "State Uttar Pradesh": 9,
    "State Uttarakhand": 5,
    "State West Bengal": 19,
}


def code_for_corpus_folder(folder: str | None) -> int | None:
    """The LGD code a crawled folder name means. For the corpus loader only."""
    return CORPUS_FOLDER_CODE.get((folder or "").strip()) if folder else None


def language_for_state(state_code) -> str | None:
    """The tessdata code for a state, by its LGD CODE. None for a state the
    table has no entry for -- an unmapped state is unknown, and guessing 'eng'
    for it would be the exact mistake this table exists to avoid.

    Takes the code, never the name: see STATE_LANG. A None code (a placement
    whose state was never resolved) is unknown too.
    """
    if state_code is None:
        return None
    try:
        return STATE_LANG.get(int(state_code))
    except (TypeError, ValueError):
        return None


def resolve_language(detected: str | None, state_codes) -> tuple[str | None, str]:
    """(language code, source) for a document.

    The rule, per the user and matching what the corpus OCR pass did: a document
    the pass read as English is English; anything else -- Non-English, an OCR
    error, or never examined at all -- takes the language of the state it is
    filed under.

    `state_codes` is the LGD code of every state the document is placed in,
    because a document is shared across placements while a state is not. Codes
    and not names: a state can be renamed, and this inference must not move with
    the spelling. Almost always one state; six
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

    codes = [c for c in (language_for_state(s) for s in state_codes) if c]
    if not codes:
        return None, "state"
    ranked = Counter(codes).most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return codes[0], "state"
    return ranked[0][0], "state"

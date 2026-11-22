"""Owner / assignee name normalization and fuzzy matching.

Handles the variation typical of DGIP records:

* Indonesian legal forms and their spellings (``PT.``, ``P.T.``, ``(Persero)``, ``Tbk``, ``CV``)
* foreign legal forms (``Co., Ltd.``, ``GmbH``, ``Kabushiki Kaisha`` ...)
* academic titles on inventor names (``Prof. Dr. Ir. Budi Santoso, S.T., M.T.``)
* pre-1972 spelling (``Gadjah`` ~ ``Gajah``, ``Tjipta`` ~ ``Cipta``, ``Soekarno`` ~ ``Sukarno``)
* well-known institutional abbreviations (``ITB``, ``UGM``, ``BRIN`` ...)

Scoring is deterministic and explainable: every match carries the reasons that
produced its score, so reviewers can see why two names were linked.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable

from .enums import ConfidenceBand, OwnerType

ID_LEGAL_FORMS = {
    "PT": "PT", "PERSEROAN TERBATAS": "PT", "PERSERO": "PERSERO", "TBK": "TBK", "CV": "CV",
    "UD": "UD", "PERUM": "PERUM", "KOPERASI": "KOPERASI", "YAYASAN": "YAYASAN", "FIRMA": "FIRMA",
}
FOREIGN_LEGAL_FORMS = {
    "INC": "INC", "INCORPORATED": "INC", "CORP": "CORP", "CORPORATION": "CORP", "LTD": "LTD",
    "LIMITED": "LTD", "CO": "CO", "COMPANY": "CO", "LLC": "LLC", "PLC": "PLC", "GMBH": "GMBH",
    "AG": "AG", "SA": "SA", "SAS": "SAS", "BV": "BV", "NV": "NV", "KK": "KK",
    "KABUSHIKI KAISHA": "KK", "PTE": "PTE", "SDN BHD": "SDN BHD", "BHD": "BHD", "AB": "AB",
    "OY": "OY", "SRL": "SRL", "SPA": "SPA", "KG": "KG", "LLP": "LLP",
}
# Forms that do not change entity identity when present on only one side.
_NEUTRAL_FORMS = {"TBK", "PERSERO", "CO", "LTD"}

TITLES = {
    "PROF", "DR", "DRS", "DRA", "IR", "H", "HJ", "ST", "MT", "MSC", "PHD", "SH", "MH", "SE", "MM",
    "SKOM", "MKOM", "SP", "SSI", "MSI", "MBA", "MENG", "BENG", "SPD", "MPD", "SKED", "SFARM",
    "APT", "DRG", "SPOG", "MKES", "MS", "MA", "BA", "BSC", "MR", "MRS", "MS", "DIPL", "ING",
}

UNIVERSITY_WORDS = {"UNIVERSITAS", "UNIVERSITY", "INSTITUT", "INSTITUTE", "POLITEKNIK", "POLYTECHNIC", "AKADEMI", "COLLEGE", "SEKOLAH"}
RESEARCH_GOV_WORDS = {"BADAN", "KEMENTERIAN", "LEMBAGA", "BALAI", "PUSAT", "DINAS", "PEMERINTAH", "BRIN", "LIPI", "BPPT", "BATAN", "LAPAN", "DIREKTORAT"}

ABBREVIATIONS = {
    "UNIV": "UNIVERSITAS", "INST": "INSTITUT", "POLTEK": "POLITEKNIK", "TEKN": "TEKNOLOGI",
    "INDO": "INDONESIA", "NAS": "NASIONAL",
}

# Institutional aliases (canonical key -> expansion). Extend via OwnerMatcher(aliases=...).
DEFAULT_ALIASES = {
    "ITB": "INSTITUT TEKNOLOGI BANDUNG",
    "ITS": "INSTITUT TEKNOLOGI SEPULUH NOPEMBER",
    "IPB": "INSTITUT PERTANIAN BOGOR",
    "IPB UNIVERSITY": "INSTITUT PERTANIAN BOGOR",
    "UI": "UNIVERSITAS INDONESIA",
    "UGM": "UNIVERSITAS GADJAH MADA",
    "UNAIR": "UNIVERSITAS AIRLANGGA",
    "UNPAD": "UNIVERSITAS PADJADJARAN",
    "UNDIP": "UNIVERSITAS DIPONEGORO",
    "UB": "UNIVERSITAS BRAWIJAYA",
    "UNHAS": "UNIVERSITAS HASANUDDIN",
    "UNS": "UNIVERSITAS SEBELAS MARET",
    "UNY": "UNIVERSITAS NEGERI YOGYAKARTA",
    "UNESA": "UNIVERSITAS NEGERI SURABAYA",
    "USU": "UNIVERSITAS SUMATERA UTARA",
    "BRIN": "BADAN RISET DAN INOVASI NASIONAL",
    "LIPI": "LEMBAGA ILMU PENGETAHUAN INDONESIA",
    "BPPT": "BADAN PENGKAJIAN DAN PENERAPAN TEKNOLOGI",
}

_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def _ascii_upper(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).upper()


def phonetic(text: str) -> str:
    """Map pre-1972 (Ejaan Soewandi) spellings to modern EYD for comparison."""
    out = text
    for old, new in (("DJ", "J"), ("TJ", "C"), ("OE", "U"), ("NJ", "NY"), ("SJ", "SY"), ("CH", "KH")):
        out = out.replace(old, new)
    return re.sub(r"(.)\1+", r"\1", out)  # collapse doubled letters (Hasanuddin ~ Hasanudin)


@dataclass(frozen=True)
class NormalizedName:
    original: str
    key: str  # canonical comparison key, legal forms and titles removed
    tokens: tuple[str, ...]
    legal_forms: frozenset[str]
    owner_type: OwnerType
    alias_of: str | None = None

    @property
    def phonetic_key(self) -> str:
        return phonetic(self.key)


def normalize_owner_name(name: str, country: str | None = None, aliases: dict[str, str] | None = None) -> NormalizedName:
    aliases = DEFAULT_ALIASES if aliases is None else aliases
    original = name
    text = _ascii_upper(name or "")
    text = text.replace("&", " DAN ")
    # "P.T." / "C.V." -> "PT" / "CV" before punctuation stripping splits them
    text = re.sub(r"\b([A-Z])\.\s?([A-Z])\.(?=\s|$|,)", r"\1\2", text)
    # Inventor names: drop trailing degree segments after commas ("Budi, S.T., M.T.")
    parts = [p.strip() for p in text.split(",")]
    if len(parts) > 1:
        head, tail = parts[0], parts[1:]
        kept = [p for p in tail if not _all_titles(p)]
        text = " ".join([head, *kept])
    text = _PUNCT.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()

    tokens = [ABBREVIATIONS.get(t, t) for t in text.split(" ") if t]
    joined = " ".join(tokens)
    forms: set[str] = set()
    for phrase, code in sorted({**ID_LEGAL_FORMS, **FOREIGN_LEGAL_FORMS}.items(), key=lambda kv: -len(kv[0])):
        pattern = rf"(?:^|\s){re.escape(phrase)}(?=\s|$)"
        if re.search(pattern, joined):
            forms.add(code)
            joined = re.sub(pattern, " ", joined)
    tokens = [t for t in joined.split(" ") if t]
    # Leading honorifics/titles on personal names
    while tokens and tokens[0] in TITLES and len(tokens) > 1:
        tokens.pop(0)
    key = " ".join(tokens)
    alias_of = None
    if key in aliases:
        alias_of = key
        key = aliases[key]
        tokens = key.split(" ")
    owner_type = classify_owner(tokens, forms, country)
    return NormalizedName(original, key, tuple(tokens), frozenset(forms), owner_type, alias_of)


def _all_titles(segment: str) -> bool:
    words = [w for w in _PUNCT.sub(" ", segment).split() if w]
    return bool(words) and all(w.replace(".", "") in TITLES for w in words)


def classify_owner(tokens: Iterable[str], forms: frozenset[str] | set[str], country: str | None = None) -> OwnerType:
    toks = set(tokens)
    foreign_country = country is not None and country.upper() not in ("ID", "IDN", "INDONESIA")
    if toks & UNIVERSITY_WORDS:
        return OwnerType.FOREIGN if foreign_country else OwnerType.UNIVERSITY
    if toks & RESEARCH_GOV_WORDS:
        return OwnerType.FOREIGN if foreign_country else OwnerType.RESEARCH_GOVERNMENT
    if forms & set(FOREIGN_LEGAL_FORMS.values()) - set(ID_LEGAL_FORMS.values()):
        return OwnerType.FOREIGN
    if foreign_country:
        return OwnerType.FOREIGN
    if forms & set(ID_LEGAL_FORMS.values()):
        return OwnerType.COMPANY
    words = [t for t in toks if t.isalpha()]
    if 1 <= len(words) <= 5 and len(words) == len(toks):
        return OwnerType.INDIVIDUAL
    return OwnerType.UNKNOWN


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100


def token_sort_ratio(a: str, b: str) -> float:
    return _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split())))


def token_set_ratio(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    inter = " ".join(sorted(sa & sb))
    da = " ".join(sorted(sa - sb))
    db = " ".join(sorted(sb - sa))
    t1 = f"{inter} {da}".strip()
    t2 = f"{inter} {db}".strip()
    scores = [_ratio(t1, t2)]
    if inter:
        scores += [_ratio(inter, t1), _ratio(inter, t2)]
    return max(scores)


@dataclass(frozen=True)
class OwnerMatch:
    candidate_id: object
    candidate_name: str
    score: float
    band: ConfidenceBand
    reasons: tuple[str, ...] = field(default=())


HIGH_THRESHOLD = 92.0
REVIEW_THRESHOLD = 80.0


def score_names(a: NormalizedName, b: NormalizedName) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if not a.key or not b.key:
        return 0.0, ["EMPTY_NAME"]
    if a.key == b.key:
        score, reasons = 100.0, ["EXACT_KEY"]
    elif a.phonetic_key == b.phonetic_key:
        score, reasons = 97.0, ["SPELLING_VARIANT"]
    else:
        variants = [
            0.6 * token_sort_ratio(x, y) + 0.4 * token_set_ratio(x, y)
            for x, y in ((a.key, b.key), (a.phonetic_key, b.phonetic_key))
        ]
        score = max(variants)
        reasons.append("FUZZY")
        # Subset names ("PT ABC" vs "PT ABC INDONESIA") are often distinct group companies.
        sa, sb = set(a.tokens), set(b.tokens)
        if sa != sb and (sa < sb or sb < sa):
            score = min(score, 88.0)
            reasons.append("SUBSET_NAME_POSSIBLE_AFFILIATE")
    if a.alias_of or b.alias_of:
        reasons.append("ALIAS")
    material_a, material_b = a.legal_forms - _NEUTRAL_FORMS, b.legal_forms - _NEUTRAL_FORMS
    if material_a and material_b and not (material_a & material_b):
        score -= 8
        reasons.append("LEGAL_FORM_CONFLICT")
    # INDIVIDUAL/UNKNOWN are heuristic fallbacks (e.g. a brand name without a legal form),
    # so only conflicts between evidence-based types are penalised.
    weak = {OwnerType.UNKNOWN, OwnerType.INDIVIDUAL}
    if a.owner_type not in weak and b.owner_type not in weak and a.owner_type != b.owner_type:
        score -= 15
        reasons.append("OWNER_TYPE_CONFLICT")
    return max(0.0, round(score, 1)), reasons


def band_for(score: float) -> ConfidenceBand:
    if score >= HIGH_THRESHOLD:
        return ConfidenceBand.HIGH
    if score >= REVIEW_THRESHOLD:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


class OwnerMatcher:
    """Index of known owners for repeated lookups (blocking keeps large portfolios fast)."""

    def __init__(self, aliases: dict[str, str] | None = None):
        self.aliases = {**DEFAULT_ALIASES, **(aliases or {})}
        self._entries: list[tuple[object, NormalizedName]] = []
        self._by_token: dict[str, list[int]] = {}

    def add(self, candidate_id: object, name: str, country: str | None = None) -> None:
        norm = normalize_owner_name(name, country, self.aliases)
        idx = len(self._entries)
        self._entries.append((candidate_id, norm))
        for tok in {phonetic(t) for t in norm.tokens}:
            self._by_token.setdefault(tok, []).append(idx)

    def match(self, name: str, country: str | None = None, limit: int = 5, min_score: float = 60.0) -> list[OwnerMatch]:
        query = normalize_owner_name(name, country, self.aliases)
        candidate_idx: set[int] = set()
        for tok in {phonetic(t) for t in query.tokens}:
            candidate_idx.update(self._by_token.get(tok, ()))
        if not candidate_idx and len(self._entries) <= 2000:
            candidate_idx = set(range(len(self._entries)))
        results = []
        for idx in candidate_idx:
            cid, norm = self._entries[idx]
            score, reasons = score_names(query, norm)
            if score >= min_score:
                results.append(OwnerMatch(cid, norm.original, score, band_for(score), tuple(reasons)))
        results.sort(key=lambda m: (-m.score, str(m.candidate_name)))
        return results[:limit]

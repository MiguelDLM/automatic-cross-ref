"""
matcher.py
Compara referencias extraídas contra la biblioteca de Zotero.
Modo solo lectura — genera candidatos de relación con score de confianza.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Any

from rapidfuzz import fuzz, process
from rich.console import Console

from extractor import ParsedReference

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Tipos de datos
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LibraryItem:
    """Item de la biblioteca de Zotero (normalizado)."""
    key: str
    title: str
    doi: Optional[str]
    arxiv_id: Optional[str]
    url: Optional[str]
    year: Optional[str]
    authors: list[str]
    item_type: str
    existing_relations: list[str] = field(default_factory=list)

    _title_normalized: str = field(default="", init=False, repr=False)
    _doi_normalized: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        self._title_normalized = _normalize_title(self.title)
        self._doi_normalized = (self.doi or "").lower().strip()


@dataclass
class MatchResult:
    """Resultado del intento de match de una referencia con un item de la biblioteca."""
    reference: ParsedReference
    matched_item: Optional[LibraryItem]
    confidence: int              # 0-100
    match_method: str            # "doi_exact" | "arxiv_exact" | "title_fuzzy" | "none"
    already_related: bool = False


@dataclass
class RelationCandidate:
    """Una relación candidata entre dos items de la biblioteca."""
    source_key: str              # item que cita
    target_key: str              # item citado
    confidence: int              # 0-100
    match_method: str
    reference_text: str          # texto original de la referencia
    already_exists: bool = False
    # Metadatos del item destino (para el informe)
    target_title: str = ""
    target_authors: list[str] = field(default_factory=list)
    target_year: Optional[str] = None
    target_doi: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades de normalización
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    """Normaliza un título para comparación: minúsculas, sin acentos, sin puntuación."""
    if not title:
        return ""
    # Normalizar unicode (NFD para separar diacríticos)
    t = unicodedata.normalize("NFD", title.lower())
    # Eliminar diacríticos
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # Eliminar puntuación excepto letras y dígitos
    t = re.sub(r"[^\w\s]", " ", t)
    # Colapsar espacios
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_doi_from_raw(raw: dict[str, Any]) -> Optional[str]:
    """Extrae el DOI de un item crudo de Zotero."""
    data = raw.get("data", {})
    doi = data.get("DOI", "") or data.get("doi", "")
    if doi:
        return doi.strip()
    # Buscar en URL
    url = data.get("url", "")
    if url and "doi.org/" in url:
        return url.split("doi.org/")[-1].strip()
    return None


def _extract_arxiv_from_raw(raw: dict[str, Any]) -> Optional[str]:
    """Extrae el ID de arXiv de un item crudo de Zotero."""
    data = raw.get("data", {})
    url = data.get("url", "")
    m = re.search(r'arxiv\.org/abs/(\d+\.\d+)', url, re.IGNORECASE)
    if m:
        return m.group(1)
    extra = data.get("extra", "")
    m = re.search(r'arXiv[\.:\s]+(\d+\.\d+)', extra, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_year_from_raw(raw: dict[str, Any]) -> Optional[str]:
    """Extrae el año de un item crudo de Zotero."""
    data = raw.get("data", {})
    date = data.get("date", "")
    m = re.search(r'(\d{4})', date)
    if m:
        return m.group(1)
    return None


def _extract_authors_from_raw(raw: dict[str, Any]) -> list[str]:
    """Extrae los autores de un item crudo de Zotero."""
    data = raw.get("data", {})
    creators = data.get("creators", [])
    authors = []
    for c in creators:
        if c.get("creatorType") == "author":
            last = c.get("lastName", "")
            first = c.get("firstName", "")
            if last and first:
                authors.append(f"{last}, {first}")
            elif last:
                authors.append(last)
    return authors


# ─────────────────────────────────────────────────────────────────────────────
# ReferenceMatcher
# ─────────────────────────────────────────────────────────────────────────────

class ReferenceMatcher:
    """
    Carga la biblioteca de Zotero y realiza matching de referencias
    extraídas de PDFs contra los items de la biblioteca.

    Estrategia (en orden de prioridad):
      1. DOI exacto (confianza 100)
      2. arXiv ID exacto (confianza 100)
      3. Fuzzy match de título normalizado (confianza variable, según threshold)
    """

    def __init__(
        self,
        fuzzy_threshold: int = 82,
        use_crossref: bool = True,
        crossref_email: str = "",
    ):
        self.fuzzy_threshold = fuzzy_threshold
        self.use_crossref = use_crossref
        self.crossref_email = crossref_email

        self._library: list[LibraryItem] = []
        self._doi_index: dict[str, LibraryItem] = {}        # doi_lower → item
        self._arxiv_index: dict[str, LibraryItem] = {}      # arxiv_id → item
        self._title_index: dict[str, LibraryItem] = {}      # title_normalized → item
        self._title_keys: list[str] = []                    # para rapidfuzz process
        self._existing_relations: dict[str, list[str]] = {} # key → [related_keys]

    # ─────────────────────────────────────────────────────────────────────────
    # Setup
    # ─────────────────────────────────────────────────────────────────────────

    # Títulos genéricos de adjuntos que no deben indexarse como artículos
    _GENERIC_TITLES = frozenset({
        "pdf", "full text pdf", "full text", "article", "paper", "manuscript",
        "preprint", "document", "untitled", "fulltext", "attachment",
    })

    def load_library(self, raw_items: list[dict[str, Any]]) -> int:
        """
        Carga e indexa todos los items de la biblioteca.
        Devuelve el número de items indexados.
        """
        self._library = []
        self._doi_index = {}
        self._arxiv_index = {}
        self._title_index = {}

        for raw in raw_items:
            data = raw.get("data", {})
            key = raw.get("key") or data.get("key", "")
            if not key:
                continue

            title = data.get("title", "") or ""
            if not title:
                continue

            # Filtrar adjuntos con títulos genéricos (ej. "PDF", "Full Text PDF")
            if _normalize_title(title) in self._GENERIC_TITLES:
                continue
            # Filtrar ítems con tipo attachment que se cuelen
            if data.get("itemType", "") == "attachment":
                continue

            item = LibraryItem(
                key=key,
                title=title,
                doi=_extract_doi_from_raw(raw),
                arxiv_id=_extract_arxiv_from_raw(raw),
                url=data.get("url"),
                year=_extract_year_from_raw(raw),
                authors=_extract_authors_from_raw(raw),
                item_type=data.get("itemType", ""),
            )
            self._library.append(item)

            if item.doi:
                self._doi_index[item._doi_normalized] = item
            if item.arxiv_id:
                self._arxiv_index[item.arxiv_id] = item
            if item._title_normalized:
                self._title_index[item._title_normalized] = item

        self._title_keys = list(self._title_index.keys())
        return len(self._library)

    def set_existing_relations(self, existing: dict[str, list[str]]):
        """Recibe las relaciones existentes en Zotero para evitar duplicados."""
        self._existing_relations = existing

    # ─────────────────────────────────────────────────────────────────────────
    # Matching
    # ─────────────────────────────────────────────────────────────────────────

    def match(self, reference: ParsedReference) -> MatchResult:
        """Intenta hacer match de una referencia contra la biblioteca."""

        # 1. DOI exacto
        if reference.doi:
            doi_norm = reference.doi.lower().strip()
            if doi_norm in self._doi_index:
                return MatchResult(
                    reference=reference,
                    matched_item=self._doi_index[doi_norm],
                    confidence=100,
                    match_method="doi_exact",
                )

        # 2. arXiv exacto
        if reference.arxiv_id:
            if reference.arxiv_id in self._arxiv_index:
                return MatchResult(
                    reference=reference,
                    matched_item=self._arxiv_index[reference.arxiv_id],
                    confidence=100,
                    match_method="arxiv_exact",
                )

        # 3. Fuzzy match de título
        if reference.title and len(reference.title) > 8 and self._title_keys:
            query = _normalize_title(reference.title)
            if len(query) > 5:
                best = process.extractOne(
                    query,
                    self._title_keys,
                    scorer=fuzz.token_sort_ratio,
                    score_cutoff=self.fuzzy_threshold,
                )
                if best:
                    matched_title, score, _ = best
                    return MatchResult(
                        reference=reference,
                        matched_item=self._title_index[matched_title],
                        confidence=int(score),
                        match_method="title_fuzzy",
                    )

        return MatchResult(
            reference=reference,
            matched_item=None,
            confidence=0,
            match_method="none",
        )

    def build_relation_candidates(
        self,
        source_item: LibraryItem,
        references: list[ParsedReference],
        min_confidence: int = 70,
    ) -> list[RelationCandidate]:
        """
        Para todas las referencias de un item, intenta hacer match y devuelve
        los candidatos de relación con confianza suficiente.
        Excluye auto-referencias (el item que cita a sí mismo).
        """
        candidates: list[RelationCandidate] = []
        existing = set(self._existing_relations.get(source_item.key, []))

        for ref in references:
            result = self.match(ref)
            if result.matched_item is None:
                continue
            if result.confidence < min_confidence:
                continue
            # Excluir auto-referencia
            if result.matched_item.key == source_item.key:
                continue

            already_exists = result.matched_item.key in existing
            tgt = result.matched_item

            candidates.append(RelationCandidate(
                source_key=source_item.key,
                target_key=tgt.key,
                confidence=result.confidence,
                match_method=result.match_method,
                reference_text=ref.raw_text[:200],
                already_exists=already_exists,
                target_title=tgt.title,
                target_authors=tgt.authors[:3],
                target_year=tgt.year,
                target_doi=tgt.doi,
            ))

        # Deduplicar (mismo target puede aparecer varias veces)
        seen: set[str] = set()
        unique: list[RelationCandidate] = []
        for c in candidates:
            if c.target_key not in seen:
                seen.add(c.target_key)
                unique.append(c)

        return unique

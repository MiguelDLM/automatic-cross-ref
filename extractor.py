"""
extractor.py
Extrae la sección de referencias de PDFs usando pdfminer.six.
Aplica los mismos patrones regex que zotero-reference para parsear entradas.
"""
from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLine
from rich.console import Console

console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Tipos de datos
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedReference:
    """Una referencia extraída del PDF."""
    raw_text: str
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    title: Optional[str] = None
    authors: list[str] = field(default_factory=list)
    year: Optional[str] = None
    url: Optional[str] = None
    ref_index: int = 0

    def identifier_summary(self) -> str:
        if self.doi:
            return f"DOI:{self.doi}"
        if self.arxiv_id:
            return f"arXiv:{self.arxiv_id}"
        if self.title:
            return f"title:{self.title[:60]}"
        return f"raw:{self.raw_text[:60]}"


# ─────────────────────────────────────────────────────────────────────────────
# Patrones (equivalentes a los de zotero-reference/src/modules/pdf.ts)
# ─────────────────────────────────────────────────────────────────────────────

# Patrones que indican el INICIO de una nueva referencia
REF_START_PATTERNS: list[re.Pattern] = [
    re.compile(r'^\(\d+\)\s?'),                          # (1)
    re.compile(r'^\[\d{0,3}\].+?[,.，．]?'),    # [10] Polygon
    re.compile(r'^［\d{0,3}］.+?[,.，．]?'),  # ［1］
    re.compile(r'^\d+[,.，．]'),                 # 1. Polygon
    re.compile(r'^\d+[^\d\w]+?[,.，．]?'),       # 1 Polygon
    re.compile(r'^\[.+?\].+?[,.，．]?'),          # [RCK+20]
    re.compile(r'^\d+\s+'),                               # 1 Polygon (espacio)
]

# Encabezados que indican inicio de sección de referencias
DEFAULT_SECTION_HEADERS = [
    "References", "Bibliography", "参考文献", "REFERENCES", "BIBLIOGRAPHY",
    "Literature cited", "Works cited", "Referencias", "LITERATURA CITADA",
    "Bibliografía", "BIBLIOGRAFÍA",
]

# DOI regex (igual que zotero-reference utils.ts)
DOI_PATTERN = re.compile(r'10\.\d{4,9}/[-._;\(\)/:A-Za-z0-9><]+[^\.\]]')
ARXIV_PATTERN = re.compile(r'arXiv[\.:](\d+\.\d+)', re.IGNORECASE)
URL_PATTERN = re.compile(r'https?://[^\s\.]+')


# ─────────────────────────────────────────────────────────────────────────────
# PDFExtractor
# ─────────────────────────────────────────────────────────────────────────────

class PDFExtractor:
    """
    Extrae la sección de referencias de un PDF y parsea cada entrada.
    Soporta PDFs en inglés, español, y chino.
    """

    def __init__(
        self,
        max_pages_from_end: int = 10,
        min_ref_length: int = 20,
        max_ref_length: int = 1000,
        section_headers: list[str] | None = None,
    ):
        self.max_pages_from_end = max_pages_from_end
        self.min_ref_length = min_ref_length
        self.max_ref_length = max_ref_length
        self.section_headers = section_headers or DEFAULT_SECTION_HEADERS

    def extract(self, pdf_path: str) -> list[ParsedReference]:
        """
        Pipeline completo: lee el PDF, detecta la sección de referencias,
        separa las entradas y parsea cada una.
        """
        lines = self._read_pdf_lines(pdf_path)
        if not lines:
            return []

        ref_lines = self._find_reference_section(lines)
        if not ref_lines:
            return []

        raw_entries = self._merge_reference_lines(ref_lines)
        results = []
        for i, entry in enumerate(raw_entries):
            entry = entry.strip()
            if len(entry) < self.min_ref_length:
                continue
            if len(entry) > self.max_ref_length:
                entry = entry[:self.max_ref_length]
            ref = self._parse_entry(entry, i)
            results.append(ref)
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Lectura del PDF
    # ─────────────────────────────────────────────────────────────────────────

    def _read_pdf_lines(self, pdf_path: str) -> list[str]:
        """Lee las últimas N páginas del PDF y devuelve líneas de texto."""
        try:
            from pdfminer.pdfpage import PDFPage
            with open(pdf_path, "rb") as f:
                num_pages = sum(1 for _ in PDFPage.get_pages(f))

            page_numbers: list[int] | None = None
            if num_pages > self.max_pages_from_end:
                page_numbers = list(range(num_pages - self.max_pages_from_end, num_pages))

            lines: list[str] = []
            pages_iter = extract_pages(str(pdf_path), page_numbers=page_numbers)
            for page in pages_iter:
                for elem in page:
                    if isinstance(elem, LTTextContainer):
                        for line in elem:
                            if isinstance(line, LTTextLine):
                                text = line.get_text().strip()
                                if text:
                                    lines.append(text)
            return lines
        except Exception as e:
            console.print(f"[dim red]Error leyendo PDF: {e}[/dim red]")
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Detección de la sección de referencias
    # ─────────────────────────────────────────────────────────────────────────

    def _find_reference_section(self, lines: list[str]) -> list[str]:
        """
        Busca el inicio de la sección de referencias y devuelve las líneas
        que la siguen. Busca desde el final hacia atrás para coger la última
        sección de referencias (en caso de que haya varias).
        """
        # Buscar desde el final para coger la sección más tardía
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            for header in self.section_headers:
                if line.lower() == header.lower() or line.lower().startswith(header.lower() + " "):
                    return lines[i + 1:]
            # Encabezado con numeración de sección: "5. References", "6 Bibliography"
            for header in self.section_headers:
                if re.match(rf'^\d+\.?\s+{re.escape(header)}\s*$', line, re.IGNORECASE):
                    return lines[i + 1:]

        # Fallback: buscar en el texto la primera aparición de un encabezado
        for i, line in enumerate(lines):
            stripped = line.strip()
            for header in self.section_headers:
                if stripped.lower() == header.lower():
                    return lines[i + 1:]

        return []

    # ─────────────────────────────────────────────────────────────────────────
    # Detección de inicio de referencia
    # ─────────────────────────────────────────────────────────────────────────

    def _is_ref_start(self, line: str) -> bool:
        """Devuelve True si la línea parece el inicio de una nueva referencia."""
        for pattern in REF_START_PATTERNS:
            if pattern.match(line):
                return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Merge de líneas en entradas completas
    # ─────────────────────────────────────────────────────────────────────────

    def _merge_reference_lines(self, lines: list[str]) -> list[str]:
        """
        Agrupa líneas de texto en entradas de referencia completas.
        Soporta dos estilos:
          - Numerado: [1], (1), 1. etc. → nueva ref al detectar patrón
          - APA hanging-indent: autor en línea corta + contenido en líneas siguientes
          - Sin patrón: línea larga = referencia completa
        """
        if not lines:
            return []

        # ¿Hay patrones de inicio reconocibles?
        has_pattern = any(self._is_ref_start(l) for l in lines[:20])

        entries: list[str] = []
        current: list[str] = []

        if has_pattern:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if self._is_ref_start(line):
                    if current:
                        entries.append(" ".join(current))
                    current = [line]
                else:
                    if current:
                        if current[-1].endswith("-"):
                            current[-1] = current[-1][:-1] + line
                        else:
                            current.append(line)
                    # Ignorar líneas sueltas antes de la primera referencia
        else:
            # Estilo APA hanging-indent: líneas cortas = solo autores,
            # líneas siguientes = continuación
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue

                # ¿Esta línea parece ser solo autores (corta, sin año, sin DOI)?
                is_author_only = (
                    len(line) < 70
                    and not DOI_PATTERN.search(line)
                    and not re.search(r'\d{4}[\s.,]', line)
                    and re.search(r'[A-Z][a-z]', line)
                    and not re.search(r'[.]{2,}', line)
                )

                merged = [line]
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if not nxt:
                        j += 1
                        continue
                    nxt_is_author = (
                        len(nxt) < 70
                        and not DOI_PATTERN.search(nxt)
                        and re.search(r'^[A-Z][a-z]+,?\s+[A-Z]', nxt)
                        and len(merged) > 2
                    )
                    if nxt_is_author:
                        break
                    if merged[-1].endswith("-"):
                        merged[-1] = merged[-1][:-1] + nxt
                    else:
                        merged.append(nxt)
                    j += 1
                    if len(" ".join(merged)) > 400:
                        break

                entry = " ".join(merged)
                if len(entry) >= self.min_ref_length:
                    entries.append(entry)
                i = j

        if current:
            entries.append(" ".join(current))

        return entries

    # ─────────────────────────────────────────────────────────────────────────
    # Parseo de DOI / arXiv / título / año / autores
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_doi(self, text: str) -> Optional[str]:
        """Extrae el DOI del texto de la referencia."""
        # Eliminar espacios para capturar DOIs con saltos de línea
        text_nospace = text.replace(" ", "")
        m = DOI_PATTERN.search(text_nospace)
        if m:
            doi = m.group(0)
            # Filtrar falsos positivos de CNKI/ISSN
            if re.search(r'(cnki|issn)', doi, re.IGNORECASE):
                return None
            return doi
        return None

    def _parse_arxiv(self, text: str) -> Optional[str]:
        """Extrae el ID de arXiv del texto."""
        m = ARXIV_PATTERN.search(text)
        if m:
            return m.group(1)
        return None

    def _parse_year(self, text: str) -> Optional[str]:
        """Extrae el año de publicación."""
        current_year = 2026
        candidates = re.findall(r'(?<!\d)(\d{4})(?!\d)', text)
        for c in candidates:
            y = int(c)
            if 1900 < y <= current_year + 1:
                return c
        return None

    def _parse_title_authors(self, text: str) -> tuple[Optional[str], list[str]]:
        """
        Intenta extraer el título y autores de una referencia en texto plano.
        Heurística: el fragmento más largo sin muchas abreviaciones es el título.
        """
        # Quitar el número de referencia al inicio
        text = re.sub(r'^\[?\d+\]?\s*\.?\s*', '', text)
        text = re.sub(r'^\(\d+\)\s*', '', text)

        title: Optional[str] = None
        authors: list[str] = []

        # Título entre comillas tipográficas
        m = re.search(r'“(.+?)”', text)
        if m:
            title = m.group(1).rstrip(',').strip()

        if not title:
            # Dividir por ". " y coger el fragmento más largo con >= 4 palabras
            parts = re.split(r'\.\s+', text)
            candidates = [p for p in parts if len(p.split()) >= 4]
            if candidates:
                # El título tiende a tener menos símbolos/abreviaturas
                def score(s: str) -> float:
                    sym = len(re.findall(r'[A-Z]\.|[,\.\-\(\)\:]', s))
                    return sym / max(len(s), 1)
                candidates.sort(key=score)
                title = candidates[0].strip()

        # Autores: texto antes del título (si lo tenemos)
        if title and title in text:
            before = text.split(title)[0].strip().rstrip(',. ')
            if before:
                authors = [before]

        return title, authors

    def _parse_entry(self, raw: str, index: int) -> ParsedReference:
        """Parsea una sola entrada de referencia."""
        doi = self._parse_doi(raw)
        arxiv_id = self._parse_arxiv(raw)
        year = self._parse_year(raw)
        title, authors = self._parse_title_authors(raw)

        url_m = URL_PATTERN.search(raw)
        url = url_m.group(0) if url_m else None

        return ParsedReference(
            raw_text=raw,
            doi=doi,
            arxiv_id=arxiv_id,
            title=title,
            authors=authors,
            year=year,
            url=url,
            ref_index=index,
        )

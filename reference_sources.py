"""
reference_sources.py
Busca referencias en fuentes externas para obtener listas de citas
cuando el PDF no tiene texto extraíble o el DOI del artículo ya se conoce.
Fuentes: Semantic Scholar · OpenAlex · CrossRef · PubMed
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional, Any
from dataclasses import dataclass, field

import httpx
from rich.console import Console

console = Console()


@dataclass
class ExternalReference:
    """Una referencia obtenida de una fuente externa."""
    title: Optional[str]
    doi: Optional[str]
    arxiv_id: Optional[str]
    year: Optional[str]
    authors: list[str] = field(default_factory=list)
    source: str = ""
    raw: dict = field(default_factory=dict)


class ReferenceSourceClient:
    """
    Consulta fuentes académicas externas para obtener las referencias
    citadas por un artículo dado su DOI o título.
    Prioridad: Semantic Scholar → OpenAlex → CrossRef → PubMed
    """

    def __init__(
        self,
        semantic_scholar_key: str = "",
        crossref_email: str = "",
        openalex_email: str = "",
        ncbi_api_key: str = "",
        request_delay: float = 0.1,
    ):
        self.ss_key = semantic_scholar_key
        self.crossref_email = crossref_email
        self.openalex_email = openalex_email
        self.ncbi_key = ncbi_api_key
        self.delay = request_delay

        headers = {"User-Agent": "ZoteroReferenceAutomator/1.0 (academic research)"}
        if semantic_scholar_key:
            headers["x-api-key"] = semantic_scholar_key
        self._http = httpx.Client(timeout=20.0, headers=headers, follow_redirects=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_references_by_doi(self, doi: str) -> list[ExternalReference]:
        """
        Devuelve las referencias citadas por el artículo con ese DOI.
        Intenta las fuentes en orden de prioridad.
        """
        for method in (
            self._ss_references,
            self._openalex_references,
            self._crossref_references,
            self._pubmed_references,
        ):
            try:
                refs = method(doi)
                if refs:
                    return refs
                time.sleep(self.delay)
            except Exception as e:
                console.print(f"[dim]Error en {method.__name__}: {e}[/dim]")
        return []

    def close(self):
        self._http.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Semantic Scholar
    # ─────────────────────────────────────────────────────────────────────────

    def _ss_references(self, doi: str) -> list[ExternalReference]:
        """Obtiene referencias desde Semantic Scholar."""
        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}/references"
        params = {
            "fields": "title,authors,year,externalIds",
            "limit": 500,
        }
        r = self._http.get(url, params=params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()

        refs = []
        for item in data.get("data", []):
            cited = item.get("citedPaper", {})
            if not cited:
                continue
            refs.append(self._parse_ss_paper(cited, "semantic_scholar"))
        return refs

    def _parse_ss_paper(self, paper: dict, source: str) -> ExternalReference:
        ext_ids = paper.get("externalIds", {}) or {}
        doi = ext_ids.get("DOI") or ext_ids.get("doi")
        arxiv_id = ext_ids.get("ArXiv")
        authors = [
            a.get("name", "") for a in (paper.get("authors") or [])
        ]
        year = paper.get("year")
        return ExternalReference(
            title=paper.get("title"),
            doi=doi,
            arxiv_id=arxiv_id,
            year=str(year) if year else None,
            authors=authors,
            source=source,
            raw=paper,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # OpenAlex
    # ─────────────────────────────────────────────────────────────────────────

    def _openalex_references(self, doi: str) -> list[ExternalReference]:
        """Obtiene referencias desde OpenAlex."""
        mailto = f"?mailto={self.openalex_email}" if self.openalex_email else ""
        url = f"https://api.openalex.org/works/doi:{doi}"
        r = self._http.get(url)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        work = r.json()

        referenced_ids = []
        for ref in work.get("referenced_works", []):
            referenced_ids.append(ref)

        if not referenced_ids:
            return []

        # Obtener detalles de cada trabajo referenciado (en batch de 50)
        refs = []
        batch_size = 50
        for i in range(0, len(referenced_ids), batch_size):
            batch = referenced_ids[i:i + batch_size]
            ids_str = "|".join(batch)
            br = self._http.get(
                "https://api.openalex.org/works",
                params={"filter": f"ids.openalex:{ids_str}", "per-page": batch_size},
            )
            if br.status_code != 200:
                continue
            bdata = br.json()
            for item in bdata.get("results", []):
                refs.append(self._parse_openalex_work(item))
            time.sleep(self.delay)

        return refs

    def _parse_openalex_work(self, work: dict) -> ExternalReference:
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        title = work.get("display_name") or work.get("title")
        year = work.get("publication_year")
        authors = []
        for auth in (work.get("authorships") or []):
            name = auth.get("author", {}).get("display_name", "")
            if name:
                authors.append(name)

        # arXiv desde IDs alternativos
        arxiv_id = None
        for loc in (work.get("locations") or []):
            landing = loc.get("landing_page_url", "") or ""
            m = re.search(r'arxiv\.org/abs/(\d+\.\d+)', landing, re.IGNORECASE)
            if m:
                arxiv_id = m.group(1)
                break

        return ExternalReference(
            title=title,
            doi=doi or None,
            arxiv_id=arxiv_id,
            year=str(year) if year else None,
            authors=authors,
            source="openalex",
            raw=work,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # CrossRef
    # ─────────────────────────────────────────────────────────────────────────

    def _crossref_references(self, doi: str) -> list[ExternalReference]:
        """Obtiene referencias desde CrossRef."""
        url = f"https://api.crossref.org/works/{doi}"
        params = {}
        if self.crossref_email:
            params["mailto"] = self.crossref_email

        r = self._http.get(url, params=params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json().get("message", {})

        raw_refs = data.get("reference", [])
        refs = []
        for ref in raw_refs:
            refs.append(self._parse_crossref_ref(ref))
        return refs

    def _parse_crossref_ref(self, ref: dict) -> ExternalReference:
        doi = ref.get("DOI") or ref.get("doi")
        title = ref.get("article-title") or ref.get("volume-title") or ref.get("unstructured")
        year = ref.get("year")
        authors = []
        author = ref.get("author")
        if author:
            authors = [author]
        return ExternalReference(
            title=title,
            doi=doi or None,
            arxiv_id=None,
            year=str(year) if year else None,
            authors=authors,
            source="crossref",
            raw=ref,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # PubMed
    # ─────────────────────────────────────────────────────────────────────────

    def _pubmed_references(self, doi: str) -> list[ExternalReference]:
        """Obtiene referencias desde PubMed (via NCBI eutils)."""
        # Paso 1: resolver DOI → PMID
        pmid = self._doi_to_pmid(doi)
        if not pmid:
            return []

        # Paso 2: obtener referencias del artículo
        return self._pmid_references(pmid)

    def _doi_to_pmid(self, doi: str) -> Optional[str]:
        """Convierte un DOI a PMID usando NCBI eutils."""
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": f"{doi}[doi]",
            "retmode": "json",
            "retmax": 1,
        }
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key

        r = self._http.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params)
        if r.status_code != 200:
            return None
        data = r.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
        return ids[0] if ids else None

    def _pmid_references(self, pmid: str) -> list[ExternalReference]:
        """Obtiene referencias de un artículo PubMed por PMID."""
        params: dict[str, Any] = {
            "dbfrom": "pubmed",
            "db": "pubmed",
            "id": pmid,
            "linkname": "pubmed_pubmed_refs",
            "retmode": "json",
        }
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key

        r = self._http.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi", params=params)
        if r.status_code != 200:
            return []
        data = r.json()

        ref_pmids = []
        for linkset in data.get("linksets", []):
            for db in linkset.get("linksetdbs", []):
                if db.get("linkname") == "pubmed_pubmed_refs":
                    ref_pmids.extend(db.get("links", []))

        if not ref_pmids:
            return []

        # Obtener detalles de los PMID referenciados (hasta 100)
        ref_pmids = ref_pmids[:100]
        return self._fetch_pubmed_details(ref_pmids)

    def _fetch_pubmed_details(self, pmids: list[str]) -> list[ExternalReference]:
        """Obtiene detalles de artículos PubMed por lista de PMIDs."""
        params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "json",
            "rettype": "abstract",
        }
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key

        r = self._http.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi", params=params)
        if r.status_code != 200:
            return []
        data = r.json()

        refs = []
        result = data.get("result", {})
        for pmid in result.get("uids", []):
            article = result.get(pmid, {})
            title = article.get("title", "")
            year = article.get("pubdate", "")[:4] if article.get("pubdate") else None
            authors = [a.get("name", "") for a in article.get("authors", [])]
            # DOI desde ArticleIds
            doi = None
            for aid in article.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value")
                    break
            refs.append(ExternalReference(
                title=title or None,
                doi=doi,
                arxiv_id=None,
                year=year,
                authors=authors,
                source="pubmed",
                raw=article,
            ))
        return refs

    def _guess_source(self, refs: list[ExternalReference]) -> str:
        """Devuelve el nombre de la fuente mayoritaria."""
        if not refs:
            return "unknown"
        return refs[0].source

"""
reference_sources.py
Busca referencias en fuentes externas para maximizar la cobertura de citas.
Consulta TODAS las fuentes siempre; si una devuelve 429 la encola para
reintento al final del pipeline sin bloquear el proceso.
Fuentes: Semantic Scholar · OpenAlex · CrossRef · CORE · PubMed
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Optional, Any
from dataclasses import dataclass, field

import httpx
from rich.console import Console

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Tipos y excepciones internas
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimited(Exception):
    """La API respondió 429 — no bloquear, encolar para reintento."""
    def __init__(self, retry_after: float = 60.0):
        self.retry_after = retry_after


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


# ─────────────────────────────────────────────────────────────────────────────
# Cliente principal
# ─────────────────────────────────────────────────────────────────────────────

class ReferenceSourceClient:
    """
    Consulta TODAS las fuentes académicas para maximizar la cobertura.

    Estrategia:
      - Intenta Semantic Scholar, OpenAlex, CrossRef, CORE y PubMed en paralelo
        lógico (secuencial para respetar rate limits).
      - Si una fuente responde 429, la encola para reintento posterior;
        el proceso continúa sin esperar.
      - Deduplica por DOI normalizado (primera fuente que lo aporte gana).
      - Al terminar el pipeline, llama a flush_retry_queue() para recuperar
        las refs que fueron rechazadas por rate limit.
    """

    _SOURCE_METHODS = (
        "_ss_references",
        "_openalex_references",
        "_crossref_references",
        "_core_references",
        "_pubmed_references",
    )

    def __init__(
        self,
        semantic_scholar_key: str = "",
        crossref_email: str = "",
        openalex_email: str = "",
        ncbi_api_key: str = "",
        core_api_key: str = "",
        request_delay: float = 0.1,
    ):
        self.ss_key = semantic_scholar_key
        self.crossref_email = crossref_email
        self.openalex_email = openalex_email
        self.ncbi_key = ncbi_api_key
        self.core_key = core_api_key
        self.delay = request_delay

        headers = {"User-Agent": "ZoteroReferenceAutomator/1.0 (academic research)"}
        if semantic_scholar_key:
            headers["x-api-key"] = semantic_scholar_key
        self._http = httpx.Client(timeout=20.0, headers=headers, follow_redirects=True)

        core_headers = {"User-Agent": "ZoteroReferenceAutomator/1.0 (academic research)"}
        if core_api_key:
            core_headers["Authorization"] = f"Bearer {core_api_key}"
        self._core_http = httpx.Client(timeout=20.0, headers=core_headers, follow_redirects=True)

        # Cola de reintentos: (doi, method_name, retry_after_segundos)
        self._retry_queue: deque[tuple[str, str, float]] = deque()

    # ─────────────────────────────────────────────────────────────────────────
    # Helper HTTP
    # ─────────────────────────────────────────────────────────────────────────

    def _checked_get(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        """GET que convierte 429 en _RateLimited para no bloquear el pipeline."""
        r = client.get(url, **kwargs)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", 60))
            raise _RateLimited(retry_after)
        return r

    # ─────────────────────────────────────────────────────────────────────────
    # API pública
    # ─────────────────────────────────────────────────────────────────────────

    def get_references_by_doi(self, doi: str) -> list[ExternalReference]:
        """
        Consulta TODAS las fuentes y fusiona los resultados.
        Deduplica por DOI (primera fuente que lo aporte gana).
        Los 429 se encolan para reintento al final del pipeline.
        """
        all_refs: list[ExternalReference] = []
        seen_dois: set[str] = set()

        for method_name in self._SOURCE_METHODS:
            method = getattr(self, method_name)
            try:
                refs = method(doi)
                for r in refs:
                    doi_key = (r.doi or "").lower().strip()
                    if doi_key and doi_key in seen_dois:
                        continue
                    if doi_key:
                        seen_dois.add(doi_key)
                    all_refs.append(r)
                if refs:
                    time.sleep(self.delay)
            except _RateLimited as e:
                console.print(
                    f"[dim yellow]{method_name}: rate limit — "
                    f"se reintentará en ≥{e.retry_after:.0f}s[/dim yellow]"
                )
                self._retry_queue.append((doi, method_name, e.retry_after))
            except Exception as e:
                console.print(f"[dim]{method_name}: {type(e).__name__}: {e}[/dim]")

        return all_refs

    def has_pending_retries(self) -> bool:
        return bool(self._retry_queue)

    def flush_retry_queue(self) -> dict[str, list[ExternalReference]]:
        """
        Procesa la cola de reintentos acumulada durante el pipeline.

        Agrupa los reintentos por fuente para minimizar el número de esperas
        (se espera una sola vez el máximo retry_after de cada fuente).
        Devuelve {doi_normalizado: [refs_recuperadas]}.
        """
        if not self._retry_queue:
            return {}

        total = len(self._retry_queue)
        console.print(f"\n[yellow]Cola de reintentos: {total} peticiones pendientes[/yellow]")

        # Agrupar por método: {method_name: [(doi, retry_after), ...]}
        by_method: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for doi, method_name, retry_after in self._retry_queue:
            by_method[method_name].append((doi, retry_after))
        self._retry_queue.clear()

        recovered: dict[str, list[ExternalReference]] = {}

        for method_name, items in by_method.items():
            wait = max(ra for _, ra in items)
            console.print(
                f"[dim]Esperando {wait:.0f}s antes de reintentar "
                f"{method_name} ({len(items)} DOIs)...[/dim]"
            )
            time.sleep(wait)

            method = getattr(self, method_name)
            for doi, _ in items:
                try:
                    refs = method(doi)
                    if refs:
                        doi_lower = doi.lower()
                        existing_dois = {(r.doi or "").lower() for r in recovered.get(doi_lower, [])}
                        new_refs = [
                            r for r in refs
                            if not ((r.doi or "").lower() and (r.doi or "").lower() in existing_dois)
                        ]
                        if new_refs:
                            recovered.setdefault(doi_lower, []).extend(new_refs)
                            console.print(
                                f"[dim green]{method_name}: +{len(new_refs)} refs "
                                f"para {doi[:45]}[/dim green]"
                            )
                    time.sleep(self.delay)
                except _RateLimited:
                    console.print(
                        f"[dim red]{method_name}: sigue con rate limit — "
                        f"se omite {doi[:30]}...[/dim red]"
                    )
                except Exception as e:
                    console.print(f"[dim]{method_name}: {e}[/dim]")

        return recovered

    def sources_summary(self, refs: list[ExternalReference]) -> str:
        """Devuelve un string con las fuentes únicas que aportaron refs."""
        sources = list(dict.fromkeys(r.source for r in refs if r.source))
        return " · ".join(sources) if sources else "unknown"

    def close(self):
        self._http.close()
        self._core_http.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Semantic Scholar
    # ─────────────────────────────────────────────────────────────────────────

    def _ss_references(self, doi: str) -> list[ExternalReference]:
        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}/references"
        r = self._checked_get(self._http, url, params={
            "fields": "title,authors,year,externalIds", "limit": 500,
        })
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        refs = []
        for item in (data.get("data") or []):
            cited = item.get("citedPaper") or {}
            if cited:
                refs.append(self._parse_ss_paper(cited, "semantic_scholar"))
        return refs

    def _parse_ss_paper(self, paper: dict, source: str) -> ExternalReference:
        ext_ids = paper.get("externalIds", {}) or {}
        doi = ext_ids.get("DOI") or ext_ids.get("doi")
        arxiv_id = ext_ids.get("ArXiv")
        authors = [a.get("name", "") for a in (paper.get("authors") or [])]
        year = paper.get("year")
        return ExternalReference(
            title=paper.get("title"),
            doi=doi, arxiv_id=arxiv_id,
            year=str(year) if year else None,
            authors=authors, source=source, raw=paper,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # OpenAlex
    # ─────────────────────────────────────────────────────────────────────────

    def _openalex_references(self, doi: str) -> list[ExternalReference]:
        params: dict = {}
        if self.openalex_email:
            params["mailto"] = self.openalex_email

        r = self._checked_get(self._http, f"https://api.openalex.org/works/doi:{doi}", params=params)
        if r.status_code == 404:
            return []
        r.raise_for_status()

        referenced_ids = r.json().get("referenced_works", [])
        if not referenced_ids:
            return []

        refs = []
        batch_size = 50
        for i in range(0, len(referenced_ids), batch_size):
            batch = referenced_ids[i:i + batch_size]
            batch_params: dict = {
                "filter": f"ids.openalex:{'|'.join(batch)}",
                "per-page": batch_size,
            }
            if self.openalex_email:
                batch_params["mailto"] = self.openalex_email
            try:
                br = self._checked_get(self._http, "https://api.openalex.org/works", params=batch_params)
            except _RateLimited:
                raise  # propagar al handler externo
            if br.status_code != 200:
                continue
            for item in br.json().get("results", []):
                refs.append(self._parse_openalex_work(item))
            time.sleep(self.delay)
        return refs

    def _parse_openalex_work(self, work: dict) -> ExternalReference:
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        title = work.get("display_name") or work.get("title")
        year = work.get("publication_year")
        authors = [
            auth.get("author", {}).get("display_name", "")
            for auth in (work.get("authorships") or [])
            if auth.get("author", {}).get("display_name")
        ]
        arxiv_id = None
        for loc in (work.get("locations") or []):
            m = re.search(
                r'arxiv\.org/abs/(\d+\.\d+)',
                loc.get("landing_page_url", "") or "",
                re.IGNORECASE,
            )
            if m:
                arxiv_id = m.group(1)
                break
        return ExternalReference(
            title=title, doi=doi or None, arxiv_id=arxiv_id,
            year=str(year) if year else None,
            authors=authors, source="openalex", raw=work,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # CrossRef
    # ─────────────────────────────────────────────────────────────────────────

    def _crossref_references(self, doi: str) -> list[ExternalReference]:
        params: dict = {}
        if self.crossref_email:
            params["mailto"] = self.crossref_email
        r = self._checked_get(self._http, f"https://api.crossref.org/works/{doi}", params=params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        raw_refs = r.json().get("message", {}).get("reference", [])
        return [self._parse_crossref_ref(ref) for ref in raw_refs]

    def _parse_crossref_ref(self, ref: dict) -> ExternalReference:
        doi = ref.get("DOI") or ref.get("doi")
        title = (
            ref.get("article-title")
            or ref.get("volume-title")
            or ref.get("unstructured")
        )
        year = ref.get("year")
        authors = [ref["author"]] if ref.get("author") else []
        return ExternalReference(
            title=title, doi=doi or None, arxiv_id=None,
            year=str(year) if year else None,
            authors=authors, source="crossref", raw=ref,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # CORE (core.ac.uk)
    # ─────────────────────────────────────────────────────────────────────────

    def _core_references(self, doi: str) -> list[ExternalReference]:
        if not self.core_key:
            return []
        r = self._checked_get(
            self._core_http,
            "https://api.core.ac.uk/v3/search/works",
            params={"q": f'doi:"{doi}"', "limit": 1},
        )
        if r.status_code == 401:
            console.print("[dim yellow]CORE API: clave inválida o expirada[/dim yellow]")
            return []
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
        if not results:
            return []
        raw_refs = results[0].get("references") or []
        refs = []
        for ref in raw_refs:
            if isinstance(ref, str):
                refs.append(ExternalReference(
                    title=ref[:300], doi=None, arxiv_id=None,
                    year=None, authors=[], source="core", raw={"rawText": ref},
                ))
            elif isinstance(ref, dict):
                refs.append(self._parse_core_ref(ref))
        return refs

    def _parse_core_ref(self, ref: dict) -> ExternalReference:
        doi = ref.get("doi") or ref.get("DOI")
        title = ref.get("title")
        year = ref.get("year") or (ref.get("publishedDate", "") or "")[:4] or None
        authors = []
        for a in (ref.get("authors") or []):
            name = a.get("name", "") if isinstance(a, dict) else str(a)
            if name:
                authors.append(name)
        arxiv_id = None
        for field_name in ("downloadUrl", "fullTextIdentifier"):
            m = re.search(
                r'arxiv\.org/abs/(\d+\.\d+)',
                ref.get(field_name, "") or "",
                re.IGNORECASE,
            )
            if m:
                arxiv_id = m.group(1)
                break
        return ExternalReference(
            title=title, doi=doi or None, arxiv_id=arxiv_id,
            year=str(year) if year else None,
            authors=authors, source="core", raw=ref,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # PubMed (NCBI eutils)
    # ─────────────────────────────────────────────────────────────────────────

    def _pubmed_references(self, doi: str) -> list[ExternalReference]:
        pmid = self._doi_to_pmid(doi)
        if not pmid:
            return []
        return self._pmid_references(pmid)

    def _doi_to_pmid(self, doi: str) -> Optional[str]:
        params: dict[str, Any] = {
            "db": "pubmed", "term": f"{doi}[doi]",
            "retmode": "json", "retmax": 1,
        }
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key
        r = self._checked_get(
            self._http,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=params,
        )
        if r.status_code != 200:
            return None
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        return ids[0] if ids else None

    def _pmid_references(self, pmid: str) -> list[ExternalReference]:
        params: dict[str, Any] = {
            "dbfrom": "pubmed", "db": "pubmed", "id": pmid,
            "linkname": "pubmed_pubmed_refs", "retmode": "json",
        }
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key
        r = self._checked_get(
            self._http,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
            params=params,
        )
        if r.status_code != 200:
            return []
        ref_pmids: list[str] = []
        for linkset in r.json().get("linksets", []):
            for db in linkset.get("linksetdbs", []):
                if db.get("linkname") == "pubmed_pubmed_refs":
                    ref_pmids.extend(db.get("links", []))
        if not ref_pmids:
            return []
        return self._fetch_pubmed_details(ref_pmids[:100])

    def _fetch_pubmed_details(self, pmids: list[str]) -> list[ExternalReference]:
        params: dict[str, Any] = {
            "db": "pubmed", "id": ",".join(pmids),
            "retmode": "json", "rettype": "abstract",
        }
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key
        r = self._checked_get(
            self._http,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params=params,
        )
        if r.status_code != 200:
            return []
        result = r.json().get("result", {})
        refs = []
        for pmid in result.get("uids", []):
            article = result.get(pmid, {})
            title = article.get("title")
            year = article.get("pubdate", "")[:4] if article.get("pubdate") else None
            authors = [a.get("name", "") for a in article.get("authors", [])]
            doi = None
            for aid in article.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value")
                    break
            refs.append(ExternalReference(
                title=title or None, doi=doi, arxiv_id=None,
                year=year, authors=authors, source="pubmed", raw=article,
            ))
        return refs

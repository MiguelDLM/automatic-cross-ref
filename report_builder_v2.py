"""
report_builder_v2.py
Genera el informe HTML v2 — incluye diagnóstico completo de todos los items,
tabla resumen de estados, grafo D3.js, y panel de items sin match.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from jinja2 import Environment, BaseLoader
from matcher import RelationCandidate, LibraryItem


# ─────────────────────────────────────────────────────────────────────────────
# Tipos de datos
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PDFDiagnostic:
    status: str
    detail: str
    path: Optional[str] = None
    external_source: Optional[str] = None


@dataclass
class ReportStats:
    total_items_in_library: int = 0
    items_with_pdf: int = 0
    items_processed: int = 0
    total_refs_extracted: int = 0
    refs_matched: int = 0
    refs_unmatched: int = 0
    new_relations: int = 0
    existing_relations: int = 0
    processing_time_s: float = 0.0


@dataclass
class ItemReport:
    item: LibraryItem
    pdf_path: Optional[str]
    refs_extracted: int
    candidates: list[RelationCandidate]
    error: Optional[str] = None
    diagnostic: Optional[PDFDiagnostic] = None
    external_refs_used: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Etiquetas y colores de estado
# ─────────────────────────────────────────────────────────────────────────────

STATUS_META = {
    "ok":            {"label": "OK",             "color": "#22c55e", "icon": "✓"},
    "ocr_needed":    {"label": "Sin texto/OCR",  "color": "#f97316", "icon": "⚠"},
    "few_refs":      {"label": "Pocas refs",     "color": "#eab308", "icon": "~"},
    "no_attachment": {"label": "Sin PDF",        "color": "#94a3b8", "icon": "−"},
    "not_found":     {"label": "PDF no hallado", "color": "#ef4444", "icon": "✗"},
    "linked_file":   {"label": "Vinculado",      "color": "#a78bfa", "icon": "↗"},
    "error":         {"label": "Error",          "color": "#ef4444", "icon": "!"},
}

METHOD_LABELS = {
    "doi_exact":   "DOI exacto",
    "arxiv_exact": "arXiv exacto",
    "title_fuzzy": "Título fuzzy",
    "none":        "Sin match",
}


# ─────────────────────────────────────────────────────────────────────────────
# ReportBuilder
# ─────────────────────────────────────────────────────────────────────────────

class ReportBuilder:
    """Genera el informe HTML interactivo y el JSON de relaciones."""

    def build(
        self,
        stats: ReportStats,
        reports: list[ItemReport],
        output_dir: str = "output",
    ) -> tuple[str, str, str]:
        """
        Genera informe HTML + JSON de relaciones + JSON de verificación.
        Devuelve (html_path, relations_json_path, verification_json_path).
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path         = str(out / f"report_{now}.html")
        json_path         = str(out / f"relations_{now}.json")
        verification_path = str(out / f"verification_{now}.json")

        graph_data = self._build_graph(reports)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)

        verification = self._build_verification(stats, reports, now)
        with open(verification_path, "w", encoding="utf-8") as f:
            json.dump(verification, f, ensure_ascii=False, indent=2)

        html = self._render_html(stats, reports, graph_data, json_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        return html_path, json_path, verification_path

    # ─────────────────────────────────────────────────────────────────────────
    # Construcción del grafo (compatible con zotero-style)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_graph(self, reports: list[ItemReport]) -> dict:
        """
        Construye el grafo de relaciones en formato compatible con zotero-style:
        { nodes: {id: {title, type, links: {id: true}}}, links: [...] }
        """
        nodes: dict[str, dict] = {}
        links: list[dict] = []

        for report in reports:
            item = report.item
            node_id = item.key
            if node_id not in nodes:
                nodes[node_id] = {
                    "id": node_id,
                    "title": item.title,
                    "type": "item",
                    "links": {},
                    "doi": item.doi or "",
                    "year": item.year or "",
                    "authors": item.authors[:3],
                }

            for candidate in report.candidates:
                target_id = candidate.target_key
                nodes[node_id]["links"][target_id] = True

                links.append({
                    "source": node_id,
                    "target": target_id,
                    "confidence": candidate.confidence,
                    "method": candidate.match_method,
                    "already_exists": candidate.already_exists,
                    "reference_text": candidate.reference_text,
                })

        # Añadir nodos target que no fueron procesados como source
        for link in links:
            tid = link["target"]
            if tid not in nodes:
                nodes[tid] = {
                    "id": tid,
                    "title": tid,
                    "type": "item",
                    "links": {},
                    "doi": "",
                    "year": "",
                    "authors": [],
                }

        return {"nodes": nodes, "links": links}

    # ─────────────────────────────────────────────────────────────────────────
    # JSON de verificación (para agente de IA)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_verification(
        self,
        stats: ReportStats,
        reports: list[ItemReport],
        timestamp: str,
    ) -> dict:
        """
        Genera un JSON estructurado para revisión posterior por IA o humano.
        Cada match incluye toda la evidencia que justifica (o no) la asignación.
        """
        matches = []
        for report in reports:
            src = report.item
            for c in report.candidates:
                if c.already_exists:
                    continue
                match_id = f"{c.source_key}__{c.target_key}"
                method_label = METHOD_LABELS.get(c.match_method, c.match_method)

                matches.append({
                    "id": match_id,
                    "status": "pending",
                    "ai_notes": None,
                    "user_notes": None,
                    "source": {
                        "key": src.key,
                        "title": src.title,
                        "authors": src.authors,
                        "year": src.year,
                        "doi": src.doi,
                    },
                    "target": {
                        "key": c.target_key,
                        "title": c.target_title,
                        "authors": c.target_authors,
                        "year": c.target_year,
                        "doi": c.target_doi,
                    },
                    "evidence": {
                        "confidence": c.confidence,
                        "method": c.match_method,
                        "method_label": method_label,
                        "raw_reference_text": c.reference_text,
                        "ref_extracted_doi": c.ref_extracted_doi,
                        "ref_extracted_title": c.ref_extracted_title,
                        "ref_normalized_title": c.ref_normalized_title,
                        "target_normalized_title": c.target_normalized_title,
                        "ref_extracted_year": c.ref_year,
                        "ref_extracted_authors": c.ref_authors,
                    },
                })

        new_count = len(matches)
        existing_count = sum(
            1 for rep in reports for c in rep.candidates if c.already_exists
        )

        return {
            "generated_at": timestamp,
            "version": "1.0",
            "ai_instructions": (
                "Review each entry in 'matches' where status='pending'. "
                "For each match, determine whether the source article plausibly cites "
                "the target article. Use 'evidence.raw_reference_text' as the extracted "
                "citation, 'evidence.ref_extracted_title' as the parsed title, and compare "
                "with 'target.title'. For DOI matches (method='doi_exact'), confidence is "
                "always 100%% — approve unless titles are completely unrelated. "
                "For fuzzy matches (method='title_fuzzy'), inspect "
                "'evidence.ref_normalized_title' vs 'evidence.target_normalized_title'. "
                "Set status to 'approved' or 'rejected', add a brief 'ai_notes' string "
                "explaining the decision. Return the complete modified JSON."
            ),
            "summary": {
                "total_new_matches": new_count,
                "already_in_zotero": existing_count,
                "pending_review": new_count,
            },
            "matches": matches,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Renderizado HTML
    # ─────────────────────────────────────────────────────────────────────────

    def _render_html(
        self,
        stats: ReportStats,
        reports: list[ItemReport],
        graph_data: dict,
        json_path: str,
    ) -> str:
        env = Environment(loader=BaseLoader())
        env.filters["status_meta"] = lambda s: STATUS_META.get(s, STATUS_META["error"])
        env.filters["method_label"] = lambda m: METHOD_LABELS.get(m, m)
        env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)

        tmpl = env.from_string(HTML_TEMPLATE)
        return tmpl.render(
            stats=stats,
            reports=reports,
            graph_json=json.dumps(graph_data, ensure_ascii=False),
            json_path=json_path,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            status_meta=STATUS_META,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Plantilla HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zotero Reference Automator — Informe</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #0a0f1e; color: #e2e8f0; line-height: 1.5; }

  /* ── Header ── */
  header { background: #111827; padding: 1.25rem 2rem; border-bottom: 1px solid #1f2937;
           display: flex; align-items: center; gap: 1rem; position: sticky; top: 0; z-index: 100; }
  header h1 { font-size: 1.1rem; font-weight: 700; color: #f8fafc; }
  header .sub { color: #64748b; font-size: 0.78rem; margin-left: auto; white-space: nowrap; }

  /* ── Layout ── */
  .container { max-width: 1500px; margin: 0 auto; padding: 1.5rem 2rem; }
  section { margin-bottom: 2.5rem; }
  h2 { font-size: 0.95rem; font-weight: 600; color: #94a3b8; text-transform: uppercase;
       letter-spacing: .06em; margin-bottom: 1rem; padding-bottom: 0.5rem;
       border-bottom: 1px solid #1f2937; }

  /* ── Stats grid ── */
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap: 0.75rem; margin-bottom: 2rem; }
  .stat-card { background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 1rem 0.75rem; text-align: center; }
  .stat-card .num { font-size: 1.8rem; font-weight: 800; color: #38bdf8; line-height: 1.1; }
  .stat-card .lbl { font-size: 0.7rem; color: #64748b; margin-top: 0.3rem; text-transform: uppercase; letter-spacing: .04em; }

  /* ── Graph ── */
  #graph-container { background: #111827; border: 1px solid #1f2937; border-radius: 10px;
                     height: 560px; position: relative; overflow: hidden; }
  #graph-svg { width: 100%; height: 100%; }
  .node circle { cursor: pointer; transition: r 0.15s; }
  .node circle:hover { r: 12; }
  .node text { font-size: 9px; fill: #94a3b8; pointer-events: none; }
  .link { stroke-opacity: 0.45; }
  .link.existing { stroke: #334155; }
  .link.new-rel   { stroke: #38bdf8; stroke-opacity: 0.75; }
  .graph-legend { position: absolute; bottom: 12px; right: 14px; display: flex; gap: 12px;
                  font-size: 0.7rem; color: #64748b; }
  .graph-legend span::before { content: ""; display: inline-block; width: 18px; height: 2px;
                                 vertical-align: middle; margin-right: 4px; }
  .graph-legend .lg-new::before  { background: #38bdf8; }
  .graph-legend .lg-exist::before { background: #334155; }
  #graph-tooltip { position: absolute; background: #1e293b; border: 1px solid #334155;
                   border-radius: 8px; padding: 0.65rem 0.85rem; font-size: 0.72rem;
                   max-width: 280px; pointer-events: none; display: none; z-index: 10;
                   box-shadow: 0 4px 20px #0008; }
  #graph-tooltip strong { color: #f1f5f9; display: block; margin-bottom: 0.25rem; }
  #graph-tooltip .tt-meta { color: #64748b; }

  /* ── Table ── */
  .table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #1f2937; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  thead { position: sticky; top: 52px; z-index: 10; }
  th { background: #111827; color: #64748b; padding: 0.65rem 1rem; text-align: left;
       font-weight: 600; text-transform: uppercase; font-size: 0.68rem; letter-spacing: .05em;
       border-bottom: 1px solid #1f2937; white-space: nowrap; }
  td { padding: 0.6rem 1rem; border-bottom: 1px solid #111827; vertical-align: top; background: #0d1526; }
  tr:hover td { background: #111827; }

  /* ── Source article cell ── */
  .art-title { font-weight: 600; color: #e2e8f0; font-size: 0.82rem; line-height: 1.35;
               max-width: 360px; word-break: break-word; }
  .art-meta  { color: #475569; font-size: 0.68rem; margin-top: 0.2rem; }
  .art-meta a { color: #38bdf8; text-decoration: none; }
  .art-meta a:hover { text-decoration: underline; }

  /* ── Status badge ── */
  .badge { display: inline-flex; align-items: center; gap: 0.25rem; padding: 0.18rem 0.6rem;
           border-radius: 999px; font-size: 0.68rem; font-weight: 700; }
  .diag-detail { color: #475569; font-size: 0.68rem; margin-top: 0.3rem; line-height: 1.3; }
  .ext-badge { color: #a78bfa; font-size: 0.65rem; margin-top: 0.2rem; }

  /* ── Candidates ── */
  .cand-summary { cursor: pointer; color: #38bdf8; font-size: 0.75rem; font-weight: 600;
                  user-select: none; list-style: none; }
  .cand-summary::-webkit-details-marker { display: none; }
  .cand-summary::before { content: "▶ "; font-size: 0.6rem; }
  details[open] .cand-summary::before { content: "▼ "; }
  .cand-list { margin-top: 0.5rem; display: flex; flex-direction: column; gap: 0.5rem; }

  .cand-card { background: #0f1929; border: 1px solid #1e3050; border-radius: 7px;
               padding: 0.55rem 0.75rem; }
  .cand-card.exists { border-color: #1f2937; opacity: 0.6; }
  .cand-top { display: flex; align-items: flex-start; gap: 0.5rem; }
  .score-pill { flex-shrink: 0; font-size: 0.65rem; font-weight: 800; padding: 0.1rem 0.4rem;
                border-radius: 5px; }
  .score-pill.high { background: #052e16; color: #4ade80; }
  .score-pill.med  { background: #422006; color: #fb923c; }
  .score-pill.low  { background: #3b0764; color: #c084fc; }
  .cand-title { font-weight: 600; color: #cbd5e1; font-size: 0.77rem; line-height: 1.35; flex: 1; }
  .cand-title a { color: #38bdf8; text-decoration: none; }
  .cand-title a:hover { text-decoration: underline; }
  .cand-authors { color: #475569; font-size: 0.65rem; margin-top: 0.15rem; }
  .method-chip { display: inline-block; font-size: 0.6rem; color: #64748b;
                 border: 1px solid #1e293b; border-radius: 4px; padding: 0 0.3rem; margin-top: 0.2rem; }
  .exists-chip { font-size: 0.62rem; color: #475569; border: 1px solid #1f2937;
                 border-radius: 4px; padding: 0 0.3rem; display: inline-block; margin-left: 0.25rem; }
  .cand-rawtext { color: #334155; font-size: 0.62rem; margin-top: 0.3rem;
                  font-style: italic; line-height: 1.3; border-left: 2px solid #1e293b; padding-left: 0.4rem; }

  /* ── Filter bar ── */
  .filter-bar { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; align-items: center; }
  .filter-bar input { background: #111827; border: 1px solid #1f2937; border-radius: 7px;
                      color: #e2e8f0; padding: 0.4rem 0.75rem; font-size: 0.8rem; outline: none; flex: 1; min-width: 200px; }
  .filter-bar input:focus { border-color: #38bdf8; }
  .filter-bar select { background: #111827; border: 1px solid #1f2937; border-radius: 7px;
                       color: #e2e8f0; padding: 0.4rem 0.6rem; font-size: 0.78rem; outline: none; cursor: pointer; }
  .filter-bar select:focus { border-color: #38bdf8; }
  #count-badge { color: #64748b; font-size: 0.75rem; white-space: nowrap; }
</style>
</head>
<body>
<header>
  <span style="font-size:1.3rem">🔗</span>
  <h1>Zotero Reference Automator</h1>
  <div class="sub">Generado {{ generated_at }} · {{ stats.total_items_in_library }} items en biblioteca</div>
</header>

<div class="container">

  <!-- Estadísticas -->
  <section>
    <div class="stats-grid">
      <div class="stat-card"><div class="num">{{ stats.total_items_in_library }}</div><div class="lbl">Items totales</div></div>
      <div class="stat-card"><div class="num">{{ stats.items_processed }}</div><div class="lbl">Procesados</div></div>
      <div class="stat-card"><div class="num">{{ stats.items_with_pdf }}</div><div class="lbl">Con PDF</div></div>
      <div class="stat-card"><div class="num">{{ stats.total_refs_extracted }}</div><div class="lbl">Refs extraídas</div></div>
      <div class="stat-card"><div class="num">{{ stats.refs_matched }}</div><div class="lbl">Con match</div></div>
      <div class="stat-card"><div class="num" style="color:#4ade80">{{ stats.new_relations }}</div><div class="lbl">Relaciones nuevas</div></div>
      <div class="stat-card"><div class="num" style="color:#94a3b8">{{ stats.existing_relations }}</div><div class="lbl">Ya existían</div></div>
      <div class="stat-card"><div class="num">{{ "%.1f"|format(stats.processing_time_s) }}s</div><div class="lbl">Tiempo</div></div>
    </div>
  </section>

  <!-- Grafo D3 -->
  <section>
    <h2>Grafo de relaciones</h2>
    <div id="graph-container">
      <svg id="graph-svg"></svg>
      <div id="graph-tooltip"></div>
      <div class="graph-legend">
        <span class="lg-new">Nuevas</span>
        <span class="lg-exist">Existentes</span>
      </div>
    </div>
  </section>

  <!-- Tabla de resultados -->
  <section>
    <h2>Detalle por artículo</h2>

    <div class="filter-bar">
      <input type="text" id="search-input" placeholder="Filtrar por título, DOI, autor…" oninput="filterTable()">
      <select id="status-filter" onchange="filterTable()">
        <option value="">Todos los estados</option>
        <option value="ok">OK (PDF extraído)</option>
        <option value="no_attachment">Sin PDF</option>
        <option value="ocr_needed">Necesita OCR</option>
        <option value="few_refs">Pocas refs</option>
        <option value="error">Error</option>
      </select>
      <select id="cand-filter" onchange="filterTable()">
        <option value="">Todas las filas</option>
        <option value="has_new">Con relaciones nuevas</option>
        <option value="has_any">Con candidatos</option>
        <option value="none">Sin candidatos</option>
      </select>
      <span id="count-badge"></span>
    </div>

    <div class="table-wrap">
    <table id="main-table">
      <thead>
        <tr>
          <th style="width:35%">Artículo fuente</th>
          <th style="width:14%">Estado PDF</th>
          <th style="width:6%" title="Referencias extraídas del PDF / API externa">#&nbsp;Refs</th>
          <th>Relaciones candidatas en tu biblioteca</th>
        </tr>
      </thead>
      <tbody>
      {% for rep in reports %}
      {% set diag = rep.diagnostic %}
      {% set meta = diag.status | status_meta if diag else status_meta["error"] %}
      {% set new_count = rep.candidates | selectattr("already_exists","equalto",false) | list | length %}
        <tr data-status="{{ diag.status if diag else 'error' }}"
            data-has-new="{{ 'yes' if new_count > 0 else 'no' }}"
            data-has-any="{{ 'yes' if rep.candidates else 'no' }}"
            data-search="{{ rep.item.title | lower }} {{ (rep.item.doi or '') | lower }} {{ rep.item.authors | join(' ') | lower }}">
          <td>
            <div class="art-title" title="{{ rep.item.title }}">{{ rep.item.title }}</div>
            <div class="art-meta">
              {% if rep.item.authors %}{{ rep.item.authors[:2] | join('; ') }}{% if rep.item.authors|length > 2 %} et al.{% endif %}{% endif %}
              {% if rep.item.year %} · {{ rep.item.year }}{% endif %}
              {% if rep.item.doi %}
               · <a href="https://doi.org/{{ rep.item.doi }}" target="_blank" title="{{ rep.item.doi }}">DOI ↗</a>
              {% endif %}
              <span style="color:#1e3050"> · {{ rep.item.key }}</span>
            </div>
          </td>
          <td>
            <span class="badge" style="background:{{ meta.color }}18;color:{{ meta.color }};border:1px solid {{ meta.color }}40">
              {{ meta.icon }}&nbsp;{{ meta.label }}
            </span>
            {% if diag and diag.detail %}
            <div class="diag-detail">{{ diag.detail[:100] }}</div>
            {% endif %}
            {% if rep.external_refs_used %}
            <div class="ext-badge">↳ vía {{ diag.external_source or 'API externa' }}</div>
            {% endif %}
          </td>
          <td style="text-align:center;color:{% if rep.refs_extracted > 0 %}#e2e8f0{% else %}#334155{% endif %}">
            {{ rep.refs_extracted if rep.refs_extracted > 0 else '—' }}
          </td>
          <td>
            {% if rep.candidates %}
            {% set new_cands = rep.candidates | selectattr("already_exists","equalto",false) | list %}
            {% set old_cands = rep.candidates | selectattr("already_exists","equalto",true)  | list %}
            <details {% if new_count > 0 %}open{% endif %}>
              <summary class="cand-summary">
                {% if new_count > 0 %}
                  <span style="color:#4ade80">{{ new_count }} nueva{{ 's' if new_count > 1 else '' }}</span>
                  {% if old_cands %}<span style="color:#475569"> · {{ old_cands|length }} ya exist{{ 'ían' if old_cands|length > 1 else 'ía' }}</span>{% endif %}
                {% else %}
                  <span style="color:#475569">{{ rep.candidates|length }} ya exist{{ 'ían' if rep.candidates|length > 1 else 'ía' }}</span>
                {% endif %}
              </summary>
              <div class="cand-list">
              {% for c in (new_cands + old_cands)[:15] %}
                {% set sc = c.confidence %}
                <div class="cand-card {% if c.already_exists %}exists{% endif %}">
                  <div class="cand-top">
                    <span class="score-pill {% if sc==100 %}high{% elif sc>=80 %}med{% else %}low{% endif %}">{{ sc }}%</span>
                    <div style="flex:1">
                      <div class="cand-title">
                        {% if c.target_doi %}
                          <a href="https://doi.org/{{ c.target_doi }}" target="_blank">{{ c.target_title or c.target_key }}</a>
                        {% else %}
                          {{ c.target_title or c.target_key }}
                        {% endif %}
                        {% if c.already_exists %}<span class="exists-chip">ya existe</span>{% endif %}
                      </div>
                      <div class="cand-authors">
                        {{ c.target_authors[:2] | join('; ') }}{% if c.target_authors|length > 2 %} et al.{% endif %}
                        {% if c.target_year %} · {{ c.target_year }}{% endif %}
                        {% if c.target_doi %} · <span style="color:#334155;font-size:0.6rem">{{ c.target_doi[:40] }}</span>{% endif %}
                      </div>
                      <span class="method-chip">{{ c.match_method | method_label }}</span>
                    </div>
                  </div>
                  <div class="cand-rawtext">{{ c.reference_text[:120] }}</div>
                </div>
              {% endfor %}
              {% if rep.candidates|length > 15 %}
              <div style="color:#475569;font-size:0.68rem;padding:0.25rem 0">
                … y {{ rep.candidates|length - 15 }} más
              </div>
              {% endif %}
              </div>
            </details>
            {% else %}
            <span style="color:#1e293b">—</span>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </section>

</div><!-- /container -->

<script>
// ── Filtro de tabla ──────────────────────────────────────────────────────────
function filterTable() {
  const q      = document.getElementById('search-input').value.toLowerCase();
  const status = document.getElementById('status-filter').value;
  const cand   = document.getElementById('cand-filter').value;
  const rows   = document.querySelectorAll('#main-table tbody tr');
  let visible  = 0;

  rows.forEach(row => {
    const matchSearch = !q || (row.dataset.search || '').includes(q);
    const matchStatus = !status || row.dataset.status === status;
    const matchCand   = !cand
      || (cand === 'has_new'  && row.dataset.hasNew  === 'yes')
      || (cand === 'has_any'  && row.dataset.hasAny  === 'yes')
      || (cand === 'none'     && row.dataset.hasAny  === 'no');

    const show = matchSearch && matchStatus && matchCand;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('count-badge').textContent = `${visible} / ${rows.length} artículos`;
}
document.addEventListener('DOMContentLoaded', () => filterTable());

// ── D3 Force Graph ───────────────────────────────────────────────────────────
const GRAPH = {{ graph_json }};

(function() {
  const svg = d3.select("#graph-svg");
  const container = document.getElementById("graph-container");
  const W = container.clientWidth;
  const H = container.clientHeight;
  const tooltip = document.getElementById("graph-tooltip");

  const links = GRAPH.links;
  const nodes = Object.values(GRAPH.nodes);

  const connectedIds = new Set();
  links.forEach(l => { connectedIds.add(l.source); connectedIds.add(l.target); });
  const visibleNodes = nodes.filter(n => connectedIds.has(n.id));

  if (visibleNodes.length === 0) {
    svg.append("text").attr("x", W/2).attr("y", H/2)
       .attr("text-anchor","middle").attr("fill","#334155").attr("font-size","14")
       .text("No hay relaciones para visualizar en este lote.");
    return;
  }

  const nodeById = {};
  visibleNodes.forEach(n => nodeById[n.id] = n);

  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.08, 6]).on("zoom", e => g.attr("transform", e.transform)));

  const sim = d3.forceSimulation(visibleNodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(90).strength(0.4))
    .force("charge", d3.forceManyBody().strength(-200))
    .force("center", d3.forceCenter(W/2, H/2))
    .force("collide", d3.forceCollide(18));

  const link = g.append("g")
    .selectAll("line").data(links).join("line")
    .attr("class", d => "link " + (d.already_exists ? "existing" : "new-rel"))
    .attr("stroke-width", d => d.already_exists ? 1 : 1.8);

  // Color nodes by connection count
  const maxDeg = Math.max(...visibleNodes.map(n => Object.keys(n.links||{}).length), 1);
  const colorScale = d3.scaleSequential(d3.interpolateCool).domain([0, maxDeg]);

  const node = g.append("g")
    .selectAll("g").data(visibleNodes).join("g").attr("class","node")
    .call(d3.drag()
      .on("start", (e,d) => { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on("drag",  (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on("end",   (e,d) => { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));

  node.append("circle")
    .attr("r", d => 5 + Math.min(Object.keys(d.links||{}).length * 1.5, 12))
    .attr("fill", d => colorScale(Object.keys(d.links||{}).length))
    .attr("stroke", "#0a0f1e").attr("stroke-width", 1.5)
    .on("mousemove", (e, d) => {
      const auths = (d.authors||[]).slice(0,2).join('; ') + (d.authors?.length > 2 ? ' et al.' : '');
      tooltip.style.display = "block";
      tooltip.innerHTML = `<strong>${(d.title||d.id).substring(0,80)}</strong>
        <div class="tt-meta">${auths}${d.year ? ' · '+d.year : ''}${d.doi ? '<br><span style="color:#38bdf8">'+d.doi+'</span>' : ''}</div>`;
      const rect = container.getBoundingClientRect();
      let left = e.clientX - rect.left + 12;
      let top  = e.clientY - rect.top  + 12;
      if (left + 290 > W) left = left - 290 - 24;
      tooltip.style.left = left + "px";
      tooltip.style.top  = top  + "px";
    })
    .on("mouseleave", () => { tooltip.style.display="none"; });

  node.append("text")
    .attr("dy", "0.32em")
    .attr("x", d => 7 + Math.min(Object.keys(d.links||{}).length * 1.5, 12))
    .text(d => (d.title||d.id).substring(0, 28));

  sim.on("tick", () => {
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("transform", d => `translate(${d.x},${d.y})`);
  });
})();
</script>
</body>
</html>
"""

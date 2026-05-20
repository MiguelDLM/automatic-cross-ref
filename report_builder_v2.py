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
    ) -> tuple[str, str]:
        """
        Genera informe HTML + JSON de relaciones.
        Devuelve (html_path, json_path).
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = str(out / f"report_{now}.html")
        json_path = str(out / f"relations_{now}.json")

        graph_data = self._build_graph(reports)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)

        html = self._render_html(stats, reports, graph_data, json_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        return html_path, json_path

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
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }
  header { background: #1e293b; padding: 1.5rem 2rem; border-bottom: 1px solid #334155; }
  header h1 { font-size: 1.4rem; color: #f1f5f9; }
  header p  { color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }
  .container { max-width: 1400px; margin: 0 auto; padding: 1.5rem 2rem; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: 1rem; margin-bottom: 2rem; }
  .stat-card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 1rem; text-align: center; }
  .stat-card .num { font-size: 2rem; font-weight: 700; color: #38bdf8; }
  .stat-card .lbl { font-size: 0.75rem; color: #94a3b8; margin-top: 0.25rem; }
  section { margin-bottom: 2.5rem; }
  h2 { font-size: 1.1rem; color: #f1f5f9; margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #334155; }
  #graph-container { background: #1e293b; border: 1px solid #334155; border-radius: 8px; height: 600px; position: relative; }
  #graph-svg { width: 100%; height: 100%; }
  .node circle { cursor: pointer; }
  .node text { font-size: 10px; fill: #cbd5e1; pointer-events: none; }
  .link { stroke: #475569; stroke-opacity: 0.5; }
  .link.new { stroke: #38bdf8; stroke-opacity: 0.8; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th { background: #1e293b; color: #94a3b8; padding: 0.6rem 0.8rem; text-align: left; position: sticky; top: 0; }
  td { padding: 0.5rem 0.8rem; border-bottom: 1px solid #1e293b; vertical-align: top; }
  tr:nth-child(even) td { background: #0f172a08; }
  tr:hover td { background: #1e293b50; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.7rem; font-weight: 600; }
  .truncate { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .candidate-list { list-style: none; padding: 0; }
  .candidate-list li { margin-bottom: 0.25rem; font-size: 0.75rem; color: #94a3b8; }
  .score { font-weight: 700; }
  .score.high { color: #22c55e; }
  .score.med  { color: #eab308; }
  .score.low  { color: #ef4444; }
  details summary { cursor: pointer; color: #38bdf8; font-size: 0.8rem; }
  .tooltip { position: absolute; background: #1e293b; border: 1px solid #475569; border-radius: 6px;
             padding: 0.75rem; font-size: 0.75rem; max-width: 300px; pointer-events: none;
             display: none; z-index: 10; }
</style>
</head>
<body>
<header>
  <h1>🔗 Zotero Reference Automator — Informe de Relaciones</h1>
  <p>Generado el {{ generated_at }} · {{ stats.total_items_in_library }} items en la biblioteca</p>
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
      <div class="stat-card"><div class="num">{{ stats.new_relations }}</div><div class="lbl">Relaciones nuevas</div></div>
      <div class="stat-card"><div class="num">{{ stats.existing_relations }}</div><div class="lbl">Ya existían</div></div>
      <div class="stat-card"><div class="num">{{ "%.1f"|format(stats.processing_time_s) }}s</div><div class="lbl">Tiempo total</div></div>
    </div>
  </section>

  <!-- Grafo D3 -->
  <section>
    <h2>Grafo de relaciones</h2>
    <div id="graph-container">
      <svg id="graph-svg"></svg>
      <div class="tooltip" id="tooltip"></div>
    </div>
  </section>

  <!-- Tabla de resultados -->
  <section>
    <h2>Detalle por artículo</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Artículo</th>
          <th>Estado PDF</th>
          <th>Refs extraídas</th>
          <th>Relaciones candidatas</th>
        </tr>
      </thead>
      <tbody>
      {% for rep in reports %}
      {% set diag = rep.diagnostic %}
      {% set meta = diag.status | status_meta if diag else status_meta["error"] %}
        <tr>
          <td>
            <div class="truncate" title="{{ rep.item.title }}">{{ rep.item.title }}</div>
            <div style="color:#64748b;font-size:0.7rem">{{ rep.item.key }}{% if rep.item.doi %} · DOI: {{ rep.item.doi }}{% endif %}</div>
          </td>
          <td>
            <span class="badge" style="background:{{ meta.color }}20;color:{{ meta.color }}">
              {{ meta.icon }} {{ meta.label }}
            </span>
            {% if diag and diag.detail %}
            <div style="color:#64748b;font-size:0.7rem;margin-top:0.2rem">{{ diag.detail[:80] }}</div>
            {% endif %}
            {% if rep.external_refs_used %}<div style="color:#a78bfa;font-size:0.7rem">↳ vía API externa</div>{% endif %}
          </td>
          <td style="text-align:center">{{ rep.refs_extracted }}</td>
          <td>
            {% if rep.candidates %}
            <details>
              <summary>{{ rep.candidates|length }} candidato(s)</summary>
              <ul class="candidate-list">
              {% for c in rep.candidates[:10] %}
                <li>
                  {% set sc = c.confidence %}
                  <span class="score {% if sc==100 %}high{% elif sc>=80 %}med{% else %}low{% endif %}">{{ sc }}%</span>
                  [{{ c.match_method | method_label }}]
                  {{ c.target_key }}
                  {% if c.already_exists %}<span style="color:#64748b">(ya existe)</span>{% endif %}
                  <br><span style="color:#475569">{{ c.reference_text[:80] }}</span>
                </li>
              {% endfor %}
              </ul>
            </details>
            {% else %}
            <span style="color:#475569">—</span>
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
const GRAPH = {{ graph_json }};

// ── D3 Force Graph ───────────────────────────────────────────────────────────
(function() {
  const svg = d3.select("#graph-svg");
  const container = document.getElementById("graph-container");
  const W = container.clientWidth;
  const H = container.clientHeight;
  const tooltip = document.getElementById("tooltip");

  const nodesMap = GRAPH.nodes;
  const links = GRAPH.links;

  const nodes = Object.values(nodesMap);
  const nodeById = {};
  nodes.forEach(n => nodeById[n.id] = n);

  // Solo mostrar nodos con al menos una conexión
  const connectedIds = new Set();
  links.forEach(l => { connectedIds.add(l.source); connectedIds.add(l.target); });
  const visibleNodes = nodes.filter(n => connectedIds.has(n.id));

  if (visibleNodes.length === 0) {
    svg.append("text").attr("x", W/2).attr("y", H/2)
       .attr("text-anchor","middle").attr("fill","#64748b")
       .text("No hay relaciones para visualizar.");
    return;
  }

  const g = svg.append("g");

  // Zoom
  svg.call(d3.zoom().scaleExtent([0.1, 4]).on("zoom", e => g.attr("transform", e.transform)));

  // Simulación
  const sim = d3.forceSimulation(visibleNodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(80).strength(0.5))
    .force("charge", d3.forceManyBody().strength(-150))
    .force("center", d3.forceCenter(W/2, H/2))
    .force("collide", d3.forceCollide(20));

  const link = g.append("g")
    .selectAll("line")
    .data(links)
    .join("line")
    .attr("class", d => "link" + (d.already_exists ? "" : " new"))
    .attr("stroke-width", d => d.already_exists ? 1 : 1.5);

  const node = g.append("g")
    .selectAll("g")
    .data(visibleNodes)
    .join("g")
    .attr("class","node")
    .call(d3.drag()
      .on("start", (e,d) => { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on("drag",  (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on("end",   (e,d) => { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));

  node.append("circle")
    .attr("r", d => 6 + Math.min(Object.keys(d.links||{}).length, 10))
    .attr("fill", "#38bdf8")
    .attr("stroke", "#0ea5e9")
    .attr("stroke-width", 1.5)
    .on("mouseover", (e, d) => {
      tooltip.style.display = "block";
      tooltip.innerHTML = `<b>${d.title||d.id}</b><br>Key: ${d.id}${d.doi?"<br>DOI: "+d.doi:""}${d.year?"<br>Año: "+d.year:""}`;
      tooltip.style.left = (e.offsetX+12)+"px";
      tooltip.style.top  = (e.offsetY+12)+"px";
    })
    .on("mouseout", () => { tooltip.style.display="none"; });

  node.append("text")
    .attr("dy", "0.31em")
    .attr("x", d => 8 + Math.min(Object.keys(d.links||{}).length, 10))
    .text(d => (d.title||d.id).substring(0,30));

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

"""
main.py
CLI principal del automatizador de referencias Zotero.
Modo solo lectura por defecto — genera informe HTML + JSON.
Comando 'apply-links' para aplicar las relaciones al Zotero local.
"""
from __future__ import annotations

import os
import sys
import time
import re
import subprocess
import unicodedata
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLine

from zotero_client import ZoteroClient
from extractor import PDFExtractor, ParsedReference
from matcher import ReferenceMatcher, LibraryItem
from reference_sources import ReferenceSourceClient, ExternalReference
from report_builder_v2 import ReportBuilder, ReportStats, ItemReport, PDFDiagnostic

load_dotenv()

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Enumeración de estados de PDF
# ─────────────────────────────────────────────────────────────────────────────

class PDFStatus(Enum):
    OK            = "ok"
    OCR_NEEDED    = "ocr_needed"
    FEW_REFS      = "few_refs"
    NO_ATTACHMENT = "no_attachment"
    NOT_FOUND     = "not_found"
    LINKED_FILE   = "linked_file"
    ERROR         = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config.yaml") -> dict:
    """Carga la configuración desde YAML y aplica defaults."""
    defaults = {
        "zotero": {
            "api_url": "http://localhost:23119/api/users/0",
            "api_key": "",
            "page_size": 100,
        },
        "extractor": {
            "max_pages_from_end": 10,
            "min_ref_length": 20,
            "max_ref_length": 1000,
            "min_text_chars": 200,
            "section_headers": [
                "References", "Bibliography", "参考文献", "REFERENCES",
                "BIBLIOGRAPHY", "Literature cited", "Works cited",
                "Referencias", "LITERATURA CITADA", "Bibliografía", "BIBLIOGRAFÍA",
            ],
        },
        "matcher": {
            "fuzzy_threshold": 82,
            "doi_is_exact": True,
            "max_candidates": 500,
            "use_crossref": True,
            "crossref_email": "",
        },
        "apis": {
            "semantic_scholar_key": "",
            "openalex_email": "",
            "ncbi_api_key": "",
        },
        "report": {
            "output_dir": "output",
            "min_confidence": 70,
        },
    }

    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for section, values in user_cfg.items():
            if section in defaults and isinstance(values, dict):
                defaults[section].update(values)
            else:
                defaults[section] = values

    return defaults


# ─────────────────────────────────────────────────────────────────────────────
# Diagnóstico de PDF
# ─────────────────────────────────────────────────────────────────────────────

def diagnose_pdf_availability(
    client: ZoteroClient, item_key: str
) -> tuple[Optional[str], PDFStatus, str]:
    """Devuelve (pdf_path, status, detail_message)."""
    try:
        attachments = client.get_item_attachments(item_key)
        if not attachments:
            return None, PDFStatus.NO_ATTACHMENT, "No hay adjuntos PDF en este item"

        for att in attachments:
            link_mode = att.get("data", {}).get("linkMode", "")
            if link_mode == "linked_file":
                path = att.get("data", {}).get("path", "")
                if path and os.path.exists(path):
                    return path, PDFStatus.OK, f"Archivo vinculado: {path}"
                return None, PDFStatus.LINKED_FILE, f"Archivo vinculado no encontrado: {path}"

            pdf_path = client.get_storage_path(att)
            if pdf_path:
                if os.path.exists(pdf_path):
                    return pdf_path, PDFStatus.OK, f"PDF encontrado: {pdf_path}"
                return None, PDFStatus.NOT_FOUND, f"Ruta en BD pero no en disco: {pdf_path}"

        return None, PDFStatus.NOT_FOUND, "No se pudo determinar la ruta del PDF"

    except Exception as e:
        return None, PDFStatus.ERROR, str(e)[:120]


def check_pdf_has_text(pdf_path: str, min_chars: int = 200) -> bool:
    """Comprueba si el PDF tiene capa de texto suficiente."""
    try:
        total = 0
        for i, page in enumerate(extract_pages(pdf_path)):
            if i > 3:
                break
            for elem in page:
                if isinstance(elem, LTTextContainer):
                    for line in elem:
                        if isinstance(line, LTTextLine):
                            total += len(line.get_text().strip())
            if total >= min_chars:
                return True
        return total >= min_chars
    except Exception:
        return False


def find_pdf_path(client: ZoteroClient, item_key: str) -> Optional[str]:
    path, status, _ = diagnose_pdf_availability(client, item_key)
    return path if status == PDFStatus.OK else None


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ─────────────────────────────────────────────────────────────────────────────

def _guess_source(refs: list) -> str:
    if not refs:
        return "unknown"
    return refs[0].source


def run_pipeline(
    cfg: dict,
    limit: Optional[int] = None,
    only_100: bool = False,
    use_external: bool = True,
    auto_ocr: bool = False,
    replace_pdf: bool = False,
):
    t_start = time.time()

    z_cfg   = cfg["zotero"]
    e_cfg   = cfg["extractor"]
    m_cfg   = cfg["matcher"]
    api_cfg = cfg.get("apis", {})
    r_cfg   = cfg.get("report", {})

    min_confidence = 100 if only_100 else r_cfg.get("min_confidence", 70)

    # ── 1. Conectar a Zotero ──────────────────────────────────────────────────
    console.rule("[bold]1 · Conectando a Zotero[/bold]")
    client = ZoteroClient(
        api_url   = z_cfg["api_url"],
        api_key   = z_cfg.get("api_key", ""),
        page_size = z_cfg.get("page_size", 100),
    )
    if not client.ping():
        console.print(Panel(
            "No se puede conectar con Zotero.\n\n"
            "  1. Abre Zotero\n"
            "  2. Edit → Preferences → Advanced → [bold]Allow other applications…[/bold]",
            title="⚠ Zotero no disponible", border_style="red"))
        sys.exit(1)
    console.print("[green]✓ Zotero conectado[/green]")

    # ── 2. Cargar biblioteca ──────────────────────────────────────────────────
    console.rule("[bold]2 · Cargando biblioteca[/bold]")
    with console.status("Descargando items…"):
        raw_items = client.get_all_items()
    console.print(f"[green]✓ {len(raw_items)} items en la biblioteca[/green]")

    # ── 3. Inicializar matcher ────────────────────────────────────────────────
    matcher = ReferenceMatcher(
        fuzzy_threshold = m_cfg.get("fuzzy_threshold", 82),
        use_crossref    = m_cfg.get("use_crossref", True),
        crossref_email  = m_cfg.get("crossref_email", os.getenv("CROSSREF_EMAIL", "")),
    )
    n_indexed = matcher.load_library(raw_items)
    console.print(f"[green]✓ {n_indexed} items indexados[/green]")

    with console.status("Cargando relaciones existentes…"):
        existing: dict[str, list[str]] = {}
        for raw in raw_items:
            key = raw.get("key", "")
            if key:
                try:
                    existing[key] = client.get_item_relations(key)
                except Exception:
                    existing[key] = []
    matcher.set_existing_relations(existing)

    # ── 4. Fuentes externas ───────────────────────────────────────────────────
    ext_client: Optional[ReferenceSourceClient] = None
    if use_external:
        ext_client = ReferenceSourceClient(
            semantic_scholar_key = api_cfg.get("semantic_scholar_key", os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")),
            crossref_email       = m_cfg.get("crossref_email", os.getenv("CROSSREF_EMAIL", "")),
            openalex_email       = api_cfg.get("openalex_email", os.getenv("OPENALEX_EMAIL", "")),
            ncbi_api_key         = api_cfg.get("ncbi_api_key", os.getenv("NCBI_API_KEY", "")),
            core_api_key         = api_cfg.get("core_api_key", os.getenv("CORE_API_KEY", "")),
        )
        sources = "Semantic Scholar · OpenAlex · CrossRef · CORE · PubMed"
        console.print(f"[green]✓ Fuentes externas habilitadas ({sources})[/green]")

    # ── 5. Extractor PDF ──────────────────────────────────────────────────────
    extractor = PDFExtractor(
        max_pages_from_end = e_cfg.get("max_pages_from_end", 10),
        min_ref_length     = e_cfg.get("min_ref_length", 20),
        max_ref_length     = e_cfg.get("max_ref_length", 1000),
        section_headers    = e_cfg.get("section_headers"),
    )

    # ── 6. Procesar items ─────────────────────────────────────────────────────
    console.rule("[bold]3 · Procesando items[/bold]")

    stats = ReportStats(total_items_in_library=len(raw_items))
    item_reports: list[ItemReport] = []
    diagnostic_counts: dict[str, int] = {}

    lib_items: list[LibraryItem] = matcher._library
    if limit:
        lib_items = lib_items[:limit]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Procesando…", total=len(lib_items))

        for lib_item in lib_items:
            progress.update(task, advance=1, description=f"[dim]{lib_item.title[:50]}[/dim]")

            pdf_path, pdf_status, pdf_detail = diagnose_pdf_availability(client, lib_item.key)
            diagnostic_counts[pdf_status.value] = diagnostic_counts.get(pdf_status.value, 0) + 1

            diag = PDFDiagnostic(
                status = pdf_status.value,
                detail = pdf_detail,
                path   = pdf_path,
            )

            refs: list[ParsedReference] = []
            candidates: list = []
            error: Optional[str] = None

            if pdf_status == PDFStatus.OK and pdf_path:
                stats.items_with_pdf += 1

                if not check_pdf_has_text(pdf_path, e_cfg.get("min_text_chars", 200)):
                    if auto_ocr:
                        console.print(f"\n[dim]Ejecutando OCR en {Path(pdf_path).name}...[/dim]")
                        ocr_dir = Path("output/ocr")
                        ocr_dir.mkdir(parents=True, exist_ok=True)
                        ocr_pdf_path = ocr_dir / f"{lib_item.key}.pdf"
                        try:
                            subprocess.run(
                                ["ocrmypdf", "--force-ocr", pdf_path, str(ocr_pdf_path)],
                                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
                            if replace_pdf:
                                import shutil
                                shutil.copy2(ocr_pdf_path, pdf_path)
                                console.print(f"[dim]PDF original reemplazado.[/dim]")
                            else:
                                pdf_path = str(ocr_pdf_path)
                            diag.detail = "OCR automático completado"
                        except Exception as e:
                            pdf_status = PDFStatus.OCR_NEEDED
                            diag.status = PDFStatus.OCR_NEEDED.value
                            diag.detail = f"Fallo OCR automático: {e}"
                    else:
                        pdf_status = PDFStatus.OCR_NEEDED
                        diag.status = PDFStatus.OCR_NEEDED.value
                        diag.detail = "PDF sin capa de texto — necesita OCR"
                        diagnostic_counts[PDFStatus.OCR_NEEDED.value] = \
                            diagnostic_counts.get(PDFStatus.OCR_NEEDED.value, 0) + 1

                if pdf_status == PDFStatus.OK:
                    try:
                        refs = extractor.extract(pdf_path)
                        if not refs:
                            diag.status = PDFStatus.FEW_REFS.value
                            diag.detail = "Sección de referencias no detectada en el PDF"
                            diagnostic_counts[PDFStatus.OK.value] -= 1
                            diagnostic_counts[PDFStatus.FEW_REFS.value] = \
                                diagnostic_counts.get(PDFStatus.FEW_REFS.value, 0) + 1
                    except Exception as e:
                        error = str(e)[:120]
                        diag.status = PDFStatus.ERROR.value
                        diag.detail = error
                        diagnostic_counts[PDFStatus.OK.value] -= 1
                        diagnostic_counts[PDFStatus.ERROR.value] = \
                            diagnostic_counts.get(PDFStatus.ERROR.value, 0) + 1

            # ── Fuentes externas ───────────────────────────────────────────
            external_refs_used = False
            if use_external and ext_client and lib_item.doi:
                pdf_failed = diag.status in (
                    PDFStatus.OCR_NEEDED.value, PDFStatus.FEW_REFS.value,
                    PDFStatus.NO_ATTACHMENT.value, PDFStatus.NOT_FOUND.value,
                    PDFStatus.LINKED_FILE.value,
                )
                if pdf_failed or not refs:
                    ext_refs = ext_client.get_references_by_doi(lib_item.doi)
                    if ext_refs:
                        refs = [
                            ParsedReference(
                                raw_text  = r.title or "",
                                doi       = r.doi,
                                arxiv_id  = r.arxiv_id,
                                title     = r.title,
                                authors   = r.authors,
                                year      = r.year,
                                ref_index = i,
                            )
                            for i, r in enumerate(ext_refs)
                        ]
                        external_refs_used = True
                        diag.external_source = _guess_source(ext_refs)
                        if pdf_failed:
                            diag.detail += f" → refs obtenidas via {diag.external_source}"

            stats.items_processed += 1
            stats.total_refs_extracted += len(refs)

            # ── Matching ───────────────────────────────────────────────────
            if refs:
                candidates = matcher.build_relation_candidates(
                    source_item    = lib_item,
                    references     = refs,
                    min_confidence = min_confidence,
                )
                stats.refs_matched       += sum(1 for c in candidates)
                stats.new_relations      += sum(1 for c in candidates if not c.already_exists)
                stats.existing_relations += sum(1 for c in candidates if c.already_exists)

            stats.refs_unmatched = stats.total_refs_extracted - stats.refs_matched

            item_reports.append(ItemReport(
                item               = lib_item,
                pdf_path           = pdf_path,
                refs_extracted     = len(refs),
                candidates         = candidates,
                error              = error,
                diagnostic         = diag,
                external_refs_used = external_refs_used,
            ))

    stats.processing_time_s = time.time() - t_start

    # ── 7. Resumen ─────────────────────────────────────────────────────────────
    console.rule("[bold]4 · Resumen de diagnóstico[/bold]")
    for status_val, count in sorted(diagnostic_counts.items(), key=lambda x: -x[1]):
        console.print(f"  {status_val}: [bold]{count}[/bold]")
    console.print(f"\n  Total refs extraídas:  [bold]{stats.total_refs_extracted}[/bold]")
    console.print(f"  Con match:             [bold cyan]{stats.refs_matched}[/bold cyan]")
    console.print(f"  Relaciones nuevas:     [bold green]{stats.new_relations}[/bold green]")
    console.print(f"  Ya existentes:         [bold dim]{stats.existing_relations}[/bold dim]")
    console.print(f"  Sin match:             [bold yellow]{stats.refs_unmatched}[/bold yellow]")

    # ── 8. Generar informe ────────────────────────────────────────────────────
    console.rule("[bold]5 · Generando informe[/bold]")
    builder = ReportBuilder()
    html_path, json_path, verification_path = builder.build(
        stats      = stats,
        reports    = item_reports,
        output_dir = r_cfg.get("output_dir", "output"),
    )
    console.print(f"[green]✓ Informe HTML:         {html_path}[/green]")
    console.print(f"[green]✓ JSON de relaciones:   {json_path}[/green]")
    console.print(f"[green]✓ JSON de verificación: {verification_path}[/green]")
    console.print(Panel(
        f"Revisa y aprueba los matches antes de aplicarlos:\n\n"
        f"  [bold]python main.py review {verification_path}[/bold]\n\n"
        "O aplica directamente (sin revisión):\n\n"
        f"  [bold]python main.py apply-links {json_path}[/bold]",
        border_style="dim"))

    if ext_client:
        ext_client.close()
    client.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Automatizador de referencias Zotero — modo solo lectura."""
    pass


@cli.command()
@click.option("--config",         default="config.yaml", show_default=True, help="Archivo de configuración")
@click.option("--limit",          default=None, type=int, help="Procesar solo N artículos (test)")
@click.option("--output-dir",     default=None, help="Directorio de salida")
@click.option("--only-100",       is_flag=True, default=False,
              help="Solo mostrar matches con 100%% de certeza (DOI/arXiv exacto)")
@click.option("--no-external",    is_flag=True, default=False,
              help="No consultar APIs externas (solo PDF local)")
@click.option("--min-confidence", default=None, type=int,
              help="Confianza mínima para incluir en informe (0-100, por defecto 70)")
@click.option("--ocr",            is_flag=True, default=False,
              help="Hacer OCR automático a PDFs sin texto (requiere ocrmypdf)")
@click.option("--replace-pdf",    is_flag=True, default=False,
              help="Si --ocr está activo, reemplazar el archivo original en Zotero por el PDF con OCR")
def run(config, limit, output_dir, only_100, no_external, min_confidence, ocr, replace_pdf):
    """
    Pipeline completo en modo SOLO LECTURA.

    Procesa TODOS los items de la biblioteca (no solo los que tienen PDF):
    para cada item diagnostica el estado del PDF y busca sus referencias
    tanto en el PDF local como en APIs externas (Semantic Scholar, OpenAlex, etc.)
    Genera un informe HTML completo con diagnóstico, estadísticas y relaciones candidatas.
    """
    cfg = load_config(config)
    if output_dir:
        cfg["report"]["output_dir"] = output_dir
    if min_confidence is not None:
        cfg["report"]["min_confidence"] = min_confidence

    mode_str = "100% exacto (DOI/arXiv)" if only_100 else f"≥{cfg['report'].get('min_confidence', 70)}% confianza"
    console.print(Panel(
        f"[bold]Modo: Solo lectura · Umbral de matching: {mode_str}[/bold]\n"
        "No se modificará ningún dato en Zotero.\n"
        f"APIs externas: {'[green]activadas[/green]' if not no_external else '[yellow]desactivadas[/yellow]'}\n"
        f"Auto OCR: {'[green]activado[/green]' if ocr else '[yellow]desactivado[/yellow]'}",
        title="🔒 Automatizador de Referencias Zotero v2",
        border_style="blue"))

    run_pipeline(
        cfg          = cfg,
        limit        = limit,
        only_100     = only_100,
        use_external = not no_external,
        auto_ocr     = ocr,
        replace_pdf  = replace_pdf,
    )


@cli.command("apply-links")
@click.argument("json_file", type=click.Path(exists=True))
@click.option("--config", default="config.yaml", help="Archivo de configuración")
def apply_links(json_file, config):
    """
    Aplica las relaciones sugeridas en Zotero a partir del JSON generado por 'run'.
    """
    import json as json_mod
    cfg = load_config(config)
    client = ZoteroClient(cfg["zotero"]["api_url"], cfg["zotero"].get("api_key", ""))

    if not client.ping():
        console.print("[red]✗ Zotero no disponible[/red]")
        sys.exit(1)

    with open(json_file, "r", encoding="utf-8") as f:
        graph = json_mod.load(f)

    links = graph.get("links", [])
    if not links:
        console.print("[yellow]No hay relaciones en el JSON.[/yellow]")
        return

    updates: dict[str, set[str]] = {}
    for edge in links:
        if edge.get("already_exists"):
            continue
        src = edge["source"]
        tgt = edge["target"]
        updates.setdefault(src, set()).add(tgt)

    if not updates:
        console.print("[green]Todas las relaciones del JSON ya existen en Zotero.[/green]")
        return

    console.print(f"Se añadirán relaciones a [cyan]{len(updates)}[/cyan] artículos fuente.")

    with Progress(console=console) as progress:
        task = progress.add_task("Aplicando relaciones…", total=len(updates))
        for src, targets in updates.items():
            success = client.add_relations(src, list(targets))
            if success:
                console.print(f"[dim]Añadidas {len(targets)} refs a {src}[/dim]")
            progress.advance(task)

    console.print("[bold green]¡Relaciones aplicadas en Zotero![/bold green]")
    client.close()


@cli.command("review")
@click.argument("verification_file", type=click.Path(exists=True))
@click.option("--config", default="config.yaml", help="Archivo de configuración")
@click.option("--apply/--no-apply", default=True, show_default=True,
              help="Aplicar automáticamente los matches aprobados al terminar la revisión")
def review(verification_file, config, apply):
    """
    Revisa interactivamente los matches candidatos y aplica los aprobados en Zotero.

    Lee el archivo verification_*.json generado por 'run', muestra cada match
    pendiente con toda la evidencia y pide confirmación antes de aplicar.
    Al terminar, guarda el JSON actualizado con las decisiones del usuario.
    """
    import json as json_mod

    cfg = load_config(config)

    with open(verification_file, "r", encoding="utf-8") as f:
        data = json_mod.load(f)

    matches = data.get("matches", [])
    pending = [m for m in matches if m.get("status") == "pending"]

    if not pending:
        console.print("[green]No hay matches pendientes de revisión.[/green]")
        return

    console.print(Panel(
        f"[bold]{len(pending)} matches pendientes[/bold] de revisión.\n\n"
        "  [bold green]a[/bold green] = aprobar   "
        "[bold red]r[/bold red] = rechazar   "
        "[bold yellow]s[/bold yellow] = saltar   "
        "[bold]q[/bold] = salir (y guardar)\n\n"
        "Los matches aprobados se aplicarán en Zotero al terminar.",
        title="Revisión interactiva de matches",
        border_style="blue",
    ))

    idx_map = {m["id"]: i for i, m in enumerate(matches)}
    approved: list[dict] = []
    reviewed = 0

    for match in pending:
        reviewed += 1
        src = match["source"]
        tgt = match["target"]
        ev  = match["evidence"]

        console.rule(f"[dim]{reviewed}/{len(pending)}[/dim]")

        # Fuente
        src_authors = "; ".join((src.get("authors") or [])[:3])
        console.print(
            f"[bold cyan]FUENTE:[/bold cyan] [bold]{src['title']}[/bold]\n"
            f"  [dim]{src_authors}{' · ' + src['year'] if src.get('year') else ''}"
            f"{' · DOI: ' + src['doi'] if src.get('doi') else ''}[/dim]"
        )

        # Destino
        tgt_authors = "; ".join((tgt.get("authors") or [])[:3])
        console.print(
            f"[bold magenta]DESTINO:[/bold magenta] [bold]{tgt['title']}[/bold]\n"
            f"  [dim]{tgt_authors}{' · ' + tgt['year'] if tgt.get('year') else ''}"
            f"{' · DOI: ' + tgt['doi'] if tgt.get('doi') else ''}[/dim]"
        )

        # Evidencia
        method_label = ev.get("method_label", ev.get("method", ""))
        console.print(
            f"\n  Confianza: [bold]{ev['confidence']}%[/bold]  ·  Método: {method_label}"
        )
        if ev.get("raw_reference_text"):
            raw = ev["raw_reference_text"][:200]
            console.print(f"  Texto extraído: [dim italic]{raw}[/dim italic]")
        if ev.get("method") == "title_fuzzy":
            console.print(
                f"  Título ref (norm.):    [yellow]{ev.get('ref_normalized_title', '')}[/yellow]\n"
                f"  Título target (norm.): [yellow]{ev.get('target_normalized_title', '')}[/yellow]"
            )

        # Decisión
        console.print()
        while True:
            choice = click.prompt(
                "  Decisión [a/r/s/q]",
                default="s",
                show_default=False,
            ).strip().lower()
            if choice in ("a", "r", "s", "q"):
                break
            console.print("  [red]Opción no válida. Usa a, r, s o q.[/red]")

        if choice == "q":
            console.print("[yellow]Revisión interrumpida. Guardando progreso...[/yellow]")
            break

        note = None
        if choice in ("a", "r"):
            note = click.prompt("  Nota (opcional, Enter para omitir)", default="", show_default=False) or None

        real_idx = idx_map[match["id"]]
        if choice == "a":
            matches[real_idx]["status"] = "approved"
            matches[real_idx]["user_notes"] = note
            approved.append(match)
            console.print("[green]  ✓ Aprobado[/green]")
        elif choice == "r":
            matches[real_idx]["status"] = "rejected"
            matches[real_idx]["user_notes"] = note
            console.print("[red]  ✗ Rechazado[/red]")
        else:
            console.print("[dim]  → Saltado[/dim]")

    # Guardar JSON actualizado con decisiones
    data["matches"] = matches
    data["summary"]["pending_review"] = sum(1 for m in matches if m.get("status") == "pending")
    data["summary"]["approved"] = sum(1 for m in matches if m.get("status") == "approved")
    data["summary"]["rejected"] = sum(1 for m in matches if m.get("status") == "rejected")

    with open(verification_file, "w", encoding="utf-8") as f:
        json_mod.dump(data, f, ensure_ascii=False, indent=2)
    console.print(f"\n[dim]JSON de verificación actualizado: {verification_file}[/dim]")

    console.rule("[bold]Resumen de revisión[/bold]")
    console.print(
        f"  Aprobados: [bold green]{len(approved)}[/bold green]   "
        f"Rechazados: [bold red]{sum(1 for m in matches if m.get('status')=='rejected')}[/bold red]   "
        f"Pendientes: [bold yellow]{sum(1 for m in matches if m.get('status')=='pending')}[/bold yellow]"
    )

    if not approved:
        console.print("[yellow]No hay matches aprobados para aplicar.[/yellow]")
        return

    if not apply:
        console.print(
            f"[dim]--no-apply activo: se omite la escritura en Zotero. "
            f"Vuelve a ejecutar sin --no-apply para aplicarlos.[/dim]"
        )
        return

    # Aplicar matches aprobados en Zotero
    console.rule("[bold]Aplicando relaciones aprobadas en Zotero[/bold]")
    client = ZoteroClient(cfg["zotero"]["api_url"], cfg["zotero"].get("api_key", ""))
    if not client.ping():
        console.print("[red]✗ Zotero no disponible. Las relaciones NO se han aplicado.[/red]")
        return

    updates: dict[str, list[str]] = {}
    for match in approved:
        src_key = match["source"]["key"]
        tgt_key = match["target"]["key"]
        updates.setdefault(src_key, []).append(tgt_key)

    ok = 0
    with Progress(console=console) as progress:
        task = progress.add_task("Aplicando…", total=len(updates))
        for src_key, targets in updates.items():
            if client.add_relations(src_key, targets):
                ok += 1
            progress.advance(task)

    console.print(
        f"[bold green]✓ Relaciones aplicadas en {ok}/{len(updates)} artículos.[/bold green]"
    )
    client.close()


if __name__ == "__main__":
    cli()

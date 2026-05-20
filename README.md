# Zotero Reference Automator

Batch pipeline that scans your Zotero library, extracts references from PDFs, matches them against other items in your library, and generates a relationship graph. Includes an interactive review step before writing anything to Zotero.

## Features

- Extracts reference sections from PDFs using `pdfminer.six` with the same regex patterns as the [zotero-reference](https://github.com/MuiseDestiny/zotero-reference) plugin
- **Cluster-based fallback detection** for papers without an explicit "References" header — uses three strategies: numbered/bracketed pattern density, APA author-year pattern, and DOI density
- Falls back to external APIs when a PDF has no text layer or no reference section: **Semantic Scholar → OpenAlex → CrossRef → CORE → PubMed**
- Matches references against your library by: DOI exact → arXiv exact → fuzzy title (rapidfuzz)
- Automatic OCR via `ocrmypdf` for image-only PDFs (`--ocr` flag)
- Read-only by default — generates an HTML report + JSON files; a separate `review` or `apply-links` command writes relations to Zotero
- Interactive `review` command to approve/reject each match with full evidence before applying
- AI-verifiable `verification_*.json` output with full evidence for every match
- Interactive D3.js force graph in the HTML report

## Requirements

- Python 3.11+
- [Zotero](https://www.zotero.org/) running locally with **"Allow other applications to communicate with Zotero"** enabled  
  (Edit → Preferences → Advanced → Allow other applications…)
- `ocrmypdf` (optional, for OCR): `sudo apt install ocrmypdf` or `brew install ocrmypdf`

## Installation

```bash
# Clone the repo
git clone https://github.com/MiguelDLM/automatic-cross-ref
cd automatic-cross-ref

# Install with uv (recommended)
pip install uv
uv sync

# Or with pip
pip install -e .
```

## Configuration

```bash
cp config_example.yaml config.yaml
cp .env.example .env
# Edit config.yaml and .env with your API credentials
```

`config.yaml` and `.env` are excluded from git — they stay on your machine.

Key config sections:

| Section | Key fields |
|---------|-----------|
| `zotero` | `api_url`, `api_key` (leave empty for local API) |
| `extractor` | `max_pages_from_end`, `min_ref_length`, `section_headers` |
| `matcher` | `fuzzy_threshold` (default 82), `use_crossref` |
| `apis` | `semantic_scholar_key`, `openalex_email`, `ncbi_api_key`, `core_api_key` |
| `report` | `output_dir`, `min_confidence` (default 70) |

## Usage

```bash
# Test with first 20 articles
uv run python main.py run --limit 20

# Full library scan (read-only, no changes to Zotero)
uv run python main.py run

# Only 100% certain matches (DOI/arXiv exact — zero false positives)
uv run python main.py run --only-100

# With automatic OCR for image-only PDFs
uv run python main.py run --ocr

# Skip external APIs (faster, offline)
uv run python main.py run --no-external

# Interactive review — approve/reject before applying
uv run python main.py review output/verification_YYYYMMDD_HHMMSS.json

# Apply without review (all new matches directly)
uv run python main.py apply-links output/relations_YYYYMMDD_HHMMSS.json
```

## Workflow

```
1. run          → generates HTML report + relations JSON + verification JSON
2. review       → interactive approve/reject, then applies approved to Zotero
   (or)
   apply-links  → applies all suggested relations without review
```

## Output files

| File | Description |
|------|-------------|
| `output/report_TIMESTAMP.html` | Interactive HTML report with D3.js force graph, filter bar, and candidate cards |
| `output/relations_TIMESTAMP.json` | Relation graph (nodes + edges) for `apply-links` |
| `output/verification_TIMESTAMP.json` | Full evidence for every match — for human or AI review |
| `output/ocr/` | OCR-processed PDFs (when `--ocr` is used) |

## Reference section detection

For PDFs with a clear section header ("References", "Bibliography", etc.), the extractor finds it by scanning backward from the last page.

For papers without a clear header (common in older journals or book chapters), three fallback strategies are tried in order:

1. **Numbered/bracketed pattern density** — finds a window of 12 lines where ≥3 lines start with `[1]`, `(1)`, `1.`, etc.
2. **APA author-year pattern** — finds lines matching `Lastname, I. (YYYY)` style with no explicit numbering
3. **DOI density** — finds a window of 10 lines with ≥2 DOIs

## AI-assisted verification

After `run`, the `verification_*.json` file contains every new match candidate with full evidence:

```json
{
  "id": "SRCKEY__TGTKEY",
  "status": "pending",
  "source": { "key": "...", "title": "...", "authors": [...], "year": "...", "doi": "..." },
  "target": { "key": "...", "title": "...", "authors": [...], "year": "...", "doi": "..." },
  "evidence": {
    "confidence": 95,
    "method": "title_fuzzy",
    "method_label": "Título fuzzy",
    "raw_reference_text": "Smith, J. (2021). The effect of...",
    "ref_extracted_title": "The effect of...",
    "ref_normalized_title": "the effect of",
    "target_normalized_title": "the effect of gravity",
    "ref_extracted_year": "2021",
    "ref_extracted_authors": ["Smith, J."]
  }
}
```

You can pass this file to an AI agent with the instructions in `ai_instructions` (already embedded in the JSON) to get `"approved"` / `"rejected"` decisions back. Then run `review` on the annotated file — it will skip already-decided matches and apply the approved ones.

## Architecture

```
PDFs → extractor.py (pdfminer + cluster fallback)
                                 ↓
               matcher.py (rapidfuzz DOI/arXiv/title)
                     ↕
       reference_sources.py (SS/OA/CR/CORE/PubMed)
                                 ↓
       report_builder_v2.py (HTML + D3.js + JSON)
                     ↓                    ↓
             review (interactive)    apply-links
                     ↓
          zotero_client.py (dc:relation write)
```

## Credits

Reference parsing patterns ported from [zotero-reference](https://github.com/MuiseDestiny/zotero-reference).  
Graph visualization inspired by [zotero-style](https://github.com/MuiseDestiny/zotero-style).

# Zotero Reference Automator

Batch pipeline that scans your Zotero library, extracts references from PDFs, matches them against other items in your library, and generates a relationship graph. Optionally applies the discovered relations back to Zotero.

## Features

- Extracts reference sections from PDFs using `pdfminer.six` with the same regex patterns as the [zotero-reference](https://github.com/MuiseDestiny/zotero-reference) plugin
- Falls back to external APIs when a PDF has no text layer or no reference section: **Semantic Scholar → OpenAlex → CrossRef → PubMed**
- Matches references against your library by: DOI exact → arXiv exact → fuzzy title (rapidfuzz)
- Automatic OCR via `ocrmypdf` for image-only PDFs (`--ocr` flag)
- Read-only by default — generates an HTML report + JSON; a separate `apply-links` command writes relations to Zotero
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

## Usage

```bash
# Test with first 20 articles, using OCR for image-only PDFs
uv run python main.py run --limit 20 --ocr

# Full library scan (read-only, no changes to Zotero)
uv run python main.py run

# Only 100% certain matches (DOI/arXiv exact)
uv run python main.py run --only-100

# Skip external APIs (faster, offline)
uv run python main.py run --no-external

# Apply the suggested relations to Zotero
uv run python main.py apply-links output/relations_YYYYMMDD_HHMMSS.json
```

## Output

- `output/report_TIMESTAMP.html` — interactive HTML report with D3.js graph
- `output/relations_TIMESTAMP.json` — relation graph (nodes + edges) for `apply-links`
- `output/ocr/` — OCR-processed PDFs (when `--ocr` is used)

## Architecture

```
PDFs → extractor.py (pdfminer) → matcher.py (rapidfuzz)
                                          ↕
                           reference_sources.py (SS/OA/CR/PubMed)
                                          ↓
                          report_builder_v2.py (HTML + D3.js + JSON)
                                          ↓
                          zotero_client.py (apply-links only)
```

## Credits

Reference parsing patterns ported from [zotero-reference](https://github.com/MuiseDestiny/zotero-reference).  
Graph visualization inspired by [zotero-style](https://github.com/MuiseDestiny/zotero-style).

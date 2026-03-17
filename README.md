# syscoin-context

A reproducible generator for `llms-full.txt` — a curated, LLM-friendly plaintext bundle of Syscoin's canonical documentation, protocol specifications, and developer references.

## What this produces

A single file, `llms-full.txt`, that bundles:

| Section | Sources |
|---|---|
| Node / Core | `syscoin/syscoin` — `doc/` Markdown, README, CONTRIBUTING |
| Protocol – NEVM | NEVM integration docs from core repo |
| Protocol – Z-DAG | Inline curated overview |
| Protocol – Rollux (L2) | `sys-labs/rollux` docs |
| Specs – SyIPs | `syscoin/syips` improvement proposals |
| Official Docs | Crawled from `docs.syscoin.org` |
| Rollux Docs | Crawled from `docs.rollux.com` |
| Developer – JS SDK | `syscoin/syscoinjs-lib` |
| Developer – Rosetta API | `syscoin/syscoin-rosetta` |
| Operations – Docker | `syscoin/docker-syscoin-core` |
| Reference – Network Parameters | Inline curated table (mainnet / testnet) |
| Reference – NEVM Quick Reference | Inline MetaMask / wallet connection guide |

## Project layout

```
syscoin-context/
├── sources.yaml                # Manifest: what to fetch, in what order
├── generate.py                 # Generator script
├── requirements.txt            # Python dependencies
├── .github/
│   └── workflows/
│       └── generate.yml        # GitHub Actions: scheduled + on-push regeneration
├── llms-full.txt               # (generated — not committed by default until first run)
└── llms-full.manifest.json     # (generated — SHA-256, size, section list)
```

## Quickstart

### Prerequisites

- Python 3.11+
- A GitHub personal access token (PAT) with `public_repo` read access (optional but recommended to avoid rate limits)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run the generator

```bash
# Full run (GitHub + web crawl)
GITHUB_TOKEN=ghp_... python generate.py

# Skip web crawl (faster, GitHub sources + static snippets only)
GITHUB_TOKEN=ghp_... python generate.py --skip-web

# Skip GitHub (web + static only)
python generate.py --skip-github

# Dry run — print section plan without fetching anything
python generate.py --dry-run

# Custom config / output paths
python generate.py --config sources.yaml --output my-bundle.txt
```

Output: `llms-full.txt` and `llms-full.manifest.json` in the current directory.

## Customising sources

Edit [`sources.yaml`](sources.yaml). The file has three source types:

### `github` entries

Pull Markdown/text files directly from GitHub repos:

```yaml
github:
  - repo: syscoin/syscoin
    ref: master           # branch, tag, or commit SHA
    label: "Syscoin Core – Docs"
    section: "Node / Core"
    paths:
      - doc/README.md
      - README.md
    # Or set paths: [] + recursive: true to pull all .md files
```

Set `GITHUB_TOKEN` in your environment for authenticated requests (5000 req/h vs 60 req/h unauthenticated).

### `web` entries

Fetch specific URLs or BFS-crawl a docs site:

```yaml
web:
  - label: "Syscoin Official Docs"
    base_url: "https://docs.syscoin.org"
    section: "Official Docs"
    crawl:
      root: "https://docs.syscoin.org"
      max_depth: 4
      max_pages: 200
      include_patterns:
        - "^https://docs\\.syscoin\\.org/"
      exclude_patterns:
        - "/api/"
```

HTML is stripped to clean plain text preserving headings, paragraphs, and code blocks.

### `static` entries

Inline hand-curated content blocks (network parameters, quick references, etc.):

```yaml
static:
  - label: "Syscoin Network Parameters"
    section: "Reference – Network Parameters"
    time_sensitive: true   # adds a staleness warning widget
    content: |
      ## Syscoin Network Parameters
      ...
```

### Assembly order

Control section order in `assembly_order:`. Sections not listed are appended at the end.

## GitHub Actions automation

The workflow at [`.github/workflows/generate.yml`](.github/workflows/generate.yml):

- **Scheduled**: runs every Monday at 03:00 UTC.
- **On push**: triggers when `sources.yaml`, `generate.py`, or `requirements.txt` change on `main`.
- **Manual**: trigger from the Actions tab with optional `--skip-web` / `--skip-github` flags.

On each run it:
1. Generates `llms-full.txt` + manifest.
2. Commits changed files back to `main` (with `[skip ci]` to prevent loops).
3. Uploads the files as a job artifact (retained 30 days).
4. Creates/updates a `llms-latest` GitHub Release with both files as downloadable assets, giving a stable permalink:

```
https://github.com/<owner>/<repo>/releases/download/llms-latest/llms-full.txt
```

### Required secrets / permissions

| Secret | Purpose |
|---|---|
| `GITHUB_TOKEN` | Auto-provided by Actions. Used to fetch public repos (5000 req/h) and to commit + create the release. No extra setup needed for public repos. |

For private repos or higher rate limits, add a PAT as a repository secret named `GITHUB_TOKEN` or a custom name and reference it in the workflow.

## Output format

`llms-full.txt` is structured plain text:

```
# Syscoin LLM Context Bundle
# Generated: 2026-03-12T03:00:00Z
# Version: 1.0.0
# ...
# Table of Contents
#   1. Node / Core
#   2. Protocol – NEVM (EVM Layer)
#   ...

================================================================================
## Node / Core
================================================================================
### Syscoin Core – Docs — README.md
Source: https://raw.githubusercontent.com/syscoin/syscoin/master/README.md

<content>

...
```

Each chunk includes a `Source:` line for traceability. Time-sensitive sections include a staleness warning.

## Design principles

- **Curated, not dumped**: only documentation and spec files, not raw source code.
- **Deterministic**: same `sources.yaml` + same repo state → same output.
- **Traceable**: every chunk has a `Source:` URL and the manifest records the generation timestamp + SHA-256.
- **Staleness-aware**: network parameters, chain IDs, and release info are explicitly flagged.
- **Polite crawling**: 500 ms delay between requests, respects HTTP errors and rate limits.

## Contributing

1. To add a new source, edit `sources.yaml` and add an entry under `github:`, `web:`, or `static:`.
2. To change section ordering, update `assembly_order:`.
3. Run `python generate.py --dry-run` to verify the plan before a full fetch.
4. Open a PR — the Actions workflow will validate the new config on merge.

## License

MIT

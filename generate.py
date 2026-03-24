#!/usr/bin/env python3
"""
generate.py — Syscoin LLM context bundle generator
Reads sources.yaml and produces llms-full.txt.

Usage:
    python generate.py [--config sources.yaml] [--output llms-full.txt]
    python generate.py --dry-run     # Print section plan without fetching
    python generate.py --skip-web    # Skip web crawling (faster, GitHub only)
    python generate.py --skip-github # Skip GitHub fetching (web/static only)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("generate")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

# File extensions we attempt to include from GitHub.
TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".1"}

# Minimum content length to bother including a page (bytes).
MIN_CONTENT_LENGTH = 100

# Polite crawl delay between HTTP requests (seconds).
CRAWL_DELAY = 0.5

# Max retries for transient HTTP errors.
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, doubles each retry


FAILED_LINKS: list[dict[str, Any]] = []


def record_failure(url: str, reason: str, **details: Any) -> None:
    FAILED_LINKS.append({
        "url": url,
        "reason": reason,
        **details,
    })


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    s.headers["User-Agent"] = "syscoin-llms-full-generator/1.0 (https://github.com/syscoin)"
    return s


SESSION = _session()


def fetch(url: str, *, retries: int = MAX_RETRIES, delay: float = CRAWL_DELAY) -> requests.Response | None:
    """Fetch a URL with retries, returning the Response or None on failure."""
    time.sleep(delay)
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, timeout=30)
            if resp.status_code == 404:
                log.warning("404 Not Found: %s", url)
                record_failure(url, "http_404", status_code=404)
                return None
            if resp.status_code == 429:
                wait = RETRY_BACKOFF ** attempt
                log.warning("Rate limited (%s). Waiting %.1fs …", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(RETRY_BACKOFF ** attempt)
            else:
                record_failure(
                    url,
                    "request_exception",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                    attempts=retries,
                )
    log.error("Giving up on %s after %d attempts", url, retries)
    return None


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def github_raw_url(repo: str, ref: str, path: str) -> str:
    return f"{GITHUB_RAW}/{repo}/{ref}/{path}"


def github_tree_url(repo: str, ref: str) -> str:
    return f"{GITHUB_API}/repos/{repo}/git/trees/{ref}?recursive=1"


def list_github_files(repo: str, ref: str) -> list[str]:
    """Return all file paths in a GitHub repo at the given ref."""
    url = github_tree_url(repo, ref)
    resp = fetch(url, delay=0)
    if resp is None:
        return []
    data = resp.json()
    if data.get("truncated"):
        log.warning("GitHub tree truncated for %s@%s — some files may be missing", repo, ref)
    return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]


def fetch_github_file(repo: str, ref: str, path: str) -> str | None:
    """Fetch the raw content of a single file from GitHub."""
    url = github_raw_url(repo, ref, path)
    resp = fetch(url, delay=0.2)
    if resp is None:
        return None
    return resp.text


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------
_NAV_TAGS = {"nav", "header", "footer", "aside", "script", "style", "noscript", "form"}
_NAV_CLASSES = re.compile(
    r"\b(nav|navbar|sidebar|footer|header|menu|breadcrumb|cookie|banner|"
    r"toc|table-of-contents|pagination|search|modal|overlay)\b",
    re.IGNORECASE,
)


def html_to_text(html: str, url: str = "") -> str:
    """
    Strip HTML to clean plain text suitable for LLM consumption.
    Preserves heading structure (### markers), code blocks, and paragraphs.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove navigation / chrome / scripts
    for tag in soup.find_all(_NAV_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        cls = " ".join(tag.get("class", []))
        if _NAV_CLASSES.search(cls):
            tag.decompose()

    # Prefer <main> or <article> if available.
    main = soup.find("main") or soup.find("article") or soup.find(id="content") or soup.find(id="main")
    root = main if main else soup

    lines: list[str] = []

    def walk(node: Any) -> None:
        if hasattr(node, "name") and node.name:
            tag = node.name.lower()
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(tag[1])
                text = node.get_text(" ", strip=True)
                lines.append(f"\n{'#' * level} {text}\n")
            elif tag in ("pre", "code"):
                code = node.get_text()
                parent = node.parent
                if tag == "pre" or (parent and parent.name == "pre"):
                    # Detect language hint from class
                    classes = " ".join(node.get("class", []))
                    lang_match = re.search(r"language-(\w+)", classes)
                    lang = lang_match.group(1) if lang_match else ""
                    lines.append(f"\n```{lang}\n{code.strip()}\n```\n")
                else:
                    lines.append(f"`{code.strip()}`")
                return  # don't recurse into code
            elif tag in ("p", "li", "dt", "dd", "blockquote"):
                text = node.get_text(" ", strip=True)
                if text:
                    if tag == "blockquote":
                        lines.append(f"\n> {text}\n")
                    elif tag == "li":
                        lines.append(f"- {text}")
                    else:
                        lines.append(f"\n{text}\n")
                return
            elif tag in ("table",):
                lines.append(f"\n{node.get_text(' ', strip=True)}\n")
                return
            elif tag == "br":
                lines.append("")
            else:
                for child in node.children:
                    walk(child)
        elif hasattr(node, "string") and node.string:
            text = str(node.string).strip()
            if text:
                lines.append(text)

    walk(root)
    body = "\n".join(lines)
    # Collapse 3+ consecutive blank lines to 2.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def crawl_site(
    root: str,
    *,
    max_depth: int = 3,
    max_pages: int = 100,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[tuple[str, str]]:
    """
    BFS crawl starting from `root`.  Returns list of (url, plain_text).
    """
    include_re = [re.compile(p) for p in (include_patterns or [])]
    exclude_re = [re.compile(p) for p in (exclude_patterns or [])]

    def allowed(url: str) -> bool:
        for pat in exclude_re:
            if pat.search(url):
                return False
        if include_re:
            return any(pat.search(url) for pat in include_re)
        return True

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(root, 0)]
    results: list[tuple[str, str]] = []

    while queue and len(results) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not allowed(url):
            continue

        log.info("Crawling [depth=%d] %s", depth, url)
        resp = fetch(url)
        if resp is None:
            continue
        ct = resp.headers.get("content-type", "")
        if "html" not in ct:
            continue

        text = html_to_text(resp.text, url)
        if len(text) >= MIN_CONTENT_LENGTH:
            results.append((url, text))

        if depth < max_depth:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                # Strip fragment and query
                parsed = urlparse(href)
                clean = parsed._replace(fragment="", query="").geturl()
                if clean not in visited and allowed(clean):
                    queue.append((clean, depth + 1))

    log.info("Crawl complete: %d pages from %s", len(results), root)
    return results


# ---------------------------------------------------------------------------
# Section builder
# ---------------------------------------------------------------------------

class Section:
    """A named content bucket that accumulates text chunks."""

    def __init__(self, name: str, time_sensitive: bool = False) -> None:
        self.name = name
        self.time_sensitive = time_sensitive
        self.chunks: list[str] = []

    def add(self, source_label: str, source_url: str, content: str) -> None:
        header = f"### {source_label}\nSource: {source_url}\n"
        self.chunks.append(f"{header}\n{content.strip()}\n")

    def render(self) -> str:
        if not self.chunks:
            return ""
        sep = "\n" + "=" * 80 + "\n"
        header = f"\n{'=' * 80}\n## {self.name}\n{'=' * 80}\n"
        if self.time_sensitive:
            header += (
                "\n> ⚠️  TIME-SENSITIVE: The information in this section may be stale.\n"
                "> Verify network parameters, versions, and endpoints before use.\n\n"
            )
        return header + sep.join(self.chunks)


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_sections(config: dict, *, skip_web: bool, skip_github: bool) -> dict[str, Section]:
    sections: dict[str, Section] = {}
    FAILED_LINKS.clear()

    def get_section(name: str, time_sensitive: bool = False) -> Section:
        if name not in sections:
            sections[name] = Section(name, time_sensitive)
        return sections[name]

    # ---- Static snippets ---------------------------------------------------
    for item in config.get("static", []):
        sec = get_section(item["section"], item.get("time_sensitive", False))
        sec.add(
            item["label"],
            "inline/static",
            item["content"],
        )
        log.info("[static] Added '%s'", item["label"])

    # ---- GitHub sources ----------------------------------------------------
    if not skip_github:
        for item in config.get("github", []):
            repo = item["repo"]
            ref = item.get("ref", "main")
            label = item["label"]
            section_name = item["section"]
            recursive = item.get("recursive", False)
            paths = item.get("paths", [])

            sec = get_section(section_name)

            # Resolve file list
            files_to_fetch: list[str] = []

            if not paths:
                # Include all text files at repo root (non-recursive by default)
                log.info("[github] Listing tree for %s@%s …", repo, ref)
                all_files = list_github_files(repo, ref)
                for fp in all_files:
                    ext = Path(fp).suffix.lower()
                    if ext in TEXT_EXTENSIONS:
                        if recursive or "/" not in fp:
                            files_to_fetch.append(fp)
            else:
                for p in paths:
                    if p.endswith("/") or not Path(p).suffix:
                        # Directory glob: list tree and filter
                        log.info("[github] Listing tree for %s@%s to expand dir '%s' …", repo, ref, p)
                        all_files = list_github_files(repo, ref)
                        prefix = p.rstrip("/") + "/"
                        for fp in all_files:
                            if fp.startswith(prefix):
                                ext = Path(fp).suffix.lower()
                                if ext in TEXT_EXTENSIONS:
                                    files_to_fetch.append(fp)
                    else:
                        files_to_fetch.append(p)

            for file_path in files_to_fetch:
                ext = Path(file_path).suffix.lower()
                if ext not in TEXT_EXTENSIONS:
                    log.debug("[github] Skipping non-text file: %s", file_path)
                    continue
                raw_url = github_raw_url(repo, ref, file_path)
                log.info("[github] Fetching %s/%s/%s", repo, ref, file_path)
                content = fetch_github_file(repo, ref, file_path)
                if content is None:
                    log.warning("[github] Skipped (not found): %s/%s", repo, file_path)
                    continue
                if len(content.strip()) < MIN_CONTENT_LENGTH:
                    log.debug("[github] Skipped (too short): %s/%s", repo, file_path)
                    continue
                sec.add(f"{label} — {file_path}", raw_url, content)

    # ---- Web sources -------------------------------------------------------
    if not skip_web:
        for item in config.get("web", []):
            label = item["label"]
            section_name = item["section"]
            sec = get_section(section_name)

            if "urls" in item:
                for url in item["urls"]:
                    log.info("[web] Fetching %s", url)
                    resp = fetch(url)
                    if resp is None:
                        continue
                    text = html_to_text(resp.text, url)
                    if len(text) >= MIN_CONTENT_LENGTH:
                        sec.add(label, url, text)

            elif "crawl" in item:
                crawl_cfg = item["crawl"]
                pages = crawl_site(
                    crawl_cfg["root"],
                    max_depth=crawl_cfg.get("max_depth", 3),
                    max_pages=crawl_cfg.get("max_pages", 100),
                    include_patterns=crawl_cfg.get("include_patterns"),
                    exclude_patterns=crawl_cfg.get("exclude_patterns"),
                )
                for url, text in pages:
                    sec.add(f"{label} — {url}", url, text)

    return sections


def assemble(config: dict, sections: dict[str, Section]) -> str:
    meta = config.get("metadata", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # File header
    lines = [
        f"# {meta.get('title', 'Syscoin LLM Context Bundle')}",
        f"# Generated: {now}",
        f"# Version: {meta.get('version', 'n/a')}",
        "#",
        f"# {meta.get('description', '')}",
        "#",
        f"# {meta.get('staleness_notice', '')}",
        "#",
        "# Table of Contents",
    ]

    order: list[str] = config.get("assembly_order", [])
    # Sections not in assembly_order go at the end.
    remaining = [k for k in sections if k not in order]
    full_order = [s for s in order if s in sections] + remaining

    for i, name in enumerate(full_order, 1):
        lines.append(f"#   {i:2d}. {name}")

    lines.append("#\n")

    parts = ["\n".join(lines)]
    for name in full_order:
        rendered = sections[name].render()
        if rendered:
            parts.append(rendered)

    parts.append(
        f"\n{'=' * 80}\n# END OF SYSCOIN LLM CONTEXT BUNDLE\n# Generated: {now}\n{'=' * 80}\n"
    )
    return "\n".join(parts)


def _github_repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _infer_intent(section: str) -> str:
    section_l = section.lower()
    if "official docs" in section_l:
        return "Learn"
    if "blog" in section_l or "ecosystem" in section_l:
        return "Ecosystem"
    if any(k in section_l for k in ("node", "operations", "docker", "sentinel", "masternode")):
        return "Run"
    if any(k in section_l for k in ("developer", "rpc", "sdk", "bridge", "wallet")):
        return "Build"
    if any(k in section_l for k in ("spec", "syip", "research")):
        return "Research / Specs"
    if any(k in section_l for k in ("reference", "protocol", "nevm", "zdag", "z-dag", "zksys", "zk")):
        return "Learn"
    return "Optional"


def _github_note(section: str, label: str, ref: str) -> str:
    section_l = section.lower()
    label_l = label.lower()
    if "rpc" in section_l or "rpc" in label_l or "cli" in label_l:
        return "RPC and CLI developer reference."
    if "sdk" in section_l:
        return "SDK docs and usage examples."
    if "bridge" in section_l or "bridge" in label_l:
        return "Bridge integration and cross-chain developer docs."
    if "wallet" in section_l or "wallet" in label_l or "snap" in label_l:
        return "Wallet integration and app-facing docs."
    if "docker" in section_l:
        return "Containerized node and operations guidance."
    if "syip" in section_l or "spec" in section_l:
        return "Protocol proposals and specification material."
    if "node" in section_l or "core" in section_l:
        return "Core node documentation and operational guidance."
    if "protocol" in section_l or "nevm" in section_l or "zk" in section_l:
        return "Protocol-level documentation and technical reference."
    return f"Canonical repository source ({ref} branch/ref)."


def _extract_failed_repos() -> set[str]:
    failed_repos: set[str] = set()
    for item in FAILED_LINKS:
        url = item.get("url", "")
        raw_m = re.search(r"raw\.githubusercontent\.com/([^/]+/[^/]+)/", url)
        if raw_m:
            failed_repos.add(raw_m.group(1))
            continue
        api_m = re.search(r"api\.github\.com/repos/([^/]+/[^/]+)/", url)
        if api_m:
            failed_repos.add(api_m.group(1))
    return failed_repos


def _extract_failed_urls() -> set[str]:
    return {item.get("url", "") for item in FAILED_LINKS if item.get("url")}


def assemble_llms_index(config: dict, full_output_name: str) -> str:
    """Build an intent-oriented llms.txt index for lightweight retrieval."""
    meta = config.get("metadata", {})
    title = meta.get("title", "Syscoin")
    description = meta.get(
        "description",
        "Curated index of canonical Syscoin docs for LLM retrieval.",
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    failed_repos = _extract_failed_repos()
    failed_urls = _extract_failed_urls()

    intent_entries: dict[str, list[tuple[str, str, str]]] = {
        "Learn": [],
        "Build": [],
        "Run": [],
        "Research / Specs": [],
        "Ecosystem": [],
        "Optional": [],
    }
    seen: set[tuple[str, str]] = set()

    def add_entry(intent: str, label: str, url: str, note: str) -> None:
        key = (label, url)
        if key in seen:
            return
        seen.add(key)
        intent_entries[intent].append((label, url, note))

    # Keep a stable first section pointing to the generated full bundle.
    add_entry(
        "Learn",
        "llms-full.txt",
        full_output_name,
        "Expanded context bundle with source-attributed content.",
    )

    for item in config.get("github", []):
        section = item.get("section", "Uncategorized")
        repo = item["repo"]
        ref = item.get("ref", "main")
        label = item.get("label", repo)
        intent = _infer_intent(section)
        note = _github_note(section, label, ref)
        if repo in failed_repos:
            note = f"Temporarily unstable source in latest generation; verify before relying on it. {note}"
        paths = item.get("paths", [])
        added_any = False

        if paths:
            for p in paths:
                # Directory-style path: link to repo tree, not raw.
                if p.endswith("/") or not Path(p).suffix:
                    tree_url = f"{_github_repo_url(repo)}/tree/{ref}/{p.rstrip('/')}"
                    if tree_url not in failed_urls:
                        add_entry(intent, f"{label} — {p.rstrip('/')}/", tree_url, f"Directory index. {note}")
                        added_any = True
                    continue

                ext = Path(p).suffix.lower()
                if ext not in TEXT_EXTENSIONS:
                    continue

                raw_url = github_raw_url(repo, ref, p)
                if raw_url in failed_urls:
                    continue
                add_entry(intent, f"{label} — {p}", raw_url, note)
                added_any = True

        if not added_any:
            repo_url = _github_repo_url(repo)
            if repo_url not in failed_urls:
                add_entry(intent, label, repo_url, note)

    for item in config.get("web", []):
        section = item.get("section", "Official Docs")
        intent = _infer_intent(section)
        label = item.get("label", "Web Docs")
        if "urls" in item:
            for url in item["urls"]:
                add_entry(intent, label, url, "Canonical web page.")
        elif "crawl" in item:
            root = item["crawl"].get("root")
            if root:
                add_entry(intent, label, root, "Crawl root for official docs.")

    for item in config.get("static", []):
        section = item.get("section", "Reference")
        intent = _infer_intent(section)
        label = item.get("label", "Static Reference")
        add_entry(intent, label, full_output_name, "Inlined reference inside llms-full.txt.")

    link_count = sum(len(v) for v in intent_entries.values())

    lines = [
        f"# {title}",
        "",
        f"> {description}",
        "",
        "Use this index for lightweight discovery. Start here, then load llms-full.txt for deep inline context.",
        "",
        "## Metadata",
        f"- Generated: {now}",
        f"- Link count: {link_count}",
        f"- Known source failures in last run: {len(FAILED_LINKS)}",
        "",
    ]

    for intent in ("Learn", "Build", "Run", "Research / Specs", "Ecosystem", "Optional"):
        entries = intent_entries[intent]
        if not entries:
            continue
        lines.append(f"## {intent}")
        for label, url, note in entries:
            lines.append(f"- [{label}]({url}): {note}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Syscoin llms-full.txt")
    parser.add_argument("--config", default="sources.yaml", help="Path to sources.yaml")
    parser.add_argument("--output", default=None, help="Output file path (default: from config)")
    parser.add_argument("--dry-run", action="store_true", help="Print section plan without fetching")
    parser.add_argument("--skip-web", action="store_true", help="Skip web crawling")
    parser.add_argument("--skip-github", action="store_true", help="Skip GitHub fetching")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        sys.exit(1)

    config = load_config(str(config_path))
    output_path = args.output or config.get("metadata", {}).get("output_file", "llms-full.txt")

    if args.dry_run:
        print("=== DRY RUN — Section plan ===")
        order = config.get("assembly_order", [])
        all_sections: list[str] = []
        for src_type in ("github", "web", "static"):
            for item in config.get(src_type, []):
                name = item.get("section", "Unknown")
                if name not in all_sections:
                    all_sections.append(name)
        ordered = [s for s in order if s in all_sections]
        remaining = [s for s in all_sections if s not in ordered]
        for i, s in enumerate(ordered + remaining, 1):
            print(f"  {i:2d}. {s}")
        print(f"\nOutput: {output_path}")
        return

    log.info("Starting Syscoin LLM context bundle generation …")
    if not os.environ.get("GITHUB_TOKEN"):
        log.warning(
            "GITHUB_TOKEN not set. GitHub API rate limits apply (60 req/h unauthenticated). "
            "Set the env var to increase to 5000 req/h."
        )

    sections = build_sections(config, skip_web=args.skip_web, skip_github=args.skip_github)
    bundle = assemble(config, sections)
    index_text = assemble_llms_index(config, Path(output_path).name)

    out = Path(output_path)
    out.write_text(bundle, encoding="utf-8")
    sha = compute_sha256(bundle)
    size_mb = out.stat().st_size / (1024 * 1024)

    log.info("Written: %s (%.2f MB, SHA-256: %s)", out, size_mb, sha)

    llms_index_path = out.with_name("llms.txt")
    llms_index_path.write_text(index_text, encoding="utf-8")
    log.info("Index: %s", llms_index_path)

    # Write a small manifest alongside the output file.
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_file": str(out),
        "size_bytes": out.stat().st_size,
        "sha256": sha,
        "sections": list(sections.keys()),
        "failure_count": len(FAILED_LINKS),
        "config_version": config.get("metadata", {}).get("version", "n/a"),
    }
    manifest_path = out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info("Manifest: %s", manifest_path)

    failures_path = out.with_suffix(".failures.json")
    failures_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_file": str(out),
        "failure_count": len(FAILED_LINKS),
        "failures": FAILED_LINKS,
    }
    failures_path.write_text(json.dumps(failures_report, indent=2) + "\n", encoding="utf-8")
    log.info("Failures: %s (%d recorded)", failures_path, len(FAILED_LINKS))


if __name__ == "__main__":
    main()

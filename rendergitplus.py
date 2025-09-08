#!/usr/bin/env python3
"""
Flatten a Git repo into a single static HTML page for fast skimming and Ctrl+F.
"""

from __future__ import annotations
import argparse
import html
import json
import os
import pathlib
import shutil
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from typing import List
from typing import Any

# External deps
from git import Repo
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, TextLexer
import markdown

try:
    import python_minifier
except ImportError:
    python_minifier = None

try:
    import rjsmin
except ImportError:
    rjsmin = None

try:
    import rcssmin
except ImportError:
    rcssmin = None

try:
    import htmlmin
except ImportError:
    htmlmin = None

MAX_DEFAULT_BYTES = 50 * 1024
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".flac",
    ".ttf", ".otf", ".eot", ".woff", ".woff2",
    ".so", ".dll", ".dylib", ".class", ".jar", ".exe", ".bin",
}
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".mkdn"}
MINIFIABLE_EXTENSIONS = {
    ".py": "python", ".json": "json", ".js": "javascript", ".css": "css",
    ".html": "html", ".htm": "html"
}


@dataclass
class RenderDecision:
    include: bool
    reason: str  # "ok" | "binary" | "too_large" | "ignored"


@dataclass
class FileInfo:
    path: pathlib.Path  # absolute path on disk
    rel: str            # path relative to repo root (slash-separated)
    size: int
    decision: RenderDecision


def bytes_human(n: int) -> str:
    """Human-readable bytes: 1 decimal for KiB and above, integer for B."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f, i = float(n), 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{int(f)} {units[i]}" if i == 0 else f"{f:.1f} {units[i]}"


def looks_binary(path: pathlib.Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        # Heuristic: try UTF-8 decode; if it hard-fails, likely binary
        data = path.open("rb").read(8192)
        if b"\x00" in data:
            return True
        data.decode("utf-8")
        return False
    except Exception:
        # If unreadable, treat as binary to be safe
        return True

def minify_code(text: str, ext: str) -> str:
    """Minify code based on file extension."""
    minify_type = MINIFIABLE_EXTENSIONS.get(ext.lower())

    try:
        if minify_type == "python" and python_minifier:
            return python_minifier.minify(text, remove_literal_statements=True)
        elif minify_type == "javascript" and rjsmin:
            return rjsmin.jsmin(text)
        elif minify_type == "css" and rcssmin:
            return rcssmin.cssmin(text)
        elif minify_type == "html" and htmlmin:
            return htmlmin.minify(text, remove_empty_space=True, remove_comments=True)
        elif minify_type == "json":
            # Use built-in json for minification
            try:
                parsed = json.loads(text)
                return json.dumps(parsed, separators=(',', ':'))
            except json.JSONDecodeError:
                return text  # Return original if not valid JSON
    except Exception:
        # If no minifier available or minification fails, return original text
        pass

    return text


def decide_file(path: pathlib.Path, repo_root: pathlib.Path, max_bytes: int) -> FileInfo:
    rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    # Ignore VCS and build junk
    if "/.git/" in f"/{rel}/" or rel.startswith(".git/"):
        return FileInfo(path, rel, size, RenderDecision(False, "ignored"))
    if size > max_bytes:
        return FileInfo(path, rel, size, RenderDecision(False, "too_large"))
    if looks_binary(path):
        return FileInfo(path, rel, size, RenderDecision(False, "binary"))
    return FileInfo(path, rel, size, RenderDecision(True, "ok"))


def get_tracked_files(repo_root: pathlib.Path) -> set[str]:
    repo = Repo(repo_root)
    return set(repo.git.ls_files().splitlines())


def collect_files(repo_root: pathlib.Path, max_bytes: int) -> List[FileInfo]:
    tracked = get_tracked_files(repo_root)
    infos: List[FileInfo] = []
    for rel in sorted(tracked):
        p = repo_root / rel
        if p.is_file():
            infos.append(decide_file(p, repo_root, max_bytes))
    return infos


def build_git_tree(repo_root: pathlib.Path) -> str:
    """
    Build a directory-tree string from Git’s tracked (and unignored) files.
    """
    repo = Repo(repo_root)
    # ls_files lists only tracked, non-ignored files
    tracked = repo.git.ls_files().splitlines()

    # Build nested dict structure
    tree: dict[str, Any] = {}
    for path in tracked:
        parts = path.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(parts[-1], None)

    lines: list[str] = [repo_root.name]

    def walk(node: dict[str, Any], prefix: str = ""):
        items = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0].lower()))
        for i, (name, subtree) in enumerate(items):
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            lines.append(f"{prefix}{branch}{name}")
            if subtree is not None:
                extension = "    " if last else "│   "
                walk(subtree, prefix + extension)

    walk(tree)
    return "\n".join(lines)


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def render_markdown_text(md_text: str) -> str:
    return markdown.markdown(md_text, extensions=["fenced_code", "tables", "toc"])  # type: ignore


def highlight_code(text: str, filename: str, formatter: HtmlFormatter) -> str:
    try:
        lexer = get_lexer_for_filename(filename, stripall=False)
    except Exception:
        lexer = TextLexer(stripall=False)
    return highlight(text, lexer, formatter)


def slugify(path_str: str) -> str:
    # Simple slug: keep alnum, dash, underscore; replace others with '-'
    out = []
    for ch in path_str:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)


def generate_cxml_text(infos: List[FileInfo], repo_dir: pathlib.Path, minify: bool = False) -> str:
    """Generate CXML format text for LLM consumption."""
    lines = ["<documents>"]

    rendered = [i for i in infos if i.decision.include]
    for index, i in enumerate(rendered, 1):
        lines.append(f'<document index="{index}">')
        lines.append(f"<source>{i.rel}</source>")
        lines.append("<document_content>")

        try:
            text = read_text(i.path)
            # Apply minification if requested and file type is supported
            if minify:
                text = minify_code(text, i.path.suffix)
            lines.append(text)
        except Exception as e:
            lines.append(f"Failed to read: {str(e)}")

        lines.append("</document_content>")
        lines.append("</document>")

    lines.append("</documents>")
    return "\n".join(lines)


def build_html(repo_url: str, repo_dir: pathlib.Path, head_commit: str, infos: List[FileInfo], minify: bool = False) -> str:
    formatter = HtmlFormatter(nowrap=False)
    pygments_css = formatter.get_style_defs('.highlight')

    # Stats
    rendered = [i for i in infos if i.decision.include]
    skipped_binary = [i for i in infos if i.decision.reason == "binary"]
    skipped_large = [i for i in infos if i.decision.reason == "too_large"]
    skipped_ignored = [i for i in infos if i.decision.reason == "ignored"]
    total_files = len(rendered) + len(skipped_binary) + len(skipped_large) + len(skipped_ignored)

    # Directory tree
    tree_text = build_git_tree(repo_dir)
    # Generate CXML text for LLM view
    cxml_text = generate_cxml_text(infos, repo_dir, minify=minify)

    # Table of contents
    toc_items: List[str] = []
    for i in rendered:
        anchor = slugify(i.rel)
        toc_items.append(
            f'<li><a href="#file-{anchor}">{html.escape(i.rel)}</a> '
            f'<span class="muted">({bytes_human(i.size)})</span></li>'
        )
    toc_html = "".join(toc_items)

    # Render file sections
    sections: List[str] = []
    for i in rendered:
        anchor = slugify(i.rel)
        ext = i.path.suffix.lower()
        try:
            text = read_text(i.path)
            if ext in MARKDOWN_EXTENSIONS:
                # Use Pygments Markdown lexer for syntax highlighting raw markdown text
                from pygments.lexers.markup import MarkdownLexer
                lexer = MarkdownLexer()
                code_html = highlight(text, lexer, formatter)
                body_html = f'<div class="highlight">{code_html}</div>'
            else:
                code_html = highlight_code(text, i.rel, formatter)
                body_html = f'<div class="highlight">{code_html}</div>'
        except Exception as e:
            body_html = f'<pre class="error">Failed to render: {html.escape(str(e))}</pre>'
        sections.append(f"""
<section class="file-section" id="file-{anchor}">
  <h2>{html.escape(i.rel)} <span class="muted">({bytes_human(i.size)})</span></h2>
  <div class="file-body">{body_html}</div>
  <div class="back-top"><a href="#top">↑ Back to top</a></div>
</section>
""")

    # Skips lists
    def render_skip_list(title: str, items: List[FileInfo]) -> str:
        if not items:
            return ""
        lis = [
            f'<li><code>{html.escape(i.rel)}</code> <span class="muted">({bytes_human(i.size)})</span></li>'
            for i in items
        ]
        return (
            f'<details open><summary>{html.escape(title)} ({len(items)})</summary>'
            f'<ul class="skip-list">\n' + "\n".join(lis) + "\n</ul></details>"
        )

    skipped_html = render_skip_list("Skipped binaries", skipped_binary) + render_skip_list(
        "Skipped large files", skipped_large
    )

    # HTML with left sidebar TOC
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Flattened repo – {html.escape(repo_url)}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, 'Apple Color Emoji','Segoe UI Emoji';
    margin: 0; padding: 0; line-height: 1.45;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 0 1rem; }}
  .meta small {{ color: #666; }}
  .counts {{ margin-top: 0.25rem; color: #333; }}
  .muted {{ color: #777; font-weight: normal; font-size: 0.9em; }}

  /* Layout with sidebar */
  .page {{ display: grid; grid-template-columns: 320px minmax(0,1fr); gap: 0; }}
  #sidebar {{
    position: sticky; top: 0; align-self: start;
    height: 100vh; overflow: auto;
    border-right: 1px solid #eee; background: #fafbfc;
  }}
  #sidebar .sidebar-inner {{ padding: 0.75rem; }}
  #sidebar h2 {{ margin: 0 0 0.5rem 0; font-size: 1rem; }}

  .toc {{ list-style: none; padding-left: 0; margin: 0; overflow-x: auto; }}
  .toc li {{ padding: 0.15rem 0; white-space: nowrap; }}
  .toc a {{ text-decoration: none; color: #0366d6; display: inline-block; text-decoration: none; }}
  .toc a:hover {{ text-decoration: underline; }}

  main.container {{ padding-top: 1rem; }}

  pre {{ background: #f6f8fa; padding: 0.75rem; overflow: auto; border-radius: 6px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono','Courier New', monospace; }}
  .highlight {{ overflow-x: auto; }}
  .file-section {{ padding: 1rem; border-top: 1px solid #eee; }}
  .file-section h2 {{ margin: 0 0 0.5rem 0; font-size: 1.1rem; }}
  .file-body {{ margin-bottom: 0.5rem; }}
  .back-top {{ font-size: 0.9rem; }}
  .skip-list code {{ background: #f6f8fa; padding: 0.1rem 0.3rem; border-radius: 4px; }}
  .error {{ color: #b00020; background: #fff3f3; }}

  :target {{ scroll-margin-top: 8px; }}

  /* View toggle */
  .view-toggle {{
    margin: 1rem 0;
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }}
  .toggle-btn {{
    padding: 0.5rem 1rem;
    border: 1px solid #d1d9e0;
    background: white;
    cursor: pointer;
    border-radius: 6px;
    font-size: 0.9rem;
  }}
  .toggle-btn.active {{
    background: #0366d6;
    color: white;
    border-color: #0366d6;
  }}
  .toggle-btn:hover:not(.active) {{
    background: #f6f8fa;
  }}

  /* LLM view */
  #llm-view {{ display: none; }}
  #llm-text {{
    width: 100%;
    height: 70vh;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.85em;
    border: 1px solid #d1d9e0;
    border-radius: 6px;
    padding: 1rem;
    resize: vertical;
  }}
  .copy-hint {{
    margin-top: 0.5rem;
    color: #666;
    font-size: 0.9em;
  }}

  /* Pygments */
  {pygments_css}
</style>
</head>
<body>
<a id="top"></a>

<div class="page">
  <nav id="sidebar"><div class="sidebar-inner">
      <h2>Table of contents ({len(rendered)})</h2>
      <ul class="toc toc-sidebar">
        <li><a href="#top">↑ Back to top</a></li>
        {toc_html}
      </ul>
  </div></nav>

  <main class="container">

    <section>
        <div class="meta">
        <div><strong>Repository:</strong> <a href="{html.escape(repo_url)}">{html.escape(repo_url)}</a></div>
        <small><strong>HEAD commit:</strong> {html.escape(head_commit)}</small>
        <div class="counts">
            <strong>Total files:</strong> {total_files} · <strong>Rendered:</strong> {len(rendered)} · <strong>Skipped:</strong> {len(skipped_binary) + len(skipped_large) + len(skipped_ignored)}
        </div>
        </div>
    </section>

    <div class="view-toggle">
      <strong>View:</strong>
      <button class="toggle-btn active" onclick="showHumanView()">👤 Human</button>
      <button class="toggle-btn" onclick="showLLMView()">🤖 LLM</button>
    </div>

    <div id="human-view">
      <section>
        <h2>Directory tree</h2>
        <details>
          <summary>Click to expand/collapse</summary>
          <pre>{html.escape(tree_text) if tree_text else 'No directory tree content available.'}</pre>
        </details>
      </section>

      <section>
        <h2>Skipped items</h2>
        <details>
          <summary>Click to expand/collapse</summary>
          <pre>{skipped_html if skipped_html else 'No skipped items.'}</pre>
        </details>
      </section>

      {"".join(sections)}
    </div>

    <div id="llm-view">
      <section>
        <h2>🤖 LLM View - CXML Format</h2>
        <p>Copy the text below and paste it to an LLM for analysis:</p>
        <textarea id="llm-text" readonly>{html.escape(cxml_text)}</textarea>
        <div class="copy-hint">
          💡 <strong>Tip:</strong> Click in the text area and press Ctrl+A (Cmd+A on Mac) to select all, then Ctrl+C (Cmd+C) to copy.
        </div>
      </section>
    </div>
  </main>
</div>

<script>
function showHumanView() {{
  document.getElementById('human-view').style.display = 'block';
  document.getElementById('llm-view').style.display = 'none';
  document.querySelectorAll('.toggle-btn').forEach(btn => btn.classList.remove('active'));
  event.target.classList.add('active');
}}

function showLLMView() {{
  document.getElementById('human-view').style.display = 'none';
  document.getElementById('llm-view').style.display = 'block';
  document.querySelectorAll('.toggle-btn').forEach(btn => btn.classList.remove('active'));
  event.target.classList.add('active');

  // Auto-select all text when switching to LLM view for easy copying
  setTimeout(() => {{
    const textArea = document.getElementById('llm-text');
    textArea.focus();
    textArea.select();
  }}, 100);
}}
</script>
</body>
</html>
"""


def derive_temp_output_path(repo_url: str, for_llm: bool = False) -> pathlib.Path:
    """Derive a temporary output path from the repo URL or local path."""
    # Extract repo name from URL like https://github.com/owner/repo or https://github.com/owner/repo.git
    # or use the directory name for local paths.
    repo_path = pathlib.Path(repo_url)
    if repo_path.is_dir():
        # For local directories, use the directory name
        repo_name = repo_path.resolve().name
    else:
        # Assume it's a URL, extract repo name
        parts = repo_url.rstrip("/").split("/")
        repo_name = parts[-1].removesuffix(".git") if len(parts) >= 2 else "repo"
    # Determine the correct suffix based on whether it's for LLM output
    suffix = ".txt" if for_llm else ".html"
    return pathlib.Path(tempfile.gettempdir()) / f"{repo_name}{suffix}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Flatten a Git repository (local or remote) to a single HTML page or text file")
    ap.add_argument("repo_source", help="Git repository URL or local git repository directory path (https://github.com/owner/repo[.git])")
    ap.add_argument("-o", "--out", help="Output file path (default: temporary file derived from repo name)")
    ap.add_argument("--max-bytes", type=int, default=MAX_DEFAULT_BYTES, help="Max file size to render (bytes); larger files are listed but skipped")
    ap.add_argument("--no-open", action="store_true", help="Don't open the HTML file in browser after generation")
    ap.add_argument("-l", "--llm", action="store_true", help="Output LLM only view (CXML text) to a text file")
    ap.add_argument("-m", "--minify", action="store_true", help="Minify supported code files for LLM consumption (For LLM output only)")
    args = ap.parse_args()

    if args.out is None:
        if args.llm:
            base_path = derive_temp_output_path(args.repo_source, for_llm=True)
            args.out = str(base_path)
        else:
            args.out = str(derive_temp_output_path(args.repo_source))

    source_path = pathlib.Path(args.repo_source)
    is_local_repo = source_path.is_dir() and (source_path / ".git").is_dir()
    tmpdir = None # Initialize tmpdir to None

    if is_local_repo:
        repo_dir = source_path.resolve()
        # For a local repo, use the directory name as the "URL" in the report
        repo_url = str(repo_dir)
        print(f"📁 Using local repository: {repo_dir}", file=sys.stderr)
        repo = Repo(repo_dir)
        head = repo.head.commit.hexsha
        print(f"✓ Local repo identified (HEAD: {head[:8]})", file=sys.stderr)
    else:
        # If not a local dir, assume it's a URL to be cloned
        repo_url = args.repo_source
        tmpdir = tempfile.mkdtemp(prefix="flatten_repo_")
        repo_dir = pathlib.Path(tmpdir, "repo")
        print(f"📁 Cloning {repo_url} to temporary directory: {repo_dir}", file=sys.stderr)
        Repo.clone_from(repo_url, repo_dir, depth=1)
        repo = Repo(repo_dir)
        head = repo.head.commit.hexsha
        print(f"✓ Clone complete (HEAD: {head[:8]})", file=sys.stderr)

    try:
        print(f"📊 Scanning files in {repo_dir}...", file=sys.stderr)
        infos = collect_files(repo_dir, args.max_bytes)
        rendered_count = sum(1 for i in infos if i.decision.include)
        skipped_count = len(infos) - rendered_count
        print(f"✓ Found {len(infos)} files total ({rendered_count} will be rendered, {skipped_count} skipped)", file=sys.stderr)

        if args.minify:
            print("🗜 Minification enabled for LLM content", file=sys.stderr)
        out_path = pathlib.Path(args.out)
        if args.llm:
            print("🔨 Generating LLM text...", file=sys.stderr)
            cxml_text = generate_cxml_text(infos, repo_dir, minify=args.minify)
            print(f"💾 Writing text file: {out_path.resolve()}", file=sys.stderr)
            out_path.write_text(cxml_text, encoding="utf-8")
            file_size = out_path.stat().st_size
            print(f"✓ Wrote {bytes_human(file_size)} to {out_path}", file=sys.stderr)
        else:
            print("🔨 Generating HTML...", file=sys.stderr)
            html_out = build_html(repo_url, repo_dir, head, infos, minify=args.minify)
            print(f"💾 Writing HTML file: {out_path.resolve()}", file=sys.stderr)
            out_path.write_text(html_out, encoding="utf-8")
            file_size = out_path.stat().st_size
            print(f"✓ Wrote {bytes_human(file_size)} to {out_path}", file=sys.stderr)

            if not args.no_open:
                print(f"🌐 Opening {out_path} in browser...", file=sys.stderr)
                webbrowser.open(f"file://{out_path.resolve()}")

        return 0
    finally:
        if tmpdir:
            print(f"🗑️  Cleaning up temporary directory: {tmpdir}", file=sys.stderr)
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

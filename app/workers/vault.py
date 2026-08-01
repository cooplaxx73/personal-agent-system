"""Full read/write access to the Obsidian vault.

Replaces the old obsidian_writer read path, which could only touch dated daily
notes inside three hard-coded folders (Deadlines/Jobs/Emails) under
"Personal Agent System" -- it literally could not read _Home.md or anything the
user wrote themselves.

Everything here is plain file I/O against OBSIDIAN_VAULT_PATH. Syncthing carries
changes to the PC on its own, so writes land on the desktop within seconds (or
whenever the PC next reconnects).

Safety rules that matter:
- every path is resolved and checked to be INSIDE the vault, so "../../etc" and
  absolute paths can't escape;
- writes are .md only, so the agent can't drop binaries or clobber .obsidian
  config;
- dot-folders (.obsidian, .stfolder, .trash) are skipped by search/list -- they
  are machine state, not notes, and .stfolder churn would drown real results;
- read/search output is capped, because this text goes straight into an LLM
  prompt and an unbounded vault would blow the context window.
"""
import os
from datetime import datetime
from pathlib import Path

VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", "")
AGENT_FOLDER = "Personal Agent System"      # default home for bot-filed notes

SKIP_DIRS = {".obsidian", ".stfolder", ".trash", ".git", "node_modules"}
MAX_WRITE_BYTES = 200_000
MAX_READ_CHARS = 20_000
MAX_HITS = 40
CONTEXT_CHARS = 160


def _root() -> Path:
    if not VAULT_PATH:
        raise RuntimeError("OBSIDIAN_VAULT_PATH not set")
    return Path(VAULT_PATH).resolve()


def _resolve(rel: str, for_write: bool = False) -> Path:
    """Resolve a vault-relative path, refusing anything that escapes the vault."""
    root = _root()
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        raise ValueError("path is required")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError("path escapes the vault")
    if for_write:
        # Be forgiving about the extension: a trailing-slash/bare name is what
        # models actually produce. Anything with a DIFFERENT extension is still
        # refused, so the agent can't drop scripts or clobber .obsidian config.
        if p.suffix == "":
            p = p.with_suffix(".md") if p.name else p / "note.md"
        elif p.suffix.lower() != ".md":
            raise ValueError("only .md files can be written")
    return p


def _walk():
    """Every markdown note in the vault, skipping machine-state folders."""
    root = _root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                yield Path(dirpath) / fn


def _rel(p: Path) -> str:
    return str(p.relative_to(_root())).replace("\\", "/")


def search(query: str, limit: int = 20, folder: str = "") -> list[dict]:
    """Case-insensitive keyword search across the whole vault.

    Returns a matching line plus surrounding context per hit rather than whole
    files, so the agent gets enough to answer without eating the context window.
    A filename match counts too -- "find my calculus notes" should hit
    Calculus.md even if the word never appears in the body.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    limit = max(1, min(int(limit or 20), MAX_HITS))
    scope = _resolve(folder) if folder else _root()

    hits = []
    for path in _walk():
        if scope != _root() and scope not in path.parents and path != scope:
            continue
        rel = _rel(path)
        name_hit = q in path.name.lower()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not name_hit and q not in text.lower():
            continue

        snippets = []
        for i, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                s = line.strip()
                snippets.append({"line": i, "text": s[:CONTEXT_CHARS]})
                if len(snippets) >= 3:
                    break
        if not snippets and name_hit:
            snippets = [{"line": 1, "text": text.strip()[:CONTEXT_CHARS]}]

        hits.append({
            "path": rel,
            "matched_filename": name_hit,
            "matches": snippets,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
        if len(hits) >= limit:
            break

    # filename matches first, then most recently edited -- both are better
    # signals of "the note you meant" than filesystem walk order
    hits.sort(key=lambda h: (not h["matched_filename"], h["modified"]), reverse=False)
    return hits


def read_note(path: str) -> dict:
    p = _resolve(path)
    if not p.exists() or not p.is_file():
        return {"error": f"no note at '{path}'", "path": path, "content": ""}
    text = p.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > MAX_READ_CHARS
    return {"path": _rel(p), "content": text[:MAX_READ_CHARS], "truncated": truncated,
            "chars": len(text)}


def write_note(path: str, content: str, mode: str = "append", title: str = "") -> dict:
    """mode: append (default) | create (fail if present) | overwrite.

    Append is the default deliberately -- it is the only non-destructive option,
    so a confused agent adds to a note rather than erasing one.
    """
    mode = (mode or "append").lower()
    if mode not in ("append", "create", "overwrite"):
        return {"error": "mode must be append, create or overwrite"}
    content = content or ""
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return {"error": "content too large"}

    p = _resolve(path, for_write=True)
    if mode == "create" and p.exists():
        return {"error": f"'{_rel(p)}' already exists -- use append or overwrite"}

    p.parent.mkdir(parents=True, exist_ok=True)
    body = content if not title else f"## {title}\n\n{content}\n"
    if mode == "append":
        prefix = "" if not p.exists() or p.read_text(encoding="utf-8").endswith("\n") else "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(prefix + body.rstrip("\n") + "\n")
    else:
        p.write_text(body.rstrip("\n") + "\n", encoding="utf-8")

    return {"path": _rel(p), "mode": mode, "bytes": p.stat().st_size,
            "note": "saved to the vault; it syncs to your PC automatically"}


def list_notes(folder: str = "", limit: int = 60) -> dict:
    """Notes in a folder (or the whole vault), most recently edited first."""
    scope = _resolve(folder) if folder else _root()
    limit = max(1, min(int(limit or 60), 200))
    out = []
    for p in _walk():
        if scope != _root() and scope not in p.parents and p != scope:
            continue
        st = p.stat()
        out.append({"path": _rel(p), "kb": round(st.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    out.sort(key=lambda x: x["modified"], reverse=True)
    return {"folder": folder or "/", "count": len(out), "notes": out[:limit]}

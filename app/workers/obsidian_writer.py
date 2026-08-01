"""Write markdown notes into an Obsidian vault -- purely file writes, no
API or auth needed. Set OBSIDIAN_VAULT_PATH (env var) to your real vault
folder once you know it; this is the only thing missing here.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", "")


def _vault_root() -> Path:
    if not VAULT_PATH:
        raise RuntimeError("OBSIDIAN_VAULT_PATH not set -- provide your vault's folder path")
    root = Path(VAULT_PATH) / "Personal Agent System"
    root.mkdir(parents=True, exist_ok=True)
    return root


def append_note(section: str, title: str, content: str) -> str:
    """Append an entry to today's note under <vault>/Personal Agent System/<section>/."""
    folder = _vault_root() / section
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    note_path = folder / f"{today}.md"

    entry = f"## {title}\n\n{content}\n\n"
    with open(note_path, "a", encoding="utf-8") as f:
        f.write(entry)

    return str(note_path)


def read_recent_notes(section: str, days_back: int = 14) -> str:
    """Read and concatenate recent daily notes from a vault section,
    returning raw markdown for the agent to reason over -- this is the
    'memory' the agent draws from, not a fresh live query."""
    folder = _vault_root() / section
    if not folder.exists():
        return ""

    now = datetime.now()
    combined = []
    for i in range(days_back):
        day = now - timedelta(days=i)
        note_path = folder / f"{day.strftime('%Y-%m-%d')}.md"
        if note_path.exists():
            combined.append(f"# {day.strftime('%Y-%m-%d')}\n" + note_path.read_text(encoding="utf-8"))

    return "\n\n".join(combined)


if __name__ == "__main__":
    # quick self-test using a throwaway folder, not your real vault
    import tempfile
    VAULT_PATH = tempfile.mkdtemp()

    path = append_note("Jobs", "Test Entry", "This is a test note to verify the writer works.")
    print(f"Wrote to: {path}")
    print("--- contents ---")
    print(Path(path).read_text(encoding="utf-8"))

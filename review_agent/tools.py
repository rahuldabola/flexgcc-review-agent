"""Read-only tools the agent can call to pull context beyond the diff.

All paths are confined to the repository root and every output is size-capped,
so a confused (or prompt-injected) model cannot read outside the repo or blow
up the context window.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .checks import is_test_path, redact_secrets
from .diff import FileDiff

MAX_CHARS = 6000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "bin", "obj", ".next"}
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rb", ".rs", ".kt", ".php", ".sql",
            ".json", ".yaml", ".yml", ".toml", ".md", ".cfg", ".ini", ".html", ".css", ".csproj", ".txt"}


class ToolError(Exception):
    pass


class RepoTools:
    def __init__(self, files: list[FileDiff], repo: str | Path | None = None):
        self.files = {f.path: f for f in files}
        self.repo = Path(repo).resolve() if repo else None

    # ---- declarations exposed to the model -------------------------------
    @staticmethod
    def declarations() -> list[dict]:
        return [
            {
                "name": "get_file_diff",
                "description": "Return the diff for one changed file with new-file line numbers on the left. Use this for every file you comment on.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            },
            {
                "name": "read_file",
                "description": "Read lines of a file from the repository AFTER the change (context such as callers, class definitions, config). Max 200 lines per call.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "search_code",
                "description": "Regex search across the repository (e.g. find callers of a changed function). Returns up to 40 'path:line: text' matches.",
                "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
            },
            {
                "name": "find_tests",
                "description": "List test files in the repository that mention a module/function name, to judge whether a change is covered.",
                "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            },
        ]

    def call(self, name: str, args: dict) -> str:
        fn = {"get_file_diff": self.get_file_diff, "read_file": self.read_file,
              "search_code": self.search_code, "find_tests": self.find_tests}.get(name)
        if fn is None:
            raise ToolError(f"Unknown tool '{name}'. Available: get_file_diff, read_file, search_code, find_tests")
        try:
            out = fn(**args)
        except TypeError as e:
            raise ToolError(f"Bad arguments for {name}: {e}") from e
        out, _ = redact_secrets(out)
        if len(out) > MAX_CHARS:
            out = out[:MAX_CHARS] + f"\n... [truncated, {len(out) - MAX_CHARS} more chars]"
        return out

    # ---- implementations --------------------------------------------------
    def get_file_diff(self, path: str) -> str:
        f = self.files.get(path) or next((v for k, v in self.files.items() if k.endswith(path)), None)
        if f is None:
            raise ToolError(f"'{path}' is not in this diff. Changed files: {', '.join(self.files)}")
        if f.binary:
            return f"{f.path}: binary file changed"
        return f"[{f.status}] " + f.render()

    def _resolve(self, path: str) -> Path:
        if self.repo is None:
            raise ToolError("No repository checkout was provided, only the diff is available. Use get_file_diff.")
        p = (self.repo / path).resolve()
        if p != self.repo and self.repo not in p.parents:
            raise ToolError("Path escapes the repository root; refused.")
        if not p.is_file():
            raise ToolError(f"File not found in repo: {path}")
        return p

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self._resolve(path)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + 199, start + 199)
        body = "\n".join(f"{i:5d}  {lines[i - 1]}" for i in range(start, end + 1))
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}"

    def _walk(self):
        if self.repo is None:
            raise ToolError("No repository checkout was provided, only the diff is available.")
        for root, dirs, names in os.walk(self.repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                p = Path(root) / n
                if p.suffix.lower() in TEXT_EXT and p.stat().st_size < 500_000:
                    yield p

    def search_code(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Invalid regex: {e}") from e
        hits: list[str] = []
        for p in self._walk():
            rel = p.relative_to(self.repo).as_posix()
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 40:
                        return "\n".join(hits) + "\n... (more matches omitted)"
        return "\n".join(hits) or "No matches."

    def find_tests(self, name: str) -> str:
        found = []
        for p in self._walk():
            rel = p.relative_to(self.repo).as_posix()
            if is_test_path(rel) and name in p.read_text(encoding="utf-8", errors="replace"):
                found.append(rel)
        return "\n".join(found) or f"No test files reference '{name}'."

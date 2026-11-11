"""Load and parse unified diffs (from a file, a local git repo, or a GitHub PR)."""
from __future__ import annotations

import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


@dataclass
class Hunk:
    header: str
    new_start: int
    lines: list[str] = field(default_factory=list)  # raw lines incl. +/-/space prefix
    added: dict[int, str] = field(default_factory=dict)  # new-file line no -> text


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added | deleted | modified | renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def added_lines(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for h in self.hunks:
            out.update(h.added)
        return out

    @property
    def changed_line_numbers(self) -> set[int]:
        """New-file line numbers that appear in any hunk (added or context)."""
        nums: set[int] = set()
        for h in self.hunks:
            n = h.new_start
            for raw in h.lines:
                if raw.startswith("-"):
                    continue
                nums.add(n)
                n += 1
        return nums

    def render(self, max_lines: int = 400) -> str:
        out = [f"--- {self.old_path or '/dev/null'}", f"+++ {self.path}"]
        for h in self.hunks:
            out.append(h.header)
            n = h.new_start
            for raw in h.lines:
                if raw.startswith("-"):
                    out.append(f"      {raw}")
                else:
                    out.append(f"{n:5d} {raw}")
                    n += 1
        if len(out) > max_lines:
            out = out[:max_lines] + [f"... ({len(out) - max_lines} more lines truncated)"]
        return "\n".join(out)


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a unified diff. Hunk bodies are consumed by their declared line counts,
    so content lines that happen to start with '---'/'+++' are handled correctly."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_left = new_left = 0
    new_no = 0
    for line in text.splitlines():
        if hunk is not None and (old_left > 0 or new_left > 0):
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            tag = line[:1] if line else " "
            hunk.lines.append(line if line else " ")
            if tag == "+":
                hunk.added[new_no] = line[1:]
                new_no += 1
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            else:
                new_no += 1
                new_left -= 1
                old_left -= 1
            continue
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = FileDiff(path=m.group(2) if m else "", old_path=m.group(1) if m else None, status="modified")
            files.append(cur)
            hunk = None
            continue
        if line.startswith("--- ") and (cur is None or cur.hunks):
            # plain `diff -u` output without "diff --git" headers
            cur = FileDiff(path="", old_path=None, status="modified")
            files.append(cur)
            hunk = None
        if cur is None:
            continue
        m = HUNK_RE.match(line)
        if m:
            hunk = Hunk(header=line, new_start=int(m.group(3)))
            cur.hunks.append(hunk)
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            new_no = hunk.new_start
        elif line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.status, cur.old_path = "renamed", line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.path = line[len("rename to "):]
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur.binary = True
        elif line.startswith("--- "):
            p = _strip_prefix(line[4:])
            if p == "/dev/null":
                cur.old_path, cur.status = None, "added"
            else:
                cur.old_path = p
        elif line.startswith("+++ "):
            p = _strip_prefix(line[4:])
            if p == "/dev/null":
                cur.status = "deleted"
            else:
                cur.path = p
    for f in files:
        if not f.path:
            f.path = f.old_path or "unknown"
    return files


def _strip_prefix(p: str) -> str:
    p = p.split("\t")[0].strip()
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def load_diff_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def load_git_diff(repo: str | Path, base: str = "HEAD~1", head: str | None = None) -> str:
    rng = [f"{base}...{head}"] if head else [base]
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", "--unified=3", *rng],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return out.stdout


def load_pr_diff(url: str, timeout: int = 30) -> str:
    """Fetch a public GitHub PR as a unified diff (no auth needed for public repos)."""
    m = PR_RE.search(url)
    if not m:
        raise ValueError(f"Not a GitHub PR URL: {url}")
    owner, repo, num = m.groups()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}",
        headers={"Accept": "application/vnd.github.v3.diff", "User-Agent": "review-agent"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
        return r.read().decode("utf-8", errors="replace")

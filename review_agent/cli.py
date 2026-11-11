"""Command line entry point.

Examples:
  python -m review_agent --diff samples/buggy_change.diff --repo samples/demo_repo
  python -m review_agent --git . --base main
  python -m review_agent --pr https://github.com/owner/repo/pull/123
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .agent import review_diff
from .diff import load_diff_file, load_git_diff, load_pr_diff
from .schema import ReviewResult

ICON = {"bug": "BUG", "risk": "RISK", "missing_test": "TEST", "style": "STYLE"}


def to_markdown(res: ReviewResult) -> str:
    r = res.review
    out = [f"# Code review: {r.verdict.replace('_', ' ').upper()}", "", r.summary, ""]
    counts = {c: sum(f.category == c for f in r.findings) for c in ICON}
    out.append(" | ".join(f"{ICON[c]}: {n}" for c, n in counts.items()))
    out.append("")
    for f in r.findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        out.append(f"## [{ICON[f.category]}][{f.severity}] {f.title}")
        out.append(f"`{loc}` - confidence {f.confidence:.1f} - {f.source}")
        out.append("")
        out.append(f.explanation)
        if f.suggestion:
            out.append(f"\n**Suggestion:** {f.suggestion}")
        out.append("")
    out.append("---")
    out.append(f"Model: {res.model} | tool calls: {len(res.tool_calls)} | files: {len(res.files_reviewed)}"
               f" | secrets redacted before LLM: {res.redactions}")
    if res.tool_calls:
        out.append("\nAgent trace:\n" + "\n".join(f"- {c}" for c in res.tool_calls))
    if res.dropped_findings:
        out.append("\nDropped by validation:\n" + "\n".join(f"- {d}" for d in res.dropped_findings))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="review_agent", description="AI code-review agent for diffs / PRs")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff", help="path to a unified diff / .patch file")
    src.add_argument("--git", metavar="REPO", help="review `git diff BASE[...HEAD]` in a local repo")
    src.add_argument("--pr", help="public GitHub pull request URL")
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head")
    ap.add_argument("--repo", help="repo checkout used for context tools (defaults to --git path)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    ap.add_argument("--out", help="also write the output to this file")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if a.diff:
        diff = load_diff_file(a.diff)
    elif a.git:
        diff = load_git_diff(a.git, a.base, a.head)
    else:
        diff = load_pr_diff(a.pr)
    repo = a.repo or a.git
    if not diff.strip():
        print("Empty diff - nothing to review.", file=sys.stderr)
        return 1

    from .llm_gemini import GeminiLLM  # imported late so tests/offline use don't need the SDK
    res = review_diff(diff, GeminiLLM(), repo=repo, max_steps=a.max_steps)
    text = res.model_dump_json(indent=2) if a.json else to_markdown(res)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return 2 if res.review.verdict == "request_changes" else 0


if __name__ == "__main__":
    raise SystemExit(main())

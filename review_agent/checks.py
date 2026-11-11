"""Cheap deterministic checks that run before the LLM.

They catch things a model sometimes misses (and never hallucinates), and the
secret scanner also redacts values so they are never sent to the model.
"""
from __future__ import annotations

import re

from .diff import FileDiff
from .schema import Finding

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub token
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI/Anthropic style
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe-style keys
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(
        r"""(?i)\b\w*(password|passwd|secret|api_?key|token)\w*\s*[:=]\s*["']([^"'\s]{6,})["']"""
    ),
]

TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(_test|\.test|\.spec|Tests?)\.\w+$|(^|/)test_[^/]+$")
CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rb", ".kt", ".rs", ".php")

LINE_RULES = [
    (re.compile(r"^\s*except\s*:\s*(#.*)?$"), "risk", "medium", "Bare `except:` swallows every error",
     "Catch the specific exception and log or re-raise it.", (".py",)),
    (re.compile(r"\b(eval|exec)\s*\("), "risk", "high", "Use of eval/exec on dynamic input",
     "Avoid eval/exec; parse the input explicitly (e.g. json.loads / ast.literal_eval).", (".py", ".js", ".ts")),
    (re.compile(r"\bconsole\.log\(|^\s*print\("), "style", "low", "Debug output left in code",
     "Remove it or use the project's logger.", (".js", ".ts", ".tsx", ".jsx", ".py")),
    (re.compile(r"\b(TODO|FIXME|HACK)\b"), "style", "low", "Unresolved TODO/FIXME added",
     "Resolve it or link a tracking issue.", None),
    (re.compile(r"verify\s*=\s*False"), "risk", "high", "TLS certificate verification disabled",
     "Keep verify=True; configure a CA bundle if needed.", (".py",)),
    (re.compile(r"""(execute|query)\(\s*f["']|(execute|query)\([^)]*["']\s*(\+|%)\s*"""), "bug", "high",
     "SQL built with string formatting (injection risk)", "Use parameterised queries.", None),
]


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pat in SECRET_PATTERNS:
        def _sub(m: re.Match) -> str:
            nonlocal count
            if "[REDACTED]" in m.group(0):
                return m.group(0)
            count += 1
            if m.lastindex and m.lastindex >= 2:
                return m.group(0).replace(m.group(2), "[REDACTED]")
            return "[REDACTED]"
        text = pat.sub(_sub, text)
    return text, count


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def static_findings(files: list[FileDiff]) -> list[Finding]:
    out: list[Finding] = []
    for f in files:
        if f.binary or f.status == "deleted":
            continue
        for no, text in f.added_lines.items():
            for pat in SECRET_PATTERNS:
                if pat.search(text):
                    out.append(Finding(category="risk", severity="high", file=f.path, line=no,
                                       title="Possible hard-coded secret",
                                       explanation="An added line matches a credential pattern. The value was redacted before being sent to the model.",
                                       suggestion="Load it from an environment variable or secret manager and rotate the exposed value.",
                                       confidence=0.9, source="static"))
                    break
            for pat, cat, sev, title, fix, exts in LINE_RULES:
                if exts and not f.path.endswith(exts):
                    continue
                if pat.search(text):
                    out.append(Finding(category=cat, severity=sev, file=f.path, line=no, title=title,
                                       explanation=f"Added line: `{text.strip()[:120]}`", suggestion=fix,
                                       confidence=0.8, source="static"))
    # Missing tests: code changed but no test file touched
    code = [f for f in files if f.path.endswith(CODE_EXT) and not is_test_path(f.path) and f.status != "deleted" and f.added_lines]
    tests = [f for f in files if is_test_path(f.path)]
    if code and not tests:
        for f in code:
            out.append(Finding(category="missing_test", severity="medium", file=f.path, line=None,
                               title="Code changed without any test changes",
                               explanation=f"{len(f.added_lines)} lines added in {f.path} but no test file is part of this change.",
                               suggestion="Add or update tests covering the new behaviour.",
                               confidence=0.6, source="static"))
    return out

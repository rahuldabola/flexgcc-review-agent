from pathlib import Path

import pytest

from review_agent.agent import Step, ToolCall, review_diff
from review_agent.checks import redact_secrets, static_findings
from review_agent.diff import parse_diff
from review_agent.tools import RepoTools, ToolError

ROOT = Path(__file__).resolve().parents[1]
DIFF = (ROOT / "samples" / "buggy_change.diff").read_text(encoding="utf-8")
REPO = ROOT / "samples" / "demo_repo"


class FakeLLM:
    """Replays a scripted list of steps and records what the agent sent back."""

    model = "fake"

    def __init__(self, steps):
        self.steps = list(steps)
        self.sent = []

    def _next(self):
        return self.steps.pop(0) if self.steps else Step()

    def start(self, system, prompt, tools):
        self.prompt, self.tool_names = prompt, [t["name"] for t in tools]
        return self._next()

    def reply(self, results):
        self.sent.append(results)
        return self._next()

    def say(self, text):
        self.sent.append(text)
        return self._next()


def submit(findings, verdict="comment"):
    return Step(calls=[ToolCall("submit_review", {"summary": "s", "verdict": verdict, "findings": findings})])


# ---- diff parsing ----------------------------------------------------------
def test_parse_line_numbers():
    files = {f.path: f for f in parse_diff(DIFF)}
    assert set(files) == {"shop/orders.py", "shop/pricing.py"}
    pricing = files["shop/pricing.py"]
    assert pricing.added_lines[30] == "        total = apply_discount(total, 0.05)"
    assert 29 in pricing.changed_line_numbers


def test_parse_new_file_and_dashes_in_content():
    d = (
        "diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n"
        "@@ -0,0 +1,3 @@\n+a = 1\n+--- not a header\n++++ also content\n"
    )
    (f,) = parse_diff(d)
    assert f.status == "added" and f.path == "x.py"
    assert f.added_lines == {1: "a = 1", 2: "--- not a header", 3: "+++ also content"}


# ---- static checks / redaction ----------------------------------------------
def test_static_checks_find_known_issues():
    titles = {(f.line, f.title) for f in static_findings(parse_diff(DIFF))}
    assert (8, "Possible hard-coded secret") in titles
    assert (17, "Bare `except:` swallows every error") in titles
    assert any("SQL" in t for _, t in titles)


def test_secret_redacted_from_prompt():
    llm = FakeLLM([submit([])])
    review_diff(DIFF, llm, repo=str(REPO))
    assert "demo-not-a-real-key-9f3k2" not in llm.prompt
    assert "[REDACTED]" in llm.prompt
    assert redact_secrets('token = "abcdef123"')[1] == 1


# ---- tools ------------------------------------------------------------------
def test_tools_are_sandboxed():
    t = RepoTools(parse_diff(DIFF), REPO)
    assert "def apply_discount" in t.call("read_file", {"path": "shop/pricing.py"})
    with pytest.raises(ToolError):
        t.call("read_file", {"path": "../../README.md"})
    with pytest.raises(ToolError):
        t.call("nope", {})
    assert "tests/test_pricing.py" in t.call("find_tests", {"name": "order_total"})


def test_tools_without_repo_explain_themselves():
    t = RepoTools(parse_diff(DIFF), None)
    with pytest.raises(ToolError, match="No repository checkout"):
        t.call("search_code", {"pattern": "x"})


# ---- agent loop ---------------------------------------------------------------
def test_agent_runs_tools_then_validates_findings():
    bug = {"category": "bug", "severity": "high", "file": "pricing.py", "line": 30,
           "title": "Bulk discount uses 0.05 instead of 5", "explanation": "percent is 0-100",
           "confidence": 0.9}
    ghost = {"category": "bug", "severity": "low", "file": "not_in_diff.py", "line": 1,
             "title": "ghost", "explanation": "x"}
    llm = FakeLLM([
        Step(calls=[ToolCall("read_file", {"path": "shop/pricing.py", "start_line": 1, "end_line": 10})]),
        submit([bug, ghost], verdict="approve"),
    ])
    res = review_diff(DIFF, llm, repo=str(REPO))
    assert "read_file" in res.tool_calls[0]
    titles = [f.title for f in res.review.findings]
    assert "Bulk discount uses 0.05 instead of 5" in titles
    assert all(f.file != "not_in_diff.py" for f in res.review.findings)
    assert res.dropped_findings and "not part of the diff" in res.dropped_findings[0]
    # file path was normalised and a high bug forces request_changes even though model said approve
    assert any(f.file == "shop/pricing.py" and f.line == 30 for f in res.review.findings)
    assert res.review.verdict == "request_changes"


def test_invalid_submit_gets_error_and_retry():
    bad = Step(calls=[ToolCall("submit_review", {"summary": "s", "verdict": "maybe", "findings": []})])
    llm = FakeLLM([bad, submit([])])
    res = review_diff(DIFF, llm, repo=str(REPO))
    assert "error" in llm.sent[0][0][1]
    assert res.review.summary == "s"


def test_model_never_submits_falls_back_to_static():
    llm = FakeLLM([Step(text="I think it's fine")] * 20)
    res = review_diff(DIFF, llm, repo=str(REPO), max_steps=4)
    assert "did not return a valid review" in res.review.summary
    assert any(f.source == "static" for f in res.review.findings)

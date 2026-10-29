"""Security guards, enforced in code (they don't depend on the model obeying a prompt).

1. sanitize_notes   - meeting notes are untrusted input: hidden text and instruction-like lines are removed
                      before the model sees them.
2. find_secrets     - passwords, tokens, keys, national IDs, phone numbers.
3. check_draft      - before a draft is queued: assignee must be on the team, no secrets, no @mentions of
                      anyone outside the team.
The GitLab MCP server repeats the assignee / label / mention checks on its side (defence in depth).
"""
from __future__ import annotations

import html
import json
import re

from . import config

ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿]")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
HIDDEN = re.compile(r"<(\w+)[^>]*(?:display\s*:\s*none|font-size\s*:\s*0|color\s*:\s*(?:#fff(?:fff)?|white))[^>]*>.*?</\1>",
                    re.S | re.I)
TAG = re.compile(r"<[^>]+>")
INSTRUCTION = re.compile(
    r"(?i)(\b(note|message|instruction)s?\s+(to|for)\s+(the\s+)?(ai|assistant|agent|bot|model)\b|"
    r"^\s*(ai|assistant|agent|system)\s*:|ignore (all |any |the |your )?(previous |above )?(rules|instructions)|"
    r"you are (an? )?(ai|assistant)|"
    r"تجاهل (التعليمات|القواعد)|(?:^|\s)(ملاحظة|رسالة) (لل)?(مساعد|ذكاء))"
)

SECRET_PATTERNS = {
    "gitlab token": re.compile(r"\bglpat-[A-Za-z0-9_\-]{12,}"),
    "api key": re.compile(r"\b(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})\b"),
    "password": re.compile(r"(?i)\b(password|passwd|pwd|كلمة (السر|المرور))\b\s*(is|=|:|هي)?\s*\S{4,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "national id": re.compile(r"(?<!\d)[12]\d{9}(?!\d)"),
    "phone number": re.compile(r"(?<!\d)(?:\+?966|0)5\d{8}(?!\d)"),
}


def sanitize_notes(raw: str) -> tuple[str, list[str]]:
    """Return (clean notes, list of what was removed)."""
    removed = []
    text = ZERO_WIDTH.sub("", raw)
    for pat, what in ((HTML_COMMENT, "HTML comment"), (HIDDEN, "hidden HTML text")):
        for m in pat.findall(text):
            removed.append(what)
        text = pat.sub("", text)
    text = html.unescape(TAG.sub("\n", text))
    kept = []
    for line in text.splitlines():
        if INSTRUCTION.search(line):
            removed.append(f"instruction-like line: {line.strip()[:120]}")
            continue
        kept.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    # data minimisation: secrets and personal data never reach the model provider
    for name, pat in SECRET_PATTERNS.items():
        if pat.search(text):
            removed.append(f"{name} redacted")
            text = pat.sub(f"[{name} removed]", text)
    if len(text) > config.MAX_NOTES_CHARS:
        text = text[: config.MAX_NOTES_CHARS]
        removed.append(f"notes cut to {config.MAX_NOTES_CHARS} characters")
    return text, removed


def plain_notes(raw: str) -> str:
    """What an unguarded agent sees: tags dropped, hidden text and comments kept."""
    text = HTML_COMMENT.sub(lambda m: m.group(0)[4:-3], raw)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(TAG.sub("\n", text))).strip()[: config.MAX_NOTES_CHARS]


def find_secrets(text: str) -> list[str]:
    return [name for name, pat in SECRET_PATTERNS.items() if pat.search(text or "")]


TEAM = json.loads(config.TEAM_FILE.read_text(encoding="utf-8"))
MEMBERS = {m["username"] for m in TEAM["members"]}
MENTION = re.compile(r"(?<![\w.])@([\w.\-]+)")


def check_draft(issue: dict) -> str | None:
    """Policy for create_issue. Returns an error message for the model, or None if allowed."""
    if issue.get("assignee") not in MEMBERS:
        return f"blocked by policy: assignee {issue.get('assignee')!r} is not on the team"
    text = " ".join([str(issue.get("title", "")), str(issue.get("description", "")),
                     " ".join(issue.get("acceptance_criteria") or [])])
    outsiders = [u for u in MENTION.findall(text) if u not in MEMBERS]
    if outsiders:
        return f"blocked by policy: mentions people outside the team: {outsiders}"
    secrets = find_secrets(text)
    if secrets:
        return f"blocked by policy: the issue contains sensitive data ({', '.join(secrets)}); remove it"
    return None

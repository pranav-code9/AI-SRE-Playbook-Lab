"""Telemetry is untrusted input.

Log lines, trace attributes, release descriptions and configuration values are
written by anyone who can influence the systems under investigation, and some
of them carry secrets or personal data. Everything that reaches a prompt from
the outside world passes through here first:

- `redact` removes secret-looking values and common personal data;
- `untrusted` also flattens control characters and caps length, so a hostile
  string can't break out of its place in a summary;
- `is_secret_key` decides which configuration keys never show their values.

This is defence in depth, not the main defence. The main defence is that the
agent's tools are read-only and every action passes through the policy gate
(Chapter 5), so an injected instruction has nothing to act with.
"""

from __future__ import annotations

import hashlib
import re

SECRET_KEY = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth|cert|dsn|conn(ection)?[_-]?string)", re.I)

_PATTERNS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{5,}\b"), "[redacted-jwt]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted-aws-key]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[redacted-private-key]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"), r"\1=[redacted]"),
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@"), "[redacted-credentials]@"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[number]"),
]
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def fingerprint(value: object) -> str:
    """A short, stable stand-in for a hidden value, so two different secrets
    still compare as different (and change a plan's hash) without being shown."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:8]


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY.search(key))


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def untrusted(text: object, limit: int = 160) -> str:
    """Make outside text safe to place inside a summary: redacted, one line,
    capped, and quoted so it reads as data rather than as part of the sentence."""
    s = _CONTROL.sub(" ", str(text)).replace("\n", " ").replace("\r", " ")
    s = redact(s).strip()
    if len(s) > limit:
        s = s[: limit - 1] + "\u2026"
    return s


def config_value(key: str, value: object) -> object:
    """Values of secret-looking configuration keys are replaced by a fingerprint."""
    if value is None or not is_secret_key(key):
        return value if not isinstance(value, str) else redact(value)
    return f"[redacted:{fingerprint(value)}]"

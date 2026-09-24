"""Bounded, best-effort redaction for explicitly enabled evidence capture.

Not a comprehensive secret/PII detector. Payloads never change on the tool path.
"""

import json
import os
import re

SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key|private.?key|credential", re.I)
PATTERNS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)"),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=~-]+", re.I),
    re.compile(r"\b(?:glc_|glsa_|gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_=-]{8,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key)[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)"),
    re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@"),
)


class Capture:
    def __init__(self, max_chars=8192):
        self.max_chars = max_chars
        # Match known credentials without ever including their values in diagnostics.
        self.secrets = sorted({v for k, v in os.environ.items()
                               if SENSITIVE.search(k) and 8 <= len(v) <= 65536}, key=len, reverse=True)

    def text(self, value):
        if len(value) > 65536:
            return "[OMITTED: oversized text]"
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        for pattern in PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value

    def encode(self, value):
        remaining = 500
        truncated = False

        def clean(item, depth=0):
            nonlocal remaining, truncated
            remaining -= 1
            if remaining < 0 or depth > 8:
                truncated = True
                return "[OMITTED: capture limit]"
            if isinstance(item, dict):
                result = {}
                for i, (key, val) in enumerate(item.items()):
                    if i >= 64 or remaining < 0:
                        truncated = True
                        break
                    key = str(key)
                    if SENSITIVE.search(key):
                        result[self.text(key)[:128]] = "[REDACTED]"
                    elif key.lower() in {"data", "blob"}:
                        result[key] = "[OMITTED: opaque data]"
                    else:
                        result[self.text(key)[:128]] = clean(val, depth + 1)
                return result
            if isinstance(item, (list, tuple)):
                if len(item) > 64:
                    truncated = True
                return [clean(x, depth + 1) for x in item[:64] if remaining >= 0]
            if isinstance(item, str):
                redacted = self.text(item)
                if len(item) > 65536 or len(redacted) > self.max_chars:
                    truncated = True
                return redacted[:self.max_chars]
            if item is None or isinstance(item, (bool, int, float)):
                return item
            return "[OMITTED: unsupported value]"

        result = clean(value)
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > self.max_chars:
            # Never cut serialized JSON or redact only a prefix of a credential.
            return json.dumps({"omitted": "payload exceeds capture limit"}), True
        return encoded, truncated

    def event(self, span, name, value):
        try:
            encoded, truncated = self.encode(value)
            span.add_event(name, {"agent_monitor.content.json": encoded,
                                 "agent_monitor.content.truncated": truncated,
                                 "agent_monitor.redaction": "best_effort"})
        except Exception:
            # Observability must not turn a successful tool call into a failure.
            span.set_attribute("agent_monitor.capture_failed", True)

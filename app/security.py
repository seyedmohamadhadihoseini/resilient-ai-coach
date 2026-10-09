"""Prompt quarantine, injection defense, and canary token leak detection.

Guarantees defense-in-depth on the prompt layer:
1. Prompt Quarantine: Encloses untrusted user input within explicit boundary tags
   and escapes delimiter breakout attempts.
2. Canary Tokens: Injects a unique per-request secret token into the system instructions.
   If the model leaks internal instructions, the canary in the output triggers an
   immediate fail-closed security block.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger(__name__)


def generate_canary_token() -> str:
    """Generate a high-entropy per-request canary token."""
    return f"CANARY_{secrets.token_hex(8)}"


def quarantine_user_input(raw_text: str) -> str:
    """Sanitize and quarantine untrusted user text inside safe boundary delimiters.

    Escapes attempts to breakout of the <user_input> boundary tags.
    """
    sanitized = raw_text.replace("</user_input>", "&lt;/user_input&gt;")
    sanitized = sanitized.replace("<user_input>", "&lt;user_input&gt;")
    return f"<user_input>\n{sanitized}\n</user_input>"


def detect_canary_leak(canary_token: str, output_text: str) -> bool:
    """Checks whether the private canary token appeared anywhere in the model output."""
    if canary_token and canary_token in output_text:
        logger.critical(
            "SECURITY ALERT: Canary token leak detected! Model output leaked system instructions: token=%s",
            canary_token,
        )
        return True
    return False
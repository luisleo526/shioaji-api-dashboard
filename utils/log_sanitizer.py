"""
Log sanitization utilities to prevent sensitive data leakage.

This module provides functions to sanitize log output by masking
sensitive information like API keys, passwords, and tokens.
"""
import re
import logging
from typing import Any, Dict, Optional


# Patterns for sensitive data
SENSITIVE_PATTERNS = [
    # API keys (common formats)
    (r'(api[_-]?key|apikey)["\s:=]+["\']?([a-zA-Z0-9_-]{16,})["\']?', r'\1="***REDACTED***"'),
    # Secret keys
    (r'(secret[_-]?key|secretkey)["\s:=]+["\']?([a-zA-Z0-9_-]{16,})["\']?', r'\1="***REDACTED***"'),
    # Passwords
    (r'(password|passwd|pwd)["\s:=]+["\']?([^\s"\']+)["\']?', r'\1="***REDACTED***"'),
    # Tokens (Bearer, JWT, etc.)
    (r'(bearer|token|auth)["\s:=]+["\']?([a-zA-Z0-9._-]{20,})["\']?', r'\1="***REDACTED***"'),
    # CA password
    (r'(ca[_-]?password)["\s:=]+["\']?([^\s"\']+)["\']?', r'\1="***REDACTED***"'),
    # Master key
    (r'(master[_-]?key)["\s:=]+["\']?([^\s"\']+)["\']?', r'\1="***REDACTED***"'),
]

# Compiled patterns for performance
COMPILED_PATTERNS = [(re.compile(pattern, re.IGNORECASE), replacement)
                     for pattern, replacement in SENSITIVE_PATTERNS]


def sanitize_string(text: str) -> str:
    """
    Sanitize a string by replacing sensitive data with redacted markers.

    Args:
        text: The string to sanitize

    Returns:
        Sanitized string with sensitive data masked
    """
    if not text:
        return text

    result = text
    for pattern, replacement in COMPILED_PATTERNS:
        result = pattern.sub(replacement, result)

    return result


def sanitize_dict(data: Dict[str, Any], sensitive_keys: Optional[set] = None) -> Dict[str, Any]:
    """
    Sanitize a dictionary by masking values of sensitive keys.

    Args:
        data: Dictionary to sanitize
        sensitive_keys: Optional set of additional keys to treat as sensitive

    Returns:
        Sanitized dictionary with sensitive values masked
    """
    if sensitive_keys is None:
        sensitive_keys = set()

    default_sensitive = {
        'api_key', 'secret_key', 'password', 'token', 'auth_key',
        'ca_password', 'master_key', 'credential', 'secret',
        'authorization', 'bearer', 'apikey', 'secretkey',
    }

    all_sensitive = default_sensitive | sensitive_keys

    result = {}
    for key, value in data.items():
        key_lower = key.lower().replace('-', '_')

        if any(s in key_lower for s in all_sensitive):
            result[key] = '***REDACTED***'
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value, sensitive_keys)
        elif isinstance(value, str):
            result[key] = sanitize_string(value)
        else:
            result[key] = value

    return result


def mask_credential(value: str, visible_chars: int = 4) -> str:
    """
    Mask a credential showing only the first few characters.

    Args:
        value: The credential to mask
        visible_chars: Number of characters to show at the beginning

    Returns:
        Masked credential like "ABCD***"
    """
    if not value or len(value) <= visible_chars:
        return '***'

    return value[:visible_chars] + '***'


class SanitizedLogFormatter(logging.Formatter):
    """
    Custom log formatter that sanitizes sensitive data in log messages.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Sanitize the message
        if isinstance(record.msg, str):
            record.msg = sanitize_string(record.msg)

        # Sanitize args if present
        if record.args:
            if isinstance(record.args, dict):
                record.args = sanitize_dict(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    sanitize_string(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )

        return super().format(record)


def configure_sanitized_logging(level: int = logging.INFO) -> None:
    """
    Configure logging with sanitized output.

    This replaces the default logging formatter with one that
    automatically sanitizes sensitive data.

    Args:
        level: The logging level to use
    """
    formatter = SanitizedLogFormatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers and add sanitized one
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

"""Run the command-line LLM that polishes and digests transcripts."""

from __future__ import annotations

import subprocess


class LLMError(Exception):
    """The command is missing, timed out or exited with an error."""


def run(command: list[str], prompt: str, timeout: int) -> str:
    """Pipe ``prompt`` to ``command`` on stdin and return what it printed."""
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LLMError(f"command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"timed out after {exc.timeout}s") from exc
    if result.returncode != 0:
        raise LLMError(f"exited {result.returncode}: {result.stderr[-2000:]}")
    return result.stdout

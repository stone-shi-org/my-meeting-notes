"""Editable prompt files.

A prompt is a markdown file with YAML frontmatter and ``## SYSTEM`` / ``## USER``
sections. They live on disk so they can be tuned without a deploy, and every
summary records the exact text and sha256 that produced it -- so editing a
prompt never orphans the history of what was already generated.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.errors import NotFoundError, ValidationError
from app.logging_config import get_logger

log = get_logger("prompts")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^##\s+(SYSTEM|USER)\s*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass
class Prompt:
    name: str
    path: Path
    body: str
    meta: dict
    system: str
    user: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def version(self) -> str:
        return str(self.meta.get("version", "1"))

    @property
    def temperature(self) -> float | None:
        value = self.meta.get("temperature")
        return float(value) if value is not None else None

    @property
    def required_placeholders(self) -> list[str]:
        return list(self.meta.get("required_placeholders") or [])

    def render(self, values: dict[str, str]) -> tuple[str, str]:
        """Substitute ``{{name}}`` placeholders.

        Uses str.replace, never str.format or an f-string: transcripts are full
        of literal braces and would either raise or silently leak field names.
        """
        return substitute(self.system, values), substitute(self.user, values)


def substitute(text: str, values: dict[str, str]) -> str:
    out = text
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", "" if value is None else str(value))
    return out


def parse(name: str, path: Path, raw: str) -> Prompt:
    match = _FRONTMATTER.match(raw)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        body_after_meta = match.group(2)
    else:
        meta = {}
        body_after_meta = raw

    if not isinstance(meta, dict):
        raise ValidationError(f"Prompt {name!r} has malformed frontmatter")

    sections = _SECTION.split(body_after_meta)
    system, user = "", ""
    # split() yields [before, "SYSTEM", text, "USER", text, ...]
    for i in range(1, len(sections) - 1, 2):
        label, content = sections[i].strip().upper(), sections[i + 1].strip()
        if label == "SYSTEM":
            system = content
        elif label == "USER":
            user = content

    if not system and not user:
        # No section headers at all: treat the whole body as the user message.
        user = body_after_meta.strip()

    return Prompt(name=name, path=path, body=raw, meta=meta, system=system, user=user)


def load(name: str, directory: Path | None = None) -> Prompt:
    directory = directory or PROMPT_DIR
    path = (directory / f"{name}.md").resolve()

    # Refuse to escape the prompt directory via a crafted name.
    if not str(path).startswith(str(directory.resolve())):
        raise ValidationError(f"Invalid prompt name {name!r}")
    if not path.exists():
        raise NotFoundError(f"Prompt {name!r} not found")

    return parse(name, path, path.read_text(encoding="utf-8"))


def load_override(name: str, raw: str) -> Prompt:
    """Wrap ad-hoc prompt text for a one-off experiment."""
    return parse(name, PROMPT_DIR / f"{name}.md", raw)


def list_prompts(directory: Path | None = None) -> list[dict]:
    directory = directory or PROMPT_DIR
    if not directory.is_dir():
        return []

    out = []
    for path in sorted(directory.glob("*.md")):
        if path.name.endswith(".bak"):
            continue
        prompt = parse(path.stem, path, path.read_text(encoding="utf-8"))
        out.append(
            {
                "name": prompt.name,
                "version": prompt.version,
                "description": prompt.meta.get("description"),
                "sha256": prompt.sha256,
                "modified_at": path.stat().st_mtime,
                "required_placeholders": prompt.required_placeholders,
            }
        )
    return out


def save(name: str, raw: str, directory: Path | None = None) -> Prompt:
    """Overwrite a prompt file atomically, keeping one backup."""
    directory = directory or PROMPT_DIR
    existing = load(name, directory)

    candidate = parse(name, existing.path, raw)

    # A prompt that lost {{transcript}} would silently summarise nothing.
    required = candidate.required_placeholders or existing.required_placeholders
    present = set(_PLACEHOLDER.findall(raw))
    missing = [p for p in required if p not in present]
    if missing:
        raise ValidationError(
            f"Prompt is missing required placeholder(s): {', '.join('{{%s}}' % m for m in missing)}"
        )

    backup = existing.path.with_suffix(".md.bak")
    backup.write_text(existing.body, encoding="utf-8")

    tmp = existing.path.with_suffix(".md.tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(existing.path)

    log.info("prompt %s saved (sha %s)", name, candidate.sha256[:12])
    return load(name, directory)

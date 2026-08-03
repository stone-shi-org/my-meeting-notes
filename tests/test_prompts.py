"""Prompt file parsing, substitution and safe saving."""

from __future__ import annotations

import pytest

from app.errors import NotFoundError, ValidationError
from app.services import prompts as prompts_svc

SAMPLE = """---
name: test_prompt
version: 3
description: A test prompt
temperature: 0.4
required_placeholders: [transcript]
---

## SYSTEM

You are a helpful assistant.

## USER

Summarise this:
{{transcript}}
"""


@pytest.fixture
def prompt_dir(tmp_path):
    (tmp_path / "test_prompt.md").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


class TestParsing:
    def test_frontmatter_becomes_metadata(self, prompt_dir):
        p = prompts_svc.load("test_prompt", prompt_dir)
        assert p.version == "3"
        assert p.temperature == 0.4
        assert p.meta["description"] == "A test prompt"
        assert p.required_placeholders == ["transcript"]

    def test_sections_are_split(self, prompt_dir):
        p = prompts_svc.load("test_prompt", prompt_dir)
        assert p.system == "You are a helpful assistant."
        assert p.user.startswith("Summarise this:")
        assert "{{transcript}}" in p.user

    def test_a_file_with_no_frontmatter_still_loads(self, tmp_path):
        (tmp_path / "bare.md").write_text("## USER\n\nJust do it: {{x}}")
        p = prompts_svc.load("bare", tmp_path)
        assert p.version == "1"
        assert "Just do it" in p.user

    def test_a_file_with_no_sections_is_all_user(self, tmp_path):
        (tmp_path / "flat.md").write_text("Everything here is the user message.")
        p = prompts_svc.load("flat", tmp_path)
        assert p.system == ""
        assert p.user == "Everything here is the user message."

    def test_missing_file_is_404(self, prompt_dir):
        with pytest.raises(NotFoundError):
            prompts_svc.load("nope", prompt_dir)

    def test_path_traversal_is_refused(self, prompt_dir):
        with pytest.raises((ValidationError, NotFoundError)):
            prompts_svc.load("../../etc/passwd", prompt_dir)

    def test_sha256_is_stable_and_content_sensitive(self, prompt_dir):
        a = prompts_svc.load("test_prompt", prompt_dir)
        b = prompts_svc.load("test_prompt", prompt_dir)
        assert a.sha256 == b.sha256
        assert len(a.sha256) == 64

        (prompt_dir / "test_prompt.md").write_text(SAMPLE + "\nextra")
        c = prompts_svc.load("test_prompt", prompt_dir)
        assert c.sha256 != a.sha256


class TestSubstitution:
    def test_placeholders_are_replaced(self, prompt_dir):
        p = prompts_svc.load("test_prompt", prompt_dir)
        _, user = p.render({"transcript": "Hello world"})
        assert "Hello world" in user
        assert "{{transcript}}" not in user

    def test_a_transcript_full_of_braces_is_safe(self, prompt_dir):
        """str.format would raise or leak here; str.replace does not."""
        p = prompts_svc.load("test_prompt", prompt_dir)
        nasty = 'He said {"json": true} and then {0} and {unclosed'
        _, user = p.render({"transcript": nasty})
        assert nasty in user

    def test_unknown_placeholders_are_left_alone(self, prompt_dir):
        p = prompts_svc.load("test_prompt", prompt_dir)
        _, user = p.render({"nothing": "x"})
        assert "{{transcript}}" in user

    def test_none_becomes_empty_string(self):
        assert prompts_svc.substitute("a{{x}}b", {"x": None}) == "ab"


class TestListing:
    def test_lists_prompts_with_metadata(self, prompt_dir):
        listed = prompts_svc.list_prompts(prompt_dir)
        assert len(listed) == 1
        assert listed[0]["name"] == "test_prompt"
        assert listed[0]["version"] == "3"
        assert listed[0]["sha256"]

    def test_backups_are_not_listed(self, prompt_dir):
        (prompt_dir / "test_prompt.md.bak").write_text(SAMPLE)
        assert len(prompts_svc.list_prompts(prompt_dir)) == 1

    def test_a_missing_directory_lists_nothing(self, tmp_path):
        assert prompts_svc.list_prompts(tmp_path / "nope") == []


class TestSaving:
    def test_saving_updates_the_file_and_the_hash(self, prompt_dir):
        before = prompts_svc.load("test_prompt", prompt_dir)
        new_body = SAMPLE.replace("A test prompt", "An edited prompt")

        after = prompts_svc.save("test_prompt", new_body, prompt_dir)
        assert after.meta["description"] == "An edited prompt"
        assert after.sha256 != before.sha256

    def test_a_backup_is_kept(self, prompt_dir):
        original = prompts_svc.load("test_prompt", prompt_dir).body
        prompts_svc.save("test_prompt", SAMPLE.replace("version: 3", "version: 4"), prompt_dir)

        backup = prompt_dir / "test_prompt.md.bak"
        assert backup.exists()
        assert backup.read_text() == original

    def test_removing_a_required_placeholder_is_refused(self, prompt_dir):
        """A prompt that lost {{transcript}} would summarise nothing at all."""
        broken = SAMPLE.replace("{{transcript}}", "")
        with pytest.raises(ValidationError, match="transcript"):
            prompts_svc.save("test_prompt", broken, prompt_dir)

        # The original must survive the rejected save.
        assert "{{transcript}}" in prompts_svc.load("test_prompt", prompt_dir).user

    def test_no_temp_file_is_left_behind(self, prompt_dir):
        prompts_svc.save("test_prompt", SAMPLE, prompt_dir)
        assert not (prompt_dir / "test_prompt.md.tmp").exists()


class TestShippedPrompt:
    """The real file the app ships with."""

    def test_summary_prompt_loads_and_declares_its_contract(self):
        p = prompts_svc.load("summary_prompt")
        assert p.system
        assert p.user
        assert "transcript" in p.required_placeholders

    def test_it_asks_for_every_field_the_parser_expects(self):
        p = prompts_svc.load("summary_prompt")
        for field in (
            "title_suggestion", "tldr", "summary_md", "topics",
            "key_decisions", "action_items", "open_questions", "participants",
        ):
            assert field in p.system, f"prompt never mentions {field}"

    def test_it_carries_every_placeholder_the_code_supplies(self):
        p = prompts_svc.load("summary_prompt")
        for key in (
            "thread_title", "thread_description", "meeting_title",
            "meeting_date", "duration_human", "speaker_list", "transcript",
        ):
            assert "{{" + key + "}}" in p.user, f"prompt never uses {key}"

    def test_it_warns_the_model_off_non_speech_markers(self):
        p = prompts_svc.load("summary_prompt")
        assert "Environmental Sounds" in p.system

    def test_note_title_prompt_carries_every_placeholder_the_code_supplies(self):
        p = prompts_svc.load("note_title_prompt")
        assert "note_body" in p.required_placeholders
        for key in ("note_body", "question", "context_label"):
            assert "{{" + key + "}}" in p.user, f"prompt never uses {key}"

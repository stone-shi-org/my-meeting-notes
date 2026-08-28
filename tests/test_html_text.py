"""HTML to plain text, on the stdlib alone.

Email bodies are converted on the way *in* so the SPA never needs an HTML
sanitizer and SQLite never stores markup it will not display. The risk in that
trade is mangling a plain-text body that merely contains angle brackets, so most
of these tests are about what must be left alone.
"""

from __future__ import annotations

from app.services.html_text import html_to_text, looks_like_html, to_plain_text


class TestDetection:
    def test_real_markup_is_detected(self):
        for raw in (
            "<p>Hi</p>",
            "<div>Hi</div>",
            "<!DOCTYPE html><html><body>Hi</body></html>",
            "<ul><li>one</li></ul>",
            "<table><tr><td>A</td></tr></table>",
            'Click <a href="http://x">here</a>',
            "I said <b>no</b>",
        ):
            assert looks_like_html(raw), raw

    def test_plain_text_with_angle_brackets_is_not_markup(self):
        """The case that matters. Guessing wrong towards "html" would delete
        real characters out of a plain-text message; guessing wrong the other way
        merely leaves markup visible."""
        for raw in (
            "3 < 5 and 7 > 2",
            "> quoted line from an earlier reply",
            "Reply to <priya@acme.com> please",
            "Use the -> operator",
            "a<b and b>c",
            "",
        ):
            assert not looks_like_html(raw), raw

    def test_none_is_not_markup(self):
        assert looks_like_html(None) is False

    def test_a_single_accidental_tag_is_not_enough(self):
        """"a<b and b>c" parses as a well-formed <b ...> tag, so a single-tag
        rule would treat a comparison as markup and delete the middle of the
        sentence. Real markup essentially always closes something."""
        assert not looks_like_html("a<b and b>c")
        assert to_plain_text("a<b and b>c") == "a<b and b>c"

    def test_a_document_marker_needs_no_corroboration(self):
        assert looks_like_html("<html>")
        assert looks_like_html("<!DOCTYPE html>")

    def test_an_email_address_in_angle_brackets_is_not_a_tag(self):
        raw = "Forward it to <priya@acme.com> and <ops@acme.com> please"
        assert not looks_like_html(raw)
        assert to_plain_text(raw) == raw


class TestConversion:
    def test_paragraphs_become_blank_line_separated(self):
        assert html_to_text("<p>One</p><p>Two</p>") == "One\n\nTwo"

    def test_br_is_a_single_newline(self):
        assert html_to_text("<div>One<br>Two</div>") == "One\nTwo"

    def test_self_closing_br_also_breaks(self):
        assert html_to_text("<div>One<br/>Two</div>") == "One\nTwo"

    def test_inline_tags_do_not_break_the_line(self):
        assert html_to_text("<p>The <b>rollback</b> is <i>booked</i></p>") == (
            "The rollback is booked"
        )

    def test_script_and_style_content_is_dropped(self):
        raw = "<div>before</div><script>alert(1)</script><style>p{color:red}</style><div>after</div>"
        text = html_to_text(raw)
        assert "alert" not in text
        assert "color:red" not in text
        assert text == "before\n\nafter"

    def test_head_metadata_is_dropped(self):
        raw = "<html><head><title>Subject line</title></head><body>Real body</body></html>"
        assert html_to_text(raw) == "Real body"

    def test_entities_are_unescaped(self):
        assert html_to_text("<p>Costs &lt; $5 &amp; rising</p>") == "Costs < $5 & rising"

    def test_nbsp_becomes_a_space(self):
        assert html_to_text("<p>one&nbsp;two</p>") == "one two"

    def test_table_cells_are_space_separated_not_concatenated(self):
        """Mail is full of layout tables, so two adjacent cells running together
        as "AB" is the common case rather than an exotic one."""
        raw = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>"
        assert html_to_text(raw) == "A B\n\nC"

    def test_list_items_are_on_their_own_lines(self):
        assert html_to_text("<ul><li>one</li><li>two</li></ul>") == "one\n\ntwo"

    def test_runs_of_blank_lines_are_collapsed(self):
        raw = "<p>one</p><div></div><div></div><div></div><p>two</p>"
        assert "\n\n\n" not in html_to_text(raw)

    def test_runs_of_spaces_collapse_but_newlines_survive(self):
        assert html_to_text("<p>one     two</p><p>three</p>") == "one two\n\nthree"

    def test_an_orphan_closing_tag_does_not_swallow_the_body(self):
        """Common in forwarded mail. Clamping the suppression counter at zero is
        what stops one stray </style> hiding everything after it."""
        assert "real text" in html_to_text("</style>and then real text")

    def test_unclosed_tags_are_tolerated(self):
        """Block boundaries degrade to single newlines with no closing tag, which
        is fine -- the words must not run together, that is all."""
        assert html_to_text("<div><p>one<p>two").splitlines() == ["one", "two"]

    def test_empty_input_is_empty_output(self):
        assert html_to_text("") == ""
        assert html_to_text(None) == ""

    def test_a_body_that_is_only_markup_yields_empty_text(self):
        assert html_to_text("<style>p{color:red}</style>") == ""


class TestToPlainText:
    def test_markup_is_converted(self):
        assert to_plain_text("<p>Hi</p><p>Bye</p>") == "Hi\n\nBye"

    def test_plain_text_keeps_its_angle_brackets(self):
        raw = "3 < 5, and see <priya@acme.com>"
        assert to_plain_text(raw) == raw

    def test_plain_text_keeps_its_quote_markers(self):
        """Quoted-reply folding downstream depends on these surviving."""
        raw = "My reply\n\nOn Tue, Priya wrote:\n> the original\n> continued"
        assert to_plain_text(raw) == raw

    def test_crlf_is_normalised_in_both_paths(self):
        assert "\r" not in to_plain_text("one\r\ntwo")
        assert "\r" not in to_plain_text("<p>one\r\ntwo</p>")

    def test_surrounding_whitespace_is_trimmed(self):
        assert to_plain_text("\n\n  Hi  \n\n") == "Hi"

    def test_empty_input(self):
        assert to_plain_text("") == ""
        assert to_plain_text(None) == ""

import html
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import markdown as md_lib
from latex2mathml.converter import convert as latex_to_mathml

from vision_client import MAX_WORKERS, describe_image

logger = logging.getLogger(__name__)

_HTML_TABLE_RE = re.compile(r"^\s*<table[\s>]", re.IGNORECASE)
_MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# pymupdf4llm marks some extracted text with ~~strikethrough~~ when its
# ligature/duplicate-glyph detection flags it as uncertain (common with "fi"/
# "ti" ligatures) -- this is almost always an extraction artifact, not real
# intentional strikethrough, so the markers are dropped rather than rendered
# as <s>, which would falsely tell a screen reader the text was "deleted."
_STRIKETHROUGH_RE = re.compile(r"~~(.*?)~~", re.DOTALL)
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")

# Some PDFs' text layer contains literal LaTeX source (not a font-encoding
# problem like the transcribe_page fallback handles -- this text extracts
# perfectly cleanly, it's just never-rendered LaTeX sitting in the prose:
# "the sum $S_n$ of the series..."). Without this, it flows straight through
# as literal dollar signs and backslashed commands, which a screen reader
# reads character by character. $$...$$ must be matched before $...$, or
# the single-dollar pattern would match half of a double-dollar pair.
# Requires no whitespace touching the delimiters (the standard convention
# for distinguishing "$x$" from ordinary text like "$5 and $10").
_DISPLAY_MATH_RE = re.compile(r"\$\$(?!\s)(.+?)(?<!\s)\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"\$(?!\s)([^$\n]+?)(?<!\s)\$")
# \(...\) is LaTeX's own native inline-math delimiter (as opposed to the
# $...$ shorthand handled above) -- some documents' text layer uses one
# convention, some the other, some both. Without this, the math *inside*
# still converts fine via the bare-token pass, but the \( \) wrapper itself
# has no handler and is left as literal escaped backslash-paren text.
_LATEX_PAREN_MATH_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)

# Some PDFs' prose contains LaTeX with no $ delimiters at all -- e.g. "the
# progression S_n = a + ar + ar^2 + \cdots + ar^{n-1}." A backslash command
# (\cdots, \frac{3}{10}) is an unambiguous signal (backslashes essentially
# never appear in ordinary prose); a short base with a bare _/^ (S_n, ar^2)
# is the standard convention for an inline subscript/superscript variable.
# This only isolates and converts the LaTeX *tokens* themselves, leaving
# surrounding characters ("=", "+", "a", digits) as plain text -- those are
# already perfectly readable as-is, and not attempting to stitch a whole
# equation together avoids the much harder, more error-prone problem of
# deciding where an undelimited equation starts/ends inside a sentence.
# Base capped at 1-2 chars (real math variables are almost always that
# short) to reduce misfiring on code-style identifiers like "num_items";
# a negative lookbehind additionally stops the base from matching mid-word
# (without it, "um_items" inside "num_items" would itself satisfy the
# 1-2-char-base rule). The extension after _/^ is deliberately restricted to
# letters/digits/+/- , not "any non-space, non-brace character" -- otherwise
# trailing sentence punctuation glued on with no space ("S_n;", "S_n.")
# gets pulled into the token instead of staying outside it as plain text.
# One level of brace nesting is supported (covers \frac{a}{1-x^{-n}} style
# denominators); deeper nesting falls back to leaving the token as-is.
# \sqrt takes an optional bracketed root index before its brace group
# (\sqrt[4]{-64}, an nth root -- routine wherever a document covers roots
# of complex numbers). Without matching it, the token stopped at "\sqrt"
# itself, leaving "[4]{-64}" as an untouched but orphaned fragment right
# after a bare "\sqrt" that fails to convert on its own (no argument).
_BRACE_GROUP = r"\{(?:[^{}]|\{[^{}]*\})*\}"
_BARE_LATEX_TOKEN_RE = re.compile(
    rf"\\[a-zA-Z]+(?:\[[^\[\]]*\])?(?:{_BRACE_GROUP})*"
    rf"|(?<![A-Za-z])[A-Za-z]{{1,2}}[_^](?:{_BRACE_GROUP}|[A-Za-z0-9+\-]+)"
)


_UNCONVERTED_MARKER_RE = re.compile(r"\\|&(?!#\d+;|#x[0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)")
# \tag{1.12} is an equation-numbering annotation, not math content -- common
# on physics/textbook equations, and otherwise-perfectly-convertible math
# was being discarded wholesale over just this one trailing command (see
# _safe_latex_to_mathml). Safe to drop entirely: the equation number it
# would have added is cosmetic, and is normally already present in the
# surrounding prose as "(1.12)" anyway.
_TAG_COMMAND_RE = re.compile(r"\\tag\*?\{[^{}]*\}")

# Spacing-only macros (\quad, \qquad, \, \; \! \: and an escaped literal
# space) carry no math content of their own -- just horizontal whitespace
# between symbols or clauses. latex2mathml has nothing to convert them
# *to*, so left in place they typically fail conversion outright and get
# left behind as literal escaped backslash text ("\quad") sitting in the
# middle of otherwise-plain sentences. Collapsing them to an ordinary
# space before any LaTeX extraction runs keeps the real math content and
# drops only the spacing, which a screen reader was never going to render
# as meaningful whitespace anyway. The leading (?<!\\) stops this from
# matching the *second* backslash of a "\\" line-break marker (e.g.
# "\\ &..." or "\\[6pt]") -- without it, "\\ " reads as "\\" + "\ " and
# the space alternative eats the second backslash, silently corrupting the
# doubled-backslash marker that _LATEX_LINEBREAK_RE depends on.
_LATEX_SPACING_CMD_RE = re.compile(r"(?<!\\)\\(?:qquad|quad|,|;|!|:| )")

# A LaTeX line break ("\\") inside an align/aligned/gather-style block is
# often followed by an optional bracketed extra-spacing argument, e.g.
# "\\[6pt]" -- amsmath's way of adding vertical gap between rows. Splitting
# on a bare "\\" alone (the previous approach) left that "[6pt]" stuck onto
# the front of the next line, which then leaked into the rendered output as
# literal bracket-and-unit text.
_LATEX_LINEBREAK_RE = re.compile(r"\\\\\s*(?:\[[^\]]*\])?")

# latex2mathml raises DoubleSuperscriptsError on three or more consecutive
# '''  prime marks (f'''(x), the standard notation for a third derivative --
# routine in any Taylor-series/calculus section). Two primes convert fine
# natively; the failure is specific to 3+. Rewritten as an explicit
# superscript of repeated \prime commands, which converts correctly, before
# the source ever reaches latex2mathml.
_TRIPLE_PRIME_RE = re.compile(r"'{3,}")


def _expand_triple_primes(match):
    return "^{" + r"\prime" * len(match.group(0)) + "}"


# latex2mathml doesn't parse a \textit{}/\textbf{}/etc. command nested
# *inside* an outer \text{...} block -- it treats the nested command's own
# closing brace as if it were the outer block's closing brace, so
# everything after that point (potentially most of a long sentence) gets
# parsed as loose math-mode content instead of text, and the outer \text
# call fails validation over the "\\textit" that leaks through raw. This is
# common wherever a document italicizes a single word inside an
# explanatory in-math aside ("\text{...evaluated \textit{only} at..."}").
# Flattening the nested command down to its plain argument -- dropping
# just the italic/bold styling, not the words themselves -- sidesteps the
# parser bug; it only fires when the *whole* source is one top-level
# \text-family call, so a standalone \textit{...} elsewhere keeps its
# styling.
_TEXT_FAMILY_RE = re.compile(r"^\\text(it|bf|rm|sf|tt|normal)?\{(.*)\}$", re.DOTALL)
_NESTED_TEXT_CMD_RE = re.compile(r"\\text(?:it|bf|rm|sf|tt|normal)?\{([^{}]*)\}")


def _flatten_nested_text_commands(latex_source):
    match = _TEXT_FAMILY_RE.match(latex_source)
    if not match:
        return latex_source
    outer_prefix = latex_source[: match.start(2)]
    inner = _NESTED_TEXT_CMD_RE.sub(r"\1", match.group(2))
    return f"{outer_prefix}{inner}}}"


def _safe_latex_to_mathml(latex_source, **kwargs):
    """latex2mathml doesn't raise on LaTeX it doesn't understand -- two known
    ways this shows up: document/layout commands like \\rule{6cm}{0.4pt}
    aren't math notation, so it silently emits the literal command name as a
    garbage <mi> identifier (e.g. <mi>\\rule</mi>) instead of failing; and
    align-style environments' "&" column markers come through as a literal
    <mi>&</mi> identifier (latex2mathml's own output always uses proper
    &#x...; entities, so any *other* bare "&" is the same kind of
    unconverted leftover). Both read aloud as literal syntax ("backslash
    rule", "and", mid-equation) instead of real math, so either is treated
    as a conversion failure -- the caller's except-Exception fallback
    (escaped plain text) is the better outcome."""
    latex_source = _TAG_COMMAND_RE.sub("", latex_source).strip()
    latex_source = _TRIPLE_PRIME_RE.sub(_expand_triple_primes, latex_source)
    latex_source = _flatten_nested_text_commands(latex_source)
    result = latex_to_mathml(latex_source, **kwargs)
    if _UNCONVERTED_MARKER_RE.search(result):
        raise ValueError(f"latex2mathml left unconverted syntax in output: {result!r}")
    return result


# \begin{aligned}/\begin{align} etc. (multi-step derivations, "f(x) &= ... \\
# &= ...") use "&" for column alignment and "\\" for line breaks -- syntax
# latex2mathml doesn't understand (see _safe_latex_to_mathml above), so each
# line is converted separately with the alignment markers stripped first
# (dropping the column-alignment *layout*, not the mathematical content).
# \begin{cases}/\begin{pmatrix} etc. also use "&"/"\\", but latex2mathml
# *does* understand those natively (verified directly) -- converting the
# whole span at once, unmodified, produces correct output for them.
_LATEX_ALIGN_ENV_NAMES = ("aligned", "align", "align*", "gather", "gather*", "split", "eqnarray", "eqnarray*")
_LATEX_NATIVE_ENV_NAMES = ("cases", "matrix", "pmatrix", "bmatrix", "vmatrix", "Bmatrix")
_LATEX_ENVIRONMENT_RE = re.compile(
    rf"\\begin\{{({'|'.join(re.escape(n) for n in _LATEX_ALIGN_ENV_NAMES + _LATEX_NATIVE_ENV_NAMES)})\}}"
    r"(.*?)\\end\{\1\}",
    re.DOTALL,
)


# A derivation sometimes uses \rule{...}{...} as its own line -- a
# typeset horizontal divider between steps, not math content (see
# _safe_latex_to_mathml). Worth dropping outright rather than falling back
# to visible escaped text ("\rule{6cm}{0.4pt}" read aloud character by
# character): a divider line has no meaning to convey once it can't be a
# visual rule anymore.
_DECORATIVE_LATEX_LINE_RE = re.compile(r"^(?:\\rule(?:{_BRACE_GROUP}){{1,2}})+$".format(_BRACE_GROUP=_BRACE_GROUP))


def _convert_aligned_environment_body(body):
    # Fault-tolerant per line: a multi-line derivation is only as good as
    # its worst line if one bad line's exception is allowed to propagate --
    # that discards every *other* line too, dumping the whole derivation as
    # raw LaTeX even though five of its six lines would have converted
    # cleanly. Falling back to escaped plain text for just the one
    # offending line keeps the rest as real MathML.
    rendered_lines = []
    for line in _LATEX_LINEBREAK_RE.split(body):
        stripped = line.replace("&", "").strip()
        if not stripped or _DECORATIVE_LATEX_LINE_RE.match(stripped):
            continue
        try:
            rendered_lines.append(_safe_latex_to_mathml(stripped, display="block"))
        except Exception:
            rendered_lines.append(f"<p>{html.escape(stripped)}</p>")
    if not rendered_lines:
        raise ValueError("empty aligned environment")
    return "".join(rendered_lines)


def _convert_latex_environment(match):
    env_name, body = match.group(1), match.group(2)
    if env_name in _LATEX_ALIGN_ENV_NAMES:
        return _convert_aligned_environment_body(body)
    return _safe_latex_to_mathml(match.group(0), display="block")


def _extract_placeholders(text, pattern, token_prefix, to_latex):
    """Shared machinery for both $...$ spans and bare LaTeX tokens: replace
    regex matches with placeholder tokens (safe from markdown's **/_ syntax
    and from HTML-escaping, unlike the real MathML), returning the
    placeholder text plus {token: mathml_html} to substitute back in after
    markdown rendering and escaping are done. On a parse failure, falls back
    to the original matched text escaped as plain text (no worse than before
    this feature existed)."""
    replacements = {}

    def _sub(match):
        token = f"@@{token_prefix}{len(replacements)}@@"
        try:
            replacements[token] = to_latex(match)
        except Exception:
            replacements[token] = html.escape(match.group(0))
        return token

    return pattern.sub(_sub, text), replacements


def _extract_math_spans(text):
    # Each pass below gets its own token prefix -- NOT a shared "MATHSPAN"
    # for all three. _extract_placeholders numbers tokens from 0 within a
    # single call, so two passes sharing one prefix can both mint
    # "@@MATHSPAN0@@": the second pass's dict entry then silently
    # overwrites the first's under .update(), and every occurrence of that
    # token in the text -- both the real display-math position and the
    # real inline-math position -- gets replaced with whichever value won,
    # corrupting one of the two spans with the other's content.
    text, display_repl = _extract_placeholders(
        text,
        _DISPLAY_MATH_RE,
        "MATHDISP",
        lambda m: _safe_latex_to_mathml(m.group(1).strip(), display="block"),
    )
    text, inline_repl = _extract_placeholders(
        text,
        _INLINE_MATH_RE,
        "MATHINL",
        lambda m: _safe_latex_to_mathml(m.group(1).strip(), display="inline"),
    )
    text, paren_repl = _extract_placeholders(
        text,
        _LATEX_PAREN_MATH_RE,
        "MATHPAREN",
        lambda m: _safe_latex_to_mathml(m.group(1).strip(), display="inline"),
    )
    display_repl.update(inline_repl)
    display_repl.update(paren_repl)
    return text, display_repl


def _extract_bare_latex_tokens(text):
    return _extract_placeholders(
        text, _BARE_LATEX_TOKEN_RE, "LATEXTOK", lambda m: _safe_latex_to_mathml(m.group(0))
    )


def _extract_latex_environments(text):
    return _extract_placeholders(text, _LATEX_ENVIRONMENT_RE, "LATEXENV", _convert_latex_environment)


_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")


def _render_emphasis(text):
    """Minimal, inline-only **bold**/_italic_ rendering. Deliberately NOT
    markdown.markdown(), which applies full block-level parsing (lists,
    headers, blockquotes...) to whatever text it's given -- heading/
    paragraph content that happens to start with a numbered-list-shaped
    prefix ("1. The Geometric Series", a real section title, not a list;
    "1. Derive the formula...", a homework problem) would otherwise get
    wrapped in <ol><li>...</li></ol>, which is exactly what surfaced as a
    <ol> nested inside a ToC <a> link and inside a <p> for numbered
    problems."""
    text = _BOLD_STAR_RE.sub(r"<strong>\1</strong>", text)
    text = _BOLD_UNDERSCORE_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_STAR_RE.sub(r"<em>\1</em>", text)
    text = _ITALIC_UNDERSCORE_RE.sub(r"<em>\1</em>", text)
    return text


def _render_inline_markdown(text):
    """Renders lingering **bold**/_italic_ markdown as real HTML instead of
    literal asterisks/underscores (which a screen reader reads character by
    character). HTML-escapes first so raw '<'/'>'/'&' in the source text
    (common in STEM prose: "x < 5") can't be misread as real HTML tags --
    markdown syntax itself is ASCII punctuation only, so escaping first
    doesn't interfere with **/_ formatting. LaTeX math ($...$ spans and bare
    undelimited tokens) is protected from both steps via placeholder tokens,
    then swapped for real MathML at the end."""
    text = _STRIKETHROUGH_RE.sub(r"\1", text)
    # Drop stray \tag{...} and spacing macros (\quad, \,, ...) up front --
    # both are common sitting just outside a $...$ span or an aligned
    # environment (an equation-numbering annotation, or padding between
    # clauses) and neither has any bare-token or environment handling of
    # its own, so left alone they surface as literal escaped backslash
    # text instead of disappearing the way they should.
    text = _TAG_COMMAND_RE.sub("", text)
    text = _LATEX_SPACING_CMD_RE.sub(" ", text)
    # Environments first: they're the largest/most encompassing spans, and
    # claiming them before the other two passes run stops "\cdots" or "a_n"
    # inside an aligned block from being fragmented out by the token pass.
    text, env_replacements = _extract_latex_environments(text)
    text, math_replacements = _extract_math_spans(text)
    text, bare_replacements = _extract_bare_latex_tokens(text)
    math_replacements.update(env_replacements)
    math_replacements.update(bare_replacements)
    rendered = _render_emphasis(html.escape(text))
    for token, mathml in math_replacements.items():
        rendered = rendered.replace(token, mathml)
    return rendered


_DATA_LIKE_FIRST_CELL_RE = re.compile(r"^\d+\.?$")
_TABLE_HEADER_ROW_RE = re.compile(r"<thead>\s*<tr>(.*?)</tr>\s*</thead>\s*<tbody>", re.DOTALL)


def _first_row_looks_like_data(content):
    """python-markdown's tables extension always treats row 1 as the header
    row -- fine for a real header, wrong when the "table" is actually a
    numbered list ("3.  0.55555...") whose first row is just the first
    item. A bare item-number ("3.", "3") as the first cell is never a
    legitimate column label, so that's the signal used to detect this."""
    first_line = content.splitlines()[0] if content else ""
    cells = [c.strip() for c in first_line.strip().strip("|").split("|")]
    return bool(cells) and bool(_DATA_LIKE_FIRST_CELL_RE.match(cells[0]))


def _demote_fake_header_row(table_html):
    """Moves the incorrectly-promoted first row from <thead>/<th> back into
    <tbody>/<td> -- so it's announced as "problem 3, value" (a data row),
    not as if "3." were a column header labeling every row beneath it."""

    def _sub(match):
        row = match.group(1).replace("<th", "<td").replace("</th>", "</td>")
        return f"<tbody><tr>{row}</tr>"

    return _TABLE_HEADER_ROW_RE.sub(_sub, table_html, count=1)


def _table_content_to_html(content):
    content = content.strip()
    content = _TAG_COMMAND_RE.sub("", content)
    content = _LATEX_SPACING_CMD_RE.sub(" ", content)
    # Bare LaTeX tokens can appear in table cells too (e.g. a list of
    # repeating decimals like "0.55555\cdots") -- protect them the same way
    # before any of the branches below process the content, then restore.
    content, bare_replacements = _extract_bare_latex_tokens(content)

    if _HTML_TABLE_RE.match(content):
        if "<caption>" not in content.lower():
            content = re.sub(
                r"(<table[^>]*>)", r"\1<caption>Table</caption>", content, count=1, flags=re.IGNORECASE
            )
        result = content
    elif content and _MARKDOWN_TABLE_ROW_RE.match(content.splitlines()[0]):
        rendered = md_lib.markdown(content, extensions=["tables"])
        if _first_row_looks_like_data(content):
            rendered = _demote_fake_header_row(rendered)
        result = rendered.replace("<table>", "<table><caption>Table</caption>", 1)
    else:
        escaped_rows = "".join(
            f"<tr><td>{html.escape(line)}</td></tr>" for line in content.splitlines() if line.strip()
        )
        result = f"<table><caption>Table</caption>{escaped_rows}</table>"

    for token, mathml in bare_replacements.items():
        result = result.replace(token, mathml)
    return result


def _formula_to_html(content):
    # A standalone formula block can itself be a whole \begin{aligned}...
    # \end{aligned}-style derivation -- give it the same environment-aware
    # handling as inline text (see _extract_latex_environments), not just a
    # raw conversion attempt that would trip the "&" rejection and fall all
    # the way back to escaped plain text. Stripped first so a trailing
    # \tag{...} or stray spacing macro right after \end{...} doesn't stop
    # the fullmatch below from recognizing the block as an environment.
    content = _TAG_COMMAND_RE.sub("", content)
    content = _LATEX_SPACING_CMD_RE.sub(" ", content)
    env_match = _LATEX_ENVIRONMENT_RE.fullmatch(content.strip())
    try:
        if env_match:
            return _convert_latex_environment(env_match)
        return _safe_latex_to_mathml(content)
    except Exception:
        return f"<p>{html.escape(content)}</p>"


def _clamp_level(block):
    level = block.get("level") or 2
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 2
    return min(max(level, 2), 6)  # h1 is reserved for the document title


def _slugify(text, used_ids):
    base = _SLUG_INVALID_RE.sub("-", text.lower()).strip("-") or "section"
    slug = base
    n = 2
    while slug in used_ids:
        slug = f"{base}-{n}"
        n += 1
    used_ids.add(slug)
    return slug


def _heading_html(block):
    level = _clamp_level(block)
    id_attr = f' id="{block["id"]}"' if block.get("id") else ""
    return f"<h{level}{id_attr}>{_render_inline_markdown(block['content'])}</h{level}>"


def _build_toc(heading_entries):
    """A real, tabbable table of contents -- a heading alone isn't reachable
    by Tab (screen readers jump between headings with their own commands,
    but that leaves sighted keyboard users with no way in); an anchor link
    is focusable and jumps on activation, which a heading by itself is not.
    Mirrors the source document's own heading nesting.

    A source document can reuse generic headings across chapters ("Example
    2.", "PROBLEMS, SECTION 6") -- fine for the headings themselves (nesting
    still disambiguates them visually), but indistinguishable out of context
    in a screen reader's "links list" view, which flattens the ToC to just
    link text. Only *duplicated* heading text gets its nearest ancestor
    heading appended ("Example 2. (Chapter 3: Infinite Series)"); unique
    headings are left alone rather than cluttering every entry."""
    if len(heading_entries) < 2:
        return ""

    text_counts = Counter(h["content"].strip() for h in heading_entries)

    parts = ['<nav aria-label="Table of contents"><h2>Contents</h2>']
    level_stack = []
    for h in heading_entries:
        level = h["level"]
        while level_stack and level < level_stack[-1]:
            parts.append("</li></ul>")
            level_stack.pop()
        if level_stack and level == level_stack[-1]:
            parts.append("</li>")
        else:
            parts.append("<ul>")
            level_stack.append(level)
        text = _render_inline_markdown(h["content"])
        if h.get("parent") and text_counts[h["content"].strip()] > 1:
            text = f"{text} ({_render_inline_markdown(h['parent'])})"
        parts.append(f'<li><a href="#{h["id"]}">{text}</a>')
    while level_stack:
        parts.append("</li></ul>")
        level_stack.pop()
    parts.append("</nav>")
    return "".join(parts)


def _image_result_to_html(result, caption):
    if result.get("kind") == "formula":
        return _formula_to_html(result.get("content", ""))
    if result.get("kind") == "decorative":
        # Omitted entirely, not narrated -- matches standard accessibility
        # guidance that non-informational images should be hidden from
        # assistive tech, not read aloud (equivalent to alt="").
        return None
    # figure/figcaption, not role="img": role="img" would hide this element's
    # children from the accessibility tree and expose only the aria-label,
    # silently dropping the actual table/sentence a screen reader needs to
    # read. figcaption announces as a caption without hiding anything after
    # it. Prefer the source document's own real caption when one was found
    # nearby (document_parser._attach_captions) -- inventing our own numbered
    # label ("Figure 1", "Figure 2"...) both duplicates and can misalign with
    # a document's existing captions.
    caption_html = _render_inline_markdown(caption) if caption else "Chart description"
    description_html = md_lib.markdown(result.get("content", ""), extensions=["tables"])
    return f"<figure><figcaption>{caption_html}</figcaption>{description_html}</figure>"


def blocks_to_html(blocks, image_results=None, api_key=None):
    """image_results: optional {path: {"kind":..., "content":...}} of
    already-fetched figure descriptions (e.g. from a Batches API run). Any
    image not already in it gets described live, concurrently -- each call
    is independent, so there's no reason to wait on them one at a time.
    api_key: forwarded to each describe_image call (per-request BYOK key
    from the upload form)."""
    image_results = dict(image_results or {})

    # One pass: assign each heading a stable id, track its nearest ancestor
    # heading (for ToC disambiguation -- see _build_toc), and collect ToC
    # entries, so heading rendering and the ToC's links agree on the same
    # ids.
    used_ids = set()
    heading_entries = []
    ancestor_stack = []  # [(level, content), ...] of currently-open ancestors
    for block in blocks:
        if block.get("type") == "heading":
            level = _clamp_level(block)
            block["id"] = _slugify(block["content"], used_ids)
            while ancestor_stack and ancestor_stack[-1][0] >= level:
                ancestor_stack.pop()
            parent = ancestor_stack[-1][1] if ancestor_stack else None
            heading_entries.append(
                {"level": level, "content": block["content"], "id": block["id"], "parent": parent}
            )
            ancestor_stack.append((level, block["content"]))

    missing = [
        (block["path"], block.get("section_context"))
        for block in blocks
        if block.get("type") == "image" and block["path"] not in image_results
    ]
    if missing:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_path = {
                executor.submit(describe_image, path, context, api_key=api_key): path
                for path, context in missing
            }
            for future in as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    image_results[path] = future.result()
                except anthropic.APIError:
                    # Mirrors document_parser.py's per-page transcription
                    # fallback: the API can reject a single call outright
                    # (e.g. output content filtering) independent of the
                    # rest of the document. One figure failing shouldn't
                    # discard every other figure and all the already-parsed
                    # text/tables/math along with it -- fall back to the
                    # same "couldn't describe" placeholder _extract_result
                    # uses for a tool-call failure, just for this one image.
                    logger.warning("describe_image failed for %s; using fallback description", path)
                    image_results[path] = {
                        "kind": "diagram",
                        "content": "(This figure could not be described automatically.)",
                    }

    html_parts = ["<h1>Document</h1>", _build_toc(heading_entries)]

    for block in blocks:
        btype = block.get("type")
        content = block.get("content", "")

        if btype == "heading":
            html_parts.append(_heading_html(block))
        elif btype == "table":
            html_parts.append(_table_content_to_html(content))
        elif btype == "ordered_list":
            # A run of consecutive numbered paragraphs (document_parser.
            # _merge_numbered_lists), e.g. a homework problem set -- one
            # real <ol> a screen reader user can arrow through, instead of
            # each item being its own separately-announced one-item list.
            start = block.get("start", 1)
            start_attr = "" if start == 1 else f' start="{start}"'
            items_html = "".join(
                f"<li>{_render_inline_markdown(item)}</li>" for item in block.get("items", [])
            )
            html_parts.append(f"<ol{start_attr}>{items_html}</ol>")
        elif btype == "formula":
            # Standalone formula blocks (from vision_client.transcribe_page's
            # math-page fallback) -- distinct from an image block whose
            # *classification* happens to be "formula", handled below.
            html_parts.append(_formula_to_html(content))
        elif btype == "image":
            result = image_results.get(block["path"])
            rendered = _image_result_to_html(result, block.get("caption"))
            if rendered is not None:
                html_parts.append(rendered)
        else:
            html_parts.append(f"<p>{_render_inline_markdown(content)}</p>")

    return "\n".join(html_parts)

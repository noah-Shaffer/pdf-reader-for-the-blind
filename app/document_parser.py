import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import pymupdf4llm

from vision_client import MAX_WORKERS, transcribe_page

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Assumes a figure's markdown image syntax sits alone on its own line, which
# is what pymupdf4llm produced in testing (block-level image, surrounded by
# blank lines) -- verify against a real document if images ever get dropped.
_IMAGE_RE = re.compile(r"^!\[.*?\]\((.*?)\)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

# pymupdf4llm has no built-in running-header/footer suppression in this
# version, and its `margins` param clips at the glyph level -- it can garble
# body text that merely sits near the margin, not just remove page furniture.
# So running headers/footers/page numbers are detected here instead, from
# already-cleanly-extracted text, via two complementary signals:
#  1. Content that repeats identically at the same page-edge position across
#     many pages (e.g. a constant chapter title on every page).
#  2. A short page-edge block containing a page-number-like token (e.g.
#     "2 1 Introduction", "viii Contents") -- these change per chapter/
#     section, so they don't repeat often enough for signal 1 to catch them,
#     but no real body paragraph is ever just a handful of words including a
#     bare page number.
_NUMBER_TOKEN_RE = re.compile(r"^[0-9]+$|^[ivxlcdm]+$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_MAX_FURNITURE_WORDS = 6
_DECORATIVE_MARKER_RE = re.compile(r"[▶◀►◄→←»«]")

# A dot-leader table-of-contents line, e.g. "1.1 Occurrence ... . . . . 12"
# or "Erratum ... . . . . E1" (a lettered page reference). Real prose never
# contains 4+ repeated ". " runs; a print ToC is pure navigation aid and is
# superseded by this document's own real headings.
_TOC_LINE_RE = re.compile(r"(\.\s*){4,}[A-Za-z]?\d+\s*$")

# A real figure caption from the source document, e.g. "Fig. 1.1 ..." or
# "Figure 3: ...". When present near an image block, this is used as its
# figcaption instead of a generic label.
_CAPTION_RE = re.compile(r"^(fig\.?|figure)\s*\d", re.IGNORECASE)

# Math typeset with a non-standard font encoding (common in older
# LaTeX-produced academic PDFs) can't be recovered by font-based text
# extraction at all -- pymupdf4llm emits the Unicode replacement character
# for glyphs it can't decode, splits sub/superscripts into disconnected
# bracket runs ("[21]", "[T]", "[3][=][2]"), and even mis-decodes some
# glyphs as unrelated letters (a PDF's "(" glyph showing up as the
# Icelandic "ð"). No amount of text cleanup recovers this -- the
# information is gone by the time it reaches us as text. Measured against
# a real document: clean pages score exactly 0 on all three signals below;
# garbled pages score 4-136. The threshold has wide margin.
_REPLACEMENT_CHAR = "�"
_BRACKET_RUN_RE = re.compile(r"\[[^\[\]]{1,4}\]")
_PSEUDO_PAREN_RE = re.compile(r"[ðÞ]")
_GARBLE_THRESHOLD = 3


def _page_looks_math_garbled(text):
    signals = (
        text.count(_REPLACEMENT_CHAR)
        + len(_BRACKET_RUN_RE.findall(text))
        + len(_PSEUDO_PAREN_RE.findall(text))
    )
    return signals >= _GARBLE_THRESHOLD


# A known, specific PDF font-extraction bug: the "ff" ligature glyph decodes
# to a literal "!" instead of the correct Unicode ("di!erent" -> "different",
# "e!ectively" -> "effectively"). A "!" directly between two letters (no
# surrounding whitespace) is never a real exclamation mark -- that always
# sits at the end of a word/sentence -- so this is a safe, specific signal.
_BROKEN_LIGATURE_RE = re.compile(r"(?<=[A-Za-z])!(?=[A-Za-z])")


def _fix_broken_ligatures(text):
    return _BROKEN_LIGATURE_RE.sub("ff", text)


def extract_pages(pdf_path, image_dir):
    """Local, free extraction: reading order, headings, tables, and cropped
    figures come straight from the PDF's own structure via pymupdf4llm --
    no Claude call involved. Only figures need Claude afterward (see
    parse_blocks -> image blocks)."""
    os.makedirs(image_dir, exist_ok=True)
    return pymupdf4llm.to_markdown(
        pdf_path,
        write_images=True,
        image_path=image_dir,
        image_format="png",
        page_chunks=True,
    )


def parse_blocks(pages, pdf_path=None):
    """pdf_path: enables the math-garbled-page fallback (renders the page
    and has Claude transcribe it directly) -- pass it whenever available.
    Without it, garbled pages are parsed locally like any other (i.e. left
    garbled)."""
    per_page_blocks = [None] * len(pages)
    garbled_jobs = []  # (page_index, page_number, local_blocks, context_text)

    for i, page in enumerate(pages):
        text = page["text"]
        if pdf_path and _page_looks_math_garbled(text):
            page_number = page["metadata"]["page"]
            local_blocks = _parse_page_text(text)
            # Context from the *local* extraction of a neighboring page, not
            # from another page's Claude transcription -- using the latter
            # would make each page's call depend on another page's result
            # and block running them concurrently below. Prefer the previous
            # page (more likely to set up notation this page continues);
            # fall back to the next page for page 1.
            if i > 0:
                context_text = pages[i - 1]["text"]
            elif i + 1 < len(pages):
                context_text = pages[i + 1]["text"]
            else:
                context_text = None
            garbled_jobs.append((i, page_number, local_blocks, context_text))
        else:
            per_page_blocks[i] = _parse_page_text(text)

    if garbled_jobs:
        logger.info(
            "Transcribing %d math-garbled page(s) via Claude vision (up to %d at once)...",
            len(garbled_jobs),
            MAX_WORKERS,
        )
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_job = {
                executor.submit(transcribe_page, pdf_path, page_number, context_text=context_text): (
                    i,
                    page_number,
                    local_blocks,
                )
                for i, page_number, local_blocks, context_text in garbled_jobs
            }
            for future in as_completed(future_to_job):
                i, page_number, local_blocks = future_to_job[future]
                try:
                    text_blocks = future.result()
                    image_blocks = [b for b in local_blocks if b["type"] == "image"]
                    # Approximate ordering: Claude's transcription has no
                    # shared position info with pymupdf4llm's image
                    # extraction, so the best available placement is text
                    # first, images after.
                    per_page_blocks[i] = text_blocks + image_blocks
                except anthropic.APIError:
                    # Seen in practice: the API can reject a page outright
                    # (e.g. output content filtering) independent of the
                    # page's actual content. One page failing shouldn't fail
                    # the whole document -- fall back to the original (still
                    # garbled) local parse, which already has correct
                    # text/image interleaving, so use it as-is rather than
                    # rebuilding it text-first.
                    logger.warning(
                        "transcribe_page failed for page %d; falling back to local (garbled) text",
                        page_number,
                        exc_info=True,
                    )
                    per_page_blocks[i] = local_blocks

    _strip_page_furniture(per_page_blocks)

    blocks = []
    for page_blocks in per_page_blocks:
        blocks.extend(page_blocks)
    blocks = _attach_captions(blocks)
    blocks = _attach_section_context(blocks)
    return _merge_numbered_lists(blocks)


def _parse_page_text(text):
    text = _fix_broken_ligatures(text)
    blocks = []
    paragraph_lines = []
    table_lines = []

    def flush_paragraph():
        if paragraph_lines:
            content = " ".join(paragraph_lines).strip()
            if content:
                blocks.append({"type": "paragraph", "content": content})
            paragraph_lines.clear()

    def flush_table():
        if table_lines:
            blocks.append({"type": "table", "content": "\n".join(table_lines)})
            table_lines.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line or _TOC_LINE_RE.search(line):
            flush_paragraph()
            flush_table()
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_paragraph()
            flush_table()
            blocks.append(
                {
                    "type": "heading",
                    "level": len(heading_match.group(1)),
                    "content": heading_match.group(2).strip(),
                }
            )
            continue

        image_match = _IMAGE_RE.match(line)
        if image_match:
            flush_paragraph()
            flush_table()
            blocks.append({"type": "image", "path": image_match.group(1)})
            continue

        if _TABLE_ROW_RE.match(line):
            flush_paragraph()
            table_lines.append(line)
            continue

        flush_table()
        paragraph_lines.append(line)

    flush_paragraph()
    flush_table()
    return blocks


def _looks_like_page_furniture(content):
    tokens = content.strip().split()
    if not tokens or len(tokens) > _MAX_FURNITURE_WORDS:
        return False
    return any(_NUMBER_TOKEN_RE.match(tok.strip(".,:")) for tok in tokens)


def _normalize_for_repetition(content):
    # Collapse digit runs so "3" vs "4" or "Chapter 3" vs "Chapter 4" still
    # count as the same repeating pattern across pages.
    return _DIGITS_RE.sub("#", content.strip())


def _strip_page_furniture(per_page_blocks):
    if len(per_page_blocks) >= 2:
        first_counts = Counter()
        last_counts = Counter()
        for page_blocks in per_page_blocks:
            if page_blocks:
                first_counts[_normalize_for_repetition(page_blocks[0].get("content", ""))] += 1
                last_counts[_normalize_for_repetition(page_blocks[-1].get("content", ""))] += 1

        def _is_repeating_furniture(normalized, counts):
            # Repetition alone (2+ occurrences at the same page-edge
            # position) is already a strong signal, regardless of length --
            # a running header's title text changes per chapter/section (so
            # it won't repeat across the *whole* document), but genuine
            # unique content essentially never repeats verbatim at a page
            # edge at all. No word-count cap here (unlike the checks below):
            # real running heads can combine a chapter title, section title,
            # and page number into one longer line, e.g. "14 <arrow>
            # Infinite Series, Power Series Chapter 1".
            return bool(normalized) and counts[normalized] >= 2

        for page_blocks in per_page_blocks:
            if page_blocks:
                norm = _normalize_for_repetition(page_blocks[0].get("content", ""))
                if _is_repeating_furniture(norm, first_counts):
                    page_blocks.pop(0)
            if page_blocks:
                norm = _normalize_for_repetition(page_blocks[-1].get("content", ""))
                if _is_repeating_furniture(norm, last_counts):
                    page_blocks.pop(-1)

    # Short, numeral-bearing edge blocks (running headers/page numbers whose
    # chapter/section title changes too often to repeat identically -- see
    # module docstring). Unlike the repetition check above, this one doesn't
    # require repetition at all, so it's restricted to type "paragraph": a
    # real detected heading (larger font, structurally significant) is
    # trusted as content even if short and numbered, e.g. a legitimate
    # "Chapter 3" opener that only appears once.
    for page_blocks in per_page_blocks:
        if (
            page_blocks
            and page_blocks[0].get("type") == "paragraph"
            and _looks_like_page_furniture(page_blocks[0].get("content", ""))
        ):
            page_blocks.pop(0)
        if (
            page_blocks
            and page_blocks[-1].get("type") == "paragraph"
            and _looks_like_page_furniture(page_blocks[-1].get("content", ""))
        ):
            page_blocks.pop(-1)

    # Decorative pointer/arrow characters (e.g. "14 <arrow> Infinite Series
    # <arrow> Chapter 1", "Section 6 ... Absolute Convergence <arrow> 13")
    # are an unambiguous running-head signal -- real heading/section titles
    # essentially never contain them. Applies regardless of block type,
    # since these get misclassified as real headings often enough to matter
    # (pymupdf4llm's font-size heuristic doesn't distinguish a running
    # head's styling from a genuine section heading's).
    for page_blocks in per_page_blocks:
        if page_blocks and _DECORATIVE_MARKER_RE.search(page_blocks[0].get("content", "")):
            page_blocks.pop(0)
        if page_blocks and _DECORATIVE_MARKER_RE.search(page_blocks[-1].get("content", "")):
            page_blocks.pop(-1)


def _attach_captions(blocks):
    """If a real figure caption ("Fig. 1.1 ...") sits immediately next to an
    image block, use it as that figure's caption and drop the now-redundant
    standalone paragraph -- instead of inventing a generic/numbered label
    that would either collide with or duplicate the source document's own
    captions. Prefers the caption immediately after the image (the more
    common convention), falling back to immediately before."""
    consumed = set()
    for i, block in enumerate(blocks):
        if block.get("type") != "image":
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(blocks) and j not in consumed:
                candidate = blocks[j]
                if candidate.get("type") == "paragraph" and _CAPTION_RE.match(
                    candidate.get("content", "").strip()
                ):
                    block["caption"] = candidate["content"]
                    consumed.add(j)
                    break

    return [b for i, b in enumerate(blocks) if i not in consumed]


def _attach_section_context(blocks):
    """Tags each image block with the nearest preceding heading, so
    vision_client.describe_image can tell Claude what section a figure
    appears in -- helps it interpret otherwise-ambiguous diagram content."""
    current_heading = None
    for block in blocks:
        if block.get("type") == "heading":
            current_heading = block.get("content")
        elif block.get("type") == "image" and current_heading:
            block["section_context"] = current_heading
    return blocks


_NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(.+)$", re.DOTALL)


def _merge_numbered_lists(blocks):
    """Consecutive paragraphs that are really one numbered list (a problem
    set: "1. Derive...", "2. Show that...", "3. Use equation...") get
    merged into one real <ol> instead of staying separate paragraphs --
    each of which a screen reader's list-parsing otherwise announces as its
    own "list, 1 item", dozens of times per section. Requires strictly
    sequential numbering (N, N+1, N+2, ...) to avoid merging two unrelated
    numbered mentions that happen to land next to each other."""
    result = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        match = block.get("type") == "paragraph" and _NUMBERED_ITEM_RE.match(block.get("content", ""))
        if not match:
            result.append(block)
            i += 1
            continue

        items = [match.group(2)]
        start_number = int(match.group(1))
        expected = start_number + 1
        j = i + 1
        while j < len(blocks):
            candidate = blocks[j]
            candidate_match = candidate.get("type") == "paragraph" and _NUMBERED_ITEM_RE.match(
                candidate.get("content", "")
            )
            if not candidate_match or int(candidate_match.group(1)) != expected:
                break
            items.append(candidate_match.group(2))
            expected += 1
            j += 1

        if len(items) >= 2:
            result.append({"type": "ordered_list", "items": items, "start": start_number})
            i = j
        else:
            result.append(block)
            i += 1

    return result

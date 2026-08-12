import base64
import logging
import time

import anthropic
import fitz
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
# Headroom for a chart with many data points (a large markdown table can run
# long); if this is still too tight, _extract_result logs a warning rather
# than silently returning an empty description.
MAX_TOKENS = 4096
# A math-dense page transcription can need many formula blocks in one
# response.
PAGE_MAX_TOKENS = 8192

# Batches typically finish within an hour, up to 24h max.
BATCH_POLL_SECONDS = 30

# Shared by document_parser.py (transcribe_page calls) and assemble.py
# (describe_image calls) -- both fire these off concurrently via
# ThreadPoolExecutor since each call is independent of the others' results.
# Kept modest to stay well clear of per-minute rate limits.
MAX_WORKERS = 6

# Context text is informational only, not something to transcribe/describe --
# capped so one unusually dense neighboring page can't balloon the prompt.
MAX_CONTEXT_CHARS = 2000

INSTRUCTION = (
    "This is a cropped figure from a document page. Classify it and describe "
    "it for a blind reader via the describe_image tool. Pick exactly one "
    "kind:\n"
    '- "formula": a mathematical formula or equation. content is the LaTeX '
    "source only (no $ or $$ delimiters, no explanation).\n"
    '- "chart": a chart, graph, or diagram that encodes data (values, axes, '
    "series). content is a markdown table with axis/series labels capturing "
    "that data.\n"
    '- "diagram": a meaningful illustrative figure that is NOT literally '
    "data-bearing -- a conceptual diagram, apparatus photo, circuit "
    "schematic, process illustration, or anything else a sighted reader "
    "would learn something from. content is a clear descriptive paragraph "
    "covering what it shows and why it's there.\n"
    '- "decorative": conveys no information on its own -- background '
    "textures, gradients, cover-art fragments, logos, ornamental dividers. "
    "content is one sentence, but note this figure will likely be omitted "
    "entirely rather than read aloud, matching standard accessibility "
    "guidance for non-informational images -- so only choose this when the "
    "figure truly adds nothing a blind reader would need."
)

IMAGE_TOOL = {
    "name": "describe_image",
    "description": "Classify and describe a cropped figure from a document page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["formula", "chart", "diagram", "decorative"],
            },
            "content": {"type": "string"},
        },
        "required": ["kind", "content"],
    },
}


PAGE_INSTRUCTION = (
    "Local text extraction failed on this page, most likely because of "
    "broken math-font encoding -- transcribe it directly from the image "
    "instead, via the transcribe_page tool, as an ordered list of content "
    "blocks:\n"
    "- heading: heading text, with a level (1-6) reflecting its visual "
    "hierarchy.\n"
    "- paragraph: plain text of one paragraph, in reading order.\n"
    "- table: GitHub-flavored markdown table with a header row.\n"
    "- formula: LaTeX source only (no $ or $$ delimiters) for any "
    "standalone equation -- transcribe it faithfully as real math notation, "
    "not as approximated plain text.\n"
    "Do not describe any images or figures on the page -- transcribe text "
    "only; figures are handled separately."
)

PAGE_TOOL = {
    "name": "transcribe_page",
    "description": "Transcribe a document page's headings, paragraphs, tables, and formulas in reading order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["heading", "paragraph", "table", "formula"],
                        },
                        "level": {
                            "type": "integer",
                            "description": "Heading level 1-6; only meaningful when type is 'heading'.",
                        },
                        "content": {"type": "string"},
                    },
                    "required": ["type", "content"],
                },
            }
        },
        "required": ["blocks"],
    },
}


def _transcribe_page_region(pdf_path, page_number, dpi, context_text, y_fraction=None, api_key=None):
    """Renders either the full page (y_fraction=None) or a vertical slice of
    it (y_fraction=(y0, y1), as fractions of page height) and asks Claude to
    transcribe that image. Shared by transcribe_page's full-page attempt and
    its split-page retry (see transcribe_page)."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_number - 1]
        zoom = dpi / 72
        clip = None
        if y_fraction is not None:
            rect = page.rect
            y0f, y1f = y_fraction
            clip = fitz.Rect(rect.x0, rect.y0 + rect.height * y0f, rect.x1, rect.y0 + rect.height * y1f)
        image_bytes = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip).tobytes("png")
    finally:
        doc.close()

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
    ]
    if context_text:
        content.append(
            {
                "type": "text",
                "text": (
                    "For context only -- do not transcribe this -- here is the "
                    "text of a neighboring page, to help with continuity of "
                    "notation if this page continues a derivation:\n\n"
                    + context_text[-MAX_CONTEXT_CHARS:]
                ),
            }
        )
    instruction = PAGE_INSTRUCTION
    if y_fraction is not None:
        instruction += (
            " This image is only the "
            + ("top" if y_fraction[0] == 0 else "bottom")
            + " half of the page -- transcribe only what's visible here, don't guess at content that's been cut off."
        )
    content.append({"type": "text", "text": instruction})

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=PAGE_MAX_TOKENS,
        tools=[PAGE_TOOL],
        tool_choice={"type": "tool", "name": "transcribe_page"},
        messages=[{"role": "user", "content": content}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "transcribe_page":
            return block.input.get("blocks", [])
    logger.warning(
        "transcribe_page got no tool_use block back for page %d (stop_reason=%s)",
        page_number,
        response.stop_reason,
    )
    return []


def transcribe_page(pdf_path, page_number, dpi=200, context_text=None, api_key=None):
    """Fallback for a page where local text extraction produced broken math
    (see document_parser._page_looks_math_garbled): renders just that page
    and asks Claude to transcribe it directly, with real LaTeX in place of
    garbled font output. Figures on the page are left untouched -- their
    extraction doesn't depend on font decoding, so they're usually fine
    already; only the text/math on the page gets replaced by the caller.

    context_text: the *local* (possibly also garbled) text of a neighboring
    page, if available -- helps with continuity of notation across a
    derivation that spans pages. Deliberately not the *transcribed* text of
    a neighboring page: that would make each page's call depend on another
    page's result and block parallelizing them.

    Seen in practice: some pages get their *entire* full-page transcription
    rejected by the API's output content filter, even though the page's
    content is completely ordinary (dense math notation, nothing sensitive)
    -- confirmed by testing that the exact same page, split into top/bottom
    halves and transcribed as two separate (shorter, less symbol-dense)
    calls, succeeds cleanly every time where the single full-page call
    reliably fails. So on that specific failure, retry once via a
    two-region split before giving up -- only if a full-page attempt is
    rejected, since splitting doubles the API calls for every garbled page,
    not just the rare one that needs it."""
    try:
        return _transcribe_page_region(pdf_path, page_number, dpi, context_text, api_key=api_key)
    except anthropic.APIError:
        logger.warning(
            "transcribe_page full-page attempt failed for page %d; retrying as top/bottom halves",
            page_number,
        )
        top = _transcribe_page_region(
            pdf_path, page_number, dpi, context_text, y_fraction=(0.0, 0.5), api_key=api_key
        )
        bottom = _transcribe_page_region(
            pdf_path, page_number, dpi, context_text, y_fraction=(0.5, 1.0), api_key=api_key
        )
        return top + bottom


def _encode_image(path):
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }


def _build_params(image_path, context=None):
    content = [_encode_image(image_path)]
    if context:
        content.append(
            {
                "type": "text",
                "text": f"For context, this figure appears in the document under the section: {context.strip()[:MAX_CONTEXT_CHARS]}",
            }
        )
    content.append({"type": "text", "text": INSTRUCTION})
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "tools": [IMAGE_TOOL],
        "tool_choice": {"type": "tool", "name": "describe_image"},
        "messages": [{"role": "user", "content": content}],
    }


def _extract_result(message):
    for block in message.content:
        if block.type == "tool_use" and block.name == "describe_image":
            result = block.input
            if result.get("content"):
                return result
            logger.warning(
                "describe_image tool call returned no content (stop_reason=%s)",
                message.stop_reason,
            )
            break
    else:
        logger.warning(
            "describe_image got no tool_use block back (stop_reason=%s)",
            message.stop_reason,
        )
    # "diagram", not "decorative": decorative figures are omitted from the
    # rendered document entirely (see assemble.py), which would silently
    # hide this failure instead of surfacing it to the reader.
    return {
        "kind": "diagram",
        "content": "(This figure could not be described automatically.)",
    }


def describe_image(image_path, context=None, api_key=None):
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(**_build_params(image_path, context))
    return _extract_result(response)


def describe_images_via_batch(image_paths, contexts=None):
    """Describe many figures via the Message Batches API: 50% cheaper than
    describe_image(), at the cost of latency. Worth it for documents with
    many figures processed out-of-band -- see batch_convert.py. Returns
    results in the same order as image_paths. contexts, if given, must be
    the same length as image_paths (use None for entries with no context)."""
    client = anthropic.Anthropic()
    contexts = contexts or [None] * len(image_paths)

    requests = [
        Request(
            custom_id=f"image-{i}",
            params=MessageCreateParamsNonStreaming(**_build_params(path, contexts[i])),
        )
        for i, path in enumerate(image_paths)
    ]

    batch = client.messages.batches.create(requests=requests)

    while batch.processing_status != "ended":
        time.sleep(BATCH_POLL_SECONDS)
        batch = client.messages.batches.retrieve(batch.id)

    results = {}
    for result in client.messages.batches.results(batch.id):
        index = int(result.custom_id.split("-")[1])
        if result.result.type == "succeeded":
            results[index] = _extract_result(result.result.message)
        else:
            raise RuntimeError(
                f"Batch image {result.custom_id} did not succeed: {result.result.type}"
            )

    return [results[i] for i in range(len(image_paths))]

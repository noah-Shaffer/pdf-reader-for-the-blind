# STEM-ACCESS

Turn a PDF into a screen-reader-accessible HTML page.

- **Headings, paragraphs, and tables** are extracted directly from the PDF's own structure (free, local, instant) — no API call.
- **Figures** (charts, diagrams, photos) are cropped out and sent to Claude, which classifies each one and either transcribes it (a formula rendered as an image → LaTeX → real MathML) or describes it for a blind reader (a chart → a real data table; a meaningful diagram → a descriptive paragraph). Purely decorative images (cover art, gradients, logos) are detected and **omitted** from the output entirely, not narrated.
- **Math is converted to real MathML from three different source patterns**, each with a different root cause: math typeset with a broken font encoding (symptoms: replacement characters, bracket-soup exponents) is re-transcribed by sending that page's image to Claude — and if that full-page transcription is rejected by the API (seen in testing: some pages' *length/density* alone trips an output content filter, regardless of the page's actual content), it's automatically retried as two shorter top-half/bottom-half calls before falling back to anything, which has resolved every case seen so far; literal LaTeX source sitting in the extracted text — `$...$`-delimited, `\(...\)`-delimited (LaTeX's own native inline-math syntax, a second convention some documents use alongside or instead of `$...$`), and bare, undelimited commands (`\cdots`, `S_n`, `\frac{3}{10}`, `\sqrt[4]{-64}`) — is detected and converted in place; and multi-line derivations (`\begin{aligned}...\end{aligned}`) are split per line and converted separately, since the underlying converter doesn't understand alignment markers. That per-line split is fault-tolerant: one unconvertible line (an unsupported command, a `\rule{...}` divider) falls back on its own instead of discarding an otherwise-good multi-line derivation wholesale. Spacing-only macros (`\quad`, `\,`, `\\[6pt]`'s extra-vertical-space argument) are stripped rather than left to leak as literal syntax, an equation-numbering `\tag{...}` is dropped as cosmetic, triple-prime notation (`f'''(x)`, routine for third derivatives) is rewritten into a form the converter accepts instead of erroring out entirely, and a `\textit{...}`/`\textbf{...}` command nested inside an outer `\text{...}` block (a document italicizing one word inside an in-math aside) is flattened to plain text rather than tripping a converter bug where the nested command's own closing brace gets mistaken for the outer block's, corrupting everything after it.
- **Common PDF extraction artifacts are cleaned up**: broken ligatures (`di!erent` → `different`), lingering Markdown syntax rendered as real HTML instead of literal asterisks, and the print table of contents / running headers / page numbers (including ones that get arrow-decorated running heads, e.g. `"14 ▶ Infinite Series"`) are detected and stripped rather than read aloud as garbage.
- **Consecutive numbered items become one real list.** A run of sequentially-numbered paragraphs (a homework problem set, an exercise list) is merged into a single `<ol>` a screen reader user can arrow through, instead of each item being announced as its own separate one-item list.
- **Figures get the source document's own captions** when one is nearby ("Fig. 1.1 ...") instead of an invented label, and each figure/page-transcription call gets a bit of surrounding context (the nearest heading, or a neighboring page's text) to help Claude interpret it correctly.
- **A real, accessible table of contents** is generated from the document's actual headings — tabbable anchor links that jump to each section, not the source PDF's print ToC (which gets stripped, since dot-leaders and page numbers read as garbage to a screen reader and are superseded by real in-document navigation anyway). Entries whose text is duplicated elsewhere in the document (a generic heading like "Example 2." reused across chapters) get their parent chapter appended so they're distinguishable out of context, e.g. in a screen reader's "links list" view.
- Figure-description and page-transcription calls run **concurrently** (not one at a time), so conversion time scales with how much time the slowest single call takes, not the sum of all of them.

## Requirements

- Python 3.9+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))

## Setup

```bash
cd STEM-ACCESS
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and paste in your key:
#   ANTHROPIC_API_KEY=sk-ant-...
```

## Running it

```bash
source .venv/bin/activate
cd app
python3 app.py
```

Open **http://127.0.0.1:5000/upload**, choose a PDF, submit, and wait — conversion time depends on how many figures the document has and how many pages need the math-transcription fallback (these run concurrently, but each individually takes a few seconds). The page confirms your submission immediately and announces it to screen readers too.

Once conversion finishes:
- The result renders directly in the browser, with a table of contents at the top for quick navigation.
- The full converted document is **automatically saved** to `app/uploads/<job>/<name>_accessible.html`, and a **Download this page as an HTML file** link lets you save it wherever you like via your browser's normal save dialog. Clicking it also announces "Downloading..." through the same kind of live status region as the upload form — a screen reader user gets a confirmation that something happened, not just the browser's own (visual-only) download indicator.

## Converting a large document

For documents with many figures where you don't need the result immediately, `batch_convert.py` submits all figure-description calls through Anthropic's Message Batches API — **50% cheaper**, at the cost of latency (typically under an hour, up to 24 hours):

```bash
cd app
python3 batch_convert.py path/to/document.pdf path/to/output.html
```

Text, headings, tables, and the math-transcription fallback are still parsed the same way either way (locally, plus concurrent Claude calls for garbled pages) — batching only changes how the *figure-description* calls are made.

## Tools & libraries

| Tool | Used for |
|---|---|
| [Flask](https://flask.palletsprojects.com/) | Web app: the `/upload` routes, rendering, file download |
| [Werkzeug](https://werkzeug.palletsprojects.com/) | Comes with Flask; used directly for `secure_filename` and serving the downloadable file |
| [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) | Low-level PDF access — rendering a single page to an image for the math-transcription fallback |
| [pymupdf4llm](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) | The core local extraction: reading order, headings, tables, and cropped figure images straight from the PDF's structure, no API call |
| [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) (Claude Sonnet 5) | Classifying and describing figures, transcribing math-garbled pages, and (optionally) the Message Batches API for bulk/cheaper runs |
| [latex2mathml](https://pypi.org/project/latex2mathml/) | Converts LaTeX (figure transcriptions, `$...$` spans, bare LaTeX tokens, and aligned-environment derivations found in prose) into real MathML |
| [Python-Markdown](https://python-markdown.github.io/) | Renders Markdown tables into HTML. Inline `**bold**`/`_italic_` text formatting is handled separately, by a small hand-written regex converter — not this library — specifically because its block-level parser mis-fires on ordinary text that happens to start with a numbered-list-shaped prefix ("1. The Geometric Series", a real heading, not a list) |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | Loads `ANTHROPIC_API_KEY` from `.env` |
| `concurrent.futures` (Python standard library) | Runs independent Claude calls (figure descriptions, page transcriptions) concurrently instead of one at a time |

Full list with exact install names: [`requirements.txt`](requirements.txt).

## Project structure

```
app/
  app.py                Flask routes: GET/POST /upload, download route
  document_parser.py    Local extraction (pymupdf4llm): headings, paragraphs,
                         tables, cropped figures; fixes broken ligatures;
                         detects and strips print ToCs and running
                         headers/footers (including arrow-decorated ones
                         misdetected as real headings); detects
                         math-garbled pages and re-transcribes them via
                         Claude vision, in parallel; attaches real captions
                         and section context to figures; merges consecutive
                         numbered paragraphs into one real ordered list
  vision_client.py      Claude calls: classify + describe a figure, or
                         transcribe a math-garbled page -- live/concurrent
                         or in bulk via the Batches API
  assemble.py            Maps parsed blocks to semantic, accessible HTML;
                         converts $...$ spans, bare LaTeX tokens, and
                         \begin{aligned}-style derivations to real MathML,
                         rejecting conversions that would silently produce
                         broken output; builds the in-document table of
                         contents, disambiguating duplicated heading text
                         with its parent chapter
  batch_convert.py       CLI for bulk/large-document conversion via Batches
  templates/
    upload.html           Accessible upload form, with submit confirmation
    result.html            Accessible result page, with a download link
  uploads/                 Uploaded PDFs + extracted figures (gitignored)
requirements.txt
.env.example
```

Want to understand *how* each piece works and *why* it's accessible? See the in-depth walkthrough: [How STEM-ACCESS Works](https://claude.ai/code/artifact/732c03f5-c0b2-4e8f-b37a-f91ed3e600a5) (note: written early in the project's development, before most of the fixes described above — the math-conversion pipeline, list/ToC structure fixes, and running-header detection have all changed substantially since).

## Known limitations

- **Table detection needs real borders/gridlines.** `pymupdf4llm`'s default table detection looks for a bordered/gridded structure in the PDF. A borderless, whitespace-aligned table (no visible lines) won't be recognized and will come through as plain paragraph text instead. Most real documents use bordered tables, so this is usually fine — but worth knowing if a document uses an unusual table style.
- **Figures are classified by Claude, not guaranteed correct.** Occasionally a crop that's actually decorative might get described as if it had data, or vice versa — spot-check output on documents where this matters. A figure classified as decorative is omitted from the output entirely, so a misclassification there means a real figure silently doesn't appear.
- **The math-garbled-page fallback and the figure-description calls can both fail individually without failing the whole document.** Seen in testing: a page transcription or a figure description can be rejected outright by an unrelated API content-filter false-positive — confirmed, on a page with completely ordinary math content, to be a false positive specific to the *length/density* of a full-page transcription: the same page split into top and bottom halves and transcribed as two shorter calls succeeds every time. So a page transcription that gets rejected is automatically retried once as two half-page calls before falling back to anything — that alone resolves it in every case seen so far. If that retry *also* fails (or a figure description fails, which isn't split-retried), the failure is handled per-item rather than per-document — a page falls back to its original (still garbled) local text, and a figure gets a "(This figure could not be described automatically.)" placeholder — so a document with many pages/figures can come out with almost everything converted and just one or two spots still showing the fallback. The **"The document description service returned an error"** page-level error now only appears for something document-wide (a missing/invalid `ANTHROPIC_API_KEY`, the Anthropic API being unreachable), not a single figure or page hiccup.
- **Not every LaTeX construct converts.** `latex2mathml` doesn't understand every command or every environment, and a handful of known gaps are worked around explicitly (spacing macros, `\tag{...}`, triple-prime notation, `\rule{...}` dividers, `\(...\)` delimiters, nested `\textit{...}`/`\textbf{...}` inside `\text{...}` — see the math bullet above). Whatever's left is detected and falls back to plain escaped text rather than showing broken/misleading MathML — readable, but not real math structure.
- **The table of contents can include front-matter text.** Cover/title-page text (book title, author name, edition) is sometimes large enough in the source PDF to trip the same heading-detection heuristic as real section headings, so it can show up as ToC entries alongside genuine content headings — and if that title text repeats (e.g. on both a cover and title page), it can also trigger the duplicate-heading disambiguation, appending an unhelpful "parent" fragment. There's no clean automated way to distinguish "large text because it's a title page" from "large text because it's a real heading" — ask if you want the ToC to start from the first real chapter heading instead.
- **Multi-line table-of-contents entries can leave a stray fragment.** The print ToC's dot-leader lines are detected and stripped, but if a long entry wraps across two lines in the extracted text, only the line ending in dots-and-a-page-number matches — the wrapped title fragment on the line before it can survive as an orphaned partial sentence.
- **No async job queue.** The web upload form is synchronous — for documents with many figures, the browser tab will wait through the conversion (concurrent calls make this faster than it was, but it still blocks). Use `batch_convert.py` for large jobs instead of the web form.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Could not read that PDF" | The file is corrupted, password-protected, or not actually a PDF |
| "The document description service returned an error" | `ANTHROPIC_API_KEY` is missing/invalid, or the Anthropic API is temporarily unavailable — check `.env` and the app's console logs. A single figure or page failing no longer triggers this on its own (see Known limitations above), so if you see it, something's wrong document-wide |
| A table renders as a wall of plain text instead of a `<table>` | The source table has no visible borders/gridlines — see Known limitations above |
| A formula/equation still shows garbled bracket-and-symbol text instead of real math | That page's Claude-vision transcription fallback failed (check the console logs for a warning) — see Known limitations above |
| A figure shows "(This figure could not be described automatically.)" | That one figure's Claude description call failed (check the console logs for a warning) — the rest of the document still converted normally — see Known limitations above |
| A formula/equation shows as plain escaped LaTeX text (backslashes and braces spelled out) instead of MathML | `latex2mathml` doesn't support that specific construct — see "Not every LaTeX construct converts" above |
| The table of contents has entries that don't look like real sections, or an odd "(...)" suffix on a front-matter entry | Front-matter text (title page, author) got picked up as headings — see Known limitations above |
| Conversion is slow | Figure descriptions and page transcriptions run concurrently, but a document with many figures or many math-garbled pages still takes real time. Consider `batch_convert.py` for large jobs |

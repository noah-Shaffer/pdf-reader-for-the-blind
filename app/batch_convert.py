"""Convert a PDF to accessible HTML. Text, headings, and tables are parsed
locally (free, instant, via pymupdf4llm) -- Claude is only used to describe
figures, and only via the Message Batches API (50% cheaper than the
synchronous Flask upload flow, at the cost of latency: typically under an
hour, up to 24h max). Useful for documents with many figures processed
out-of-band rather than interactively.

Usage:
    python batch_convert.py path/to/document.pdf path/to/output.html
"""
import argparse
import logging
import os

from dotenv import load_dotenv

from document_parser import extract_pages, parse_blocks
from vision_client import describe_images_via_batch
from assemble import blocks_to_html

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", help="Path to the input PDF")
    parser.add_argument("output_path", help="Path to write the resulting HTML file")
    args = parser.parse_args()

    image_dir = os.path.join(os.path.dirname(os.path.abspath(args.output_path)), "images")

    logger.info("Parsing PDF locally...")
    pages = extract_pages(args.pdf_path, image_dir=image_dir)
    blocks = parse_blocks(pages, pdf_path=args.pdf_path)
    image_blocks = [b for b in blocks if b["type"] == "image"]
    image_paths = [b["path"] for b in image_blocks]
    contexts = [b.get("section_context") for b in image_blocks]
    logger.info("Parsed %d blocks (%d figures)", len(blocks), len(image_paths))

    image_results = {}
    if image_paths:
        logger.info("Submitting %d figures to the Batches API...", len(image_paths))
        results = describe_images_via_batch(image_paths, contexts=contexts)
        image_results = dict(zip(image_paths, results))
        logger.info("Batch complete.")

    content_html = blocks_to_html(blocks, image_results=image_results)

    with open(args.output_path, "w") as f:
        f.write(content_html)
    logger.info("Wrote %s", args.output_path)


if __name__ == "__main__":
    main()

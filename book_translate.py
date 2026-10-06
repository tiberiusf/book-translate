#!/usr/bin/env python3
"""
book_translate.py: translate a book PDF into another language, keeping tone and humor.

Pipeline
  1. Extract text from the input PDF (PyMuPDF), rebuild paragraphs, drop page numbers.
  2. Style pass: Claude reads the opening and writes a short style guide plus a
     glossary (names, nicknames, invented words, running jokes) with chosen renderings.
  3. Translate one whole chapter per call (chapters are found from headings such as
     "Глава 3"; very long chapters, or books without headings, are split at paragraph
     boundaries). Every call gets the style guide, the glossary and the end of the
     previous translated chapter, so voice and names stay consistent.
  4. Revision pass: a second call compares source and draft and fixes lost jokes,
     flattened voice, mistranslations, unnatural phrasing and glossary drift.
  5. Typeset the result as a clean PDF (full Romanian diacritics).

Progress is saved after every step (<output>.progress.json), so an interrupted run
resumes where it stopped.

Use only for public-domain works, works you hold rights to, or uses covered by fair use.

Choosing the model (in order of precedence)
  1. --model claude-fable-5-1                 for one run
  2. export BOOK_TRANSLATE_MODEL=claude-fable-5-1   for every run in this shell
  3. the DEFAULT_MODEL line below             to change the built-in default
  Model IDs: https://docs.claude.com/en/docs/about-claude/models
  --revise-model sets a different model for the revision pass (defaults to --model).

Usage
  export ANTHROPIC_API_KEY=sk-ant-...
  book-translate input.pdf output.pdf                       # ru -> ro, with revision pass
  book-translate input.pdf output.pdf --pages 1-20          # try a sample first
  book-translate input.pdf output.pdf --no-revise           # cheaper, single pass
  book-translate input.pdf output.pdf --audience "kids 5-8" --source ru --target ro
  book-translate input.pdf output.pdf --dry-run             # no API calls, tests layout

Requires: pip install pymupdf reportlab anthropic
Scanned PDFs: pages without a text layer are read with Claude vision automatically (--ocr auto).
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pymupdf
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape

LANG_NAMES = {"ru": "Russian", "ro": "Romanian", "en": "English", "uk": "Ukrainian",
              "fr": "French", "de": "German", "it": "Italian", "es": "Spanish"}
# ==== MODEL: change this line to switch the built-in default ====
DEFAULT_MODEL = os.environ.get("BOOK_TRANSLATE_MODEL", "claude-opus-5-5")
# Chapters longer than this are split at paragraph boundaries (keeps output within limits).
MAX_CHAPTER_WORDS = 7000
MAX_OUTPUT_TOKENS = 32000
HEADING_RE = re.compile(r"^(глава|часть|chapter|part|capitolul|partea)\b|^[IVXLC]+\.?$|^\d{1,3}\.?$",
                        re.IGNORECASE)


# ---------- 1. extraction ----------

def parse_pages(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        out.extend(range(lo - 1, min(hi, n)))
    return out


def extract_paragraphs(pdf_path, pages_spec=None, ocr=None, ocr_mode="auto", ocr_cache=None,
                       on_ocr=None):
    """ocr: function(png_bytes) -> text, used for pages without a text layer (or all pages
    when ocr_mode == "force"). ocr_cache: dict page_index -> text, so reruns don't re-OCR."""
    doc = pymupdf.open(pdf_path)
    paras, buf = [], ""
    ocr_cache = {} if ocr_cache is None else ocr_cache
    page_list = parse_pages(pages_spec, doc.page_count)
    for i in page_list:
        page = doc[i]
        blocks = [b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0]
        has_text = len("".join(blocks).strip()) >= 30
        if ocr and ocr_mode != "off" and (ocr_mode == "force" or not has_text):
            key = str(i)
            if key not in ocr_cache:
                print(f"OCR page {i + 1} ({page_list.index(i) + 1}/{len(page_list)})...", flush=True)
                png = page.get_pixmap(dpi=200).tobytes("png")
                ocr_cache[key] = ocr(png)
                if on_ocr:
                    on_ocr()
            blocks = re.split(r"\n\s*\n", ocr_cache[key])
        for raw in blocks:
            text = raw.strip()
            if not text or re.fullmatch(r"[\d\s\-–—.]+", text):   # page numbers
                continue
            # pull chapter headings out onto their own paragraph
            lines = [l.strip() for l in text.split("\n")]
            pieces, cur = [], []
            for l in lines:
                if HEADING_RE.match(l) and len(l) < 60:
                    if cur:
                        pieces.append("\n".join(cur))
                    pieces.append(l)
                    cur = []
                else:
                    cur.append(l)
            if cur:
                pieces.append("\n".join(cur))
            for piece in pieces:
                # join hyphenated line breaks, then remaining line breaks
                piece = re.sub(r"(\w)[-\u00ad]\n(\w)", r"\1\2", piece)
                piece = re.sub(r"\s*\n\s*", " ", piece).strip()
                if not piece:
                    continue
                # a block starting lowercase continues the previous one (page break mid-paragraph)
                if buf and piece[:1].islower() and not HEADING_RE.match(buf):
                    buf += " " + piece
                else:
                    if buf:
                        paras.append(buf)
                    buf = piece
    if buf:
        paras.append(buf)
    doc.close()
    if sum(len(p) for p in paras) < 200:
        sys.exit("Almost no text found, even after OCR. Check the PDF, or try --ocr force.")
    return paras


def split_chapters(paras, max_words=MAX_CHAPTER_WORDS):
    """One unit per chapter; over-long chapters (or heading-less books) split at paragraphs."""
    chapters, cur = [], []
    for p in paras:
        if HEADING_RE.match(p) and len(p) < 60 and cur:
            chapters.append(cur)
            cur = []
        cur.append(p)
    if cur:
        chapters.append(cur)
    # short front matter before the first heading rides along with chapter 1
    if len(chapters) > 1 and sum(len(p.split()) for p in chapters[0]) < 300:
        chapters[1] = chapters[0] + chapters[1]
        chapters.pop(0)

    def split_long_paragraph(p, limit):
        # PDFs without paragraph spacing can extract as one huge block; cut at sentence ends
        if len(p.split()) <= limit:
            return [p]
        out, cur = [], ""
        for s in re.split(r"(?<=[.!?…»])\s+", p):
            if cur and len((cur + " " + s).split()) > limit:
                out.append(cur)
                cur = s
            else:
                cur = (cur + " " + s).strip()
        return out + ([cur] if cur else [])

    units = []
    for ch in chapters:
        ch = [piece for p in ch for piece in split_long_paragraph(p, max(max_words // 3, 200))]
        words = sum(len(p.split()) for p in ch)
        if words <= max_words:
            units.append(ch)
            continue
        n_parts = -(-words // max_words)          # ceil
        target = words / n_parts
        part, n = [], 0
        for p in ch:
            w = len(p.split())
            heading_only = len(part) == 1 and HEADING_RE.match(part[0])
            if part and n + w > target and not heading_only:
                units.append(part)
                part, n = [], 0
            part.append(p)
            n += w
        if part:
            units.append(part)
    return units


# ---------- 2 & 3. Claude calls ----------

class Translator:
    def __init__(self, src, tgt, audience, model, revise_model, dry_run):
        self.src, self.tgt = LANG_NAMES.get(src, src), LANG_NAMES.get(tgt, tgt)
        self.tgt_code = tgt
        self.audience, self.model, self.dry_run = audience, model, dry_run
        self.revise_model = revise_model or model
        if not dry_run:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                sys.exit("ANTHROPIC_API_KEY is not set. Run: export ANTHROPIC_API_KEY=sk-ant-...")
            self.anthropic = anthropic
            self.client = anthropic.Anthropic(api_key=api_key)

    def _call(self, system, user, max_tokens=MAX_OUTPUT_TOKENS, model=None):
        for attempt in range(5):
            try:
                with self.client.messages.stream(
                    model=model or self.model, max_tokens=max_tokens,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user}],
                ) as stream:
                    msg = stream.get_final_message()
                if msg.stop_reason == "max_tokens":
                    sys.exit("Output was cut off at the token limit; rerun with a lower "
                             "--max-chapter-words (e.g. 4000) after deleting the .progress.json file.")
                return "".join(b.text for b in msg.content if b.type == "text").strip()
            except (self.anthropic.NotFoundError, self.anthropic.AuthenticationError,
                    self.anthropic.PermissionDeniedError, self.anthropic.BadRequestError) as e:
                sys.exit(f"API rejected the request (check the model name and API key): {e}")
            except Exception as e:  # rate limits, overload, network
                wait = 10 * 2 ** attempt
                print(f"  API error ({e}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
        sys.exit("Giving up after repeated API errors; rerun to resume.")

    def ocr_page(self, png_bytes):
        if self.dry_run:
            return ("[OCR dry run] " + "The transcribed text of this page would appear here. " * 5).strip()
        import base64
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png_bytes).decode()}},
            {"type": "text", "text": f"""Transcribe the {self.src} text on this book page exactly, character for character.
- One paragraph per block, blocks separated by a blank line; join lines inside a paragraph and rejoin words hyphenated across lines.
- Chapter titles on their own line.
- Skip running headers, page numbers and illustration captions.
- If the page has no text, output nothing.
Output only the transcription."""},
        ]
        return self._call("You are a precise OCR transcriber.", content, max_tokens=8000)

    def style_guide(self, sample):
        if self.dry_run:
            return "STYLE (dry run)\nGLOSSARY (dry run)"
        system = (f"You are a senior literary translator from {self.src} into {self.tgt}, "
                  f"specializing in humorous and children's literature.")
        user = f"""Read this opening of a book and prepare notes for translating the whole book into {self.tgt}.
Audience: {self.audience}.

Write two sections, plain text, under 600 words total:

STYLE
- narrator's voice, register, sentence rhythm, how the humor works (situational, wordplay,
  child logic, irony), dialogue habits, era/setting details to keep or gently explain.
- a short list of concrete rules for the translation.

GLOSSARY
- one line per proper name, nickname, diminutive, invented word, recurring phrase or joke:
  original -> chosen {self.tgt} rendering (with a few words of reasoning when not obvious).
  Keep names recognizable; choose diminutives that sound natural in {self.tgt}.

OPENING:
{sample}"""
        return self._call(system, user, max_tokens=4000)

    def translate(self, text, guide, prev_tail):
        if self.dry_run:
            return "\n\n".join(f"[{self.tgt_code}] {p}" for p in text.split("\n\n"))
        system = f"""You are a senior literary translator from {self.src} into {self.tgt}, specializing in humorous and children's literature. Audience: {self.audience}.

Goal: a translation a native {self.tgt} reader enjoys as if the book were written in {self.tgt}, with the same laughs in the same places.
- Preserve meaning, tone, voice, pacing and every joke. When a joke depends on wordplay that cannot survive literally, recreate an equivalent joke in {self.tgt} that fits the scene.
- Natural, idiomatic {self.tgt}; never calque {self.src} syntax. Dialogue should sound spoken.
- Follow the style guide and glossary exactly; keep names consistent.
- Keep the paragraph structure: same number of paragraphs, separated by one blank line. Chapter headings stay on their own line.
- Use correct orthography and diacritics. For Romanian: ă â î ș ț (comma-below, not cedilla) and Romanian quotation marks „...”, dialogue dashes as in the source.
- Output only the translation. No notes, no commentary.

STYLE GUIDE AND GLOSSARY:
{guide}"""
        user = ""
        if prev_tail:
            user += f"For continuity, the previous chapter ended (already translated):\n<<<\n{prev_tail}\n>>>\n\n"
        user += f"Translate this chapter:\n<<<\n{text}\n>>>"
        out = self._call(system, user)
        return out.strip().strip("<>").strip()

    def revise(self, source, draft, guide):
        if self.dry_run:
            return draft
        system = f"""You are the editor of a {self.tgt} literary translation of a humorous {self.src} book. Audience: {self.audience}. You are a native {self.tgt} speaker and an expert in {self.src}.

Compare the draft translation with the source, then produce the final, revised translation of the whole chapter. Check, in this order:
1. Accuracy: nothing omitted, added or misunderstood (idioms, irony, who says what).
2. Humor: every joke lands in {self.tgt}, in the same spot. If a pun or wordplay was translated literally and died, recreate a working equivalent.
3. Voice: the narrator's personality, rhythm and quirks survive; undo any smoothing into generic, tidy prose. Dialogue sounds spoken.
4. Naturalness: no calques of {self.src} syntax or word order; idiomatic {self.tgt} a native reader would not notice as translated. It should read well aloud.
5. Consistency with the style guide and glossary (names, nicknames, recurring phrases).
6. Orthography: correct diacritics (for Romanian: ă â î ș ț with comma-below), quotation marks and dialogue punctuation.

Change only what improves the text; keep good passages as they are. Keep the same paragraphs, separated by one blank line, headings on their own line.
Output only the final translation. No notes, no commentary, no list of changes.

STYLE GUIDE AND GLOSSARY:
{guide}"""
        user = (f"SOURCE ({self.src}):\n<<<\n{source}\n>>>\n\n"
                f"DRAFT TRANSLATION ({self.tgt}):\n<<<\n{draft}\n>>>")
        out = self._call(system, user, model=self.revise_model)
        return out.strip().strip("<>").strip()


# ---------- 4. typesetting ----------

def find_font():
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        ("/Library/Fonts/Times New Roman.ttf", "/Library/Fonts/Times New Roman Bold.ttf"),
        ("/System/Library/Fonts/Supplemental/Times New Roman.ttf",
         "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
        ("C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/timesbd.ttf"),
    ]
    for reg, bold in candidates:
        if os.path.exists(reg):
            return reg, bold if os.path.exists(bold) else reg
    sys.exit("No Unicode serif font found; pass --font /path/to/font.ttf")


def build_pdf(paragraphs, out_path, title, font_path=None):
    reg, bold = (font_path, font_path) if font_path else find_font()
    pdfmetrics.registerFont(TTFont("Body", reg))
    pdfmetrics.registerFont(TTFont("BodyBold", bold))
    body = ParagraphStyle("body", fontName="Body", fontSize=11, leading=15.5,
                          alignment=TA_JUSTIFY, firstLineIndent=5 * mm, spaceAfter=2)
    head = ParagraphStyle("head", fontName="BodyBold", fontSize=15, leading=20,
                          alignment=TA_CENTER, spaceBefore=6 * mm, spaceAfter=6 * mm)
    ttl = ParagraphStyle("title", parent=head, fontSize=20, leading=26, spaceBefore=40 * mm)

    story = [Paragraph(escape(title), ttl), PageBreak()]
    for p in paragraphs:
        if HEADING_RE.match(p) or (len(p) < 60 and p.isupper()):
            story += [Spacer(1, 4 * mm), Paragraph(escape(p), head)]
        else:
            story.append(Paragraph(escape(p), body))

    def page_no(canvas, doc):
        canvas.setFont("Body", 9)
        canvas.drawCentredString(A5[0] / 2, 10 * mm, str(doc.page))

    SimpleDocTemplate(str(out_path), pagesize=A5, leftMargin=16 * mm, rightMargin=16 * mm,
                      topMargin=16 * mm, bottomMargin=18 * mm, title=title
                      ).build(story, onLaterPages=page_no)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--source", default="ru")
    ap.add_argument("--target", default="ro")
    ap.add_argument("--audience", default="children aged 5-10 and the parents reading aloud to them")
    ap.add_argument("--pages", help="e.g. 1-20 or 3,5,9-12 (1-based)")
    ap.add_argument("--title", help="title for the output PDF (default: input file name)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"model ID (default: {DEFAULT_MODEL}; or set BOOK_TRANSLATE_MODEL)")
    ap.add_argument("--revise-model", help="model for the revision pass (default: same as --model)")
    ap.add_argument("--no-revise", action="store_true", help="skip the revision pass (cheaper, faster)")
    ap.add_argument("--max-chapter-words", type=int, default=MAX_CHAPTER_WORDS,
                    help=f"split chapters longer than this (default {MAX_CHAPTER_WORDS})")
    ap.add_argument("--ocr", choices=["auto", "force", "off"], default="auto",
                    help="auto: OCR pages with no text layer (scans) via Claude vision; "
                         "force: OCR every page (use when the PDF's text layer is garbage); off: never")
    ap.add_argument("--font", help="TTF font with full diacritics")
    ap.add_argument("--dry-run", action="store_true", help="skip API calls; tests extraction and layout")
    args = ap.parse_args()

    out = Path(args.output)
    progress_path = out.with_suffix(out.suffix + ".progress.json")
    state = json.loads(progress_path.read_text()) if progress_path.exists() else {}

    tr = Translator(args.source, args.target, args.audience, args.model, args.revise_model, args.dry_run)

    def save():
        progress_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    ocr_cache = state.setdefault("ocr", {})
    paras = extract_paragraphs(args.input, args.pages, ocr=tr.ocr_page, ocr_mode=args.ocr,
                               ocr_cache=ocr_cache, on_ocr=save)
    if ocr_cache:
        print(f"{len(ocr_cache)} page(s) read with OCR")
    units = split_chapters(paras, args.max_chapter_words)
    words = sum(len(p.split()) for p in paras)
    print(f"~{words} words in {len(units)} chapter(s)/part(s). "
          f"Model: {args.model}" + ("" if args.no_revise else f", revision: {args.revise_model or args.model}"))
    if state.get("n_units") not in (None, len(units)):
        sys.exit(f"{progress_path} is from a different run; delete it to start over.")

    if "guide" not in state:
        sample = "\n\n".join(p for u in units[:2] for p in u)[:15000]
        print("Writing style guide and glossary...")
        state.update(guide=tr.style_guide(sample), n_units=len(units), done=[], draft=None)
        save()
        print(state["guide"], "\n")

    done = state["done"]
    for i in range(len(done), len(units)):
        label = f"{i + 1}/{len(units)}"
        source = "\n\n".join(units[i])
        if not state.get("draft"):
            print(f"Translating {label}...", flush=True)
            prev_tail = done[-1][-2000:] if done else ""
            state["draft"] = tr.translate(source, state["guide"], prev_tail)
            save()
        if args.no_revise:
            final = state["draft"]
        else:
            print(f"Revising {label}...", flush=True)
            final = tr.revise(source, state["draft"], state["guide"])
        done.append(final)
        state["draft"] = None
        save()

    out_paras = [p.strip() for t in done for p in re.split(r"\n\s*\n", t) if p.strip()]
    build_pdf(out_paras, out, args.title or Path(args.input).stem, args.font)
    guide_path = out.with_suffix(".glossary.txt")
    guide_path.write_text(state["guide"])
    progress_path.unlink()
    print(f"Done: {out}  (style guide and glossary: {guide_path})")


if __name__ == "__main__":
    main()

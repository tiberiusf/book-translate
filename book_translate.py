#!/usr/bin/env python3
"""
book_translate.py: translate a book PDF into another language, keeping tone and humor.

Pipeline
  1. Extract text from the input PDF (PyMuPDF), rebuild paragraphs, drop page numbers.
     Headings are recognized by font size (book title / part / section) and carried
     through translation as #, ## and ### markers.
  2. Style pass: Claude reads the opening and writes a short style guide plus a
     glossary (names, nicknames, invented words, running jokes) with chosen renderings.
  3. Translate one whole chapter per call (chapters are found from headings; short
     ones, such as one-page stories, are packed together; very long chapters, or books
     without headings, are split at paragraph boundaries). Every call gets the style guide, the glossary and the end of the
     previous translated chapter, so voice and names stay consistent.
  4. Revision pass: a second call compares source and draft and fixes lost jokes,
     flattened voice, mistranslations, unnatural phrasing and glossary drift.
  5. Typeset the result as a clean PDF (full Romanian diacritics), with the translated
     title and author on the cover and proper chapter headings.

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
from collections import Counter
from pathlib import Path

import pymupdf
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import CondPageBreak, PageBreak, Paragraph, SimpleDocTemplate, Spacer
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
# Headings travel through the pipeline as Markdown-style markers:
# "# " book title, "## " part or chapter, "### " section or story.
MARKED_RE = re.compile(r"^(#{1,3})[ \t]+(.+)$", re.S)


def heading_level(p):
    """1-3 for a paragraph marked as a heading, 0 for body text."""
    m = MARKED_RE.match(p)
    return len(m.group(1)) if m else 0


def heading_text(p):
    m = MARKED_RE.match(p)
    return m.group(2).strip() if m else p


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


def _page_lines(page):
    """(text, font size, block number) for every text line on a page, in reading order."""
    out = []
    for bno, block in enumerate(page.get_text("dict", sort=True)["blocks"]):
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line["spans"]).strip()
            if text:
                size = max(line["spans"], key=lambda s: len(s["text"].strip()))["size"]
                out.append((text, round(size, 1), bno))
    return out


def heading_sizes(doc, page_list):
    """Map font sizes clearly larger than the body text to heading levels.
    A size used on only a few lines, above all others, is the book title (1);
    the rest, largest first, are parts/chapters (2) and sections (3)."""
    chars, lines = Counter(), Counter()
    for i in page_list:
        for text, size, _ in _page_lines(doc[i]):
            chars[size] += len(text)
            lines[size] += 1
    if not chars:
        return {}
    body = chars.most_common(1)[0][0]
    # headings are short lines; a big size with long lines is body text in another style
    sizes = sorted((s for s in chars if s >= body * 1.12 and chars[s] / lines[s] < 80),
                   reverse=True)
    levels = {}
    if len(sizes) > 1 and lines[sizes[0]] <= 3:
        levels[sizes.pop(0)] = 1
    for n, size in enumerate(sizes):
        levels[size] = min(2 + n, 3)
    return levels


def _text_blocks(page, levels):
    """Raw text blocks for one page; heading lines become their own marked block."""
    blocks, cur, cur_key = [], [], None
    for text, size, bno in _page_lines(page):
        level = levels.get(size, 0) if sum(c.isalpha() for c in text) >= 2 else 0
        key = (bno, level)
        if cur and key != cur_key:
            blocks.append((cur_key[1], cur))
            cur = []
        cur.append(text)
        cur_key = key
    if cur:
        blocks.append((cur_key[1], cur))
    return [("#" * lvl + " " + " ".join(ls)) if lvl else "\n".join(ls) for lvl, ls in blocks]


def extract_paragraphs(pdf_path, pages_spec=None, ocr=None, ocr_mode="auto", ocr_cache=None,
                       on_ocr=None):
    """ocr: function(png_bytes) -> text, used for pages without a text layer (or all pages
    when ocr_mode == "force"). ocr_cache: dict page_index -> text, so reruns don't re-OCR."""
    doc = pymupdf.open(pdf_path)
    paras, buf = [], ""
    ocr_cache = {} if ocr_cache is None else ocr_cache
    page_list = parse_pages(pages_spec, doc.page_count)
    levels = heading_sizes(doc, page_list)
    for i in page_list:
        page = doc[i]
        blocks = _text_blocks(page, levels)
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
            if heading_level(text):
                if buf:
                    paras.append(buf)
                buf = re.sub(r"\s+", " ", text)
                continue
            # pull chapter headings out onto their own paragraph
            lines = [l.strip() for l in text.split("\n")]
            pieces, cur = [], []
            for l in lines:
                if HEADING_RE.match(l) and len(l) < 60:
                    if cur:
                        pieces.append("\n".join(cur))
                    # "Глава 3" / "Chapter 3" is a chapter; a bare "3" or "IV" a section
                    pieces.append(("## " if l[:1].isalpha() and not re.fullmatch(r"[IVXLC]+\.?", l)
                                   else "### ") + l)
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
                if buf and piece[:1].islower() and not heading_level(buf) \
                        and not heading_level(piece):
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
    """One unit per chapter; short chapters (one-page stories, front matter) are packed
    together up to max_words; over-long chapters (or heading-less books) split at paragraphs."""
    def words(ps):
        return sum(len(p.split()) for p in ps)

    chapters, cur = [], []
    for p in paras:
        if heading_level(p) and cur and not all(heading_level(q) for q in cur):
            chapters.append(cur)
            cur = []
        cur.append(p)
    if cur:
        chapters.append(cur)
    packed = []
    for ch in chapters:
        if packed and words(packed[-1]) + words(ch) <= max_words:
            packed[-1] = packed[-1] + ch
        else:
            packed.append(ch)
    chapters = packed

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
            heading_only = all(heading_level(q) for q in part)
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
- Titles on their own line, marked like Markdown: "# " for the book title, "## " for a part
  or chapter title, "### " for a section or story title. Don't mark anything else.
- Skip running headers, page numbers and illustration captions.
- If the page has no text, output nothing.
Output only the transcription."""},
        ]
        return self._call("You are a precise OCR transcriber.", content, max_tokens=8000)

    def style_guide(self, sample, file_name=""):
        if self.dry_run:
            return "TITLE:\nAUTHOR:\nSTYLE (dry run)\nGLOSSARY (dry run)"
        system = (f"You are a senior literary translator from {self.src} into {self.tgt}, "
                  f"specializing in humorous and children's literature.")
        user = f"""Read this opening of a book and prepare notes for translating the whole book into {self.tgt}.
Audience: {self.audience}.

Start with exactly two lines, for the cover of the translated book:
TITLE: the book's title translated into {self.tgt} as a published translation would title it
AUTHOR: the author's name as {self.tgt} books write it (empty if unknown)
Use the opening and the file name ("{file_name}") as clues; leave out years, editions and "translated".

Then write two sections, plain text, under 600 words total:

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
            return "\n\n".join(re.sub(r"^((?:#{1,3} )?)", rf"\1[{self.tgt_code}] ", p)
                               for p in text.split("\n\n"))
        system = f"""You are a senior literary translator from {self.src} into {self.tgt}, specializing in humorous and children's literature. Audience: {self.audience}.

Goal: a translation a native {self.tgt} reader enjoys as if the book were written in {self.tgt}, with the same laughs in the same places.
- Preserve meaning, tone, voice, pacing and every joke. When a joke depends on wordplay that cannot survive literally, recreate an equivalent joke in {self.tgt} that fits the scene.
- Natural, idiomatic {self.tgt}; never calque {self.src} syntax. Dialogue should sound spoken.
- Follow the style guide and glossary exactly; keep names consistent.
- Keep the paragraph structure: same number of paragraphs, separated by one blank line. Headings start with #, ## or ###: keep that marker exactly and translate the text after it.
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

Change only what improves the text; keep good passages as they are. Keep the same paragraphs, separated by one blank line. Headings start with #, ## or ###: keep those markers exactly.
Output only the final translation. No notes, no commentary, no list of changes.

STYLE GUIDE AND GLOSSARY:
{guide}"""
        user = (f"SOURCE ({self.src}):\n<<<\n{source}\n>>>\n\n"
                f"DRAFT TRANSLATION ({self.tgt}):\n<<<\n{draft}\n>>>")
        out = self._call(system, user, model=self.revise_model)
        return out.strip().strip("<>").strip()


# ---------- 4. typesetting ----------

def split_title_pages(paras):
    """Separate the book's own title pages (author, title, "Stories", publisher, city and
    year) from the text: the short lines before the first section heading or real
    paragraph, when they include the book's title. The cover replaces them.
    Returns (title, remaining paragraphs)."""
    end = next((i for i, p in enumerate(paras) if heading_level(p) in (2, 3)
                or (not heading_level(p) and len(p.split()) >= 12)), len(paras))
    front = paras[:end]
    titles = [heading_text(p) for p in front if heading_level(p) == 1]
    if not titles or end > 20:
        return "", paras
    rest = paras[end:]
    # a different big heading right before the text opens it (e.g. the first part)
    if heading_level(front[-1]) == 1 and heading_text(front[-1]).lower() != titles[0].lower():
        rest = [front[-1]] + rest
    return titles[0], rest


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


def build_pdf(paragraphs, out_path, title, author="", font_path=None):
    reg, bold = (font_path, font_path) if font_path else find_font()
    pdfmetrics.registerFont(TTFont("Body", reg))
    pdfmetrics.registerFont(TTFont("BodyBold", bold))
    body = ParagraphStyle("body", fontName="Body", fontSize=11, leading=15.5,
                          alignment=TA_JUSTIFY, firstLineIndent=5 * mm, spaceAfter=2)
    part = ParagraphStyle("part", fontName="BodyBold", fontSize=17, leading=22, keepWithNext=1,
                          alignment=TA_CENTER, spaceBefore=20 * mm, spaceAfter=10 * mm)
    section = ParagraphStyle("section", fontName="BodyBold", fontSize=12.5, leading=16,
                             keepWithNext=1, alignment=TA_CENTER, spaceBefore=6 * mm,
                             spaceAfter=3 * mm)
    ttl = ParagraphStyle("title", parent=part, fontSize=22, leading=28, spaceBefore=45 * mm)
    byline = ParagraphStyle("author", parent=part, fontName="Body", fontSize=13, leading=18,
                            spaceBefore=0, spaceAfter=0)

    story = ([Paragraph(escape(author), byline)] if author else []) + [Paragraph(escape(title), ttl)]
    story.append(PageBreak())
    for p in paragraphs:
        level = heading_level(p)
        if not level and (HEADING_RE.match(p) or (len(p) < 60 and p.isupper())):
            level = 3
        text = escape(heading_text(p))
        if level in (1, 2):
            if not isinstance(story[-1], PageBreak):
                story.append(PageBreak())
            story.append(Paragraph(text, part))
        elif level == 3:
            story += [CondPageBreak(30 * mm), Paragraph(text, section)]
        else:
            story.append(Paragraph(text, body))

    def page_no(canvas, doc):
        canvas.setFont("Body", 9)
        canvas.drawCentredString(A5[0] / 2, 10 * mm, str(doc.page))

    SimpleDocTemplate(str(out_path), pagesize=A5, leftMargin=16 * mm, rightMargin=16 * mm,
                      topMargin=16 * mm, bottomMargin=18 * mm, title=title, author=author
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
    ap.add_argument("--title", help="title for the cover (default: the translated title)")
    ap.add_argument("--author", help="author for the cover (default: from the book)")
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
        state.update(guide=tr.style_guide(sample, Path(args.input).stem),
                     n_units=len(units), done=[], draft=None)
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
    # the last TITLE:/AUTHOR: line wins, in case the model corrected itself
    meta = {k: (re.findall(rf"^{k}:[ \t]*(.*)$", state["guide"], re.M) or [""])[-1].strip()
            for k in ("TITLE", "AUTHOR")}
    own_title, out_paras = split_title_pages(out_paras)
    title = args.title or meta["TITLE"] or own_title or Path(args.input).stem
    author = meta["AUTHOR"] if args.author is None else args.author
    build_pdf(out_paras, out, title, author, args.font)
    guide_path = out.with_suffix(".glossary.txt")
    guide_path.write_text(state["guide"])
    progress_path.unlink()
    print(f"Done: {out}  (style guide and glossary: {guide_path})")


if __name__ == "__main__":
    main()

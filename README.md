# book-translate

Point this at a book PDF and get back a translated, typeset PDF that keeps
the original's voice and humor -- not just its meaning.

```
book-translate input.pdf output.pdf
```

The default is Russian to Romanian for a children's-book audience; any
source/target pair Claude handles works via `--source` / `--target`.

## Why this exists

Pasting a book into a translator chunk by chunk gives you correct sentences
and a dead book: names drift between chapters, jokes are translated
literally and stop being funny, and the narrator's voice gets smoothed into
generic prose. This script is built around avoiding exactly that:

- **A style guide and glossary first.** Before translating anything, Claude
  reads the opening and writes down the narrator's voice, how the humor
  works, and a glossary of every name, nickname, diminutive, invented word
  and running joke with one chosen rendering. Every later call follows it,
  so "Пушок" is the same character on page 3 and page 300.
- **Whole chapters, with continuity.** Chapters are detected from headings
  ("Глава 3", "Chapter 3", Roman numerals, ...) and translated one per call,
  each with the end of the previous translated chapter for context. Very
  long chapters (or books without headings) are split at paragraph
  boundaries, never mid-sentence.
- **A separate revision pass.** A second call acts as editor: it compares
  source and draft and fixes lost jokes, flattened voice, calques,
  mistranslations and glossary drift. Puns that died in translation are
  recreated, not explained. Skip it with `--no-revise` to halve the cost.
- **Proper typesetting.** Output is a clean A5 book PDF with a Unicode
  font, so Romanian diacritics (ă â î ș ț with comma-below) and Cyrillic
  render correctly.

## What makes it resilient

Progress is saved to `<output>.progress.json` after every step -- the style
guide, each draft, each revised chapter. If a run is interrupted (network,
rate limits, Ctrl-C), rerun the same command and it resumes where it
stopped. Transient API errors are retried with backoff; the file is deleted
once the PDF is written.

## Install (macOS / Linux, no global installs)

Needs Python 3.9+.

```bash
git clone https://github.com/tiberiusf/book-translate.git ~/.local/share/book-translate
~/.local/share/book-translate/install.sh

# Your Anthropic API key (get one at https://console.anthropic.com/settings/keys)
# Add this to your ~/.zshrc (or ~/.bash_profile) so it persists across terminal sessions:
export ANTHROPIC_API_KEY="sk-ant-...your key..."
```

`install.sh` creates a private virtualenv inside the clone and a small
wrapper at `~/.local/bin/book-translate`. To update later:

```bash
git -C ~/.local/share/book-translate pull && ~/.local/share/book-translate/install.sh
```

To uninstall: `rm -rf ~/.local/share/book-translate ~/.local/bin/book-translate`.

## Usage examples

```bash
# Try a sample first, to check the voice before paying for the whole book
book-translate book.pdf sample.pdf --pages 1-20

# The whole book, Russian -> Romanian, with the revision pass
book-translate book.pdf book.ro.pdf

# Cheaper single pass
book-translate book.pdf book.ro.pdf --no-revise

# Other languages and audiences
book-translate book.pdf book.en.pdf --source ru --target en --audience "adults"

# Test extraction and layout without any API calls
book-translate book.pdf test.pdf --dry-run
```

Alongside the PDF you also get `<output>.glossary.txt` -- the style guide
and glossary Claude used, handy for checking its choices.

## Options

| Flag | Default | What it does |
|---|---|---|
| `--source` / `--target` | `ru` / `ro` | Language codes (or names) to translate from and into. |
| `--audience` | children 5-10 and parents | Who the translation is for; shapes register and word choice. |
| `--pages` | all | 1-based pages to use, e.g. `1-20` or `3,5,9-12`. |
| `--title` | input file name | Title on the output PDF's first page. |
| `--model` | `claude-opus-5-5` | Model for translation. `BOOK_TRANSLATE_MODEL` env var sets it for every run. |
| `--revise-model` | same as `--model` | A different model for the revision pass. |
| `--no-revise` | off | Skip the revision pass (cheaper, faster). |
| `--max-chapter-words` | `7000` | Split chapters longer than this. Lower it if output gets cut off. |
| `--font` | auto | Path to a TTF font with full diacritics. |
| `--dry-run` | off | No API calls; tests extraction, chapter splitting and layout. |

## Limitations

- Needs a text layer. Scanned books (images only) must be OCR'd first,
  e.g. `ocrmypdf -l rus in.pdf out.pdf`.
- Output is text only: illustrations and original layout are not carried over.
- Use it only for public-domain works, works you hold the rights to, or
  uses covered by fair use.

## Attribution

Built by Tiberius with Claude as a pair-programmer (design discussion,
implementation, and debugging).

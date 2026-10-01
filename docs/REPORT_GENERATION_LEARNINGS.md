# Aptech Weekly Status Report - Generation Learnings

This document captures everything a future agent needs to rebuild the Aptech
Limited Weekly Status Report fast and correctly, so the branded output stays
faithful (sem-to-sem) to the client reference (`branded-source.docx` / the
Google Docs original) without re-deriving the layout.

The entire report is produced by one self-contained script:
`/projects/sandbox/atlas/generate_weekly_doc.py` (python-docx + boto3 +
matplotlib). It runs in `--mock` mode with no AWS credentials. There is a
byte-identical copy at `/projects/sandbox/generate_weekly_doc.py` that MUST stay
in sync (same md5).

All of the numbers below are the ACTUAL values landed in the final code. If you
change the code, update this file too.

> Note on dashes: this file uses regular hyphens only. The account heading in
> the report data uses an en dash (the short dash in "Name - ID"), which comes
> from the account data and is intentional. Never introduce the long em dash
> anywhere.

---

## 1. Full report layout, top to bottom

### PAGE 1 - Branded cover (`build_cover_page`)

Order top to bottom (matches the reference exactly):

1. Two spacer paragraphs push the block down so it fills the page.
2. Large Operisoft logo, centered, 3.0 in wide.
3. "Weekly Status Report" - black Cambria serif, 28pt, BOLD (`_add_cover_title`).
4. "Aptech Limited" - same black Cambria serif, 28pt, NOT bold.
5. Aptech "Unleash your potential" banner, centered, 2.6 in wide.
6. "Submitted By" block, all centered bold 11pt:
   - "Submitted By"
   - "Operisoft Technologies Pvt Ltd"
   - the cover date (see below)
7. AWS Advanced Tier Partner badge LAST at the bottom, centered, 2.6 in wide.
   The three qualifying phrases (Public Sector / Immersion Day /
   Well-Architected Partner Program) are baked into the badge image, so they are
   NOT repeated as text.
8. A trailing paragraph with `page_break_before = True` forces the report body
   (master cost table) to start on page 2.

Cover date behavior: the cover date is the GENERATION date (the day the script
runs, via `date.today()`), formatted `DD/MM/YYYY` (e.g. `23/09/2026`). It is
NOT the report data window and NOT hardcoded. Implemented in `_cover_date()`.

Cover title font: `COVER_TITLE_FONT = "Cambria"` at 28pt, color black. Cambria
may be unavailable on some systems; Word/LibreOffice font substitution falls
back to a serif face (Times New Roman is a safe equivalent).

### PER-PAGE HEADER + watermark (`build_branded_header`)

On EVERY page (it is the default, non-first-page-only header):

- A borderless two-column header table spanning the content width:
  - LEFT cell: AWS partner cluster image (`aws_partner_cluster.png`), 0.85 in
    wide, left-aligned.
  - RIGHT cell: Operisoft header logo (`operisoft_logo_header.png`), 1.5 in
    wide, right-aligned.
- A diagonal "CONFIDENTIAL" watermark injected into the default header via
  `_add_watermark` (see section 5 for the exact VML markup and the LibreOffice
  caveat).

### PAGE 2 - Summary page (inside `generate_docx_report`)

Order top to bottom:

1. Title: "Aptech Limited Weekly Status Report\n(DD Month to DD Month YYYY)" -
   15pt, bold, NAVY, centered.
2. Heading "Cost Summary Difference of All AWS Accounts" - 14pt bold NAVY. This
   paragraph carries the `"Summary"` bookmark (the target of every account
   page's "Summary" back-link and of the account-name hyperlinks in the master
   table).
3. The compact master cost table with ALL 20 account rows (No 1..20) PLUS the
   "Total Cost" row, all on ONE page (see section 3 for how).
4. The TWO summary bullet lines BELOW the table (this is the sem-to-sem fix:
   they used to sit above the table). Both are disc bullets, bold, 12pt:
   - "The billing for the current week (DD Month to DD Month) has
     {decreased|increased} compared to the previous week."
   - "The cost difference is $X.XX."
   Direction is "decreased" when the total diff is <= 0, else "increased".
5. "Security Best Practices Links:" heading (12pt bold) + a 2-column links
   table (Content / Link).

Master table columns (7): No, Account Name (hyperlink to the account page),
Account ID, Last Week Cost (prev range), Tax Cost, Current Week Cost (cur
range), Services. The Total Cost row merges cells 0..2 into "Total Cost", shows
total prev / total tax / and current total prefixed with a down or up arrow
glyph depending on the diff sign.

### ACCOUNT PAGES - one per account (page break before each)

For each of the 20 accounts, in `No` order:

1. A centered "banner" paragraph with `page_break_before = True` so each account
   starts on a fresh page: a dashed run, the word "Summary" as an internal
   hyperlink back to the `"Summary"` bookmark (10pt bold, color 1F487C), then
   another dashed run. The dashes are plain hyphen runs, Arial 9.5pt bold.
2. The account-name heading "{name} - {account_id}" (the data contains an en
   dash separator): CENTERED, BOLD, UNDERLINED, black, 14pt. It carries the
   `_acc_{account_id}` bookmark that the master table name links jump to.
3. A dashed separator line: a run of 128 hyphens, Arial 9.5pt bold.
4. Body disc bullets (see section 2 for the indent hierarchy):
   - "Billing and Cost Overview" - OUTER (top-level) bullet, bold.
   - "Total cost for the week: $X" - INNER (deeper) bullet, amount bold.
   - "Average Daily Cost: $X" - INNER (deeper) bullet, amount bold.
5. Two cost images (overview, then breakdown), centered, auto-sized so the whole
   account fits one page (`plan_cost_images` + `account_text_height_in`).
6. Optional "Total Tax Cost: $X" line when tax > 0 (plain text, not a bullet).
7. The cost remark line (item["remark"], e.g. "The costs decreased/increased by
   ... due to ...") - an OUTER disc bullet, 12pt.
8. "No Activity performed by Operisoft in this account." - an OUTER disc bullet,
   bold, 12pt.
9. "Resource Utilization & Alarms" heading (14pt bold) + the alarms table (or a
   "No Resource Utilization & Alarms." line when there are none).

After the last account: a blank line then "-- End Of Document --" (11pt italic,
centered). This appears exactly once at the very end.

---

## 2. Bullet and indent conventions (precise)

Disc bullets are implemented locally (NOT via python-docx "List Bullet" styles)
so the glyph and indents are exact and portable to Word / LibreOffice / Google
Docs.

- `_ensure_disc_numbering(doc)` registers (once per document) one `abstractNum`
  (id 9100) with 3 levels, every level a solid disc, plus one concrete `num`
  (numId 9101). The bullet glyph is U+25CF (`&#9679;`, black circle / disc). The
  numbering is added to `doc.part.numbering_part`.
- `add_bullet(doc, segments, ilvl, left_in, hanging_in, size, before, after,
  ...)` creates a bulleted paragraph. `segments` is a list of `(text, bold)`
  tuples so a single line can bold just part of it (e.g. the dollar amount).
  Indent is set PER PARAGRAPH via `left_indent = Inches(left_in)` and
  `first_line_indent = Inches(-hanging_in)`, and a `numPr` (ilvl + numId) is
  appended inside `pPr` so the dot renders.

Exact values used:

Summary-page bullets (below the master table):
- Both lines: `ilvl=0`, `left_in=0.64`, `hanging_in=0.25`, `size=12`, bold,
  `before=0`, `after=6`.
- (Reference equivalent: numPr ilvl 0, bold, 12pt, ind left ~927 twips hanging
  360.)

Account-page body bullets:
- "Billing and Cost Overview": OUTER bullet, `ilvl=0`, `left_in=0.37`,
  `hanging_in=0.25`, 12pt, bold. (Reference ind left ~567 twips.)
- "Total cost for the week: $X": INNER bullet, `ilvl=1`, `left_in=0.94`,
  `hanging_in=0.25`, 12pt; label not bold, the money value bold (two segments).
  (Reference ind left ~1352 twips.)
- "Average Daily Cost: $X": INNER bullet, `ilvl=1`, `left_in=0.94`,
  `hanging_in=0.25`, 12pt; label not bold, money bold.
- Cost remark: OUTER bullet, `ilvl=0`, `left_in=0.37`, `hanging_in=0.25`, 12pt,
  not bold.
- "No Activity performed by Operisoft in this account.": OUTER bullet, `ilvl=0`,
  `left_in=0.37`, `hanging_in=0.25`, 12pt, bold.

The balanced "ek line aage, ek line thodi piche" look comes entirely from this
outer-vs-inner hierarchy: "Billing and Cost Overview" sits at the outer indent,
the two cost lines sit one level deeper.

Bullet font is Arial throughout (`add_bullet` sets `r.font.name = "Arial"`).

---

## 3. Compact master table techniques (all 20 rows + Total on ONE page)

The master cost table has a header row + 20 data rows + 1 Total row and must fit
a single page. Levers used (all in `generate_docx_report` and helpers):

- Column widths: `MASTER_COL_WIDTHS_IN = [0.35, 1.3, 0.95, 0.85, 0.65, 0.95,
  1.95]`, which sums to exactly 7.0 in (the content width). Do NOT change these
  unless the table overflows horizontally; they already fit.
- Small cell fonts via `style_cell(cell, size, ..., before, after)` (Arial):
  - Header cells: 8pt bold, `before=0`, `after=0`.
  - Data cells: 7pt, `before=0`, `after=0`.
  - Total row cells: 8.5pt bold, `before=0`, `after=0`.
- Zeroed in-cell paragraph spacing: `style_cell` passes `before=0, after=0` so
  there is no empty gap under the text in each cell. `style_cell` also forces
  single line spacing.
- Zeroed cell vertical margins: `set_table_cell_margins(table1, top=0, bottom=0,
  left=50, right=50)` removes top/bottom cell padding (twips) while keeping a
  little side padding so text does not touch the borders.
- `keep_row_on_one_page(row)` adds `w:cantSplit` to every row (header, each data
  row, Total) so no single row breaks across pages.
- The title above the table is 15pt with `after=2` and the heading `after=2`, so
  no large empty paragraph pushes the table down.

If a future change makes the table spill, tighten further in this order: data
font 7 -> 6.5, confirm spacing is 0/0, confirm cell margins top/bottom are 0.
Prefer tightening over splitting the table across pages.

Note: `repeat_as_header(row)` exists for the alarms table (repeats the header if
the table ever continues onto a next page); it is not needed on the master table
once everything fits one page.

---

## 4. Branding assets

Assets live in `/projects/sandbox/assets/branding/` and are resolved RELATIVE to
the script (`_branding_dir()` tries `<script>/assets/branding`, then
`<script>/../assets/branding`, then `<cwd>/assets/branding`). `asset_path(name)`
returns the absolute path or logs a warning and returns `None` so a missing
asset is skipped gracefully instead of crashing the report.

Expected files:
- `aptech_logo.png`
- `operisoft_logo_large.png`
- `operisoft_logo_header.png`
- `aws_partner_badge.png`
- `aws_partner_cluster.png`

KNOWN CONTENT-SWAP GOTCHA (important): on disk, `operisoft_logo_large.png` and
`aptech_logo.png` have SWAPPED visual content:
- `aptech_logo.png` actually holds the large OPERISOFT logo.
- `operisoft_logo_large.png` actually holds the APTECH "Unleash your potential"
  banner.

The code works AROUND this by referencing each file by its ACTUAL content, not
its name. So on the cover:
- The top large Operisoft logo is loaded from `aptech_logo.png`.
- The Aptech banner is loaded from `operisoft_logo_large.png`.

`operisoft_logo_header.png` (used in the per-page header) is CORRECT and
untouched. If someone "fixes" the file names/content later, the cover
`_add_centered_image` calls in `build_cover_page` must be updated to match.

Header image widths: cluster 0.85 in (left), header logo 1.5 in (right).
Cover image widths: large logo 3.0 in, Aptech banner 2.6 in, AWS badge 2.6 in.

---

## 5. CONFIDENTIAL watermark and the LibreOffice / PDF caveat

The watermark is a VML text-path shape injected into the DEFAULT header
(`_WATERMARK_XML` + `_add_watermark`), so Word / LibreOffice / Google Docs
render it on every page. python-docx has no native watermark API, so the exact
reference markup (from `word/header1.xml`) is injected. Key attributes (do not
change; these match the reference and are correct):
- shape type `#_x0000_t136` (VML text path)
- `rotation:315` (diagonal)
- `fillcolor="#c0c0c0"`
- fill `opacity="32768f"` (about 50%)
- `string="CONFIDENTIAL"`, z-index negative so it sits behind content.

LibreOffice PDF caveat: LibreOffice headless PDF export DROPS this VML watermark
and may slightly reflow the layout versus Microsoft Word / Google Docs. That is
a RENDERER limitation, not a bug in the markup. Treat the LibreOffice PDF as a
layout approximation only. The AUTHORITATIVE rendering target is Microsoft Word
/ Google Docs, where the watermark shows correctly. Do NOT try to "fix" the
missing watermark by editing the LibreOffice output or the markup; verify the
markup is intact in the .docx instead (see the zip check in section 6).

---

## 6. How to generate and verify

Generate (mock mode, no AWS needed):

```
cd /projects/sandbox/atlas
python3 generate_weekly_doc.py --mock --start 2026-09-14 --end 2026-09-20
```

This writes `Aptech-Limited-Weekly-Status-Report_20260914_to_20260920.docx` in
the current directory.

Convert to PDF with LibreOffice (not on PATH; use the full path):

```
/opt/libreoffice26.2/program/soffice --headless --convert-to pdf --outdir . \
  Aptech-Limited-Weekly-Status-Report_20260914_to_20260920.docx
```

Render pages to PNG for visual comparison:

```
pdftoppm -png -r 90 Aptech-Limited-Weekly-Status-Report_20260914_to_20260920.pdf out
```

(Use `-f`/`-l` to limit page range, e.g. `-f 2 -l 5` for the summary and first
account pages. Reference renders are `/projects/sandbox/_ref_pg-02.png`
(compact 20-row table), `_ref_pg-03.png` (Total row then the two bullets below),
`_ref_pg-04.png` (account page bullets + centered bold underlined heading).)

Verify the watermark markup survived in the .docx (LibreOffice PDF will not show
it, so check the file itself):

```
python3 -c "import zipfile; z=zipfile.ZipFile('Aptech-Limited-Weekly-Status-Report_20260914_to_20260920.docx'); hdr=[n for n in z.namelist() if 'header' in n and n.endswith('.xml')]; xml=z.read(hdr[0]).decode('utf-8','ignore'); print('watermark intact:', ('_x0000_t136' in xml and 'CONFIDENTIAL' in xml and 'c0c0c0' in xml.lower() and '32768' in xml))"
```

Keep the two script copies byte-identical after ANY edit:

```
md5sum /projects/sandbox/atlas/generate_weekly_doc.py /projects/sandbox/generate_weekly_doc.py
```

The two hashes MUST match. If you edit the atlas copy, copy it over the root
copy and re-check.

Visual acceptance checklist (compare against the reference renders):
- Summary page: rows No 1..20 AND the Total Cost row all on ONE page.
- The two cost-summary bullets render as dots BELOW the table, not above.
- Account pages: centered + bold + underlined heading, "Summary" back-link and
  dashed separators framing it.
- Account body: "Billing and Cost Overview" as an outer dot; the two cost lines
  as deeper-indented inner dots with bold amounts; bulleted remark; bulleted
  bold "No Activity..." line.
- Each account occupies exactly one page; "-- End Of Document --" appears once at
  the very end.

---

## 7. Protected areas (do not break)

Changes to layout should stay inside `generate_docx_report` and its small
helpers (`add_bullet`, `_ensure_disc_numbering`, `set_table_cell_margins`,
`style_cell`, `account_text_height_in`, `plan_cost_images`). Do NOT modify:
`main()`, argument parsing, `mock_daily_costs`, `generate_mock_alarm_data`,
`fetch_daily_service_costs`, `assume_role_session`, `process_region`,
`build_cover_page` structure, `build_branded_header`, or the watermark markup.
The script must stay 100% self-contained and runnable in `--mock` mode without
AWS credentials.

One-page-per-account fitting depends on `account_text_height_in` estimating the
non-image height correctly. If you change body line sizes, spacing
(`ACC_SPACE_AFTER`), or bullet indents, update the matching terms in
`account_text_height_in` so `plan_cost_images` still shrinks the two cost images
enough to keep every account on one page. The remark bullet is estimated against
the narrower outer-bullet width (`w - 0.37`) and the "No Activity" line is
estimated at 12pt line height to match the current bullets.

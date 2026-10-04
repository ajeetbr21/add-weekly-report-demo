# Aptech Weekly Status Report Generator

Ek self-contained Python script jo **Aptech Limited Weekly Status Report** ko
branded Word (`.docx`) format mein generate karta hai — Operisoft branding, AWS
cost summary table, per-account cost pages, charts aur CONFIDENTIAL watermark ke
saath.

Report ka poora layout aur har design decision yahan documented hai:
[`docs/REPORT_GENERATION_LEARNINGS.md`](docs/REPORT_GENERATION_LEARNINGS.md).

## Contents

| File | Kya hai |
|------|---------|
| `generate_weekly_doc.py` | Main script (python-docx + boto3 + matplotlib) |
| `docs/REPORT_GENERATION_LEARNINGS.md` | Report layout + generation ki poori guide |
| `Aptech-Weekly-Status-Report-DEMO.docx` / `.pdf` | Demo report (sample output) |
| `Aptech-Limited-Weekly-Status-Report_20260914_to_20260920.docx` / `.pdf` | Ek real date-range ki generated report |

## Requirements

- Python 3.9+
- `pip install -r requirements.txt`

## Usage

Mock mode (koi AWS credentials nahi chahiye — demo/test data use karta hai):

```bash
python3 generate_weekly_doc.py --mock --start 2026-09-14 --end 2026-09-20
```

Ye current directory mein ek `.docx` report likhta hai.

> **Note:** Branding images (`aptech_logo.png`, `operisoft_logo_large.png`,
> `operisoft_logo_header.png`, `aws_partner_badge.png`,
> `aws_partner_cluster.png`) ek `assets/branding/` folder se padhi jaati hain.
> Agar wo folder maujood nahi hai to script crash nahi hoti — wo logo/badge
> images ko skip karke baaki report bana deti hai. Branded output ke liye wo 5
> PNG files `assets/branding/` mein rakho.

### PDF mein convert karna (optional)

LibreOffice se:

```bash
soffice --headless --convert-to pdf --outdir . <report-file>.docx
```

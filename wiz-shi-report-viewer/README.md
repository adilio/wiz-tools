# Wiz SHI Report Viewer

A zero-dependency, single-file web application that transforms Wiz security issue export CSVs into an interactive, richly-formatted report viewer.

## Features

- **Interactive table** — flat or grouped-by-issue views with expandable detail rows
- **Filtering & search** — filter by severity, source, or free-text search across all fields
- **Presentation mode** — full-screen slide deck for security briefings (arrow key navigation)
- **Notes** — per-issue notes that persist in browser localStorage (scoped per file)
- **Wiz Graph Query links** — extracts and links to Wiz Explorer queries from CSV data
- **Source health overview** — collapsible panel showing issue counts and severity breakdown per source
- **Dark / light theme** — instant toggle, defaults to Wiz brand colors

## Usage

No installation or build step required. Open `wiz-shi-report-viewer.html` directly in any modern browser.

1. Open `wiz-shi-report-viewer.html` in Chrome, Firefox, Safari, or Edge
2. Upload or drag-and-drop a Wiz issues CSV export
3. Explore, filter, and annotate your findings

Works entirely offline — no server, no dependencies, no external requests (except Google Fonts on first load).

## CSV Format

Export issues from Wiz and upload the resulting CSV. The parser expects these columns:

| Column | Required | Notes |
|---|---|---|
| `Name` | Yes | Issue / vulnerability name |
| `Severity` | Yes | Critical, High, Medium, Low, Informational |
| `Status` | Yes | Open, In Progress, Resolved |
| `Source` | Yes | Detection source / scanner |
| `Deployment` | No | Environment or deployment name |
| `Region` | No | Cloud region |
| `Created At` | No | Auto-formatted to YYYY-MM-DD |
| `Last Active At` | No | Last detection date |
| `Product impact` | No | Markdown supported |
| `Remediation` | No | Numbered list format supported |
| `Category` | No | Issue category |

The parser handles quoted fields, embedded commas, newlines within fields, and UTF-8 BOM.

## Severity Colors

| Severity | Color |
|---|---|
| Critical | Purple |
| High | Red |
| Medium | Orange |
| Low | Green |
| Informational | Blue |

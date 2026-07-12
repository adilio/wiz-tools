# wiz-tools

A small collection of practical tools for working with [Wiz](https://www.wiz.io) data, exports, and sizing workflows.

The repo is intentionally lightweight: most tools are self-contained and can be used directly from the browser, terminal, or Azure Cloud Shell without a build step.

> **Sizing scripts have moved.** All Wiz sizing tooling (cloud resource
> counting, Defend ingest estimation, code/developer counting, Microsoft 365)
> now lives in **[wiz-sizing](https://github.com/adilio/wiz-sizing)** as
> curl-able per-cloud scripts. The old `sizing-scripts/` tree is preserved at
> [`wiz-sizing/reference/`](https://github.com/adilio/wiz-sizing/tree/main/reference).

## Published Pages

A static catalog is published from the [`docs/`](docs/) directory:

- [wiz-tools landing page](https://adilio.github.io/wiz-tools/)
- [Wiz SHI Report Viewer](https://adilio.github.io/wiz-tools/wiz-shi-report-viewer/)
- [Wiz Sizing Scripts](https://adilio.github.io/wiz-tools/sizing-scripts/) — legacy catalog page; the scripts themselves now live in [wiz-sizing](https://github.com/adilio/wiz-sizing)
- [Legacy Microsoft 365 Sizing Script page](https://adilio.github.io/wiz-tools/m365-sizing-xl/) kept for older shared links

## Tools

| Tool | Purpose | How to run |
|---|---|---|
| [Wiz SHI Report Viewer](wiz-shi-report-viewer/) | Turn Wiz security issue CSV exports into an interactive report viewer. | Open `wiz-shi-report-viewer/wiz-shi-report-viewer.html` in a browser. |
| [Wiz Sizing (moved)](https://github.com/adilio/wiz-sizing) | Estimate Wiz billable units: cloud resources, Defend ingest, code/developer counts, Microsoft 365. | See the [wiz-sizing README](https://github.com/adilio/wiz-sizing#readme) for the per-cloud one-liners. |

## Repository Layout

```text
.
├── docs/                    # GitHub Pages/static published versions
├── sizing-scripts/           # Pointer stub → github.com/adilio/wiz-sizing
├── wiz-shi-report-viewer/    # Single-file Wiz security issue report viewer
├── LICENSE
└── README.md
```

## Quick Start

### Wiz SHI Report Viewer

Open the viewer directly in a modern browser:

```text
wiz-shi-report-viewer/wiz-shi-report-viewer.html
```

Then upload a Wiz security issue CSV export.

### Sizing

Use [wiz-sizing](https://github.com/adilio/wiz-sizing) — one curl-able script
per cloud (Azure, AWS, GCP), plus `wiz-code.sh` for developer counting and
`wiz-365.ps1` for Microsoft 365.

## Notes

- These tools are shared as practical helpers, not official Wiz product components.
- Review scripts before running them in production tenants.
- Some tools require administrative permissions in the target environment.

## License

MIT. See [LICENSE](LICENSE).

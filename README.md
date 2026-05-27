# wiz-tools

A small collection of practical tools for working with [Wiz](https://www.wiz.io) data, exports, and sizing workflows.

The repo is intentionally lightweight: most tools are self-contained and can be used directly from the browser, terminal, or Azure Cloud Shell without a build step.

## Published Pages

A static catalog is published from the [`docs/`](docs/) directory:

- [wiz-tools landing page](https://adilio.github.io/wiz-tools/)
- [Wiz SHI Report Viewer](https://adilio.github.io/wiz-tools/wiz-shi-report-viewer/)
- [Microsoft 365 Sizing Script](https://adilio.github.io/wiz-tools/m365-sizing-xl/)

## Tools

| Tool | Purpose | How to run |
|---|---|---|
| [Wiz SHI Report Viewer](wiz-shi-report-viewer/) | Turn Wiz security issue CSV exports into an interactive report viewer. | Open `wiz-shi-report-viewer/wiz-shi-report-viewer.html` in a browser. |
| [Microsoft 365 Sizing Script](m365-sizing-xl/) | Estimate Wiz billable units for Microsoft 365 SaaS users and virtual drives. | Run `m365-sizing-xl/365_Sizing_Script.ps1` from Azure Cloud Shell. |

## Repository Layout

```text
.
├── docs/                    # GitHub Pages/static published versions
├── m365-sizing-xl/           # Microsoft 365 sizing and discovery scripts
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

### Microsoft 365 Sizing Script

The script is optimized for Azure Cloud Shell.

```powershell
cd m365-sizing-xl
./365_Sizing_Script.ps1
```

The script creates a temporary Entra ID application, requests Microsoft Graph permissions, scans Microsoft 365 users and drives, prints final counts, and removes the temporary application when it finishes.

See [m365-sizing-xl/README.md](m365-sizing-xl/README.md) for prerequisites, Cloud Shell instructions, and operational notes.

## Notes

- These tools are shared as practical helpers, not official Wiz product components.
- Review scripts before running them in production tenants.
- Some tools require administrative permissions in the target environment.

## License

MIT. See [LICENSE](LICENSE).

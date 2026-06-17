# wiz-tools

A small collection of practical tools for working with [Wiz](https://www.wiz.io) data, exports, and sizing workflows.

The repo is intentionally lightweight: most tools are self-contained and can be used directly from the browser, terminal, or Azure Cloud Shell without a build step.

## Published Pages

A static catalog is published from the [`docs/`](docs/) directory:

- [wiz-tools landing page](https://adilio.github.io/wiz-tools/)
- [Wiz SHI Report Viewer](https://adilio.github.io/wiz-tools/wiz-shi-report-viewer/)
- [Wiz Sizing Scripts](https://adilio.github.io/wiz-tools/sizing-scripts/) including Microsoft 365, Azure DevOps, GCP, Defend ingestion, and other sizing scripts
- [Legacy Microsoft 365 Sizing Script page](https://adilio.github.io/wiz-tools/m365-sizing-xl/) kept for older shared links

## Tools

| Tool | Purpose | How to run |
|---|---|---|
| [Wiz SHI Report Viewer](wiz-shi-report-viewer/) | Turn Wiz security issue CSV exports into an interactive report viewer. | Open `wiz-shi-report-viewer/wiz-shi-report-viewer.html` in a browser. |
| [Microsoft 365 Sizing Script](sizing-scripts/saas/microsoft-365/) | Estimate Wiz billable units for Microsoft 365 SaaS users and virtual drives. | Run `sizing-scripts/saas/microsoft-365/365_Sizing_Script.ps1` from Azure Cloud Shell. |
| [Wiz Sizing Scripts](sizing-scripts/) | Organized copies of Wiz Code, Cloud, SaaS, Defend ingestion, and infrastructure resource-discovery scripts. | Browse the relevant provider folder and run that script's documented command. |

## Repository Layout

```text
.
├── docs/                    # GitHub Pages/static published versions
├── sizing-scripts/           # Organized Code, Cloud, Defend, and SaaS sizing scripts
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
cd sizing-scripts/saas/microsoft-365
./365_Sizing_Script.ps1
```

The script creates a temporary Entra ID application, requests Microsoft Graph permissions, scans Microsoft 365 users and drives, prints final counts, and removes the temporary application when it finishes.

See [sizing-scripts/saas/microsoft-365/README.md](sizing-scripts/saas/microsoft-365/README.md) for the canonical script location.

## Notes

- These tools are shared as practical helpers, not official Wiz product components.
- Review scripts before running them in production tenants.
- Some tools require administrative permissions in the target environment.

## License

MIT. See [LICENSE](LICENSE).

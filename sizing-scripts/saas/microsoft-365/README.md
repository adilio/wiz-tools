# Microsoft 365 Sizing Script

This is the canonical home of the Microsoft 365 sizing script.

Run from Azure Cloud Shell:

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/adilio/wiz-tools/main/sizing-scripts/saas/microsoft-365/365_Sizing_Script.ps1 -OutFile 365_Sizing_Script.ps1
./365_Sizing_Script.ps1
```

For full operational notes, use the published sizing catalog at `docs/sizing-scripts/`. The older `docs/m365-sizing-xl/` page is retained only so previously shared Microsoft 365 documentation links keep working.

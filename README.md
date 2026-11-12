# Chemotion Sample Manager

Chemotion Sample Manager is a desktop app based on [Chemotion API](https://github.com/Chemotion/chemotionapi) for viewing, bulk-editing, and printing [Chemotion ELN](https://chemotion.net/) sample information.

> ⚠️ **Caution.** Chemotion ELN v4 has not yet been tested with Chemotion Sample Manager.

![Chemotion Sample Manager with anonymized locations and connection details](docs/images/inventory-anonymized.png)

*App interface with molecule structures and selected columns. Identifying text has been anonymized.*

## Contents
- [Features](#features)
- [Supported environments](#supported-environments)
- [Setup](#setup)
- [Use](#use)
- [Customization](#customization)
- [Data handling](#data-handling)
- [Project structure](#project-structure)

## Features
- Browse samples from multiple collections in a customizable table with molecule structures and sample details.
- Sort by column and filter by location or regular expression.
- Edit sample details in bulk.
- Remove samples from collections.
- Open a sample in Chemotion for further inspection.
- Create A4 PDF checklists for sample verification.
- Export a collection.

## Supported environments
- macOS 11+ (Intel or Apple Silicon).
- Windows 10+ (64-bit Intel/AMD).
- Chemotion ELN v3.1.2. Other versions may work but have not been verified.

## Setup
- **Standalone app:** Ready to use without installing Python.
- **Customizable launcher:** Runs the editable Python source and requires Python 3.14. Choose this option to customize the app.

### Standalone app
1. Download the ZIP for your platform from [Releases](https://github.com/Kenichi-Endo/chemotion-sample-manager/releases): **macos-apple-silicon**, **macos-intel**, or **windows-x64**.
2. Extract the ZIP.

### Customizable launcher
1. Install Python 3.14, if not installed: [Python for macOS](https://www.python.org/downloads/macos/) or [Python for Windows](https://www.python.org/downloads/windows/). On Windows, select **Add python.exe to PATH** and keep **tcl/tk and IDLE** selected during installation.
2. Download **Source code (zip)** from [Releases](https://github.com/Kenichi-Endo/chemotion-sample-manager/releases) and extract it, or clone the repository.
3. Locate the launcher in `launchers/`. Do not change its location.

### Common setup
1. Open the app.
   - On macOS, if you see **Apple could not verify “Chemotion Sample Manager” is free of malware that may harm your Mac or compromise your privacy.**, dismiss the warning. Open **[System Settings → Privacy & Security](x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension)**, scroll down to the bottom, select **Open Anyway**, and confirm.
     If macOS says **The application “Chemotion Sample Manager.app” cannot be opened** when you open the customizable launcher:
     1. Open [Terminal](file:///System/Applications/Utilities/Terminal.app), type `cd ` (including the space), drag the extracted project folder into Terminal, and press Return.
     2. Run:
        ```sh
        chmod +x "launchers/macos/Chemotion Sample Manager.app/Contents/MacOS/Chemotion Sample Manager"
        ```
     3. Reopen the launcher.
   - On Windows, if you see **Windows protected your PC**, select **More info → Run anyway**.
2. Keep an internet connection available while the launcher installs dependencies on its first run. This may take a few minutes.
3. Enter the Chemotion server root URL (for example, `https://eln.example.org`), your username, and password, then select **Sign in**.
4. Select collections (use Cmd/Ctrl/Shift-click for multiple selection).
5. Click **Go**.
6. Use **Columns** to choose visible columns. Drag column headings to reorder them, or click a heading to sort.
7. Adjust **Font size** and **Structure size**.

## Use
- Launch the app.
- If you want to change collections or need only the loaded samples, press Esc or click **Stop**.
- Adjust **Location** or **Filter** to filter the displayed samples.
- Type while outside an input field to select rows by the sorted column.
- To edit one sample, click a cell. For **Location**, an existing location can be chosen from the dropdown. For **Amount**, include a unit such as `250 mg`. Press (Shift+)Return/Enter or Tab to save directly to Chemotion and move to the next cell, or Esc to cancel.
- To edit multiple samples, use Command/Ctrl-click to select multiple samples, or Shift-click to select a range. Edit a cell in the selection to apply the value to all selected rows.
- To check or edit a sample in Chemotion, double-click a structure or short label.
- To remove a sample from all loaded collections, select it and press Delete.
- To verify samples, click **Print…** to create an A4 checklist. Choose the columns and their order, page grouping, and optional **Correct** and **Incorrect** check columns.
- To export a collection, click **Export collection…**. You can continue working while **Export queued** is shown; keep the app open until the completion dialog appears. This can be useful as a backup. Synchronized collections cannot be exported.

![Sample verification checklist with anonymized locations](docs/images/checklist-anonymized.png)

*A4 sample checklist with verification columns. Locations have been anonymized.*

## Customization
Edit the Python files in `src/chemotion_sample_manager/` manually or with a coding agent, then restart the customizable launcher.

## Data handling
Most communication with Chemotion uses the built-in functions of [chemotionapi](https://github.com/Chemotion/chemotionapi) version **312.0.2**: sign-in (`Instance.login()`), loading collections (`Instance.get_root_collection()`, which also calls `RootCollection.load_sync_collection()`), sample listing (`ElementSet.load_elements()` and `ElementSet.next_page()`), sample details (`Instance.get_sample()`), sample edits (`Sample.save()`), and removing samples from collections (`Collection.remove_elements()`). Vendor metadata, creating molecule names, structure images, and collection exports use custom requests through the same authenticated connection (`Connection.get()`, `Connection.post()`, and `Connection.put()`).

Sample information is retrieved over HTTPS and held in memory while the app is open.

With **Remember** enabled, your username and password are saved locally in macOS Keychain or Windows Credential Manager under `io.github.kenichi-endo.chemotion-sample-manager`.

Server and display preferences are saved separately in the current user's profile:

- macOS: `~/Library/Application Support/Chemotion Sample Manager/settings.json`
- Windows: `%APPDATA%\Chemotion Sample Manager\settings.json`

Customizable launcher logs are in `~/Library/Logs/Chemotion Sample Manager/launcher.log` on macOS or `%LOCALAPPDATA%\Chemotion Sample Manager\Logs\launcher.log` on Windows.

## Project structure
- `src/chemotion_sample_manager/` — Python application code.
  - `__init__.py` — Package identity and application name.
  - `__main__.py` — Entry point for the module and installed command.
  - `gui.py` — Window, inventory table, dialogs, cell editing, and structure thumbnails.
  - `client.py` — Chemotion sign-in, sample retrieval and updates, and collection export.
  - `samples.py` — Sample rows, columns, formatting, sorting, and filtering.
  - `settings.py` — Preferences, print settings, and credential storage.
  - `printing.py` — PDF checklist generation.
  - `bootstrap.py` — Python environment and dependency setup for the customizable launchers.
  - `standalone.py` — Standalone app entry point and dependency checks.
- `launchers/` — Customizable launchers.
- `scripts/` — Standalone build and macOS icon scripts.
- `tests/` — Automated tests.
- `pyproject.toml` — Package configuration and dependencies.

<style>
li > :is(ul, ol) {
    margin-top: 0;
}
li > p:has(+ :is(ul, ol)),
li:has(+ li) > :is(ul, ol):last-child,
li:has(+ li) > :is(ul, ol):last-child > li:last-child > p:last-child {
    margin-bottom: 0;
}
</style>

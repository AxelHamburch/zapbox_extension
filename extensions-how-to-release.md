# ZapBox Extension – Release & Update Guide

## Overview

Everything lives in one repository:
- **`zapbox_extension`** – the Python code of the LNbits extension **and** `extensions.json` with the SHA256 hashes of all releases

The extension manifest URL for LNbits:
```
https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/extensions.json
```

> **⚠️ CRITICAL: Tag vs. main must be different commits!**
> `extensions.json` is part of this repo. The tag ZIP includes whatever `extensions.json` was at tag time. To avoid a chicken-and-egg hash problem:
> 1. Tag is placed on the **code commit** (before the hash is known)
> 2. SHA256 is calculated from the tag's ZIP
> 3. Hash is committed to `extensions.json` on **main only** — the tag is **never moved**
>
> LNbits reads `extensions.json` from `main` (correct hash) and downloads the ZIP from the tag (matching hash).
```

Every time `zapbox_extension` is changed, the hash in `extensions.json` must be recalculated.

---

## A) Bugfix / Update (same version, e.g. still v2.0.3)

### 1. Make changes and push

```bash
cd d:\VSCode\zapbox_extension
git add <changed files>
git commit -m "fix: description"
git push
```

### 2. Delete and recreate the tag

Since the ZIP content changes with every new commit, the tag must be moved so GitHub generates a fresh ZIP for the same version:

```bash
git tag -d v2.0.3                        # delete local tag
git push origin :refs/tags/v2.0.3        # delete remote tag
git tag v2.0.3                           # create tag at current commit
git push origin v2.0.3                   # push
```

### 3. Calculate SHA256 hash (PowerShell)

Wait a few seconds for GitHub to build the ZIP, then:

```powershell
Start-Sleep -Seconds 5
$zip = "$env:TEMP\zapbox_v2.0.3.zip"
Invoke-WebRequest -Uri "https://github.com/AxelHamburch/zapbox_extension/archive/refs/tags/v2.0.3.zip" -OutFile $zip
(Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
```

### 4. Update the hash in `extensions.json`

File: `d:\VSCode\zapbox_extension\extensions.json`

> **Important:** Do **not** overwrite old entries. **Add the new version at the bottom** of the array so users can choose which version to install. LNbits picks the highest version number – placing it last avoids false "update available" notifications for older entries.

### 5. Commit and push

```bash
cd d:\VSCode\zapbox_extension
git add extensions.json
git commit -m "fix: update extensions.json hash for v2.0.3"
git push
```

> **Note:** After pushing, the updated `extensions.json` is immediately available via GitHub raw URL – no SFTP upload needed.
> **⚠️ Do NOT move the tag after this step!** The tag must stay on the code commit from step 2.

### 6. Reinstall the extension in LNbits

In LNbits: uninstall the extension → reinstall it (so the new static files are loaded).

---

## B) New Release (new version, e.g. v2.1.0)

### 1. Bump the version number

In `zapbox_extension/config.json`:
```json
"version": "2.1.0"
```

### 2. Commit and push changes

```bash
cd d:\VSCode\zapbox_extension
git add .
git commit -m "feat: release v2.1.0 – description"
git push
```

### 3. Create and push a new tag

```bash
git tag v2.1.0
git push origin v2.1.0
```

> **No deletion needed** – the previous tag remains intact in history.

### 4. Calculate SHA256 hash (PowerShell)

```powershell
Start-Sleep -Seconds 5
$zip = "$env:TEMP\zapbox_v2.1.0.zip"
Invoke-WebRequest -Uri "https://github.com/AxelHamburch/zapbox_extension/archive/refs/tags/v2.1.0.zip" -OutFile $zip
(Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
```

### 5. Update `extensions.json`

Add the new version **at the bottom** of the array. Keep old entries above it – do **not** delete them.

```json
{
  "extensions": [
    {
      "id": "zapbox",
      "repo": "https://github.com/AxelHamburch/zapbox_extension",
      "name": "Zap⚡Box",
      "version": "<PREVIOUS VERSION>",
      "min_lnbits_version": "1.4.0",
      "short_description": "Turn things on with bitcoin – NFC Bolt Card support for ZapBox",
      "icon": "https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/static/image/icon.png",
      "details_link": "https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/config.json",
      "archive": "https://github.com/AxelHamburch/zapbox_extension/archive/refs/tags/v<PREVIOUS VERSION>.zip",
      "hash": "<PREVIOUS HASH>"
    },
    {
      "id": "zapbox",
      "repo": "https://github.com/AxelHamburch/zapbox_extension",
      "name": "Zap⚡Box",
      "version": "2.1.0",
      "min_lnbits_version": "1.4.0",
      "short_description": "Turn things on with bitcoin – NFC Bolt Card support for ZapBox",
      "icon": "https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/static/image/icon.png",
      "details_link": "https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/config.json",
      "archive": "https://github.com/AxelHamburch/zapbox_extension/archive/refs/tags/v2.1.0.zip",
      "hash": "<NEW HASH>"
    }
  ]
}
```

### 6. Commit and push

```bash
cd d:\VSCode\zapbox_extension
git add extensions.json config.json
git commit -m "release: v2.1.0 – update extensions.json"
git push
```

> **⚠️ Do NOT move the tag after this step!** The tag must stay on the code commit from step 3.

### 7. Reinstall the extension in LNbits

In LNbits: uninstall the extension → reinstall it (so the new static files are loaded).

---

## Quick Checklist

| Step | Bugfix (same version) | New Release |
|---|---|---|
| Commit & push changes | ✅ | ✅ |
| Delete old tag (local + remote) | ✅ | ❌ (not needed) |
| Create & push new tag | ✅ | ✅ |
| Recalculate SHA256 | ✅ | ✅ |
| Update `extensions.json` hash | ✅ | ✅ |
| Update `extensions.json` version + URL | ❌ | ✅ |
| Keep old version entry in `extensions.json` | ✅ | ✅ |
| Bump version in `config.json` | ❌ | ✅ |
| Reinstall extension in LNbits | ✅ | ✅ |
| Create GitHub Release with release notes | ❌ | ✅ |

---

## C) GitHub Release erstellen (mit Release Notes)

Nachdem Tag und `extensions.json` fertig sind, wird auf GitHub ein offizieller Release angelegt. Die Release Notes fassen alle Änderungen **seit dem letzten offiziellen Release** zusammen.

### 1. Commits seit dem letzten Release ermitteln

Den Startpunkt (Basis-Commit des letzten Release) aus der Tabelle unten entnehmen, dann:

```powershell
cd d:\VSCode\zapbox_extension
git log <BASIS-COMMIT>..HEAD --oneline
git log <BASIS-COMMIT>..HEAD --format="%H %s%n%b"
```

Die Commits nach inhaltlichen Themen gruppieren (Features, Bug fixes, UI-Änderungen, chore-Commits weglassen).

### 2. Release Notes als Markdown-Datei anlegen

Datei im ZapBox-Repository unter `temp/` ablegen, damit sie leicht kopiert werden kann:

```
d:\VSCode\ZapBox\temp\zapbox-extension-v<VERSION>-release-notes.md
```

Struktur:
```markdown
## What's new since v<VORHERIGE VERSION>

### Feature-Titel *(vX.Y.Z)*
Kurze Erklärung warum diese Änderung nötig war (Problem) und was sie löst.
- Bullet-Points zu Details

### Bug fixes
- **Kurztitel** *(vX.Y.Z)*: was war kaputt und wie gefixt.

---

## Compatibility
| Component | Required version |
…

---

## Upgrade notes
Hinweis auf Migrationen, Breaking Changes oder besondere Schritte.
```

### 3. Release auf GitHub anlegen

Auf GitHub → Releases → "Draft a new release":
- **Tag:** `v<VERSION>` (bereits vorhanden)
- **Title:** kurze, lesbare Bezeichnung, z. B. `v2.5.2 — Identity Login: LNURL-auth & NFC tap`
- **Body:** Inhalt der Markdown-Datei aus Schritt 2 einfügen

> **Title-Konvention:** `v<VERSION> — <Was ist neu in einem Satz>`

---

## Referenz: Letzter offizieller Release

Diese Tabelle nach jedem Release aktualisieren, damit beim nächsten Release der richtige Startpunkt bekannt ist.

### zapbox_extension

| Version | Tag-Commit | Commit-Message |
|---------|-----------|----------------|
| **v2.5.4** *(aktuell)* | `594253e` | `fix: use websocket_updater so settled payments reach the device` |
| v2.5.3 *(Basis für v2.5.4 Release Notes)* | `e61a244` | — |

Für den nächsten Release: `git log 594253e..HEAD --oneline`

### tagid_extension

| Version | Tag-Commit | Commit-Message |
|---------|-----------|----------------|
| **v2.1.0** *(aktuell)* | `2796a87` | `chore: restore short_description for v2.1.0 entry` |
| v2.0.0 *(Basis für v2.1.0 Release Notes)* | `b81c204` | — |

Für den nächsten Release: `git log 2796a87..HEAD --oneline`

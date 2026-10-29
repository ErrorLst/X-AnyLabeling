# Packaging

Build from the repository root on the target operating system with the matching Python dependencies installed.

## Build

On Windows, use PowerShell (Bash is not required):

```powershell
# From the repository root. Defaults to win-cpu single-file.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_executable.ps1

# Or run the batch wrapper:
scripts\build_windows.bat

# GPU variant:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_executable.ps1 -Target win-gpu -Variant CUDA12
```

The Python interpreter must have the application dependencies plus PyInstaller. The script looks at `$env:PYTHON`, an active conda environment, the local `xal-cpu` environment and finally `python` on PATH. `-CheckOnly` prints the selected interpreter and paths without starting the long build.

On Linux and macOS, use Bash:

```bash
bash scripts/build_executable.sh linux-cpu
bash scripts/build_executable.sh macos-installer
```

Windows installers through Inno Setup can still be built with `bash scripts/build_executable.sh win-cpu-installer` from Git Bash; that path requires Inno Setup 6.7.3 or later. The PowerShell entry point above builds the portable single-file exe, which is the normal Windows artifact. For GPU builds set `X_ANYLABELING_VARIANT` to `CUDA11`, `CUDA12`, or `CUDA13` and install the matching extra (`gpu-cu11`, `gpu`, `gpu-cu13`).

Build outputs go to `dist/`. PyInstaller specifications are in `packaging/pyinstaller/specs`; platform installers are in `packaging/windows` and `packaging/linux`.

## Windows permission markers

A build executed inside a DSH/Windows sandbox can attach two markers to
the produced executable: extra access entries and a Low mandatory
integrity label. A Low label is especially harmful for a onefile build:
the process cannot unpack its payload into the normal temporary
directory, so the application exits before showing a window.

`scripts/clear_windows_sandbox_acls.ps1` removes both markers. It resets
the artifact permissions to the current user, SYSTEM and Administrators
(FullControl) plus BUILTIN\Users (ReadAndExecute), sets the mandatory
integrity label to Medium, saves a backup under
`%TEMP%\xal-acl-backup-*`, and verifies that no low label, sandbox,
package or AppContainer SIDs remain. The parent directory is not
changed.

The build script first runs `scripts/prepare_windows_build.ps1`: it checks
the current process integrity and sets the `dist` tree to the Medium
level before PyInstaller starts, so new artifacts inherit a clean label.
If the terminal is Low (for example a DSH workspace-write shell), that
preflight stops immediately with instructions to use a normal terminal
or DSH full access. The build script then calls
`clear_windows_sandbox_acls.ps1` automatically for Windows single-file
builds (`win-cpu` / `win-gpu`), and `scripts/package_windows.ps1` calls it for
the generated Setup.exe. If a cleanup step fails, the build stops after
the artifact was written and prints the backup path; rerun from a
normal terminal.

Manual use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/clear_windows_sandbox_acls.ps1 -Path dist\X-AnyLabeling-v4.2.0-CPU.exe -AllowRoot dist
```

## Automatic updates

Generate a signing key once and keep it outside the repository:

```bash
source .venv-cpu/bin/activate
mkdir -p ~/.config/xanylabeling
python scripts/generate_update_key.py --output ~/.config/xanylabeling/update-signing.key
```

Configure these GitHub Actions settings:

| Name | Type | Value |
| --- | --- | --- |
| `UPDATE_SIGNING_KEY` | Secret | Private key file contents |
| `UPDATE_PUBLIC_KEY` | Variable | Public key printed by the generator |
| `UPDATE_FEED_URL` | Variable | Optional HTTPS manifest URL for forks or custom hosting |
| `AUTO_UPDATE_ENABLED` | Variable | Set to `true` after testing installation and updates on each target platform |

Keep a secure backup of the signing key and reuse it for future releases; key rotation is not supported. Builds without `UPDATE_PUBLIC_KEY` cannot install updates automatically. Linux single-file builds embed their update metadata and helper. Source checkouts do not install updates automatically.

Updates reuse the published Windows `Setup.exe`, macOS DMG, and Linux executable. No separate `Update.zip` is generated or uploaded. The signed manifest records the format, size, and SHA-256 digest of each release artifact.

The Windows installer supports `/UPDATESTAGE=1` for extracting into an isolated staging directory without changing shortcuts or uninstall registration. The updater verifies the staged application, replaces the installation, and restores the previous version if startup fails. Linux replaces the existing executable in place, preserving its filename; macOS mounts the DMG read-only, copies the application bundle to staging with `ditto`, and detaches the image before replacing the installed application.

Update signatures do not replace Windows code signing or macOS signing and notarization.

## Test

```bash
source .venv-cpu/bin/activate
QT_QPA_PLATFORM=offscreen pytest tests/test_updates
```

On Windows, set `INNO_SETUP_COMPILER` to the full path of `ISCC.exe` to run the Setup tests. Before release, test installation, upgrades, failed-update recovery, and uninstallation on each target platform.

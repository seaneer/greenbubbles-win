# Release checklist

This project publishes source and a portable ZIP. It does not publish a signed
installer.

## Version and tag

1. Update `version` in `pyproject.toml` and `SERVER_INFO` in
   `src/greenbubbles_win/mcp.py`.
2. Add a `CHANGELOG.md` entry under the new version.
3. Commit, then tag with `vX.Y.Z` (for example `v0.2.1`).
4. The tag triggers `.github/workflows/release.yml`, which runs the unit layer
   and uploads the portable ZIP as a workflow artifact.

## Build locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.lock
.venv\Scripts\pip install -e .
run-tests.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-portable.ps1
```

The script writes:

- `dist\greenbubbles-win-<version>-portable.zip`
- `dist\greenbubbles-win-<version>-portable.zip.sha256`

## Publish

1. Download the artifact from the workflow run.
2. Create a **draft** GitHub Release for the tag.
3. Attach the ZIP and its `.sha256` file.
4. Copy the changelog entry into the release notes.
5. Publish only after the local pre-release checklist has been completed
   (the checklist is kept outside the public repository).

Do not attach `state/`, `reports/`, `dist/build-portable-*` or any file
containing real chat data, keys or tokens.

## Signing

The ZIP is unsigned. If you have a Windows code-signing certificate, sign the
launcher or the embedded executable before publishing and record the
certificate thumbprint in the release notes. Without a signature, SmartScreen
may show a warning on first launch.

## Checksums

Always publish the SHA-256 value. Users can verify it with:

```powershell
Get-FileHash .\greenbubbles-win-0.2.1-portable.zip -Algorithm SHA256
```

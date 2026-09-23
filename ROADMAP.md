# Roadmap / ideas

Comment on (or open) an issue first so we don't duplicate work.

## Good first issues
- [ ] Split the 5,000-line `backuppro.py` into modules (engine / UI / notifications / WinPE)
- [x] Add screenshots and an animated demo to the README
- [ ] Convert the HTML manual to Markdown
- [ ] Add a `--version` flag and a single version constant

## Security
- [ ] `wbadmin` is invoked with `-password:` on the command line (visible to other local processes) - find a safer approach
- [ ] Redact NAS address and usernames in `backup_log.txt`
- [ ] Encrypt profile exports

## Features
- [ ] Cloud targets (S3 / Backblaze / OneDrive) via rclone
- [ ] Backup verification report (HTML/PDF)
- [ ] Linux/macOS support for Quick Sync
- [ ] Unit tests for `BackupEngine`
- [ ] Build and attach a signed `.exe` on every tagged release (GitHub Actions)

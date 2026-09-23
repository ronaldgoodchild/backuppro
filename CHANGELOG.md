# Changelog

Reconstructed from the original development history (February - July 2026).

## [Unreleased]
- Security: NAS and email passwords are stored in Windows Credential Manager (`keyring`) instead of plain text in `backup_config.json`; legacy configs migrate automatically
- Renamed `backuppro3.py` to `backuppro.py`; added requirements, docs, CI

## [3.0] - 2026-07-12
- Notifications: email, SMS via carrier gateways, ntfy push
- WinPE recovery media builder, restore scripts, scheduling and profiles tabs
- 218 KB single-file release

## [3.1 - 3.3] - 2026-02-07
- Incremental backups, deduplication, encryption, verification (iterations of the "ultimate" build)

## [2.x] - 2026-02-06
- 2.0 first GUI; 2.1 advanced imaging suite; 2.2 professional edition; 2.3 master edition

## [1.0] - 2026-02-06
- Original command-line backup tool

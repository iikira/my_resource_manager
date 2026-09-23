# MySQL Backup — Design Spec

Date: 2026-09-23
Topic: mysql-backup RPM-packaged shell project with systemd timer + rclone upload

## Purpose

A shell-script project packaged as an RPM. After installation it registers as a
systemd service + timer that, once per day, dumps each MySQL database to its own
`.sql` file, zips them into `o2mysql.<YYYYMMDDHHMMSS>.zip`, uploads the zip to an
rclone remote, and removes local temporary artifacts.

## Scope (in)

- Core backup shell script (`mysql-backup.sh`).
- Configuration file template (shell key=value).
- Enable script that reads config and generates the systemd service + timer units.
- RPM spec for packaging (`rpmbuild`).
- GitHub Actions workflow (manual trigger) that builds the RPM and publishes it to
  GitHub Releases.
- Remote integration test harness (paramiko over an HTTP CONNECT proxy) that builds
  and installs the RPM on the remote host and verifies the full chain.

## Scope (out)

- rclone credential management (user pre-configures `rclone config`).
- Local retention of historical backups (uploaded zip is removed locally).
- Restoration tooling.

## Decisions (confirmed with partner)

| Concern | Decision |
|---|---|
| Config format | shell `.conf` (key=value), sourced |
| Scheduling | systemd timer (`OnCalendar=*-*-* HH:00:00`) |
| Service user | configurable in conf, default `opc` |
| Run-user config timing | `enable` script reads conf, generates service+timer units, then `systemctl enable --now` |
| RPM build | `rpmbuild` + `.spec` |
| Retention | none — local temp removed after upload |
| rclone auth | user pre-sets `rclone config`; rpm carries no credentials |
| Real-scenario test | paramiko over HTTP CONNECT proxy, remote build + `rpm -ivh` full install verification |
| Secrets | public repo; all credentials via env vars, never hardcoded |

## Architecture

### File layout (installed by RPM)

```
/usr/libexec/mysql-backup/mysql-backup.sh        # core backup logic
/usr/sbin/mysql-backup-enable                    # enable script (generates + enables units)
/etc/mysql-backup/mysql-backup.conf.template      # default config template (shipped)
/etc/mysql-backup/mysql-backup.conf               # live config (created from template, 0600)
```

### Core script `mysql-backup.sh`

1. `source /etc/mysql-backup/mysql-backup.conf` (fail if missing).
2. Stamp = `date +%Y%m%d%H%M%S`. Work dir = `/var/lib/mysql-backup/tmp/o2mysql.$Stamp/`.
3. `mysql -h $MYSQL_HOST -u $MYSQL_USER -p$MYSQL_PASS -N -B -e "SHOW DATABASES"`.
4. Exclude system schemas: `information_schema`, `performance_schema`, `sys` (always);
   `mysql` excluded by default, toggle `BACKUP_MYSQL_SCHEMA=0`.
5. For each DB: `mysqldump --single-transaction --routines --triggers --events ... > $db.sql`.
6. `zip o2mysql.$Stamp.zip *.sql` in the work dir.
7. `rclone copy` (config via `$RCLONE_CONFIG` if set, else user default) to
   `$RCLONE_REMOTE`.
8. Cleanup work dir (and zip) regardless of success/failure via `trap`.
9. Non-zero exit on any step failure; systemd captures exit code; logs via journalctl.

### Config `mysql-backup.conf` keys

```
BACKUP_HOUR=5                 # hour of day (24h) for the timer
BACKUP_USER=opc               # systemd service User=
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASS=password           # placeholder default; user edits live conf
MYSQL_EXCLUDE="information_schema performance_schema sys mysql"
RCLONE_REMOTE=drive:MySQLBackup
RCLONE_CONFIG=                # empty => user default ~/.config/rclone/rclone.conf
BACKUP_DIR=/var/lib/mysql-backup
```

### Enable script `mysql-backup-enable`

- Reads `/etc/mysql-backup/mysql-backup.conf`.
- Generates `/etc/systemd/system/mysql-backup.service` with `User=$BACKUP_USER`,
  `ExecStart=/usr/libexec/mysql-backup/mysql-backup.sh`.
- Generates `/etc/systemd/system/mysql-backup.timer` with
  `OnCalendar=*-*-* ${BACKUP_HOUR}:00:00`, `Persistent=true`.
- `systemctl daemon-reload && systemctl enable --now mysql-backup.timer`.
- Prints status / next-run via `systemctl list-timers`.

### RPM spec

- `Name: mysql-backup`, `Version: 1.0.0`, `Release: 1`, `License: MIT`.
- `Requires: rclone` and a mysql client (`mysql`, `mysqldump`) — soft: `Requires:
  /usr/bin/mysqldump` style or `Requires: mysql`.
- `%install` installs the src tree into the buildroot at FHS paths.
- `%post` prints instructions: edit `/etc/mysql-backup/mysql-backup.conf`, then run
  `mysql-backup-enable`.
- `%postun` removes generated unit files if removing the package.

### GitHub Actions

- `workflow_dispatch` (manual).
- Container: `fedora:latest` (has rpmbuild).
- Steps: checkout → set up build tree → `rpmbuild -bb` → upload `.rpm` artifacts →
  `softprops/action-gh-release` to publish to Releases (tag from ref or input).

### Remote integration test `tests/remote_integration.py`

- Based on a paramiko-over-HTTP-CONNECT-proxy connection pattern.
- Credentials exclusively via `os.environ`: `SSH_KEY_FILE`, `KEY_PASS`, `SSH_USER`,
  `DEST_HOST`, `DEST_PORT`, `PROXY_HOST`, `PROXY_PORT`, `PROXY_USER`, `PROXY_PASS`,
  `MYSQL_USER`, `MYSQL_PASS`, `RCLONE_REMOTE` (optional override).
- Flow: open HTTP CONNECT tunnel → paramiko transport → authenticate.
- Copy project `src/` + `packaging/` to remote via SFTP/`cat`.
- Remote: install build deps (`dnf install -y rpm-build rclone mysql` / `mariadb`),
  build RPM, `rpm -ivh`, edit conf from env vars, run `mysql-backup-enable`,
  manually trigger `systemctl start mysql-backup.service`, then verify a zip exists
  under `$RCLONE_REMOTE` via `rclone lsf`.
- All credential-bearing values are parameterized; the committed script hardcodes
  none.

## Security

- Public repo. No credentials in source. Config template ships `MYSQL_PASS=password`
  placeholder only; live conf is user-owned (`0600`, never shipped with a real value).
- Test script reads everything from env; no host/key/password literals.
- `.gitignore` already covers `.env`.

## Testing approach

- Shellcheck the script locally where available.
- Real-scenario: the remote harness is the primary integration test.
- Manual trigger of `mysql-backup.service` (or `systemctl start`) produces a zip and
  uploads it; `rclone lsf` confirms presence.

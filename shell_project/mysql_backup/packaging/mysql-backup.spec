Name:           mysql-backup
Version:        1.0.0
Release:        1%{?dist}
Summary:        Daily MySQL backup (per-DB .sql -> zip -> rclone) as a systemd timer service

License:        MIT
URL:            https://github.com/iikira/my_resource_manager
Source0:        %{name}-%{version}.tar.gz

# Runtime deps: a mysql client (mysql/mysqldump), zip. rclone is intentionally
# NOT a hard dependency — install/configure it separately as needed.
Requires:       /usr/bin/zip
Requires:       /usr/bin/mysqldump
Requires:       /usr/bin/mysql
BuildArch:      noarch
# Force gzip payload compression so the package installs on older rpm clients
# (e.g. CentOS 7 / rpm 4.11) that don't support PayloadIsZstd. Built on Fedora
# 41+ (rpm 6.x) which defaults to zstd.
%define _binary_payload w9.gzdio
%define _source_payload w9.gzdio

%description
mysql-backup dumps each MySQL database to its own .sql file, zips them into
o2mysql.<timestamp>.zip, uploads the zip to an rclone remote, and removes local
temporary artifacts. Installed as a systemd service + timer (configured via
/etc/mysql-backup/mysql-backup.conf and enabled with mysql-backup-enable).

%prep
%setup -q

%install
install -D -m 0755 src/usr/libexec/mysql-backup/mysql-backup.sh \
    %{buildroot}%{_libexecdir}/mysql-backup/mysql-backup.sh
install -D -m 0755 src/usr/sbin/mysql-backup-enable \
    %{buildroot}%{_sbindir}/mysql-backup-enable
install -D -m 0600 src/etc/mysql-backup/mysql-backup.conf.template \
    %{buildroot}%{_sysconfdir}/mysql-backup/mysql-backup.conf.template
# Create the live config from the template on first install only (%%post below
# handles it). Ship the template; do NOT ship a live conf with a real password.
install -d -m 0750 %{buildroot}%{_localstatedir}/lib/mysql-backup

%files
%license LICENSE
%doc README.md
%{_libexecdir}/mysql-backup/mysql-backup.sh
%{_sbindir}/mysql-backup-enable
%config(noreplace) %{_sysconfdir}/mysql-backup/mysql-backup.conf.template
%dir %attr(0750, root, root) %{_localstatedir}/lib/mysql-backup

%post
# Seed live config from template if absent.
if [ ! -f %{_sysconfdir}/mysql-backup/mysql-backup.conf ]; then
    cp -p %{_sysconfdir}/mysql-backup/mysql-backup.conf.template \
          %{_sysconfdir}/mysql-backup/mysql-backup.conf
    chmod 0600 %{_sysconfdir}/mysql-backup/mysql-backup.conf
fi

echo
echo "mysql-backup installed. Next steps:"
echo "  1. Edit %{_sysconfdir}/mysql-backup/mysql-backup.conf (set MYSQL_PASS,"
echo "     RCLONE_REMOTE, BACKUP_HOUR, BACKUP_USER, etc.)."
echo "  2. Ensure BACKUP_USER has a working rclone config for RCLONE_REMOTE."
echo "  3. Run: sudo %{_sbindir}/mysql-backup-enable"
echo "  4. (optional) test once: sudo systemctl start mysql-backup.service"
echo "     then: journalctl -u mysql-backup.service -e"
echo

%postun
# On removal (not upgrade), delete generated unit files.
if [ "$1" -eq 0 ]; then
    rm -f /etc/systemd/system/mysql-backup.service
    rm -f /etc/systemd/system/mysql-backup.timer
    systemctl daemon-reload 2>/dev/null || true
fi

%changelog
* Wed Sep 23 2026 mysql-backup <noreply@example.com> - 1.0.0-1
- Initial package.

# mysql-backup

每日 MySQL 备份工具，打包成 RPM。安装后注册为 **systemd 服务 + 定时器**：每次运行将每个数据库导出为独立的 `.sql` 文件，打包成 `o2mysql.<年月日时分秒>.zip`，上传到 rclone 远程，最后清理本地临时文件。

```
mysql -> mysqldump（逐库）-> zip -> rclone copy -> 清理
```

## 安装后的文件

| 路径 | 用途 |
|---|---|
| `/usr/libexec/mysql-backup/mysql-backup.sh` | 核心备份脚本（由服务调用执行） |
| `/usr/sbin/mysql-backup-enable` | 读取配置，生成并启用 systemd 单元 |
| `/etc/mysql-backup/mysql-backup.conf.template` | 默认配置模板 |
| `/etc/mysql-backup/mysql-backup.conf` | 实际配置（由模板生成，权限 `0600`） |
| `/var/lib/mysql-backup` | 临时暂存目录（每次运行后清理） |

## 配置文件（`/etc/mysql-backup/mysql-backup.conf`）

Shell `key=value` 格式，由脚本 source 加载。主要字段：

```
BACKUP_HOUR=5                          # 定时器每天触发的时间（24小时制，整点）
BACKUP_USER=opc                        # systemd 服务以该用户身份运行
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASS="password"                   # 在此填入真实密码（文件权限 0600）
MYSQL_EXCLUDE="information_schema performance_schema sys mysql"
RCLONE_REMOTE=drive:MySQLBackup         # 必须已通过 `rclone config` 预先配置
RCLONE_CONFIG=                          # 可选，显式指定 rclone.conf 路径
BACKUP_DIR=/var/lib/mysql-backup
```

> **安全说明**：这是**公共仓库**，切勿提交真实凭据。随包模板里的 `MYSQL_PASS="password"`
> 仅为占位符。实际 `mysql-backup.conf` 权限为 `0600`、由运行用户所有，仓库中绝不携带真实密码。
> 含特殊字符（`< > & | $` 等）的值必须加引号，因为配置文件会被 bash source 执行。

## 使用方式

```bash
# 1. 安装 RPM（通过 GitHub Actions 工作流构建）
sudo rpm -Uvh mysql-backup-1.0.0-1.*.rpm

# 2. 编辑实际配置文件
sudo vi /etc/mysql-backup/mysql-backup.conf

# 3. 确保 BACKUP_USER 已为 RCLONE_REMOTE 配置好可用的 rclone 远程
#    例如 sudo -u opc rclone config   （配置 drive: 远程）

# 4. 生成并启用 systemd 服务 + 定时器
sudo mysql-backup-enable

# 5.（可选）手动运行一次以验证
sudo systemctl start mysql-backup.service
journalctl -u mysql-backup.service -e
```

`mysql-backup-enable` 子命令：

- `mysql-backup-enable` — 生成单元 + 启用 + 启动定时器
- `mysql-backup-enable --status` — 查看定时器状态和下次运行时间
- `mysql-backup-enable --disable` — 停止并移除生成的单元

> `mysql-backup-enable` 会将 `/etc/mysql-backup/mysql-backup.conf` 的属主改为
> `BACKUP_USER`（保持 `0600`），以便服务以该用户运行时能读取配置。

## 构建 RPM（GitHub Actions）

手动运行 **Build and Release RPM** 工作流（Actions → Run workflow）。它在
Fedora 容器中构建，并将 `.rpm` 发布到 GitHub Releases。

## 本地构建 RPM（用于测试）

```bash
cd shell_project/mysql_backup
rpmdev-setuptree
mkdir -p /tmp/stage/mysql-backup-1.0.0
cp -r src /tmp/stage/mysql-backup-1.0.0/
cp packaging/mysql-backup.spec /tmp/stage/mysql-backup-1.0.0/
echo '# mysql-backup' > /tmp/stage/mysql-backup-1.0.0/README.md
cp LICENSE /tmp/stage/mysql-backup-1.0.0/ 2>/dev/null || echo dummy > /tmp/stage/mysql-backup-1.0.0/LICENSE
( cd /tmp/stage && tar czf ~/rpmbuild/SOURCES/mysql-backup-1.0.0.tar.gz mysql-backup-1.0.0 )
cp packaging/mysql-backup.spec ~/rpmbuild/SPECS/
rpmbuild -bb ~/rpmbuild/SPECS/mysql-backup.spec
```

## 集成测试

`tests/remote_integration.py` 通过 HTTP CONNECT 代理连接远程主机，在远程
构建并安装 RPM，注入凭据（仅注入到远程的配置文件中），然后验证完整备份链路。
**脚本中不硬编码任何凭据**，全部从环境变量读取。

`tests/run_test.py` 是便捷加载器，从 `.env` 文件加载环境变量后运行测试，
避免在命令行暴露凭据：

```bash
# 用法：python tests/run_test.py <.env 文件路径>
# .env 中需提供（含特殊字符的值须加引号）：
#   SSH_USER、DEST_HOST（必填）、DEST_PORT、SSH_KEY_FILE、KEY_PASS
#   PROXY_HOST（必填）、PROXY_PORT、PROXY_USER、PROXY_PASS
#   MYSQL_USER、MYSQL_PASS（必填）、MYSQL_HOST
#   RCLONE_REMOTE
python tests/run_test.py /path/to/secrets/mysql_backup/.env
```

测试流程：代理隧道 → SSH 认证 → 上传工程到远程 → 远程构建 RPM →
`rpm -ivh` 安装 → 注入配置 → `mysql-backup-enable` 注册服务 →
手动触发 `mysql-backup.service` → 校验 `rclone lsf` 远程存在 zip → 清理卸载。

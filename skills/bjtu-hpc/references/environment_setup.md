# 新电脑环境配置与账号迁移

使用本清单配置新的 macOS 控制端并迁移 BJTU HPC 账号别名。显式导出 token 或 CAS 密码时，工具会自动生成口令加密的 JSON 信封；迁移文件和解密口令仍需分开传输与保管。

## 1. 配置工作区与 Python 3.12

复制或同步完整的 `slurm` 工作区，然后设置绝对路径：

```bash
export SLURM_DIR="/absolute/path/to/slurm"
cd "$SLURM_DIR"
```

安装 Python 3.12 的 Conda/Mambaforge 环境。当前控制端优先沿用已有路径：

```bash
export HPC_PYTHON=<PYTHON3.12>
"$HPC_PYTHON" --version
```

版本必须为 Python 3.12；不要使用 macOS 自带的 `python3`。

安装依赖与 Playwright Chromium：

```bash
"$HPC_PYTHON" -m pip install -r requirements.txt
"$HPC_PYTHON" -m playwright install chromium
"$HPC_PYTHON" -c 'import requests, paramiko, playwright, mcp, jsonschema; print("dependencies ok")'
```

确认 `ssh`、`screen`、`tar` 均在 `PATH` 中。此时先不要运行深度诊断或在线认证检查。

## 2. 在旧电脑导出

最安全的默认方式只导出账号元数据，但新电脑需要重新登录或刷新 token：

```bash
"$HPC_PYTHON" hpc_accounts.py export-json ~/Desktop/bjtu-hpc-accounts.json
```

如需迁移后直接尝试使用当前认证状态，显式包含 token；只有确实需要时才包含 CAS 登录密码：

```bash
"$HPC_PYTHON" hpc_accounts.py export-json ~/Desktop/bjtu-hpc-accounts.json \
  --include-tokens --include-credentials
chmod 600 ~/Desktop/bjtu-hpc-accounts.json
```

命令会要求输入并再次确认至少 12 个字符的迁移口令。口令不进入命令行、shell history 或 JSON；含 token/CAS 凭据的导出总是自动加密。迁移 JSON 使用 `scrypt` 派生 256-bit 密钥，并以 `AES-256-GCM` 认证加密整个载荷。账号别名、token、CAS 登录名和密码均不会以明文出现在加密信封中。

默认导出全部别名；可重复传入 `--name NAME` 只选择部分账号。可用 `--encrypt` 强制加密不含秘密的元数据-only 导出。导出不会包含 Playwright 浏览器 profile、cookie 或旧电脑的 profile 路径。只能通过私密渠道传输该文件；禁止提交到 Git、粘贴到聊天或放入共享目录。

## 3. 在新电脑导入

AirDrop、云同步或移动介质可能改变文件权限，导入前先收紧权限：

```bash
chmod 600 /private/path/bjtu-hpc-accounts.json
cd "$SLURM_DIR"
"$HPC_PYTHON" hpc_accounts.py import-json /private/path/bjtu-hpc-accounts.json \
  --use-exported-default --sync-legacy-token
```

如果文件是加密信封，`import-json` 会交互式要求输入旧电脑导出时设置的口令，在内存中解密并验证 GCM 认证标签，然后才检查内部 schema/SHA-256 并写入账号库。不要先生成明文解密文件。口令错误、信封字段变化或密文被篡改都会在任何账号写入前失败。

默认冲突策略是 `error`：只要存在同名别名，整次导入不写入任何账号。先检查目标电脑，再按需使用 `--on-conflict skip`；只有明确要覆盖账号元数据/迁移密钥时才使用 `--on-conflict replace`。元数据-only 的 replace 会保留目标电脑已有的 token 和本机 browser profile 路径。

导入器会检查加密信封、格式与 schema 版本、AEAD 认证、内部 SHA-256 完整性、`0600` 文件权限、账号名及字段白名单，并且不会连接 portal。账号库和凭据库均以 `0600` 写入；Playwright profile 在后续登录时按目标电脑路径重新创建。明文迁移 JSON 只允许不含 token/CAS 凭据的元数据。

## 4. 验证并恢复认证

先在不打印敏感值的情况下检查账号，再验证导入的 token：

```bash
"$HPC_PYTHON" hpc_accounts.py list
"$HPC_PYTHON" hpc_credentials.py list
"$HPC_PYTHON" hpc_accounts.py validate --all --json
"$HPC_PYTHON" hpc_doctor.py --json
```

导入的 token 可能已经过期。对每个无效别名运行可见刷新流程：

```bash
"$HPC_PYTHON" hpc_refresh_flow.py NAME --visible-only
```

在 Playwright 窗口中完成 CAS 登录和验证码，等待 HPC portal 页面加载后关闭窗口。新电脑会为该别名创建新的隔离 browser profile。

## 5. 清理迁移文件

所有需要的别名验证通过后，从两台电脑及传输服务中删除迁移 JSON。保持 `~/.bjtu_hpc_accounts.json` 与 `~/.bjtu_hpc_credentials.json` 权限为 `0600`。不要把浏览器 profile、portal cookie、临时 SSH certificate 或迁移 JSON 放入仓库。

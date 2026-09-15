# 部署与组件维护

本技能提供 macOS 网络预检、人工尝试计数和 GPT 手动恢复说明。它不安装 VPN、不提供账号/令牌，也不包含 MotionPro Guard 原生应用。Guard 是可选的本机组件；没有它时仍可使用官方 MotionPro 与 MotionProOTP 界面完成已授权的登录。

## 部署参数

| 项目 | 默认位置 / 配置要求 |
| --- | --- |
| MotionPro 客户端 | `/Applications/MotionPro.app`；从实际 About 界面核实版本 |
| OTP 应用 | `/Applications/MotionProOTP.app`；厂商 bundle ID `net.arraynetworks.MotionProOTP`；运行实例可能使用 App Translocation 路径 |
| VPN 网关与账号 | 使用用户当前已配置并授权的站点；BJTU 网关示例为 `vpn.bjtu.edu.cn`；不在 Git 中保存账号凭据 |
| 厂商状态工具 | `/usr/local/motionpro/vpn_cmdline getstatus`，工作目录为 `/usr/local/motionpro` |
| 可选 Guard 应用 | 用户自行部署的 `MotionPro Guard.app`；安装路径、host bundle ID 和 widget bundle ID 由该部署确定 |
| 快照、策略和互斥锁 | 默认位于 `~/Library/Application Support/MotionProGuard/`，文件名为 `status.json`、`recovery-policy.json`、`guard.lock` |
| 手动尝试记录 | 同一目录的 `manual-attempt.json`；仅含随机尝试 ID、时间和结果，仍应留在 Git 之外 |
| 静态凭据 | 使用原生客户端保存的凭据或系统钥匙串；钥匙串 service/account 以本机部署为准，不发布内容 |
| 客户端配置 | `~/Library/Application Support/MotionPro/motionpro.ini`；包含敏感数据，不整文件输出或提交 |
| Guard 源码、启动配置 | 由部署者指定本地路径；本仓库不包含本机 LaunchAgent、签名产物或运行状态 |

脚本仅使用 Python 标准库；可使用配置好的 `HPC_PYTHON` 解释器运行。`manual_attempt.py` 使用 POSIX `flock`，面向这里的 macOS Guard；不用于 Windows 的 VPN 客户端。

## Guard 共享状态兼容要求

在让手动辅助脚本接管已有 Guard 前，确认该部署遵循以下协议。任一条件不符时，先暂停自动提交并适配协议，不能尝试写入一个未知的策略格式。

- Guard 运行期间持有同一个 `guard.lock` 的排他锁；手动修改必须在 Guard 已退出后取得该锁。
- `recovery-policy.json` 包含 `enabled`、`submissions`、`consecutiveFailures`、`successCount`、`manualHold`；时间采用 Swift 默认 `Date` JSON 编码，即从 2001-01-01 UTC 起的秒数。
- Guard 从文件读取策略，在重新启动后继续计算同一滚动小时的提交次数，且 JSON 解码允许未知字段。手动结算的 `motionproAccessResultId` / `motionproAccessResultOutcome` 用于中断恢复，不含认证信息。
- 快照的 `updatedAt` 使用相同的时间格式，状态标志为布尔值；快照仅用于诊断 Guard，自身不证明实际 VPN 隧道或目标资源可达。
- 手动流程只消耗共享提交预算、记录人工结果；不会增加 Guard 的自动恢复成功统计，也不会自动重新开启用户暂停的功能。

## 逐层排错

1. WidgetKit 显示、Guard 主进程和厂商隧道是不同对象。快照超过 180 秒视为陈旧；连接以厂商状态为准。
2. 配套 Guard 的预期策略为每 10 秒检查、持续离线 30 秒后恢复、最多 2 次提交/小时、连续失败 2 次暂停。检查本机实际部署是否相符。
3. OTP 包装应用在后台可能停止更新。恢复时带到前台并确认计时器推进，再取新口令；不将后台缓存当作新 OTP。
4. Guard 的辅助功能授权与 GPT 电脑操作工具的权限属于不同进程。Guard 未获授权不一定阻止 GPT 手动登录；GPT 工具也不可用时才说明具体限制。
5. 网关可能有会话寿命限制。恢复是重新认证，不代表取消时限；普通 keepalive 不能被当作绕过认证过期的证据。
6. 隧道正常但 HPC token 过期时，使用 HPC 技能的集成认证刷新，不先断开 VPN。

## 仅在任务包含组件维护时使用

- WidgetKit 图库中检查本机配置的组件名称；扩展注册成功不等于已添加到桌面。
- 用 `pluginkit -m -v -i <GUARD_WIDGET_BUNDLE_ID>` 检查所部署的扩展。直接用 Swift 编译 WidgetKit 扩展时应验证 NSExtension 入口；曾遇到缺少 `-Xlinker -e -Xlinker _NSExtensionMain` 导致扩展立即退出的问题。
- 重编译或 ad hoc 重签可能改变辅助功能授权关联的代码要求。必要时通过系统设置移除并重新添加准确的 Guard 项；系统验证由用户在本机完成，不编辑 TCC 数据库。
- 在稳定的本地构建目录签名与打包，避免云同步占位文件或 Finder 扩展属性影响编译/签名。验证实际安装产物，再注册扩展。
- 配套 Guard 若使用临时自动连接/第二密码事务，应按该部署的回滚协议处理。Qt 客户端可能在退出时覆盖磁盘配置，不能凭“已经登录成功”就删除尚待退出清理的事务。
- 日常网络接入不要求重装组件、重授权限或故意断线测试；保持正常连接。

## 官方资料

[BJTU VPN 说明](https://highpc.bjtu.edu.cn/vpn/index.htm)、[电脑端使用说明](https://highpc.bjtu.edu.cn/docs/2025-07/b95170afa1a44f579782e41fcb0f578b.docx)、[OTP 说明](https://highpc.bjtu.edu.cn/docs/2025-07/afe61c1a0a394a109af5f2d079c90b88.docx)。具体锁定政策以当前校方文档为准；本技能采用更保守的 2 次本地预算。

只输出必要的脱敏时间/状态。不要整段输出 MotionPro 日志、INI、钥匙串内容或 `launchctl print`（可能包含其他服务继承的环境密钥）。

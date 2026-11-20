# 后台运行连接器（手动启停）

连接器默认是前台进程（`serve` 占住一个窗口）。下面几组 bat 让它在后台常驻，你手动停。
**不会开机自启**，也不会注册计划任务；重启电脑后需要自己点一次启动。

## 两种接入模式

| 模式 | 绑定地址 | 适用 | 启动 | 停止 |
| --- | --- | --- | --- | --- |
| A 局域网直连 | `0.0.0.0:8787` | 可信内网机器直接访问 | `start-connector-bg.cmd` | `stop-connector.cmd` |
| B SSH 隧道（推荐） | `127.0.0.1:8787` | 调用方用 `ssh -L` 打隧道 | `start-connector-local.cmd` | `stop-connector-local.cmd` |

两者共用端口 8787 与 `state\connector.pid`，**不能同时运行**；切换时先停再启。
`connector-status.cmd` 对两种模式都适用。

## A. 局域网直连

1. 双击 `start-connector-bg.cmd`，显示 PID 后自动关窗，监听 `0.0.0.0:8787`。
2. 其他机器访问 `http://<本机IP>:8787`。
3. 双击 `stop-connector.cmd` 停止。

防火墙只放行可信来源（按网段改）：

```powershell
New-NetFirewallRule -DisplayName "greenbubbles 8787" -Direction Inbound `
  -Protocol TCP -LocalPort 8787 -RemoteAddress 192.168.0.0/16 -Action Allow
```

明文 HTTP，token 与聊天内容在网段内可见，**不要映射到公网**。

## B. SSH 隧道

### B1. 本机启动（只监听回环）

双击 `start-connector-local.cmd`，等价于：

```powershell
.\scripts\connector-service.ps1 start -BindHost 127.0.0.1 -Port 8787
```

### B2. Windows 开启 OpenSSH Server（一次性，管理员）

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-NetFirewallRule -DisplayName "OpenSSH 22" -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow
```

`sshd` 默认端口 22；已装过 SSH 服务可跳过。

验证（两条都应通过）：

```powershell
Get-WindowsCapability -Online -Name OpenSSH.Server* | Select-Object Name,State
Get-Service sshd
```

预期分别是 `State: Installed` 与 `Status: Running`。

如果 `Add-WindowsCapability` 返回 `RestartNeeded: False`，但 capability 一直停在
`InstallPending`（Win11 Home、受限 Windows Update 或被组策略锁定的机器上实测会遇到），
不要反复重试；改用官方签名构建：

1. 从 [PowerShell/Win32-OpenSSH Releases](https://github.com/PowerShell/Win32-OpenSSH/releases)
   下载 `OpenSSH-Win64.zip`。
2. 解压后检查 `sshd.exe` 的 Authenticode 签名发布者为 Microsoft：

   ```powershell
   Get-AuthenticodeSignature .\OpenSSH-Win64\sshd.exe | Format-List Status,SignerCertificate
   ```

3. 复制到 `C:\Program Files\OpenSSH`，在**管理员** PowerShell 中执行：

   ```powershell
   & 'C:\Program Files\OpenSSH\install-sshd.ps1'
   Set-Service sshd -StartupType Automatic
   Start-Service sshd
   Get-Service sshd
   ```

`InstallPending` 时服务未注册，所以前面 `Set-Service sshd` 报“没有找到服务”是预期现象；
改用 Win32-OpenSSH 后再验证 `Get-Service sshd`。

### B3. 调用方建隧道

在**调用方机器**（Linux/macOS/Windows 均可）执行：

```bash
ssh -N -L 8787:127.0.0.1:8787 <user>@<windows-host>
```

- 隧道建立后，调用方的 `http://127.0.0.1:8787` 就等于 Windows 上的连接器；
- 端口不暴露到局域网，token 与聊天内容都在 SSH 加密通道里；
- 断开 SSH 即断开访问，Windows 后台进程不受影响；
- 长期挂可加 `-o ServerAliveInterval=30`，或用 `autossh`。

### B4. 停止

双击 `stop-connector-local.cmd`；调用方 `Ctrl+C` 或结束 ssh 进程即断开隧道。

## 令牌是什么

`serve` 第一次启动生成一个 32 字节随机串（URL-safe，约 43 字符），保存在 `state\token.txt`，
等价于这个只读接口的**密码 / API Key**。

- 每个请求带 `Authorization: Bearer <token>`；
- `/health` 与 `/` 免鉴权，其余路由缺 token 或错误一律 401；
- **非本机 Agent 必须拿到它**，否则只能访问 `/health`；
- 交付方式：通过可信渠道（当面、加密消息、密码库）交给调用方；建议调用方自己保存为
  环境变量 `GB_TOKEN`。**服务端不读取任何环境变量，只读 `state\token.txt`**，
  `GB_TOKEN` 只是调用方 shell 里的变量名；
  不要写进提示词文档、代码仓库或聊天记录；
- 轮换：停止连接器 → 删除 `state\token.txt` → 启动，会生成新 token，所有调用方同步更换；
- B 模式下 token 只走 SSH 隧道，不会在局域网上明文出现；
- 日志会把 URL 里的 `?token=` 替换成 `<redacted>`。

## 运行文件

| 文件 | 内容 |
| --- | --- |
| `state\connector.pid` | 当前后台进程 PID；停止后删除 |
| `state\logs\connector-<时间戳>.log` | 启动与访问日志（stdout） |
| `state\logs\connector-<时间戳>.err.log` | 未捕获异常与回溯（stderr） |

每次启动生成一组带时间戳的日志，不会覆盖上一次。

## 排查

- **启动失败**：看最新的 `state\logs\connector-*.err.log`；常见原因是端口被占用（脚本会提示占用 PID）。
- **启动需要一点时间**：首次启动要解密缓存/预热索引，`/health` 可能 10–20 秒才通，脚本最多等 60 秒。
- **提示 already running with a different bind**：当前跑的是另一种模式，先停再启。
- **B 模式调用方连不上**：先在本机 `connector-status.cmd` 确认 health；再确认 `sshd` 在跑、22 端口可达、隧道命令没拼错。
- **隧道进程还在，但调用方 curl 报 `(52) Empty reply from server`**：SSH 转发正常、
  Windows 侧 8787 没人监听（连接器未启动或已被结束）。在 Windows 上跑
  `connector-status.cmd`，必要时 `start-connector-local.cmd` 重启；**不需要重建隧道**。
- **连接器在某次会话结束后悄悄消失**：被外部硬终止时不会留 traceback，PID 文件也可能还在。
  从桌面双击启动，不要从会被关闭的终端/Agent 会话里启动；以 `connector-status.cmd` 为准。
- **微信退出**：已有 `state\keys.json` 时仍可读缓存；否则先登录微信再启动。

## 安全

- A 模式只用于可信内网，配来源 IP 白名单，不做公网映射。
- B 模式是推荐方式：服务不碰局域网，SSH 负责加密与身份认证。
- 停止服务不会删除聊天数据、缓存或索引。

# Windows 部署说明

## 2026-10-10 修复版补充

本版修复启动脚本路径，增加进程完整性自检、手动登录模式和有限的更深控件扫描。云端只能验证模拟及假控件测试；真实客户端的运行仍需现场验证。

### 先检查执行环境

不要在带 Low 完整性标签的助手工作区中部署 Real 环境。建议使用普通用户目录（例如 `C:\Users\123\wdms_real_test\WDMS_Automation`），在该目录重新创建 `.venv`。不要直接复制旧虚拟环境，也不要只移动源码而继续使用旧工作区下的 Python。

从普通、非管理员 PowerShell，在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts\check_environment.py
```

这个只读命令不需要 `config.json`，不启动或操作 WDMS。Low/Untrusted 返回 `ENV_INTEGRITY_TOO_LOW` 和退出码 2。Medium 仅说明完整性满足最低要求，不保证没有其它令牌、会话或 UIA 访问限制。可见进程少于 20 只产生警告，不作为拒绝运行的独立依据。

如出现 `WDMS_ACCESS_DENIED` 或 `WDMS_PROCESS_IDENTITY_UNREADABLE`，检查实际运行的 Python 路径、进程令牌、WDMS 所在会话和权限；不要以强制提权、修改工作区标签或重复启动客户端代替排查。状态文件会保留连接异常类型及数字系统错误码，不写入未经脱敏的底层异常文本。按 HWND 连接仅在进程路径和唯一主窗口验证后进行，不能解决 Low 进程无法读取控件树的问题。

### 使用手动登录模式

首次配置时可复制 `config.existing-session.sample.json` 为 `config.json`，不要覆盖已存在的现场配置。该模板设定：

- `real.launch_if_missing=false`：不自动启动 WDMS。
- `real.login.mode="existing_session"`：先由使用者登录 WDMS，脚本只确认已登录会话，不读取密码凭据。
- 仍需填写登录窗口标题、`session_marker_id`、`session_marker_value` 和必要的文本来源。看见登录窗口时返回 `LOGIN_REQUIRED`；找不到已登录标记时返回 `LOGIN_STATE_UNCONFIRMED`。

该模式不要求用户名、凭据目标、登录输入控件和登录成功/失败标记。省略 `login.mode` 的旧配置仍按 `automatic` 模式处理。自动登录可配置 `success_marker_scope="main"` 或 `"login"`；脚本会等待成功标记出现，而不是在按钮调用后立即判定缺失。不要把登录窗口关闭本身当作登录成功。

### 窗口与扫描配置

主窗口和登录窗口标题都按正则完整匹配；建议分别填写 `^医药企业仓储运输管理系统$` 和 `^用户登录$`（需以现场实际标题为准）。主窗口必须为无 owner 的顶层窗口，且进程路径、窗口类名、窗口唯一性仍须匹配。宽泛表达式不能替代窗口核验。

`real.control_scan` 默认 `max_depth=6`、`max_nodes=700`，可配置深度 1–8、节点数 32–2000。Table/DataGrid/DataItem/Tree/TreeItem 可作为定位对象，但不扫描其后代，避免展开大型结果表。所有控件定位仍受隔离子进程硬时限保护。扫描不到网格行不能推断网格中没有数据。

四个采集控件 ID 可以按现场配置，不再强制等于历史模板值；必须非空且互不重复。查询证据控件仍不能复用筛选框、按钮或库存汇总字段。

### 查询证据仍是现场待解决项

`version` 要求真正的查询刷新代次，不是 `lblCount` 或库存汇总值；同值库存不能用作代次。`transition` 要求现场可观察的 busy→idle 转换，并同时确认结果货主归属。新版会在旧结果货主仍显示时记录 busy，只有 idle 且结果货主匹配后才读取库存。

这两类证据目前尚未从本机实测取得。不要把 `cmbLjq` 选中值、数值稳定、固定等待或按钮恢复可用单独当成结果归属与刷新证明。0 库存可能是真实业务结果，不能一律报错，也不能仅凭零值认定结果有效。

下一步请在 Medium 环境取得：精确 24 货主清单、导航/会话控件，以及一次查询全过程的完成和归属证据。若现有 UIA 确实不提供，需要进一步调查客户端导出或其它可核验的结果通道，再决定新的采集策略；本版不伪造证据、不解除查询门槛。

客户端显示版本和更新清单版本应分别记录，发生冲突时不要假定二者相同；`client_version` 使用当前运行客户端显示版本，并在 `validation_record` 中注明来源与冲突。

准备完整后可继续 1、3、24 货主逐级测试。正式全量仍为一次手动运行并退出，不自动创建计划任务。

本说明用于授权的 Windows WDMS 客户端电脑。当前项目开发与 Mock 验证在 macOS 完成，尚未验证 Windows 控件、凭据管理器或任务计划执行。

## 安装与配置

1. 将整个 `WDMS_Automation` 文件夹复制到本机固定目录。安装 Python 3.11 或 3.12，并确保能在终端运行 `py -3.11 --version`（或 `py -3.12 --version`）。
2. 在项目目录打开 PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
Copy-Item config.sample.json config.json
```

3. 在 `config.json` 中填写现场确认的 24 个货主名称和稳定 ID、WDMS 绝对可执行路径、唯一窗口标题正则及窗口类名、登录控件、登录状态标记、三步导航控件和目标页面标记。不要照抄历史电脑的 PID 或猜测控件值。
4. 在“控制面板 → 凭据管理器 → Windows 凭据 → 添加普通凭据”中录入用户名与密码。凭据目标必须与 `real.login.credential_target` 完全一致。配置文件只保存用户名和凭据目标名，不保存密码。
5. 查询完成信号必须先在现场由人工验证。优先使用可证明刷新代次更新的信号，并同时读取明确结果货主标识。若无代次，只可使用经现场验证的查询状态开始/完成转换和结果货主标识。单独的固定等待、查询按钮恢复、下拉框选中名称不足以启用 Real。完成验证记录后才填写策略、`confirmed`、日期、客户端版本和控件 AutomationId。

先运行 `python main.py --mode mock`，确认本机依赖和报表流程正常。现场由获授权使用者建立 WDMS 正常基线后，按 1 个货主、少量货主、24 个货主逐级验收；全部通过前不要安排无人值守任务。

## 手动运行及状态查看

项目目录下双击 `scripts\run_inventory.bat`，或执行：

```powershell
.\.venv\Scripts\python.exe main.py --mode real --limit 1
.\.venv\Scripts\python.exe main.py --mode real --limit 3
.\.venv\Scripts\python.exe main.py --mode real
.\.venv\Scripts\python.exe scripts\check_status.py --mode real
```

试验输出在 `output\real-trial\`，正式全量输出在 `output\real\`。不要把试验报表当作当日完整报表。`check_status.py --mode real --json` 可输出机器可读状态，不输出库存或货主明细。

## Windows 任务计划程序

现场验收通过后，由电脑使用者在“任务计划程序”中手动创建任务：

1. 触发器设为每天 18:00。
2. 操作选择 `scripts\run_inventory.bat`，并填写 WDMS 项目目录作为“起始于”工作目录。
3. 选择“仅当用户登录时运行”，保留可交互桌面会话。
4. 在“如果任务已在运行”中选择“不启动新实例”。程序自身的跨进程锁仍会阻止不同模式或输出目录下的并行采集。
5. 保存后手动运行一次任务并查看“上次运行结果”、本地状态和报表。锁屏、注销、休眠及客户端升级后的行为需现场观察。不要通过取消 Windows 密码、禁用锁屏或提高权限来绕过限制。

项目不自动注册任务计划，也不假设 WorkBuddy 可读取本地文件或运行本地命令。若现场 WorkBuddy 具备获准的本地执行方式，可单独安排它在 18:05 后运行 `check_status.py`。

## 退出码和故障处理

- `0`：本次模式的报表发布且状态提交完成。Real 全量仍为 `completed_pending_review`，等待人工审核。
- `2`：任务失败、配置/证据未验证、输出或状态无效。查看脱敏错误码和说明。
- `3`：已有任务持锁。等待当前任务结束后再检查状态。

查询超时、WDMS 无响应、模态窗口或货主/数值校验失败时，任务停止，不重试该货主，不关闭 WDMS。自动化子进程有动作硬时限；终止子进程不能保证恢复 WDMS。现场遇到 WDMS 无响应时由人工处置。

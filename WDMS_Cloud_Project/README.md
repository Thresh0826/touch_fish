# WDMS 库存采集与报表程序

**2026-10-14 慢网络登录等待：`one-click-steps-v7.5-network-login-wait`。** 用户选择提交后最长2分钟。登录/加载窗口持续存在按正常等待处理，窗口消失后继续在同一120秒期限内等待主窗口可用及原首页标记。仅提交一次登录；不改变点击、部门判定或冷启动响应等待。登录监督420秒、会话阶段425秒，取消和更短外层期限传给子进程。每次首页扫描独立取证。完整换包与复测按包外说明执行，详情见[登录结果等待](docs/login_completion_20261014.md)。

**2026-10-14 冷启动就绪修复：`one-click-steps-v7.4-startup-ready`。** 依据用户确认的现场原因，登录窗口出现后先以只读探活确认UI线程响应；部门状态等待每轮也要求响应，卡顿时不读取缓存状态、不累计稳定时间，恢复后重新观察至少5秒并要求状态稳定2秒。读取控件前的就绪检查与首次部门等待共享最多90秒。现有点击协议、部门选中判据及消息超时配置保持与b1c8c59a一致。源码431项通过；完整换包及冷启动复测按包外说明执行，详情见[启动等待说明](docs/startup_readiness_20261014.md)。

**2026-10-14 六项复核修复：`one-click-steps-v7.3-native-message-policy`。** 登录标签单次消息等待上限从500ms调整为2500ms，失败提示与日志保留原生错误码和耗时，先释放再记录，日志后复核已恢复的部门状态。补齐测试UTF-8、符号链接权限及无Tk兼容；加入打包静态门禁，共用报表表格生成代码，完善异常说明及关键接口类型。源码420项通过；原始现场登录日志尚未取得，真实Windows效果待复测。完整换包与操作步骤见包外说明，六项对照见[复核修改记录](docs/review_followup_20261014.md)。

以下各日期说明为历史修改记录。

**2026-10-14 测试隔离修复。** 合成采集测试统一替换运行提示窗和结果弹窗，避免Windows自动测试中的预期失败案例显示真实“库存采集已停止”窗口。新增回归在模拟Windows下检查失败、成功、期限及预算测试不启动提示窗，并核对源码/打包版Demo按钮调用工作流入口。当前全量405项通过；运行代码与edc34ac1相同。用户报告该弹窗出现在Demo一键运行期间，实际版本及进程关联仍待本轮flow和console日志确认，不能把测试隔离修复当作现场根因已确认。

**2026-10-14 启动修复：`one-click-steps-v7.1-notice-cwd`。** 修复提示窗迁移后引用未定义ROOT、在Windows上启动即失败的问题。提示窗与worker共用源码/打包资源目录解析；内部异常显示类型及代码位置，不展示异常原始参数。新增测试实际执行Windows提示窗分支与合成进程就绪/停止协议，Windows真实界面仍待现场验证。使用本次完整源码包及包外操作说明。

**2026-10-13 当前源码：一键流程 v7（`one-click-steps-v7-contracts`）。** 完整解压源码运行包，打开 `WDMS_Cloud_Project` 并双击 `启动Demo.bat`。程序按本轮实际名单采集前三个或全部24个货主，生成《x年x月x日物流库存》三列表；结果仍需人工核对。安装与回传步骤见包外 `Windows_PowerShell操作说明.md` 和 [一键流程说明](docs/workflow_trial.md)。

本轮以用户运行包（14）为基线修复输入检查、空闲判断、外层超时、合计文本误判及跨午夜校验，并拆分窗口适配、采集规则、流程编排、日志、进程监督和报表职责。采集完成采用独立任务凭证，保留人工核对标记。结构与接口见 [原生采集架构](docs/native_architecture.md)。Mac合成验证不能替代Windows现场验证。

以下历史诊断与正式Real模式说明供开发查阅，当前Demo入口按上述一键流程说明操作。

**当前现场步骤：自动启动、登录，然后诊断菜单。** 按 [菜单 MSAA 诊断说明](docs/menu_msaa_diagnostic.md) 运行 `scripts/run_startup_menu_diagnostic.py`。已有登录会话则单独使用 `scripts/diagnose_wdms_menu.py`。源码包包含它调用的 `scripts/wdms_menu_msaa.ps1`。先取得菜单证据，再推进实际导航；本版尚未证明菜单可点击。

**v2.3：** 已依据现场日志修复散件小数解析及 Excel 保存/回读，新增只读货主名单诊断。见 [v2.3 说明](docs/decimal_fix_v2_3.md)。自动切换及查询完成仍待验证，不直接运行 Real。

**窗口诊断 v2：** 现场发生未响应后，请先按 [v2 说明](docs/window_probe_fix_v2.md) 更新并运行 `scripts/check_wdms_window.py`。本轮不扫描 UIA、不执行 Real，确认窗口检查结果后再决定后续。

2026-10-10 修复版：修复 Windows 批处理路径，新增 `scripts/check_environment.py`、手动登录模板 `config.existing-session.sample.json`，调整控件定位与诊断。先读 [修复版现场交接](docs/fix_handoff_20261010.md) 和 [部署补充](docs/deployment.md)。真实查询证据尚待现场验证；本版不代表 Real 已验收。

本项目提供 Mock 和 Windows Real 两种运行模式。Mock 使用 24 个虚拟货主，可在 macOS 上完整运行；Real 通过独立 Windows 子进程控制本地 WDMS 窗口。程序不连接远程服务，不访问数据库，也不使用 AI 处理库存。

## 本地 Mock 运行

在项目目录安装普通依赖并运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --mode mock
.venv/bin/python scripts/check_status.py --mode mock
```

Mock 报表写入 `output/mock/`，状态写入 `state/mock/status.json`。报表带隐藏的任务身份页；状态只保留任务日期、模式、范围、计数、报表路径和摘要，不保存库存明细或货主清单。

常用命令：

```bash
python main.py                         # 默认 Mock
python main.py --mode mock
python scripts/check_status.py --mode mock --json
python main.py --mode real --limit 1   # Windows 现场逐级试验
python main.py --mode real --limit 3
python main.py --mode real             # 仅现场验收后执行 24 个货主
python scripts/check_status.py --mode real
```

`--limit` 只能取 1 至 23，输出会进入 `output/real-trial/`。正式 Real 任务必须省略该参数，输出写入 `output/real/` 或配置的 `real_output_dir`。`--output-dir` 可改变报表根目录，但锁和状态仍固定在项目的 `state/` 下。

程序退出码：0 表示已生成本次模式对应的报表；2 表示失败或状态无效；3 表示已有任务占用全局锁。Mock 的成功只代表模拟流程通过；Real 的 `completed_pending_review` 仍须人工核对。

## Real 模式配置

首次使用前复制 `config.sample.json` 为 `config.json`。Mock 不需要修改此配置。Real 配置中的 24 个货主名称、可执行路径、窗口标题和类名、登录控件、三步导航控件、页面标记及查询完成信号都必须在目标 Windows 电脑逐项确认。样例保留空值，程序会在缺少配置或查询证据未经现场验证时拒绝 Real 任务。

已知历史 AutomationId 仅用于配置模板的固定字段映射：`cmbLjq`、`btnSearch`、`lblZjs`、`lblSjs`。现场仍须核实客户端版本和控件归属。程序不使用坐标或 Alt+F 降级。

密码不得放入 `config.json`、批处理、命令行或日志。Real 模式从 Windows 凭据管理器读取用户指定的 Generic Credential。登录、导航和查询信号的具体控件必须由现场授权使用者确认；`query_evidence.confirmed` 默认是 `false`。将其改为 `true` 之前，需要记录验证日期、客户端版本和现场验收记录，并填入有因果依据的完成与结果货主控件。

完整的安装步骤、任务计划配置和现场限制见 [部署说明](docs/deployment.md)；首次现场验收步骤见 [验收清单](docs/acceptance.md)。

## 当前验证边界

开发环境为 macOS，没有真实 WDMS 客户端。本项目只在本地运行 Mock 和假控件测试；没有连接、启动或读取企业 WDMS，也没有创建本机定时任务。Real 的 Win32、UIA、Windows 凭据管理器、WDMS 登录、查询完成信号及 Task Scheduler 仍需在授权 Windows 环境验证。查询完成及货主归属信号没有得到现场验证前，Real 模式必须保持阻断。

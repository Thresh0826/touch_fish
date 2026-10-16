# WDMS 顶部菜单 MSAA 只读诊断

## 当前版本：容器额度诊断 v5

2026-10-12 16:07及16:08两次现场日志均验证v4已运行，但在第7个容器访问第129节点时停止，9个候选中后两个尚未扫描。顶部工具栏的ROLE_SYSTEM_CLIENT（4）包装也被过滤；是否包含目标菜单仍未知。

v5将CLIENT对象作为结构包装遍历，只读其role和子节点数，不读取包装本身name/state；子元素ID仍按原角色白名单处理。每个容器可用节点额度为剩余全局额度除以剩余容器数，整数向下取整，未用完的额度留给后续容器。耗尽本容器额度时，记录menu_msaa_container_pruned并释放本容器返回的全部COM引用，再继续下一容器；总节点仍不超过128。深度4、每对象32孩子、全局5秒与子进程硬超时保留，其他全局错误仍停止。

出现任一深度剪裁或容器额度剪裁时，最终为menu_msaa_partial，退出2；容器结束记录另有truncated标志，Python即使未收到剪裁事件也拒绝将truncated容器判定完成。此次改动仍为诊断，不执行菜单点击，不能保证已定位。

## 历史版本：分支覆盖诊断 v4

最新现场日志在第二个容器访问第32个节点时触发深度限制，剩余容器未扫描。v4 保留4层、128节点、每对象32子节点和5秒的读取预算；遇到有子节点的第4层对象时，记录 `menu_msaa_branch_pruned`，跳过它的子树，继续其他分支和容器。没有增加深度或客户端动作。

每个已读取角色的节点记录 `menu_msaa_node`：节点序号、所属容器、路径、深度、对象/子元素类型及role（不可取得时null）。只复用已有角色查询，不新增COM调用，也不记录任意界面文字。名称仍只保留原白名单。结构记录能显示窗口包装层、被过滤的角色以及目标在哪条分支缺失，减少只看到“匹配0”后反复补日志的情况。

只要有分支被跳过，末尾记录 `menu_msaa_partial`、已扫描容器数和部分匹配，返回2，不写成功完成标记。时间/节点数/子节点数超限或COM错误仍立即停止；部分证据不能作为导航成功或可点击依据。Python接受并校验完整扫描的结构记录，拒绝任何裁剪或部分结果。Windows实际编译和遍历仍需现场验证。

## 自动准备登录后再诊断

`scripts/run_startup_menu_diagnostic.py` 是新的连续入口：复用本机配置自动启动 WDMS、提交一次登录、确认主页成功，然后调用原有只读菜单诊断。不需要手动启动和登录；整个流程包含登录操作，只有菜单诊断阶段是只读。任一阶段失败或收到取消请求时停止，不重试登录、不执行菜单点击或库存查询。与一键库存流程共用 `workflow.lock`。

Windows 复用现有 Python 环境，在项目目录执行：

```powershell
& $pythonPath (Join-Path $projectPath 'scripts\run_startup_menu_diagnostic.py')
```

本机须已保存正确的 WDMS 路径和 Windows 登录凭据；已有登录会话使用下文的单独诊断入口。菜单结果位于打印的流程目录内 `menu\menu_...`；登录日志只留本机核查，不默认回传。新增 6 项 mock 流程测试覆盖调用顺序、失败停止、主页成功确认、取消和非 Windows 拦截。全量 Python 200 项通过，未验证 Windows 实际启动登录。

## 静态证据与待验证事项

收到的 ZIP 内有 5 个文件：`App.exe`、`CustomControls.dll`、`CustomForms.dll`、`FormDocking.dll`、`CapabilityComponents.dll`。本轮只通过 ZIP 读取成员字节并计算哈希，没有解压或执行附件。

- `App.exe` SHA256：`01fa61f59c9ee867aca63029be5681db2332578f758ca386c0d64f86988c36e4`
- `CustomControls.dll` SHA256：`8e8befd608377c7a8e4c30488a8917a89cc93b9a9603cac640313aa2e8ad8a80`

已检查的 CLR 元数据显示：`BaseToolStrip` 继承 `System.Windows.Forms.ToolStrip`，`CToolStrip` 继承 `BaseToolStrip`，`BaseToolStripMenuItem` 继承 `ToolStripMenuItem`，`CToolStripMenuItem` 继承 `BaseToolStripMenuItem`。`App.exe` 还包含 `Tsp_顶部子系统菜单`、`加载子系统菜单按钮`、`加载模块导航菜单` 和 `set_AccessibleName` 等符号。尚未反编译相关方法体，因此还不能确定目标菜单名称实际在哪里设置，也不能确认这些符号和目标菜单项的运行时对应关系。

微软文档说明，`ToolStripItem` 是 `Component`，由 `ToolStrip` 或其下拉容器承载；它不是独立加入窗体的普通控件。[ToolStripItem 文档](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.toolstripitem?view=netframework-4.8.1) 这使“原生 HWND 扫描可能只读到容器”成为需要验证的解释，不能据此断言 WDMS 一定暴露或一定不暴露菜单名称。

本诊断通过 [AccessibleObjectFromWindow](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessibleobjectfromwindow) 获取候选窗口的 MSAA 对象，并用 [AccessibleChildren](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessiblechildren) 读取受限子树。后者可能返回父对象的 child ID 或新的可访问对象；返回数组位置是索引，不是 child ID。脚本分别处理这两种结果，并把 `S_FALSE` 的短结果作为不完整诊断处理。

## 本轮做了什么

父入口先在最多 10 秒内监督主页窗口发现进程。发现阶段复用现有本机设置、可执行文件核验、窗口定位和进程身份检查；窗口树最多 96 个节点、深度 3、协作预算 3 秒。它只读取可见 `STATIC` 标签来确认“用户密码修改”，且日志只保留这个确认标记；同时要求存在 `MDICLIENT`。只有两个主页标记都成立且请求文件原子写入成功，才启动第二阶段。

第二阶段使用 Windows 自带 Windows PowerShell 5.1、`Accessibility` 和 `System.Web.Extensions` 程序集，在最多 20 秒的监督期限内读取候选容器。C# 协作预算为 5 秒、最多 128 个 MSAA 节点、递归深度 4、每个可访问容器最多 32 个子项。所有超限、取消、超时、COM 错误或不完整结果都会阻止成功完成标记；脚本只清理自己启动的诊断进程，不操作 WDMS 进程。

MSAA 读取只访问 role、name、state 和 child count，不读取 value，不调用 `accDoDefaultAction`、`accSelect`、输入模拟或 UIA。名称只会在去掉 `&` 并 `Trim` 后等于以下白名单时写入 `msaa.jsonl`：`在库管理`、`在库查询`、`在库商品检索`、`Tsp_顶部子系统菜单`。请求文件仅含 schema、主页 HWND、PID 和候选容器的 HWND、类名、矩形及深度。

`menu_diagnostic_complete` 只表示有界的只读枚举完成，不表示菜单已定位可点击或导航成功。零个“在库管理”匹配会单独记为 `menu_target_not_exposed`；多个匹配会记为 `menu_target_ambiguous`，不会选择候选或执行动作。关闭状态的下级菜单可能不出现在本轮子树中，因此“在库查询”或“在库商品检索”零匹配不代表它们不存在。输出的 `child_path` 只供诊断，不是可执行点击定位器。

## Windows 现场运行

由用户在 Windows 手动应用本轮四个文件。关闭旧 Demo，然后手动登录 WDMS 并停留在主页：不要打开查询页面，关闭弹窗，并保持主窗口未最小化。无需更新 EXE，也无需重跑 24 货主测试。

在 Windows PowerShell 中将两个路径替换为现场实际路径和已有的 Windows Python 解释器：

```powershell
$projectPath = 'C:\实际项目目录\WDMS_Cloud_Project'
$pythonPath = 'C:\实际Python目录\python.exe'
& $pythonPath (Join-Path $projectPath 'scripts\diagnose_wdms_menu.py')
```

两阶段监督上限合计 30 秒，另有清理时间。若 WDMS 显示未响应，立即按 Ctrl+C 停止，或使用 `--cancel-file 'C:\Temp\stop-menu-diagnostic'` 启动，并从另一个 PowerShell 窗口创建该文件；不要重复运行。辅助功能调用可能触发 WDMS 自己的处理逻辑，本方案不能保证客户端完全不受影响。

诊断结束后，只把本机诊断目录中的 `request.json`、`msaa.jsonl` 和 `diagnostic.jsonl` 放入获准的固定交接位置供用户回传。不要回传原始业务日志、账号信息或业务截图。若没有完成标记、出现 PowerShell/Add-Type 编译错误或 COM 封送错误，保留这三份诊断文件；不要据此声称导航已修复。Windows 的 PowerShell/C# 编译、COM 封送和真实菜单名称读取均需现场确认。

## Mac 本地检查结果

- `.venv/bin/python -m unittest tests.test_menu_diagnostic -q`：29 项通过，退出码 0，覆盖中断、容器名、事件顺序、计数、节点预算和v4结构记录校验，裁剪结果不得判为完成。
- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q`：206 项通过，退出码 0；输出两条 `unittest.mock` 未关闭文件的 `ResourceWarning`。
- 根目录 `git diff --check`：退出码 0。
- Python AST 解析、PowerShell UTF-8 BOM/解码及“真实货主配置文件不存在”断言：通过，退出码 0。

这些检查没有运行 Windows、PowerShell、C# 或真实 WDMS。Windows MSAA 现场结果尚未取得。

2026-10-12 修复验收缺陷：名称白名单另外保留 `Tsp_顶部子系统菜单` 作为容器证据，不将其作为业务导航目标。`AccessibleChildren` 返回数组统一由外层 `finally` 清理，覆盖短结果、预算中断和遍历异常，不在子节点分支重复释放。COM 清理已做源码核查，尚未在 Windows 运行验证。

后续交付修补：打包白名单和必需文件清单加入 `wdms_menu_msaa.ps1`，缺文件时拒绝生成包。结果校验拒绝容器未开始就完成、交错处理中途容器、计数与匹配不一致、超过 128 个访问节点以及非白名单 role/state；这些检查只判定诊断结果完整性，不授权菜单点击。

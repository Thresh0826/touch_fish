# WDMS 云端任务交接 · 2026-10-11

本文件记录当前进度。docs/ 中部分文档描述历史版本，不能据此认为导航已通过。

2026-10-14更新：下列现场记录为历史阶段；当前源码使用one-click-steps-v7.5-network-login-wait。换包与本轮登录故障复测以包外操作说明及docs/review_followup_20261014.md为准。

## 目标与工作方式

开发一个 Windows 桌面程序，一键打开 WDMS、保存并使用本机凭据登录、导航、依次查询 24 个货主，并生成仅含“货主、整件数、散件数”的 Excel。运行提示不要求确认，结束时提示结果。最终在 Windows 用 PyInstaller 打包，目标电脑无需 Python。定时执行尚未开发。

云端没有 WDMS 内网或 Windows 桌面。用户在 Windows 执行更新并上传日志；云端审阅源码、修改、离线测试、交付补丁。不要声称云端已经验证真实登录、菜单或查询。

## 已现场验证

- 24 个货主采集和三列 Excel，用户人工核对准确。散件允许小数，不能截断。
- 原生 Win32 采集、Alt+F 查询。查询按钮的 BM_CLICK 和父窗口通知此前没有触发查询；已验收的采集方式不要随意替换。
- 保存凭据后自动打开 WDMS、填写并登录。部门标签选中时蓝色、对应下拉框可用；点击已选中的标签会取消选择。
- 启动时部门状态先全不可用，约 2 秒后恢复保存状态。登录代码采用至少 5 秒只读等待、连续稳定 2 秒、点击前复查，保留已选中部门。这一稳定等待用户已验证正常。
- STATIC 标签瞬间 SendInput 点击没有生效；现实现仅对未选中标签发送一次 WM_LBUTTONDOWN/UP，并确认对应下拉框可用。
- 登录首页的原生文字只有“用户密码修改”。登录确认要求登录窗口关闭、主窗口进程与配置程序匹配、可见 MDI 工作区及该菜单标记。最新现场日志已通过。

## 当前阻断：自定义菜单定位

最新现场轮次 flow_20261010_164325_b9ab8838：自动登录通过；导航在第一步“在库管理”停止，错误 NAVIGATION_TARGET_NOT_FOUND，没有点击菜单，没有开始采集。

顶部菜单位置随用户权限变化，不能写死位置。原生扫描及有超时的自定义 WinForms Window WM_GETTEXT 没有读到“在库管理”。主要容器为 WindowsForms10.Window.8.app.0.378734a，只有左侧“用户密码修改”作为 STATIC 可读。

用户要求优先通过组件元素定位，暂不使用 OCR。后续方向：只读检查原生菜单句柄和命令 ID，或独立进程中有界检查 MSAA；结合实际客户端程序/DLL 静态分析菜单类型和事件。不能恢复无界 UIA 遍历：此前 UIA 导致 WDMS 未响应。

用户准备上班后提供实际运行 Client 目录的文件列表，先挑选 App.exe、CustomControls.dll、CustomForms.dll、CapabilityComponents.dll、ProjectCommon.dll 等与菜单有关的程序。不要索要完整数据库、连接配置或日志目录。尚未收到客户端二进制文件。

## 代码入口

- scripts/WDMS_Demo.py：GUI 和各 worker 路由。
- desktop_demo/app.py：首次配置和 GUI，一键入口已接入。
- desktop_demo/workflow.py：登录 → 导航 → 原生采集，阶段失败或无成功标记就停止，统一运行提示和完成提示。
- desktop_demo/login.py：已验证的登录实现，最新 revision 为 department-settle-home-markers-v6。
- desktop_demo/navigation.py：未通过现场的菜单定位，最新 revision 为 native-navigation-v2。已修复首次扫描日志的 path 参数冲突。
- scripts/run_native_trial.py：已验证采集，保留默认单元测试入口，并增加全流程托管提示和结果清单参数。
- main.py 的 legacy Real 模式有旧 UIA/配置门槛；不要误将其当成已经现场验收的 Demo 路径，也不要解除它的验证门槛。

## 数据与离线测试

此迁移包排除现场日志、报表、配置凭据、Windows 虚拟环境以及真实 24 货主名单。scripts/native_trial_owners.sample.json 是明确标注的离线占位名单。

复用已有Python环境，直接运行静态门禁和临时合成数据测试；不要将sample复制为真实native_trial_owners.json：

```bash
python scripts/check_source.py
python -m unittest discover -s tests -p 'test_*.py' -q
```

历史迁移检查为103项，当前本地为420项通过。缺少开发依赖时明确报告并使用获准环境，不每轮联网安装。真实名单不应提交仓库。

## Windows 测试位置

用户原环境：C:\Users\123\wdms_real_test\WDMS_Automation\.venv\Scripts\python.exe。

当前 Demo 源码：桌面\WDMS_自动登录Demo_v2\WDMS_Automation。

实际客户端：C:\Users\123\Desktop\WDMS安装包\WDMS客户端自动更新\Client\App.exe。

配置、凭据引用、流程日志：%LOCALAPPDATA%\WDMSInventoryDemo。流程证据为 workflow_logs\flow_*，含 flow.jsonl、login/、navigation/。密码仅在 Windows 凭据管理器中，不写配置 JSON 或日志。

更新后必须关闭旧 Demo 再启动；打包 exe 不会因外部源码替换而自动更新，需要重新构建。不应把其他路径里的旧 Demo 当成当前版本。

下一步从组件识别解决“在库管理”的定位，再验证“在库查询 → 在库商品检索”，之后继续验收一键流程。


2026-10-14最新冷启动修复见docs/startup_readiness_20261014.md：用户确认冷启动UI未就绪，已加入只读响应等待；点击协议及部门判定保持。


2026-10-14慢网络登录：提交后共用120秒等待登录消失、主窗口可用及首页确认；只提交一次，登录监督420秒、会话阶段425秒。详见docs/login_completion_20261014.md。

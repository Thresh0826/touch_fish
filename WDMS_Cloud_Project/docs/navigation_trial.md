# 后台自动导航与开发诊断

当前修订为 `native-navigation-v9-page-wait`。导航模块和开发诊断保留，但主界面不再提供单独导航按钮；现场只点一键运行。关闭旧Demo，把完整源码包解压到新目录，打开WDMS_Cloud_Project并双击启动Demo.bat，复用现有Python环境及本机配置。

## 本轮现场步骤

保持唯一目标账号WDMS、未最小化且无弹窗；未运行时一键流程可自动启动登录。为观察完整导航路线，可先关闭“在库商品检索”页签，再点击顶部“企业管理”回到主页。在Demo选择前3个货主并点“一键运行：登录并生成表格”，期间不要操作键鼠；导航之后自动读取名单并采集。

预期导航完成三个动作：顶部“在库管理”→侧栏“在库查询”展开→点击展开后的“在库商品检索”。若已真正展开，继续点击实际可见的检索项；若页面已打开并确认有效，导航阶段结束。最终核对货主下拉、查询按钮、整件数及散件数标签的实际可见性；仅读菜单文字或发送输入不算页面完成。导航模块本身不查询或切换货主，一键流程随后进入采集模块。

## 折叠菜单定位

IsWindowVisible检查的是WS_VISIBLE，折叠分组内被父窗口裁剪的子控件仍可能返回true。v7因而把可读的隐藏检索项误当成已展开；09:52现场发送了标签WM鼠标消息，但没有展开分组或打开页面。

v8用GetParent核对真实父链，用GetClientRect/ClientToScreen逐层求有效显示范围。若唯一“在库商品检索”标签的列表面板被其直接上层分组裁剪，且该分组属于标题已确认的“在库管理”侧栏，则推导该分组的上方标题区域为“在库查询”目标。这是结合用户确认布局的结构定位；不宣称读到了该自绘标题。布局不符合、父链不完整或目标不唯一时停止。

侧栏用SendInput发送一对实际鼠标按下/抬起。发送前核对前台、父链、裁剪、窗口命中、实时文字、键鼠状态；透明STATIC只能通过其真实祖先路径核对。先展开，再重新扫描检索项；展开失败不点击被裁剪的标签。保留原有18号图标算法。

## 打开检索页后的衔接

2026-10-13 13:17的一键日志确认三次菜单点击均已发送：13:18:41.275点击检索项，13:18:41.676开始扫描新页面，13:18:41.982因一处STATIC标签WM_GETTEXT超过100ms而报WinError=1460。此前主窗口探活成功，扫描已出现新查询区域，但没有完整页面确认或导航完成记录；货主名单阶段未启动。日志符合页面切换时短暂读取超时的情况，尚未证明完整检索页已经就绪。

v9在每次导航状态读取前后重新核对原主窗口PID、类名、可见/非最小化/可用状态。仅对带结构化WinError=1460的标签或自定义菜单文字读取失败，在既有页面等待期限内暂停后重新扫描；主窗口仍须通过探活。每次扫描使用独立文件，失败的半份扫描不参与后续页面确认。点击后等待共25秒，不因重读重新计算期限，也不重新点击菜单。定位时保留原有单次扫描5秒预算。持续超时保留原错误，权限拒绝、其他错误、取消、窗口变化或未响应则停止。

独立的货主名单阶段使用相同只读重扫机制，确认实际检索页后才读取两次完整名单。此机制不重试货主选择、查询或登录。当前工作流v5按用户最新要求去掉名单确认弹窗，读取及校验后直接采集，名称与数据在输出Excel后核对。

依据：[IsWindowVisible](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-iswindowvisible)、[WindowFromPoint](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-windowfrompoint)、[GetClientRect](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclientrect)。

## 日志回传

报错结束后停止继续操作，不重复点击。在任意PowerShell窗口运行：

```powershell
explorer.exe "$env:LOCALAPPDATA\WDMSInventoryDemo\workflow_logs"
```

一键流程导航文件在最近flow_…/navigation子目录，保留主auto_navigation、scan和存在时的_menu目录。开发者单独运行导航命令行时，默认仍写navigation_logs。sidebar_group_target、navigation_target_clipped、navigation_mouse_geometry、navigation_mouse_hit及navigation_mouse_input_sent记录父链、范围与输入；page_scan_deferred/recovered表示仅重读页面，native_scan_failed包含报错控件和错误码。成功必须有inventory_page_ready及automatic_navigation_complete。

此前Mac导航/图标/鼠标66项专项、全量265项Python检查通过。用户报告v8导航单元通过；本轮13:17日志进一步证实三次点击，但没有页面完成确认。v9的读取恢复和跨阶段连接使用临时合成数据验证，不能代替Windows现场复测。

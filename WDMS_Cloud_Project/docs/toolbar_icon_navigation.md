# 顶部在库管理图标导航

导航修订 `native-navigation-v9-page-wait`，匹配协议仍为 `toolbar-icon-v2`。用户已确认：App.exe 原始 PNG 资源18对应“在库管理”，横向位置随账号权限变化。原始142×130图标放在 `assets/toolbar_inventory_18.png`，固定SHA256由入口核对；没有执行原客户端附件或更改DLL。

当原生文字定位首个“在库管理”失败时，自动运行既有MSAA诊断。只选择当前WDMS内唯一、完整枚举、role22的顶部工具栏；其他容器被剪裁不能当作整个诊断通过。首个文字等待缩短为2秒，后两步仍各20秒。

`scripts/wdms_toolbar_icon.ps1`和`toolbar_icon_matcher.cs`使用.NET Framework的System.Drawing截取工具栏，在内存中比较原始图标。无需Windows.Media.Ocr、中文语言包、OpenCV或新增Python依赖；不联网、不上传、不持久保存画面。按工具栏高度逐个整数像素搜索图标高度，保持图标比例、裁掉外边框，使用归一化灰度相关。此前每隔2像素搜索会漏掉一半尺寸，新版补齐这些尺寸，五秒计算预算保留。粗搜索后细化位置，同一位置的不同尺寸合为一个目标；不同位置的第二候选用于歧义判断。

匹配分数至少0.86，领先第二位置至少0.06；这是相关分数阈值，不是准确率承诺。零匹配、低分或多个相近候选均停止。匹配点通过MSAA的AccessibleObjectFromPoint核对为可用的role43按钮，accLocation矩形必须包含图标且位于该工具栏；不依赖按钮名称。禁用、隐藏、离屏或无法确认的按钮不点击。

点击前重新截取匹配并核对图标及按钮位置一致，再验证PID、类名、工具栏位置、前台窗口、点击命中、键鼠状态。仅发送一次鼠标按下/抬起；部分输入只释放鼠标，不重复点击。图像搜索预算5秒，桥接子进程总监督20秒；限制只保护UI读取与定位，和QQ模型编码时间无关。

随后进入“在库查询 → 在库商品检索”。先用真实GetParent链及GetClientRect/ClientToScreen核对检索项是否被分组裁剪。只有实际显示范围有效才可跳过已经展开的分组；只有文字及WS_VISIBLE不能据此跳过。分组标题无法读到时，按用户确认布局，从唯一检索标签的父列表/分组及已确认“在库管理”侧栏推导标题区域。结构不符合则停止。侧栏用一对实际SendInput，输入前后核对父链、范围、前台、实时文字、命中及键鼠状态；展开后重扫描，再点击检索项。最后须识别货主下拉、查询按钮及整件数/散件数标签并核对进程和显示范围，才记录inventory_page_ready。不查询、切换货主或采集。详见navigation_trial.md。

## 本地证据与现场验证

2026-10-13 09:08的现场日志确认v5已在Windows执行工具栏图像匹配，耗时112ms，分数0.8534236低于0.86，因此没有点击；顶部role22容器完整且含3个role43按钮。该日志尚不能证明最高候选就是目标，也未验证点命中的按钮矩形。

2026-10-13 09:34的现场日志确认v6两次匹配均为0.8643424、高度42、耗时124/120ms，点命中role43按钮且两次位置一致，发送一次down/up。随后侧栏caption变为“在库管理”，depth8检索STATIC可读。此前仅据这些字段判断菜单已展开是错误的：分组底边142，检索标签顶边155，被折叠分组裁剪。v7因此错误跳过展开步骤。

09:51 v7现场已有一轮识别预先打开的检索页；09:52从主页的完整导航仍错误跳过第2步，标签WM鼠标消息显示delivered=true但没有打开检索页，第3步等待25秒后停止。用户四张UI截图确认必须先展开在库查询。v8修正裁剪和输入方式，顶部图标算法不变；新版真实父链、客户区及侧栏实际鼠标效果尚待Windows复测。

用Mac附带Pillow/NumPy近似部署算法，逐像素搜索在此前用户截图的分数由0.8818034提升至0.9176594；6种合成偶数高度命中正确尺寸，25种其他资源图标没有通过，重复图标和空白区域均拒绝。该研究不执行System.Drawing，不能作为新版Windows C#、跨DPI或低配性能通过的证据。0.9119是更早重采样方案的研究分数，不作为本轮结论。Python协议、位置、取消、输入和打包测试使用模拟Windows接口。

新版失败日志增加候选矩形、缩放高度、工具栏矩形、分数门槛和匹配协议；成功日志也记录缩放高度。只保存数值和位置，不保存画面。若新版仍低分，依据这些字段继续核对，不将0.8534自动判为成功。

参考微软[AccessibleObjectFromPoint](https://learn.microsoft.com/windows/win32/api/oleacc/nf-oleacc-accessibleobjectfrompoint)与[accLocation托管签名](https://learn.microsoft.com/dotnet/api/accessibility.iaccessible.acclocation)。09:34日志已验证该轮顶部按钮的点命中与输入；不能据此宣称侧栏展开、检索点击或整条导航已在Windows通过。

## Windows使用

关闭源码版Demo、保留唯一WDMS已登录且未最小化。完整解压新版源码包，在新WDMS_Cloud_Project目录双击启动Demo.bat，继续复用本机Python与现有配置。本轮先关闭检索页签，并点顶部企业管理回到主页，验证完整三步。也可在该目录的PowerShell运行：

```powershell
& .\启动Demo.bat
```

主界面不再提供单独导航按钮，选择前3个货主并点“一键运行：登录并生成表格”。运行期间不要操作键鼠，导航后继续读取名单和采集；失败即停不重复。在PowerShell执行下面命令打开日志目录，取本轮flow_…/navigation内的auto_navigation、scan和整个_menu目录：

```powershell
explorer.exe "$env:LOCALAPPDATA\WDMSInventoryDemo\workflow_logs"
```

`toolbar_icon_target`记录匹配位置/分数，`toolbar_icon_click_sent`表示已发送输入；只有页面确认及`automatic_navigation_complete`才表示本轮导航完成。旧项目残留的OCR文件不会被新入口使用。

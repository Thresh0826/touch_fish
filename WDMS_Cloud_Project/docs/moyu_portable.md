# 摸鱼小工具：带环境便携包

本说明保留用于f1f1deab基础便携包。当前v3交付是不带依赖的源码更新包，启动、截图定位和直接复制请按“摸鱼小工具操作说明.md”；它借用旧便携目录的runtime，旧目录及原ZIP不改动。

此包保留Python源码运行方式，并自带Windows 64位Python 3.12.14、Tk界面所需的Tcl/Tk、Visual C++运行库、openpyxl 3.1.5和et-xmlfile 2.0.0。不需要安装或选择Python，不调用pip，不联网下载，不修改系统PATH或注册表。

适用于Windows 10/11 64位。它仍使用系统自带的Windows PowerShell、.NET和Windows离线文字识别；缺少这些系统组件或可用OCR语言时，定位检查会说明缺项并停止。此包没有把Windows系统组件当作已验证。打包主机是Mac，只核验文件、PE架构、依赖版本及完整性；Windows启动、桥接编译、识别和业务闭环均待现场验证。

## 启动

1. 取消旧版“选择python.exe”的窗口。保留旧源码目录、旧运行包及电脑原有Python，不替换其中任何文件。
2. 将本次 **摸鱼小工具带环境便携包.zip** 完整解压到新建空文件夹，例如 **MoyuTools_Portable**。不要直接在压缩包里运行，也不要只复制某一个脚本或python.exe。
3. 双击解压目录最外层的 **启动摸鱼小工具.bat**。启动会先检查包内环境，再打开摸鱼小工具界面，不应再弹出Python选择框。

也可以在最外层解压目录打开PowerShell，执行：

`.\启动摸鱼小工具.bat`

整个WDMS_Cloud_Project\runtime文件夹都必须保留，它包含Python解释器、DLL、标准库、Tcl/Tk及表格依赖。工具子进程也使用同一包内Python，并隔离电脑上其他Python环境。环境检查失败时停止，不改用其他解释器或自动安装。

本版沿用表格逐行业务流程和原表格版进度目录：

`%LOCALAPPDATA%\MoyuTools\sheet_rows`

已在旧源码版运行过表格任务时，不需要删除进度；保持相同快照、目标文件夹和定位即可续跑。窗口改变需要重新定位并沿用旧进度，不能直接重发未知状态的查询或粘贴。

## 第一行试跑

手工登录本机WDMS，打开“送货单检索”、设好固定条件，把上方“送货单号”列拖到前面。远程桌面内打开Everything，完整显示搜索框、ZIP文件名和结果总数；准备已建立的本地目标文件夹。

在小工具选择.xlsx及工作表、导入预览，核对原表行号、下单日期和单据编号。工具按每行日期设置同一天的起止日期，将单据编号填入“合同编号”；无结果记录为未找到并继续，多条或读数不明确则暂停。原表只读，包含所选工作表全部数据行，隐藏或筛选隐藏的行也会处理；需要部分记录时先另存为只含这些记录的.xlsx。

先完成首次定位，点“一行试跑”。核对原表记录、WDMS合同编号和送货单号、远程匹配ZIP、本地ZIP能打开且内容正确后，再开始整表。30秒初始等待、5秒结果稳定以及超时暂停规则保持不变。

完整操作流程可查看同目录的 **摸鱼小工具操作说明.md**。本说明的带环境启动步骤优先于其中“选择既有Python”的源码包步骤。

## 启动或试跑失败

启动检查失败：停在黑色控制台，不反复重启，不改动runtime。保留错误文字，在任意PowerShell窗口运行：

`explorer.exe "$env:LOCALAPPDATA\MoyuTools\sheet_rows"`

回传该目录中的 **portable_startup.json** 和错误画面，并附包外 **SOURCE_MANIFEST.json**、**PORTABLE_MANIFEST.json**。若文件不存在，直接说明缺项。

如果仍弹Python选择框，说明启动的不是完整带环境包，或runtime及PORTABLE_RUNTIME.json没有正确解压；先取消，回传解压目录的文件列表画面，不要随便选择其他Python。

定位失败：回传该目录下的 **calibration.jsonl**，存在时附 **bridge_startup.txt**。业务操作失败：点“停止”，保留当前行和暂存文件，等待原查询或复制结束；用“打开日志”进入当前任务目录，回传 **events.jsonl** 及本轮的 **请求编号.console.txt**、存在时的 **请求编号.result.json**、**请求编号.watch.json**。不要重复点击查询、粘贴或删除暂存目录。

原表、业务ZIP、凭据、settings/binding/progress/request不默认回传。日志先按允许范围脱敏。公司现场由你亲自运行，WorkBuddy只读取你手工放入固定交接文件夹的允许资料。

## 来源与验证

Python采用Astral维护的python-build-standalone（20260901，Windows x86_64，CPython 3.12.14）；保留其完整许可文件、Tcl/Tk及运行库。表格库使用PyPI原始通用wheel，保留对应dist-info和许可。来源、下载大小和SHA256记在PORTABLE_MANIFEST.json与WDMS_Cloud_Project/PORTABLE_RUNTIME.json中。重建时默认只使用已核对的缓存，维护者显式使用--download才下载公开依赖；现场启动没有下载步骤。

相关项目说明：[Python Standalone Builds](https://docs.astral.sh/python-build-standalone/)、[便携发行包布局](https://docs.astral.sh/python-build-standalone/distributions/)。

包的windows_verified及windows_runtime_executed标记为false。它表示依赖已随包交付，不表示此包已在你的Windows上启动或通过真实业务验收。源码及独立解压合成检查见包外本地验证结果.json。

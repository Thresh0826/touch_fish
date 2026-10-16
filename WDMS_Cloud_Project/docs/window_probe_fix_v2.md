# WDMS 窗口诊断修复 v2（2026-10-10）

## 审阅范围与判断

本次上传的 WDMS_Automation.zip 共 39 个条目，不含旧 test_logs/wdms_verify_onsite.py 或 onsite_verify.jsonl。因此无法审阅原现场脚本，更不能从“元素数=0”确定卡死根因。0 元素可能是读取失败，不可当作控件不存在，也不可当作正常完成。

已有项目通过 pywinauto/UIA 获取属性和 children()，虽然限制扫描深度与节点数，单次 COM/提供程序调用仍可能阻塞。这种阻塞也可能影响客户端；停止诊断子进程不能保证让已卡住的 WDMS 恢复。v2 将下一轮现场诊断移到不使用 UIA 的独立路径，不宣称真实 UIA 采集已修好。

## 修改

- 新增 scripts/check_wdms_window.py，默认只枚举匹配完整标题的可见、无 owner 顶层窗口，检查最小化状态及响应性。
- 使用 Win32 窗口元数据和有 500ms 时限的 WM_NULL。未调用 UIA、children()/descendants()、RawViewWalker、查询、导出、截图、SendKeys、鼠标或剪贴板操作。未读库存或密码。
- 诊断在独立子进程执行，父进程等待上限 10 秒。超时或 Ctrl+C 只结束诊断子进程，不结束/重启 WDMS。清理自身进程另有至多约 1 秒等待；系统调度和磁盘开销不含在等待时限内。
- 每个跨进程检查阶段前写入并 flush JSONL，保存最后一步。每次新建日志，不覆盖旧现场证据。异常、超时和窗口缺失都返回 2；不能以零元素报告成功。
- test_logs/wdms_verify_onsite.py 替换为转调新入口的兼容文件。旧 --invoke-search 参数会被拒绝，避免误用原现场点击功能。
- Real 模块增加 state/real/worker_scan_<随机标识>.jsonl 记录，在属性、类型、children() 前后记步骤，并增加扫描时间及队列大小限制。没有把库存或凭据写入该日志。Real 仍依赖 UIA 和父进程硬时限；单次调用仍可能阻塞。这部分仅供后续取证，本轮不要运行 Real。

## 本轮执行

1. 由现场正常方式恢复 WDMS，确认能人工操作，再手动登录，保持主窗口可见。不要在其仍未响应时继续检查。
2. 更新前备份旧的 test_logs/wdms_verify_onsite.py，保留旧 JSONL。新包没有 .venv、config.json、状态或报表，不覆盖这些内容。
3. 使用已通过 Medium 环境检查的 .venv，从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\check_wdms_window.py --out test_logs
```

只执行这一个窗口检查，完成后先确认 WDMS 是否仍可人工操作。将终端输出和本次 onsite_win32_*.jsonl 交回云端。窗口检查成功不代表 UIA 控件、查询信号或库存采集成功。

默认标题为“医药企业仓储运输管理系统”。若实际标题不同，使用 --title 指定完整实际标题，不要用宽泛正则或从多个窗口中任意选取。

脚本另有 --native-controls，可做有限的 HWND 元数据检查（不读控件文本，原生数字 ID 也不是 AutomationId），但本轮不加此参数。先等待默认检查结果。

## 验证

云端 Linux / Python 3.12：35 项测试通过。新增测试覆盖实际子进程超时/回收、最后步骤保存、部分日志损坏、默认不枚举子控件、未响应不报成功、窗口缺失/歧义/最小化、旧点击参数拒绝、原生元数据上限及 Real 扫描错误前的记录。

Mock CLI 与状态检查成功。Windows 原生 API 调用、真实 WDMS 响应行为不能在云端复现，需要本次现场检查。原现场脚本仍需使用备份文件取证，不能根据新入口成功宣称已经定位原卡死问题。

# 2026-10-14 复核六项的修改与验证

基线为58f33f19，依据用户提供的《复核-20261014-v2.1对照6项.md》实施。当前工作流为`one-click-steps-v7.3-native-message-policy`，源码版本以本包SOURCE_MANIFEST.json为准。报告声明未修改Windows交付文件，本轮未导入其他来源代码。

| 复核项 | 本轮修改 | 验证与限制 |
| --- | --- | --- |
| P1-1 登录标签500ms | NativeMessagePolicy集中毫秒时限，登录标签按下/释放等待上限均为2500ms；记录即时WinError、timeout_ms、elapsed_ms；先释放再记录，输入失败优先保留；日志等待后再次确认部门尚未选中 | 5项登录回归在旧源码3失败/2错误，修复后通过；只发一次down/up，失败仍停止。尚无现场原始登录日志，不能证明500ms是该轮唯一根因 |
| P1-2 测试环境假设 | AST核对并补齐40处实际文本调用的UTF-8编码；符号链接权限不足或不支持时明确跳过；Tk改为GUI用例内导入，缺Tk只跳过两项GUI检查，采集隔离继续执行 | 合成无Tk子进程实际运行4项相关检查、跳过2项；模拟1314权限错误确认skip；没有创建真实货主配置 |
| P2-1 静态门禁 | scripts/check_source.py使用标准库AST和symtable检查语法、未定义全局读取、未使用导入、测试编码、裸原生消息超时及无说明的except/pass；完整交付打包前强制运行 | 当前扫描83个Python模块无发现，门禁的错误场景有回归。现有环境没有pyflakes/ruff/mypy，未联网安装；此门禁不分析动态属性和局部变量完整数据流，不能替代完整lint或类型检查 |
| P2-2 报表重复及消息超时 | 两个发布入口共用services/workbook_table.py的三列表格生成、文本货主、整数/Decimal格式及列宽；所有源码SendMessageTimeoutW时限均来自NativeMessagePolicy，QueryPolicy包含该策略，worker装配传给原生客户端 | 正式入口继续保留隐藏身份页、摘要和原子发布；试采入口继续保留日期标题、人工核对及自己的回读约束。保留两种发布契约，消除重复表格生成实现 |
| P3-1 异常忽略 | 为AST识别的14处运行代码except/pass说明具体理由：辅助显示、清理、日志回退等不得掩盖主失败 | 门禁拒绝未说明的同类忽略；没有新增忽略业务失败的路径。原有清理行为保留 |
| P3-2 类型标注 | 补充消息策略、任务DTO、进度Protocol、批次结果、GUI工作流边界、命令构造、提示窗及共享表格接口标注 | 关键公开接口get_type_hints在Python3.9解析通过；未把全库声明为已全面类型化，也没有声称通过mypy |

额外更正：工作流和登录revision都更新，现场可与旧v7.1区分；兼容再导出用明确__all__保留，实际废弃导入删除。CLOUD_HANDOFF中的旧“复制sample为真实名单后测试”步骤移除，测试直接使用临时合成数据。

本地源码全量420项通过，仍有两条既有未关闭管道ResourceWarning。打包需保存源码恢复点，强制静态门禁，核验每项清单和摘要，再在独立解压目录运行同样全量检查。Windows输入、标签实际冷启动耗时及库存结果仍由用户现场复测，包的windows_verified保持false。日后任何交付变更需同步更新WORKFLOW_REVISION和说明，不能只改外部ZIP文件名。

微软接口依据：[SendMessageTimeoutW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw)。接口返回0可能是超时或失败，消息本身的处理返回值为0不代表接口失败；不能忽略失败返回或重复点击来补救。

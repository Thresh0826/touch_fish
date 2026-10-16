# 开发与模拟验收记录

日期：2026-10-09。项目在 macOS 上完成；环境为 Python 3.9.6、openpyxl 3.1.5。Windows 部署目标建议 Python 3.11 或 3.12。

## 已完成

- 实现 Mock 与 Windows Real 两种模式、严格整数读取、货主名单与归属校验、原子 Excel 发布、任务状态、跨进程全局锁、状态检查、硬时限子进程监督及中断清理。
- Real 模式的登录、导航、控件、货主列表和查询完成/结果归属信号均由配置驱动；未知或未现场验证的配置拒绝运行。密码只从 Windows 凭据管理器读取。
- 提供 Windows 部署说明、首次现场验收清单、批处理入口、样例配置及模拟测试。

## 本机验证

在项目目录执行：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 main.py --mode mock
python3 scripts/check_status.py --mode mock --json
```

实际测试使用项目外的 `work/venv/bin/python`。18 个单元测试全部通过，覆盖模拟全量报表、错误/过期/缺项/错归属、严格数值解析、保存前失败保留旧文件、发布后摘要失败状态、跨进程锁、硬时限、KeyboardInterrupt 清理和假 UI 控件逻辑。`compileall` 返回码为 0；Mock 全量运行返回码为 0；状态检查返回 `mock_complete`。

生成的模拟工作簿为 `output/mock/2026年10月9日物流库存分析.xlsx`，包含“库存分析”工作表及 24 个模拟货主、两列整数库存值，另有 veryHidden 身份元数据页。第一行模拟库存为 0/0。此文件仅用于验证程序流程。

## 未验证范围

当前电脑没有真实 WDMS，也没有 Windows 环境。因此没有连接企业系统或测试真实查询；Win32/UIA 指针调用、实际登录、现场控件标识、完整货主列表读取和查询完成/归属判据均须在获授权的 Windows 电脑验收。Real 模式当前必须经过现场证据配置校验，查询完成和结果货主证据未经确认时会拒绝执行。没有创建本机定时任务。

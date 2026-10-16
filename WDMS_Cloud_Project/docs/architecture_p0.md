# WDMS Demo P0 改造本地检查记录

2026-10-12。QQ 任务 476b5089-317e-4caa-9f20-8b61848d38a4 写入初始实现；桌面修补本轮发现的测试错误并补充此报告。旧 QQ 验收结果仍为未通过，不能把本次检查描述成旧任务已通过。

2026-10-13 当前职责拆分与任务凭证设计见 [native_architecture.md](native_architecture.md)。以下P0初始检查记录保留作为历史，不代表本轮测试数量或现场状态。

## 当前实现范围

- core/step_executor.py 提供轻量同步步骤执行器、成功确认、取消和期限检查，失败停止后续步骤；没有线程重试。期限为协作检查，阻塞操作仍由既有子进程监督实施硬超时。
- services/trial_excel_reporter.py 独立生成货主、整件数、散件数三列表，保留小数、明确货主字符串类型，临时文件回读验证后发布。
- 登录、导航、采集入口传递取消文件及单调时钟期限；子进程监督检查取消并终止自己启动的子进程，不以此授权关闭 WDMS。
- desktop_demo/workflow.py 接入步骤执行器与本地流程锁，当前版本标记 one-click-steps-v7-contracts。基础条件检查后准备会话、导航，再由受监督的owner_discovery子进程只读实际货主名单，校验后直接采集。去掉首次/变化确认弹窗，名称和数据在输出Excel后一起核对。快照及摘要绑定原WDMS进程和主窗口；v6只采用终止事件归类失败、记录采集退出码/错误输出，并归档本轮采集日志到flow_…/collection，不复制配置、名单快照或业务表，不复用旧报告；详见workflow_trial.md。
- navigation v9与owner_discovery共用只读页面重扫：仅结构化文字读取WinError=1460在阶段等待期限内重试，每次独立扫描；原主窗口需保持身份与可用状态并通过探活。不重试登录、菜单输入或查询；完整页面确认与阶段完成记录继续作为衔接条件。
- native_trial 测试使用临时合成货主，不依赖真实现场名单。

当前目标仍为在库管理→在库查询→在库商品检索及既定库存三列报表。此改造没有证明已解决现场菜单定位问题，不另建通用 RPA 框架，不将 WorkBuddy 变成现场执行工具。

## 已执行验证

解释器：项目 .venv/bin/python，Python 3.9.6，openpyxl 3.1.5，psutil 7.2.2。

在 WDMS_Cloud_Project 目录执行：

```sh
.venv/bin/python -m unittest tests.test_step_executor tests.test_step_supervision tests.test_workflow tests.test_native_trial -q
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q
```

定向 56 项、全量 171 项均通过，退出码 0。修补内容是初始化 WorkflowTests.root，以及将两项取消测试的无效 --limit 2 改为受支持的 3；没有放宽业务参数或删除断言。

七个业务改动模块 AST 解析通过；真实 scripts/native_trial_owners.json 仍不存在。Git diff --check 通过。全量测试仍输出资源未关闭警告，测试结果通过；本轮未据此顺手扩展修改范围。

## 尚未验证

Mac 上的测试使用 mock，不证明 Windows 控件、登录等待、菜单导航、取消操作和库存采集已通过。Windows 现场实施与复测由用户完成；QQ 交付补丁尚未生成或发送。本报告没有替代新的 QQ 验收结论。

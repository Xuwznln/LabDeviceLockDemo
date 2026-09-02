# LabDeviceLockDemo

[English](README.md)

一个最小的 Uni-Lab-OS 设备包，把**调度器的锁语义变成可观测的证据**。三台虚拟设备同进程：
两台 `lock_probe_demo`（`lock_probe_a`、`lock_probe_b`）把每次动作执行记成带时间戳的区间
（账本），一台 `lock_auditor_demo`（`lock_auditor`）读两份账本，把区间关系翻译成可断言的结论。

演示内容（全部走网页式工作流提交路径：`POST /api/v1/workflow-tasks` + `GET /api/v1/scheduler/resources`）：

- **动作锁**（`lock_probe_a.occupy`，普通 `@action`）：调度器隐式申请 `(device_id, action_name)`
  动作锁，同时提交的两个 `occupy` 严格串行、先提交先执行；第二个在
  `/scheduler/resources` 里是 `waiting`，`blockers` 指向第一个 attempt；
- **`always_free`**（`lock_probe_a.peek`，`@action(always_free=True)`）：不申请动作锁，
  同一台设备、同一个动作的两次 `peek` 重叠执行。动作锁的粒度是 `(device, action)`，不同动作
  本来就不互斥，所以 always_free 的证据必须是**同一个动作的两次调用并行**；
- **物料锁**（`process_plate`，`@action(always_free=True, materials_need_lock=["plate"])`）：
  唯一的锁是 `plate` 参数解析出的权威 `material_uuid`。A、B 两台设备处理同一块 P1 串行
  （B 排在 `material` 身份后面），A 同时处理 P1 和 P2 则并行——锁按板互斥，不按设备；
- **审计即工作流节点**：`lock_auditor.audit` 经点对点 `call_device_action` 读账本，四条结论
  任一不成立就抛 `AssertionError`，任务失败——绿灯就意味着语义成立。

有意不覆盖：工作流级锁与相邻 job 之间的锁 handoff 尚未接入 `@workflow`；调度器没有优先级
抢占，竞争由统一调度器的先到先得队列决定。

## 从 GitHub 安装

```bash
unilab package install https://github.com/Xuwznln/LabDeviceLockDemo --ref <commit-sha>
```

本地开发可使用：

```bash
git clone https://github.com/Xuwznln/LabDeviceLockDemo.git
cd LabDeviceLockDemo
python -m pip install -e .
```

本地演示不需要 AK/SK，也不依赖云端实验室。

## 有终止条件的双运行时 smoke

```bash
python -m lock_demo.smoke --backend hostlink --timeout 90
python -m lock_demo.smoke --backend ros2 --timeout 120
```

smoke 启动真实运行时（`unilab -g graph/lock_demo.json`，启动时把 `@workflow` 模板上报到本机
Workflow Authority），然后复现"网页上连点几次运行"：

1. **「锁演示：准备物料」**：`lock_probe_a.prepare_plates` 按固定权威 uuid 幂等 `ensure` 两块板 P1 / P2（锁对象）。
2. **动作锁组**——四个任务背靠背创建：`occupy-1`、`occupy-2`（`lock_probe_a/occupy`，各 1.5 s）和
   `peek-1`、`peek-2`（`lock_probe_a/peek`，各 1.0 s）。`occupy-1` 持锁期间，
   `GET /api/v1/scheduler/resources` 里 `occupy-2` 为 `waiting`、身份是 `action`、`blockers` 非空；
   两个 `peek` 立即开始（`concurrency_at_start = 2`）。
3. **物料锁组**——三个任务背靠背创建：`a-p1`（`lock_probe_a/process_plate` P1）、`b-p1`
   （`lock_probe_b/process_plate` P1）、`a-p2`（`lock_probe_a/process_plate` P2）。`b-p1` 排在
   `material` 身份（P1）后面等 `a-p1` 结束；`a-p2` 与 `a-p1` 并行。
4. **「锁账本审计」**：`lock_auditor.audit` 核对 `action_lock_serialized`、
   `always_free_same_action_parallel`、`material_lock_serialized_across_devices`、
   `material_lock_is_per_plate`，任一为假即任务失败。

节点结果一律读 `GET /api/v1/workflow-tasks/{uuid}/node-runs`。

## 手动启动

```bash
python -m unilabos --backend hostlink --skip_env_check \
  --devices ./lock_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/lock_demo.json

python -m unilabos --backend ros2 --disable_hostlink --skip_env_check \
  --devices ./lock_demo --external_devices_only \
  --visual disable --disable_browser \
  -g ./graph/lock_demo.json
```

然后打开管理页面：先运行一次「锁演示：准备物料」，再快速连点两个 occupy 工作流和两个 peek
工作流的"运行"，执行期间观察 `/api/v1/scheduler/resources`。

## 目录

```text
graph/lock_demo.json               两种 backend 共用的一份图（A、B、审计器）
lock_demo/
  labware.py                       lock_demo_plate 资源 + 固定的 P1 / P2 uuid
  lock_probe.py                    occupy / peek / process_plate / prepare_plates / ledger
  lock_auditor.py                  audit：账本 -> 四条锁语义结论
  workflows.py                     @workflow 模板（准备、两个并发组、审计）
  smoke.py                         经管理 API 驱动的有终止条件真实运行时证明
tests/test_hostlink_smoke.py       HostLink 集成断言
```

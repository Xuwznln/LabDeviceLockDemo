"""锁演示的默认工作流：每条只有一个节点，并发竞争来自"同时提交多条"。

``@workflow`` 的步骤严格串行，所以调度竞争不能在一条工作流内部制造；网页上连点
几次"运行"（或 e2e/smoke 同时创建多个任务）才会让多个 attempt 同时向
``BackendScheduler`` 申请资源。三组工作流对应三种锁语义：

- 准备：``prepare_plates`` 幂等 ensure 两块演示板；
- 动作锁组（同时提交）：``occupy-1`` / ``occupy-2`` 争 ``lock_probe_a/occupy`` 的动作锁，
  第二个排队；``peek-1`` / ``peek-2`` 是同一台设备上同一个 always_free 动作，两次并行；
- 物料锁组（同时提交）：A、B 各处理 P1（争物料锁），A 同时处理 P2（同设备、同动作、异板 → 并行，
  说明 always_free + 物料锁按板互斥）；
- 审计：``lock_auditor/audit`` 读账本核对，不成立即失败。

动作锁的粒度是 ``(device_id, action_name)``：不同动作之间本来就不互斥，所以
always_free 的证据必须来自"同一个动作的两次调用重叠"。
"""

from unilabos.registry.workflows import WorkflowBuildContext, WorkflowGuide, workflow

from .labware import PLATE_P1_UUID, PLATE_P2_UUID

PREPARE_WORKFLOW_NAME = "锁演示：准备物料"
OCCUPY_FIRST_WORKFLOW_NAME = "动作锁：占用 A（第一次）"
OCCUPY_SECOND_WORKFLOW_NAME = "动作锁：占用 A（第二次）"
PEEK_FIRST_WORKFLOW_NAME = "always_free：探测 A（第一次）"
PEEK_SECOND_WORKFLOW_NAME = "always_free：探测 A（第二次）"
PROCESS_P1_ON_A_WORKFLOW_NAME = "物料锁：A 处理 P1"
PROCESS_P1_ON_B_WORKFLOW_NAME = "物料锁：B 处理 P1"
PROCESS_P2_ON_A_WORKFLOW_NAME = "物料锁：A 处理 P2"
AUDIT_WORKFLOW_NAME = "锁账本审计"

#: 占用 / 处理的持续时间：远大于派发延迟，让"重叠 / 不重叠"的判定稳定。
HOLD_SECONDS = 1.5
PEEK_SECONDS = 1.0

#: smoke / e2e 按此顺序提交：列表内为并发组。
ACTION_LOCK_GROUP = (
    OCCUPY_FIRST_WORKFLOW_NAME,
    OCCUPY_SECOND_WORKFLOW_NAME,
    PEEK_FIRST_WORKFLOW_NAME,
    PEEK_SECOND_WORKFLOW_NAME,
)
MATERIAL_LOCK_GROUP = (
    PROCESS_P1_ON_A_WORKFLOW_NAME,
    PROCESS_P1_ON_B_WORKFLOW_NAME,
    PROCESS_P2_ON_A_WORKFLOW_NAME,
)


_CONCURRENT_NOTE = (
    "@workflow 的步骤严格串行，锁竞争来自\"同时提交多条\"：先把这一组模板各实例化成工作流，"
    "再在「实验流程」页连点运行（间隔 < 1s），或按 smoke 的并发组同时创建任务。"
)


@workflow(
    display_name=PREPARE_WORKFLOW_NAME,
    description="固定 uuid 幂等 ensure 演示板 P1 / P2",
    tags=["lock-demo"],
    guide=WorkflowGuide(
        preparation=[
            "「设备」页确认探针 lock_probe_a、lock_probe_b 与审计器 lock_auditor 在线。",
            "无需手动出库：两块演示板 P1 / P2 由本模板按固定 uuid ensure（重跑幂等）。",
        ],
        expected=["任务 succeeded；「物料」页里出现 P1、P2 两块板。"],
        notes=["锁演示的第一步；跑完再做「动作锁」组、「物料锁」组，最后跑「锁账本审计」。"],
    ),
)
def prepare(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/prepare_plates",
        {},
        name="准备演示板",
        description="按固定 uuid ensure 演示板 P1 / P2 到物料权威。",
    )


@workflow(
    display_name=OCCUPY_FIRST_WORKFLOW_NAME,
    description="申请 lock_probe_a/occupy 动作锁并持有 1.5s（与第二次同时提交）",
    tags=["lock-demo", "action-lock"],
    guide=WorkflowGuide(
        preparation=["与「动作锁：占用 A（第二次）」同时提交，才能看到第二个在调度器排队。"],
        expected=["任务 succeeded；「任务排程」/「运行监控」里 occupy-1 先执行 1.5s。"],
        notes=[_CONCURRENT_NOTE],
    ),
)
def occupy_first(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/occupy",
        {"tag": "occupy-1", "duration_s": HOLD_SECONDS},
        name="占用 A #1",
        description="申请 (lock_probe_a, occupy) 动作锁并持有 1.5s，账本记下起止时间。",
    )


@workflow(
    display_name=OCCUPY_SECOND_WORKFLOW_NAME,
    description="同一动作锁的第二个申请：在调度器排队（waiting，blockers 指向第一次），第一次释放后才执行",
    tags=["lock-demo", "action-lock"],
    guide=WorkflowGuide(
        preparation=["紧接「动作锁：占用 A（第一次）」之后提交（第一次还在执行中）。"],
        expected=[
            "提交后先在「任务排程」看到资源申请 waiting，blockers 指向第一次的 attempt。",
            "第一次释放后才开始执行，两次占用的时间区间不重叠；任务最终 succeeded。",
        ],
        notes=[_CONCURRENT_NOTE],
    ),
)
def occupy_second(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/occupy",
        {"tag": "occupy-2", "duration_s": HOLD_SECONDS},
        name="占用 A #2",
        description="同一动作锁的第二个申请：排队等第一次释放后再持有 1.5s。",
    )


@workflow(
    display_name=PEEK_FIRST_WORKFLOW_NAME,
    description="always_free 动作第一次：不申请动作锁，资源申请没有任何锁身份",
    tags=["lock-demo", "always-free"],
    guide=WorkflowGuide(
        preparation=["与「always_free：探测 A（第二次）」同时提交。"],
        expected=["任务 succeeded；资源申请里没有任何锁身份（always_free 不申请动作锁）。"],
        notes=[_CONCURRENT_NOTE],
    ),
)
def peek_first(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/peek",
        {"tag": "peek-1", "duration_s": PEEK_SECONDS},
        name="探测 A #1",
        description="@action(always_free=True) 的探测动作，持续 1s，不申请动作锁。",
    )


@workflow(
    display_name=PEEK_SECOND_WORKFLOW_NAME,
    description="always_free 动作第二次：与第一次同设备同动作，仍然并行（普通动作会像 occupy 一样排队）",
    tags=["lock-demo", "always-free"],
    guide=WorkflowGuide(
        preparation=["紧接「always_free：探测 A（第一次）」之后提交。"],
        expected=["两次探测的时间区间重叠（同设备同动作仍并行），与 occupy 的排队形成对照；任务 succeeded。"],
        notes=[_CONCURRENT_NOTE],
    ),
)
def peek_second(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/peek",
        {"tag": "peek-2", "duration_s": PEEK_SECONDS},
        name="探测 A #2",
        description="与第一次同设备同动作的第二次探测，应与第一次并行执行。",
    )


@workflow(
    display_name=PROCESS_P1_ON_A_WORKFLOW_NAME,
    description="A 处理 P1：独占 P1 的物料锁 1.5s",
    tags=["lock-demo", "material-lock"],
    guide=WorkflowGuide(
        preparation=[
            "先跑「锁演示：准备物料」，P1 / P2 已在「物料」页里。",
            "与「物料锁：B 处理 P1」「物料锁：A 处理 P2」同时提交。",
        ],
        expected=["任务 succeeded；A 先持有 P1 的物料锁 1.5s。"],
        notes=[_CONCURRENT_NOTE],
    ),
)
def process_p1_on_a(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/process_plate",
        {"plate": {"uuid": PLATE_P1_UUID}, "tag": "a-p1", "duration_s": HOLD_SECONDS},
        name="A 处理 P1",
        description="materials_need_lock=[\"plate\"]：按 P1 的权威 uuid 申请物料锁并持有 1.5s。",
    )


@workflow(
    display_name=PROCESS_P1_ON_B_WORKFLOW_NAME,
    description="B 处理同一块 P1：不同设备、无动作锁冲突，仍因物料锁排队到 A 之后",
    tags=["lock-demo", "material-lock"],
    guide=WorkflowGuide(
        preparation=["紧接「物料锁：A 处理 P1」之后提交。"],
        expected=[
            "「任务排程」里 B 的资源申请 waiting，blockers 指向 A 处理 P1（不同设备也互斥，因为是同一块板）。",
            "A 释放后 B 才开始，两段时间不重叠；任务 succeeded。",
        ],
        notes=[_CONCURRENT_NOTE],
    ),
)
def process_p1_on_b(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_b/process_plate",
        {"plate": {"uuid": PLATE_P1_UUID}, "tag": "b-p1", "duration_s": HOLD_SECONDS},
        name="B 处理 P1",
        description="另一台设备处理同一块 P1：没有动作锁冲突，但物料锁让它排在 A 之后。",
    )


@workflow(
    display_name=PROCESS_P2_ON_A_WORKFLOW_NAME,
    description="A 同时处理另一块 P2：同设备、同动作（always_free）、异板 → 与 A 处理 P1 并行",
    tags=["lock-demo", "material-lock"],
    guide=WorkflowGuide(
        preparation=["紧接「物料锁：A 处理 P1」之后提交。"],
        expected=["与 A 处理 P1 的时间区间重叠（同设备同动作但不同板，互不阻塞）；任务 succeeded。"],
        notes=[_CONCURRENT_NOTE],
    ),
)
def process_p2_on_a(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/process_plate",
        {"plate": {"uuid": PLATE_P2_UUID}, "tag": "a-p2", "duration_s": HOLD_SECONDS},
        name="A 处理 P2",
        description="同一台设备处理另一块板 P2：物料锁按板互斥，与处理 P1 并行。",
    )


@workflow(
    display_name=AUDIT_WORKFLOW_NAME,
    description="读取两台探针的账本，核对四条锁语义结论（不成立即失败）",
    tags=["lock-demo", "audit"],
    guide=WorkflowGuide(
        preparation=["动作锁组（4 条）与物料锁组（3 条）都跑完并全部结束后再跑。"],
        expected=[
            "任务 succeeded，返回值里四条结论都为真：occupy 两次不重叠、peek 两次重叠、A/B 处理 P1 不重叠、A 处理 P1/P2 重叠。",
            "任一结论不成立任务即 failed，返回值指出是哪一条。",
        ],
    ),
)
def audit(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_auditor/audit",
        {"probe_a": "lock_probe_a", "probe_b": "lock_probe_b"},
        name="审计锁账本",
        description="读两台探针记录的起止时间，核对四条锁语义结论。",
    )

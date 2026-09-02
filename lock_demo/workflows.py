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

from unilabos.registry.workflows import WorkflowBuildContext, workflow

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


@workflow(display_name=PREPARE_WORKFLOW_NAME, description="固定 uuid 幂等 ensure 演示板 P1 / P2", tags=["lock-demo"])
def prepare(ctx: WorkflowBuildContext) -> None:
    ctx.run("lock_probe_a/prepare_plates", {}, name="准备演示板")


@workflow(
    display_name=OCCUPY_FIRST_WORKFLOW_NAME,
    description="申请 lock_probe_a/occupy 动作锁并持有 1.5s（与第二次同时提交）",
    tags=["lock-demo", "action-lock"],
)
def occupy_first(ctx: WorkflowBuildContext) -> None:
    ctx.run("lock_probe_a/occupy", {"tag": "occupy-1", "duration_s": HOLD_SECONDS}, name="占用 A #1")


@workflow(
    display_name=OCCUPY_SECOND_WORKFLOW_NAME,
    description="同一动作锁的第二个申请：在调度器排队（waiting，blockers 指向第一次），第一次释放后才执行",
    tags=["lock-demo", "action-lock"],
)
def occupy_second(ctx: WorkflowBuildContext) -> None:
    ctx.run("lock_probe_a/occupy", {"tag": "occupy-2", "duration_s": HOLD_SECONDS}, name="占用 A #2")


@workflow(
    display_name=PEEK_FIRST_WORKFLOW_NAME,
    description="always_free 动作第一次：不申请动作锁，资源申请没有任何锁身份",
    tags=["lock-demo", "always-free"],
)
def peek_first(ctx: WorkflowBuildContext) -> None:
    ctx.run("lock_probe_a/peek", {"tag": "peek-1", "duration_s": PEEK_SECONDS}, name="探测 A #1")


@workflow(
    display_name=PEEK_SECOND_WORKFLOW_NAME,
    description="always_free 动作第二次：与第一次同设备同动作，仍然并行（普通动作会像 occupy 一样排队）",
    tags=["lock-demo", "always-free"],
)
def peek_second(ctx: WorkflowBuildContext) -> None:
    ctx.run("lock_probe_a/peek", {"tag": "peek-2", "duration_s": PEEK_SECONDS}, name="探测 A #2")


@workflow(
    display_name=PROCESS_P1_ON_A_WORKFLOW_NAME,
    description="A 处理 P1：独占 P1 的物料锁 1.5s",
    tags=["lock-demo", "material-lock"],
)
def process_p1_on_a(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/process_plate",
        {"plate": {"uuid": PLATE_P1_UUID}, "tag": "a-p1", "duration_s": HOLD_SECONDS},
        name="A 处理 P1",
    )


@workflow(
    display_name=PROCESS_P1_ON_B_WORKFLOW_NAME,
    description="B 处理同一块 P1：不同设备、无动作锁冲突，仍因物料锁排队到 A 之后",
    tags=["lock-demo", "material-lock"],
)
def process_p1_on_b(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_b/process_plate",
        {"plate": {"uuid": PLATE_P1_UUID}, "tag": "b-p1", "duration_s": HOLD_SECONDS},
        name="B 处理 P1",
    )


@workflow(
    display_name=PROCESS_P2_ON_A_WORKFLOW_NAME,
    description="A 同时处理另一块 P2：同设备、同动作（always_free）、异板 → 与 A 处理 P1 并行",
    tags=["lock-demo", "material-lock"],
)
def process_p2_on_a(ctx: WorkflowBuildContext) -> None:
    ctx.run(
        "lock_probe_a/process_plate",
        {"plate": {"uuid": PLATE_P2_UUID}, "tag": "a-p2", "duration_s": HOLD_SECONDS},
        name="A 处理 P2",
    )


@workflow(
    display_name=AUDIT_WORKFLOW_NAME,
    description="读取两台探针的账本，核对四条锁语义结论（不成立即失败）",
    tags=["lock-demo", "audit"],
)
def audit(ctx: WorkflowBuildContext) -> None:
    ctx.run("lock_auditor/audit", {"probe_a": "lock_probe_a", "probe_b": "lock_probe_b"}, name="审计锁账本")

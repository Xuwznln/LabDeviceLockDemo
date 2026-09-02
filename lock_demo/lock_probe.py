"""锁探针设备 — 把调度器的三种锁语义变成可观测的时间区间。

每个动作执行时在本设备的账本（ledger）里记一条 ``{action, tag, started_at,
finished_at}``。三种动作只差在锁声明上：

- ``occupy``：普通动作。调度器为它隐式申请 ``(device_id, occupy)`` 动作锁，
  同一设备上两次 ``occupy`` 一定串行，且按提交顺序先到先得；
- ``peek``：``always_free=True``。不申请动作锁，可以和本设备任何动作并行；
- ``process_plate``：``always_free=True`` + ``materials_need_lock=["plate"]``。
  不申请动作锁，但独占 ``plate`` 参数解析出的权威物料 uuid——不同设备处理同一块
  板串行，同一设备处理不同板并行。

设备自身不做任何排队：并发、互斥全部由 ``BackendScheduler`` 在派发前决定，
账本只是事后证据（由 ``lock_auditor.audit`` 核对）。
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from unilabos.registry.decorators import action, device, not_action, topic_config

from .labware import PLATE_UUIDS, build_lock_plate


@device(
    id="lock_probe_demo",
    display_name="锁探针",
    category=["virtual_device"],
    description="用带时间戳的账本暴露调度器的动作锁、always_free 与物料锁语义",
    supported_backends=["hostlink", "ros2"],
)
class LockProbeDemo:
    """执行即记账的探针设备。"""

    run_in_test_mode = True

    def __init__(self, device_id: Optional[str] = None, **kwargs: Any) -> None:
        """初始化锁探针。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 lock_probe_demo。
        """
        self.device_id = device_id or "lock_probe_demo"
        self.logger = logging.getLogger(f"LockProbe.{self.device_id}")
        self._start_time = time.time()
        self._records: List[Dict[str, Any]] = []
        self._ledger_lock = threading.Lock()
        self._active: int = 0
        self._peak_concurrency: int = 0

    @not_action
    def post_init(self, node: Any) -> None:
        self._device_node = node

    @property
    @topic_config(period=1.0)
    def heartbeat(self) -> int:
        """自启动以来的心跳秒数。"""
        return int(time.time() - self._start_time)

    @property
    @topic_config()
    def active_actions(self) -> int:
        """当前正在本设备上执行的动作数（>1 说明有并行）。"""
        return self._active

    @property
    @topic_config()
    def peak_concurrency(self) -> int:
        """历史最大并行动作数。"""
        return self._peak_concurrency

    # ── 账本 ────────────────────────────────────────────────

    @not_action
    def _run_timed(self, action_name: str, tag: str, duration_s: float, extra: Dict[str, Any]) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "device_id": self.device_id,
            "action": action_name,
            "tag": tag,
            "started_at": time.time(),
            "finished_at": None,
            **extra,
        }
        with self._ledger_lock:
            self._records.append(record)
            self._active += 1
            self._peak_concurrency = max(self._peak_concurrency, self._active)
            concurrency_at_start = self._active
        self.logger.info(
            f"[LockProbe] {action_name}({tag}) 开始，持续 {duration_s}s，本设备并行数 {concurrency_at_start}"
        )
        try:
            time.sleep(max(0.0, float(duration_s)))
        finally:
            with self._ledger_lock:
                record["finished_at"] = time.time()
                self._active -= 1
        self.logger.info(f"[LockProbe] {action_name}({tag}) 结束")
        return {
            "success": True,
            "device_id": self.device_id,
            "action": action_name,
            "tag": tag,
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "concurrency_at_start": concurrency_at_start,
        }

    # ── 动作 ────────────────────────────────────────────────

    @action(
        display_name="占用",
        description="普通动作：隐式申请 (device_id, occupy) 动作锁，同一设备上串行、先提交先执行",
        feedback_interval=1.0,
    )
    def occupy(self, tag: str = "occupy", duration_s: float = 1.5) -> Dict[str, Any]:
        """占用本设备的 occupy 动作锁并持续一段时间。

        Args:
            tag[标签]: 写入账本的记录标签，供审计按名核对。
            duration_s[持续秒数]: 动作持续时间。
        """
        return self._run_timed("occupy", tag, duration_s, {})

    @action(
        display_name="探测",
        description="always_free 动作：不申请动作锁，可与本设备正在执行的 occupy 并行",
        always_free=True,
        feedback_interval=1.0,
    )
    def peek(self, tag: str = "peek", duration_s: float = 0.6) -> Dict[str, Any]:
        """不受动作锁约束的只读式探测。

        Args:
            tag[标签]: 写入账本的记录标签。
            duration_s[持续秒数]: 动作持续时间。
        """
        return self._run_timed("peek", tag, duration_s, {})

    @action(
        display_name="处理板",
        description="always_free + materials_need_lock=[plate]：不申请动作锁，只独占 plate 指向的权威物料",
        always_free=True,
        materials_need_lock=["plate"],
        feedback_interval=1.0,
    )
    def process_plate(
        self, plate: dict, tag: str = "process", duration_s: float = 1.5
    ) -> Dict[str, Any]:
        """处理一块板：执行期间该板的物料锁被本 attempt 独占。

        Args:
            plate[板]: 权威物料引用 ``{"uuid": material_uuid}``。
            tag[标签]: 写入账本的记录标签。
            duration_s[持续秒数]: 动作持续时间。
        """
        from unilabos.resources.materials import resolve_materials_gateway

        plate_uuid = str((plate or {}).get("uuid") or "").strip()
        if not plate_uuid:
            raise ValueError("plate 必须携带权威物料 uuid")
        # 锁身份必须是权威发放的物料：这里回读权威确认它存在（不存在会抛错让 job 失败）。
        material = resolve_materials_gateway().get_material(plate_uuid)
        return self._run_timed(
            "process_plate",
            tag,
            duration_s,
            {"plate_uuid": plate_uuid, "plate_name": material.material.name},
        )

    @action(
        display_name="准备演示板",
        description="按固定 uuid 幂等 ensure 两块演示板 P1 / P2（物料锁的锁对象）",
        always_free=True,
        feedback_interval=1.0,
    )
    def prepare_plates(self) -> Dict[str, Any]:
        """确保权威中存在 P1 / P2 两块板。"""
        from unilabos.resources import materials

        ensured: Dict[str, str] = {}
        for label, plate_uuid in PLATE_UUIDS.items():
            tree_set = materials.ensure(build_lock_plate(f"lock_plate_{label}", plate_uuid))
            root = tree_set.trees[0].root_node.res_content
            assert root.uuid == plate_uuid, (root.uuid, plate_uuid)
            ensured[label] = root.uuid
        self.logger.info(f"[LockProbe] 演示板就绪: {ensured}")
        return {"success": True, "plates": ensured}

    @action(
        display_name="读取账本",
        description="返回本设备全部执行记录（时间区间），供审计器核对锁语义",
        always_free=True,
        feedback_interval=1.0,
    )
    def ledger(self) -> Dict[str, Any]:
        """读取账本（always_free，不影响正在执行的动作）。"""
        with self._ledger_lock:
            records = [dict(record) for record in self._records]
        return {
            "success": True,
            "device_id": self.device_id,
            "records": records,
            "peak_concurrency": self._peak_concurrency,
        }

    @action(
        display_name="清空账本",
        description="清空执行记录与并行峰值",
        always_free=True,
        feedback_interval=1.0,
    )
    def reset_ledger(self) -> Dict[str, Any]:
        """清空账本。"""
        with self._ledger_lock:
            cleared = len(self._records)
            self._records.clear()
            self._peak_concurrency = self._active
        return {"success": True, "cleared": cleared}

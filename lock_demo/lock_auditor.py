"""锁审计器 — 读取各探针的账本，按时间区间核对调度器的锁语义。

审计是一个普通的工作流节点：它经共用 ``DeviceNode.call_device_action`` 点对点读取
``lock_probe_*.ledger``，把区间关系翻译成四条可断言的结论；任一条不成立就抛
``AssertionError``，让这个 job 失败、任务失败——证据不通过就不该有绿灯。

四条结论（标签由 workflows.py 固定）：

1. 动作锁串行：同一设备上 ``occupy-1`` / ``occupy-2`` 区间不重叠，且先提交的先执行；
2. always_free 并行：同一设备上同一动作的 ``peek-1`` / ``peek-2`` 区间重叠
   （动作锁粒度是 (device, action)，不同动作本来就不互斥，所以证据必须是同一动作的两次调用）；
3. 物料锁串行：A 上的 ``a-p1`` 与 B 上的 ``b-p1`` 处理同一块板，区间不重叠且 A 先；
4. 物料锁按板：A 上的 ``a-p2`` 处理另一块板，与 ``a-p1`` 重叠（同设备同动作，always_free 免动作锁，
   物料锁只按板互斥）。
"""

import logging
import time
from typing import Any, Dict, List, Optional

from unilabos.registry.decorators import action, device, not_action, topic_config

#: 默认探针节点 id（需与 graph 一致）。
DEFAULT_PROBES = ["lock_probe_a", "lock_probe_b"]


def _interval(records: List[Dict[str, Any]], device_id: str, tag: str) -> Dict[str, Any]:
    matches = [
        record
        for record in records
        if record.get("device_id") == device_id and record.get("tag") == tag
    ]
    if len(matches) != 1:
        raise AssertionError(f"{device_id} 账本里标签 {tag!r} 应恰有 1 条记录，实际 {len(matches)}")
    record = matches[0]
    if record.get("finished_at") is None:
        raise AssertionError(f"{device_id}.{tag} 尚未结束，账本不完整")
    return record


def _overlaps(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    return first["started_at"] < second["finished_at"] and second["started_at"] < first["finished_at"]


@device(
    id="lock_auditor_demo",
    display_name="锁审计器",
    category=["virtual_device"],
    description="读取锁探针账本并核对动作锁 / always_free / 物料锁的时间区间关系",
    supported_backends=["hostlink", "ros2"],
)
class LockAuditorDemo:
    """把账本区间翻译成锁语义结论的审计设备。"""

    run_in_test_mode = True

    def __init__(
        self,
        device_id: Optional[str] = None,
        probes: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        """初始化锁审计器。

        Args:
            device_id[设备ID]: 设备实例 ID，默认 lock_auditor_demo。
            probes[探针列表]: 需要读取账本的探针节点 id，默认 lock_probe_a / lock_probe_b。
        """
        self.device_id = device_id or "lock_auditor_demo"
        self.probes = [str(item) for item in (probes or DEFAULT_PROBES)]
        self.logger = logging.getLogger(f"LockAuditor.{self.device_id}")
        self._start_time = time.time()
        self._audits: int = 0
        self._last_verdict: str = ""

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
    def last_verdict(self) -> str:
        """最近一次审计结论摘要。"""
        return self._last_verdict

    @not_action
    def _collect(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for probe in self.probes:
            ledger = self._device_node.call_device_action(
                probe, "ledger", {}, server_wait_timeout=10.0, timeout=10.0
            )
            records.extend(ledger.get("records") or [])
        return records

    @action(
        display_name="审计锁账本",
        description="核对 occupy 串行、peek 并行、同板串行、异板并行四条结论，不成立即失败",
        always_free=True,
        feedback_interval=1.0,
    )
    def audit(
        self,
        probe_a: str = "lock_probe_a",
        probe_b: str = "lock_probe_b",
    ) -> Dict[str, Any]:
        """读取账本并核对锁语义。

        Args:
            probe_a[探针A]: 承担动作锁 / always_free / 异板并行场景的探针节点 id。
            probe_b[探针B]: 与 A 争同一块板的探针节点 id。
        """
        self._audits += 1
        records = self._collect()

        occupy_1 = _interval(records, probe_a, "occupy-1")
        occupy_2 = _interval(records, probe_a, "occupy-2")
        peek_1 = _interval(records, probe_a, "peek-1")
        peek_2 = _interval(records, probe_a, "peek-2")
        a_p1 = _interval(records, probe_a, "a-p1")
        b_p1 = _interval(records, probe_b, "b-p1")
        a_p2 = _interval(records, probe_a, "a-p2")

        checks = {
            "action_lock_serialized": (
                not _overlaps(occupy_1, occupy_2)
                and occupy_2["started_at"] >= occupy_1["finished_at"]
            ),
            "always_free_same_action_parallel": _overlaps(peek_1, peek_2),
            "material_lock_serialized_across_devices": (
                a_p1.get("plate_uuid") == b_p1.get("plate_uuid")
                and not _overlaps(a_p1, b_p1)
                and b_p1["started_at"] >= a_p1["finished_at"]
            ),
            "material_lock_is_per_plate": (
                a_p1.get("plate_uuid") != a_p2.get("plate_uuid") and _overlaps(a_p1, a_p2)
            ),
        }
        failed = sorted(name for name, passed in checks.items() if not passed)
        intervals = {
            f"{record['device_id']}.{record['tag']}": {
                "action": record["action"],
                "started_at": record["started_at"],
                "finished_at": record["finished_at"],
                **({"plate_uuid": record["plate_uuid"]} if "plate_uuid" in record else {}),
            }
            for record in (occupy_1, occupy_2, peek_1, peek_2, a_p1, b_p1, a_p2)
        }
        self._last_verdict = "pass" if not failed else "fail:" + ",".join(failed)
        self.logger.info(f"[LockAuditor] 审计结论: {self._last_verdict}")
        if failed:
            raise AssertionError(f"锁语义核对失败: {failed}; intervals={intervals}")
        return {"success": True, "checks": checks, "intervals": intervals}

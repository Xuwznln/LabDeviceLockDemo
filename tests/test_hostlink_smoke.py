from __future__ import annotations

from lock_demo.smoke import (
    assert_action_lock_group,
    assert_audit,
    assert_material_lock_group,
    run_smoke,
)


def test_real_lock_hostlink_smoke() -> None:
    proof = run_smoke("hostlink", timeout=90.0)
    # 动作锁：同设备同动作串行、先提交先执行；always_free 不排队
    assert_action_lock_group(proof["action_lock_group"])
    # 物料锁：不同设备处理同一块板串行；同设备处理不同板并行
    assert_material_lock_group(proof["material_lock_group"])
    # 审计器按账本区间核对四条结论
    assert_audit(proof["audit"])

"""参数类型决定前端选择器与物料锁：探针参数是 DeviceSlot（设备选择器），板是 ResourceSlot（物料选择器 + 物料锁）。"""

from pathlib import Path

import lock_demo

PACKAGE_DIR = Path(lock_demo.__file__).resolve().parent


def _actions(device_id):
    from unilabos.registry.registry import Registry

    registry = Registry()
    registry.setup(devices_dirs=[str(PACKAGE_DIR)], complete_registry=True, external_only=True)
    return registry.device_type_registry[device_id]["class"]["action_value_mappings"]


def test_auditor_probe_parameters_get_device_selectors():
    audit = _actions("lock_auditor_demo")["audit"]
    assert audit["placeholder_keys"] == {"probe_a": "unilabos_devices", "probe_b": "unilabos_devices"}
    goal = audit["schema"]["properties"]["goal"]["properties"]
    # 值仍是设备 ID 字符串，模板里的 "lock_probe_a" / "lock_probe_b" 照常可用
    assert goal["probe_a"]["type"] == goal["probe_b"]["type"] == "string"
    assert goal["probe_a"]["default"] == "lock_probe_a" and goal["probe_b"]["default"] == "lock_probe_b"
    assert not audit.get("materials_need_lock")


def test_plate_parameter_stays_a_locked_resource():
    process = _actions("lock_probe_demo")["process_plate"]
    assert process["placeholder_keys"] == {"plate": "unilabos_resources"}
    assert process["materials_need_lock"] == ["plate"]

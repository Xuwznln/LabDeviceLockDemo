"""模板必须显式实例化，不能把模板 UUID 当工作流 UUID 或轮询旧实例列表。"""

import time

import pytest
from types import SimpleNamespace

from lock_demo import smoke


def test_readiness_waits_for_device_capabilities(monkeypatch):
    calls = []
    remaining = [[], [{"action_capabilities": [
        {"action_name": name} for name in ["prepare_plates","occupy","process_plate","audit"]
    ]}]]

    def api(port, path):
        calls.append(path)
        if path == "/health":
            return {"status": "ok", "execution": "ready"}
        assert path == "/runtime/endpoints?state=online&limit=100"
        return remaining.pop(0)

    monkeypatch.setattr(smoke, "_api_request", api)
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    smoke._wait_management_api(8002, SimpleNamespace(poll=lambda: None), time.monotonic() + 2)
    assert calls.count("/runtime/endpoints?state=online&limit=100") == 2


def test_smoke_instantiates_reported_template_before_submission(monkeypatch):
    name = '锁演示：准备物料'
    calls = []

    def api(port, path, payload=None):
        assert port == 8002
        calls.append((path, payload))
        if path == "/registry/workflow-templates":
            return {"templates": [{"uuid": "template", "display_name": name}]}
        if path == "/workflows/from-template":
            assert payload == {"template_uuid": "template", "bindings": {}}
            return {"workflow": {"uuid": "workflow", "name": name}}
        if path == "/workflow-tasks":
            assert payload["workflow_uuid"] == "workflow"
            return {"uuid": "task", "status": "succeeded"}
        if path == "/workflow-tasks/task":
            return {"uuid": "task", "status": "succeeded", "workflow_snapshot": {"nodes": []}}
        if path in {"/workflow-tasks/task/jobs", "/workflow-tasks/task/node-runs"}:
            return []
        raise AssertionError(f"意外调用旧接口或未知路由: {path}")

    monkeypatch.setattr(smoke, "_api_request", api)
    smoke._find_workflow(8002, name, time.monotonic() + 2)
    assert calls[:2] == [
        ("/registry/workflow-templates", None),
        ("/workflows/from-template", {"template_uuid": "template", "bindings": {}}),
    ]

def test_smoke_rejects_ambiguous_template_name(monkeypatch):
    name = '锁演示：准备物料'

    def api(port, path, payload=None):
        assert path == "/registry/workflow-templates"
        return {"templates": [
            {"uuid": "one", "display_name": name},
            {"uuid": "two", "display_name": name},
        ]}

    monkeypatch.setattr(smoke, "_api_request", api)
    with pytest.raises(AssertionError, match="重复"):
        smoke._find_workflow(8002, name, time.monotonic() + 2)

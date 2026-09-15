"""调度锁演示的有限时 smoke：起真实运行时，经管理 HTTP API 并发提交工作流并核对锁语义。

网页上"连点几次运行"就是并发提交；本脚本复现同样的 HTTP 调用序列：

1. ``GET  /api/v1/workflows``                 找到 host 启动时上报的 @workflow 模板；
2. ``POST /api/v1/workflow-tasks``            先跑「准备物料」，再把一组工作流**同时**创建为任务；
3. ``GET  /api/v1/scheduler/resources``       在组内竞争进行时抓统一调度器的锁快照：
   被挡住的任务处于 ``waiting`` 且 ``blockers`` 指向持锁的 attempt；
4. ``GET  /api/v1/workflow-tasks/{uuid}``      等全部任务终态；
5. 最后提交「锁账本审计」：审计器读两台探针的账本，四条结论任一不成立就让该任务失败。

三种锁语义：动作锁（同设备同动作串行、先提交先执行）、always_free（不排队、与
occupy 并行）、物料锁（不同设备处理同一块板串行；同设备处理不同板并行）。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

#: 与 lock_demo/workflows.py 保持一致（smoke 独立运行，不 import 设备包）。
PREPARE_WORKFLOW_NAME = "锁演示：准备物料"
ACTION_LOCK_GROUP = (
    "动作锁：占用 A（第一次）",
    "动作锁：占用 A（第二次）",
    "always_free：探测 A（第一次）",
    "always_free：探测 A（第二次）",
)
MATERIAL_LOCK_GROUP = ("物料锁：A 处理 P1", "物料锁：B 处理 P1", "物料锁：A 处理 P2")
AUDIT_WORKFLOW_NAME = "锁账本审计"
#: 组内预期在调度器排队的任务（第二个 occupy、B 处理 P1）。
EXPECT_WAITING = {"动作锁：占用 A（第二次）", "物料锁：B 处理 P1"}

TERMINAL = {"succeeded", "failed"}


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def assert_action_lock_group(proof: dict[str, Any]) -> None:
    """动作锁组：四条任务都成功；第二次 occupy 曾在调度器排队（动作锁）；两次 peek 都不曾排队。"""

    tasks = proof["tasks"]
    assert [item["workflow_name"] for item in tasks] == list(ACTION_LOCK_GROUP)
    assert all(item["task_status"] == "succeeded" for item in tasks), tasks
    queued = proof["queued"]
    assert set(queued) == {"动作锁：占用 A（第二次）"}, queued
    request = queued["动作锁：占用 A（第二次）"]
    assert request["status"] == "waiting" and request["blockers"], request
    kinds = {identifier["kind"] for identifier in request["identifiers"]}
    assert kinds == {"action"}, request
    # 两次 peek 同设备同动作：always_free → 第二次开始时第一次仍在执行（设备侧并行数 2）
    peeks = [item for item in tasks if item["workflow_name"].startswith("always_free")]
    values = [item["node_runs"][0]["return_info"]["return_value"] for item in peeks]
    assert all(value["action"] == "peek" for value in values), values
    assert max(value["concurrency_at_start"] for value in values) >= 2, values


def assert_material_lock_group(proof: dict[str, Any]) -> None:
    """物料锁组：三条任务都成功；B 处理 P1 曾因物料锁排队。"""

    tasks = proof["tasks"]
    assert [item["workflow_name"] for item in tasks] == list(MATERIAL_LOCK_GROUP)
    assert all(item["task_status"] == "succeeded" for item in tasks), tasks
    queued = proof["queued"]
    assert set(queued) == {"物料锁：B 处理 P1"}, queued
    request = queued["物料锁：B 处理 P1"]
    assert request["status"] == "waiting" and request["blockers"], request
    kinds = {identifier["kind"] for identifier in request["identifiers"]}
    assert kinds == {"material"}, request


def assert_audit(proof: dict[str, Any]) -> None:
    """审计工作流成功，且四条结论全部为真。"""

    assert proof["task_status"] == "succeeded", f"审计任务未成功: {proof}"
    (node_run,) = proof["node_runs"]
    value = node_run["return_info"]["return_value"]
    assert value["success"] is True, value
    assert value["checks"] == {
        "action_lock_serialized": True,
        "always_free_same_action_parallel": True,
        "material_lock_serialized_across_devices": True,
        "material_lock_is_per_plate": True,
    }, value["checks"]


# ---------------------------------------------------------------------------
# 进程与 HTTP
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _graph_path(repo_root: Path) -> Path:
    """优先读取 wheel 安装的数据文件，editable/source 模式回退到仓库 graph。"""

    installed = Path(sysconfig.get_path("data")) / "share" / "lock_demo" / "graph" / "lock_demo.json"
    if installed.is_file():
        return installed
    source = repo_root / "graph" / "lock_demo.json"
    if source.is_file():
        return source
    raise FileNotFoundError("Lock demo graph 未随 distribution 安装")


def _base_command(repo_root: Path, database_root: Path, management_port: int, backend: str) -> list[str]:
    import unilabos

    config_path = Path(unilabos.__file__).resolve().parent / "config" / "example_config.py"
    command = [
        sys.executable,
        "-m",
        "unilabos",
        "--backend",
        backend,
        "--skip_env_check",
        "--devices",
        str(repo_root / "lock_demo"),
        "--external_devices_only",
        "--visual",
        "disable",
        "--disable_browser",
        "--port",
        str(management_port),
        "--server_database_root",
        str(database_root),
        "--working_dir",
        str(database_root / "work"),
        "--config",
        str(config_path),
        "-g",
        str(_graph_path(repo_root)),
    ]
    return command


def _api_request(port: int, path: str, payload: dict[str, Any] | None = None) -> Any:
    """请求管理 API；workflow 风格 {"code":0,"data":...} 自动解包，诊断路由原样返回。"""

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if payload is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method="GET" if payload is None else "POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if isinstance(body, dict) and "code" in body:
        if body["code"] != 0:
            raise RuntimeError(f"管理 API {path} 返回错误: {body}")
        return body.get("data")
    return body


def _wait_management_api(port: int, process: subprocess.Popen[Any], deadline: float) -> None:
    """HTTP 存活不代表 Host 已上报能力；等执行端点包含本演示的动作再导入模板。"""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("runtime process exited before the management API came up")
        try:
            health = _api_request(port, "/health")
            endpoints = _api_request(port, "/runtime/endpoints?state=online&limit=100")
            actions = {
                capability["action_name"]
                for endpoint in endpoints
                for capability in endpoint.get("action_capabilities", [])
                if capability.get("state", "active") == "active"
            }
            if (
                health.get("status") == "ok"
                and health.get("execution") == "ready"
                and {"prepare_plates","occupy","process_plate","audit"} <= actions
            ):
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError("管理 API / Host 执行面 / 设备动作能力未在时限内就绪")


def _find_workflow(port: int, name: str, deadline: float) -> dict[str, Any]:
    """模板上报不创建工作流实例；按当前 API 显式绑定并实例化。"""
    while time.monotonic() < deadline:
        listing = _api_request(port, "/registry/workflow-templates")
        matches = [item for item in listing["templates"] if item["display_name"] == name]
        assert len(matches) <= 1, f"工作流模板显示名重复: {name!r}"
        if matches:
            instantiated = _api_request(
                port, "/workflows/from-template", {"template_uuid": matches[0]["uuid"], "bindings": {}}
            )
            return instantiated["workflow"]
        time.sleep(0.3)
    raise RuntimeError(f"未在注册表检索到工作流模板 {name!r}")


def _submit(port: int, name: str, deadline: float) -> dict[str, Any]:
    workflow = _find_workflow(port, name, deadline)
    task = _api_request(port, "/workflow-tasks", {"workflow_uuid": workflow["uuid"], "run_mode": "normal"})
    return {"workflow_uuid": workflow["uuid"], "workflow_name": name, "task_uuid": task["uuid"]}


def _queued_request(port: int, task_uuid: str, deadline: float) -> dict[str, Any]:
    """等到该任务的资源申请在统一调度器里排队（waiting + blockers）。"""

    while time.monotonic() < deadline:
        snapshot = _api_request(port, "/scheduler/resources")
        for request in snapshot.get("requests", []):
            if request.get("task_uuid") == task_uuid and request.get("status") == "waiting" and request.get("blockers"):
                return request
        time.sleep(0.05)
    raise RuntimeError(f"任务 {task_uuid} 未在调度器观察到排队（waiting + blockers）")


def _await(port: int, submitted: dict[str, Any], deadline: float) -> dict[str, Any]:
    task_uuid = submitted["task_uuid"]
    status = ""
    while time.monotonic() < deadline and status not in TERMINAL:
        status = str(_api_request(port, f"/workflow-tasks/{task_uuid}").get("status") or "")
        if status in TERMINAL:
            break
        # 本演示的动作都不该失败：一旦某个 attempt 被挂进错误决策链，立刻带报文失败，不空等超时
        held = [
            item for item in _api_request(port, "/error-decisions")["items"] if item.get("task_id") == task_uuid
        ]
        if held:
            raise AssertionError(f"任务 {submitted['workflow_name']!r} 的 attempt 进入了错误决策链: {held[0]}")
        time.sleep(0.2)
    if status not in TERMINAL:
        raise RuntimeError(f"工作流任务 {task_uuid} 未在时限内结束: {status}")
    node_runs = _api_request(port, f"/workflow-tasks/{task_uuid}/node-runs")
    return {
        **submitted,
        "task_status": status,
        "node_runs": [
            {
                "uuid": run["uuid"],
                "status": run["status"],
                "attempt_count": int(run.get("attempt_count") or 0),
                "return_info": dict(run.get("return_info") or {}),
                "error_info": list(run.get("error_info") or []),
            }
            for run in node_runs
        ],
    }


def run_group(port: int, names: Sequence[str], deadline: float) -> dict[str, Any]:
    """同时创建一组任务（网页连点"运行"），抓排队快照，再等全部终态。"""

    submitted = [_submit(port, name, deadline) for name in names]
    queued = {
        item["workflow_name"]: _queued_request(port, item["task_uuid"], deadline)
        for item in submitted
        if item["workflow_name"] in EXPECT_WAITING
    }
    return {"tasks": [_await(port, item, deadline) for item in submitted], "queued": queued}


def run_single(port: int, name: str, deadline: float) -> dict[str, Any]:
    return _await(port, _submit(port, name, deadline), deadline)


def run_smoke(backend: str = "hostlink", timeout: float = 60.0) -> dict[str, Any]:
    """启动真实图，经管理 API 并发提交三组工作流并审计，返回可机读证据。"""

    if backend not in {"hostlink", "ros2"}:
        raise ValueError("backend must be hostlink or ros2")
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=f"lock-demo-{backend}-") as directory:
        root = Path(directory)
        log_path = root / "runtime.log"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        management_port = _free_port()
        command = _base_command(repo_root, root / "db", management_port, backend)
        # ROS2 也保留 HostLink 的能力登记与物料管理通道。
        command += ["--hostlink_bind", "127.0.0.1", "--hostlink_port", str(_free_port())]
        if backend == "ros2":
            domain_id = str(10 + management_port % 190)
            environment["ROS_DOMAIN_ID"] = domain_id
            command += ["--ros_domain_id", domain_id]

        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command, cwd=repo_root, env=environment, stdout=output, stderr=subprocess.STDOUT, text=True
            )
            try:
                deadline = time.monotonic() + timeout
                _wait_management_api(management_port, process, deadline)
                prepare = run_single(management_port, PREPARE_WORKFLOW_NAME, deadline)
                assert prepare["task_status"] == "succeeded", prepare
                action_lock = run_group(management_port, ACTION_LOCK_GROUP, deadline)
                assert_action_lock_group(action_lock)
                material_lock = run_group(management_port, MATERIAL_LOCK_GROUP, deadline)
                assert_material_lock_group(material_lock)
                audit = run_single(management_port, AUDIT_WORKFLOW_NAME, deadline)
                assert_audit(audit)
                return {
                    "success": True,
                    "backend": backend,
                    "prepare": prepare,
                    "action_lock_group": action_lock,
                    "material_lock_group": material_lock,
                    "audit": audit,
                }
            except Exception:
                sys.stderr.write("SMOKE FAILED\n" + log_path.read_text(encoding="utf-8", errors="replace") + "\n")
                raise
            finally:
                _stop(process)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("hostlink", "ros2"), default="hostlink")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    print(json.dumps(run_smoke(args.backend, args.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

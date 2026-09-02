# LabDeviceLockDemo

[中文说明](README_zh.md)

A minimal Uni-Lab-OS device package that makes the **scheduler's lock semantics
observable**. Three virtual devices share one process: two `lock_probe_demo`
instances (`lock_probe_a`, `lock_probe_b`) that record every action as a timed
interval in an in-memory ledger, and a `lock_auditor_demo` (`lock_auditor`) that
reads both ledgers and turns the intervals into pass/fail conclusions.

What it demonstrates (all through the web-style workflow submission path,
`POST /api/v1/workflow-tasks` + `GET /api/v1/scheduler/resources`):

- **Action lock** (`lock_probe_a.occupy`, a plain `@action`): the scheduler
  implicitly claims the `(device_id, action_name)` lock, so two `occupy`
  tasks submitted together run strictly one after the other, in submission
  order; the second one is visible as a `waiting` request whose `blockers`
  point at the first attempt;
- **`always_free`** (`lock_probe_a.peek`, `@action(always_free=True)`): no
  action lock is claimed, so two `peek` tasks on the *same device and same
  action* overlap. The lock granularity is `(device, action)`, therefore
  different actions never conflict — the evidence for `always_free` has to be
  two calls of the *same* action running side by side;
- **Material lock** (`process_plate`, `@action(always_free=True,
  materials_need_lock=["plate"])`): the only lock is the authority-issued
  `material_uuid` of the `plate` argument. Device A and device B processing the
  same plate P1 run one after the other (B is queued behind a `material`
  identifier), while device A processing P1 and P2 at the same time overlaps —
  the lock is per plate, not per device;
- **Audit as a workflow node**: `lock_auditor.audit` reads the ledgers via
  point-to-point `call_device_action` and raises `AssertionError` if any of the
  four conclusions fails, so a green task really means the semantics held.

Not covered on purpose: workflow-level locks and lock handoff between
consecutive jobs are not wired into `@workflow` yet, and there is no priority
preemption — contention is resolved by the unified scheduler's FIFO queue.

## Install from GitHub

```bash
unilab package install https://github.com/Xuwznln/LabDeviceLockDemo --ref <commit-sha>
```

For local development:

```bash
git clone https://github.com/Xuwznln/LabDeviceLockDemo.git
cd LabDeviceLockDemo
python -m pip install -e .
```

No AK/SK and no cloud lab required.

## Terminating dual-runtime smoke

```bash
python -m lock_demo.smoke --backend hostlink --timeout 90
python -m lock_demo.smoke --backend ros2 --timeout 120
```

The smoke boots the real runtime (`unilab -g graph/lock_demo.json`, which also
reports the `@workflow` templates to the local Workflow Authority) and replays
what the web UI does when you click "run" several times in a row:

1. **"锁演示：准备物料"**: `lock_probe_a.prepare_plates` idempotently `ensure`s two
   plates P1 / P2 with fixed authority uuids (the lock objects).
2. **Action-lock group** — four tasks created back to back:
   `occupy-1`, `occupy-2` (`lock_probe_a/occupy`, 1.5 s each) and `peek-1`,
   `peek-2` (`lock_probe_a/peek`, 1.0 s each). While `occupy-1` holds the lock,
   `GET /api/v1/scheduler/resources` shows `occupy-2` as `waiting` with an
   `action` identifier and non-empty `blockers`; the two `peek`s start
   immediately (`concurrency_at_start = 2`).
3. **Material-lock group** — three tasks created back to back:
   `a-p1` (`lock_probe_a/process_plate` P1), `b-p1` (`lock_probe_b/process_plate`
   P1) and `a-p2` (`lock_probe_a/process_plate` P2). `b-p1` is `waiting` behind a
   `material` identifier (P1) until `a-p1` finishes; `a-p2` runs alongside
   `a-p1`.
4. **"锁账本审计"**: `lock_auditor.audit` checks
   `action_lock_serialized`, `always_free_same_action_parallel`,
   `material_lock_serialized_across_devices`, `material_lock_is_per_plate` and
   fails the task if any is false.

Node results are read from `GET /api/v1/workflow-tasks/{uuid}/node-runs`.

## Manual start

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

Then open the management UI, run "锁演示：准备物料" once, click "run" on the two
occupy workflows and the two peek workflows in quick succession, and watch
`/api/v1/scheduler/resources` while they execute.

## Layout

```text
graph/lock_demo.json               one graph shared by both backends (A, B, auditor)
lock_demo/
  labware.py                       lock_demo_plate resource + fixed P1 / P2 uuids
  lock_probe.py                    occupy / peek / process_plate / prepare_plates / ledger
  lock_auditor.py                  audit: ledgers -> four lock-semantics conclusions
  workflows.py                     @workflow templates (prepare, two groups, audit)
  smoke.py                         terminating real-runtime proof driven through the management API
tests/test_hostlink_smoke.py       HostLink integration assertions
```

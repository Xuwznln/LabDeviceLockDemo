"""演示包自带的物料：一块 2 孔小板，用来当"物料锁"的锁对象。

物料锁的身份是 Materials Authority 分配的 ``material_uuid``；本演示用固定 uuid
``ensure`` 两块板（P1 / P2），工作流参数里只传 ``{"uuid": ...}``。
"""

from uuid import UUID, uuid5

from pylabrobot.resources import Plate, Well
from pylabrobot.resources.utils import create_ordered_items_2d

from unilabos.registry.decorators import resource

#: 固定 uuid：两块演示板在权威里的身份（与 workflows.py / smoke.py 共用）。
PLATE_P1_UUID = "5a0c4e1d-2b7f-4c93-9e61-000000003101"
PLATE_P2_UUID = "5a0c4e1d-2b7f-4c93-9e61-000000003102"
PLATE_UUIDS = {"P1": PLATE_P1_UUID, "P2": PLATE_P2_UUID}


@resource(
    id="lock_demo_plate",
    category=["plate"],
    description="2 孔演示板：物料锁演示的锁对象，只有身份没有工艺意义",
    display_name="锁演示板",
)
def lock_demo_plate(name: str) -> Plate:
    return Plate(
        name=name,
        size_x=127.76,
        size_y=85.48,
        size_z=14.0,
        lid=None,
        model="lock_demo_plate",
        ordered_items=create_ordered_items_2d(
            Well,
            num_items_x=2,
            num_items_y=1,
            dx=20.0,
            dy=30.0,
            dz=2.0,
            item_dx=40.0,
            item_dy=0.0,
            size_x=20.0,
            size_y=20.0,
            size_z=10.0,
            max_volume=500.0,
        ),
    )


def build_lock_plate(name: str, plate_uuid: str) -> Plate:
    """带固定权威 uuid 的板草稿（供 ``materials.ensure`` 幂等创建）。

    ``ensure`` 是"按给定 uuid 收养整棵树"，所以孔位也要有稳定 uuid：由板 uuid 派生。
    """

    plate = lock_demo_plate(name)
    plate.unilabos_uuid = plate_uuid
    for well in plate.children:
        well.unilabos_uuid = str(uuid5(UUID(plate_uuid), well.name))
    return plate

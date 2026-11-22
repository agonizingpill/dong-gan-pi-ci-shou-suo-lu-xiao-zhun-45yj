from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from src.auth import password_hash
from src.db import engine
from src.ledger import checksum, queue_fit
from src.models import (
    BatchState, ChamberRun, DryBatch, RecipeLot, Role, RunState, ShrinkageSample, Stage,
    TrayPosition, User,
)

STAMP = datetime(2026, 11, 21, 0, 0)

# 五个托盘位置，TRAY-D 为门侧托盘；TRAY-B 在目标批缺同位置参考。
TRAY_LAYOUT = [('TRAY-A', False), ('TRAY-B', False), ('TRAY-C', False), ('TRAY-D', True), ('TRAY-E', False)]


def points(minutes, pressure, temp, *, vacuum=True):
    series = [(0, 'freeze', -39.0, 101000.0), (45, 'freeze', -37.0, 98000.0)]
    if vacuum:
        series += [(60, 'vacuum', temp - 4, pressure + 7), (60 + minutes // 3, 'vacuum', temp - 1, pressure + 2),
                   (60 + 2 * minutes // 3, 'vacuum', temp + 1, pressure - 3),
                   (60 + minutes, 'vacuum', temp + 4, pressure - 6)]
    series += [(90 + minutes, 'finish', 9.0, 14.0), (130 + minutes, 'finish', 22.0, 10.0)]
    return [dict(minute=m, phase=phase, shelf_c=t, pressure_pa=p) for m, phase, t, p in series]


def add_pair(db, run_id, specimen, position, feed, shrinkage):
    for stage, reading in [(Stage.PRE, feed), (Stage.POST, round(feed * (1 - shrinkage / 100), 5))]:
        db.add(ShrinkageSample(run_id=run_id, specimen=specimen, stage=stage, thickness_mm=reading,
                               position=position, technician='operator', recorded_at=STAMP))


def seed(db):
    for role in Role:
        if db.get(User, role.value) is None:
            db.add(User(username=role.value, password_hash=password_hash('Lab-demo-1062'), role=role))
    for values in [
        ('AP-26R3', '青苹果薄片 · AP03', 14.2, 8.0, 9.0, 16.0),
        ('PE-26R1', '香梨薄片 · PE01', 12.8, 7.0, 7.0, 14.0),
        ('ST-26R2', '草莓切片 · ST02', 10.1, 10.0, 12.0, 20.0),
    ]:
        code, product, solids, nominal, low, high = values
        if db.get(RecipeLot, code) is None:
            db.add(RecipeLot(code=code, product=product, solids_pct=solids, nominal_mm=nominal,
                             window_low=low, window_high=high))
    db.flush()
    fixtures = [
        ('FD-1101', 7.1, 170, 23, -27, 10, BatchState.REFERENCE),
        ('FD-1104', 8.0, 240, 31, -20, 14, BatchState.REFERENCE),
        ('FD-1108', 9.1, 340, 45, -12, 20, BatchState.REFERENCE),
        ('FD-1121A', 8.0, 243, 32, -20, 13, BatchState.COLLECTING),
        ('FD-1121B', 8.2, 252, 32, -19, None, BatchState.COLLECTING),
        ('FD-1121C', 9.3, 365, 49, -9, 19, BatchState.COLLECTING),
    ]
    for code, feed, minutes, pressure, temp, shrinkage, state in fixtures:
        if db.get(DryBatch, code) is not None:
            continue
        batch = DryBatch(code=code, recipe_code='AP-26R3', dryer='FDX-02', material_lot=f'APL-{code[3:]}',
                         feed_mm=feed, state=state)
        db.add(batch)
        db.flush()
        for position, door_side in TRAY_LAYOUT:
            db.add(TrayPosition(batch_code=code, position=position, door_side=door_side))
        if code in ('FD-1121B', 'FD-1121C'):
            db.add(ChamberRun(batch_code=code, source_name=f'{code}_chamber_r0.csv', state=RunState.DRAFT,
                              points=points(minutes, pressure, temp, vacuum=False)))
            db.flush()
        if shrinkage is None:
            continue
        readings = points(minutes, pressure, temp)
        run = ChamberRun(batch_code=code, source_name=f'{code}_chamber_r1.csv', state=RunState.FROZEN,
                         points=readings, checksum=checksum(readings), frozen_at=STAMP)
        db.add(run)
        db.flush()
        if state == BatchState.REFERENCE:
            # 同位置参考：普通托盘稳定落在工艺窗内；门侧 TRAY-D 贴近上沿 16%；TRAY-B 无同位置参考。
            reference_layout = {
                'FD-1101': {'TRAY-A': 10.8, 'TRAY-C': 11.2, 'TRAY-D': 15.1, 'TRAY-E': 10.6},
                'FD-1104': {'TRAY-A': 12.4, 'TRAY-C': 12.8, 'TRAY-D': 15.4, 'TRAY-E': 12.0},
                'FD-1108': {'TRAY-A': 13.6, 'TRAY-C': 14.2, 'TRAY-D': 15.7, 'TRAY-E': 13.2},
            }
            for specimen, value in reference_layout[code].items():
                add_pair(db, run.id, specimen, specimen, feed, value)
        elif code == 'FD-1121A':
            # 整批预测合格，但门侧托盘 TRAY-D 首测超窗（17.2% > 16%）。
            target_specimens = [
                ('TRAY-A', 'TRAY-A', shrinkage - 0.4),
                ('TRAY-C', 'TRAY-C', shrinkage + 0.4),
                ('TRAY-D', 'TRAY-D', 17.2),
            ]
            for specimen, position, value in target_specimens:
                add_pair(db, run.id, specimen, position, feed, value)
        else:
            for specimen, delta in [('TRAY-A', -0.4), ('TRAY-C', 0.4)]:
                add_pair(db, run.id, specimen, specimen, feed, shrinkage + delta)
        db.flush()
        if code in ('FD-1121A', 'FD-1121C'):
            queue_fit(db, batch, 'operator')
    return len(db.scalars(select(DryBatch.code)).all())


def main():
    with engine.connect() as connection:
        if connection.scalar(text("SELECT GET_LOCK('freeze-demo-seed', 15)")) != 1:
            raise RuntimeError('unable to lock demo seed')
        connection.commit()
        try:
            with OrmSession(bind=connection) as db, db.begin():
                count = seed(db)
            print(f'demo seed committed, batches={count}')
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('freeze-demo-seed')"))


if __name__ == '__main__':
    main()

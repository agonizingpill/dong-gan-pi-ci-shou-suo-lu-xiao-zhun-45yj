import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import inspect, select

from .db import QUEUE, redis, utcnow
from .models import (
    BatchState, CalibrationRelease, ChamberRun, DryBatch, LabEvent, Phase, RecipeLot,
    ReleaseState, RunState, ShrinkageSample, Shift, Stage, TrayPosition,
)

log = logging.getLogger(__name__)

# 收缩率估计范围距离工艺窗边沿小于该余量（百分点）即视为“碰边”。
EDGE_MARGIN_PCT = 1.0


def checksum(data):
    normalized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(normalized.encode()).hexdigest()


def record(db, actor, action, detail, code=None):
    db.add(LabEvent(batch_code=code, actor=actor, action=action, detail=detail))


def blocked(db, actor, action, detail, code):
    record(db, actor, f'{action}.blocked', detail, code)
    db.commit()
    raise HTTPException(409, detail)


def row(model):
    result = {}
    for col in inspect(type(model)).column_attrs:
        value = getattr(model, col.key)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc).isoformat()
        result[col.key] = value
    return result


def locked_batch(db, code):
    batch = db.scalar(select(DryBatch).where(DryBatch.code == code).with_for_update())
    if batch is None:
        raise HTTPException(404, '没有找到该冻干批次')
    return batch


def latest_run(db, code):
    return db.scalar(select(ChamberRun).filter_by(batch_code=code).order_by(ChamberRun.id.desc()).limit(1))


def release_for(db, run_id):
    return db.scalar(select(CalibrationRelease).filter_by(run_id=run_id))


def samples_for(db, run_id, *, retest=False):
    return db.scalars(select(ShrinkageSample).filter_by(run_id=run_id, is_retest=retest)
                      .order_by(ShrinkageSample.specimen, ShrinkageSample.id)).all()


def vacuum_points(points):
    return [point for point in points if point['phase'] == Phase.VACUUM]


def curve_issue(points):
    vacuum = vacuum_points(points)
    if len(vacuum) < 2:
        return '曲线缺少完整真空段，至少补录两个真空采集点'
    phases = [point['phase'] for point in points]
    order = {Phase.FREEZE: 0, Phase.VACUUM: 1, Phase.FINISH: 2}
    if phases[0] != Phase.FREEZE or phases[-1] != Phase.FINISH or phases != sorted(phases, key=order.get):
        return '曲线阶段应按预冻、抽真空、解析干燥顺序采集'
    return None


def training_run(db, batch):
    run = latest_run(db, batch.code)
    if not run:
        raise ValueError('批次缺少设备曲线，请先导入设备记录')
    issue = curve_issue(run.points)
    if issue:
        raise ValueError(issue)
    if run.state != RunState.FROZEN:
        raise ValueError('设备曲线尚未冻结，试算与放行必须引用冻结版本')
    return run


def paired_measurements(db, run):
    pairs = {}
    for sample in samples_for(db, run.id):
        pairs.setdefault(sample.specimen, {})[sample.stage] = round(sample.thickness_mm, 5)
    if len(pairs) < 2 or any(len(pair) != 2 for pair in pairs.values()):
        raise ValueError('样本阶段不足，至少两个试片且每个均须配齐冻干前与冻干后厚度')
    if any(pair[Stage.POST] > pair[Stage.PRE] for pair in pairs.values()):
        raise ValueError('样本阶段读数异常，请核查冻干后厚度大于前厚度的试片')
    return pairs


def add_curve(db, batch, payload, actor):
    previous = latest_run(db, batch.code)
    if previous:
        release = release_for(db, previous.id)
        if release and release.state != ReleaseState.HELD:
            blocked(db, actor, 'curve.import', '当前校准尚未结案，复测留置后才能导入新周期', batch.code)
    if batch.state in (BatchState.RELEASED, BatchState.REFERENCE):
        blocked(db, actor, 'curve.import', '已放行或归档的批次保留原曲线', batch.code)
    if db.scalar(select(ChamberRun.id).filter_by(batch_code=batch.code, source_name=payload['source_name'])):
        blocked(db, actor, 'curve.import', '同名曲线已存在，请用新文件名保留修订历史', batch.code)
    run = ChamberRun(batch_code=batch.code, **payload)
    db.add(run)
    batch.state = BatchState.COLLECTING
    record(db, actor, 'curve.import', run.source_name, batch.code)
    db.flush()
    return run


def freeze_curve(db, batch, run, actor):
    if run.state != RunState.DRAFT or latest_run(db, batch.code).id != run.id:
        blocked(db, actor, 'curve.freeze', '仅当前待采曲线可以冻结', batch.code)
    issue = curve_issue(run.points)
    if issue:
        blocked(db, actor, 'curve.freeze', issue, batch.code)
    run.state = RunState.FROZEN
    run.frozen_at = utcnow()
    run.checksum = checksum(run.points)
    record(db, actor, 'curve.freeze', f'{run.source_name} · {run.checksum}', batch.code)


def add_sample(db, batch, run, payload, actor):
    if batch.state != BatchState.COLLECTING or latest_run(db, batch.code).id != run.id or run.state != RunState.FROZEN:
        blocked(db, actor, 'sample.record', '仅采集中当前冻结曲线接受常规阶段测量', batch.code)
    matches = {item.stage: item.thickness_mm for item in samples_for(db, run.id) if item.specimen == payload['specimen']}
    if payload['stage'] in matches:
        blocked(db, actor, 'sample.record', '同一曲线、试片和阶段已有测量', batch.code)
    matches[payload['stage']] = payload['thickness_mm']
    if len(matches) == 2 and matches[Stage.POST] > matches[Stage.PRE]:
        blocked(db, actor, 'sample.record', '冻干后厚度不能大于冻干前读数，请核对试片', batch.code)
    if payload.get('position') and not db.scalar(
            select(TrayPosition.id).filter_by(batch_code=batch.code, position=payload['position'])):
        blocked(db, actor, 'sample.record', f"本批未登记托盘位置 {payload['position']}，请先建档位置", batch.code)
    sample = ShrinkageSample(run_id=run.id, technician=actor, **payload)
    db.add(sample)
    record(db, actor, 'sample.record',
           f"{sample.specimen} · {sample.stage} · {sample.thickness_mm:g} mm"
           f" · {sample.position or '未标位置'}", batch.code)
    db.flush()
    return sample


def archive_reference(db, batch, actor, note):
    if batch.state != BatchState.COLLECTING:
        blocked(db, actor, 'reference.archive', '只有采集状态可以认证为历史标定批次', batch.code)
    try:
        pairs = paired_measurements(db, training_run(db, batch))
    except ValueError as exc:
        blocked(db, actor, 'reference.archive', str(exc), batch.code)
    batch.state = BatchState.REFERENCE
    record(db, actor, 'reference.archive', f'{len(pairs)} 组配对 · {note}', batch.code)


def feature_row(batch, recipe, run):
    vacuum = vacuum_points(run.points)
    return [
        round(recipe.solids_pct, 5), round(batch.feed_mm / recipe.nominal_mm, 6),
        vacuum[-1]['minute'] - vacuum[0]['minute'],
        round(sum(point['pressure_pa'] for point in vacuum) / len(vacuum), 6),
        round(sum(point['shelf_c'] for point in vacuum) / len(vacuum), 6),
    ]


def observed(pairs):
    return round(sum(100 * (1 - pair[Stage.POST] / pair[Stage.PRE]) for pair in pairs.values()) / len(pairs), 6)


def shrinkage_of(before_mm, after_mm):
    return round(100 * (1 - after_mm / before_mm), 6)


def positions_for(db, batch_code):
    return db.scalars(select(TrayPosition).filter_by(batch_code=batch_code)
                      .order_by(TrayPosition.door_side.desc(), TrayPosition.position)).all()


def register_position(db, batch, payload, actor):
    if batch.state != BatchState.COLLECTING:
        blocked(db, actor, 'position.register', '试算提交后托盘位置已冻结，不能再增改', batch.code)
    if db.scalar(select(TrayPosition.id).filter_by(batch_code=batch.code, position=payload['position'])):
        blocked(db, actor, 'position.register', '该托盘位置已登记，位置编号不能重复', batch.code)
    position = TrayPosition(batch_code=batch.code, **payload)
    db.add(position)
    record(db, actor, 'position.register',
           f"{position.position} · {'门侧托盘' if position.door_side else '普通托盘'}", batch.code)
    db.flush()
    return position


def position_shrinkage_pairs(db, run_id, position):
    """Shrinkage values of fully paired first-measurement specimens at one position."""
    pairs = {}
    for sample in samples_for(db, run_id):
        if sample.position != position:
            continue
        pairs.setdefault(sample.specimen, {})[sample.stage] = round(sample.thickness_mm, 5)
    values = [observed({specimen: pair}) for specimen, pair in pairs.items() if len(pair) == 2]
    return sorted(values)


def build_position_plan(db, batch, recipe, run):
    """Build the supplementary-test plan from same-recipe/same-position references.

    Each position is estimated with the shrinkage range of certified reference
    batches at the same physical location. Positions touching the process window
    edge or lacking a reference need an independent retest; the rest are pooled.
    """
    window = [round(recipe.window_low, 5), round(recipe.window_high, 5)]
    references = db.scalars(select(DryBatch).filter_by(recipe_code=recipe.code, state=BatchState.REFERENCE)
                            .order_by(DryBatch.code)).all()
    ref_runs = [(historical, latest_run(db, historical.code)) for historical in references]

    plan_positions = []
    for tray in positions_for(db, batch.code):
        ref_values = []
        ref_batches = []
        for historical, ref_run in ref_runs:
            values = position_shrinkage_pairs(db, ref_run.id, tray.position)
            if values:
                ref_values.extend(values)
                ref_batches.append(historical.code)
        if not ref_values:
            basis = 'no_reference'
            estimate_range = None
        else:
            estimate_range = [round(min(ref_values), 6), round(max(ref_values), 6)]
            low, high = estimate_range
            edge = (low <= window[0] + EDGE_MARGIN_PCT) or (high >= window[1] - EDGE_MARGIN_PCT)
            basis = 'window_edge' if edge else 'reference_range'
        plan_positions.append({
            'position': tray.position, 'door_side': tray.door_side, 'basis': basis,
            'reference_batches': ref_batches, 'estimate_range': estimate_range,
            'requirement': 'independent' if basis in ('no_reference', 'window_edge') else 'pooled',
        })

    independent = [item['position'] for item in plan_positions if item['requirement'] == 'independent']
    pooled = [item['position'] for item in plan_positions if item['requirement'] == 'pooled']
    pool = pooled[0] if pooled else None
    for item in plan_positions:
        if item['requirement'] == 'pooled':
            item['pool_group'] = pool
    return {'window': window, 'edge_margin_pct': EDGE_MARGIN_PCT,
            'positions': plan_positions, 'independent_positions': independent,
            'pooled_positions': pooled, 'pool_group': pool}


def retest_rows(db, run_id):
    """Retest specimens de-duplicated by specimen; latest reading wins."""
    rows = {}
    for sample in db.scalars(select(ShrinkageSample).filter_by(run_id=run_id, is_retest=True)
                             .order_by(ShrinkageSample.id)).all():
        rows.setdefault(sample.specimen, {'specimen': sample.specimen, 'position': sample.position,
                                          'shift': sample.shift.value if sample.shift else None,
                                          'technician': sample.technician, 'stages': {}})
        rows[sample.specimen]['stages'][sample.stage] = round(sample.thickness_mm, 5)
        rows[sample.specimen]['recorded_at'] = sample.recorded_at
    return list(rows.values())


def plan_coverage(db, release, run):
    """Match recorded retests against the frozen plan; wrong-position tests never count."""
    window = release.snapshot['window']
    plan = release.position_plan or {'positions': [], 'independent_positions': [],
                                     'pooled_positions': [], 'pool_group': None}
    valid_positions = {item['position'] for item in plan['positions']}
    pool = plan.get('pool_group')
    covered = {}
    mispositioned = []
    for row in retest_rows(db, run.id):
        pair = row['stages']
        if Stage.PRE not in pair or Stage.POST not in pair:
            continue
        pct = shrinkage_of(pair[Stage.PRE], pair[Stage.POST])
        verdict = {'specimen': row['specimen'], 'position': row['position'], 'shift': row['shift'],
                   'technician': row['technician'], 'shrinkage_pct': pct,
                   'inside': inside(pct, window), 'recorded_at': row['recorded_at']}
        if row['position'] not in valid_positions:
            mispositioned.append(verdict)
            continue
        covered.setdefault(row['position'], []).append(verdict)

    lines = []
    missing = []
    pool_satisfied = {}
    if pool:
        pool_satisfied[pool] = any(entry['inside'] for entry in covered.get(pool, []))
    for item in plan['positions']:
        position = item['position']
        entries = covered.get(position, [])
        in_window = next((entry for entry in entries if entry['inside']), None)
        any_entry = entries[0] if entries else None
        if item['requirement'] == 'independent':
            satisfied = in_window is not None
            if not satisfied:
                missing.append(position)
        else:
            # 合抽位置共享同一处抽测：在牵头位置测一枚窗内试片即覆盖整组。
            satisfied = pool_satisfied.get(pool, False)
            if position == pool and not satisfied:
                missing.append(f'POOL:{pool}')
        lines.append({
            'position': position, 'door_side': item['door_side'], 'basis': item['basis'],
            'requirement': item['requirement'], 'pool_group': item.get('pool_group'),
            'estimate_range': item['estimate_range'], 'reference_batches': item['reference_batches'],
            'retests': entries, 'satisfied': satisfied,
            'latest_pct': any_entry['shrinkage_pct'] if any_entry else None,
            'in_window': in_window is not None,
        })
    required_count = len(plan['independent_positions']) + (1 if pool else 0)
    distinct_valid = {entry['specimen'] for entries in covered.values() for entry in entries}
    complete = not missing
    all_inside = all(entry['inside'] for entries in covered.values() for entry in entries)
    return {'lines': lines, 'missing': missing, 'mispositioned': mispositioned,
            'required_count': required_count, 'valid_specimen_count': len(distinct_valid),
            'complete': complete, 'all_inside': all_inside}


def queue_fit(db, batch, actor):
    if batch.state != BatchState.COLLECTING:
        blocked(db, actor, 'fit.queue', '该批次当前状态不可提交新试算', batch.code)
    recipe = db.get(RecipeLot, batch.recipe_code)
    try:
        run = training_run(db, batch)
        target_pairs = paired_measurements(db, run)
    except ValueError as exc:
        blocked(db, actor, 'fit.queue', str(exc), batch.code)
    if not positions_for(db, batch.code):
        blocked(db, actor, 'fit.queue', '请先登记本批托盘位置，补测计划按位置生成', batch.code)
    reference = db.scalars(select(DryBatch).filter_by(recipe_code=recipe.code, state=BatchState.REFERENCE)
                           .order_by(DryBatch.code)).all()
    if len(reference) < 3:
        blocked(db, actor, 'fit.queue', '至少需要三个同配方的已认证标定参考批次', batch.code)
    training = []
    for historical in reference:
        archived = latest_run(db, historical.code)
        pairs = paired_measurements(db, archived)
        training.append({
            'batch': historical.code, 'curve_hash': archived.checksum,
            'features': feature_row(historical, recipe, archived), 'observed': observed(pairs), 'pairs': pairs,
        })
    feature_columns = zip(*(item['features'] for item in training), strict=True)
    if not any(max(column) - min(column) > 1e-9 for column in feature_columns):
        blocked(db, actor, 'fit.queue', '参考批次工况一致，请认证有工况变化的同配方历史批次', batch.code)
    snapshot = {
        'batch': batch.code, 'recipe': recipe.code, 'curve_hash': run.checksum,
        'features': feature_row(batch, recipe, run), 'sample_pairs': target_pairs,
        'window': [round(recipe.window_low, 5), round(recipe.window_high, 5)], 'training': training,
    }
    release = CalibrationRelease(run_id=run.id, requested_by=actor, input_hash=checksum(snapshot),
                                 snapshot=snapshot, position_plan=build_position_plan(db, batch, recipe, run))
    db.add(release)
    batch.state = BatchState.CALIBRATING
    record(db, actor, 'fit.queue', f'{run.source_name} · {release.input_hash}', batch.code)
    db.flush()
    return release


def wake_worker(release_id):
    try:
        redis.rpush(QUEUE, release_id)
    except RedisError:
        log.exception('Redis wake-up delayed; worker will scan committed calibrations')


def retry_fit(db, batch, release, actor):
    if release.state != ReleaseState.FAILED:
        blocked(db, actor, 'fit.retry', '仅计算失败的试算可按原输入重试', batch.code)
    release.state = ReleaseState.QUEUED
    release.error = None
    release.touched_at = utcnow()
    record(db, actor, 'fit.retry', f'以原输入 {release.input_hash} 重新计算', batch.code)


def inside(value, window):
    return window[0] <= value <= window[1]


def measure_retest(db, batch, run, release, payload, actor):
    if release.state not in (ReleaseState.AWAITING, ReleaseState.RETEST):
        blocked(db, actor, 'retest.record', '当前校准状态不接受复测', batch.code)
    plan = release.position_plan
    valid_positions = {item['position'] for item in plan['positions']} if plan else set()
    if payload.position not in valid_positions:
        blocked(db, actor, 'retest.record',
                f"本批托盘位置不含 {payload.position}，错位置的补测不能顶数", batch.code)
    # 同一试片的复测重试允许重测（保留历史行），但不重复计入有效试片数。
    prior = [item for item in samples_for(db, run.id, retest=True) if item.specimen == payload.specimen]
    retry = bool(prior)
    attempt_no = (max(item.attempt_no for item in prior) + 1) if prior else 0
    if not retry and any(item.specimen == payload.specimen for item in samples_for(db, run.id)):
        blocked(db, actor, 'retest.record', '补测须取独立试片，试片编号不能与首测重复', batch.code)
    db.add_all([
        ShrinkageSample(run_id=run.id, technician=actor, specimen=payload.specimen, stage=stage,
                        thickness_mm=reading, is_retest=True, attempt_no=attempt_no,
                        position=payload.position, shift=payload.shift)
        for stage, reading in [(Stage.PRE, payload.before_mm), (Stage.POST, payload.after_mm)]
    ])
    pct = shrinkage_of(payload.before_mm, payload.after_mm)
    in_window = inside(pct, release.snapshot['window'])
    release.recheck_pct = pct
    release.recheck_note = payload.note
    release.touched_at = utcnow()
    db.flush()
    coverage = plan_coverage(db, release, run)
    tag = '重试' if retry else '新试片'
    record(db, actor, 'retest.record',
           f"{payload.position} · {payload.specimen} · {payload.shift.value} · {pct:.2f}% · {tag} · {payload.note}",
           batch.code)
    if release.state == ReleaseState.RETEST:
        # 预测已超窗的周期：任何补测都不能翻案，按规则留置整批，另开周期再评估。
        release.state = ReleaseState.HELD
        verdict = '实测超窗' if not in_window else '预测超窗，补测仅留档'
        record(db, actor, 'retest.held', f'{payload.specimen} {verdict}，整批留置', batch.code)
        return coverage
    if not in_window:
        # 预测窗内但某处补测超窗：先留档，签发时整批留置。
        record(db, actor, 'retest.out_of_window',
               f'{payload.position} 补测 {pct:.2f}% 超窗，签发将留置整批', batch.code)
        return coverage
    if coverage['complete'] and coverage['all_inside']:
        release.state = ReleaseState.VERIFIED
        record(db, actor, 'retest.verified',
               f"补测齐 {coverage['required_count']} 处且均在窗内，待主管签发", batch.code)
    return coverage


def sign_release(db, batch, run, release, actor, note):
    if run.state != RunState.FROZEN or latest_run(db, batch.code).id != run.id:
        blocked(db, actor, 'release.sign', '仅当前冻结曲线允许签发', batch.code)
    if release.state == ReleaseState.RELEASED:
        blocked(db, actor, 'release.sign', '该批次已签发', batch.code)
    if release.state not in (ReleaseState.VERIFIED, ReleaseState.AWAITING):
        blocked(db, actor, 'release.sign', '当前校准状态不接受签发', batch.code)
    coverage = plan_coverage(db, release, run)
    if not coverage['complete'] or not coverage['all_inside']:
        release.state = ReleaseState.HELD
        release.touched_at = utcnow()
        missing = '、'.join(coverage['missing']) or '存在超窗补测'
        blocked(db, actor, 'release.sign', f'补测未齐或存在超窗（{missing}），整批留置', batch.code)
    release.state = ReleaseState.RELEASED
    release.signed_at = utcnow()
    release.signed_by = actor
    release.release_note = note
    release.touched_at = release.signed_at
    batch.state = BatchState.RELEASED
    record(db, actor, 'release.sign', f'{run.source_name} · {note}', batch.code)

"""Install the laboratory schema on an empty MySQL database and upgrade older snapshots."""
import re

from sqlalchemy import inspect, text

from src import models
from src.db import engine

# 旧库到当前结构的增量：表名 -> 需要补齐的列定义（MySQL 方言）。
COLUMN_PATCHES = {
    'shrinkage_samples': {
        'position': "VARCHAR(32) NULL",
        'shift': "ENUM('morning','evening','night') NULL",
        'attempt_no': "INTEGER NOT NULL DEFAULT 0",
    },
    'calibration_releases': {
        'position_plan': "JSON NULL",
    },
}


def column_definition(engine, definition):
    if engine.dialect.name == 'sqlite':
        definition = re.sub(r"ENUM\([^)]*\)", 'VARCHAR(32)', definition)
        definition = definition.replace('JSON', 'JSON')
    return definition


def upgrade_existing(inspector):
    """Add columns introduced after the first release; no data is rewritten."""
    existing_tables = set(inspector.get_table_names())
    for table, columns in COLUMN_PATCHES.items():
        if table not in existing_tables:
            continue
        present = {column['name'] for column in inspector.get_columns(table)}
        with engine.begin() as connection:
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {name} {column_definition(engine, definition)}'))
    # 旧的唯一约束按 (run_id, specimen, stage) 建立，重试需要纳入 attempt_no。
    # 约束替换依赖 MySQL 的在线 DDL；SQLite 仅用于本地验证且由 create_all 建当前结构。
    if engine.dialect.name == 'mysql' and 'shrinkage_samples' in existing_tables:
        unique_sets = [
            tuple(column['name'] for column in uq['column_names'])
            for uq in inspector.get_unique_constraints('shrinkage_samples')
        ]
        with engine.begin() as connection:
            if ('run_id', 'specimen', 'stage') in unique_sets:
                connection.execute(text(
                    'ALTER TABLE shrinkage_samples DROP INDEX run_id, '
                    'ADD UNIQUE KEY uq_run_specimen_stage_attempt (run_id, specimen, stage, attempt_no)'))
            elif ('run_id', 'specimen', 'stage', 'attempt_no') not in unique_sets:
                connection.execute(text(
                    'ALTER TABLE shrinkage_samples '
                    'ADD UNIQUE KEY uq_run_specimen_stage_attempt (run_id, specimen, stage, attempt_no)'))


if __name__ == '__main__':
    models.Base.metadata.create_all(engine)
    upgrade_existing(inspect(engine))

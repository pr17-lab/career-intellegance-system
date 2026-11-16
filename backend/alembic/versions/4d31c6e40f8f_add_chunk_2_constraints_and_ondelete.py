"""add_chunk_2_constraints_and_ondelete

Revision ID: 4d31c6e40f8f
Revises: 4a0e9fc1107b
Create Date: 2026-09-20 13:44:00.275948

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4d31c6e40f8f'
down_revision: Union[str, None] = '4a0e9fc1107b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('fk_interview_sessions_roadmap_task_id', 'interview_sessions', type_='foreignkey')
    op.create_foreign_key(
        'fk_interview_sessions_roadmap_task_id', 'interview_sessions', 'roadmap_tasks',
        ['roadmap_task_id'], ['id'], ondelete='SET NULL',
    )
    op.drop_constraint('student_skills_skill_id_fkey', 'student_skills', type_='foreignkey')
    op.create_foreign_key(
        'student_skills_skill_id_fkey', 'student_skills', 'skill_taxonomy',
        ['skill_id'], ['id'], ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint('student_skills_skill_id_fkey', 'student_skills', type_='foreignkey')
    op.create_foreign_key(
        'student_skills_skill_id_fkey', 'student_skills', 'skill_taxonomy',
        ['skill_id'], ['id'],
    )
    op.drop_constraint('fk_interview_sessions_roadmap_task_id', 'interview_sessions', type_='foreignkey')
    op.create_foreign_key(
        'fk_interview_sessions_roadmap_task_id', 'interview_sessions', 'roadmap_tasks',
        ['roadmap_task_id'], ['id'],
    )

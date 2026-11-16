"""
Ownership checks for roadmap task IDs (Phase 1).

Convention: user A (admin, ``sample_user`` / ``auth_client``) owns the task.
User B (student, ``other_user`` / ``other_auth_client``) must never be able to
read or change it by supplying its ID.
"""
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models.interview import InterviewQuestion, InterviewSession
from app.models.learning_resource import LearningResource
from app.models.roadmap import Roadmap, RoadmapTask


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_roadmap_for(db, user, skill, resource_url="https://example.com/b-resource"):
    """Seed an active roadmap with a learn and an apply task for ``user``."""
    roadmap = Roadmap(
        id=uuid.uuid4(), user_id=user.id, job_role="Backend Engineer",
        status="active", total_tasks=2, completed_tasks=0,
    )
    db.add(roadmap)
    db.commit()
    learn = RoadmapTask(
        id=uuid.uuid4(), roadmap_id=roadmap.id, skill_id=skill.id,
        associated_skill_id=skill.id, phase="learn", task_type="learn",
        title="B learn", estimated_hours=5, order_index=1, status="pending",
        resource_url=resource_url,
    )
    apply_ = RoadmapTask(
        id=uuid.uuid4(), roadmap_id=roadmap.id, skill_id=skill.id,
        associated_skill_id=skill.id, phase="apply", task_type="apply",
        title="B apply", estimated_hours=5, order_index=2, status="pending",
    )
    db.add_all([learn, apply_])
    db.commit()
    return roadmap, learn, apply_


def snap_task(db, task):
    """Everything an attacker could change on the task or its roadmap."""
    db.refresh(task)
    rm = db.query(Roadmap).filter(Roadmap.id == task.roadmap_id).one()
    db.refresh(rm)
    return {
        "status": task.status,
        "validation_status": task.validation_status,
        "submission_link": task.submission_link,
        "completed_at": task.completed_at,
        "feedback_score": task.feedback_score,
        "learning_resource_id": task.learning_resource_id,
        "resource_source": task.resource_source,
        "rm_completed_tasks": rm.completed_tasks,
        "rm_status": rm.status,
    }


def snap_resources(db):
    db.expire_all()
    return sorted(
        (str(r.id), r.upvotes, r.downvotes) for r in db.query(LearningResource).all()
    )


def snap_all_tasks(db, roadmap):
    db.expire_all()
    return sorted(
        (str(t.id), t.status, t.validation_status, t.submission_link)
        for t in db.query(RoadmapTask).filter(RoadmapTask.roadmap_id == roadmap.id)
    )


@pytest.fixture
def a_tasks(sample_roadmap_and_tasks, db_session):
    """User A's roadmap/tasks, with a resource URL on the learn task."""
    roadmap, learn, apply_, skill = sample_roadmap_and_tasks
    learn.resource_url = "https://react.dev/a-resource"
    db_session.commit()
    return roadmap, learn, apply_, skill


@pytest.fixture
def b_tasks(db_session, other_user, a_tasks):
    skill = a_tasks[3]
    return make_roadmap_for(db_session, other_user, skill)


GITHUB = {"submission_link": "https://github.com/test/repo"}


# ---------------------------------------------------------------------------
# A1: POST /roadmap/tasks/{id}/complete
# ---------------------------------------------------------------------------

def test_complete_apply_foreign_task_404_and_unchanged(
    other_auth_client, db_session, a_tasks
):
    roadmap, learn, apply_, _ = a_tasks
    learn.status = "completed"
    db_session.commit()
    before = snap_task(db_session, apply_)

    with patch("app.modules.roadmap.router.verify_github_complexity_async") as verify:
        resp = other_auth_client.post(
            f"/api/roadmap/tasks/{apply_.id}/complete", json=GITHUB
        )

    assert resp.status_code == 404
    assert snap_task(db_session, apply_) == before
    verify.assert_not_called()


def test_complete_foreign_non_apply_task_404_and_unchanged(
    other_auth_client, db_session, a_tasks
):
    _, learn, _, _ = a_tasks
    before = snap_task(db_session, learn)
    resp = other_auth_client.post(f"/api/roadmap/tasks/{learn.id}/complete", json={})
    assert resp.status_code == 404
    assert snap_task(db_session, learn) == before


def test_complete_foreign_apply_cannot_complete_owners_practice_sibling(
    other_auth_client, db_session, other_user, other_student_profile, a_tasks
):
    """B's own high interview score must not complete A's practice sibling."""
    roadmap, learn, apply_, skill = a_tasks
    learn.status = "completed"
    practice = RoadmapTask(
        id=uuid.uuid4(), roadmap_id=roadmap.id, skill_id=skill.id,
        associated_skill_id=skill.id, phase="practice", task_type="practice",
        title="A practice", estimated_hours=2, order_index=2, status="pending",
    )
    db_session.add(practice)
    # B has a completed interview scoring 8 on the same skill.
    sess = InterviewSession(
        user_id=other_user.id, branch="CSE", topic="x", status="completed",
        is_micro=True, associated_skill_id=skill.id,
    )
    db_session.add(sess)
    db_session.flush()
    db_session.add(InterviewQuestion(
        session_id=sess.id, topic="x", question="q", difficulty="hard", ai_score=8,
    ))
    db_session.commit()
    before = snap_all_tasks(db_session, roadmap)

    with patch("app.modules.roadmap.router.verify_github_complexity_async"):
        resp = other_auth_client.post(
            f"/api/roadmap/tasks/{apply_.id}/complete", json=GITHUB
        )

    assert resp.status_code == 404
    assert snap_all_tasks(db_session, roadmap) == before


def test_complete_owner_apply_still_succeeds(auth_client, db_session, a_tasks):
    _, learn, apply_, _ = a_tasks
    learn.status = "completed"
    db_session.commit()
    with patch("app.modules.roadmap.router.verify_github_complexity_async") as verify:
        resp = auth_client.post(f"/api/roadmap/tasks/{apply_.id}/complete", json=GITHUB)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    verify.assert_called_once()


def test_complete_owner_non_apply_still_succeeds(auth_client, a_tasks):
    _, learn, _, _ = a_tasks
    resp = auth_client.post(f"/api/roadmap/tasks/{learn.id}/complete", json={})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# A2: POST /roadmap/tasks/{id}/vote
# ---------------------------------------------------------------------------

def test_vote_foreign_task_404_and_unchanged(other_auth_client, db_session, a_tasks):
    _, learn, _, _ = a_tasks
    before_task = snap_task(db_session, learn)
    before_res = snap_resources(db_session)

    resp = other_auth_client.post(
        f"/api/roadmap/tasks/{learn.id}/vote", json={"vote_type": "upvote"}
    )

    assert resp.status_code == 404
    assert snap_task(db_session, learn) == before_task
    assert snap_resources(db_session) == before_res
    assert before_res == []  # and nothing was created


def test_vote_foreign_task_with_curated_resource_unchanged(
    other_auth_client, db_session, a_tasks
):
    """Existing LearningResource counters must not move either."""
    _, learn, _, skill = a_tasks
    lr = LearningResource(
        skill_id=skill.id, title="t", resource_url=learn.resource_url,
        platform="p", phase="learn", upvotes=3, downvotes=1,
    )
    db_session.add(lr)
    db_session.flush()
    learn.learning_resource_id = lr.id
    db_session.commit()
    before = snap_resources(db_session)

    resp = other_auth_client.post(
        f"/api/roadmap/tasks/{learn.id}/vote", json={"vote_type": "downvote"}
    )
    assert resp.status_code == 404
    assert snap_resources(db_session) == before


def test_vote_owner_still_succeeds(auth_client, a_tasks):
    _, learn, _, _ = a_tasks
    resp = auth_client.post(
        f"/api/roadmap/tasks/{learn.id}/vote", json={"vote_type": "upvote"}
    )
    assert resp.status_code == 200
    assert (resp.json()["upvotes"], resp.json()["downvotes"]) == (1, 0)


@pytest.mark.parametrize("bad", ["x", "UPVOTE", "", "up"])
def test_vote_invalid_vote_type_422_and_creates_nothing(
    auth_client, db_session, a_tasks, bad
):
    _, learn, _, _ = a_tasks
    before_task = snap_task(db_session, learn)
    resp = auth_client.post(
        f"/api/roadmap/tasks/{learn.id}/vote", json={"vote_type": bad}
    )
    assert resp.status_code == 422
    assert snap_resources(db_session) == []
    assert snap_task(db_session, learn) == before_task


# ---------------------------------------------------------------------------
# Same status and same body for "doesn't exist" vs "exists but not yours"
# ---------------------------------------------------------------------------

def _post(client, path, json):
    r = client.post(path, json=json)
    return r.status_code, r.json()


def test_complete_unknown_and_foreign_task_are_indistinguishable(
    other_auth_client, a_tasks
):
    _, learn, apply_, _ = a_tasks
    for real in (learn.id, apply_.id):
        unknown = _post(other_auth_client, f"/api/roadmap/tasks/{uuid.uuid4()}/complete", GITHUB)
        foreign = _post(other_auth_client, f"/api/roadmap/tasks/{real}/complete", GITHUB)
        assert foreign == unknown
        assert foreign[0] == 404


def test_vote_unknown_and_foreign_task_are_indistinguishable(other_auth_client, a_tasks):
    _, learn, _, _ = a_tasks
    body = {"vote_type": "upvote"}
    unknown = _post(other_auth_client, f"/api/roadmap/tasks/{uuid.uuid4()}/vote", body)
    foreign = _post(other_auth_client, f"/api/roadmap/tasks/{learn.id}/vote", body)
    assert foreign == unknown
    assert foreign[0] == 404


# ---------------------------------------------------------------------------
# No admin bypass: admin A acting on student B's task
# ---------------------------------------------------------------------------

def test_admin_cannot_complete_or_vote_on_other_users_task(
    auth_client, db_session, b_tasks
):
    roadmap_b, learn_b, apply_b = b_tasks
    before_learn = snap_task(db_session, learn_b)
    before_apply = snap_task(db_session, apply_b)
    before_res = snap_resources(db_session)

    with patch("app.modules.roadmap.router.verify_github_complexity_async") as verify:
        r1 = auth_client.post(f"/api/roadmap/tasks/{apply_b.id}/complete", json=GITHUB)
        r2 = auth_client.post(f"/api/roadmap/tasks/{learn_b.id}/complete", json={})
    r3 = auth_client.post(
        f"/api/roadmap/tasks/{learn_b.id}/vote", json={"vote_type": "upvote"}
    )

    assert (r1.status_code, r2.status_code, r3.status_code) == (404, 404, 404)
    verify.assert_not_called()
    assert snap_task(db_session, learn_b) == before_learn
    assert snap_task(db_session, apply_b) == before_apply
    assert snap_resources(db_session) == before_res


# ---------------------------------------------------------------------------
# B1: POST /interview/sessions  (roadmap_task_id in body)
# B2: POST /interview/sessions/micro  (roadmap_task_id query param)
# ---------------------------------------------------------------------------

FAKE_QUESTIONS = (
    [
        {"question": "What is FastAPI?", "topic": "FastAPI", "difficulty": "medium"},
        {"question": "What is Depends?", "topic": "FastAPI", "difficulty": "medium"},
    ],
    "mock",
)


@contextmanager
def no_ai():
    """No network: stub question generation, force the static micro fallback."""
    with patch(
        "app.modules.interview.service.InterviewService.generate_questions_async",
        return_value=FAKE_QUESTIONS,
    ), patch("app.modules.interview.service.settings.GROQ_API_KEY", None),          patch("app.modules.interview.service.settings.GEMINI_API_KEY", None):
        yield


def session_count(db, user):
    db.expire_all()
    return db.query(InterviewSession).filter(InterviewSession.user_id == user.id).count()


def create_session(client, task_id):
    return client.post(
        "/api/interview/sessions",
        json={"jd_text": "", "limit": 2, "roadmap_task_id": str(task_id)},
    )


def create_micro(client, skill_id, task_id):
    return client.post(
        f"/api/interview/sessions/micro?skill_id={skill_id}&roadmap_task_id={task_id}"
    )


def test_create_session_foreign_task_404_and_no_session(
    other_auth_client, db_session, other_user, other_student_profile, a_tasks
):
    _, _, apply_, _ = a_tasks
    before = snap_task(db_session, apply_)
    with no_ai():
        resp = create_session(other_auth_client, apply_.id)
    assert resp.status_code == 404
    assert session_count(db_session, other_user) == 0
    assert db_session.query(InterviewSession).count() == 0
    assert snap_task(db_session, apply_) == before


def test_create_session_unknown_and_foreign_task_are_indistinguishable(
    other_auth_client, other_student_profile, a_tasks
):
    _, _, apply_, _ = a_tasks
    with no_ai():
        unknown = create_session(other_auth_client, uuid.uuid4())
        foreign = create_session(other_auth_client, apply_.id)
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


def test_create_session_admin_cannot_use_other_users_task(
    auth_client, db_session, sample_user, sample_student_profile, b_tasks
):
    _, _, apply_b = b_tasks
    with no_ai():
        resp = create_session(auth_client, apply_b.id)
    assert resp.status_code == 404
    assert session_count(db_session, sample_user) == 0


def test_create_session_owner_still_succeeds(
    auth_client, sample_student_profile, a_tasks
):
    _, _, apply_, skill = a_tasks
    with no_ai():
        resp = create_session(auth_client, apply_.id)
    assert resp.status_code == 201
    assert resp.json()["roadmap_task_id"] == str(apply_.id)
    assert resp.json()["associated_skill_id"] == str(skill.id)


def test_micro_session_foreign_task_404_not_500_and_no_session(
    other_auth_client, db_session, other_user, other_student_profile, a_tasks
):
    _, _, apply_, skill = a_tasks
    before = snap_task(db_session, apply_)
    with no_ai():
        resp = create_micro(other_auth_client, skill.id, apply_.id)
    assert resp.status_code == 404
    assert session_count(db_session, other_user) == 0
    assert db_session.query(InterviewSession).count() == 0
    assert snap_task(db_session, apply_) == before


def test_micro_session_unknown_and_foreign_task_are_indistinguishable(
    other_auth_client, other_student_profile, a_tasks
):
    _, _, apply_, skill = a_tasks
    with no_ai():
        unknown = create_micro(other_auth_client, skill.id, uuid.uuid4())
        foreign = create_micro(other_auth_client, skill.id, apply_.id)
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


def test_micro_session_admin_cannot_use_other_users_task(
    auth_client, db_session, sample_user, sample_student_profile, b_tasks, a_tasks
):
    _, _, apply_b = b_tasks
    skill = a_tasks[3]
    with no_ai():
        resp = create_micro(auth_client, skill.id, apply_b.id)
    assert resp.status_code == 404
    assert session_count(db_session, sample_user) == 0


def test_micro_session_owner_still_succeeds(
    auth_client, sample_student_profile, a_tasks
):
    _, _, apply_, skill = a_tasks
    with no_ai():
        resp = create_micro(auth_client, skill.id, apply_.id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["roadmap_task_id"] == str(apply_.id)
    assert resp.json()["is_micro"] is True

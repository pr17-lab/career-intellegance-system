"""
Ownership checks for roadmap task IDs (Phase 1).

Convention: user A (admin, ``sample_user`` / ``auth_client``) owns the task.
User B (student, ``other_user`` / ``other_auth_client``) must never be able to
read or change it by supplying its ID.
"""
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# C1: micro-interview completion hook (evaluate_single_answer_async)
# ---------------------------------------------------------------------------

@contextmanager
def gemini_scores(technical_score):
    """Mock the Gemini evaluation call so the answer scores ``technical_score``."""
    import json as _json
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": _json.dumps({
        "technical_score": technical_score, "communication_score": technical_score,
        "verdict": "Strong", "feedback": "ok", "mistakes": [],
        "improvement": "none", "model_answer": "m",
    })}]}}]}
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    with patch("app.modules.interview.service.settings.GEMINI_API_KEY", "test"),          patch("app.modules.interview.service.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=False)
        yield


def micro_session_with_last_answer_pending(db, user, skill, task_id):
    """Micro session; two questions already scored 8/7, third answered but unscored."""
    sess = InterviewSession(
        user_id=user.id, branch="CSE", topic="Micro", status="active",
        is_micro=True, associated_skill_id=skill.id, roadmap_task_id=task_id,
    )
    db.add(sess)
    db.flush()
    qs = [
        InterviewQuestion(session_id=sess.id, topic="t", question="Q1", difficulty="hard",
                          ai_score=8, user_answer="a1"),
        InterviewQuestion(session_id=sess.id, topic="t", question="Q2", difficulty="hard",
                          ai_score=7, user_answer="a2"),
        InterviewQuestion(session_id=sess.id, topic="t", question="Q3", difficulty="hard",
                          user_answer="a3"),
    ]
    db.add_all(qs)
    db.commit()
    return sess, qs[2]


async def run_hook(db, user, sess, last_q):
    from app.modules.interview.service import interview_service
    with gemini_scores(8):
        return await interview_service.evaluate_single_answer_async(
            db, user_id=user.id, session_id=sess.id, question_id=last_q.id,
            user_answer="a3",
        )


@pytest.mark.asyncio
async def test_hook_does_not_complete_task_owned_by_someone_else(
    db_session, other_user, a_tasks
):
    """B's session carries A's task ID (e.g. a row created before the fix)."""
    roadmap, _, apply_, skill = a_tasks
    sess, last_q = micro_session_with_last_answer_pending(
        db_session, other_user, skill, apply_.id
    )
    before = snap_task(db_session, apply_)

    result = await run_hook(db_session, other_user, sess, last_q)

    assert result["technical_score"] == 8          # evaluation itself still works
    assert snap_task(db_session, apply_) == before  # A's task and roadmap untouched
    assert apply_.status == "pending"


@pytest.mark.asyncio
async def test_hook_foreign_task_for_admin_session_owner_is_skipped_too(
    db_session, sample_user, b_tasks, a_tasks
):
    """No admin bypass: admin A's session pointing at B's task does nothing."""
    _, _, apply_b = b_tasks
    skill = a_tasks[3]
    sess, last_q = micro_session_with_last_answer_pending(
        db_session, sample_user, skill, apply_b.id
    )
    before = snap_task(db_session, apply_b)
    await run_hook(db_session, sample_user, sess, last_q)
    assert snap_task(db_session, apply_b) == before


@pytest.mark.asyncio
async def test_hook_owner_still_completes_own_task(db_session, sample_user, a_tasks):
    roadmap, _, apply_, skill = a_tasks
    sess, last_q = micro_session_with_last_answer_pending(
        db_session, sample_user, skill, apply_.id
    )
    await run_hook(db_session, sample_user, sess, last_q)
    db_session.refresh(apply_)
    db_session.refresh(roadmap)
    assert apply_.status == "completed"
    assert apply_.validation_status == "verified"
    assert roadmap.completed_tasks == 1


# ---------------------------------------------------------------------------
# D1-D3: background verify_github_complexity_async(repo, user_id, db, task_id)
# ---------------------------------------------------------------------------

REPO = "https://github.com/student/fastapi-app"


@contextmanager
def github_ok():
    """Mock GitHub + Gemini so verification succeeds (extracts 'FastAPI')."""
    def resp(status=200, payload=None):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        r.raise_for_status = MagicMock()
        return r

    repo = resp(payload={"description": "FastAPI App"})
    commits = resp(payload=[{"sha": f"sha{i}"} for i in range(15)])
    contents = resp(payload=[{"name": "conftest.py"}])
    readme = resp(payload={"content": "dGVzdCByZWFkbWU="})
    gemini = resp(payload={"candidates": [{"content": {"parts": [{"text": "[\"FastAPI\"]"}]}}]})

    async def get(url, headers=None):
        if "/commits" in url:
            return commits
        if "/contents" in url:
            return contents
        if "/readme" in url:
            return readme
        return repo

    client = AsyncMock()
    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(return_value=gemini)
    with patch("app.modules.skills.project_service.settings.GEMINI_API_KEY", "test"),          patch("app.modules.skills.project_service.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=False)
        yield


@contextmanager
def github_404():
    client = AsyncMock()
    missing = MagicMock()
    missing.status_code = 404
    client.get = AsyncMock(return_value=missing)
    with patch("app.modules.skills.project_service.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__ = AsyncMock(return_value=client)
        cls.return_value.__aexit__ = AsyncMock(return_value=False)
        yield


async def verify(db, user, task):
    from app.modules.skills.project_service import verify_github_complexity_async
    await verify_github_complexity_async(
        repo_url=REPO, user_id=user.id, db=db, task_id=task.id
    )


@pytest.mark.asyncio
async def test_verify_success_does_not_complete_foreign_task(
    db_session, other_user, a_tasks
):
    _, _, apply_, _ = a_tasks
    before = snap_task(db_session, apply_)
    with github_ok():
        await verify(db_session, other_user, apply_)
    assert snap_task(db_session, apply_) == before


@pytest.mark.asyncio
async def test_verify_failure_does_not_reset_foreign_completed_task(
    db_session, other_user, a_tasks
):
    """D1: today B's failed check flips A's completed task back to pending."""
    roadmap, _, apply_, _ = a_tasks
    apply_.status = "completed"
    apply_.validation_status = "verified"
    roadmap.completed_tasks = 1
    db_session.commit()
    before = snap_task(db_session, apply_)
    with github_404():
        await verify(db_session, other_user, apply_)
    assert snap_task(db_session, apply_) == before


@pytest.mark.asyncio
async def test_verify_exception_path_does_not_reset_foreign_completed_task(
    db_session, other_user, a_tasks
):
    """D3: an error after the HTTP phase must not touch a foreign task either."""
    roadmap, _, apply_, _ = a_tasks
    apply_.status = "completed"
    apply_.validation_status = "verified"
    roadmap.completed_tasks = 1
    db_session.commit()
    before = snap_task(db_session, apply_)
    with github_ok(), patch(
        "app.modules.skills.project_service.calculate_composite_score",
        side_effect=RuntimeError("boom"),
    ):
        await verify(db_session, other_user, apply_)
    assert snap_task(db_session, apply_) == before


@pytest.mark.asyncio
async def test_verify_admin_cannot_complete_other_users_task(
    db_session, sample_user, b_tasks, a_tasks
):
    _, _, apply_b = b_tasks
    before = snap_task(db_session, apply_b)
    with github_ok():
        await verify(db_session, sample_user, apply_b)
    assert snap_task(db_session, apply_b) == before


@pytest.mark.asyncio
async def test_verify_owner_still_completes_own_task(db_session, sample_user, a_tasks):
    roadmap, _, apply_, _ = a_tasks
    with github_ok():
        await verify(db_session, sample_user, apply_)
    db_session.refresh(apply_)
    db_session.refresh(roadmap)
    assert apply_.status == "completed"
    assert apply_.validation_status == "verified"
    assert roadmap.completed_tasks == 1


@pytest.mark.asyncio
async def test_verify_owner_failure_still_marks_own_task_failed(
    db_session, sample_user, a_tasks
):
    _, _, apply_, _ = a_tasks
    with github_404():
        await verify(db_session, sample_user, apply_)
    db_session.refresh(apply_)
    assert apply_.validation_status == "failed"
    assert apply_.status == "pending"

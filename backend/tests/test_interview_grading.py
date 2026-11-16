"""Grading of a single answer. The LLM is faked: no real API calls."""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.interview import InterviewQuestion, InterviewSession
from app.models.skill_taxonomy import SkillTaxonomy
from app.models.student_project import StudentProject
from app.models.student_skill import StudentSkill
from app.modules.interview.prompts import MAX_ANSWER_CHARS, build_grading_message
from app.modules.interview.service import interview_service

NOT_EVALUATED = "We couldn't evaluate this answer. It was saved without a score."


def reply(technical_score=8, **overrides):
    data = {
        "technical_score": technical_score,
        "communication_score": 7,
        "feedback": "Good.",
        "mistakes": [],
        "improvement": "Add an example.",
        "model_answer": "A model answer.",
    }
    data.update(overrides)
    return json.dumps(data)


def fake_llms(gemini, groq):
    """Each argument is a reply string, or an Exception to raise."""
    def fake(outcome):
        if isinstance(outcome, Exception):
            return AsyncMock(side_effect=outcome)
        return AsyncMock(return_value=outcome)

    gemini_patch = patch("app.modules.interview.service._gemini_grade", fake(gemini))
    groq_patch = patch("app.modules.interview.service._groq_grade", fake(groq))
    return gemini_patch, groq_patch


async def grade(db, user, question, gemini, groq=RuntimeError("groq down")):
    gemini_patch, groq_patch = fake_llms(gemini, groq)
    with gemini_patch, groq_patch:
        result = await interview_service.evaluate_single_answer_async(
            db,
            user_id=user.id,
            session_id=question.session_id,
            question_id=question.id,
            user_answer="My answer",
        )
    db.refresh(question)
    return result


def make_session(db, user, project=None, scores=(None,)):
    """A micro session with one question per entry in `scores` (None = not scored yet)."""
    session = InterviewSession(
        user_id=user.id,
        branch="CSE",
        topic="Test",
        status="active",
        is_micro=True,
        project_id=project.id if project else None,
    )
    db.add(session)
    db.flush()
    questions = [
        InterviewQuestion(
            session_id=session.id, topic="React", question=f"Q{i}", difficulty="hard",
            ai_score=score, user_answer="Answer" if score is not None else None,
        )
        for i, score in enumerate(scores)
    ]
    db.add_all(questions)
    db.commit()
    return questions


def make_project(db, user):
    project = StudentProject(
        id=uuid.uuid4(), user_id=user.id, title="Grading project",
        description="Demo", repo_url="https://github.com/student/demo", depth_verified=False,
    )
    db.add(project)
    db.commit()
    return project


@pytest.mark.asyncio
@pytest.mark.parametrize("score, verdict", [(10, "Strong"), (8, "Strong"), (7, "Adequate"), (5, "Adequate"), (4, "Weak"), (0, "Weak")])
async def test_valid_reply_saves_score_and_computes_verdict(db_session, sample_user, score, verdict):
    (q,) = make_session(db_session, sample_user)

    # The model claims a different verdict; the code ignores it.
    result = await grade(db_session, sample_user, q, reply(score, verdict="Weak" if score >= 5 else "Strong"))

    assert q.ai_score == score
    assert q.ai_verdict == verdict
    assert q.user_answer == "My answer"
    assert q.evaluated_at is not None
    assert result["technical_score"] == score
    assert result["verdict"] == verdict


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_reply", [
    reply(99),
    reply(-1),
    reply(7.5),
    json.dumps({"technical_score": 7}),
    json.dumps([json.loads(reply())]),
    "not json at all",
])
async def test_invalid_reply_saves_no_score(db_session, sample_user, bad_reply):
    (q,) = make_session(db_session, sample_user)

    result = await grade(db_session, sample_user, q, bad_reply, bad_reply)

    assert q.user_answer == "My answer"
    assert q.ai_score is None
    assert q.evaluated_at is None
    assert result["technical_score"] is None
    assert result["feedback"] == NOT_EVALUATED


@pytest.mark.asyncio
async def test_groq_used_when_gemini_fails(db_session, sample_user):
    (q,) = make_session(db_session, sample_user)

    await grade(db_session, sample_user, q, RuntimeError("gemini down"), reply(9))

    assert q.ai_score == 9
    assert q.evaluated_at is not None


@pytest.mark.asyncio
async def test_groq_used_when_gemini_reply_is_invalid(db_session, sample_user):
    (q,) = make_session(db_session, sample_user)

    await grade(db_session, sample_user, q, reply(99), reply(6))

    assert q.ai_score == 6


@pytest.mark.asyncio
async def test_both_fail_leaves_weights_unchanged(db_session, sample_user):
    skill = SkillTaxonomy(id=uuid.uuid4(), skill_name="React", category="frontend", skill_type="tool")
    db_session.add(skill)
    db_session.commit()
    ss = StudentSkill(
        user_id=sample_user.id, skill_id=skill.id, resume_weight=30.0,
        project_weight=0.0, interview_weight=20.0, communication_weight=40.0,
    )
    db_session.add(ss)
    db_session.commit()
    (q,) = make_session(db_session, sample_user)

    result = await grade(db_session, sample_user, q, RuntimeError("down"), RuntimeError("down"))

    db_session.refresh(ss)
    assert q.user_answer == "My answer"
    assert q.ai_score is None
    assert q.evaluated_at is None
    assert result["feedback"] == NOT_EVALUATED
    assert float(ss.interview_weight) == 20.0
    assert float(ss.communication_weight) == 40.0


@pytest.mark.asyncio
async def test_project_with_unscored_question_is_not_verified(db_session, sample_user):
    project = make_project(db_session, sample_user)
    _, _, last = make_session(db_session, sample_user, project, scores=(9, None, None))

    await grade(db_session, sample_user, last, reply(9))

    db_session.refresh(project)
    assert project.depth_verified is False


@pytest.mark.asyncio
async def test_project_last_answer_unscored_is_not_verified(db_session, sample_user):
    project = make_project(db_session, sample_user)
    _, _, last = make_session(db_session, sample_user, project, scores=(9, 9, None))

    await grade(db_session, sample_user, last, RuntimeError("down"), RuntimeError("down"))

    db_session.refresh(project)
    assert project.depth_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize("last_score, verified", [(9, True), (5, True), (4, False)])
async def test_project_verified_when_all_scored_and_average_at_least_7(db_session, sample_user, last_score, verified):
    project = make_project(db_session, sample_user)
    # Other two questions score 8 and 8: averages are 8.33, 7.0 and 6.67.
    _, _, last = make_session(db_session, sample_user, project, scores=(8, 8, None))

    await grade(db_session, sample_user, last, reply(last_score))

    db_session.refresh(project)
    assert project.depth_verified is verified


def test_answer_is_capped_in_prompt():
    message = build_grading_message("general", "", "", "Q?", "x" * (MAX_ANSWER_CHARS + 500))

    assert "x" * MAX_ANSWER_CHARS in message
    assert "x" * (MAX_ANSWER_CHARS + 1) not in message
    assert "<question>Q?</question>" in message

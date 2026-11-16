"""Weak skills found in a finished WebSocket interview are added to the active roadmap. The LLM is faked."""
import json
import uuid
from unittest.mock import AsyncMock, patch

from app.models.interview import InterviewQuestion, InterviewSession
from app.models.roadmap import Roadmap, RoadmapTask
from app.models.skill_taxonomy import SkillTaxonomy
from app.modules.interview.service import extract_weak_skills
from tests.test_api_interview import _get_token

SKILL = "Weak Skill Topic"


def grade_reply(score):
    return json.dumps({
        "technical_score": score, "communication_score": 5, "feedback": "f",
        "mistakes": [], "improvement": "i", "model_answer": "m",
    })


def make_skill(db):
    skill = db.query(SkillTaxonomy).filter(SkillTaxonomy.skill_name == SKILL).first()
    if not skill:
        skill = SkillTaxonomy(id=uuid.uuid4(), skill_name=SKILL, category="core_cs", skill_type="concept")
        db.add(skill)
        db.commit()
    return skill


def make_roadmap(db, user):
    roadmap = Roadmap(id=uuid.uuid4(), user_id=user.id, job_role="Dev", status="active", total_tasks=0, completed_tasks=0)
    db.add(roadmap)
    db.commit()
    return roadmap


def make_session(db, user):
    """A 3-question micro session: two scored 9, the last one waiting for an answer."""
    session = InterviewSession(user_id=user.id, branch="CSE", topic="T", status="active", is_micro=True)
    db.add(session)
    db.flush()
    questions = [
        InterviewQuestion(session_id=session.id, topic=SKILL, question="Q1", difficulty="easy", user_answer="a", ai_score=9),
        InterviewQuestion(session_id=session.id, topic=SKILL, question="Q2", difficulty="easy", user_answer="a", ai_score=9),
        InterviewQuestion(session_id=session.id, topic=SKILL, question="Q3", difficulty="easy"),
    ]
    db.add_all(questions)
    db.commit()
    return session, questions[2]


def answer_last_question(client, session, question, gemini, groq=RuntimeError("down")):
    """Submit the last answer over the WebSocket. Returns the events up to session_completed."""
    token = _get_token(client)
    events = []
    with patch("app.modules.interview.service._gemini_grade", AsyncMock(side_effect=gemini if isinstance(gemini, Exception) else None, return_value=gemini)), \
         patch("app.modules.interview.service._groq_grade", AsyncMock(side_effect=groq if isinstance(groq, Exception) else None, return_value=groq)), \
         patch("app.modules.roadmap.service.generate_resources_with_llm", AsyncMock(return_value=None)), \
         client.websocket_connect(f"/api/interview/ws/interview/{session.id}?token={token}") as ws:
        ws.receive_json()  # session_ready
        ws.send_json({"event": "submit_answer", "data": {"question_id": str(question.id), "answer_text": "my answer"}})
        events.append(ws.receive_json())  # answer_evaluated
        events.append(ws.receive_json())  # session_completed
        ws.send_json({"event": "ping", "data": {}})
        events.append(ws.receive_json())  # pong: the socket still works
    return events


def tasks_for_skill(db, skill):
    return db.query(RoadmapTask).filter(RoadmapTask.skill_id == skill.id).all()


def test_weak_answer_adds_tasks_to_active_roadmap(client, db_session, sample_user):
    skill = make_skill(db_session)
    roadmap = make_roadmap(db_session, sample_user)
    session, last = make_session(db_session, sample_user)

    events = answer_last_question(client, session, last, grade_reply(3))

    assert [e["event"] for e in events] == ["answer_evaluated", "session_completed", "pong"]
    assert sorted(t.phase for t in tasks_for_skill(db_session, skill)) == ["apply", "learn", "practice"]
    db_session.refresh(roadmap)
    assert roadmap.total_tasks == 3


def test_same_skill_weak_again_adds_no_duplicates(client, db_session, sample_user):
    skill = make_skill(db_session)
    make_roadmap(db_session, sample_user)
    for _ in range(2):
        session, last = make_session(db_session, sample_user)
        answer_last_question(client, session, last, grade_reply(3))

    assert len(tasks_for_skill(db_session, skill)) == 3


def test_no_active_roadmap_creates_nothing(client, db_session, sample_user):
    skill = make_skill(db_session)
    session, last = make_session(db_session, sample_user)

    events = answer_last_question(client, session, last, grade_reply(3))

    assert events[1]["event"] == "session_completed"
    assert tasks_for_skill(db_session, skill) == []
    assert db_session.query(Roadmap).filter(Roadmap.user_id == sample_user.id).count() == 0


def test_unscored_answer_is_not_weak(client, db_session, sample_user):
    skill = make_skill(db_session)
    make_roadmap(db_session, sample_user)
    session, last = make_session(db_session, sample_user)

    # Both models fail, so the last answer is saved without a score.
    answer_last_question(client, session, last, RuntimeError("down"))

    db_session.refresh(last)
    assert last.ai_score is None
    assert tasks_for_skill(db_session, skill) == []


def test_extract_weak_skills_ignores_unscored():
    questions = [
        InterviewQuestion(topic="A", ai_score=None),
        InterviewQuestion(topic="B", ai_score=3),
        InterviewQuestion(topic="C", ai_score=9),
    ]
    assert extract_weak_skills(questions) == ["B"]


def test_roadmap_update_failure_keeps_session_completed(client, db_session, sample_user):
    make_skill(db_session)
    make_roadmap(db_session, sample_user)
    session, last = make_session(db_session, sample_user)

    with patch("app.modules.roadmap.service.update_roadmap_with_weak_skills", AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("app.modules.interview.router.logger") as router_logger:
        events = answer_last_question(client, session, last, grade_reply(3))

    assert [e["event"] for e in events] == ["answer_evaluated", "session_completed", "pong"]
    db_session.refresh(session)
    assert session.status == "completed"
    assert "Failed to update roadmap" in router_logger.error.call_args.args[0]

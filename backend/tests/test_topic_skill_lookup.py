"""Topic/micro sessions use their own skill; other topics match a skill name or alias exactly."""
import json
import uuid
from unittest.mock import patch


from app.models.interview import InterviewQuestion, InterviewSession
from app.models.roadmap import RoadmapTask
from app.models.skill_taxonomy import SkillTaxonomy
from app.models.student_project import StudentProject
from app.models.student_skill import StudentSkill
from app.modules.skills.service import find_skill_by_topic
from tests.test_weak_skill_roadmap import answer_last_question, grade_reply, make_roadmap


def add_skill(db, name, aliases=None):
    skill = SkillTaxonomy(id=uuid.uuid4(), skill_name=name, category="core_cs", skill_type="concept", aliases=aliases)
    db.add(skill)
    db.commit()
    return skill


def make_session(db, user, topic, skill_id, project_id=None):
    """Micro session: two answered questions, then one unanswered question about `topic`."""
    session = InterviewSession(
        user_id=user.id, branch="CSE", topic="T", status="active", is_micro=True,
        associated_skill_id=skill_id, project_id=project_id,
    )
    db.add(session)
    db.flush()
    for n in (1, 2):
        db.add(InterviewQuestion(session_id=session.id, topic="Done", question=f"Q{n}", difficulty="easy", user_answer="a", ai_score=9))
    question = InterviewQuestion(session_id=session.id, topic=topic, question="Q3", difficulty="easy")
    db.add(question)
    db.commit()
    return session, question


def interview_weight(db, user, skill):
    row = db.query(StudentSkill).filter(StudentSkill.user_id == user.id, StudentSkill.skill_id == skill.id).first()
    return row.interview_weight if row else None


def test_topic_session_uses_its_own_skill_not_llm_topic_text(client, db_session, sample_user):
    skill = add_skill(db_session, "Own Skill")
    other = add_skill(db_session, "Llm Topic Text")
    make_roadmap(db_session, sample_user)
    session, question = make_session(db_session, sample_user, "Llm Topic Text", skill.id)

    answer_last_question(client, session, question, grade_reply(9))

    assert interview_weight(db_session, sample_user, skill) == 10
    assert interview_weight(db_session, sample_user, other) is None


def test_topic_session_roadmap_uses_its_own_skill(client, db_session, sample_user):
    skill = add_skill(db_session, "Own Skill")
    add_skill(db_session, "Llm Topic Text")
    make_roadmap(db_session, sample_user)
    session, question = make_session(db_session, sample_user, "Llm Topic Text", skill.id)

    answer_last_question(client, session, question, grade_reply(2))

    tasks = db_session.query(RoadmapTask).all()
    assert tasks and {t.skill_id for t in tasks} == {skill.id}


def test_topic_session_without_skill_uses_shared_lookup(client, db_session, sample_user):
    skill = add_skill(db_session, "Fallback Topic", ["fb"])
    session, question = make_session(db_session, sample_user, "FB", None)

    events = answer_last_question(client, session, question, grade_reply(9))

    assert events[0]["event"] == "answer_evaluated"
    assert interview_weight(db_session, sample_user, skill) == 10


def test_single_letter_topic_does_not_match_alias_substrings(db_session):
    add_skill(db_session, "C++", ["cpp", "C plus plus"])
    add_skill(db_session, "CSS", ["Cascading Style Sheets"])
    assert find_skill_by_topic(db_session, "C") is None

    exact = add_skill(db_session, "C")
    assert find_skill_by_topic(db_session, "C") == exact


def test_exact_alias_match_ignores_case(db_session):
    skill = add_skill(db_session, "JavaScript", ["JS", "ECMAScript"])
    assert find_skill_by_topic(db_session, "js") == skill
    assert find_skill_by_topic(db_session, "ecmascript") == skill
    assert find_skill_by_topic(db_session, "javascript") == skill


def test_unmatched_topic_is_skipped_and_logged(client, db_session, sample_user):
    add_skill(db_session, "Something Else")
    roadmap = make_roadmap(db_session, sample_user)
    session, question = make_session(db_session, sample_user, "Nonexistent Topic", None)

    with patch("app.modules.skills.service.logger") as skills_logger:
        answer_last_question(client, session, question, grade_reply(2))

    assert "Nonexistent Topic" in str(skills_logger.warning.call_args_list)
    assert db_session.query(RoadmapTask).filter(RoadmapTask.roadmap_id == roadmap.id).count() == 0


def test_alias_shared_by_two_skills_matches_nothing(client, db_session, sample_user):
    add_skill(db_session, "Skill One", ["shared"])
    add_skill(db_session, "Skill Two", ["SHARED"])

    with patch("app.modules.skills.service.logger") as skills_logger:
        assert find_skill_by_topic(db_session, "shared") is None
        warning = str(skills_logger.warning.call_args_list)
        assert "shared" in warning and "Skill One" in warning and "Skill Two" in warning

        session, question = make_session(db_session, sample_user, "Shared", None)
        answer_last_question(client, session, question, grade_reply(9))

    assert db_session.query(StudentSkill).filter(StudentSkill.user_id == sample_user.id).count() == 0


def test_skill_name_wins_over_shared_alias(db_session):
    named = add_skill(db_session, "Python")
    add_skill(db_session, "Other One", ["python"])
    add_skill(db_session, "Other Two", ["python"])
    assert find_skill_by_topic(db_session, "python") == named


def test_new_student_skill_row_starts_at_zero(client, db_session, sample_user):
    skill = add_skill(db_session, "Brand New")
    session, question = make_session(db_session, sample_user, "Brand New", skill.id)
    reply = json.dumps({
        "technical_score": 3, "communication_score": 0, "feedback": "f",
        "mistakes": [], "improvement": "i", "model_answer": "m",
    })

    answer_last_question(client, session, question, reply)

    row = db_session.query(StudentSkill).filter(StudentSkill.user_id == sample_user.id).one()
    assert (row.resume_weight, row.project_weight, row.interview_weight, row.communication_weight) == (0, 0, 0, 0)


def test_project_session_ignores_associated_skill(client, db_session, sample_user):
    session_skill = add_skill(db_session, "Session Skill")
    topic_skill = add_skill(db_session, "Topic Skill")
    project = StudentProject(
        id=uuid.uuid4(), user_id=sample_user.id, title="P", description="d",
        extracted_skills=[], repo_url="https://github.com/student/p", depth_verified=False,
    )
    db_session.add(project)
    db_session.commit()
    session, question = make_session(db_session, sample_user, "Topic Skill", session_skill.id, project.id)

    answer_last_question(client, session, question, grade_reply(9))

    assert interview_weight(db_session, sample_user, topic_skill) == 10
    assert interview_weight(db_session, sample_user, session_skill) is None

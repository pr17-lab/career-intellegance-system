"""
InterviewService (v2.0)
-----------------------
DB-backed sessions, async ML inference with fallback, and
session-state lifecycle management.

Architectural notes:
  - Transaction safety: session + questions committed atomically with rollback on error.
  - Lifecycle: status auto-advances to "completed" when all questions are answered.
  - Performance: list_sessions() uses joinedload to avoid N+1 on question_count.
  - ML pipeline: Groq → Gemini → built-in question bank (graceful degradation).
    All inference runs in-process; no separate ml_service container required.
"""
from __future__ import annotations

import io
import json
import logging
import random
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Callable, Any
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc

from app.core.config import settings
from app.modules.interview.prompts import GRADING_SYSTEM_PROMPT, build_grading_message
from app.models.interview import InterviewSession, InterviewQuestion, SessionStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gemini auth helper
# ---------------------------------------------------------------------------
# Google AI Studio now issues AQ. prefix keys ("Auth Keys"). These must be
# sent via the x-goog-api-key HTTP header — the legacy ?key= query-param
# authentication is NOT supported for the AQ. format (returns 401).
# Do NOT fall back to ?key= for any new Gemini calls.

def _gemini_headers() -> dict:
    """Return the x-goog-api-key auth header for all Gemini native endpoint calls."""
    return {"x-goog-api-key": settings.GEMINI_API_KEY}


# ---------------------------------------------------------------------------
# Built-in question bank — fallback when ML service is unavailable
# ---------------------------------------------------------------------------

FALLBACK_QUESTION_BANK: dict[str, list[dict]] = {
    "CSE": [
        {"topic": "Data Structures", "question": "Explain the difference between a stack and a queue, and provide a real-world software use case for each.", "difficulty": "medium"},
        {"topic": "Databases", "question": "What is the purpose of database normalization, and how does it prevent data anomalies?", "difficulty": "medium"},
        {"topic": "Operating Systems", "question": "Describe the concept of virtual memory and how paging mechanisms work in a modern OS.", "difficulty": "medium"},
        {"topic": "Networking", "question": "Explain the differences between TCP and UDP protocols, highlighting scenarios where one is preferred over the other.", "difficulty": "medium"},
        {"topic": "Software Engineering", "question": "What are the core principles of RESTful API design?", "difficulty": "medium"},
    ],
    "ECE": [
        {"topic": "Digital Logic", "question": "Explain the difference between combinational and sequential logic circuits.", "difficulty": "medium"},
        {"topic": "Signals", "question": "What is the Nyquist-Shannon sampling theorem, and why is it critical in digital signal processing?", "difficulty": "medium"},
        {"topic": "Embedded Systems", "question": "Describe the function of an interrupt in a microcontroller architecture and how it differs from polling.", "difficulty": "medium"},
        {"topic": "Circuits", "question": "Explain the operational principles of an ideal operational amplifier (Op-Amp).", "difficulty": "medium"},
        {"topic": "Communication", "question": "What are the key differences between amplitude modulation (AM) and frequency modulation (FM)?", "difficulty": "medium"},
    ],
    "MECH": [
        {"topic": "Thermodynamics", "question": "State the second law of thermodynamics and explain its implications for heat engine efficiency.", "difficulty": "medium"},
        {"topic": "Fluid Mechanics", "question": "Explain Bernoulli's principle and describe one of its practical engineering applications.", "difficulty": "medium"},
        {"topic": "Materials Science", "question": "Describe the typical stress-strain curve for a ductile material, identifying the yield point and ultimate tensile strength.", "difficulty": "medium"},
        {"topic": "Manufacturing", "question": "What are the primary differences between casting and forging manufacturing processes?", "difficulty": "medium"},
        {"topic": "Mechanics", "question": "Explain the concept of fatigue failure in mechanical components and how it can be mitigated.", "difficulty": "medium"},
    ],
    "default": [
        {"topic": "General Engineering", "question": "Describe a challenging technical problem you encountered and the analytical steps you took to solve it.", "difficulty": "medium"},
        {"topic": "General Engineering", "question": "How do you approach learning a completely new tool or technology required for a project?", "difficulty": "medium"},
        {"topic": "General Engineering", "question": "Explain a complex engineering concept to someone without a technical background.", "difficulty": "medium"},
        {"topic": "General Engineering", "question": "Discuss the importance of version control in collaborative engineering projects.", "difficulty": "medium"},
        {"topic": "General Engineering", "question": "What strategies do you use to ensure the quality and reliability of your technical deliverables?", "difficulty": "medium"},
    ]
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _strip_json_fences(raw: str) -> str:
    """Remove markdown code fences (```json ... ```) wrapping the LLM response."""
    raw = raw.strip()
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline:].strip()
        else:
            raw = raw[3:].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return raw



class GradingResult(BaseModel):
    """The only reply shape we accept from the grading model."""
    model_config = ConfigDict(protected_namespaces=())  # allows the field name model_answer
    technical_score: int = Field(strict=True, ge=0, le=10)
    communication_score: int = Field(strict=True, ge=0, le=10)
    feedback: str
    mistakes: list[str]
    improvement: str
    model_answer: str


def _verdict(technical_score: int) -> str:
    if technical_score >= 8:
        return "Strong"
    if technical_score >= 5:
        return "Adequate"
    return "Weak"


async def _gemini_grade(user_message: str) -> str:
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not configured")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.GEMINI_MODEL}:generateContent"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": GRADING_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {"temperature": 0.3, "response_mime_type": "application/json"},
    }
    async with httpx.AsyncClient(timeout=90.0) as client_http:
        resp = await client_http.post(url, json=payload, headers=_gemini_headers())
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


async def _groq_grade(user_message: str) -> str:
    if not settings.GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY not configured")
    from groq import AsyncGroq
    client = AsyncGroq(api_key=settings.GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "system", "content": GRADING_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


async def _grade_answer(user_message: str) -> Optional[GradingResult]:
    """Gemini first, then Groq once. None means neither gave a valid grade."""
    for name, ask in (("Gemini", _gemini_grade), ("Groq", _groq_grade)):
        try:
            raw = await ask(user_message)
            return GradingResult.model_validate_json(_strip_json_fences(raw))
        except Exception as e:
            logger.warning("%s grading failed: %s", name, e)
    return None


def _clean_text(t: str) -> str:
    """Strip HTML tags, collapse whitespace, and cap at 3000 chars."""
    if not t:
        return ""
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()[:3000]


def _normalize_question_dict(item: dict) -> dict:
    if not isinstance(item, dict):
        return item
    q = item.get("question", "").strip()
    code = item.get("code_snippet", "").strip()
    if code and "```" not in q:
        q = f"{q}\n\n```\n{code}\n```"
    item["question"] = q
    return item

def _normalize_questions_list(questions: list) -> list:
    if not isinstance(questions, list):
        return questions
    return [_normalize_question_dict(item) for item in questions if isinstance(item, dict)]

# ---------------------------------------------------------------------------
# InterviewService
# ---------------------------------------------------------------------------

class InterviewService:
    """Business logic for the Interview module (v2.0 — DB-backed)."""

    # ------------------------------------------------------------------
    # Question generation — async pipeline (3-Tier Fallback)
    # ------------------------------------------------------------------

    async def generate_questions_async(
        self,
        *,
        branch: str,
        semester: int,
        jd_text: str = "",
        resume_context: Optional[str] = None,
        student_skills: Optional[list] = None,
        limit: int = 10,
        on_chunk: Optional[Callable[[str], Any]] = None,
        associated_skill_id: Optional[UUID] = None,
        db: Optional[Session] = None,
    ) -> tuple[list[dict], str]:
        # Topic practice prompt customization
        if associated_skill_id and db:
            from app.models.skill_taxonomy import SkillTaxonomy
            from app.models.student_skill import StudentSkill
            skill = db.query(SkillTaxonomy).filter(SkillTaxonomy.id == associated_skill_id).first()
            if skill:
                skill_name = skill.skill_name
                skill_description = skill.description or "Core technical concepts."
                
                # Fetch student skill level
                current_status = "Missing"
                if student_skills:
                    for ss in student_skills:
                        if ss.skill_id == associated_skill_id:
                            current_status = ss.level or "Missing"
                            break
                
                level_map = {
                    "weak": "Weak",
                    "moderate": "Moderate",
                    "strong": "High Potential",
                    "missing": "Missing"
                }
                current_status_label = level_map.get(current_status.lower(), current_status)
                
                prompt = f"""You are a senior technical interviewer conducting a focused practice
interview with a final-year engineering student. This is a TOPIC-BASED
practice session — the student has chosen to drill one specific skill
area, independent of any job description or project.

TOPIC: {skill_name}
TOPIC DESCRIPTION: {skill_description}
STUDENT'S CURRENT LEVEL ON THIS TOPIC: {current_status_label} (e.g. Weak / High
Potential / Missing — from their profile, so calibrate difficulty
accordingly: start at fundamentals for Missing/Weak, move faster into
depth for High Potential)

YOUR GOAL
Help the student genuinely get sharper at {skill_name} through realistic
interview questions — not to catch them out. This is practice, not a
pass/fail gate, so keep the tone encouraging even when correcting mistakes.

HOW TO CONDUCT THIS INTERVIEW
1. Start with a foundational or commonly-asked question in this topic —
   the kind that comes up early in a real technical round.
2. Listen to the answer, then ask ONE natural follow-up that either:
   - probes deeper into something they mentioned, or
   - gently redirects if they missed the core of the question, or
   - moves to a related sub-topic if they answered well and you want to
     cover more ground.
3. Ask 4-6 questions total across the session, increasing in depth as the
   student demonstrates competence. Do not ask more than one question at a time.
4. Never lecture mid-interview. If an answer is wrong or incomplete, ask a
   clarifying or corrective follow-up rather than immediately explaining
   the right answer — save full explanations for the end-of-session feedback.
5. Keep your own turns short — 1-3 sentences. This is the student's time to talk.

AFTER EACH ANSWER, return a JSON object with this exact structure and
nothing else (no prose outside the JSON):

{{
  "question_asked": "<the question you just asked>",
  "student_answer_summary": "<one-sentence neutral summary of what they said>",
  "score": <integer 0-10>,
  "ai_feedback": "<2-3 sentences, specific to what they got right/wrong>",
  "mistakes": ["<specific gap or error, if any>", "..."],
  "model_answer": "<a concise, correct answer to the question just asked>",
  "next_action": "follow_up" | "next_question" | "end_session"
}}

END OF SESSION
After 4-6 questions, or if the student explicitly asks to stop, set
next_action to "end_session" and include a final summary object instead:

{{
  "session_summary": "<3-4 sentences on overall performance in this topic>",
  "overall_score": <integer 0-10>,
  "strengths": ["...", "..."],
  "gaps_to_work_on": ["...", "..."],
  "recommended_next_topic": "<a related skill worth practicing next, or null>"
}}

TONE
Direct, warm, and specific — like a senior engineer who wants the student
to actually improve, not an examiner trying to trip them up. Avoid generic
praise ("good job!") in favor of specific observations ("you correctly
identified the N+1 query issue, but missed that indexing alone won't fix
it if the join itself is the bottleneck").


Generate exactly {limit} interview questions for this topic as described above. Candidate Branch is {branch} and Semester is {semester}.
"""
            else:
                prompt = f"""You are a senior technical interviewer.
Generate exactly {limit} interview questions tailored to this candidate.

Candidate Branch: {branch}
Candidate Semester: {semester}
"""
        else:
            prompt = f"""You are a senior technical interviewer.
Generate exactly {limit} interview questions tailored to this candidate.

Candidate Branch: {branch}
Candidate Semester: {semester}
"""
        if jd_text and jd_text.strip():
            prompt += f"\nJOB DESCRIPTION:\n{jd_text.strip()}\n"
        if student_skills:
            skills_lines = []
            for ss in student_skills:
                skill_name = ss.skill.skill_name if ss.skill else "Unknown"
                skills_lines.append(f"- {skill_name} (Level: {ss.level or 'unknown'}, Confidence: {ss.confidence_score or 0})")
            skills_str = "\n".join(skills_lines)
            prompt += f"\nCANDIDATE SKILLS PROFILE:\n{skills_str}\n"
        elif resume_context and resume_context.strip() and resume_context.strip() != "None":
            prompt += f"\nCANDIDATE RESUME:\n{resume_context.strip()}\n"

        prompt += """
STRICT RULES:
- Instead of text trivia, force the questions to generate code snippets containing intentional bugs, time complexity traps (O(N^2)), or vulnerability risks.
- The student must evaluate and debug these code snippets to identify the issues.
- If you ask a technical question about a bug, query, or code snippet, you MUST explicitly provide the code block immediately after the question sentence. Use standard Markdown backticks formatting (e.g., ```json\n <query_here>\n```). Never say 'the following code' without writing out the code block.
- Generate technical questions appropriate for the candidate's background.
- If Job Description is provided, heavily base questions on its requirements.
- Probe the skills and projects listed in their CANDIDATE SKILLS PROFILE.
- Difficulty distribution: 40% easy, 40% medium, 20% hard.
- Return ONLY a valid JSON object matching this schema exactly:
{
  "questions": [
    {
      "topic": "string",
      "question": "string (the natural language question, e.g. 'What is the bug here?')",
      "code_snippet": "string (the actual code block, query, or configuration to debug. Keep empty '' if not applicable)",
      "difficulty": "easy|medium|hard",
      "follow_up": "string"
    }
  ]
}
"""

        # Tier 1: Groq
        if settings.GROQ_API_KEY:
            try:
                from groq import AsyncGroq
                client = AsyncGroq(api_key=settings.GROQ_API_KEY)
                if on_chunk:
                    response_stream = await client.chat.completions.create(
                        model=settings.GROQ_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                        max_tokens=4000,
                        response_format={"type": "json_object"},
                        stream=True
                    )
                    full_text = ""
                    async for chunk in response_stream:
                        token = chunk.choices[0].delta.content
                        if token:
                            full_text += token
                            await on_chunk(token)
                    raw = full_text.strip()
                else:
                    response = await client.chat.completions.create(
                        model=settings.GROQ_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                        max_tokens=4000,
                        response_format={"type": "json_object"}
                    )
                    raw = (response.choices[0].message.content or "").strip()
                parsed = json.loads(raw)
                questions = parsed.get("questions", [])
                if isinstance(questions, list) and len(questions) > 0:
                    questions = _normalize_questions_list(questions)
                    return questions[:limit], "groq"
            except Exception as e:
                logger.error(f"Tier 1 (Groq) failed: {type(e).__name__}: {e}")

        # Tier 2: Gemini
        if settings.GEMINI_API_KEY:
            try:
                if on_chunk:
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{settings.GEMINI_MODEL}:streamGenerateContent?alt=sse"
                    )
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.7, 
                            "response_mime_type": "application/json"
                        },
                    }
                    full_text = ""
                    async with httpx.AsyncClient(timeout=90.0) as http_client:
                        async with http_client.stream("POST", url, json=payload, headers=_gemini_headers()) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if line.startswith("data: "):
                                    json_str = line[len("data: "):]
                                    try:
                                        chunk = json.loads(json_str)
                                        token = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                        if token:
                                            full_text += token
                                            await on_chunk(token)
                                    except json.JSONDecodeError:
                                        continue
                    raw = full_text.strip()
                else:
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{settings.GEMINI_MODEL}:generateContent"
                    )
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.7, 
                            "response_mime_type": "application/json"
                        },
                    }
                    async with httpx.AsyncClient(timeout=90.0) as http_client:
                        resp = await http_client.post(url, json=payload, headers=_gemini_headers())
                        resp.raise_for_status()
                        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                
                parsed = json.loads(_strip_json_fences(raw))
                questions = parsed.get("questions", [])
                if isinstance(questions, list) and len(questions) > 0:
                    questions = _normalize_questions_list(questions)
                    return questions[:limit], "gemini"
            except Exception as e:
                logger.error(f"Tier 2 (Gemini) failed: {type(e).__name__}: {e}")

        # Tier 3: Static Fallback
        logger.warning("Tier 1 and 2 failed. Falling back to Tier 3 (Static Fallback).")
        bank = list(FALLBACK_QUESTION_BANK.get(branch.upper(), FALLBACK_QUESTION_BANK["default"]))
        random.shuffle(bank)
        
        return bank[:limit], "static_fallback"

    async def generate_next_question_async(
        self,
        *,
        db: Session,
        session: InterviewSession,
        user_id: uuid.UUID,
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> tuple[dict, str]:
        """
        Dynamically generate the next interview question based on the conversation history.
        """
        # Build conversation history
        history_str = ""
        for i, q in enumerate(session.questions):
            history_str += f"\n--- Question {i+1} ---\nTopic: {q.topic}\nQuestion: {q.question}\n"
            if q.user_answer:
                history_str += f"User Answer: {q.user_answer}\n"
            if q.ai_verdict:
                history_str += f"AI Evaluation: {q.ai_verdict} (Score: {q.ai_score}/10)\n"

        from app.modules.academic.service import AcademicService
        _academic_svc = AcademicService()
        profile = _academic_svc.get_student_profile(db, user_id)
        semester = profile.semester if profile else 1

        if session.project_id:
            from app.models.student_project import StudentProject
            project = db.query(StudentProject).filter(StudentProject.id == session.project_id).first()
            if project:
                skills_list = []
                if project.extracted_skills:
                    if isinstance(project.extracted_skills, list):
                        skills_list.extend(project.extracted_skills)
                    elif isinstance(project.extracted_skills, str):
                        try:
                            skills_list.extend(json.loads(project.extracted_skills))
                        except Exception:
                            pass
                if project.tech_stack:
                    skills_list.extend(project.tech_stack)
                
                project_prompt = f"""You are a senior technical interviewer conducting a project deep-dive with
a final-year engineering student, in the style of "walk me through this
project" questions from a real interview. Your goal is to verify genuine
understanding of a project the student has listed on their profile — not
just that the repository exists, but that they can defend the decisions
in it.

PROJECT NAME: {project.title}
PROJECT DESCRIPTION: {project.description or ""}
DETECTED TECH STACK: {project.tech_stack or []}
SKILL TAGS ASSOCIATED WITH THIS PROJECT: {skills_list}

IMPORTANT: Ground every question in the actual project details above.
Do not ask generic role-fit or textbook questions that any student could
answer without having built this specific project. If the provided
context is thin (e.g. a sparse README), it is fine to ask the student to
first describe the project in their own words, then use their description
to inform your follow-ups.

HOW TO CONDUCT THIS INTERVIEW
1. Open with: "Walk me through {project.title} — what does it do and why
   did you build it that way?" and let them describe it in their own words.
2. Based on their description AND the tech stack/structure context, ask
   3-5 follow-ups probing genuine understanding, for example:
   - Why a specific technology/library was chosen over an obvious alternative
     (e.g. "why FastAPI over Django here?")
   - What would break or need to change if a stated constraint changed
     (e.g. "what would you need to change if this had to handle 100x the
     traffic?")
   - A specific implementation detail visible in the stack/structure
     ("how does {{specific_component}} actually work under the hood?")
   - A tradeoff or limitation they may not have considered
     ("what's the biggest weakness in this design as it stands?")
3. If an answer sounds rehearsed, vague, or inconsistent with the stated
   tech stack, probe once more before moving on — this is the core
   anti-gaming mechanism of this interview type, so don't let a vague
   answer pass unchallenged, but stay constructive in tone.
4. Keep your own turns short — 1-3 sentences per turn.

AFTER EACH ANSWER, return a JSON object with this exact structure and
nothing else (no prose outside the JSON):

{{
  "question_asked": "<the question you just asked>",
  "student_answer_summary": "<one-sentence neutral summary of what they said>",
  "depth_signal": "genuine_understanding" | "surface_level" | "inconsistent_with_repo" | "unclear",
  "score": <integer 0-10>,
  "ai_feedback": "<2-3 sentences on the specific answer>",
  "mistakes": ["<specific gap, inconsistency, or vague spot, if any>", "..."],
  "next_action": "follow_up" | "next_question" | "end_session"
}}

END OF SESSION
After 3-5 questions, or if the student asks to stop, set next_action to
"end_session" and return a final summary instead:

{{
  "session_summary": "<3-4 sentences on how well the student demonstrated
  real ownership and understanding of this project>",
  "overall_score": <integer 0-10>,
  "depth_verified_recommendation": true | false,
  "reasoning": "<1-2 sentences justifying the depth_verified_recommendation
  — be conservative; only recommend true if the student clearly demonstrated
  they understand the implementation, not just the concept of the project>",
  "strengths": ["...", "..."],
  "gaps_to_work_on": ["...", "..."]
}}

TONE
Skeptical but fair — like an interviewer who has seen plenty of
AI-generated or copy-pasted projects and knows the difference between
someone who built something and someone who can only describe it. Do not
be hostile; if the student demonstrates real understanding, say so
plainly and move on rather than continuing to probe for gaps that aren't there.
"""
                prompt = project_prompt + f"\n\nReact dynamically and generate the next single follow-up question for this project based on history.\nINTERVIEW CONVERSATION HISTORY SO FAR:\n{history_str}\n"
            else:
                prompt = f"""You are a senior technical interviewer conducting a live conversational code review and technical screen.
React dynamically to the candidate's previous responses and generate the NEXT single interview question.

Candidate Branch: {session.branch}
Candidate Semester: {semester}
"""
                prompt += f"\nINTERVIEW CONVERSATION HISTORY SO FAR:\n{history_str}\n"
        elif session.associated_skill_id:
            from app.models.skill_taxonomy import SkillTaxonomy
            from app.models.student_skill import StudentSkill
            skill = db.query(SkillTaxonomy).filter(SkillTaxonomy.id == session.associated_skill_id).first()
            if skill:
                skill_name = skill.skill_name
                skill_description = skill.description or "Core technical concepts."
                
                # Fetch student skill level
                ss = db.query(StudentSkill).filter(
                    StudentSkill.user_id == user_id,
                    StudentSkill.skill_id == session.associated_skill_id
                ).first()
                current_status = (ss.level or "Missing") if ss else "Missing"
                
                level_map = {
                    "weak": "Weak",
                    "moderate": "Moderate",
                    "strong": "High Potential",
                    "missing": "Missing"
                }
                current_status_label = level_map.get(current_status.lower(), current_status)
                
                topic_prompt = f"""You are a senior technical interviewer conducting a focused practice
interview with a final-year engineering student. This is a TOPIC-BASED
practice session — the student has chosen to drill one specific skill
area, independent of any job description or project.

TOPIC: {skill_name}
TOPIC DESCRIPTION: {skill_description}
STUDENT'S CURRENT LEVEL ON THIS TOPIC: {current_status_label} (e.g. Weak / High
Potential / Missing — from their profile, so calibrate difficulty
accordingly: start at fundamentals for Missing/Weak, move faster into
depth for High Potential)

YOUR GOAL
Help the student genuinely get sharper at {skill_name} through realistic
interview questions — not to catch them out. This is practice, not a
pass/fail gate, so keep the tone encouraging even when correcting mistakes.

HOW TO CONDUCT THIS INTERVIEW
1. Start with a foundational or commonly-asked question in this topic —
   the kind that comes up early in a real technical round.
2. Listen to the answer, then ask ONE natural follow-up that either:
   - probes deeper into something they mentioned, or
   - gently redirects if they missed the core of the question, or
   - moves to a related sub-topic if they answered well and you want to
     cover more ground.
3. Ask 4-6 questions total across the session, increasing in depth as the
   student demonstrates competence. Do not ask more than one question at a time.
4. Never lecture mid-interview. If an answer is wrong or incomplete, ask a
   clarifying or corrective follow-up rather than immediately explaining
   the right answer — save full explanations for the end-of-session feedback.
5. Keep your own turns short — 1-3 sentences. This is the student's time to talk.

AFTER EACH ANSWER, return a JSON object with this exact structure and
nothing else (no prose outside the JSON):

{{
  "question_asked": "<the question you just asked>",
  "student_answer_summary": "<one-sentence neutral summary of what they said>",
  "score": <integer 0-10>,
  "ai_feedback": "<2-3 sentences, specific to what they got right/wrong>",
  "mistakes": ["<specific gap or error, if any>", "..."],
  "model_answer": "<a concise, correct answer to the question just asked>",
  "next_action": "follow_up" | "next_question" | "end_session"
}}

END OF SESSION
After 4-6 questions, or if the student explicitly asks to stop, set
next_action to "end_session" and include a final summary object instead:

{{
  "session_summary": "<3-4 sentences on overall performance in this topic>",
  "overall_score": <integer 0-10>,
  "strengths": ["...", "..."],
  "gaps_to_work_on": ["...", "..."],
  "recommended_next_topic": "<a related skill worth practicing next, or null>"
}}

TONE
Direct, warm, and specific — like a senior engineer who wants the student
to actually improve, not an examiner trying to trip them up. Avoid generic
praise ("good job!") in favor of specific observations ("you correctly
identified the N+1 query issue, but missed that indexing alone won't fix
it if the join itself is the bottleneck").
"""
                prompt = topic_prompt + f"\n\nReact dynamically and generate the next single question for this topic based on history.\nINTERVIEW CONVERSATION HISTORY SO FAR:\n{history_str}\n"
            else:
                prompt = f"""You are a senior technical interviewer conducting a live conversational code review and technical screen.
React dynamically to the candidate's previous responses and generate the NEXT single interview question.

Candidate Branch: {session.branch}
Candidate Semester: {semester}
"""
                prompt += f"\nINTERVIEW CONVERSATION HISTORY SO FAR:\n{history_str}\n"
        else:
            prompt = f"""You are a senior technical interviewer conducting a live conversational code review and technical screen.
React dynamically to the candidate's previous responses and generate the NEXT single interview question.

Candidate Branch: {session.branch}
Candidate Semester: {semester}
"""
            prompt += f"\nINTERVIEW CONVERSATION HISTORY SO FAR:\n{history_str}\n"
        prompt += """
STRICT RULES:
- Generate exactly ONE single follow-up or new technical question.
- Do NOT repeat the previous questions.
- React to the user's previous answer: if they made mistakes or did well, you can probe deeper or transition to a related skill.
- Force technical questions to contain code reviews, debug puzzles, or complexity analysis.
- If you ask a technical question about a bug, query, or code snippet, you MUST explicitly provide the code block immediately after the question sentence. Use standard Markdown backticks formatting (e.g., ```json\n <query_here>\n```). Never say 'the following code' without writing out the code block.
- Return ONLY a valid JSON object matching this schema exactly:
{
  "topic": "string",
  "question": "string",
  "difficulty": "easy|medium|hard",
  "follow_up": "string"
}
"""

        # Tier 1: Groq
        if settings.GROQ_API_KEY:
            try:
                from groq import AsyncGroq
                client = AsyncGroq(api_key=settings.GROQ_API_KEY)
                if on_chunk:
                    response_stream = await client.chat.completions.create(
                        model=settings.GROQ_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                        max_tokens=2000,
                        response_format={"type": "json_object"},
                        stream=True
                    )
                    full_text = ""
                    async for chunk in response_stream:
                        token = chunk.choices[0].delta.content
                        if token:
                            full_text += token
                            await on_chunk(token)
                    raw = full_text.strip()
                else:
                    response = await client.chat.completions.create(
                        model=settings.GROQ_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                        max_tokens=2000,
                        response_format={"type": "json_object"}
                    )
                    raw = (response.choices[0].message.content or "").strip()
                parsed = json.loads(_strip_json_fences(raw))
                parsed = _normalize_question_dict(parsed)
                return parsed, "groq"
            except Exception as e:
                logger.error(f"Dynamic generation Tier 1 (Groq) failed: {e}")

        # Tier 2: Gemini
        if settings.GEMINI_API_KEY:
            try:
                if on_chunk:
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{settings.GEMINI_MODEL}:streamGenerateContent?alt=sse"
                    )
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.7, 
                            "response_mime_type": "application/json"
                        },
                    }
                    full_text = ""
                    async with httpx.AsyncClient(timeout=90.0) as http_client:
                        async with http_client.stream("POST", url, json=payload, headers=_gemini_headers()) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if line.startswith("data: "):
                                    json_str = line[len("data: "):]
                                    try:
                                        chunk = json.loads(json_str)
                                        token = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                        if token:
                                            full_text += token
                                            await on_chunk(token)
                                    except json.JSONDecodeError:
                                        continue
                    raw = full_text.strip()
                else:
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{settings.GEMINI_MODEL}:generateContent"
                    )
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.7, 
                            "response_mime_type": "application/json"
                        },
                    }
                    async with httpx.AsyncClient(timeout=90.0) as http_client:
                        resp = await http_client.post(url, json=payload, headers=_gemini_headers())
                        resp.raise_for_status()
                        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                
                parsed = json.loads(_strip_json_fences(raw))
                parsed = _normalize_question_dict(parsed)
                return parsed, "gemini"
            except Exception as e:
                logger.error(f"Dynamic generation Tier 2 (Gemini) failed: {e}")

        # Tier 3: Static Fallback
        bank = list(FALLBACK_QUESTION_BANK.get(session.branch.upper(), FALLBACK_QUESTION_BANK["default"]))
        random.shuffle(bank)
        return bank[0], "static_fallback"

    async def create_micro_interview_session(
        self,
        db: Session,
        user_id: uuid.UUID,
        skill_id: uuid.UUID,
        roadmap_task_id: Optional[uuid.UUID] = None,
    ) -> InterviewSession:
        """
        Create a shortened interview session (capped strictly at 3 questions)
        focused explicitly on the provided skill_id.
        """
        from app.modules.academic.service import AcademicService
        from app.models.skill_taxonomy import SkillTaxonomy
        from app.models.roadmap import Roadmap, RoadmapTask
        from app.models.interview import InterviewQuestion
        from app.modules.roadmap.service import get_owned_task_or_404

        # A client-supplied task ID must belong to this user (404 otherwise).
        if roadmap_task_id:
            get_owned_task_or_404(db, roadmap_task_id, user_id)

        _academic_svc = AcademicService()
        profile = _academic_svc.get_student_profile(db, user_id)
        branch = profile.department if profile else "CSE"
        
        skill = db.query(SkillTaxonomy).filter(SkillTaxonomy.id == skill_id).first()
        skill_name = skill.skill_name if skill else "Software Engineering"
        
        # Find corresponding roadmap task to associate if not provided
        if not roadmap_task_id:
            roadmap = db.query(Roadmap).filter(Roadmap.user_id == user_id, Roadmap.status == "active").first()
            if roadmap:
                task = db.query(RoadmapTask).filter(
                    RoadmapTask.roadmap_id == roadmap.id,
                    RoadmapTask.skill_id == skill_id,
                    RoadmapTask.phase == "apply"
                ).first()
                if task:
                    roadmap_task_id = task.id
                
        # Specialized prompt for micro-interviews
        prompt = f"""You are a senior technical interviewer.
Conduct an advanced micro-interview focusing exclusively on the tool/skill: {skill_name}.
Bypass general icebreakers and warm-ups completely.
Generate exactly 3 extremely advanced code-review or troubleshooting puzzle questions targeting {skill_name}.
Each question must present a code snippet or architectural design containing a subtle bug, time complexity trap, security vulnerability, or concurrency deadlock, and ask the candidate to diagnose, debug, and fix it.
If you ask a technical question about a bug, query, or code snippet, you MUST explicitly provide the code block immediately after the question sentence. Use standard Markdown backticks formatting (e.g., ```json\n <query_here>\n```). Never say 'the following code' without writing out the code block.

STRICT RULE:
Return ONLY a valid JSON object matching this schema exactly:
{{
  "questions": [
    {{
      "topic": "{skill_name}",
      "question": "string (the natural language question, e.g. 'What is the bug here?')",
      "code_snippet": "string (the actual code block, query, or configuration to debug. Keep empty '' if not applicable)",
      "difficulty": "hard",
      "follow_up": "string"
    }}
  ]
}}
"""
        questions = []
        source = "static_fallback"
        
        # Try ML generators using 3-Tier Fallback
        # Tier 1: Groq
        if settings.GROQ_API_KEY:
            try:
                from groq import AsyncGroq
                client = AsyncGroq(api_key=settings.GROQ_API_KEY)
                response = await client.chat.completions.create(
                    model=settings.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=4000,
                    response_format={"type": "json_object"}
                )
                raw = (response.choices[0].message.content or "").strip()
                parsed = json.loads(_strip_json_fences(raw))
                questions = parsed.get("questions", [])
                if isinstance(questions, list) and len(questions) > 0:
                    questions = _normalize_questions_list(questions[:3])
                    source = "groq"
            except Exception as e:
                logger.error(f"Micro-interview Tier 1 (Groq) failed: {e}")

        # Tier 2: Gemini
        if not questions and settings.GEMINI_API_KEY:
            try:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{settings.GEMINI_MODEL}:generateContent"
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.7, 
                        "response_mime_type": "application/json"
                    },
                }
                async with httpx.AsyncClient(timeout=90.0) as http_client:
                    resp = await http_client.post(url, json=payload, headers=_gemini_headers())
                    resp.raise_for_status()
                    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    parsed = json.loads(_strip_json_fences(raw))
                    questions = parsed.get("questions", [])
                    if isinstance(questions, list) and len(questions) > 0:
                        questions = _normalize_questions_list(questions[:3])
                        source = "gemini"
            except Exception as e:
                logger.error(f"Micro-interview Tier 2 (Gemini) failed: {e}")

        # Tier 3: Static Fallback
        if not questions:
            logger.warning("Micro-interview Tier 1 and 2 failed. Falling back to static.")
            questions = [
                {
                    "topic": skill_name,
                    "question": f"Review this code snippet utilizing {skill_name}. Identify the concurrency leak or resource management error and show how to refactor it safely.",
                    "difficulty": "hard",
                    "follow_up": "What are the edge cases for this fix?"
                },
                {
                    "topic": skill_name,
                    "question": f"Explain the time and memory complexity footprint of {skill_name} under high payload volume. What design patterns resolve O(N^2) bottlenecks here?",
                    "difficulty": "hard",
                    "follow_up": "How does this scale across distributed nodes?"
                },
                {
                    "topic": skill_name,
                    "question": f"Identify a major security exploit (e.g. injection, session hijack) common in default implementations of {skill_name} and describe how to mitigate it.",
                    "difficulty": "hard",
                    "follow_up": "How do you test this vulnerability programmatically?"
                }
            ]
            source = "static_fallback"

        try:
            session = InterviewSession(
                user_id=user_id,
                branch=branch,
                topic=f"Micro-Interview: {skill_name}",
                status=SessionStatus.ACTIVE,
                is_micro=True,
                associated_skill_id=skill_id,
                roadmap_task_id=roadmap_task_id
            )
            db.add(session)
            db.flush()

            for q in questions:
                db.add(InterviewQuestion(
                    session_id=session.id,
                    topic=q.get("topic", skill_name),
                    question=q.get("question", ""),
                    difficulty=q.get("difficulty", "hard"),
                    source=source,
                    follow_up=q.get("follow_up"),
                ))

            db.commit()
            db.refresh(session)
            logger.info(
                "Micro-Interview session %s created: user=%s skill=%s",
                session.id, user_id, skill_name
            )
            return session
        except Exception:
            db.rollback()
            logger.exception("Failed to create micro-interview session — rolled back")
            raise

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def list_sessions(
        self,
        db: Session,
        user_id: uuid.UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[InterviewSession], int]:
        """Return a paginated page of sessions for user_id, newest first."""
        base_query = db.query(InterviewSession).filter(InterviewSession.user_id == user_id)
        total = base_query.count()
        sessions = (
            base_query
            .options(joinedload(InterviewSession.questions))
            .order_by(desc(InterviewSession.created_at))
            .limit(limit)
            .offset(offset)
            .all()
        )
        return sessions, total

    def create_session(
        self,
        db: Session,
        user_id: uuid.UUID,
        branch: str,
        topic: Optional[str],
        questions: list[dict],
    ) -> InterviewSession:
        """Atomically persist a new session + all its questions."""
        try:
            session = InterviewSession(
                user_id=user_id,
                branch=branch,
                topic=topic,
                status=SessionStatus.ACTIVE,
            )
            db.add(session)
            db.flush()

            for q in questions:
                db.add(InterviewQuestion(
                    session_id=session.id,
                    topic=q.get("topic", "General"),
                    question=q.get("question", ""),
                    difficulty=q.get("difficulty", "medium"),
                    source=q.get("source"),
                    follow_up=q.get("follow_up"),
                ))

            db.commit()
            db.refresh(session)
            logger.info(
                "Session %s created: user=%s branch=%s questions=%d",
                session.id, user_id, branch, len(session.questions),
            )
            return session
        except Exception:
            db.rollback()
            logger.exception("Failed to create interview session — rolled back")
            raise

    def get_session(
        self,
        db: Session,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Optional[InterviewSession]:
        """Return a session owned by user_id, eagerly loading its questions."""
        return (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == session_id,
                InterviewSession.user_id == user_id,
            )
            .options(joinedload(InterviewSession.questions))
            .first()
        )

    def delete_session(
        self,
        db: Session,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        """Delete a session and all its questions. Returns False if not found."""
        session = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.id == session_id,
                InterviewSession.user_id == user_id,
            )
            .first()
        )
        if not session:
            return False
        db.delete(session)
        db.commit()
        logger.info("Session %s deleted by user %s", session_id, user_id)
        return True

    # ------------------------------------------------------------------
    # AI answer evaluation
    # ------------------------------------------------------------------

    def _grading_message(self, db: Session, question: InterviewQuestion, user_answer: str, user_id: uuid.UUID) -> str:
        session = question.session
        mode, details, level = "general", "", ""

        if session.project_id:
            from app.models.student_project import StudentProject
            project = db.query(StudentProject).filter(StudentProject.id == session.project_id).first()
            if project:
                mode = "project"
                details = (
                    f"Project: {project.title}\nDescription: {project.description or ''}\n"
                    f"Tech stack: {project.tech_stack or []}\nSkills: {project.extracted_skills or []}"
                )
        elif session.associated_skill_id:
            from app.models.skill_taxonomy import SkillTaxonomy
            from app.models.student_skill import StudentSkill
            skill = db.query(SkillTaxonomy).filter(SkillTaxonomy.id == session.associated_skill_id).first()
            if skill:
                ss = db.query(StudentSkill).filter(
                    StudentSkill.user_id == user_id,
                    StudentSkill.skill_id == skill.id,
                ).first()
                mode = "topic"
                details = f"Skill: {skill.skill_name}\nDescription: {skill.description or ''}"
                level = (ss.level if ss else None) or "missing"

        return build_grading_message(mode, details, level, question.question, user_answer)

    async def evaluate_single_answer_async(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        question_id: uuid.UUID,
        user_answer: str,
    ) -> dict:
        """
        Evaluate a single technical question answer using Gemini 1.5 Flash.
        Saves scores and applies weight calibration & score recalculation in real-time.
        """
        question = (
            db.query(InterviewQuestion)
            .join(InterviewSession)
            .filter(
                InterviewQuestion.id == question_id,
                InterviewSession.id == session_id,
                InterviewSession.user_id == user_id,
            )
            .first()
        )
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")

        question.user_answer = user_answer

        reply = await _grade_answer(self._grading_message(db, question, user_answer, user_id))
        if reply is None:
            # Keep the answer but give it no score, so it never counts as graded.
            db.commit()
            return {
                "question_id": str(question.id),
                "technical_score": None,
                "communication_score": None,
                "verdict": None,
                "feedback": "We couldn't evaluate this answer. It was saved without a score.",
                "confidence_score": None,
                "level": None,
            }

        question.ai_score = reply.technical_score
        question.ai_verdict = _verdict(reply.technical_score)
        question.ai_feedback = reply.feedback
        question.model_answer = reply.model_answer
        question.mistakes = reply.mistakes
        question.improvement = reply.improvement
        question.evaluated_at = datetime.now(timezone.utc)

        from app.models.student_skill import StudentSkill
        from app.models.skill_taxonomy import SkillTaxonomy
        from app.modules.skills.engine import calculate_composite_score
        from app.modules.skills.service import find_skill_by_topic
        from app.utils.academic import score_to_level

        topic = question.topic
        tech_score = reply.technical_score
        comm_score = reply.communication_score

        session = question.session
        if session.associated_skill_id and not session.project_id:
            tax = db.query(SkillTaxonomy).filter(SkillTaxonomy.id == session.associated_skill_id).first()
        else:
            tax = find_skill_by_topic(db, topic)

        new_conf = 0.0
        new_level = "weak"
        if tax:
            ss = (
                db.query(StudentSkill)
                .filter(
                    StudentSkill.user_id == user_id,
                    StudentSkill.skill_id == tax.id,
                )
                .first()
            )
            if not ss:
                ss = StudentSkill(
                    user_id=user_id,
                    skill_id=tax.id,
                    resume_weight=0.0,
                    project_weight=0.0,
                    interview_weight=0.0,
                    communication_weight=0.0,
                )
                db.add(ss)
                db.flush()

            current_int = float(ss.interview_weight) if ss.interview_weight else 0.0

            if tech_score >= 8:
                ss.interview_weight = min(current_int + 10.0, 100.0)
            elif tech_score <= 4:
                ss.interview_weight = max(current_int - 10.0, 0.0)

            ss.communication_weight = float(comm_score) * 10.0
            ss.is_interview_scored = True

            # Ensure "interview" is in the source list
            is_sqlite = db.bind.dialect.name == "sqlite"
            src_list = list(ss.source) if ss.source else []
            if "interview" not in src_list:
                src_list.append("interview")
                if not is_sqlite:
                    ss.source = src_list

            res_wt = float(ss.resume_weight) if ss.resume_weight else 0.0
            pr_wt = float(ss.project_weight) if ss.project_weight else 0.0
            in_wt = float(ss.interview_weight) if ss.interview_weight else 0.0
            comm_wt = float(ss.communication_weight) if ss.communication_weight else 0.0

            new_conf = calculate_composite_score(res_wt, pr_wt, in_wt, comm_wt, is_interview_scored=True)
            ss.confidence_score = new_conf
            ss.level = score_to_level(new_conf)
            new_level = ss.level

        db.commit()
        db.refresh(question)

        # Hook WebSocket Calibration into Roadmap Progress for micro-interviews
        if session and session.is_micro:
            all_questions = session.questions
            all_evaluated = all(q.ai_score is not None for q in all_questions)
            if all_evaluated:
                scores = [int(q.ai_score) for q in all_questions]
                avg_score = sum(scores) / len(scores) if scores else 0.0
                if avg_score >= 7.0:
                    from app.models.roadmap import RoadmapTask
                    task = None
                    if session.roadmap_task_id:
                        # The stored ID is not trusted: it must belong to this user.
                        from app.modules.roadmap.service import find_owned_task
                        task = find_owned_task(db, session.roadmap_task_id, user_id)
                        if task is None:
                            logger.warning(
                                "Skipping roadmap completion: session %s references task %s "
                                "which is not owned by user %s",
                                session.id, session.roadmap_task_id, user_id,
                            )
                    else:
                        from app.models.roadmap import Roadmap
                        roadmap = db.query(Roadmap).filter(Roadmap.user_id == user_id, Roadmap.status == "active").first()
                        if roadmap and session.associated_skill_id:
                            task = db.query(RoadmapTask).filter(
                                RoadmapTask.roadmap_id == roadmap.id,
                                RoadmapTask.skill_id == session.associated_skill_id,
                                RoadmapTask.phase == "practice",
                                RoadmapTask.status != "completed"
                            ).first()
                            if not task:
                                task = db.query(RoadmapTask).filter(
                                    RoadmapTask.roadmap_id == roadmap.id,
                                    RoadmapTask.skill_id == session.associated_skill_id,
                                    RoadmapTask.phase == "apply",
                                    RoadmapTask.status != "completed"
                                ).first()
                    
                    if task:
                        task.status = "completed"
                        task.validation_status = "verified"
                        task.completed_at = datetime.now(timezone.utc)
                        
                        rm = task.roadmap
                        if rm:
                            rm.completed_tasks = (rm.completed_tasks or 0) + 1
                            if rm.completed_tasks >= rm.total_tasks:
                                rm.status = "completed"
                        
                        # Calibrate skill on task completion
                        from app.modules.roadmap.service import _update_skill_on_task_completion
                        _update_skill_on_task_completion(db, user_id, task.skill_id)
                        
                        db.commit()
                        logger.info("Micro-interview successful! RoadmapTask %s completed and verified", task.id)

                if session.project_id:
                    from app.models.student_project import StudentProject
                    project = db.query(StudentProject).filter(StudentProject.id == session.project_id).first()
                    if project:
                        # Every question is scored here (all_evaluated above).
                        is_verified = avg_score >= 7.0

                        if is_verified:
                            project.depth_verified = True
                            project.depth_verified_at = datetime.now(timezone.utc)
                        else:
                            project.depth_verified = False
                            project.depth_verified_at = None
                        db.commit()
                        logger.info("Project %s marked as depth verified: %s (avg_score=%s)", project.id, project.depth_verified, avg_score)

                        # Update interview_weight in student_skills for the skill tags associated with that project
                        skills_list = []
                        if project.extracted_skills:
                            if isinstance(project.extracted_skills, list):
                                skills_list.extend(project.extracted_skills)
                            elif isinstance(project.extracted_skills, str):
                                try:
                                    skills_list.extend(json.loads(project.extracted_skills))
                                except Exception:
                                    pass
                        if project.tech_stack:
                            skills_list.extend(project.tech_stack)

                        if skills_list:
                            for sname in set(skills_list):
                                is_sqlite = db.bind.dialect.name == "sqlite"
                                tax = find_skill_by_topic(db, sname)

                                if tax:
                                    ss = db.query(StudentSkill).filter(
                                        StudentSkill.user_id == user_id,
                                        StudentSkill.skill_id == tax.id
                                    ).first()
                                    if not ss:
                                        ss = StudentSkill(
                                            user_id=user_id,
                                            skill_id=tax.id,
                                            resume_weight=0.0,
                                            project_weight=50.0,  # default
                                            interview_weight=0.0,
                                            communication_weight=0.0
                                        )
                                        db.add(ss)
                                        db.flush()

                                    current_int = float(ss.interview_weight) if ss.interview_weight else 0.0
                                    # Use average score of the session for calibration
                                    if avg_score >= 8.0:
                                        ss.interview_weight = min(current_int + 10.0, 100.0)
                                    elif avg_score <= 4.0:
                                        ss.interview_weight = max(current_int - 10.0, 0.0)

                                    ss.communication_weight = float(comm_score) * 10.0
                                    ss.is_interview_scored = True

                                    # Update source to include "interview"
                                    src_list = list(ss.source) if ss.source else []
                                    if "interview" not in src_list:
                                        src_list.append("interview")
                                        if not is_sqlite:
                                            ss.source = src_list

                                    res_wt = float(ss.resume_weight) if ss.resume_weight else 0.0
                                    pr_wt = float(ss.project_weight) if ss.project_weight else 0.0
                                    in_wt = float(ss.interview_weight) if ss.interview_weight else 0.0
                                    comm_wt = float(ss.communication_weight) if ss.communication_weight else 0.0

                                    new_conf = calculate_composite_score(res_wt, pr_wt, in_wt, comm_wt, is_interview_scored=True)
                                    ss.confidence_score = new_conf
                                    ss.level = score_to_level(new_conf)

                            db.commit()

        return {
            "question_id": str(question.id),
            "technical_score": tech_score,
            "communication_score": comm_score,
            "verdict": question.ai_verdict,
            "feedback": question.ai_feedback,
            "confidence_score": new_conf,
            "level": new_level,
        }

    async def create_practice_project_session(
        self,
        db: Session,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        limit: int = 3,
    ) -> InterviewSession:
        """
        Create a project-based depth-verification interview session.
        Grounded in the repository files, README, tech stack, and extracted skills.
        """
        import base64
        import json
        import httpx
        from app.models.student_project import StudentProject
        from app.models.skill_taxonomy import SkillTaxonomy
        from app.models.interview import InterviewQuestion
        from app.modules.academic.service import AcademicService
        
        project = db.query(StudentProject).filter(
            StudentProject.id == project_id,
            StudentProject.user_id == user_id
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        # Query student profile for branch/semester details
        _academic_svc = AcademicService()
        profile = _academic_svc.get_student_profile(db, user_id)
        branch = profile.department if profile else "CSE"

        # Initialize defaults
        folder_names = "Unavailable"
        readme_text = f"Title: {project.title}. Description: {project.description or ''}"
        
        # Try fetching live repo data from GitHub
        if project.repo_url:
            try:
                # Parse owner and repo from URL: https://github.com/owner/repo
                parts = project.repo_url.rstrip("/").split("/")
                if len(parts) >= 2:
                    owner, repo = parts[-2], parts[-1]
                    headers = {
                        "User-Agent": "SATA-Career-Intelligence-Platform",
                        "Accept": "application/vnd.github.v3+json",
                    }
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        # 1. Fetch file structure
                        contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
                        resp_contents = await client.get(contents_url, headers=headers)
                        if resp_contents.status_code == 200:
                            contents = resp_contents.json()
                            if isinstance(contents, list):
                                folder_names = ", ".join([item.get("name", "") for item in contents])

                        # 2. Fetch README
                        readme_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
                        resp_readme = await client.get(readme_url, headers=headers)
                        if resp_readme.status_code == 200:
                            readme_data = resp_readme.json()
                            b64_content = readme_data.get("content", "").replace("\n", "").replace(" ", "")
                            if b64_content:
                                readme_bytes = base64.b64decode(b64_content)
                                readme_text = readme_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                logger.warning(f"Failed to query live GitHub details for project {project_id}: {e}")

        # Try to find a taxonomy skill from the project's extracted skills to set as associated_skill_id
        associated_skill_id = None
        skills_list = []
        if project.extracted_skills:
            if isinstance(project.extracted_skills, list):
                skills_list.extend(project.extracted_skills)
            elif isinstance(project.extracted_skills, str):
                try:
                    skills_list.extend(json.loads(project.extracted_skills))
                except Exception:
                    pass
        if project.tech_stack:
            skills_list.extend(project.tech_stack)

        if skills_list:
            import sqlalchemy as sa
            # Query the first matching taxonomy skill
            for sname in skills_list:
                tax = db.query(SkillTaxonomy).filter(sa.func.lower(SkillTaxonomy.skill_name) == sname.lower()).first()
                if tax:
                    associated_skill_id = tax.id
                    break

        project_prompt = f"""You are a senior technical interviewer conducting a project deep-dive with
a final-year engineering student, in the style of "walk me through this
project" questions from a real interview. Your goal is to verify genuine
understanding of a project the student has listed on their profile — not
just that the repository exists, but that they can defend the decisions
in it.

PROJECT NAME: {project.title}
PROJECT DESCRIPTION (from README): {readme_text[:3000]}
DETECTED TECH STACK: {project.tech_stack or []}
SKILL TAGS ASSOCIATED WITH THIS PROJECT: {skills_list}
REPO STRUCTURE NOTES (if available): {folder_names}

IMPORTANT: Ground every question in the actual project details above.
Do not ask generic role-fit or textbook questions that any student could
answer without having built this specific project. If the provided
context is thin (e.g. a sparse README), it is fine to ask the student to
first describe the project in their own words, then use their description
to inform your follow-ups.

HOW TO CONDUCT THIS INTERVIEW
1. Open with: "Walk me through {project.title} — what does it do and why
   did you build it that way?" and let them describe it in their own words.
2. Based on their description AND the tech stack/structure context, ask
   3-5 follow-ups probing genuine understanding, for example:
   - Why a specific technology/library was chosen over an obvious alternative
     (e.g. "why FastAPI over Django here?")
   - What would break or need to change if a stated constraint changed
     (e.g. "what would you need to change if this had to handle 100x the
     traffic?")
   - A specific implementation detail visible in the stack/structure
     ("how does {{specific_component}} actually work under the hood?")
   - A tradeoff or limitation they may not have considered
     ("what's the biggest weakness in this design as it stands?")
3. If an answer sounds rehearsed, vague, or inconsistent with the stated
   tech stack, probe once more before moving on — this is the core
   anti-gaming mechanism of this interview type, so don't let a vague
   answer pass unchallenged, but stay constructive in tone.
4. Keep your own turns short — 1-3 sentences per turn.

TONE
Skeptical but fair — like an interviewer who has seen plenty of
AI-generated or copy-pasted projects and knows the difference between
someone who built something and someone who can only describe it. Do not
be hostile; if the student demonstrates real understanding, say so
plainly and move on rather than continuing to probe for gaps that aren't there.
"""

        prompt = project_prompt + f"""
Generate exactly 3 extremely specific, advanced technical questions about the actual implementation, tradeoffs, and architectural decisions made in this repository.
Return ONLY a valid JSON object matching this schema exactly:
{{
  "questions": [
    {{
      "topic": "Project: {project.title}",
      "question": "string (the specific question asking about an implementation detail or tradeoff)",
      "difficulty": "hard",
      "follow_up": "string"
    }}
  ]
}}
"""

        questions = []
        source = "static_fallback"

        # Try Groq (Tier 1)
        if settings.GROQ_API_KEY:
            try:
                from groq import AsyncGroq
                client = AsyncGroq(api_key=settings.GROQ_API_KEY)
                response = await client.chat.completions.create(
                    model=settings.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=4000,
                    response_format={"type": "json_object"}
                )
                raw = (response.choices[0].message.content or "").strip()
                parsed = json.loads(_strip_json_fences(raw))
                questions = parsed.get("questions", [])
                if isinstance(questions, list) and len(questions) > 0:
                    questions = _normalize_questions_list(questions[:3])
                    source = "groq"
            except Exception as e:
                logger.error(f"Project depth-verification Groq failed: {e}")

        # Try Gemini (Tier 2)
        if not questions and settings.GEMINI_API_KEY:
            try:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{settings.GEMINI_MODEL}:generateContent"
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.7, 
                        "response_mime_type": "application/json"
                    },
                }
                async with httpx.AsyncClient(timeout=90.0) as http_client:
                    resp = await http_client.post(url, json=payload, headers=_gemini_headers())
                    resp.raise_for_status()
                    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    parsed = json.loads(_strip_json_fences(raw))
                    questions = parsed.get("questions", [])
                    if isinstance(questions, list) and len(questions) > 0:
                        questions = _normalize_questions_list(questions[:3])
                        source = "gemini"
            except Exception as e:
                logger.error(f"Project depth-verification Gemini failed: {e}")

        # Static Fallback (Tier 3)
        if not questions:
            questions = [
                {
                    "topic": f"Project: {project.title}",
                    "question": f"Walk me through the architecture of {project.title}. Why did you choose this folder structure and stack?",
                    "difficulty": "hard",
                    "follow_up": "How does this scale?"
                },
                {
                    "topic": f"Project: {project.title}",
                    "question": f"What was the single most difficult technical challenge you faced when building {project.title}, and how did you resolve it?",
                    "difficulty": "hard",
                    "follow_up": "What is the complexity tradeoff?"
                },
                {
                    "topic": f"Project: {project.title}",
                    "question": f"If you had to scale this repository's backend to handle 100x concurrent active users, what component would break first and how would you refactor it?",
                    "difficulty": "hard",
                    "follow_up": "Explain the caching strategy."
                }
            ]
            source = "static_fallback"

        try:
            session = InterviewSession(
                user_id=user_id,
                branch=branch,
                topic=f"Project Depth Screen: {project.title}",
                status=SessionStatus.ACTIVE,
                is_micro=True,
                associated_skill_id=associated_skill_id,
                project_id=project.id
            )
            db.add(session)
            db.flush()

            for q in questions:
                db.add(InterviewQuestion(
                    session_id=session.id,
                    topic=q.get("topic", f"Project: {project.title}"),
                    question=q.get("question", ""),
                    difficulty=q.get("difficulty", "hard"),
                    source=source,
                    follow_up=q.get("follow_up"),
                ))

            db.commit()
            db.refresh(session)
            return session
        except Exception:
            db.rollback()
            raise


# ---------------------------------------------------------------------------
# Module-level singleton (imported by router)
# ---------------------------------------------------------------------------

interview_service = InterviewService()


async def create_micro_interview_session(user_id: uuid.UUID, skill_id: uuid.UUID, db: Session, roadmap_task_id: Optional[uuid.UUID] = None) -> InterviewSession:
    """Module-level initializer for micro-interview sessions."""
    return await interview_service.create_micro_interview_session(db, user_id, skill_id, roadmap_task_id)


# ---------------------------------------------------------------------------
# Post-evaluation helpers — zero LLM calls
# ---------------------------------------------------------------------------

def extract_weak_skills(
    questions: list,
    score_threshold: int = 5,
    top_n: int = 5,
) -> list[str]:
    """
    Derive weak skill topics purely from scored interview questions.

    Rules:
    - Only consider questions where ai_score is set and ai_score < score_threshold.
    - Count topic frequency with Counter.
    - Return the top_n most frequent topics (deduped, ordered by frequency desc).
    - Returns [] when no questions qualify (no LLM needed).
    """
    weak_topics = [
        q.topic
        for q in questions
        if q.ai_score is not None and int(q.ai_score) < score_threshold and q.topic
    ]
    if not weak_topics:
        return []
    counts = Counter(weak_topics)
    return [topic for topic, _ in counts.most_common(top_n)]


def build_skill_practice_context(db: Session, skill_id: uuid.UUID) -> str:
    """
    Builds session context from skill_taxonomy (skill name, description, common interview angles).
    Shared between roadmap-triggered and direct topic practice interviews.
    """
    from app.models.skill_taxonomy import SkillTaxonomy
    skill = db.query(SkillTaxonomy).filter(SkillTaxonomy.id == skill_id).first()
    if not skill:
        return ""
    return (
        f"Practice interview for skill: {skill.skill_name}\n"
        f"Description: {skill.description or ''}\n"
        f"Focus areas: Core concepts, performance tuning, common design patterns, and debugging scenarios for {skill.skill_name}."
    )

"""
Authentication API routes: login, registration, and current-user lookup.
"""

import math
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import verify_password, create_access_token, get_password_hash, validate_password_strength
from app.models.user import User
from app.models.student_profile import StudentProfile
from app.api.dependencies.database import get_db
from app.api.dependencies.auth import get_current_user
from app.schemas.auth import Token, UserResponse, StudentRegistration, UpdateProfileRequest, ChangePasswordRequest

router = APIRouter(tags=["Authentication"])


# ---------------------------------------------------------------------------
# Background task: compute skills + gaps after registration
# ---------------------------------------------------------------------------

def _run_skill_extraction(user_id: str) -> None:
    """Called as a BackgroundTask after successful student registration."""
    from app.core.database import SessionLocal
    from app.modules.skills.engine import compute_skills_for_student, compute_gaps_for_student

    # Use contextmanager pattern to ensure the session is always closed,
    # mirroring what FastAPI's Depends(get_db) does — avoids holding open
    # connections and keeps the session scoped to this single background job.
    with SessionLocal() as db:
        try:
            compute_skills_for_student(db, user_id)
            compute_gaps_for_student(db, user_id)
            db.commit()
        except Exception as e:
            db.rollback()
            # Non-fatal — skills can be recomputed later
            import logging
            logging.getLogger(__name__).warning("Skill extraction failed for %s: %s", user_id, e)



# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@router.post("/register")
async def register_student(
    payload: StudentRegistration,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):

    # --- Validation ---
    is_valid, msg = validate_password_strength(payload.password)
    if not is_valid:
        raise HTTPException(status_code=422, detail=msg)
    if payload.department not in ["CSE", "ECE", "AIML", "MECH", "AI&ML"]:
        raise HTTPException(status_code=422, detail="Invalid department")
    if not 1 <= payload.current_semester <= 8:
        raise HTTPException(status_code=422, detail="Semester must be between 1 and 8")

    # --- Uniqueness checks ---
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=422, detail="An account with this email already exists")

    if payload.student_id and payload.student_id.strip():
        safe_student_id = payload.student_id.strip().upper()
        if db.query(User).filter(User.student_id == safe_student_id).first():
            raise HTTPException(
                status_code=422,
                detail="This Student ID is already taken. Try adding your batch year e.g. S2024001",
            )
    else:
        last_user = db.query(User).filter(User.student_id.like(f"S{payload.batch_year}%")).order_by(User.student_id.desc()).first()
        if last_user and last_user.student_id:
            try:
                # Extract the last 3 digits
                last_num = int(last_user.student_id[-3:])
                safe_student_id = f"S{payload.batch_year}{last_num + 1:03d}"
            except ValueError:
                safe_student_id = f"S{payload.batch_year}001"
        else:
            safe_student_id = f"S{payload.batch_year}001"

    # --- Create user ---
    new_user = User(
        email=payload.email,
        student_id=safe_student_id,
        password_hash=get_password_hash(payload.password),
        is_active=True,
    )
    db.add(new_user)
    db.flush()

    new_profile = StudentProfile(
        user_id=new_user.id,
        name=payload.full_name,
        department=payload.department,
        batch_year=payload.batch_year,
        semester=payload.current_semester,
        performance_status="Average",
        backlog_count=0,
        active_backlog=False,
    )
    db.add(new_profile)
    db.flush()

    db.commit()
    db.refresh(new_profile)

    background_tasks.add_task(_run_skill_extraction, str(new_user.id))

    return {
        "message": "Registration successful",
        "student_id": safe_student_id,
        "user_id": str(new_user.id),
        "cgpa": None,
        "semesters_recorded": 0,
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """
    Authenticate with student_id + password and return a JWT bearer token.
    Rate-limited to 5 requests per minute per IP.
    """
    safe_username = form_data.username.strip().upper()
    user = db.query(User).filter(User.student_id == safe_username).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect student ID or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.is_locked():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is locked due to too many failed login attempts. Please try again later.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Please contact support.",
        )

    if not (user.password_hash and verify_password(form_data.password, user.password_hash)):
        user.record_failed_login()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect student ID or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user.reset_failed_attempts()
    db.commit()

    access_token = create_access_token(
        data={"sub": user.email, "user_id": str(user.id), "student_id": user.student_id},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    
    return {"message": "Login successful"}


@router.post("/logout")
async def logout(response: Response):
    """
    Clear the HttpOnly cookie to logout the user.
    """
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return {"message": "Logout successful"}


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the currently authenticated user with profile information."""
    profile = db.query(StudentProfile).filter(
        StudentProfile.user_id == current_user.id
    ).first()

    return {
        "id": current_user.id,
        "email": current_user.email,
        "student_id": current_user.student_id,
        "name": profile.name if profile else None,
        "branch": profile.department if profile else None,
        "semester": profile.semester if profile else None,
    }


# ---------------------------------------------------------------------------
# Update own profile (name / semester)
# ---------------------------------------------------------------------------

@router.patch("/me", response_model=UserResponse)
async def update_my_profile(
    payload: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Allow the authenticated student to update their own name and/or semester.
    Returns the refreshed UserResponse so the frontend can update its auth context.
    """
    profile = db.query(StudentProfile).filter(
        StudentProfile.user_id == current_user.id
    ).first()

    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")

    if payload.name is not None:
        profile.name = payload.name.strip()
    if payload.semester is not None:
        profile.semester = payload.semester

    db.commit()
    db.refresh(profile)

    return {
        "id": current_user.id,
        "email": current_user.email,
        "student_id": current_user.student_id,
        "name": profile.name,
        "branch": profile.department,
        "semester": profile.semester,
    }


# ---------------------------------------------------------------------------
# Change own password
# ---------------------------------------------------------------------------

@router.put("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Allow the authenticated student to change their password.
    Verifies the current password before applying the update.
    """
    from app.core.security import verify_password, get_password_hash, validate_password_strength

    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    is_valid, msg = validate_password_strength(payload.new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=msg,
        )

    current_user.password_hash = get_password_hash(payload.new_password)
    current_user.reset_failed_attempts()
    db.commit()

    return {"message": "Password updated successfully"}

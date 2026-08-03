from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth
import social_db as sdb
from email_sender import send_email

RESET_TOKEN_LIFETIME = timedelta(hours=1)

router = APIRouter(prefix="/werun")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PROVINCES = [
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
    "Mpumalanga", "North West", "Northern Cape", "Western Cape",
]


def _ctx(request: Request, **extra) -> dict:
    return {"request": request, "current_user": auth.get_current_user(request), **extra}


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/werun/login", status_code=303)


@router.get("/health")
def health():
    try:
        sdb.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Postgres unreachable: {exc}")
    return JSONResponse({"status": "ok"})


# -- signup / login / logout --------------------------------------------

@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(
        "werun/signup.html", _ctx(request, active_tab="signup", error=None)
    )


@router.post("/signup")
def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
):
    email = email.strip().lower()
    if not email or not password or not display_name.strip():
        return templates.TemplateResponse(
            "werun/signup.html",
            _ctx(request, active_tab="signup", error="All fields are required."),
            status_code=400,
        )
    if sdb.get_user_by_email(email) is not None:
        return templates.TemplateResponse(
            "werun/signup.html",
            _ctx(request, active_tab="signup", error="An account with that email already exists."),
            status_code=400,
        )
    password_hash = auth.hash_password(password)
    user_id = sdb.create_user(email=email, password_hash=password_hash, display_name=display_name.strip())
    auth.log_in_user(request, user_id)
    return RedirectResponse("/werun/profile", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        "werun/login.html", _ctx(request, active_tab="login", error=None)
    )


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = sdb.get_user_by_email(email)
    if user is None or not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "werun/login.html",
            _ctx(request, active_tab="login", error="Invalid email or password."),
            status_code=400,
        )
    auth.log_in_user(request, user["id"])
    return RedirectResponse("/werun/clubs", status_code=303)


@router.post("/logout")
def logout(request: Request):
    auth.log_out_user(request)
    return RedirectResponse("/", status_code=303)


# -- forgot / reset password ------------------------------------------------

@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request):
    return templates.TemplateResponse(
        "werun/forgot_password.html", _ctx(request, active_tab="login", sent=False)
    )


@router.post("/forgot-password")
def forgot_password_submit(request: Request, email: str = Form(...)):
    user = sdb.get_user_by_email(email.strip().lower())
    if user is not None:
        raw_token, token_hash = auth.generate_reset_token()
        expires_at = datetime.now(timezone.utc) + RESET_TOKEN_LIFETIME
        sdb.create_password_reset_token(user["id"], token_hash, expires_at)
        reset_url = f"{str(request.base_url).rstrip('/')}/werun/reset-password?token={raw_token}"
        send_email(
            to=user["email"],
            subject="Reset your WeRun password",
            html_body=(
                f"<p>Hi {user['display_name']},</p>"
                f"<p>Click the link below to reset your WeRun password. "
                f"This link expires in 1 hour and can only be used once.</p>"
                f'<p><a href="{reset_url}">{reset_url}</a></p>'
                f"<p>If you didn't request this, you can safely ignore this email.</p>"
            ),
        )
    # Same response whether or not the email matched an account -- otherwise
    # this form becomes a way to check which emails have WeRun accounts.
    return templates.TemplateResponse(
        "werun/forgot_password.html", _ctx(request, active_tab="login", sent=True)
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request, token: str = ""):
    valid = bool(token) and sdb.get_valid_reset_token(auth.hash_token(token)) is not None
    return templates.TemplateResponse(
        "werun/reset_password.html",
        _ctx(request, active_tab="login", token=token, valid=valid, error=None),
    )


@router.post("/reset-password")
def reset_password_submit(request: Request, token: str = Form(...), password: str = Form(...)):
    reset_token = sdb.get_valid_reset_token(auth.hash_token(token))
    if reset_token is None:
        return templates.TemplateResponse(
            "werun/reset_password.html",
            _ctx(request, active_tab="login", token=token, valid=False,
                 error="This reset link is invalid or has expired. Request a new one."),
            status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            "werun/reset_password.html",
            _ctx(request, active_tab="login", token=token, valid=True,
                 error="Password must be at least 8 characters."),
            status_code=400,
        )
    new_hash = auth.hash_password(password)
    sdb.consume_reset_token(reset_token["id"], reset_token["user_id"], new_hash)
    return RedirectResponse("/werun/login", status_code=303)


# -- profile --------------------------------------------------------------

@router.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request):
    if auth.get_current_user(request) is None:
        return _login_redirect()
    return templates.TemplateResponse(
        "werun/profile.html", _ctx(request, active_tab="profile", provinces=PROVINCES)
    )


@router.post("/profile")
def profile_submit(
    request: Request,
    display_name: str = Form(...),
    city: str = Form(""),
    province: str = Form(""),
    experience_level: str = Form("beginner"),
    bio: str = Form(""),
):
    user = auth.get_current_user(request)
    if user is None:
        return _login_redirect()
    sdb.update_profile(
        user["id"], display_name.strip(), city.strip() or None, province or None,
        experience_level, bio.strip() or None,
    )
    return RedirectResponse("/werun/profile", status_code=303)


# -- clubs ------------------------------------------------------------------

@router.get("/clubs", response_class=HTMLResponse)
def clubs_list(request: Request, province: str = ""):
    clubs = sdb.list_clubs(province=province or None)
    return templates.TemplateResponse(
        "werun/clubs.html",
        _ctx(request, active_tab="clubs", clubs=clubs, provinces=PROVINCES,
             selected_province=province, error=None),
    )


@router.post("/clubs")
def clubs_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    city: str = Form(""),
    province: str = Form(...),
    meeting_schedule: str = Form(""),
):
    user = auth.get_current_user(request)
    if user is None:
        return _login_redirect()
    if not name.strip() or not province:
        clubs = sdb.list_clubs()
        return templates.TemplateResponse(
            "werun/clubs.html",
            _ctx(request, active_tab="clubs", clubs=clubs, provinces=PROVINCES,
                 selected_province="", error="Club name and province are required."),
            status_code=400,
        )
    club_id = sdb.create_club(
        name=name.strip(), description=description.strip() or None, city=city.strip() or None,
        province=province, meeting_schedule=meeting_schedule.strip() or None, created_by=user["id"],
    )
    return RedirectResponse(f"/werun/clubs/{club_id}", status_code=303)


@router.get("/clubs/{club_id}", response_class=HTMLResponse)
def club_detail(request: Request, club_id: int):
    club = sdb.get_club(club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")
    user = auth.get_current_user(request)
    is_member = sdb.is_club_member(club_id, user["id"]) if user else False
    room_id = sdb.get_club_chat_room_id(club_id)
    return templates.TemplateResponse(
        "werun/club_detail.html",
        _ctx(request, active_tab="clubs", club=club, is_member=is_member, room_id=room_id),
    )


@router.post("/clubs/{club_id}/join")
def club_join(request: Request, club_id: int):
    user = auth.get_current_user(request)
    if user is None:
        return _login_redirect()
    if sdb.get_club(club_id) is None:
        raise HTTPException(status_code=404, detail="Club not found")
    sdb.join_club(club_id, user["id"])
    return RedirectResponse(f"/werun/clubs/{club_id}", status_code=303)


# -- chat (shared by club rooms and pairing rooms) -------------------------

@router.get("/api/rooms/{room_id}/messages")
def room_messages_list(request: Request, room_id: int, since_id: int = 0):
    user = auth.require_user(request)
    if not sdb.user_can_access_room(user["id"], room_id):
        raise HTTPException(status_code=403, detail="Not a member of this chat")
    messages = sdb.list_messages(room_id, since_id=since_id)
    for m in messages:
        m["created_at"] = m["created_at"].isoformat()
    return JSONResponse(messages)


@router.post("/api/rooms/{room_id}/messages")
def room_messages_post(request: Request, room_id: int, body: str = Form(...)):
    user = auth.require_user(request)
    if not sdb.user_can_access_room(user["id"], room_id):
        raise HTTPException(status_code=403, detail="Not a member of this chat")
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Message body cannot be empty")
    if len(body) > 2000:
        raise HTTPException(status_code=400, detail="Message too long")
    message = sdb.add_message(room_id, user["id"], body)
    message["created_at"] = message["created_at"].isoformat()
    return JSONResponse(message)


# -- My First Marathon (mentor pairing) -------------------------------------

@router.get("/first-marathon", response_class=HTMLResponse)
def first_marathon_list(request: Request):
    user = auth.get_current_user(request)
    open_requests = sdb.list_open_pairing_requests()
    my_pairings = sdb.list_my_pairings(user["id"]) if user else []
    return templates.TemplateResponse(
        "werun/first_marathon.html",
        _ctx(request, active_tab="first-marathon", open_requests=open_requests,
             my_pairings=my_pairings, error=None),
    )


@router.post("/first-marathon/request")
def first_marathon_request(
    request: Request, target_race: str = Form(""), note: str = Form("")
):
    user = auth.get_current_user(request)
    if user is None:
        return _login_redirect()
    sdb.create_pairing_request(user["id"], target_race.strip() or None, note.strip() or None)
    return RedirectResponse("/werun/first-marathon", status_code=303)


@router.post("/first-marathon/{pairing_id}/accept")
def first_marathon_accept(request: Request, pairing_id: int):
    user = auth.get_current_user(request)
    if user is None:
        return _login_redirect()
    pairing = sdb.get_pairing(pairing_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if pairing["mentee_id"] == user["id"]:
        raise HTTPException(status_code=400, detail="You can't mentor your own request")
    sdb.accept_pairing(pairing_id, user["id"])  # no-op if already taken; UI just redirects back
    return RedirectResponse("/werun/first-marathon", status_code=303)


@router.get("/pairings/{pairing_id}", response_class=HTMLResponse)
def pairing_chat(request: Request, pairing_id: int):
    user = auth.get_current_user(request)
    if user is None:
        return _login_redirect()
    pairing = sdb.get_pairing(pairing_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="Pairing not found")
    room_id = sdb.get_pairing_chat_room_id(pairing_id)
    if room_id is None or not sdb.user_can_access_room(user["id"], room_id):
        raise HTTPException(status_code=403, detail="Not part of this pairing")
    return templates.TemplateResponse(
        "werun/pairing_chat.html",
        _ctx(request, active_tab="first-marathon", pairing=pairing, room_id=room_id),
    )

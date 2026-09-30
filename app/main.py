import asyncio
import logging
import time

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, llm, storage
from .compiler import CompileError, compile_pdf
from .extractor import ExtractionError, extract_text, missing_lines
from .latex import ValidationError, join_tex, render_resume, repair_json_escapes, split_tex, validate_body
from .prompts import EDIT_SYSTEM, EXTRACT_SYSTEM, edit_user_message

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="Resume Editor")
storage.init()


PUBLIC_API = {"/api/login", "/api/logout"}


@app.middleware("http")
async def require_login(request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API and not auth.token_valid(request.cookies.get(auth.COOKIE)):
        return JSONResponse(status_code=401, content={"detail": "Please sign in"})
    return await call_next(request)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    # Make browsers revalidate the page and its JS/CSS on every load, so an updated
    # frontend is picked up immediately (unchanged files still return a cheap 304).
    response = await call_next(request)
    path = request.url.path
    if path == "/" or (path.startswith("/static/") and not path.startswith("/static/vendor/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    ip = request.client.host if request.client else "?"
    if auth.locked_out(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    if not auth.check_credentials(ip, req.email, req.password):
        await asyncio.sleep(1)  # slow down password guessing
        raise HTTPException(401, "Wrong email or password")
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        auth.COOKIE,
        auth.make_token(),
        max_age=config.LOGIN_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.COOKIE)
    return response


class EditRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    provider: str | None = None


def _require_session(sid: str) -> None:
    if not storage.session_exists(sid):
        raise HTTPException(404, "Session not found")


@app.get("/api/config")
def get_config():
    names = llm.available_providers()
    return {"providers": [{"id": n, "label": llm.LABELS[n]} for n in names], "login": auth.enabled()}


async def _structure_resume(text: str, provider: str | None) -> tuple[dict, str]:
    """Ask the LLM for structured JSON, and re-ask if it silently dropped any content."""
    user = f"RESUME TEXT:\n{text}"
    best, best_missing, used = None, None, None
    for attempt in range(config.MAX_EDIT_RETRIES + 1):
        t = time.monotonic()
        data, name = await llm.complete_json_with_provider(EXTRACT_SYSTEM, user, provider)
        missing = missing_lines(text, data)
        log.info("upload: structure attempt %d via %s took %.1fs, %d lines missing",
                 attempt + 1, name, time.monotonic() - t, len(missing))
        if best is None or len(missing) < len(best_missing):
            best, best_missing, used = data, missing, name
        if not missing:
            break
        user = (
            f"RESUME TEXT:\n{text}\n\nYOUR PREVIOUS ANSWER LEFT OUT THESE LINES. Include every one of "
            "them, in its original section and position, and return the complete JSON again:\n"
            + "\n".join(missing)
        )
    return best, used


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), provider: str | None = None):
    content = await file.read()
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 5 MB")
    try:
        text = extract_text(file.filename or "", content)
        data, used = await _structure_resume(text, provider)
        tex = render_resume(data)
        t = time.monotonic()
        pdf = await compile_pdf(tex)
        log.info("upload: compile took %.1fs", time.monotonic() - t)
    except ExtractionError as e:
        raise HTTPException(400, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    except CompileError as e:
        raise HTTPException(500, f"Could not compile the generated LaTeX: {e}")

    sid = storage.create_session(file.filename or "resume")
    version = storage.add_version(sid, tex, pdf, "Imported from uploaded file")
    return {"session_id": sid, "version": version, "provider": llm.LABELS[used]}


@app.post("/api/sessions/{sid}/edit")
async def edit(sid: str, req: EditRequest):
    _require_session(sid)
    current = storage.get_version(sid)
    head, body, tail = split_tex(current["tex"])

    error = None
    preferred = req.provider
    providers = llm.available_providers()
    for attempt in range(config.MAX_EDIT_RETRIES + 1):
        t = time.monotonic()
        try:
            result, used = await llm.complete_json_with_provider(
                EDIT_SYSTEM, edit_user_message(body, req.message, error), preferred
            )
        except llm.LLMError as e:
            raise HTTPException(502, str(e))
        # If this answer turns out unusable, give the retry to the next provider rather than
        # asking the same (possibly weaker) model again.
        if len(providers) > 1:
            preferred = providers[(providers.index(used) + 1) % len(providers)]

        if result.get("status") == "refused":
            return {"status": "refused", "message": result.get("reason") or "I can only edit your resume."}

        new_body = repair_json_escapes(str(result.get("body") or ""))
        try:
            validate_body(body, new_body)
            new_tex = join_tex(head, new_body, tail)
            pdf = await compile_pdf(new_tex)
        except (ValidationError, CompileError) as e:
            error = str(e)
            log.info("edit: attempt %d via %s failed after %.1fs: %s", attempt + 1, used, time.monotonic() - t, error[:200])
            continue
        log.info("edit: attempt %d via %s succeeded in %.1fs", attempt + 1, used, time.monotonic() - t)

        summary = str(result.get("summary") or "Updated resume")
        version = storage.add_version(sid, new_tex, pdf, summary, req.message)
        return {"status": "ok", "message": summary, "version": version, "provider": llm.LABELS[used]}

    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "message": f"The edit could not be applied safely, so your resume was left unchanged. Last problem: {error}",
        },
    )


@app.get("/api/sessions/{sid}/versions")
def versions(sid: str):
    _require_session(sid)
    return storage.list_versions(sid)


def _version_or_404(sid: str, version: int):
    row = storage.get_version(sid, version)
    if not row:
        raise HTTPException(404, "Version not found")
    return row


@app.get("/api/sessions/{sid}/versions/{version}/pdf")
def version_pdf(sid: str, version: int, download: bool = False):
    row = _version_or_404(sid, version)
    disposition = "attachment" if download else "inline"
    return Response(
        row["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="resume_v{version}.pdf"'},
    )


@app.get("/api/sessions/{sid}/versions/{version}/tex")
def version_tex(sid: str, version: int):
    row = _version_or_404(sid, version)
    return Response(
        row["tex"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="resume_v{version}.tex"'},
    )


@app.post("/api/sessions/{sid}/revert/{version}")
def revert(sid: str, version: int):
    row = _version_or_404(sid, version)
    new_v = storage.add_version(sid, row["tex"], row["pdf"], f"Reverted to version {version}")
    return {"status": "ok", "version": new_v, "message": f"Reverted to version {version}"}


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

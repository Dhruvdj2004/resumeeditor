"""Rendering the template, splitting locked/editable parts, and validating LLM edits."""
import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import TEMPLATE_DIR

BODY_START = "%%BODY_START"
BODY_END = "%%BODY_END"
MARKER_RE = re.compile(r"^%%(BEGIN|END):([A-Z0-9_]+)\s*$", re.M)

_TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEX_ESCAPE_RE = re.compile("|".join(re.escape(k) for k in _TEX_ESCAPES))


def tex_escape(value) -> str:
    return _TEX_ESCAPE_RE.sub(lambda m: _TEX_ESCAPES[m.group()], str(value or ""))


def url_escape(value) -> str:
    # Inside \href the URL is read verbatim-ish; only these break it.
    return str(value or "").replace("\\", "").replace("%", r"\%").replace("#", r"\#").replace("{", "").replace("}", "")


def marker_name(title) -> str:
    name = re.sub(r"[^A-Z0-9]+", "_", str(title or "").upper()).strip("_")
    return name or "SECTION"


_BOLD_MD_RE = re.compile(r"\*\*(.+?)\*\*")


def rich_text(value) -> str:
    """Escape for LaTeX, then turn **bold** markers into \\textbf{...}."""
    escaped = _BOLD_MD_RE.sub(lambda m: r"\textbf{" + m.group(1) + "}", tex_escape(value))
    return escaped.replace("**", "")  # drop any unpaired markers


_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    block_start_string="((*",
    block_end_string="*))",
    variable_start_string="((((",
    variable_end_string="))))",
    comment_start_string="((#",
    comment_end_string="#))",
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    undefined=StrictUndefined,
)
_env.filters.update(tex=tex_escape, url=url_escape, rich=rich_text)

SECTION_TYPES = {"entries", "skills", "bullets", "paragraph"}


def normalize_resume(data: dict) -> dict:
    """Make the LLM's JSON safe for the template: fill defaults, drop junk, dedupe markers."""

    def s(v):
        return str(v).strip() if v is not None else ""

    def plain(v):  # fields rendered without bold support
        return s(v).replace("**", "")

    def lst(v):
        return [s(x) for x in v if s(x)] if isinstance(v, list) else []

    def dicts(v):
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    contact = []
    for c in dicts(data.get("contact")):
        text, url = plain(c.get("text")), s(c.get("url"))
        if text or url:
            contact.append({"text": text or url, "url": url})

    sections, used = [], {"HEADER"}
    for sec in dicts(data.get("sections")):
        title = plain(sec.get("title")) or "Additional"
        kind = s(sec.get("type")).lower()
        out = {"title": title, "type": kind if kind in SECTION_TYPES else "bullets"}
        if out["type"] == "entries":
            entries = [
                {
                    "title": plain(e.get("title")),
                    "date": plain(e.get("date")),
                    "subtitle": plain(e.get("subtitle")),
                    "meta": plain(e.get("meta")),
                    "bullets": lst(e.get("bullets")),
                }
                for e in dicts(sec.get("entries"))
            ]
            out["entries"] = [e for e in entries if any(e.values())]
            filled = bool(out["entries"])
        elif out["type"] == "skills":
            out["rows"] = [
                {"label": plain(r.get("label")), "value": s(r.get("value"))}
                for r in dicts(sec.get("rows"))
                if s(r.get("label")) or s(r.get("value"))
            ]
            filled = bool(out["rows"])
        elif out["type"] == "paragraph":
            out["text"] = s(sec.get("text"))
            filled = bool(out["text"])
        else:
            out["items"] = lst(sec.get("items"))
            filled = bool(out["items"])
        if not filled:
            continue
        base = marker_name(title)
        name, n = base, 2
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        out["marker"] = name
        sections.append(out)

    return {
        "name": plain(data.get("name")) or "Your Name",
        "headline": plain(data.get("headline")),
        "contact": contact,
        "sections": sections,
    }


def render_resume(data: dict) -> str:
    tex = _env.get_template("resume.tex.j2").render(r=normalize_resume(data))
    return re.sub(r"\n{3,}", "\n\n", tex)


# ---------------------------------------------------------------- splitting


class ValidationError(Exception):
    pass


def split_tex(tex: str) -> tuple[str, str, str]:
    """Return (locked_head, editable_body, locked_tail). Markers must sit on their own lines."""
    a = re.search(rf"^{BODY_START}[ \t]*$", tex, re.M)
    b = re.search(rf"^{BODY_END}[ \t]*$", tex, re.M)
    if not a or not b or b.start() < a.end():
        raise ValidationError("Document is missing body markers")
    return tex[: a.end()], tex[a.end() : b.start()].strip("\n"), tex[b.start() :]


_JSON_ESCAPE_DAMAGE = [
    # Models sometimes write "\begin" inside JSON without doubling the backslash, which JSON
    # decodes as a control character: \b -> backspace, \f -> form feed, \t -> tab, \r -> CR.
    (re.compile("\x08"), r"\\b"),
    (re.compile("\x0c"), r"\\f"),
    (re.compile("\t(?=[a-zA-Z])"), r"\\t"),
    (re.compile("\r(?=[a-zA-Z])"), r"\\r"),
]


def repair_json_escapes(body: str) -> str:
    for pattern, replacement in _JSON_ESCAPE_DAMAGE:
        body = pattern.sub(replacement, body)
    return body


def join_tex(head: str, body: str, tail: str) -> str:
    return f"{head}\n{body.strip(chr(10))}\n{tail}"


# ---------------------------------------------------------------- validation

FORBIDDEN = [
    "input", "include", "write", "write18", "immediate", "openout", "openin", "read",
    "def", "edef", "gdef", "xdef", "let", "newcommand", "renewcommand", "providecommand",
    "usepackage", "RequirePackage", "documentclass", "catcode", "csname", "endcsname",
    "makeatletter", "special", "directlua", "ShellEscape", "includegraphics", "verbatiminput",
    "lstinputlisting", "loop", "repeat", "afterassignment", "expandafter",
]
# Harmless formatting commands the model may use even if the body didn't have them yet.
SAFE_EXTRA = {
    "textbf", "textit", "emph", "underline", "href", "small", "footnotesize", "item",
    "section", "resumeItem", "resumeSubheading", "resumeItemListStart", "resumeItemListEnd",
    "resumeSkillsStart", "resumeSkillsEnd", "resumeSkill", "vspace", "textasciitilde", "textasciicircum", "textbackslash",
    "textbar", "ldots", "textendash", "textemdash", "quad", "hfill", "newline", "begin", "end",
}
CMD_RE = re.compile(r"\\([A-Za-z@]+)")
ALLOWED_ENVS = {"center", "itemize", "tabular*", "tabular", "tabularx"}


def _markers(body: str) -> list[tuple[str, str]]:
    return MARKER_RE.findall(body)


def _check_markers(body: str) -> list[str]:
    marks = _markers(body)
    if not marks:
        raise ValidationError("All %%BEGIN/%%END section markers were removed")
    stack, names = [], []
    for kind, name in marks:
        if kind == "BEGIN":
            if stack:
                raise ValidationError(f"Section {name} starts inside section {stack[-1]}")
            stack.append(name)
            names.append(name)
        else:
            if not stack or stack[-1] != name:
                raise ValidationError(f"%%END:{name} has no matching %%BEGIN:{name}")
            stack.pop()
    if stack:
        raise ValidationError(f"Section {stack[-1]} is missing its %%END marker")
    if len(names) != len(set(names)):
        raise ValidationError("A section marker appears more than once")
    if "HEADER" not in names:
        raise ValidationError("The HEADER section was removed")
    return names


def _check_braces(body: str) -> None:
    depth = 0
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            i += 2  # skip escaped char (\{, \}, \\, \%)
            continue
        if c == "%":  # comment until end of line
            nl = body.find("\n", i)
            i = len(body) if nl == -1 else nl
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                raise ValidationError("Unbalanced braces: extra '}'")
        i += 1
    if depth:
        raise ValidationError(f"Unbalanced braces: {depth} unclosed '{{'")


def _check_envs(body: str) -> None:
    stack = []
    for kind, env in re.findall(r"\\(begin|end)\{([^}]*)\}", body):
        if env not in ALLOWED_ENVS:
            raise ValidationError(f"Environment '{env}' is not allowed")
        if kind == "begin":
            stack.append(env)
        elif not stack or stack.pop() != env:
            raise ValidationError(f"\\end{{{env}}} does not match its \\begin")
    if stack:
        raise ValidationError(f"\\begin{{{stack[-1]}}} is never closed")


def validate_body(old_body: str, new_body: str) -> None:
    if not new_body or not new_body.strip():
        raise ValidationError("Returned body is empty")
    if BODY_START in new_body or BODY_END in new_body:
        raise ValidationError("Body must not contain %%BODY_START/%%BODY_END")

    _check_markers(new_body)

    used = set(CMD_RE.findall(new_body))
    bad = used & set(FORBIDDEN)
    if bad:
        raise ValidationError(f"Forbidden command(s) used: {', '.join(sorted(bad))}")
    allowed = set(CMD_RE.findall(old_body)) | SAFE_EXTRA
    unknown = used - allowed
    if unknown:
        raise ValidationError(
            f"New LaTeX command(s) not present in the resume: {', '.join(sorted(unknown))}"
        )

    _check_braces(new_body)
    _check_envs(new_body)

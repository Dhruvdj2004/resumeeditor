"""LaTeX -> PDF with Tectonic, in a throwaway directory, with insecure features disabled."""
import asyncio
import re
import shutil
import tempfile
from pathlib import Path

from .config import COMPILE_TIMEOUT


class CompileError(Exception):
    pass


def _summarize_log(log: str) -> str:
    # Keep the lines that point at the actual TeX error.
    errors = [l for l in log.splitlines() if l.startswith("error") or l.startswith("!") or "Undefined control" in l]
    text = "\n".join(errors[:8]) or log[-1500:]
    return re.sub(r"/[^\s:]*/(resume\.tex)", r"\1", text)


async def compile_pdf(tex: str) -> bytes:
    if not shutil.which("tectonic"):
        raise CompileError("tectonic is not installed (brew install tectonic)")
    with tempfile.TemporaryDirectory(prefix="resume-") as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(tex, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            "tectonic", "--untrusted", "--chatter", "minimal", "--outdir", tmp, str(src),
            cwd=tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=COMPILE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise CompileError(f"LaTeX compile timed out after {COMPILE_TIMEOUT}s")
        pdf = Path(tmp) / "resume.pdf"
        if proc.returncode != 0 or not pdf.exists():
            raise CompileError(_summarize_log(out.decode("utf-8", errors="ignore")))
        return pdf.read_bytes()

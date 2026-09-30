"""Compile a sample resume once so Tectonic downloads and caches every LaTeX package it needs.

Run at Docker build time; afterwards real compiles don't have to fetch anything.
"""
import asyncio

from app.compiler import compile_pdf
from app.latex import render_resume

SAMPLE = {
    "name": "Sample Person",
    "headline": "Engineer",
    "contact": [{"text": "sample@example.com", "url": "mailto:sample@example.com"}],
    "sections": [
        {"title": "Summary", "type": "paragraph", "text": "Builds **useful** things."},
        {
            "title": "Experience",
            "type": "entries",
            "entries": [
                {"title": "Company", "date": "2020 -- 2024", "subtitle": "Role", "meta": "City",
                 "bullets": ["Did *work* and **shipped** it."]}
            ],
        },
        {"title": "Skills", "type": "skills", "rows": [{"label": "Languages", "value": "Python, SQL"}]},
        {"title": "Awards", "type": "bullets", "items": ["An award"]},
    ],
}

if __name__ == "__main__":
    pdf = asyncio.run(compile_pdf(render_resume(SAMPLE)))
    print(f"Tectonic cache warmed ({len(pdf)} byte sample PDF)")

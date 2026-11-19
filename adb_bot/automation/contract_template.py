"""The raw contract (Managementvertrag) as an editable text in the manager (17.11, Rene: "Rohvertrag in den Einstellungen,
den ich jederzeit ändern kann").

The master is /opt/docuseal/template/managementvertrag.html with DocuSeal tags {{Name;role=…}} for the 14 fields. The manager
edits the <body> with the tags shown as fixed chips; save() checks that every tag is still there exactly once, keeps the old
version, writes the master, builds the white-token copy (FLD01..14, field_spec.json), prints both to PDF and publishes a new
DocuSeal template (publish_template.py switches DOCUSEAL_TEMPLATE_ID). Contracts already sent keep the version they were sent with.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

TPL = Path("/opt/docuseal/template")
MASTER = TPL / "managementvertrag.html"
FIELDS = TPL / "managementvertrag_fields.html"
VERSIONS = TPL / "versions"
PDF_PY = "/root/tutorial/.venv/bin/python"          # the venv with playwright + chromium
PUBLISH = ["/root/adb_bot/.venv/bin/python3", str(TPL / "publish_template.py")]
TAG = re.compile(r"\{\{([^};]+)(;[^}]*)?\}\}")
WIDTH = {"FLD02": 85, "FLD03": 40, "FLD04": 110, "FLD05": 85, "FLD07": 14, "FLD10": 70, "FLD13": 70}   # mm, default 38
ALLOWED = {"h1", "h2", "h3", "p", "b", "strong", "i", "em", "u", "ul", "ol", "li", "br", "div", "span"}


def _spec() -> list[dict]:
    return json.loads((TPL / "field_spec.json").read_text())


def _split(doc: str) -> tuple[str, str, str]:
    a = doc.index("<body>") + len("<body>")
    b = doc.rindex("</body>")
    return doc[:a], doc[a:b], doc[b:]


def _tags(body: str) -> dict[str, str]:
    """name -> full tag text, in document order."""
    return {m.group(1).strip(): m.group(0) for m in TAG.finditer(body)}


def get() -> dict:
    """Body for the editor: tags become non-editable chips that carry only the field name."""
    _, body, _ = _split(MASTER.read_text())
    chips = TAG.sub(lambda m: f'<span class="ph" contenteditable="false" data-name="{html.escape(m.group(1).strip())}">'
                              f'{html.escape(m.group(1).strip())}</span>', body)
    meta = json.loads((TPL / "template_meta.json").read_text()) if (TPL / "template_meta.json").exists() else {}
    vers = sorted((p.name for p in VERSIONS.glob("*")), reverse=True)[:10] if VERSIONS.exists() else []
    return {"body": chips.strip(), "template_id": meta.get("template_id"), "updated": dt.datetime.fromtimestamp(MASTER.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
            "fields": [f["name"] for f in _spec()], "versions": vers}


class _Clean(HTMLParser):
    """Keeps text and a small set of formatting tags; a chip turns back into its DocuSeal tag."""

    def __init__(self, tags: dict[str, str]):
        super().__init__(convert_charrefs=True)
        self.tags, self.out, self.skip, self.seen = tags, [], 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            self.skip += tag != "br"
            return
        if tag == "span" and "ph" in (a.get("class") or "").split():
            name = a.get("data-name", "")
            if name not in self.tags:
                raise ValueError(f"Unbekannter Platzhalter: {name}")
            self.out.append(self.tags[name]); self.seen.append(name)
            self.skip = 1                                   # the chip's own label is not contract text
            return
        if tag not in ALLOWED or tag == "span":
            return
        keep = {k: v for k, v in attrs if (k == "class" and tag in ("p", "div") and v in ("muted", "parties", "sig", "line"))
                or (k == "style" and v.replace(" ", "") == "page-break-before:always")}   # the signature page starts on its own page
        self.out.append(f"<{tag}" + "".join(f' {k}="{html.escape(v)}"' for k, v in keep.items()) + (">" if tag != "br" else ">"))

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if tag in ALLOWED and tag not in ("span", "br"):
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def _fields_doc(doc: str) -> str:
    """The white-token copy publish_template.py reads the field boxes from."""
    tok = {f["name"]: f["token"] for f in _spec()}

    def span(m):
        t = tok[m.group(1).strip()]
        h = "18mm" if t in ("FLD10", "FLD13") else "auto"
        return (f'<span class="tag" style="display:inline-block;min-width:{WIDTH.get(t, 38)}mm;height:{h};vertical-align:bottom;'
                f'border-bottom:1px dotted #999">{{{{{t}}}}}</span>')
    out = TAG.sub(span, doc)
    return out.replace("</style>", ".tag{color:#ffffff;}\n</style>", 1)


def _pdf(src: Path, dst: Path) -> None:
    code = ("import sys\nfrom playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n b = p.chromium.launch(); pg = b.new_page()\n"
            " pg.goto('file://' + sys.argv[1]); pg.pdf(path=sys.argv[2], prefer_css_page_size=True, print_background=True); b.close()\n")
    subprocess.run([PDF_PY, "-c", code, str(src), str(dst)], check=True, capture_output=True, timeout=120)


def save(body_html: str, by: str = "") -> dict:
    head, old_body, tail = _split(MASTER.read_text())
    tags = _tags(old_body)
    c = _Clean(tags)
    c.feed(body_html or "")
    c.close()
    missing = [n for n in tags if n not in c.seen]
    double = sorted({n for n in c.seen if c.seen.count(n) > 1})
    if missing:
        raise ValueError("Diese Platzhalter fehlen im Text: " + ", ".join(missing))
    if double:
        raise ValueError("Diese Platzhalter stehen doppelt drin: " + ", ".join(double))
    body = "\n" + "".join(c.out).strip() + "\n"
    if len(re.sub(r"<[^>]+>|\s", "", body)) < 2000:
        raise ValueError("Der Text ist viel kürzer als vorher, gespeichert wird nicht. Ist beim Kopieren etwas verloren gegangen?")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    vdir = VERSIONS / stamp
    vdir.mkdir(parents=True, exist_ok=True)
    for f in (MASTER, FIELDS, TPL / "managementvertrag.pdf", TPL / "managementvertrag_fields.pdf", TPL / "template_meta.json"):
        if f.exists():
            (vdir / f.name).write_bytes(f.read_bytes())
    (vdir / "by.txt").write_text(by)
    doc = head + body + tail
    MASTER.write_text(doc)
    FIELDS.write_text(_fields_doc(doc))
    try:
        _pdf(MASTER, TPL / "managementvertrag.pdf")
        _pdf(FIELDS, TPL / "managementvertrag_fields.pdf")
        r = subprocess.run(PUBLISH + [str(TPL / "managementvertrag_fields.pdf")], capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    except Exception as e:                                  # put the previous version back, nothing half-published
        for f in vdir.iterdir():
            if f.name != "by.txt":
                (TPL / f.name).write_bytes(f.read_bytes())
        raise ValueError(f"Vertrag konnte nicht veröffentlicht werden, alter Stand bleibt: {str(e)[:200]}") from e
    meta = json.loads((TPL / "template_meta.json").read_text())
    return {"ok": True, "template_id": meta.get("template_id"), "version": stamp}


def preview_pdf() -> Path | None:
    p = TPL / "managementvertrag.pdf"
    return p if p.exists() else None

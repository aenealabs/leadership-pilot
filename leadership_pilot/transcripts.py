"""Turn an uploaded file into interview notes: plain text, Word, or a Teams transcript (.vtt).

A Teams transcript becomes one line per turn, "Speaker: what they said", with timestamps and
cue numbers removed and consecutive cues from the same speaker joined, so quote tracing
works on what people said rather than on the file format.
"""

import io
import re

MAX_BYTES = 2_000_000
VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>|$)", re.DOTALL)
TIMING = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s+-->\s+")
TAG = re.compile(r"</?[^>]+>")
SPEAKER = re.compile(r"^([^:<>]{1,40}):\s+(.*)$", re.DOTALL)


class TranscriptError(ValueError):
    """The file cannot be read as notes; the message is user-facing."""


def notes_from_file(filename: str, data: bytes) -> str:
    name = filename.lower()
    if len(data) > MAX_BYTES:
        raise TranscriptError("That file is over 2 MB. Paste the notes instead.")
    if name.endswith(".docx"):
        return _docx(data)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise TranscriptError("Upload a UTF-8 text file, a Word document, or a Teams "
                              "transcript (.vtt).") from e
    if name.endswith(".vtt") or text.lstrip().startswith("WEBVTT"):
        return vtt_to_notes(text)
    if name.endswith((".txt", ".md")):
        return text.strip()
    raise TranscriptError("Upload a .txt, .md, .docx, or Teams .vtt file.")


def _docx(data: bytes) -> str:
    from docx import Document

    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:  # python-docx raises several types for a bad file
        raise TranscriptError("That Word document could not be read.") from e
    return "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())


def vtt_to_notes(text: str) -> str:
    turns: list[list[str]] = []  # [speaker, text]
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        while lines and not TIMING.match(lines[0]):
            lines.pop(0)  # cue identifier
        if not lines:
            continue
        body = " ".join(lines[1:])
        if voice := VOICE.search(body):  # Teams: <v Speaker Name>text</v>
            speaker, said = voice.group(1), voice.group(2)
        elif prefixed := SPEAKER.match(body):  # other tools: "Speaker Name: text"
            speaker, said = prefixed.group(1), prefixed.group(2)
        else:
            speaker, said = "", body
        said = " ".join(TAG.sub("", said).split())
        if not said:
            continue
        if turns and turns[-1][0] == speaker.strip():
            turns[-1][1] += " " + said
        else:
            turns.append([speaker.strip(), said])
    if not turns:
        raise TranscriptError("No speech found in that transcript.")
    return "\n\n".join(f"{s}: {t}" if s else t for s, t in turns)

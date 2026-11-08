"""Post templates.

Admin-editable text with:   {variables}   <text> = bold   <q>text</q> = quote   [label](https://link) = link
Example:   <q><{title}></q>
           <➣ TYPE :> {catagory}
           <q><Synopsis:> {synopsis}</q>
"""
import html
import re

DEFAULT_TEMPLATES = {
    "sub": (
        "<q><{title}></q>\n"
        "────────────────────────\n"
        "<➣ TYPE :> {catagory}\n"
        "<➥ AVERAGE RATING :> {rating}\n"
        "<➥ EPISODE :> {episode}\n"
        "<➥ Total EPISODE :> {total_episode}\n"
        "<➥AIRED ON:> {aired_on}\n"
        "<➥ STUDIO:> {studio}\n"
        "<➣ GENRES :> {genres}\n"
        "────────────────────────\n"
        "<q><Synopsis:> {synopsis}</q>"
    ),
    "episode": (
        "<q><{title}></q>\n"
        "────────────────────────\n"
        "<➣ TYPE :> {catagory}\n"
        "<➥ AVERAGE RATING :> {rating}\n"
        "<➥ EPISODE :> {episode}\n"
        "<➥ Total EPISODE :> {total_episode}\n"
        "<➥AIRED ON:> {aired_on}\n"
        "<➥ STUDIO:> {studio}\n"
        "<➣ GENRES :> {genres}\n"
        "────────────────────────\n"
        "<q><Synopsis:> {synopsis}</q>"
    ),
    "file": (
        "<q><{title}></q>\n"
        "────────────────────────\n"
        "<➣ EPISODE :> {episode} / {total_episode}\n"
        "<➥ QUALITY :> {quality}\n"
        "<➥ SIZE :> {size}\n"
        "<➥ TYPE :> {catagory}\n"
        "<➥ AVERAGE RATING :> {rating}\n"
        "<➥AIRED ON:> {aired_on}\n"
        "<➥ STUDIO:> {studio}\n"
        "<➣ GENRES :> {genres}\n"
        "────────────────────────"
    ),
}
TEMPLATE_NAMES = {"sub": "Sub channel (anime post)", "episode": "Episode channel (episode post)",
                  "file": "File channel (video caption)"}

VARIABLES = {
    "title": "anime title",
    "alt_title": "other titles",
    "catagory": "type: TV, OVA, Movie… (also {category} / {type})",
    "rating": "average rating / score",
    "episode": "episode number (anime post: how many episodes are posted so far)",
    "total_episode": "total episodes of the anime",
    "aired_on": "aired date",
    "studio": "studio(s)",
    "genres": "genres, comma separated",
    "synopsis": "synopsis (shortened automatically to fit)",
    "status": "Completed / Airing…",
    "duration": "episode length",
    "quality": "quality of the file(s)",
    "size": "file size(s)",
    "ep_title": "episode page title",
}
ALIASES = {"category": "catagory", "type": "catagory"}

_TOKEN = re.compile(r"(</?q>|</?(?:b|i|u|s|code)>|<[^<>]*>)", re.I)
_PASS = re.compile(r"^</?(?:b|i|u|s|code)>$", re.I)
_LINK = re.compile(r"\[([^\]]+)\]\(((?:https?|tg)://[^)\s]+)\)")
_VAR = re.compile(r"\{(\w+)\}")


def clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else (s[: n - 1].rstrip() + "…" if n > 1 else "")


def compile_template(tpl):
    """Template text -> Telegram HTML that still contains {placeholders}."""
    out = []
    for tok in _TOKEN.split(tpl or ""):
        low = tok.lower()
        if low == "<q>":
            out.append("<blockquote>")
        elif low == "</q>":
            out.append("</blockquote>")
        elif _PASS.match(tok):
            out.append(low)
        elif len(tok) > 2 and tok.startswith("<") and tok.endswith(">"):
            out.append("<b>" + html.escape(tok[1:-1], quote=False) + "</b>")
        else:
            out.append(html.escape(tok, quote=False))
    return _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', "".join(out))


def check_template(tpl):
    """Returns a list of problems (empty = fine)."""
    problems = []
    if len(re.findall(r"<q>", tpl, re.I)) != len(re.findall(r"</q>", tpl, re.I)):
        problems.append("every <q> needs a closing </q>")
    for name in set(_VAR.findall(tpl)):
        if ALIASES.get(name, name) not in VARIABLES:
            problems.append(f"unknown variable {{{name}}}")
    if not tpl.strip():
        problems.append("the template is empty")
    return problems


def render(tpl, values, limit=1000):
    """Fill a template. Values are HTML-escaped; synopsis/genres/... are shortened so the result fits a caption."""
    compiled = compile_template(tpl)
    work = {k: ("" if v is None else str(v)) for k, v in values.items()}

    def build():
        esc = {k: html.escape(v, quote=False) for k, v in work.items()}
        return _VAR.sub(lambda m: esc.get(ALIASES.get(m.group(1), m.group(1)), m.group(0)), compiled)

    text = build()
    for key in ("synopsis", "genres", "alt_title", "studio", "ep_title"):
        for _ in range(40):
            if len(text) <= limit or not work.get(key):
                break
            over = len(text) - limit
            work[key] = clip(work[key], len(work[key]) - over - 8) if len(work[key]) > over + 8 else ""
            text = build()
    return text


SAMPLE = {
    "title": "Sample Anime", "alt_title": "Sample Alt", "catagory": "TV", "rating": "7.7", "episode": "3",
    "total_episode": "12", "aired_on": "Aug 2009", "studio": "Studio X, Studio Y",
    "genres": "Comedy, Romance, Drama", "synopsis": "A short sample synopsis so you can see how the post will look.",
    "status": "Completed", "duration": "24m", "quality": "720p", "size": "412.3 MB", "ep_title": "Sample Anime - Episode 3",
}
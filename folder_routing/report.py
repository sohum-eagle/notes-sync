"""Dry-run report rendering. Output contains titles and links for human review,
so it is written to a file the operator chooses, never to logs."""
from collections import Counter

from .policy import AUTO, REVIEW, SKIP, POLICY_VERSION


def render_markdown(rows, heading="Folder routing dry run", notes=()):
    """rows: dicts with provider, source_id, title, link, decision, reasons, explanation, already_member."""
    counts = Counter((r["provider"], "already_member" if r.get("already_member") else r["decision"]) for r in rows)
    out = [f"# {heading}", "", f"Policy version: `{POLICY_VERSION}`. Writes: none (dry run).", ""]
    out += [f"- {n}" for n in notes]
    out += ["", "| Provider | Already in folder | Would auto-file | Review | Skip |", "|---|---|---|---|---|"]
    for p in sorted({r["provider"] for r in rows}):
        out.append(f"| {p} | {counts[(p, 'already_member')]} | {counts[(p, AUTO)]} | "
                   f"{counts[(p, REVIEW)]} | {counts[(p, SKIP)]} |")
    sections = [
        ("Would auto-file", lambda r: not r.get("already_member") and r["decision"] == AUTO),
        ("Review queue", lambda r: not r.get("already_member") and r["decision"] == REVIEW),
        ("Already in folder (preserved, never changed)", lambda r: r.get("already_member")),
        ("Skipped (no PLSA attendance signal)", lambda r: not r.get("already_member") and r["decision"] == SKIP),
    ]
    for name, pred in sections:
        items = [r for r in rows if pred(r)]
        out += ["", f"## {name} ({len(items)})", ""]
        if not items:
            out.append("None.")
            continue
        out += ["| Provider | Date | Meeting | Decision basis |", "|---|---|---|---|"]
        for r in sorted(items, key=lambda r: r.get("date", ""), reverse=True):
            title = (r.get("title") or "(untitled)").replace("|", "/")
            link = f"[{title}]({r['link']})" if r.get("link") else title
            out.append(f"| {r['provider']} | {r.get('date', '')[:10]} | {link} | `{', '.join(r['reasons'])}` |")
    return "\n".join(out) + "\n"

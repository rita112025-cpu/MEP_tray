"""Self-contained HTML report. Dynamic text is escaped. No script and no external resources."""
from __future__ import annotations

import html
import re

from . import CHECK_LABELS, CHECK_ORDER
from .checks import Finding

_MAX_CELL = 500
_CTRL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

_CSS = """
body{font-family:"Microsoft JhengHei","PingFang TC","Noto Sans CJK TC",sans-serif;margin:24px auto;max-width:1200px;
color:#111;background:#fff;line-height:1.5;padding:0 16px}
h1{font-size:1.45rem;margin:.2em 0}h2{font-size:1.12rem;border-bottom:2px solid #444;padding-bottom:.2em;margin-top:1.8em}
table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:.9rem}
th,td{border:1px solid #888;padding:4px 6px;text-align:left;vertical-align:top;word-break:break-word}
th{background:#eee}
.banner{border:3px solid #000;border-left-width:14px;padding:10px 14px;margin:1em 0;background:#fff8dc}
.note{border-left:4px solid #666;padding:4px 10px;margin:.6em 0;background:#f4f4f4}
.mono{font-family:Consolas,"Courier New",monospace;font-size:.85rem}
@media print{body{max-width:none;margin:8mm}h2{break-after:avoid}tr{break-inside:avoid}}
""".strip()

_BLURB = {
    "required_parameters": "比對規則檔列出的參數名稱與前綴。缺少參數，或值為空白，各列一筆。",
    "open_connectors": "在規則檔列出的品類中，is_connected 為 false 的端點各列一筆。沒有端點資料時，只有規則明示 flag_when_no_connectors 才列為發現。",
    "elevation": "偏移與樓層不一致都只用規則檔寫明的門檻。對不到門檻的元件略過，不套用預設尺寸。",
    "duplicates": "同一族與類型在容差內的位置、族名後綴、以及數量區間，三項都由規則檔指定。",
}

_HEADERS = ("元件 Id", "品類", "內建品類", "族", "類型", "樓層", "檢查項", "規則 Id", "問題")


def render_html(snapshot: dict, rules: dict, findings: list[Finding]) -> str:
    counts = {check: 0 for check in CHECK_ORDER}
    for finding in findings:
        counts[finding.check] = counts.get(finding.check, 0) + 1
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-Hant">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; sandbox">',
        f"<title>{_esc('BIM 模型自動巡檢報告')}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{_esc('BIM 模型自動巡檢報告')}</h1>",
        '<div class="banner">',
        f"<p>{_esc('本報告只列出規則檔已啟用的檢查結果。門檻只來自規則檔。')}</p>",
        f"<p>{_esc('讀取的是 JSON 快照。這份報告的產生過程不修改、不儲存、不同步模型。')}</p>",
        f"<p>{_esc('零筆發現只代表這些規則沒有命中。')}</p>",
        "</div>",
        f'<p class="note">{_esc("專案標籤")}：<span class="mono">{_esc(snapshot["source"]["project_label"])}</span>'
        f'　{_esc("單位")}：<span class="mono">{_esc(snapshot["source"]["units"])}</span>'
        f'　{_esc("發現")}：<span class="mono">{len(findings)}</span></p>',
    ]
    notes = rules.get("notes") or ""
    if notes:
        parts.append(f'<p class="note">{_esc(notes)}</p>')
    parts.append(_summary(rules, counts))
    parts.append(_details(findings))
    parts.append("</body></html>\n")
    return "\n".join(parts)


def _summary(rules: dict, counts: dict) -> str:
    rows = []
    notes = []
    for check in CHECK_ORDER:
        enabled = rules["checks"][check]["enabled"]
        state = "啟用" if enabled else "未啟用"
        rows.append((CHECK_LABELS[check], state, counts[check]))
        notes.append(f"<h3>{_esc(CHECK_LABELS[check])}</h3><p>{_esc(_BLURB[check])}</p>")
    body = _table(("檢查項", "狀態", "發現數"), rows) + "\n" + "\n".join(notes)
    return f'<section id="summary"><h2>{_esc("檢查摘要")}</h2>\n{body}\n</section>'


def _details(findings: list[Finding]) -> str:
    rows = [
        (
            item.element_id,
            item.category,
            item.builtin_category,
            item.family,
            item.type_name,
            item.level,
            CHECK_LABELS.get(item.check, item.check),
            item.rule_id,
            item.issue,
        )
        for item in findings
    ]
    table = _table(_HEADERS, rows, empty="沒有發現")
    return f'<section id="details"><h2>{_esc("明細")}</h2>\n{table}\n</section>'


def _table(headers: tuple[str, ...], rows: list[tuple], empty: str = "") -> str:
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>")
    if not body_rows and empty:
        body_rows.append(f'<tr><td colspan="{len(headers)}">{_esc(empty)}</td></tr>')
    return "<table><thead><tr>" + head + "</tr></thead><tbody>\n" + "\n".join(body_rows) + "\n</tbody></table>"


def _esc(value: object) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = _CTRL.sub(lambda match: "\\u%04X" % ord(match.group()), text)
    if len(text) > _MAX_CELL:
        text = text[:_MAX_CELL - 1] + "…"
    return html.escape(text, quote=True)

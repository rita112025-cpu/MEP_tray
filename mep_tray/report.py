"""自足單一 HTML 報告（標準庫產生；無 JS、無外部資源；可由瀏覽器列印成 PDF）。

安全原則：所有動態文字（障礙物名稱、type_name、notes、subject、clause、suggestion、disclosures、檔名…）
一律「先 scrub（脫敏）再 html.escape(quote=True)」；HTML 片段只能由本模組的固定字面值組成（Raw 包裝），
沒有任何把使用者文字當 HTML 的路徑。報告本體不含建立時間（只在 manifest），故同輸入同 run_id 的位元組可相同。

章節順序固定（每段有 id）：disclosure → inputs → governing → findings → compliance → route → files → diff。
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass

from .disclosure import BANNER_FIXED, ENGINE_CHANGED_NOTE, REVIT_STATUS, SCOPE_NOTE
from .rules import LABELS
from .sanitize import scrub

MAX_FINDINGS = 500
MAX_SEGMENTS = 200
MAX_OBSTACLES = 200
MAX_DIFF_ROWS = 200
MAX_CELL = 300          # 單一儲存格/說明文字的字元上限（超過截斷並加「…」），避免使用者輸入撐爆頁面
# 移除/可見化的字元：C0/C1 控制字元（保留 \n \t）、零寬與雙向控制（LRM/RLM/ALM、U+202A–202E、U+2066–2069）、
# 行/段分隔符、BOM。障礙物名稱含 U+202E 之類會把相鄰文字視覺上倒轉，可用來偽裝揭露。
_CTRL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f؜​-‏ -‮⁠-⁯﻿]")


BADGE = {"CLASH": ("✖", "衝突"), "FAIL": ("✖", "不符合"), "UNVERIFIED": ("？", "規範值未驗證"),
         "PASS": ("✔", "符合")}
SOURCE_LABEL = {"inputs": "輸入變更", "rules": "規範值變更", "both": "輸入與規範值皆變更", "none": "輸入與規範值皆無變更",
                "engine": "引擎變更（輸入與規範值相同，結果不同）",
                "unexplained": "無法解釋（輸入、規範值、引擎皆相同但結果不同；疑非決定性或程式問題）"}

CSS = """
body{font-family:"Microsoft JhengHei","PingFang TC","Noto Sans CJK TC",sans-serif;margin:24px auto;max-width:1100px;
color:#111;background:#fff;line-height:1.5;padding:0 16px}
h1{font-size:1.5rem;margin:.2em 0}h2{font-size:1.15rem;border-bottom:2px solid #444;padding-bottom:.2em;margin-top:2em}
table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:.9rem}
th,td{border:1px solid #888;padding:4px 6px;text-align:left;vertical-align:top;word-break:break-word}
th{background:#eee}caption{text-align:left;font-weight:bold;margin-bottom:.3em}
.banner{border:3px solid #000;border-left-width:14px;padding:10px 14px;margin:1em 0;background:#fff8dc}
.banner strong{font-size:1.05rem}.note{border-left:4px solid #666;padding:4px 10px;margin:.6em 0;background:#f4f4f4}
.warn{border:2px solid #000;padding:6px 10px;margin:.6em 0;font-weight:bold}
.mono{font-family:Consolas,"Courier New",monospace;font-size:.85rem}
.badge{display:inline-block;border:1px solid #000;padding:0 6px;font-weight:bold;white-space:nowrap}
.unv{background:#fff1b8}.bad{background:#ffd6d6}.ok{background:#d9f2d9}
@media print{body{max-width:none;margin:8mm}h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}}
""".strip()


class Raw(str):
    """已確認安全的 HTML 片段（只能由本模組以固定字面值與 esc() 組成）。"""


def fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        r = round(v, 3) + 0.0
        return f"{r:g}"
    if isinstance(v, (list, tuple)):
        return ", ".join(fmt(x) for x in v)
    return str(v)


def clean(s: str, cap: int = MAX_CELL) -> str:
    """控制/雙向覆寫字元改為可見的 \\uXXXX 字面；超過長度上限截斷並加「…」。"""
    s = _CTRL.sub(lambda m: "\\u%04X" % ord(m.group()), s)
    return s if len(s) <= cap else s[:cap - 1] + "…"


def esc(v) -> str:
    """脫敏 → 控制字元可見化與長度上限 → HTML 跳脫。Raw 原樣通過。"""
    if isinstance(v, Raw):
        return str(v)
    return html.escape(clean(scrub(fmt(v))), quote=True)


def badge(status: str) -> Raw:
    sym, label = BADGE.get(status, ("?", status))
    cls = {"PASS": "ok", "UNVERIFIED": "unv"}.get(status, "bad")
    return Raw(f'<span class="badge {cls}">{esc(sym)} {esc(label)}</span> <span class="mono">{esc(status)}</span>')


def table(headers, rows, caption: str | None = None) -> str:
    out = ["<table>"]
    if caption:
        out.append(f"<caption>{esc(caption)}</caption>")
    out.append("<thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def limited(rows: list, n: int, noun: str, hint: str = "完整資料請見 manifest／JSON") -> tuple[list, str]:
    if len(rows) <= n:
        return rows, ""
    return rows[:n], f'<p class="warn">僅顯示前 {n} 筆{esc(noun)}，共 {len(rows)} 筆；{esc(hint)}。</p>'


def section(sid: str, title: str, body: str) -> str:
    return f'<section id="{sid}">\n<h2>{esc(title)}</h2>\n{body}\n</section>'


@dataclass
class ReportData:
    run_id: str
    inp: object
    codes: list
    gov: dict
    route: object
    reports: dict
    stats: dict
    manifest: dict | None
    disclosures: list
    dwg_note: str = ""
    acad_audit: int | None = None


def data_from_run(run) -> ReportData:
    return ReportData(run.run_id, run.inputs, list(run.codes), run.gov, run.route, run.reports, run.stats,
                      run.manifest, list(run.disclosures), run.dwg_note, run.acad_audit)


# ───────────────────────── 章節 ─────────────────────────
def _disclosure(d: ReportData) -> str:
    unverified = d.stats.get("unverified_checks", 0) > 0 or any(not g.verified for g in d.gov.values())
    parts = []
    if unverified:
        parts.append(f'<div class="banner"><strong>{esc(BANNER_FIXED)}</strong></div>')
    # 範圍聲明永遠印（含全部規範值已驗證時），否則零 finding 的報告會被讀成「全部合規」
    parts.append(f'<div class="banner"><strong>{esc(SCOPE_NOTE)}</strong></div>')
    items = "".join(f"<li>{esc(x)}</li>" for x in d.disclosures)
    parts.append(f'<div class="banner"><ul>{items}</ul></div>' if items else "")
    return section("disclosure", "揭露事項", "\n".join(p for p in parts if p))


def _inputs(d: ReportData) -> str:
    i, opts = d.inp, (d.manifest or {}).get("options", {})
    rows = [("房間 (m)", f"{fmt(i.room[0])} → {fmt(i.room[1])}"), ("橋架寬 × 高 (mm)", f"{fmt(i.tray_w_mm)} × {fmt(i.tray_h_mm)}"),
            ("橋架類型", i.tray_type), ("起點 (m)", fmt(i.start)), ("終點 (m)", "；".join(fmt(e) for e in i.ends)),
            ("規範（依勾選順序）", ", ".join(d.codes)), ("格距 (m)", d.route.cell_m),
            ("Revit CableTrayType 名稱", opts.get("type_name")), ("Revit 座標基準", opts.get("basis"))]
    if opts.get("origin_mm") is not None:
        rows.append(("模型原點 (mm)", fmt(opts["origin_mm"])))
    if opts.get("rotation_deg") is not None:
        rows.append(("模型繞 Z 旋轉 (度，逆時針為正)", fmt(opts["rotation_deg"])))
    body = [table(["項目", "值"], rows, "輸入摘要")]
    if i.cables_defaulted:
        body.append('<p class="warn">未輸入電纜資料：填充率以預設電纜（Ø20 mm × 10 條）計算。</p>')
    else:
        body.append(table(["外徑 (mm)", "條數", "類型"],
                          [(c["od_mm"], c.get("count", 1), c.get("kind")) for c in i.cables], "電纜"))
    obs = [(o["name"], o["kind"], fmt(o["lo"]), fmt(o["hi"])) for o in i.obstacles]
    shown, note = limited(obs, MAX_OBSTACLES, "障礙物")
    body.append(table(["名稱", "類型", "lo (m)", "hi (m)"], shown, "障礙物") if shown else "<p>無障礙物（空房間）。</p>")
    body.append(note)
    return section("inputs", "輸入摘要", "\n".join(body))


def _sources_text(g) -> str:
    """勝出規範對該參數的出處：規則編號、PDF 頁、附錄頁、強度；多筆全部保留。無出處則「—」。"""
    def one(s):
        if "document" in s:
            return f"{s['document']} {s['section']} p.{s['document_page']}（{s['strength']}）"
        return f"{s['rule_id']} PDF p.{s['pdf_page']}（{s['appendix_page']}，{s['strength']}）"
    return "；".join(one(s) for s in g.sources) or "—"


def _governing(d: ReportData) -> str:
    rows = []
    for key, g in sorted(d.gov.items()):
        vals = "；".join(f"{c}={fmt(v)}" for c, v in g.all_values.items())
        rows.append((key, g.label, "下限（≥）" if g.direction == "min" else "上限（≤）", f"{fmt(g.value)} {g.unit}",
                     g.code, g.clause, "已驗證" if g.verified else "未驗證", vals, _sources_text(g)))
    body = table(["參數", "名稱", "方向", "生效值", "勝出規範", "條文", "規範值狀態", "各規範原值", "出處"], rows,
                 "各參數採用最嚴格條件；勝出者為「生效值」的來源")
    return section("governing", "規範勝出表", body)


def _finding_row(f: dict) -> list:
    fix = f.get("fix")
    fix_txt = (f"沿 {fix['sign']}{fix['axis']} 移動至少 {fmt(fix['move_mm'])} mm" if fix else "—")
    basis = f"{f['code'] or '—'}：{f['clause'] or '—'}" + ("" if f["verified"] else "（規範值未驗證）")
    return [badge(f["status"]), f["kind"], f["subject"], fmt(f["location"]),
            f"{fmt(f['actual'])} / {fmt(f['required'])} {f['unit']}", basis, f["suggestion"] or "—", fix_txt]


def _totals(items: list[dict], unit: str) -> str:
    """不截斷的總計（表格被截斷時，使用者仍看得到真正的數量）。"""
    by = Counter(f"{x['kind']}／{LABELS.get(x['status'], x['status'])}" for x in items)
    parts = "；".join(f"{k}：{n}" for k, n in sorted(by.items())) or "無"
    unv = sum(1 for x in items if x["status"] == "UNVERIFIED")
    return (f'<p class="note"><strong>總計（未截斷）：共 {len(items)} {esc(unit)}</strong>；規範值未驗證 {unv} {esc(unit)}；'
            f"依類型／狀態：{esc(parts)}。</p>")


def _findings(d: ReportData) -> str:
    fs = [f.to_dict() for key in ("clash", "compliance") if key in d.reports for f in d.reports[key].findings]
    if not fs:
        unv = d.stats.get("unverified_checks", 0) > 0
        msg = "零問題（僅涵蓋本工具已實作之檢查項）。" + ("但所依規範值尚未驗證，零問題不代表合規。" if unv else "")
        return section("findings", "問題清單", f"<p>{esc(msg)}</p>")
    hint = f"完整清單見 tray_{d.run_id}.json（含 status 與 fix）及 DXF 標註"
    _, note = limited(fs, MAX_FINDINGS, "問題", hint)                      # 先切片再建列：工作量被上限擋住
    rows = [_finding_row(f) for f in fs[:MAX_FINDINGS]]
    body = table(["狀態", "類型", "對象", "位置 (m)", "實際 / 要求", "規範依據", "建議", "建議位移（fix）"], rows,
                 f"共 {len(fs)} 筆")
    return section("findings", "問題清單", _totals(fs, "筆問題") + body + note)


def _compliance(d: ReportData) -> str:
    cs = [c.to_dict() for key in ("clash", "compliance") if key in d.reports for c in d.reports[key].checks]
    rows = []
    for c in cs[:MAX_FINDINGS]:                                              # 先切片再建列
        basis = f"{c['code'] or '—'}：{c['clause'] or '—'}"
        rows.append([badge(c["status"]), c["kind"], c["subject"], f"{fmt(c['actual'])} / {fmt(c['required'])} {c['unit']}",
                     basis, "規範值未驗證" if not c["verified"] else "已驗證"])
    _, note = limited(cs, MAX_FINDINGS, "檢查項", f"完整清單見 tray_{d.run_id}.json 與 DXF 標註")
    body = table(["狀態", "類型", "對象", "實際 / 要求", "規範依據", "規範值狀態"], rows,
                 "全部已驗算項（含符合與規範值未驗證）") if rows else "<p>無檢查項。</p>"
    return section("compliance", "合規表", (_totals(cs, "項檢查") if cs else "") + body + note)


def _route(d: ReportData) -> str:
    s = d.stats
    src = s.get("hanger_span_source")
    span = f"{fmt(s.get('hanger_span_m'))} m（來源：{'預設值，非規範值' if src == 'default' else src}）"
    rows = [("線段數", s.get("segments")), ("接頭數", s.get("joints")), ("總長 (m)", s.get("length_m")),
            ("吊架數", s.get("hangers")), ("吊架間距", span), ("轉彎數", s.get("bends")),
            ("格距 (m)", d.route.cell_m), ("規範值未驗證的檢查項數", s.get("unverified_checks"))]
    return section("route", "路徑統計", table(["項目", "值"], rows))


def _files(d: ReportData) -> str:
    m = d.manifest
    if not m:
        return section("files", "產出檔與雜湊", "<p>未提供 manifest。</p>")
    frows = [(k, v["name"], v["bytes"], Raw(f'<span class="mono">{esc(v["sha256"])}</span>'), v["determinism"])
             for k, v in sorted(m["files"].items())]
    h, e, env = m["hashes"], m["engine"], m.get("environment", {})
    body = [table(["類別", "檔名", "位元組", "SHA256", "確定性語意"], frows, "產出檔（不列本報告與 manifest 本身：它們的雜湊含本表）"),
            '<p class="note">確定性語意：「same-run-id」＝相同輸入且相同 run_id 時位元組相同；「no-cad」＝在沒有 AutoCAD 的環境下；'
            '「never」＝不具確定性（DWG 由 AutoCAD 產生，含其自身時間戳）。完整性檢查只偵測損壞，不防刻意竄改。</p>',
            table(["雜湊", "值"], [(k, Raw(f'<span class="mono">{esc(v)}</span>')) for k, v in sorted(h.items())], "manifest 雜湊"),
            table(["引擎", "值"], sorted((k, v) for k, v in e.items()), "引擎"),
            table(["環境（描述用，不進結果雜湊）", "值"], sorted((k, v) for k, v in env.items()), "環境")]
    if d.dwg_note:
        body.append(f'<p class="note">DWG／AutoCAD：{esc(d.dwg_note)}</p>')
    body.append(f'<p class="note">Revit：{esc(REVIT_STATUS)}</p>')
    return section("files", "產出檔與雜湊", "\n".join(body))


# ───────────────────────── 差異 ─────────────────────────
def _diff_rows(d: dict) -> list:
    rows = [("變更", k, v[0], v[1]) for k, v in d["changed"].items()]
    rows += [("新增", k, "—", v) for k, v in d["added"].items()]
    rows += [("移除", k, v, "—") for k, v in d["removed"].items()]
    return rows


def diff_body(diff, a_label: str = "A", b_label: str = "B") -> str:
    if not diff.ok:
        return f'<p class="warn">拒絕比對：{esc(diff.error.message)}（{esc(diff.error.code)}）</p>'
    out = [f'<p><strong>差異來源：</strong>{esc(SOURCE_LABEL.get(diff.source, diff.source))}　'
           f'<span class="mono">source={esc(diff.source)}</span>　比對 {esc(a_label)} → {esc(b_label)}</p>']
    if diff.engine_changed:
        rows = [(k, v[0], v[1]) for k, v in diff.engine.items()]
        out.append(f'<div class="warn">{esc(ENGINE_CHANGED_NOTE)}</div>')
        out.append(table(["引擎欄位", "舊", "新"], rows, "引擎差異"))
    out.append(f"<p>結果{'相同' if diff.results_equal else '不同'}。</p>")
    r = _diff_rows(diff.inputs)
    shown, note = limited(r, MAX_DIFF_ROWS, "輸入差異")
    out.append(table(["類型", "欄位", "舊", "新"], shown, "輸入差異（逐欄位）") if shown else "<p>輸入無差異。</p>")
    out.append(note)
    ru = diff.rules
    rr = [("變更", f"{k}.{f}", v[0], v[1]) for k, fields in ru["changed"].items() for f, v in fields.items()]
    rr += [("新增", k, "—", v["code"]) for k, v in ru["added"].items()] + [("移除", k, v["code"], "—") for k, v in ru["removed"].items()]
    out.append(table(["類型", "規範參數.欄位", "舊", "新"], rr, "生效規範值差異（快照比對）") if rr else "<p>生效規範值無差異。</p>")
    if ru["hash_only_change"]:
        out.append('<p class="note">規則檔雜湊不同，但生效的規範值（值/單位/方向/代號/條文/verified）全部相同，不算規則變更。</p>')
    res = diff.result
    segs = res["segments"]
    for key, title in (("added", "新增線段"), ("removed", "移除線段")):
        rows, note = limited([(fmt(s[0]), fmt(s[1])) for s in segs[key]], MAX_SEGMENTS, title)
        out.append(table(["端點 1 (mm)", "端點 2 (mm)"], rows, f"{title}（共 {len(segs[key])} 筆）") if rows else f"<p>無{esc(title)}。</p>")
        out.append(note)
    if res["counts"]:
        out.append(table(["項目", "舊", "新", "差"], [(k, v["before"], v["after"], v.get("delta", "—"))
                                                    for k, v in res["counts"].items()], "數值差異"))
    fd = res["findings"]
    for key, title in (("added", "新增問題"), ("resolved", "已解決問題")):
        rows = [(f["kind"], f["subject"], f["code"], f["count"], fmt(f["values"])) for f in fd[key]]
        out.append(table(["類型", "對象", "規範", "筆數", "值 (實際, 要求, 狀態)"], rows, title) if rows else f"<p>無{esc(title)}。</p>")
    rows = [(f["kind"], f["subject"], f["code"], f"{f['before']['count']} → {f['after']['count']}",
             fmt(f["before"]["values"]), fmt(f["after"]["values"])) for f in fd["changed"]]
    out.append(table(["類型", "對象", "規範", "筆數", "舊值", "新值"], rows, "數值改變的問題") if rows else "<p>無數值改變的問題。</p>")
    out.append('<p class="note">問題以（類型, 對象, 規範）為鍵聚合；位置變化由線段差異呈現，不做位置配對。</p>')
    return "\n".join(out)


# ───────────────────────── 組裝 ─────────────────────────
def _page(title: str, body: str) -> str:
    return ('<!DOCTYPE html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(title)}</title>\n<style>\n{CSS}\n</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n")


def render_report_data(d: ReportData, diff=None) -> str:
    parts = [f"<h1>{esc('MEP 橋架設計報告')}</h1>", f'<p class="mono">run_id：{esc(d.run_id)}</p>',
             _disclosure(d), _inputs(d), _governing(d), _findings(d), _compliance(d), _route(d), _files(d)]
    if diff is not None:
        parts.append(section("diff", "與前一版差異", diff_body(diff, "前一版", d.run_id)))
    parts.append('<footer><p class="note">本報告不含建立時間（見 manifest.json 的 created_at）。'
                 '可由瀏覽器「列印」另存為 PDF。</p></footer>')
    return _page(f"MEP 橋架設計報告 {d.run_id}", "\n".join(parts))


def render_report(run, diff=None) -> str:
    """run: pipeline.RunResult（ok=True）。純函式，無 I/O。"""
    return render_report_data(data_from_run(run), diff)


def render_diff_report(diff, a_label: str, b_label: str) -> str:
    """只含差異章節的獨立頁（不存進版本資料夾）。"""
    body = [f"<h1>{esc('MEP 版本差異報告')}</h1>", section("diff", "版本差異", diff_body(diff, a_label, b_label))]
    return _page(f"MEP 版本差異報告 {a_label} → {b_label}", "\n".join(body))

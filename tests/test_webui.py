import copy
import http.client
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from mep_tray import acad
from mep_tray import export_dxf as X
from mep_tray import pipeline as P
from mep_tray import versioning as V
from mep_tray import webui as W
from mep_tray.disclosure import BANNER_FIXED, SCOPE_NOTE
from mep_tray.rules import load_rules


# ───────────── 夾具與輔助 ─────────────
@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setattr(acad, "find_accore", lambda: None)
    monkeypatch.setattr(X, "find_oda", lambda: None)
    s = W.make_server(0, make_dwg=False)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.jobs.join(120)                                          # 不遺留背景工作（單一工作者）到下一個測試
    s.close()


def wait_until(cond, timeout=30.0, step=0.02):
    """事件驅動等待：輪詢條件，而不是固定睡眠（CPU 被占滿時固定睡眠會誤傷）。"""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def good():
    return {"room": {"x": 12, "y": 6, "z": 4}, "tray": {"width_mm": 300, "height_mm": 100, "kind": "power"},
            "start": {"x": 1, "y": 1, "z": 3}, "ends": [{"x": 10, "y": 1, "z": 3}], "codes": ["CNS", "MRT_APPX_C"],
            "cell_m": 0.25, "cable": {"od_mm": 20, "count": 10}}


def call(s, method, path, body=None, headers=None, host=None, origin="same"):
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=20)
    h = {"Host": host if host is not None else f"127.0.0.1:{s.port}"}
    if origin == "same":
        h["Origin"] = f"http://127.0.0.1:{s.port}"
    elif origin is not None:
        h["Origin"] = origin
    h.update(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, data)
    c.close()
    return out


def jcall(s, method, path, body=None, **kw):
    st, h, d = call(s, method, path, body, **kw)
    return st, h, json.loads(d.decode("utf-8")) if d else None


def T(s, rest=""):
    return f"/t/{s.token}/{rest}"


def raw(s, payload: bytes, shut_wr=False, timeout=6):
    k = socket.create_connection(("127.0.0.1", s.port), timeout=timeout)
    try:
        k.sendall(payload)
        if shut_wr:
            k.shutdown(socket.SHUT_WR)
    except ConnectionError:                                   # 伺服器可能在我們送完之前就回應並關閉（例如 503/413）
        pass
    chunks = []
    try:
        while True:
            c = k.recv(65536)
            if not c:
                break
            chunks.append(c)
    except (socket.timeout, ConnectionError):
        pass
    k.close()
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if head.startswith(b"HTTP/") else None
    return status, head, body


def rawreq(s, lines, body=b"", method="POST", path=None, with_host=True, with_origin=True):
    path = path or T(s, "api/run")
    hs = []
    if with_host:
        hs.append(f"Host: 127.0.0.1:{s.port}")
    if with_origin:
        hs.append(f"Origin: http://127.0.0.1:{s.port}")
    return (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(hs + list(lines)) + "\r\n\r\n").encode() + body


def run_job(s, body=None):
    st, _, d = jcall(s, "POST", T(s, "api/run"), body or good())
    assert st == 202, d
    jid = d["job_id"]
    for _ in range(1200):                                     # 最多等 120 秒；完成即離開
        st, _, j = jcall(s, "GET", T(s, f"api/jobs/{jid}"))
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# ───────────── 頁面、標頭、靜態資源 ─────────────
def test_index_has_exactly_the_four_blocks_and_safe_headers(srv):
    st, h, d = call(srv, "GET", T(srv))
    html_ = d.decode("utf-8")
    assert st == 200 and h["content-type"].startswith("text/html")
    assert h["content-security-policy"] == W.UI_CSP and "unsafe-inline" not in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
    assert h["referrer-policy"] == "no-referrer" and h["x-frame-options"] == "DENY"
    assert h["cross-origin-resource-policy"] == "same-origin" and h["connection"] == "close"
    for n in ("① 尺寸輸入", "② 規範勾選", "③ 執行", "④ 結果下載"):
        assert n in html_
    assert html_.count("<section") == 4 and 'lang="zh-Hant"' in html_
    assert re.findall(r"<script[^>]*>", html_) == ['<script src="app.js">'] and "</script><" not in html_.replace(
        '<script src="app.js"></script></body>', "")
    assert " style=" not in html_ and "onclick" not in html_ and "javascript:" not in html_
    assert "<noscript>" in html_ and BANNER_FIXED in html_


def test_every_control_has_a_unique_explicit_label_and_checkboxes_follow_rules(srv):
    html_ = call(srv, "GET", T(srv))[2].decode("utf-8")
    ids = re.findall(r'<(?:input|select)[^>]*?id="([^"]+)"', html_)
    assert len(ids) == len(set(ids)) and len(ids) >= 25
    names = {}
    for i in ids:
        m = re.search(rf'<label for="{re.escape(i)}">([^<]+)</label>', html_)
        assert m, f"{i} 沒有明確的 <label for>"
        names[i] = m.group(1)
    assert len(set(names.values())) == len(names), "無障礙名稱重複"         # 例如不得有多個「X (m)」
    assert "<label><input" not in html_                                      # 不用「包住輸入框」的標籤
    for prefix, title in (("room", "房間"), ("start", "起點"), ("ends0", "終點 1")):
        assert [names[f"{prefix}_{a}"] for a in "xyz"] == [f"{title} {a} (m)" for a in "XYZ"]
    assert names["cell_m"] == "格距" and names["tray_kind_power"] == "電力橋架" and names["tray_kind_signal"] == "訊號橋架"
    for code, info in load_rules()["codes"].items():
        assert f'<label for="code_{code}">{code}：' in html_ and f'value="{code}"' in html_
    assert 'role="status"' in html_ and 'aria-live="polite"' in html_ and 'role="alert"' in html_


def test_render_index_escapes_rule_code_names():
    rules = load_rules()
    rules["codes"]['X"><img src=x onerror=1>'] = rules["codes"]["CNS"]
    html_ = W.render_index("tok", rules)
    assert "<img" not in html_ and "&lt;img" in html_


def test_static_assets_and_js_hygiene(srv):
    st, h, js = call(srv, "GET", T(srv, "app.js"))
    assert st == 200 and h["content-type"].startswith("application/javascript") and h["x-content-type-options"] == "nosniff"
    text = js.decode("utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write", "setTimeout(\"",
                   "http://", "https://", "XMLHttpRequest"):
        assert banned not in text, banned
    assert "textContent" in text
    st, h, css = call(srv, "GET", T(srv, "app.css"))
    assert st == 200 and h["content-type"].startswith("text/css") and b"@import" not in css and b"url(" not in css


# ───────────── token / Host / Origin ─────────────
def test_wrong_or_missing_token_is_indistinguishable_from_unknown_route(srv):
    base = call(srv, "GET", T(srv, "nope"))
    assert base[0] == 404
    for path in ("/", "/t/", "/t/wrong/", "/t/wrong/api/versions", f"/t/{srv.token}x/", f"/T/{srv.token}/",
                 "/api/versions", f"/{srv.token}/", "/t/" + srv.token[:-1] + "/"):
        st, h, d = call(srv, "GET", path)
        assert st == 404 and d == base[2], path


@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1:1", "127.0.0.1", "localhost", "[::1]:80", "", "127.0.0.1.evil.com"])
def test_bad_host_header_is_forbidden_even_with_the_right_token(srv, host):
    st, d = (call(srv, "GET", T(srv), host=host)[0], None)
    assert st == 403


def test_localhost_host_and_origin_are_accepted(srv):
    p = srv.port
    assert call(srv, "GET", T(srv), host=f"localhost:{p}", origin=f"http://localhost:{p}")[0] == 200


def test_missing_host_header_is_rejected(srv):
    st, _, _ = raw(srv, rawreq(srv, [], method="GET", path=T(srv), with_host=False, with_origin=False))
    assert st == 403


@pytest.mark.parametrize("origin", [None, "http://evil.com", "null", "https://127.0.0.1:{p}", "http://127.0.0.1:1",
                                    "http://127.0.0.1", "http://127.0.0.1.evil.com:{p}"])
def test_post_requires_exact_origin(srv, origin):
    o = origin.format(p=srv.port) if origin else None
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), good(), origin=o)
    assert st == 403 and d["error"]["code"] == "bad_origin"
    assert srv.jobs.running() is None and not srv.jobs.jobs


def test_cross_site_fetch_metadata_is_rejected_for_get_and_post(srv):
    assert call(srv, "GET", T(srv), headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(srv, "POST", T(srv, "api/run"), good(), headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(srv, "GET", T(srv), headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
    assert call(srv, "GET", T(srv), headers={"Sec-Fetch-Site": "none"})[0] == 200


def test_get_with_foreign_origin_is_rejected(srv):
    assert call(srv, "GET", T(srv), origin="http://evil.com")[0] == 403
    assert call(srv, "GET", T(srv), origin=None)[0] == 200


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_other_methods_are_405_with_allow(srv, method):
    st, h, _ = call(srv, method, T(srv))
    assert st == 405 and h["allow"] == "GET, POST"


# ───────────── 請求本文處理（原始 socket）─────────────
def test_content_type_must_be_json_but_charset_parameter_is_fine(srv):
    body = json.dumps(good()).encode()
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), body, headers={"Content-Type": "text/plain"})
    assert st == 415 and d["error"]["code"] == "unsupported_media_type"
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), body, headers={"Content-Type": "application/json; charset=utf-8"})
    assert st == 202, (st, d, srv.jobs.running(), list(srv.log_lines)[-5:])
    srv.jobs.join(120)


def test_missing_content_length_is_411(srv):
    st, _, body = raw(srv, rawreq(srv, ["Content-Type: application/json"]))
    assert st == 411 and json.loads(body)["error"]["code"] == "length_required"


def test_transfer_encoding_is_always_rejected_even_with_content_length(srv):
    payload = b'{"a":1}'
    for extra in (["Transfer-Encoding: chunked"], ["Transfer-Encoding: identity"],
                  ["Transfer-Encoding: chunked", f"Content-Length: {len(payload)}"]):
        st, _, _ = raw(srv, rawreq(srv, ["Content-Type: application/json"] + extra, payload))
        assert st == 400
    assert not srv.jobs.jobs


def test_expect_100_continue_is_417(srv):
    st, _, _ = raw(srv, rawreq(srv, ["Content-Type: application/json", "Expect: 100-continue", "Content-Length: 2"], b"{}"))
    assert st == 417


@pytest.mark.parametrize("cl", [["Content-Length: -1"], ["Content-Length: abc"], ["Content-Length: 1e3"], ["Content-Length: 0x10"],
                                ["Content-Length: 1234567890123"], ["Content-Length: "], ["Content-Length: +5"]])
def test_malformed_content_length_is_rejected(srv, cl):
    st, _, _ = raw(srv, rawreq(srv, ["Content-Type: application/json"] + cl, json.dumps(good()).encode()))
    assert st in (400, 413)
    assert not srv.jobs.jobs


@pytest.mark.parametrize("second", ["{n}", "{m}", "0"])
def test_duplicate_content_length_headers_are_rejected_even_with_a_valid_body(srv, second):
    body = json.dumps(good()).encode()                       # 合法本文：若重複標頭被放行，就會真的建立工作
    n = len(body)
    hdrs = ["Content-Type: application/json", f"Content-Length: {n}", "Content-Length: " + second.format(n=n, m=n + 1)]
    st, _, resp = raw(srv, rawreq(srv, hdrs, body))
    assert st == 400 and json.loads(resp)["error"]["code"] == "bad_content_length"
    assert not srv.jobs.jobs and srv.jobs.running() is None


def test_oversized_body_is_413_without_reading_it(srv):
    st, _, body = raw(srv, rawreq(srv, ["Content-Type: application/json", f"Content-Length: {W.MAX_BODY + 1}"]))
    assert st == 413 and json.loads(body)["error"]["code"] == "payload_too_large"


def test_body_shorter_than_content_length_is_400_not_a_hang(srv):
    t0 = time.time()
    st, _, body = raw(srv, rawreq(srv, ["Content-Type: application/json", "Content-Length: 100"], b'{"a":'), shut_wr=True)
    assert st == 400 and json.loads(body)["error"]["code"] == "short_body" and time.time() - t0 < 30            # 意圖：不卡住（逾時門檻放寬以容納 CPU 被占滿）


def test_bytes_after_the_declared_body_are_not_treated_as_another_request(srv):
    body = json.dumps(good()).encode()
    smuggled = rawreq(srv, [], method="GET", path=T(srv, "api/versions"), with_origin=False)
    first = rawreq(srv, ["Content-Type: application/json", f"Content-Length: {len(body)}"], body)
    raw(srv, first + smuggled)
    srv.jobs.join(120)
    lines = [l for l in srv.log_lines if l.startswith(("POST", "GET"))]
    assert [l.split()[0] for l in lines] == ["POST"]                    # 只處理了一個請求；連線隨後關閉


def test_every_response_closes_the_connection(srv):
    for path in (T(srv), T(srv, "app.js"), "/nope", T(srv, "api/versions")):
        assert call(srv, "GET", path)[1]["connection"] == "close"


@pytest.mark.parametrize("payload", [b"not json", b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b'{"a":1,"a":2}', b"[]",
                                     b'"str"', b"\xff\xfe\x00", b"", b"{" * 50 + b"}" * 50,
                                     b'{"a":' + b"[" * 30 + b"]" * 30 + b"}"])
def test_bad_json_bodies_are_400(srv, payload):
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), payload, headers={"Content-Type": "application/json"})
    assert st == 400 and d["error"]["code"] == "bad_json"


# ───────────── 欄位驗證 ─────────────
def mutate(path, value):
    d = copy.deepcopy(good())
    cur = d
    keys = re.findall(r"[^.\[\]]+", path)
    for k in keys[:-1]:
        cur = cur[int(k)] if k.isdigit() else cur[k]
    last = keys[-1]
    cur[int(last) if last.isdigit() else last] = value
    return d


def expect_field(body, field=None, code=None):
    with pytest.raises(W.ApiError) as ei:
        W.validate_request(body)
    assert ei.value.status == 400
    if field is not None:
        assert ei.value.field == field, (ei.value.field, ei.value.message)
    if code is not None:
        assert ei.value.code == code
    return ei.value


NUMERIC = [("room.x", 0.5, 200), ("room.y", 0.5, 200), ("room.z", 0.5, 200), ("tray.width_mm", 50, 2000),
           ("tray.height_mm", 20, 500), ("start.x", 0, 12), ("start.y", 0, 6), ("start.z", 0, 4),
           ("ends[0].x", 0, 12), ("ends[0].y", 0, 6), ("ends[0].z", 0, 4), ("cable.od_mm", 1, 200)]


@pytest.mark.parametrize("path,lo,hi", NUMERIC)
def test_every_numeric_field_rejects_bool_string_null_nan_range(path, lo, hi):
    for bad in (True, False, "5", None, [], {}, float("nan"), float("inf")):
        expect_field(mutate(path, bad), path)
    eps = 0.01
    expect_field(mutate(path, lo - eps), path, "out_of_range")
    expect_field(mutate(path, hi + eps), path, "out_of_range")
    expect_field(mutate(path, -1e9), path)
    expect_field(mutate(path, 1e9), path)


def test_numeric_boundaries_are_inclusive_and_valid_values_pass():
    for path, lo, hi in NUMERIC:
        if path.startswith(("start", "ends")) or path.startswith("room"):
            continue
        W.validate_request(mutate(path, lo))
        W.validate_request(mutate(path, hi))
    inp, codes = W.validate_request(good())
    assert inp.room == ((0, 0, 0), (12, 6, 4)) and codes == ["CNS", "MRT_APPX_C"] and inp.cell_m == 0.25


def test_numbers_are_rounded_to_a_millimetre_before_use():
    body = mutate("start.x", 1.0000004)
    body["start"]["y"] = 0.30000000000000004
    inp, _ = W.validate_request(body)
    assert inp.start == (1.0, 0.3, 3.0)
    assert W.validate_request(mutate("tray.width_mm", 300.0004))[0].tray_w_mm == 300.0


def test_cable_count_must_be_a_real_integer_and_partial_cable_is_rejected():
    for bad in (True, 3.5, 3.0, "3", None, 0, 1001, -1):
        expect_field(mutate("cable.count", bad), "cable.count")
    expect_field({**good(), "cable": {"od_mm": 20}}, "cable.count", "missing_field")
    expect_field({**good(), "cable": {"od_mm": 20, "count": 1, "kind": "x"}}, None, "unknown_field")
    d = good()
    del d["cable"]
    inp, _ = W.validate_request(d)
    assert inp.cables == [] and inp.cables_defaulted


def test_structure_errors_name_the_offending_field():
    for k in ("room", "tray", "start", "ends", "codes"):
        d = good()
        del d[k]
        expect_field(d, k, "missing_field")
    expect_field({**good(), "extra": 1}, None, "unknown_field")
    expect_field(mutate("room", [1, 2, 3]), "room", "invalid_type")
    expect_field(mutate("room", {"x": 1, "y": 1}), "room.z", "missing_field")
    expect_field(mutate("room", {"x": 1, "y": 1, "z": 1, "w": 1}), None, "unknown_field")
    expect_field(mutate("tray.kind", "metal"), "tray.kind", "invalid_choice")
    expect_field(mutate("ends", []), "ends")
    expect_field(mutate("ends", [good()["ends"][0]] * (W.MAX_END_POINTS + 1)), "ends")
    expect_field(mutate("ends", "x"), "ends")
    expect_field(mutate("start", [1, 1, 3]), "start")


def test_end_and_start_points_must_be_inside_the_room():
    expect_field(mutate("start.z", 4.01), "start.z", "out_of_range")
    expect_field(mutate("ends[0].x", 12.5), "ends[0].x", "out_of_range")
    two = mutate("ends", [good()["ends"][0], {"x": 5, "y": 7, "z": 1}])
    expect_field(two, "ends[1].y", "out_of_range")


def test_codes_must_be_known_unique_nonempty_strings():
    for bad in ([], "CNS", None, [1], ["CNS", None], {"a": 1}):
        expect_field(mutate("codes", bad), "codes")
    expect_field(mutate("codes", ["CNS", "nope"]), "codes", "unknown_code")
    expect_field(mutate("codes", ["cns"]), "codes", "unknown_code")                       # 不做大小寫正規化
    assert W.validate_request(mutate("codes", ["CNS", "CNS", "IEC"]))[1] == ["CNS", "IEC"]


def test_cell_must_be_in_the_whitelist():
    for bad in (0.3, 0.01, 0, -0.25, "0.25", True, None, 1):
        expect_field(mutate("cell_m", bad), "cell_m")
    for ok in W.CELLS:
        small = mutate("room", {"x": 3, "y": 3, "z": 3})
        small["start"] = {"x": 1, "y": 1, "z": 1}
        small["ends"] = [{"x": 2, "y": 1, "z": 1}]
        small["cell_m"] = ok
        assert W.validate_request(small)[0].cell_m == ok
    d = good()
    del d["cell_m"]
    assert W.validate_request(d)[0].cell_m == 0.25


def test_fine_cell_is_only_for_small_grids_and_huge_grids_are_refused():
    e = expect_field(mutate("cell_m", 0.05), "cell_m", "cell_too_fine")                  # 12×6×4 @0.05 = 240 萬格
    assert "0.05" in e.message and "500,000" in e.message
    big = mutate("room", {"x": 200, "y": 200, "z": 200})
    big["start"], big["ends"] = {"x": 1, "y": 1, "z": 1}, [{"x": 5, "y": 1, "z": 1}]
    expect_field(big, "cell_m", "grid_too_large")
    from mep_tray.geometry import Box
    from mep_tray.router import grid_cells
    assert grid_cells(Box((0, 0, 0), (60, 60, 60)), 0.05) > W.FINE_CELL_MAX_GRID


def test_web_limits_match_the_router_limit_and_use_its_grid_formula():
    import inspect
    from mep_tray import router
    assert W.MAX_GRID_CELLS == inspect.signature(router.route_tray).parameters["max_cells"].default
    assert W.MAX_OBSTACLE_WORK == router.MAX_OBSTACLE_WORK if hasattr(W, "MAX_OBSTACLE_WORK") else True
    assert "grid_cells" in inspect.getsource(W.validate_request)                           # 不自己重寫網格公式


def obstacle(**kw):
    o = {"name": "pipe", "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}
    o.update(kw)
    return o


def test_obstacle_validation_cases():
    ok = W.validate_request({**good(), "obstacles": [obstacle()]})[0]
    assert ok.obstacles == [obstacle()] or ok.obstacles[0]["name"] == "pipe"
    cases = [
        ([obstacle()] * (W.MAX_OBSTACLES + 1), "obstacles", "too_many_obstacles"),
        ([obstacle(name="x" * 101)], "obstacles[0].name", "invalid_name"),
        ([obstacle(name="")], "obstacles[0].name", "invalid_name"),
        ([obstacle(name="   ")], "obstacles[0].name", "invalid_name"),
        ([obstacle(name="a" + chr(0) + "b")], "obstacles[0].name", "invalid_name"),
        ([obstacle(name="a" + chr(0x202E) + "b")], "obstacles[0].name", "invalid_name"),
        ([obstacle(name="a\r\nb")], "obstacles[0].name", "invalid_name"),
        ([obstacle(name=5)], "obstacles[0].name", "invalid_name"),
        ([obstacle(kind="lava")], "obstacles[0].kind", "invalid_choice"),
        ([obstacle(lo=[1, 2])], "obstacles[0].lo", "invalid_type"),
        ([obstacle(hi="x")], "obstacles[0].hi", "invalid_type"),
        ([obstacle(lo=[5, 0, True])], "obstacles[0].lo[2]", "invalid_number"),
        ([obstacle(hi=[5.3, 4, "3"])], "obstacles[0].hi[2]", "invalid_number"),
        ([obstacle(lo=[5, 0, 0], hi=[5, 4, 3])], "obstacles[0]", "invalid_box"),
        ([obstacle(lo=[6, 0, 0], hi=[5, 4, 3])], "obstacles[0]", "invalid_box"),
        ([obstacle(lo=[-1001, 0, 0])], "obstacles[0].lo[0]", "out_of_range"),
        ([obstacle(extra=1)], None, "unknown_field"),
        ([{"name": "x", "kind": "water", "lo": [0, 0, 0]}], "obstacles[0].hi", "missing_field"),
        (["str"], "obstacles[0]", "invalid_type"),
        ("x", "obstacles", "invalid_type"),
    ]
    for obs, field, code in cases:
        expect_field({**good(), "obstacles": obs}, field, code)


def test_whole_room_obstacle_is_a_400_before_any_job_is_created(srv):
    body = {"room": {"x": 30, "y": 15, "z": 5}, "tray": {"width_mm": 300, "height_mm": 100, "kind": "power"},
            "start": {"x": 1, "y": 7, "z": 3}, "ends": [{"x": 29, "y": 7, "z": 3}], "codes": ["CNS", "MRT_APPX_C"],
            "cell_m": 0.1, "obstacles": [obstacle(name="whole", lo=[-1, -1, -1], hi=[31, 16, 6])]}
    t0 = time.time()
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), body)
    assert st == 400 and d["error"]["code"] == "obstacle_work_limit" and d["error"]["field"] == "obstacles"
    assert time.time() - t0 < 15.0 and not srv.jobs.jobs and srv.jobs.running() is None      # 意圖：不是 46 秒


def test_http_validation_error_shape(srv):
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), mutate("room.x", True))
    assert st == 400 and set(d["error"]) == {"code", "field", "message"} and d["error"]["field"] == "room.x"
    assert not srv.jobs.jobs


# ───────────── 工作 / 下載（真管線，cell 0.25）─────────────
def test_successful_run_end_to_end_and_downloads_match_the_manifest(srv):
    j = run_job(srv)
    assert j["state"] == "done" and [f["kind"] for f in j["files"]] == ["dxf", "revit_json", "report", "manifest"]
    assert BANNER_FIXED in j["banners"] and SCOPE_NOTE in j["banners"]
    rid = j["run_id"]
    lr = V.load_version(rid)
    assert lr.ok
    for f in j["files"]:
        st, h, d = call(srv, "GET", f["href"])
        assert st == 200 and h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
        assert h["content-type"] == W.KIND_TYPE[f["kind"]]
        if f["kind"] == "manifest":
            assert d == (Path(srv.jobs.token and V.output_root() / rid / "manifest.json")).read_bytes()
        else:
            import hashlib
            assert hashlib.sha256(d).hexdigest() == lr.manifest["files"][f["kind"]]["sha256"]
            assert f["sha8"] == lr.manifest["files"][f["kind"]]["sha256"][:8]


def test_download_headers_report_is_inline_and_sandboxed_others_are_attachments(srv):
    j = run_job(srv)
    by = {f["kind"]: f["href"] for f in j["files"]}
    st, h, _ = call(srv, "GET", by["report"])
    assert h["content-security-policy"] == W.DOWNLOAD_CSP and "sandbox" in h["content-security-policy"]
    assert h["content-disposition"].startswith('inline; filename="report_')
    for kind in ("dxf", "revit_json", "manifest"):
        st, h, _ = call(srv, "GET", by[kind])
        assert h["content-disposition"].startswith("attachment; filename=")
        assert "sandbox" in h["content-security-policy"]
        fn = re.search(r'filename="([^"]+)"', h["content-disposition"]).group(1)
        assert re.fullmatch(r"[A-Za-z0-9._-]+", fn)


@pytest.mark.parametrize("seg", ["..", "%2e%2e", "%2E%2E", "a%2fb", "..%5c..", "%00", "x" * 300, "con", "", "a%20b", "%E4%B8%AD%E6%96%87",
                                 "20260101-000000-abcdef12/../..", "%252e%252e"])
def test_download_path_traversal_and_junk_run_ids_are_404(srv, seg):
    j = run_job(srv)
    for kind in ("dxf", "manifest", "report"):
        assert call(srv, "GET", T(srv, f"download/{seg}/{kind}"))[0] == 404
    assert call(srv, "GET", T(srv, f"download/{j['run_id']}/{seg}"))[0] == 404


def test_unknown_kinds_and_directory_listing_are_404(srv):
    j = run_job(srv)
    rid = j["run_id"]
    for kind in ("exe", "dxf/../manifest", "DXF", "tray_x.dxf", "manifest.json", "..%2fmanifest", "report/"):
        assert call(srv, "GET", T(srv, f"download/{rid}/{kind}"))[0] == 404, kind
    for path in ("download", "download/", f"download/{rid}", f"download/{rid}/", "api", "api/", "diff", "diff/x",
                 f"diff/{rid}", "output", "output/"):
        assert call(srv, "GET", T(srv, path))[0] == 404, path


def test_unknown_run_corrupt_version_and_old_version_without_report(srv):
    j = run_job(srv)
    rid = j["run_id"]
    assert call(srv, "GET", T(srv, "download/20250101-000000-deadbeef/dxf"))[0] == 404
    d = V.output_root() / rid
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    del m["files"]["report"]
    (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")                     # 模擬舊版：沒有 report 項
    assert call(srv, "GET", T(srv, f"download/{rid}/dxf"))[0] == 200
    assert call(srv, "GET", T(srv, f"download/{rid}/revit_json"))[0] == 200
    assert call(srv, "GET", T(srv, f"download/{rid}/report"))[0] == 404
    (d / "manifest.json").write_text("garbage", encoding="utf-8")
    for kind in ("dxf", "manifest", "report"):
        assert call(srv, "GET", T(srv, f"download/{rid}/{kind}"))[0] == 404


def test_symlinked_output_file_is_not_served(srv, tmp_path):
    import os
    j = run_job(srv)
    d = V.output_root() / j["run_id"]
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    name = json.loads((d / "manifest.json").read_text(encoding="utf-8"))["files"]["dxf"]["name"]
    (d / name).unlink()
    try:
        os.symlink(secret, d / name)
    except (OSError, NotImplementedError):
        pytest.skip("無法建立 symlink")
    st, _, body = call(srv, "GET", T(srv, f"download/{j['run_id']}/dxf"))
    assert st == 404 and b"TOP SECRET" not in body


def test_second_run_links_a_diff_report_to_the_previous_version(srv):
    a = run_job(srv)
    assert a["diff_href"] is None
    body = good()
    body["tray"]["width_mm"] = 400
    b = run_job(srv, body)
    assert b["state"] == "done" and b["diff_href"].endswith(f"/diff/{a['run_id']}/{b['run_id']}")
    st, h, d = call(srv, "GET", b["diff_href"])
    text = d.decode("utf-8")
    assert st == 200 and "sandbox" in h["content-security-policy"] and "輸入變更" in text
    assert call(srv, "GET", T(srv, "diff/../x"))[0] == 404 and call(srv, "GET", T(srv, f"diff/{a['run_id']}/%2e%2e"))[0] == 404
    st, _, d = call(srv, "GET", T(srv, f"diff/{a['run_id']}/20250101-000000-deadbeef"))
    assert st == 200 and "拒絕比對".encode() in d


def test_versions_api_lists_recent_versions_and_ignored_counts(srv):
    run_job(srv)
    (V.output_root() / "revit2025_gui").mkdir()
    (V.output_root() / (P.STAGE_PREFIX + "zz")).mkdir()
    st, _, d = jcall(srv, "GET", T(srv, "api/versions"))
    assert st == 200 and len(d["versions"]) == 1 and d["ignored"] == 1 and d["stage_leftovers"] == 1
    assert d["versions"][0]["error"] is None


def test_versions_api_is_capped_at_ten(srv, monkeypatch):
    class VI:
        def __init__(self, i):
            self.i = i

        def to_dict(self):
            return {"run_id": f"r{self.i:03d}", "created_at": "", "error": None}
    monkeypatch.setattr(W.V, "list_versions", lambda: [VI(i) for i in range(25)])
    assert [v["run_id"] for v in W.recent_versions()] == [f"r{i:03d}" for i in range(24, 14, -1)]


# ───────────── 單一工作者、錯誤、清理 ─────────────
def test_second_post_while_busy_is_409_and_first_still_completes(srv, monkeypatch):
    gate = threading.Event()
    real = W.P.run

    def slow(*a, **k):
        assert gate.wait(90)
        return real(*a, **k)
    monkeypatch.setattr(W.P, "run", slow)
    st, _, d = jcall(srv, "POST", T(srv, "api/run"), good())
    assert st == 202
    jid = d["job_id"]
    st2, _, d2 = jcall(srv, "POST", T(srv, "api/run"), good())
    assert st2 == 409 and d2["error"]["code"] == "busy"
    assert jcall(srv, "GET", T(srv, f"api/jobs/{jid}"))[2]["state"] == "running"
    gate.set()
    srv.jobs.join(120)
    assert jcall(srv, "GET", T(srv, f"api/jobs/{jid}"))[2]["state"] == "done"
    assert jcall(srv, "POST", T(srv, "api/run"), good())[0] == 202                          # 完成後可再執行
    srv.jobs.join(120)


def test_simultaneous_posts_exactly_one_is_accepted(srv, monkeypatch):
    gate = threading.Event()
    real = W.P.run
    monkeypatch.setattr(W.P, "run", lambda *a, **k: (gate.wait(90), real(*a, **k))[1])
    codes, barrier = [], threading.Barrier(6)

    def go():
        barrier.wait()
        codes.append(jcall(srv, "POST", T(srv, "api/run"), good())[0])
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [202, 409, 409, 409, 409, 409]
    gate.set()
    srv.jobs.join(120)


def test_pipeline_failure_becomes_human_readable_error_and_leaves_nothing(srv):
    body = {**good(), "obstacles": [obstacle(name="slab", kind="structure", lo=[5, -1, -1], hi=[5.3, 7, 5])]}
    j = run_job(srv, body)
    e = j["error"]
    assert j["state"] == "error" and (e["stage"], e["code"]) == ("route", "no_route")
    assert e["title"] == "找不到可行路徑" and "障礙物" in e["hint"] and e["cleanup_failed"] is False
    root = V.output_root()                                                  # 失敗的執行不得建立任何版本或暫存
    assert not root.exists() or not any(root.iterdir())


def test_internal_crash_is_reported_without_traceback_or_paths(srv, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError(f"secret detail at {Path.home()}")
    monkeypatch.setattr(W.P, "run", boom)
    j = run_job(srv)
    e = j["error"]
    assert e["code"] == "internal_error" and e["message"] == "RuntimeError"
    assert str(Path.home()) not in json.dumps(j) and "secret detail" not in json.dumps(j)
    assert srv.jobs.running() is None and jcall(srv, "POST", T(srv, "api/run"), good())[0] == 202
    srv.jobs.join(120)


def test_run_id_collision_is_retried_with_a_new_id(srv, monkeypatch):
    first = run_job(srv)
    taken = first["run_id"]
    ids = iter([taken, taken, "20990101-000000-aaaaaaaa"])
    monkeypatch.setattr(W.V, "new_run_id", lambda *a, **k: next(ids))
    j = run_job(srv)
    assert j["state"] == "done" and j["run_id"] == "20990101-000000-aaaaaaaa"


def test_human_error_mapping_covers_all_known_codes_and_cleanup_hint():
    for (stage, code), (title, hint) in W.HUMAN.items():
        h = W.human_error(P.PipelineError(stage, code, "m"))
        assert h["title"] == title and h["hint"] == hint and h["stage"] == stage and h["code"] == code
    h = W.human_error(P.PipelineError("io", "os_error", "m", cleanup_failed=True))
    assert "暫存" in h["hint"] and h["cleanup_failed"] is True
    assert W.human_error(P.PipelineError("zzz", "yyy", "m"))["title"] == "執行失敗"


def test_job_status_route_validates_ids_and_keeps_only_recent_jobs(srv):
    for bad in ("x", "../x", "0" * 15, "G" * 16, "0" * 17, "0" * 16):
        assert call(srv, "GET", T(srv, f"api/jobs/{bad}"))[0] == 404
    srv.jobs.keep = 3
    for _ in range(5):
        run_job(srv)
    assert len(srv.jobs.jobs) == 3


# ───────────── 伺服器層 ─────────────
def test_binds_only_to_loopback_and_refuses_other_hosts(srv):
    assert srv.server_address[0] == "127.0.0.1"
    for host in ("0.0.0.0", "", "::", "192.168.1.5", "localhost"):
        with pytest.raises(ValueError):
            W.WebServer(0, "t", False, host)
    with pytest.raises(ValueError):
        W.make_server(0, host="0.0.0.0")


def test_busy_preferred_port_falls_back_to_a_free_port(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        busy = blocker.getsockname()[1]
        s = W.make_server(busy, make_dwg=False)
        try:
            assert s.port != busy and s.port > 0
        finally:
            s.server_close()
    finally:
        blocker.close()


def test_tokens_differ_per_start_are_long_and_old_tokens_stop_working(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    s1, s2 = W.make_server(0, make_dwg=False), W.make_server(0, make_dwg=False)
    try:
        assert s1.token != s2.token and len(s1.token) >= 43 and re.fullmatch(r"[A-Za-z0-9_-]+", s1.token)
        threading.Thread(target=s2.serve_forever, daemon=True).start()
        assert call(s2, "GET", f"/t/{s1.token}/")[0] == 404 and call(s2, "GET", s2.url[s2.url.index("/t/"):])[0] == 200
    finally:
        s1.server_close()
        s2.close()


def test_connection_cap_returns_503_beyond_the_limit_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setattr(W, "MAX_CONNECTIONS", 3)
    s = W.make_server(0, make_dwg=False)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    held = []
    try:
        for _ in range(3):                                    # 占滿 3 個連線（不送任何資料，處理緒會卡在讀請求行）
            k = socket.create_connection(("127.0.0.1", s.port), timeout=5)
            held.append(k)
        assert wait_until(lambda: s._sem._value == 0), "3 個連線尚未占滿處理名額"           # 事件驅動：名額真的用完才送第 4 條
        st, head, _ = raw(s, rawreq(s, [], method="GET", path=f"/t/{s.token}/", with_origin=False), timeout=15)
        assert st == 503 and b"Connection: close" in head
        for k in held:
            k.close()
        assert wait_until(lambda: s._sem._value == 3), "放開連線後名額沒有恢復"
        assert call(s, "GET", f"/t/{s.token}/")[0] == 200
    finally:
        for k in held:
            k.close()
        s.close()
    assert W.MAX_CONNECTIONS == 3


def test_default_limits_are_the_documented_ones():
    assert (W.MAX_BODY, W.MAX_CONNECTIONS, W.SOCKET_TIMEOUT_S, W.MAX_OBSTACLES) == (1_048_576, 16, 15, 200)
    assert W.Handler.timeout == W.SOCKET_TIMEOUT_S and W.Handler.protocol_version == "HTTP/1.0"


def test_half_sent_request_is_dropped_after_the_socket_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setattr(W.Handler, "timeout", 1)
    s = W.make_server(0, make_dwg=False)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        k = socket.create_connection(("127.0.0.1", s.port), timeout=6)
        k.sendall(b"GET /t/x/ HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: ")
        t0 = time.time()
        assert k.recv(100) == b"" and time.time() - t0 < 30          # 伺服器在逾時後主動斷線
        k.close()
    finally:
        s.close()


def test_close_reports_the_running_job_and_frees_the_port(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    s = W.make_server(0, make_dwg=False)
    th = threading.Thread(target=s.serve_forever, daemon=True)
    th.start()
    gate = threading.Event()
    monkeypatch.setattr(W.P, "run", lambda *a, **k: (gate.wait(60), P.run.__wrapped__(*a, **k))[1]
                        if hasattr(P.run, "__wrapped__") else gate.wait(60))
    jid = s.jobs.submit(*W.validate_request(good()))
    port = s.port
    assert s.close() == jid
    th.join(30)
    assert not th.is_alive()
    gate.set()
    k = socket.socket()
    try:
        k.bind(("127.0.0.1", port))                               # 埠已釋放
    finally:
        k.close()


def test_logs_never_contain_the_token_or_request_bodies(srv, capsys):
    j = run_job(srv)
    call(srv, "GET", T(srv, "app.js"))
    call(srv, "GET", j["files"][0]["href"])
    call(srv, "GET", f"/t/wrong-{srv.token}/")
    text = "\n".join(srv.log_lines)
    cap = capsys.readouterr()
    assert srv.token not in text and srv.token not in cap.out + cap.err
    assert "<token>" in text and "pipe" not in text and "CNS" not in text


def test_no_response_body_contains_absolute_paths(srv):
    j = run_job(srv)
    bodies = [call(srv, "GET", T(srv))[2], call(srv, "GET", T(srv, "api/versions"))[2], json.dumps(j).encode()]
    bodies.append(call(srv, "GET", T(srv, "download/zzz/dxf"))[2])
    bodies.append(call(srv, "POST", T(srv, "api/run"), mutate("room.x", 0))[2])
    import tempfile
    for b in bodies:
        t = b.decode("utf-8", "replace").lower()
        assert str(Path.home()).lower() not in t and tempfile.gettempdir().lower() not in t
        assert not re.search(r"[a-z]:[\\/]", t)


def test_close_before_serving_started_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    s = W.make_server(0, make_dwg=False)
    t0 = time.time()
    assert s.close() is None and time.time() - t0 < 15


def test_main_prints_one_token_url_and_shuts_down_cleanly_on_ctrl_c(tmp_path, monkeypatch, capsys):
    import types
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))

    class FakeThread:                                          # 不啟動真執行緒；is_alive 模擬使用者按 Ctrl+C
        def __init__(self, target=None, daemon=None):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            raise KeyboardInterrupt
    ns = types.SimpleNamespace(**vars(threading))             # 只替換 webui 模組裡的 threading，不影響全域
    ns.Thread = FakeThread
    monkeypatch.setattr(W, "threading", ns)
    assert W.main(["--port", "0"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:\d+/t/[A-Za-z0-9_-]{43,}/", out) and out.count("/t/") == 1
    assert "已關閉" in out


def test_submit_decides_on_obstacle_state_not_on_aria_invalid_that_clearErrors_wipes():
    """瀏覽器實測抓到的缺陷：clearErrors() 先清掉 aria-invalid，之後才用它判斷，壞檔案被靜默忽略後照常執行。"""
    js = W.APP_JS
    assert js.count("obstaclesBad = true") >= 3 and "obstaclesBad = false" in js and "if (obstaclesBad)" in js
    assert 'getAttribute("aria-invalid") === "true"' not in js
    assert js.index("clearErrors(); clear($(\"results\"));") < js.index("if (obstaclesBad)")


# ───────────── 結果區 XY 示意（階段 3 第一刀） ─────────────
def test_xy_preview_fits_room_inside_canvas_and_picks_a_nice_scale_bar():
    room = ((0.0, 0.0, 0.0), (12.0, 6.0, 4.0))
    segs = [((1.0, 1.0, 3.0), (10.0, 1.0, 3.0))]
    p = W.xy_preview(room, segs)
    assert p["caption"] == "示意圖，非施工圖"
    w, h = p["size"]
    x, y, rw, rh = p["room"]
    assert w == 640 and h == 280
    assert 0 <= x < x + rw <= w and 0 <= y < y + rh <= h
    assert abs((rw / rh) - (12 / 6)) < 0.02
    bar = p["scale_bar"]
    assert bar["label"].endswith(" m") and bar["length_px"] > 20
    assert x <= bar["x"] < bar["x"] + bar["length_px"] <= x + rw
    assert y + rh <= bar["y"] <= h
    metres = float(bar["label"].split()[0])
    assert abs(bar["length_px"] / metres - rw / 12.0) < 0.5


def test_xy_preview_puts_model_origin_at_the_bottom_left_of_the_room_rect():
    room = ((0.0, 0.0, 0.0), (10.0, 5.0, 4.0))
    p = W.xy_preview(room, [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))])
    x0, y0, x1, y1 = p["segments"][0]
    rx, ry, rw, rh = p["room"]
    assert abs(x0 - rx) < 1 and abs(x1 - (rx + rw)) < 1
    assert abs(y0 - (ry + rh)) < 1 and abs(y1 - (ry + rh)) < 1


def test_successful_run_payload_includes_xy_preview_of_the_route(srv):
    j = run_job(srv)
    assert j["preview"]["caption"] == "示意圖，非施工圖" and j["preview"]["default_view"] == "xy"
    assert list(j["preview"]["views"]) == ["xy", "xz", "yz"]
    p = j["preview"]["views"]["xy"]
    assert p["caption"] == "示意圖，非施工圖" and len(p["segments"]) >= 1
    assert p["size"] == [640, 280]
    body = json.dumps(j, allow_nan=False)
    assert "D:\\\\" not in body and "/github/" not in body


def test_app_draws_preview_from_job_json_without_html_injection():
    js = W.APP_JS
    assert "d.preview" in js and 'getContext("2d")' in js
    assert "d.preview.caption" in js and "scale_bar" in js
    assert "createElement(\"canvas\")" in js
    assert "innerHTML" not in js
    assert "#preview" in W.APP_CSS or "canvas" in W.APP_CSS


def test_xy_preview_marks_start_and_end_points_in_canvas_pixels():
    room = ((0.0, 0.0, 0.0), (10.0, 5.0, 4.0))
    p = W.xy_preview(room, [((0.0, 0.0, 3.0), (10.0, 0.0, 3.0))], start=(0.0, 5.0, 3.0), ends=[(10.0, 0.0, 3.0)])
    rx, ry, rw, rh = p["room"]
    sx, sy = p["start"]
    (ex, ey), = p["ends"]
    assert abs(sx - rx) < 1 and abs(sy - ry) < 1                 # 模型左上角 (0, 5) → 畫布框左上
    assert abs(ex - (rx + rw)) < 1 and abs(ey - (ry + rh)) < 1    # 模型 (10, 0) → 畫布框右下


def test_xy_preview_rejects_bad_geometry_with_value_error():
    with pytest.raises(ValueError):
        W.xy_preview(((0, 0, 0), (0, 5, 4)), [])
    with pytest.raises(ValueError):
        W.xy_preview(((0, 0, 0), (10, 5, 4)), [((0, 0, 0), (float("nan"), 0, 0))])
    seg = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        W.xy_preview(((0, 0, 0), (10, 5, 4)), [seg] * (W.PREVIEW_MAX_SEGMENTS + 1))


def test_preview_failure_keeps_the_successful_job_done_with_a_text_notice(srv, monkeypatch):
    def boom(*a, **k):
        raise ValueError("x")
    monkeypatch.setattr(W, "view_preview", boom)
    j = run_job(srv)
    assert j["state"] == "done" and j["files"]
    assert j["preview"] is None and j["preview_error"] == W.PREVIEW_UNAVAILABLE


def test_running_job_status_does_not_carry_preview_payload():
    jm = W.JobManager("t" * 32)
    jm.jobs["abc"] = {"state": "running"}
    assert "preview" not in jm.status("abc")
    assert 'd.state === "running"' in W.APP_JS and "drawPreview(box, d);" in W.APP_JS
    assert W.APP_JS.index("drawPreview(box, d);") > W.APP_JS.index("function renderResult(d)")


def test_app_validates_preview_and_falls_back_to_text_and_draws_start_end_scale():
    js = W.APP_JS
    assert "previewOk(pv)" in js and "d.preview_error" in js
    assert "無法顯示示意圖" in js and "此瀏覽器無法繪製示意圖" in js
    assert "起點" in js and "終點" in js and "pv.start" in js and "pv.ends" in js
    assert js.index("XY 平面示意") < js.index('text("h3", "下載")')     # 位於統計表之後、下載清單之前
    assert js.index("box.appendChild(t);") < js.index("drawPreview(box, d);") < js.index('text("h3", "下載")')


# ───────────── 結果區 XY 示意圖層：障礙物／吊架／接頭（階段 3 第二刀） ─────────────
ROOM10 = ((0.0, 0.0, 0.0), (10.0, 5.0, 4.0))
SEG10 = [((0.0, 0.0, 3.0), (10.0, 0.0, 3.0))]


def _px(p, x, y):
    """模型 (x, y) m → 畫布像素（與 xy_preview 相同換算，以 room 框反推）。"""
    rx, ry, rw, rh = p["room"]
    return rx + x * rw / 10.0, ry + (5.0 - y) * rh / 5.0


def test_xy_preview_without_layers_still_has_empty_layer_fields():
    p = W.xy_preview(ROOM10, SEG10)
    assert p["obstacles"] == [] and p["hangers"] == [] and p["joints"] == [] and p["notes"] == []
    assert p["counts"] == {"obstacles": 0, "hangers": 0, "joints": 0}


def test_xy_preview_projects_obstacle_footprints_to_pixel_rects_with_kind_but_no_name():
    obs = [{"name": "秘密名稱", "kind": "water", "lo": [2.0, 1.0, 0.0], "hi": [3.0, 4.0, 3.2]}]
    p = W.xy_preview(ROOM10, SEG10, obstacles=obs)
    (o,) = p["obstacles"]
    assert set(o) == {"kind", "rect"} and o["kind"] == "water"
    x, y, w, h = o["rect"]
    x0, y_top = _px(p, 2.0, 4.0)
    x1, y_bot = _px(p, 3.0, 1.0)
    assert abs(x - x0) < 0.01 and abs(y - y_top) < 0.01 and abs(w - (x1 - x0)) < 0.01 and abs(h - (y_bot - y_top)) < 0.01
    assert "秘密名稱" not in json.dumps(p, ensure_ascii=False)
    assert p["counts"]["obstacles"] == 1


def test_xy_preview_clips_obstacles_to_the_room_and_drops_those_fully_outside():
    obs = [{"name": "a", "kind": "structure", "lo": [-1.0, -1.0, -1.0], "hi": [11.0, 0.5, 5.0]},
           {"name": "b", "kind": "duct", "lo": [20.0, 20.0, 0.0], "hi": [21.0, 21.0, 1.0]},
           {"name": "c", "kind": "lava", "lo": [4.0, 2.0, 0.0], "hi": [5.0, 3.0, 1.0]}]
    p = W.xy_preview(ROOM10, SEG10, obstacles=obs)
    rx, ry, rw, rh = p["room"]
    kinds = [o["kind"] for o in p["obstacles"]]
    assert kinds == ["structure", "other"]                    # 房外整個略過；未知類型以 other 顯示
    x, y, w, h = p["obstacles"][0]["rect"]
    assert abs(x - rx) < 0.01 and abs(x + w - (rx + rw)) < 0.01 and abs(y + h - (ry + rh)) < 0.01
    assert p["counts"]["obstacles"] == 3


def test_xy_preview_caps_obstacles_at_max_obstacles_with_a_note():
    assert W.PREVIEW_MAX_OBSTACLES == W.MAX_OBSTACLES == 200
    obs = [{"name": f"o{i}", "kind": "other", "lo": [1.0, 1.0, 0.0], "hi": [2.0, 2.0, 1.0]} for i in range(250)]
    p = W.xy_preview(ROOM10, SEG10, obstacles=obs)
    assert len(p["obstacles"]) == 200 and p["counts"]["obstacles"] == 250
    assert any("障礙物" in n and "200" in n for n in p["notes"])


def test_xy_preview_maps_hangers_dedupes_vertical_stacks_and_caps_them():
    hs = [(1.0, 0.0, 3.0), (1.0, 0.0, 2.0), (5.0, 0.0, 3.0)]   # 垂直段上的吊架在俯視重疊 → 只畫一次
    p = W.xy_preview(ROOM10, SEG10, hangers=hs)
    assert len(p["hangers"]) == 2 and p["counts"]["hangers"] == 3
    hx, hy = p["hangers"][0]
    ex, ey = _px(p, 1.0, 0.0)
    assert abs(hx - ex) < 0.01 and abs(hy - ey) < 0.01
    many = [(0.001 * i, 0.0, 3.0) for i in range(W.PREVIEW_MAX_HANGERS + 50)]
    p = W.xy_preview(ROOM10, SEG10, hangers=many)
    assert len(p["hangers"]) <= W.PREVIEW_MAX_HANGERS and any("吊架" in n for n in p["notes"])


def test_xy_preview_maps_joints_with_kind_and_caps_them():
    js = [{"kind": "elbow", "point": [10.0, 0.0, 3.0]}, {"kind": "tee", "point": [5.0, 0.0, 3.0]},
          {"kind": "cross", "point": [2.0, 0.0, 3.0]}, {"kind": "union", "point": [1.0, 0.0, 3.0]},
          {"kind": "weird", "point": [3.0, 0.0, 3.0]}]
    p = W.xy_preview(ROOM10, SEG10, joints=js)
    assert [j["kind"] for j in p["joints"]] == ["elbow", "tee", "cross", "union", "unsupported"]
    x, y = p["joints"][0]["pt"]
    ex, ey = _px(p, 10.0, 0.0)
    assert abs(x - ex) < 0.01 and abs(y - ey) < 0.01
    many = [{"kind": "union", "point": [0.001 * i, 0.0, 3.0]} for i in range(W.PREVIEW_MAX_JOINTS + 5)]
    p = W.xy_preview(ROOM10, SEG10, joints=many)
    assert len(p["joints"]) == W.PREVIEW_MAX_JOINTS and p["counts"]["joints"] == W.PREVIEW_MAX_JOINTS + 5
    assert any("接頭" in n for n in p["notes"])


def test_xy_preview_rejects_non_finite_layer_data():
    with pytest.raises(ValueError):
        W.xy_preview(ROOM10, SEG10, hangers=[(float("inf"), 0.0, 0.0)])
    with pytest.raises(ValueError):
        W.xy_preview(ROOM10, SEG10, joints=[{"kind": "elbow", "point": [float("nan"), 0, 0]}])
    with pytest.raises(ValueError):
        W.xy_preview(ROOM10, SEG10, obstacles=[{"kind": "water", "lo": [0, 0, 0], "hi": [float("nan"), 1, 1]}])


def test_preview_joints_come_from_the_same_builder_as_revit_json():
    from mep_tray import export_revit as ER
    from mep_tray.router import Route
    route = Route(waypoints=[], segments=[((0.0, 0.0, 3.0), (5.0, 0.0, 3.0)), ((5.0, 0.0, 3.0), (5.0, 4.0, 3.0))],
                  bends=[(5.0, 0.0, 3.0)], length_m=9.0, hangers=[(1.0, 0.0, 3.0)])
    js = W.preview_joints(route)
    expect = ER.build_joints(ER.split_segments(route))
    assert [j["kind"] for j in js] == [j["kind"] for j in expect] == ["elbow"]
    assert js[0]["point"] == [5.0, 0.0, 3.0]                   # mm → m


class _FakeRes:
    def __init__(self, inputs, route):
        self.inputs, self.route = inputs, route


def _fake_res(obstacles=()):
    from mep_tray.model import Inputs
    from mep_tray.router import Route
    inp = Inputs(room=ROOM10, start=(0.0, 0.0, 3.0), ends=[(10.0, 0.0, 3.0)], obstacles=list(obstacles))
    route = Route(waypoints=[], segments=list(SEG10), bends=[], length_m=10.0, hangers=[(1.0, 0.0, 3.0)])
    return _FakeRes(inp, route)


def test_safe_preview_includes_all_layers_from_request_and_route():
    res = _fake_res([{"name": "p", "kind": "duct", "lo": [4.0, 1.0, 0.0], "hi": [5.0, 2.0, 1.0]}])
    p, err = W.safe_preview(res)
    p = p["views"]["xy"]
    assert err is None and len(p["obstacles"]) == 1 and len(p["hangers"]) == 1 and p["notes"] == []


def test_safe_preview_drops_only_a_broken_layer_and_keeps_the_base_preview(monkeypatch):
    def boom(route):
        raise RuntimeError("x")
    monkeypatch.setattr(W, "preview_joints", boom)
    p, err = W.safe_preview(_fake_res())
    assert err is None and set(p["views"]) == {"xy", "xz", "yz"}
    p = p["views"]["xy"]
    assert p["segments"] and p["joints"] == [] and len(p["hangers"]) == 1
    assert any("接頭" in n for n in p["notes"])


def test_safe_preview_falls_back_to_base_when_layer_data_is_invalid():
    res = _fake_res()
    res.route.hangers = [(float("nan"), 0.0, 3.0)]
    p, err = W.safe_preview(res)
    assert err is None and set(p["views"]) == {"xy", "xz", "yz"}
    p = p["views"]["xy"]
    assert p["segments"] and p["hangers"] == [] and p["obstacles"] == [] and p["joints"] == []
    assert any("圖層" in n for n in p["notes"])


def test_successful_run_with_obstacle_carries_obstacle_hanger_and_joint_layers(srv):
    body = {**good(), "obstacles": [obstacle(name="pipe", kind="water", lo=[5, 3, 0], hi=[5.3, 6, 3.2])]}
    j = run_job(srv, body)
    assert j["state"] == "done", j
    p = j["preview"]["views"]["xy"]
    assert [o["kind"] for o in p["obstacles"]] == ["water"]
    assert p["counts"]["hangers"] == j["summary"]["stats"]["hangers"] and p["hangers"]
    assert p["counts"]["joints"] == j["summary"]["stats"]["joints"]
    assert all(j_["kind"] in ("elbow", "tee", "cross", "union", "unsupported") for j_ in p["joints"])
    assert "pipe" not in json.dumps(j["preview"]) and len(json.dumps(j["preview"])) < 100_000


def test_app_draws_obstacle_hanger_joint_layers_with_traditional_chinese_legend():
    js = W.APP_JS
    for kw in ("pv.obstacles", "pv.hangers", "pv.joints", "pv.notes", "drawObstacles", "drawJoint"):
        assert kw in js, kw
    for kw in ("障礙物", "吊架", "彎頭", "三通", "四通", "直接頭"):
        assert kw in js, kw
    assert "innerHTML" not in js and "eval(" not in js
    # 圖層繪製順序：障礙物在橋架之下、吊架與接頭在橋架之上、起訖點最上
    body = js[js.index("function drawView"):]
    assert body.index("drawObstacles(ctx") < body.index("k < sg.length") < body.index("k < hs.length") \
        < body.index("drawJoint(ctx, jt") < body.index("if (pv.start)")


# ───────────── 結果區 XY／XZ／YZ 視角切換（階段 3 第三刀） ─────────────
def _vpx(p, h, v, room_h, room_v):
    """模型 (水平軸, 垂直軸) m → 畫布像素（原點在房間框左下；垂直軸向上）。"""
    rx, ry, rw, rh = p["room"]
    return rx + h * rw / room_h, ry + (room_v - v) * rh / room_v


def test_view_preview_xy_is_the_same_as_xy_preview():
    obs = [{"name": "n", "kind": "water", "lo": [2.0, 1.0, 0.0], "hi": [3.0, 4.0, 3.2]}]
    kw = dict(start=(0.0, 0.0, 3.0), ends=[(10.0, 0.0, 3.0)], obstacles=obs, hangers=[(1.0, 0.0, 3.0)],
              joints=[{"kind": "elbow", "point": [10.0, 0.0, 3.0]}])
    a, b = W.xy_preview(ROOM10, SEG10, **kw), W.view_preview(ROOM10, SEG10, "xy", **kw)
    assert a == b and a["view"] == "xy" and a["axes"] == ["X", "Y"]


def test_view_preview_xz_puts_x_right_and_z_up_and_fits_the_room_elevation():
    p = W.view_preview(ROOM10, SEG10, "xz", start=(0.0, 0.0, 3.0), ends=[(10.0, 0.0, 3.0)])
    assert p["view"] == "xz" and p["axes"] == ["X", "Z"] and p["size"] == [640, 280]
    rx, ry, rw, rh = p["room"]
    assert 0 <= rx < rx + rw <= 640 and 0 <= ry < ry + rh <= 280
    assert abs(rw / rh - 10 / 4) < 0.02                                # 立面寬高比 = X : Z
    x0, y0, x1, y1 = p["segments"][0]
    ex0, ey0 = _vpx(p, 0.0, 3.0, 10.0, 4.0)
    ex1, ey1 = _vpx(p, 10.0, 3.0, 10.0, 4.0)
    assert max(abs(x0 - ex0), abs(y0 - ey0), abs(x1 - ex1), abs(y1 - ey1)) < 0.01
    sx, sy = p["start"]
    assert abs(sx - ex0) < 0.01 and abs(sy - ey0) < 0.01
    assert p["scale_bar"]["label"].endswith(" m") and rx <= p["scale_bar"]["x"] and p["scale_bar"]["y"] >= ry + rh


def test_view_preview_yz_puts_y_right_and_z_up():
    seg = [((2.0, 1.0, 3.0), (2.0, 4.0, 3.0))]                         # 沿 Y 的段
    p = W.view_preview(ROOM10, seg, "yz")
    assert p["view"] == "yz" and p["axes"] == ["Y", "Z"]
    rx, ry, rw, rh = p["room"]
    assert abs(rw / rh - 5 / 4) < 0.02
    x0, y0, x1, y1 = p["segments"][0]
    ex0, ey0 = _vpx(p, 1.0, 3.0, 5.0, 4.0)
    ex1, ey1 = _vpx(p, 4.0, 3.0, 5.0, 4.0)
    assert max(abs(x0 - ex0), abs(y0 - ey0), abs(x1 - ex1), abs(y1 - ey1)) < 0.01


def test_segments_perpendicular_to_the_view_plane_become_dots():
    segs = [((1.0, 1.0, 0.5), (1.0, 1.0, 3.0)),                        # 垂直（Z）
            ((1.0, 1.0, 3.0), (1.0, 4.0, 3.0)),                        # 沿 Y
            ((1.0, 4.0, 3.0), (8.0, 4.0, 3.0))]                        # 沿 X

    def dots(view):
        p = W.view_preview(ROOM10, segs, view)
        return [s[0] == s[2] and s[1] == s[3] for s in p["segments"]]
    assert dots("xy") == [True, False, False]
    assert dots("xz") == [False, True, False]
    assert dots("yz") == [False, False, True]


def test_view_preview_projects_and_clips_obstacles_in_each_view_plane():
    obs = [{"name": "秘密", "kind": "water", "lo": [2.0, 1.0, 1.0], "hi": [3.0, 4.0, 9.0]},   # Z 超出房高 → 裁到 4
           {"name": "高處", "kind": "duct", "lo": [5.0, 1.0, 5.0], "hi": [6.0, 2.0, 6.0]}]    # 整個在房高之上
    xy = W.view_preview(ROOM10, SEG10, "xy", obstacles=obs)
    assert [o["kind"] for o in xy["obstacles"]] == ["water", "duct"]   # 俯視不分高度
    xz = W.view_preview(ROOM10, SEG10, "xz", obstacles=obs)
    assert [o["kind"] for o in xz["obstacles"]] == ["water"] and xz["counts"]["obstacles"] == 2
    x, y, w, h = xz["obstacles"][0]["rect"]
    x0, y_top = _vpx(xz, 2.0, 4.0, 10.0, 4.0)
    x1, y_bot = _vpx(xz, 3.0, 1.0, 10.0, 4.0)
    assert abs(x - x0) < 0.01 and abs(y - y_top) < 0.01 and abs(w - (x1 - x0)) < 0.01 and abs(h - (y_bot - y_top)) < 0.01
    yz = W.view_preview(ROOM10, SEG10, "yz", obstacles=obs)
    x, y, w, h = yz["obstacles"][0]["rect"]
    y0p, z_top = _vpx(yz, 1.0, 4.0, 5.0, 4.0)
    y1p, z_bot = _vpx(yz, 4.0, 1.0, 5.0, 4.0)
    assert abs(x - y0p) < 0.01 and abs(w - (y1p - y0p)) < 0.01 and abs(y - z_top) < 0.01 and abs(h - (z_bot - z_top)) < 0.01
    assert "秘密" not in json.dumps([xy, xz, yz], ensure_ascii=False)


def test_view_preview_projects_hangers_joints_and_dedupes_per_view():
    hs = [(1.0, 0.0, 3.0), (1.0, 2.0, 3.0), (1.0, 0.0, 2.0)]
    assert len(W.view_preview(ROOM10, SEG10, "xy", hangers=hs)["hangers"]) == 2
    assert len(W.view_preview(ROOM10, SEG10, "xz", hangers=hs)["hangers"]) == 2   # 沿 Y 重疊
    yz = W.view_preview(ROOM10, SEG10, "yz", hangers=hs)
    assert len(yz["hangers"]) == 3 and yz["counts"]["hangers"] == 3
    hx, hy = yz["hangers"][1]
    ex, ey = _vpx(yz, 2.0, 3.0, 5.0, 4.0)
    assert abs(hx - ex) < 0.01 and abs(hy - ey) < 0.01
    jt = W.view_preview(ROOM10, SEG10, "xz", joints=[{"kind": "tee", "point": [5.0, 2.0, 3.0]}])
    (j,) = jt["joints"]
    ex, ey = _vpx(jt, 5.0, 3.0, 10.0, 4.0)
    assert j["kind"] == "tee" and abs(j["pt"][0] - ex) < 0.01 and abs(j["pt"][1] - ey) < 0.01


def test_view_preview_rejects_unknown_views_and_flat_rooms():
    with pytest.raises(ValueError):
        W.view_preview(ROOM10, SEG10, "zx")
    flat = ((0.0, 0.0, 0.0), (10.0, 5.0, 0.0))
    W.view_preview(flat, SEG10, "xy")                                   # 俯視不需要高度
    for v in ("xz", "yz"):
        with pytest.raises(ValueError):
            W.view_preview(flat, SEG10, v)


def test_preview_views_returns_all_three_views_with_xy_default():
    pv = W.preview_views(ROOM10, SEG10, start=(0.0, 0.0, 3.0), ends=[(10.0, 0.0, 3.0)])
    assert pv["caption"] == "示意圖，非施工圖" and pv["default_view"] == "xy"
    assert list(pv["views"]) == ["xy", "xz", "yz"] == list(W.PREVIEW_VIEWS)
    for k, v in pv["views"].items():
        assert v["view"] == k and v["size"] == [640, 280] and v["caption"] == pv["caption"]
        assert set(v) >= {"room", "segments", "start", "ends", "obstacles", "hangers", "joints", "counts", "notes",
                          "scale_bar", "axes", "title"}
    assert [pv["views"][k]["title"] for k in ("xy", "xz", "yz")] == ["XY 平面示意（俯視）", "XZ 立面示意（前視）",
                                                                     "YZ 立面示意（側視）"]


def test_safe_preview_keeps_other_views_when_one_view_fails(monkeypatch):
    real = W.view_preview

    def flaky(room, segments, view="xy", *a, **k):
        if view == "yz":
            raise ValueError("x")
        return real(room, segments, view, *a, **k)
    monkeypatch.setattr(W, "view_preview", flaky)
    p, err = W.safe_preview(_fake_res())
    assert err is None and list(p["views"]) == ["xy", "xz"] and p["default_view"] == "xy"
    assert any("YZ" in n for n in p["views"]["xy"]["notes"])


def test_safe_preview_default_falls_to_first_available_view_when_xy_fails(monkeypatch):
    real = W.view_preview

    def flaky(room, segments, view="xy", *a, **k):
        if view == "xy":
            raise ValueError("x")
        return real(room, segments, view, *a, **k)
    monkeypatch.setattr(W, "view_preview", flaky)
    p, err = W.safe_preview(_fake_res())
    assert err is None and list(p["views"]) == ["xz", "yz"] and p["default_view"] == "xz"


def test_safe_preview_size_guard_drops_to_the_default_view_then_to_text(monkeypatch):
    full, _ = W.safe_preview(_fake_res())
    n_all = len(json.dumps(full, ensure_ascii=False).encode("utf-8"))
    n_xy = len(json.dumps(full["views"]["xy"], ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(W, "PREVIEW_MAX_BYTES", n_xy + 400)
    assert n_all > W.PREVIEW_MAX_BYTES
    p, err = W.safe_preview(_fake_res())
    assert err is None and list(p["views"]) == ["xy"] and any("資料量" in n for n in p["views"]["xy"]["notes"])
    monkeypatch.setattr(W, "PREVIEW_MAX_BYTES", 100)
    p, err = W.safe_preview(_fake_res())
    assert p is None and err == W.PREVIEW_UNAVAILABLE


def test_preview_byte_cap_is_modest_and_above_a_typical_job(srv):
    assert W.PREVIEW_MAX_BYTES <= 1_000_000 < W.MAX_BODY + 1
    j = run_job(srv)
    assert len(json.dumps(j["preview"], ensure_ascii=False).encode("utf-8")) < 60_000


def test_app_switches_views_with_dom_buttons_and_event_listeners():
    js = W.APP_JS
    for kw in ('createElement("button")', 'addEventListener("click"', '"aria-pressed"', 'type = "button"',
               '"XY 俯視"', '"XZ 前視"', '"YZ 側視"', "XZ 立面示意", "YZ 立面示意", "pr.views", "default_view"):
        assert kw in js, kw
    assert "onclick" not in js and ".onclick" not in js and "innerHTML" not in js and "eval(" not in js
    assert "http://" not in js and "https://" not in js
    body = js[js.index("function drawPreview"):js.index("function renderResult")]
    assert "clear(holder)" in body and "show(" in body                 # 切換時重畫
    assert '"views"' in body or "className = \"views\"" in body or 'className = "views"' in body
    assert "aria-pressed" in W.APP_CSS and ".views" in W.APP_CSS


def test_app_draws_axis_labels_and_view_specific_legend():
    js = W.APP_JS
    assert "vw.h" in js and "vw.v" in js and "fillText(" in js
    for kw in ("沿 Y 向的段顯示為點", "沿 X 向的段顯示為點", "垂直段顯示為點", "vw.side", "vw.title"):
        assert kw in js, kw
    assert "legendText(vw, obs, hs, jt)" in js


def test_index_still_has_four_sections_and_one_script_with_view_switching(srv):
    html_ = call(srv, "GET", T(srv))[2].decode("utf-8")
    assert html_.count("<section") == 4 and re.findall(r"<script[^>]*>", html_) == ['<script src="app.js">']
    assert "<button" not in html_.split("④ 結果下載")[1]                # 切換按鈕由 app.js 以 DOM 建立


# ───────────── 起訖點標籤避讓（階段 3 第三刀追加：YZ 起點與終點重合時標籤重疊） ─────────────
def test_app_places_start_end_labels_with_a_layout_helper():
    js = W.APP_JS
    assert "function groupMarkers(" in js and "function layoutLabels(" in js and "function boxHit(" in js
    body = js[js.index("function drawView"):js.index("function drawPreview")]
    assert "layoutLabels(groupMarkers(marks, MARK_TOL)" in body and "measureText(s).width" in body
    assert 'fillText("起點"' not in body and 'fillText("終點"' not in body       # 標籤一律經過排版
    assert body.index("drawJoint(ctx, jt") < body.index("if (pv.start)") < body.index("layoutLabels(")
    helpers = js[js.index("function groupMarkers"):js.index("// ── end label layout ──")]
    assert '"／"' in helpers and "Math.min(Math.max(" in helpers               # 重合合併；貼邊時夾回畫布內


_NODE = shutil.which("node")
_LABEL_DRIVER = r"""
var measure = function (s) { return s.length * 12; };
function run(marks, w, h) {
  var g = groupMarkers(marks, MARK_TOL);
  return {groups: g, labs: layoutLabels(g, measure, w, h, LABEL_H)};
}
function m(x, y, label) { return {x: x, y: y, label: label}; }
var out = {
  same: run([m(100, 100, "起點"), m(100, 100, "終點1")], 640, 280),
  allsame: run([m(50, 60, "起點"), m(50, 60, "終點1"), m(51, 61, "終點2"), m(50, 60, "終點3")], 640, 280),
  near: run([m(100, 100, "起點"), m(109, 103, "終點1"), m(100, 110, "終點2"), m(112, 96, "終點3")], 640, 280),
  edge: run([m(636, 4, "起點"), m(636, 10, "終點1"), m(2, 278, "終點2"), m(638, 276, "終點3")], 640, 280),
  plain: run([m(100, 100, "起點"), m(400, 200, "終點1")], 640, 280)
};
process.stdout.write(JSON.stringify(out));
"""


def _label_boxes(case):
    return [(l["x"], l["y"] - 12, l["x"] + len(l["text"]) * 12, l["y"] + 2) for l in case["labs"]]


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


@pytest.mark.skipif(_NODE is None, reason="node 未安裝")
def test_label_layout_merges_coincident_points_and_never_overlaps_in_node(tmp_path):
    js = W.APP_JS
    helpers = js[js.index("  var MARK_TOL"):js.index("// ── end label layout ──")]
    f = tmp_path / "labels.js"
    f.write_text('"use strict";\n' + helpers + _LABEL_DRIVER, encoding="utf-8")
    r = subprocess.run([_NODE, str(f)], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = json.loads(r.stdout.decode("utf-8"))
    assert [l["text"] for l in out["same"]["labs"]] == ["起點／終點1"]
    assert [l["text"] for l in out["allsame"]["labs"]] == ["起點／終點1／終點2／終點3"]
    assert [l["text"] for l in out["near"]["labs"]] == ["起點", "終點1", "終點2", "終點3"]
    assert [l["text"] for l in out["edge"]["labs"]] == ["起點", "終點1", "終點2", "終點3"]
    for name in ("same", "allsame", "near", "edge", "plain"):
        case = out[name]
        boxes = _label_boxes(case)
        for b in boxes:                                                    # 全部在畫布內
            assert 0 <= b[0] and b[2] <= 640 and 0 <= b[1] and b[3] <= 280, (name, b)
        for i in range(len(boxes)):                                        # 標籤彼此不重疊
            for j in range(i + 1, len(boxes)):
                assert not _overlap(boxes[i], boxes[j]), (name, i, j)
        marks = [(g["x"] - 6, g["y"] - 6, g["x"] + 6, g["y"] + 6) for g in case["groups"]]
        if name != "edge":                                                 # 有空間時也不蓋住任何記號
            for b in boxes:
                assert not any(_overlap(b, mk) for mk in marks), (name, b)
    s, e = out["plain"]["labs"]                                            # 不擁擠時維持原本位置（右上／右上）
    assert (s["x"], s["y"]) == (107, 93) and (e["x"], e["y"]) == (407, 193)

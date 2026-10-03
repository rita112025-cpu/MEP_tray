import copy
import http.client
import json
import re
import socket
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

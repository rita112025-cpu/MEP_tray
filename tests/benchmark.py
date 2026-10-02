"""Local, bounded routing benchmark: python -m tests.benchmark."""
import json
import math
import platform
import time
import tracemalloc

from mep_tray.geometry import Box
from mep_tray.clash import check_route
from mep_tray.model import Inputs
from mep_tray.router import Obstacle, route_tray
from mep_tray.rules import merge_strictest


def main():
    gov = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])
    cases = [
        ("small", Box((0, 0, 0), (12, 6, 4)), [(10, 1, 3)], [], 0.25),
        ("obstacles", Box((0, 0, 0), (20, 12, 6)), [(18, 1, 3), (18, 10, 3)],
         [Obstacle(f"O{i}", "structure", Box((i, 4, 1), (i + 0.2, 5, 4)))
          for i in range(3, 15)], 0.25),
        ("large_straight", Box((0, 0, 0), (200, 12, 6)), [(180, 1, 3)], [], 0.5),
    ]
    results = []
    for name, room, ends, obstacles, cell in cases:
        tracemalloc.start()
        start = time.perf_counter()
        route = route_tray(room, (1, 1, 3), ends, obstacles, 0.3, 0.1, "power", gov, cell=cell)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # Correctness is checked outside the timed section; speed alone is not a pass.
        inp = Inputs(room=(room.lo, room.hi), start=(1, 1, 3), ends=ends,
                     obstacles=[dict(name=o.name, kind=o.kind, lo=o.box.lo, hi=o.box.hi) for o in obstacles])
        report = check_route(inp, route, gov)
        assert all(end in {p for path in route.waypoints for p in path} for end in ends)
        assert all(sum(abs(a[i] - b[i]) > 1e-9 for i in range(3)) == 1 for a, b in route.segments)
        assert math.isclose(route.length_m, sum(math.dist(a, b) for a, b in route.segments))
        assert not any(f.status == "CLASH" for f in report.findings)
        assert math.isclose(route.length_m, {"small": 9.0, "obstacles": 35.0, "large_straight": 179.0}[name])
        results.append(dict(case=name, room_m=[room.lo, room.hi], cell_m=cell,
                            endpoints=len(ends), obstacles=len(obstacles),
                            runtime_s=round(elapsed, 6), python_peak_bytes=peak,
                            segments=len(route.segments), route_length_m=route.length_m,
                            geometry_checks=len(report.checks), correctness="PASS"))
    print(json.dumps(dict(environment=dict(os=platform.platform(), python=platform.python_version(),
                                          machine=platform.machine(), processor=platform.processor()),
                          memory_metric="tracemalloc peak Python allocations; not process RSS",
                          timing="one run per case with tracemalloc enabled; no FPS (no canvas UI)",
                          results=results), indent=2))


if __name__ == "__main__":
    main()

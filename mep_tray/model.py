"""輸入資料結構（UI/CLI/版本管理共用）。所有長度：橋架尺寸 mm、場景座標 m。

電纜資料（外徑/數量）為填充率驗算所必需。UI 只提供「電纜外徑、條數」兩個尺寸欄；
未填時採 DEFAULT_CABLE 並將 cables_defaulted=True，報告必須揭露。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .geometry import Box, Vec
from .router import Obstacle

DEFAULT_CABLE = {"od_mm": 20.0, "count": 10, "kind": "power"}
OBSTACLE_KINDS = {"water", "duct", "heat", "structure", "tray_power", "tray_signal", "other"}


@dataclass
class Inputs:
    tray_w_mm: float = 300
    tray_h_mm: float = 100
    tray_type: str = "power"                       # power | signal
    room: tuple = ((0, 0, 0), (12, 6, 4))          # (lo, hi) m
    start: Vec = (1.0, 1.0, 3.0)
    ends: list = field(default_factory=lambda: [(10.0, 1.0, 3.0)])
    obstacles: list = field(default_factory=list)  # [{name, kind, lo, hi}]
    cables: list = field(default_factory=list)     # [{od_mm, count, kind}]
    codes: list = field(default_factory=lambda: ["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])
    cell_m: float | None = None                    # None=依規範淨距自動

    @property
    def cables_defaulted(self) -> bool:
        return not self.cables

    def effective_cables(self) -> list[dict]:
        return self.cables or [dict(DEFAULT_CABLE, kind=self.tray_type)]

    def room_box(self) -> Box:
        return Box(tuple(self.room[0]), tuple(self.room[1]))

    def obstacle_objs(self) -> list[Obstacle]:
        return [Obstacle(o["name"], o["kind"], Box(tuple(o["lo"]), tuple(o["hi"])))
                for o in self.obstacles]

    def validate(self) -> None:
        if self.tray_type not in ("power", "signal"):
            raise ValueError("tray_type 需為 power|signal")
        if self.tray_w_mm <= 0 or self.tray_h_mm <= 0:
            raise ValueError("橋架寬/高需為正數")
        for o in self.obstacles:
            if o.get("kind") not in OBSTACLE_KINDS:
                raise ValueError(f"障礙物類型未知: {o.get('kind')!r}")
            if any(l >= h for l, h in zip(o["lo"], o["hi"])):
                raise ValueError(f"障礙物 {o.get('name')} 的 lo/hi 不合法")
        for c in self.cables:
            if c["od_mm"] <= 0 or c.get("count", 1) <= 0:
                raise ValueError("電纜外徑/條數需為正數")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Inputs":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        i = cls(**known)
        i.room = tuple(tuple(x) for x in i.room)
        i.start = tuple(i.start)
        i.ends = [tuple(e) for e in i.ends]
        i.validate()
        return i

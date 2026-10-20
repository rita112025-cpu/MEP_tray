namespace MepTray.Core;

public sealed record RodResult(double? LengthMm, string? Code, string? Message);

/// <summary>
/// rod_length.mode = to_structure 的長度計算（純邏輯，單位 mm；Python 對應 supports.rod_length_to_structure）。
/// 長度 = 結構面 Z − 吊架承托面 Z + offset_mm；若設 round_to_mm 則取最接近的倍數（恰在中間時遠離 0 進位），最後取到 0.001 mm。
/// 結構面必須高於橋架頂面；結果必須落在 [min_mm, max_mm]。min／max／offset 全由設定檔提供，這裡沒有任何預設值。
/// </summary>
public static class RodLength
{
    public const string NotAboveTray = "not_above_tray";
    public const string BelowMin = "below_min";
    public const string AboveMax = "above_max";

    public static RodResult Compute(double bearingZMm, double hitZMm, double trayTopZMm, HangerConfig cfg)
    {
        if (hitZMm <= trayTopZMm)
            return new RodResult(null, NotAboveTray, $"命中的結構面 Z={hitZMm:F1} mm 不高於橋架頂面 Z={trayTopZMm:F1} mm，吊桿無法穿過橋架");
        var raw = hitZMm - bearingZMm + cfg.RodOffsetMm;
        if (cfg.RodRoundToMm is double step)
            raw = Math.Round(raw / step, MidpointRounding.AwayFromZero) * step;
        var len = Math.Round(raw, 3) + 0.0;
        if (len < cfg.RodMinMm)
            return new RodResult(null, BelowMin, $"吊桿長度 {len:F1} mm 小於設定下限 min_mm={cfg.RodMinMm:F1}（結構面 Z={hitZMm:F1}、承托面 Z={bearingZMm:F1}、offset={cfg.RodOffsetMm:F1}）");
        if (len > cfg.RodMaxMm)
            return new RodResult(null, AboveMax, $"吊桿長度 {len:F1} mm 超過設定上限 max_mm={cfg.RodMaxMm:F1}（結構面 Z={hitZMm:F1}、承托面 Z={bearingZMm:F1}、offset={cfg.RodOffsetMm:F1}）");
        return new RodResult(len, null, null);
    }
}

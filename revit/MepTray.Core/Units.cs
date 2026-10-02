namespace MepTray.Core;

public static class Units
{
    /// <summary>mm → 英呎（Revit API 內部長度單位）。全專案唯一的換算點。</summary>
    public static double MmToFeet(double mm) => mm / Fields.MmPerFoot;

    public static double FeetToMm(double ft) => ft * Fields.MmPerFoot;
}

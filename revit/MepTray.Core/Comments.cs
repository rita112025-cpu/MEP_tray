using System.Text.RegularExpressions;

namespace MepTray.Core;

/// <summary>Comments 參數的 [MEP-TRAY] 區段處理。行為須與 Python 的 export_revit.merge_comments/comment_body 一致（有跨語言契約測試）。</summary>
public static class Comments
{
    static readonly Regex SectionRe = new(
        Regex.Escape(Fields.TagOpen) + ".*?" + Regex.Escape(Fields.TagClose), RegexOptions.Singleline);

    /// <summary>保留原 Comments；已有 [MEP-TRAY] 區段則就地更新，否則附加。冪等。</summary>
    public static string Merge(string? old, string sectionBody)
    {
        old ??= "";
        var sec = $"{Fields.TagOpen} {sectionBody} {Fields.TagClose}";
        if (SectionRe.IsMatch(old)) return SectionRe.Replace(old, _ => sec, 1);
        return old + (old.Length > 0 && !old.EndsWith('\n') ? "\n" : "") + sec;
    }

    public static string Body(TrayModel m, string segId)
    {
        var head = $"RUN={m.RunId}; SEG={segId}; TYPE={m.Tray.Kind}";
        var fs = m.Findings.Where(f => f.SegmentId == segId).ToList();
        if (fs.Count == 0) return head;
        return head + "; FINDINGS=" + string.Join("; ", fs.Select(f =>
            $"{f.Status} {f.Kind} {(string.IsNullOrEmpty(f.Code) ? "-" : f.Code)}"));
    }
}

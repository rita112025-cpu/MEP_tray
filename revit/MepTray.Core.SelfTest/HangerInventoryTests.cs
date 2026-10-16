using System.Text;
using MepTray.Core;

/// <summary>吊架族群盤點（HangerInventory.cs）的純邏輯自我測試；sampleDir 非 null 時寫出範例 JSON/CSV。</summary>
static class HangerInventoryTests
{
    static FamilyEntry Fam(string src, string name, params string[] types) => new()
    {
        Source = src, FamilyName = name, FilePath = src == "rfa" ? $@"X:\族 庫\{name}.rfa" : null, Types = types.ToList(), PlacementType = "WorkPlaneBased",
    };

    public static void Run(Action<bool, string> Check, string? sampleDir)
    {
        // 標示（不過濾）
        Check(HangerInventory.IsHangerLike("吊架(角鐵)") && HangerInventory.IsHangerLike("Pipe HANGER") && !HangerInventory.IsHangerLike("泵浦防震基座", null),
              "吊架類標示：中英文、大小寫、不命中者");
        Check(HangerInventory.PlacementNote("Invalid") == "待實機確認" && HangerInventory.PlacementNote(null) == "待實機確認"
              && HangerInventory.PlacementNote("WorkPlaneBased") == null, "放置方式無法判定 → 待實機確認");
        Check(HangerInventory.ParseRevitYear("Autodesk Revit 2019") == 2019 && HangerInventory.ParseRevitYear("2025") == 2025
              && HangerInventory.ParseRevitYear("garbage") == null, "解析族檔儲存版本年份");
        var len = HangerInventory.Length(1.0, "304.8 mm");
        Check(len.Mm == 304.8 && len.Raw == "1" && len.RawUnit == "ft (Revit internal)" && len.Status == "ok", "長度保留 raw/raw_unit 並換算 mm");

        // 資料夾列舉：中文＋空白路徑、子資料夾、備份檔、不存在
        var root = Path.Combine(Path.GetTempPath(), "MEP tray 測試 " + Guid.NewGuid().ToString("N")[..8], "13_固定工具 & 吊架");
        try
        {
            Directory.CreateDirectory(Path.Combine(root, "APP管及導管"));
            foreach (var f in new[] { "吊架(角鐵).rfa", "螺桿.rfa", "吊架(角鐵).0001.rfa", "說明 檔.txt", @"APP管及導管\吊架(槽鋼).rfa" })
                File.WriteAllText(Path.Combine(root, f), "x");
            var top = HangerInventory.EnumerateRfa(root, false, out var e1);
            var backups = new List<string>();
            var deep = HangerInventory.EnumerateRfa(root, true, out var e2, backups);
            Check(e1 == null && top.Count == 2 && top.All(f => f.EndsWith(".rfa")), "列舉 .rfa（不含子資料夾、略過備份與非 rfa）");
            Check(e2 == null && deep.Count == 3 && deep.Any(f => f.Contains("APP管及導管")) && backups.Count == 1, "列舉含子資料夾；備份檔另列");
            Check(deep.SequenceEqual(deep.OrderBy(f => f, StringComparer.Ordinal)), "列舉結果排序決定性");
            var none = HangerInventory.EnumerateRfa(Path.Combine(root, "不存在 的資料夾"), true, out var e3);
            Check(none.Count == 0 && e3 != null && e3.Contains("不存在"), "資料夾不存在 → 空清單＋原因（不假裝成功）");

            // 批次：單檔失敗不中斷；取消後其餘標為 cancelled_unprocessed
            var calls = new List<string>();
            var batch = HangerInventory.RunBatch(deep, f =>
            {
                calls.Add(f);
                if (f.Contains("槽鋼")) throw new IOException("模擬損毀");
                return new FamilyEntry { Source = "rfa", FilePath = f, FamilyName = Path.GetFileNameWithoutExtension(f) };
            }, () => false);
            Check(batch.Count == 3 && calls.Count == 3 && batch.Count(b => b.Status == "failed") == 1
                  && batch.Single(b => b.Status == "failed").Reason!.Contains("模擬損毀"), "單檔失敗記錄原因並繼續下一檔");
            int n = 0;
            var cancelled = HangerInventory.RunBatch(deep, f => new FamilyEntry { Source = "rfa", FilePath = f, FamilyName = "x" }, () => n++ >= 1);
            Check(cancelled.Count == 3 && cancelled.Count(c => c.Status == "ok") == 1 && cancelled.Count(c => c.Status == "cancelled_unprocessed") == 2
                  && cancelled.All(c => c.FilePath != null), "取消：已處理保留、其餘 cancelled_unprocessed（含路徑）");
        }
        finally { try { Directory.Delete(Path.GetDirectoryName(root)!, true); } catch (Exception) { } }

        // 報告：重點族、摘要、值狀態、CSV/JSON
        var r = new InventoryReport { GeneratedAt = "2026-10-16 10:30:00 +08:00", DocumentTitle = "測試 專案", ProjectScanned = true, RfaFolder = @"X:\族 庫",
                                      ReadOnlyCheck = new ReadOnlyCheck { ElementCountBefore = 100, ElementCountAfter = 100 } };
        var proj = Fam("project", "吊架(角鐵)", "圓形", "矩形");
        proj.HangerLike = true;
        proj.Params.Add(new ParamDef { Name = "寬度", Scope = "type", StorageType = "Double", DataType = "autodesk.spec.aec:length-2.0.0", ValueKind = "type_value",
                                       ValuesByType = { ["矩形"] = HangerInventory.Length(400 / 304.8, "400 mm"), ["圓形"] = HangerInventory.NoValue() } });
        proj.InstanceCount = 1;
        proj.Instances.Add(new InstanceInfo { ElementId = 12345, TypeName = "矩形", LocationMm = new[] { 1000.0, 2000, 3000 }, Level = "2F",
            Params = { new InstanceParam { Name = "螺桿長度", StorageType = "Double", Value = HangerInventory.Length(600 / 304.8, "600 mm") },
                       new InstanceParam { Name = "註解", StorageType = "String", Value = HangerInventory.ReadError(new InvalidOperationException("模擬讀取失敗")) } } });
        var rfa = Fam("rfa", "吊架(角鐵)", "圓形", "矩形");
        rfa.Params.Add(new ParamDef { Name = "螺桿長度", Scope = "instance", StorageType = "Double", Formula = "吊高 - 50 mm", IsReporting = false, ValueKind = "family_default",
                                      ValuesByType = { ["矩形"] = HangerInventory.Length(550 / 304.8, "550 mm") } });
        var rfaFail = Fam("rfa", "壞檔, \"引號\"");
        rfaFail.Status = "failed"; rfaFail.Reason = "檔案損毀：x";
        var newer = Fam("rfa", "新版族");
        newer.Status = "skipped"; newer.Reason = "較新版本";
        var cancel = Fam("rfa", "未處理");
        cancel.Status = "cancelled_unprocessed"; cancel.Reason = "使用者取消，未處理";
        var weird = Fam("project", "=HYPERLINK(\"x\")");
        weird.PlacementType = "Invalid";
        r.Families.AddRange(new[] { weird, rfaFail, newer, cancel, rfa, proj });
        HangerInventory.Finish(r);
        Check(r.Families[0].Focus && r.Families[1].Focus && r.Families[0].Source == "project", "排序：吊架(角鐵) 優先，專案在前");
        Check(r.Focus.TypeInProject && r.Focus.TypeInRfa && r.Focus.Note.Contains("矩形"), "重點族：吊架(角鐵) / 矩形 找到");
        Check(weird.PlacementNote == "待實機確認", "Finish 補上 待實機確認");
        Check(r.ProjectSummary.Total == 2 && r.RfaSummary.Ok == 1 && r.RfaSummary.Failed == 1 && r.RfaSummary.Skipped == 1 && r.RfaSummary.CancelledUnprocessed == 1,
              "摘要：成功/失敗/略過/取消未處理 分別計數");
        Check(HangerInventory.Validate(r).Count == 0, "合法報告通過驗證: " + string.Join("|", HangerInventory.Validate(r)));

        var miss = HangerInventory.FindFocus(new[] { Fam("project", "吊架(角鐵)", "圓形"), Fam("rfa", "吊架(槽鋼)", "矩形") });
        Check(!miss.TypeInProject && miss.FamilyInProject && !miss.FamilyInRfa && miss.Note.Contains("沒有類型「矩形」") && miss.Note.Contains("族檔中未找到")
              && miss.Note.Contains("不以其他族替代"), "重點族缺少時明確說明、不替代");

        var csv = HangerInventory.ToCsv(r);
        var lines = csv.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        Check(lines[0] == string.Join(",", HangerInventory.CsvColumns), "CSV 表頭");
        Check(csv.Contains("type_value") && csv.Contains("family_default") && csv.Contains("instance_actual"), "CSV 區分 類型值/族預設值/實例實際值");
        Check(csv.Contains(",no_value,") && csv.Contains(",read_error,") && csv.Contains("模擬讀取失敗"), "CSV 區分 無值 與 讀取失敗");
        Check(csv.Contains("\"壞檔, \"\"引號\"\"\"") && csv.Contains("'=HYPERLINK"), "CSV 逗號/引號跳脫、防公式注入");
        Check(csv.Contains(",400,") && csv.Contains("吊高 - 50 mm") && csv.Contains("12345") && csv.Contains("1000 2000 3000"), "CSV 含 mm、公式、ElementId、位置");

        var bad = HangerInventory.FromJson(HangerInventory.ToJson(r));
        bad.ReadOnlyCheck!.ElementCountAfter = 101;
        bad.Families.First(f => f.Source == "rfa" && f.Status == "ok").Params[0].ValueKind = "type_value";
        bad.Families.First(f => f.Status == "failed").Reason = null;
        Check(HangerInventory.Validate(bad).Count >= 3, "偵測：元素數改變、實例參數族值未標 family_default、失敗無原因");
        var rt = HangerInventory.FromJson(HangerInventory.ToJson(r));
        Check(rt.Families.Count == 6 && rt.Families[0].Instances[0].Params.Any(p => p.Value.Status == "read_error") && rt.ReadOnlyCheck!.Passed, "JSON 往返");

        // 文件清理檢查（Application.Documents 前後集合比對）
        DocInfo D(string path, string title = "") => new() { PathName = path, Title = title };
        var before = new[] { D(@"C:\專案\測試 專案.rvt", "測試 專案"), D(@"X:\族 庫\已開啟.rfa", "已開啟"), D("", "未存檔專案1") };
        var rfaA = @"X:\族 庫\APP管及導管\吊架(角鐵).rfa"; var rfaB = @"X:\族 庫\螺桿.rfa";
        var ok = HangerInventory.CompareDocs(before, before.Select(d => D(d.PathName.ToLowerInvariant(), d.Title)), new[] { rfaA, rfaB });
        Check(ok.Passed && ok.OpenedByTool.Count == 2, "文件清理：工具開的都關了、原本的都在（路徑不分大小寫）");
        Check(ok.ToolOpenedAndClosed.Count == 2 && ok.PreExistingStillOpen.Count == 3 && ok.PreExistingStillOpen.Contains("未存檔專案1"),
              "文件清理：分列 工具開啟且已關閉／原本開啟仍開啟（無路徑用標題）");
        var famFlag = new DocInfo { PathName = rfaA, Title = "吊架(角鐵)", IsFamilyDocument = true };
        var famRt = HangerInventory.FromJson(HangerInventory.ToJson(new InventoryReport { DocCleanup = HangerInventory.CompareDocs(new[] { famFlag }, new[] { famFlag }, new string[0]) }));
        Check(famRt.DocCleanup!.Before[0].IsFamilyDocument && famRt.DocCleanup.Before[0].Display == rfaA && new DocInfo { Title = "T" }.Display == "T",
              "文件快照記錄 IsFamilyDocument；Display 無路徑時用 Title");
        var left = HangerInventory.CompareDocs(before, before.Append(D(rfaA, "吊架(角鐵)")), new[] { rfaA, rfaB });
        Check(!left.Passed && left.LeftOpenByTool.SequenceEqual(new[] { rfaA }), "文件清理：偵測工具開啟未關閉");
        var closedUser = HangerInventory.CompareDocs(before, before.Where(d => !d.PathName.EndsWith("已開啟.rfa")), new[] { rfaB });
        Check(!closedUser.Passed && closedUser.PreExistingMissing.Single().EndsWith("已開啟.rfa"), "文件清理：偵測使用者原本開啟的文件被關閉");
        var lostUnsaved = HangerInventory.CompareDocs(before, before.Where(d => d.PathName != ""), Array.Empty<string>());
        Check(!lostUnsaved.Passed && lostUnsaved.PreExistingMissing.Single() == "未存檔專案1", "文件清理：未存檔文件以標題比對");
        var extra = HangerInventory.CompareDocs(before, before.Append(D(@"Y:\其他.rvt", "其他")), Array.Empty<string>());
        Check(!extra.Passed && extra.UnexpectedNew.Count == 1, "文件清理：偵測非預期新增文件");
        var reuse = HangerInventory.CompareDocs(before, before, new string[0]);
        Check(reuse.Passed && reuse.OpenedByTool.Count == 0, "文件清理：已由使用者開啟的族檔不列為工具開啟");

        // 來源 A 不使用 EditFamily：族檔才有的欄位不可得
        var projBad = HangerInventory.FromJson(HangerInventory.ToJson(r));
        projBad.Families.First(f => f.Source == "project" && f.Params.Count > 0).Params[0].Formula = "x";
        Check(HangerInventory.Validate(projBad).Any(m => m.Contains("EditFamily")), "專案來源出現公式會被驗證擋下");
        Check(HangerInventory.ProjectUnavailableFields.SequenceEqual(new[] { "formula", "is_reporting", "is_determined_by_formula" }), "專案來源不可得欄位清單");
        var withCleanup = HangerInventory.FromJson(HangerInventory.ToJson(r));
        withCleanup.DocCleanup = left;
        Check(HangerInventory.Validate(withCleanup).Any(m => m.Contains("文件清理")), "報告中文件清理失敗會被驗證擋下");

        if (sampleDir != null)
        {
            var (j, c) = HangerInventory.Write(r, Path.Combine(sampleDir, "吊架族群盤點 範例.json"));
            var bytes = File.ReadAllBytes(c);
            Check(bytes.Length > 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF, "CSV 以 UTF-8 BOM 寫出");
            Check(File.ReadAllBytes(j)[0] == (byte)'{', "JSON 無 BOM");
        }
    }
}
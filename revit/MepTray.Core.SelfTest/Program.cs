using System.Text.Json;
using System.Text.Json.Nodes;
using MepTray.Core;

// 用法: SelfTest <model.json> <expected_comments.json>
var failures = new List<string>();
void Check(bool ok, string what) { if (!ok) failures.Add(what); Console.WriteLine($"{(ok ? "PASS" : "FAIL")}  {what}"); }

if (args.Length != 2) { Console.Error.WriteLine("usage: SelfTest <model.json> <expected_comments.json>"); return 2; }

// 單位
Check(Units.MmToFeet(304.8) == 1.0, "304.8 mm = 1 ft");
Check(Math.Abs(Units.FeetToMm(Units.MmToFeet(1234.5)) - 1234.5) < 1e-9, "mm→ft→mm 往返");

// Comments
var once = Comments.Merge("原備註", "RUN=a; SEG=S001");
Check(once.StartsWith("原備註") && once.Contains(Fields.TagOpen), "Comments 保留原文並附加區段");
Check(Comments.Merge(once, "RUN=a; SEG=S001") == once, "Comments 冪等");
var upd = Comments.Merge("上\n" + once + "\n下", "RUN=b");
Check(upd.Split(Fields.TagOpen).Length == 2 && !upd.Contains("RUN=a") && upd.StartsWith("上\n") && upd.EndsWith("\n下"),
      "Comments 就地更新且不重複");
var bad = Comments.Merge("x [MEP-TRAY] 沒有結尾", "RUN=a");
Check(bad.StartsWith("x [MEP-TRAY] 沒有結尾") && bad.Contains(Fields.TagClose), "Comments 殘缺標記不當作區段");

// 模型載入與驗證
var model = ModelLoader.Load(args[0]);
Check(ModelLoader.Validate(model).Count == 0, "Python 產生的模型通過 C# 驗證: " + string.Join("|", ModelLoader.Validate(model)));
Check(ModelLoader.BasisError("UNSPECIFIED") != null, "UNSPECIFIED 被拒絕");
Check(ModelLoader.BasisError("WORLD") != null, "未知基準被拒絕");
foreach (var b in ModelLoader.Bases) Check(ModelLoader.BasisError(b) == null, $"基準 {b} 被接受");

var broken = ModelLoader.Parse(File.ReadAllText(args[0]));
broken.SchemaVersion = 99; broken.Tray.WidthMm = -1;
broken.Segments[0].End = (double[])broken.Segments[0].Start.Clone();
broken.Segments.Add(new SegmentDto { Id = broken.Segments[0].Id });
Check(ModelLoader.Validate(broken).Count >= 4, "壞模型被偵測（版本/寬度/零長度/重複 id）");

// 只支援「平移 origin + 繞 Z 軸旋轉」；軸與 rotation_deg 不一致、非繞 Z 的軸、非有限數值一律在匯入前被拒絕。
foreach (var change in new Action<TrayModel>[] {
    m => m.CoordinateSystem.AxisX = new[] { 0.0, 1.0, 0.0 },                 // 軸與 rotation_deg=0 不一致
    m => m.CoordinateSystem.RotationDeg = 90.0,                              // 只改角度、軸未同步
    m => { m.CoordinateSystem.AxisZ = new[] { 1.0, 0.0, 0.0 }; },            // 非繞 Z 軸
    m => m.CoordinateSystem.Origin = new[] { double.NaN, 0.0, 0.0 },
    m => m.CoordinateSystem.Origin = new[] { 1.0, 2.0 },
    m => m.CoordinateSystem.RotationDeg = double.PositiveInfinity,
})
{
    var transformed = ModelLoader.Load(args[0]);
    change(transformed);
    Check(ModelLoader.Validate(transformed).Count > 0, "不一致或非有限的座標變換在匯入前被拒絕");
}
foreach (var deg in new[] { 30.0, 90.0, -45.0, 180.0 })
{
    var local = ModelLoader.Load(args[0]);
    var th = deg * Math.PI / 180.0;
    local.CoordinateSystem.Origin = new[] { 2000.0, -1000.0, 500.0 };
    local.CoordinateSystem.RotationDeg = deg;
    local.CoordinateSystem.AxisX = new[] { Math.Cos(th), Math.Sin(th), 0.0 };
    local.CoordinateSystem.AxisY = new[] { -Math.Sin(th), Math.Cos(th), 0.0 };
    Check(ModelLoader.Validate(local).Count == 0, $"origin 平移 + 繞 Z 旋轉 {deg}° 且軸一致時通過驗證");
    var p = ModelLoader.ToBasis(local.CoordinateSystem, new[] { 1000.0, 0.0, 300.0 });
    Check(Math.Abs(p[0] - (2000 + 1000 * Math.Cos(th))) < 1e-9 && Math.Abs(p[1] - (-1000 + 1000 * Math.Sin(th))) < 1e-9 && Math.Abs(p[2] - 800) < 1e-9,
          $"ToBasis = origin + Rz({deg}°)·點");
}
var duplicateJoint = ModelLoader.Load(args[0]);
duplicateJoint.Joints.Add(duplicateJoint.Joints[0]);
Check(ModelLoader.Validate(duplicateJoint).Count > 0, "重複 joint id 在 ToDictionary 前被拒絕");
var repeatedLeg = ModelLoader.Load(args[0]);
repeatedLeg.Joints[0].Segments[1] = repeatedLeg.Joints[0].Segments[0];
Check(ModelLoader.Validate(repeatedLeg).Count > 0, "同一接頭重複引用線段被拒絕");
var missingLeg = ModelLoader.Load(args[0]);
missingLeg.Joints[0].Segments[0] = "MISSING";
Check(ModelLoader.Validate(missingLeg).Count > 0, "不存在的線段引用被拒絕");
var emptyLegs = ModelLoader.Load(args[0]);
emptyLegs.Joints[0].Segments.Clear();
Check(ModelLoader.Validate(emptyLegs).Count > 0, "接頭空線段集合被拒絕");

// 實際 JSON 可包含 null；驗證必須回傳錯誤而非 NullReferenceException。
foreach (var field in new[] { "coordinate_system", "tray", "segments", "joints", "findings", "disclosures" })
{
    var node = JsonNode.Parse(File.ReadAllText(args[0]))!;
    node[field] = null;
    Check(ModelLoader.Validate(ModelLoader.Parse(node.ToJsonString())).Count > 0, $"nested null rejected: {field}");
}
foreach (var field in new[] { "segments", "joints", "findings", "disclosures" })
{
    var node = JsonNode.Parse(File.ReadAllText(args[0]))!;
    node[field]!.AsArray().Add((JsonNode?)null);
    Check(ModelLoader.Validate(ModelLoader.Parse(node.ToJsonString())).Count > 0, $"null collection element rejected: {field}");
}
foreach (var field in new[] { "start", "end" })
{
    var node = JsonNode.Parse(File.ReadAllText(args[0]))!;
    node["segments"]![0]![field] = null;
    Check(ModelLoader.Validate(ModelLoader.Parse(node.ToJsonString())).Count > 0, $"null segment {field} rejected");
}
foreach (var field in new[] { "point", "segments" })
{
    var node = JsonNode.Parse(File.ReadAllText(args[0]))!;
    node["joints"]![0]![field] = null;
    Check(ModelLoader.Validate(ModelLoader.Parse(node.ToJsonString())).Count > 0, $"null joint {field} rejected");
}
Check(ModelLoader.Validate(null).Count > 0, "null model rejected");
foreach (var field in new[] { "origin", "axis_x", "axis_y", "axis_z" })
{
    var node = JsonNode.Parse(File.ReadAllText(args[0]))!;
    node["coordinate_system"]![field] = null;
    Check(ModelLoader.Validate(ModelLoader.Parse(node.ToJsonString())).Count > 0, $"null coordinate vector rejected: {field}");
}
var empty = ModelLoader.Load(args[0]);
empty.Segments.Clear();
Check(ModelLoader.Validate(empty).Count > 0, "empty segments rejected");
var straight = ModelLoader.Load(args[0]);
straight.Joints.Clear(); straight.Findings.Clear(); straight.Disclosures.Clear();
Check(ModelLoader.Validate(straight).Count == 0, "empty optional collections accepted");

// 與 Python 的 Comments 契約
var expected = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(args[1]))!;
foreach (var (seg, body) in expected) Check(Comments.Body(model, seg) == body, $"Comments.Body 與 Python 一致: {seg}");
Check(expected.Count > 0, "契約樣本非空");

Console.WriteLine(failures.Count == 0 ? "ALL PASS" : $"{failures.Count} FAILED");
return failures.Count == 0 ? 0 : 1;

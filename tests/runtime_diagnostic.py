"""Read-only binary diagnostics; never unblocks files or changes OS policy.

python -m tests.runtime_diagnostic --output docs/verification/runtime.json
"""
import argparse
import base64
import json
import os
import subprocess
from pathlib import Path

from tests.dotnet_util import ROOT, find_dotnet
from tests.test_revit_dotnet import _build_model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dotnet = find_dotnet()
    if dotnet is None:
        raise RuntimeError(".NET SDK 10 not found")
    work = ROOT / ".handoff-temp" / "runtime-inputs"
    work.mkdir(parents=True, exist_ok=True)
    _, model, expected = _build_model(work)
    project = ROOT / "revit" / "MepTray.Core.SelfTest"
    binary = project / "bin" / "Release" / "net10.0" / "MepTray.Core.SelfTest.dll"
    commands = [[dotnet, option] for option in ("--info", "--list-runtimes", "--list-sdks")]
    commands += [[dotnet, str(binary), str(model), str(expected)],
                 [dotnet, "run", "--project", str(project), "-c", "Release", "--no-build",
                  "--", str(model), str(expected)]]
    invocations = []
    for command in commands:
        result = subprocess.run(command, capture_output=True, shell=False, timeout=120,
                                env=dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1"))
        invocations.append(dict(command=command, command_line=subprocess.list2cmdline(command),
                                exit_code=result.returncode,
                                stdout=result.stdout.decode("utf-8", errors="replace"),
                                stderr=result.stderr.decode("utf-8", errors="replace"),
                                stdout_raw_base64=base64.b64encode(result.stdout).decode("ascii"),
                                stderr_raw_base64=base64.b64encode(result.stderr).decode("ascii"),
                                binary_path=command[0]))
    bins = [Path(dotnet), binary.with_suffix(".exe"), binary, binary.parent / "MepTray.Core.dll"]
    quoted = ",".join("'" + str(path).replace("'", "''") + "'" for path in bins)
    script = """
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
    $bins = @(%s)
    $records = foreach ($bin in $bins) {
        $item = Get-Item -LiteralPath $bin
        $sig = Get-AuthenticodeSignature -LiteralPath $bin
        [pscustomobject]@{
            item_full = ($item | Format-List * | Out-String)
            path = $item.FullName; length = $item.Length; attributes = [string]$item.Attributes
            modified_utc = $item.LastWriteTimeUtc.ToString('o')
            sha256 = (Get-FileHash -LiteralPath $bin -Algorithm SHA256).Hash
            authenticode = [string]$sig.Status; signature_message = $sig.StatusMessage
            streams = @(Get-Item -LiteralPath $bin -Stream * | Select-Object Stream,Length)
        }
    }
    $events = @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-CodeIntegrity/Operational';
        StartTime=(Get-Date).AddHours(-4)} -MaxEvents 500 |
        Where-Object { $_.Message -like '*MEP_tray*' } |
        Select-Object @{Name='TimeUtc';Expression={$_.TimeCreated.ToUniversalTime().ToString('o')}},Id,RecordId,Message,
            @{Name='Xml';Expression={$_.ToXml()}})
    @{binaries=@($records); code_integrity_events=$events} | ConvertTo-Json -Depth 8
    """ % quoted
    evidence = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, shell=False)
    if evidence.returncode != 0:
        raise RuntimeError(evidence.stderr)
    record = dict(invocations=invocations, evidence=json.loads(evidence.stdout),
                  deps=json.loads(binary.with_suffix(".deps.json").read_text(encoding="utf-8")),
                  os_policy_changed=False, files_unblocked=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([dict(command=i["command_line"], exit_code=i["exit_code"])
                      for i in invocations], indent=2))
    print(f"Evidence: {args.output}")


if __name__ == "__main__":
    main()

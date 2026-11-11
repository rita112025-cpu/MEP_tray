"""Build the Manual-mode Dynamo graph that embeds the extractor script."""
from __future__ import annotations

from pathlib import Path

_NODE_ID = "a11ce0c0de0000000000000000000001"
_IN0 = "a11ce0c0de0000000000000000000002"
_IN1 = "a11ce0c0de0000000000000000000003"
_OUT = "a11ce0c0de0000000000000000000004"


def build_graph(code: str) -> dict:
    """Return a Dynamo 3 workspace JSON object. RunType is Manual."""
    return {
        "Uuid": "b1c0a11e-7a31-4e0a-9c1e-0b1a6416a001",
        "IsCustomNode": False,
        "Description": (
            "Read-only BIM inspector snapshot for Revit. "
            "Run this graph manually. It writes a .json file and does not modify the open model."
        ),
        "Name": "BIM Inspector Extract",
        "ElementResolver": {"ResolutionMap": {}},
        "Inputs": [],
        "Outputs": [],
        "Nodes": [
            {
                "ConcreteType": "PythonNodeModels.PythonNode, PythonNodeModels",
                "NodeType": "PythonScriptNode",
                "Code": code,
                "Engine": "CPython3",
                "VariableInputPorts": True,
                "Id": _NODE_ID,
                "Inputs": [
                    _port(_IN0, "IN[0]", "Output .json path"),
                    _port(_IN1, "IN[1]", "De-identified project label"),
                ],
                "Outputs": [_port(_OUT, "OUT", "Written .json path")],
                "Replication": "Disabled",
                "Description": "Read-only snapshot. Manual run only.",
            }
        ],
        "Connectors": [],
        "Dependencies": [],
        "NodeLibraryDependencies": [],
        "Thumbnail": "",
        "GraphDocumentationURL": None,
        "ExtensionWorkspaceData": [
            {
                "ExtensionGuid": "28992e1d-abb9-417f-8b1b-05e053bee670",
                "Name": "Properties",
                "Version": "3.3.2",
                "Data": {},
            }
        ],
        "Author": "",
        "Linting": {
            "activeLinter": "None",
            "activeLinterId": "7b75fb44-43fd-4631-a878-29f4d5d8399a",
            "warningCount": 0,
            "errorCount": 0,
        },
        "Bindings": [],
        "View": {
            "Dynamo": {
                "ScaleFactor": 1.0,
                "HasRunWithoutCrash": False,
                "IsVisibleInDynamoLibrary": True,
                "Version": "3.3.2",
                "RunType": "Manual",
                "RunPeriod": "1000",
            },
            "Camera": {
                "Name": "Background Preview",
                "EyeX": -17.0,
                "EyeY": 24.0,
                "EyeZ": 50.0,
                "LookX": 12.0,
                "LookY": -13.0,
                "LookZ": -58.0,
                "UpX": 0.0,
                "UpY": 1.0,
                "UpZ": 0.0,
            },
            "NodeViews": [
                {
                    "ShowGeometry": True,
                    "Name": "BIM Inspector Extract (read-only)",
                    "Id": _NODE_ID,
                    "IsSetAsInput": False,
                    "IsSetAsOutput": False,
                    "Excluded": False,
                    "X": 280.0,
                    "Y": 180.0,
                }
            ],
            "Annotations": [],
            "X": 0.0,
            "Y": 0.0,
            "Zoom": 1.0,
        },
    }


def _port(port_id: str, name: str, description: str) -> dict:
    return {
        "Id": port_id,
        "Name": name,
        "Description": description,
        "UsingDefaultValue": False,
        "Level": 2,
        "UseLevels": False,
        "KeepListStructure": False,
    }


def main() -> None:
    import json

    root = Path(__file__).resolve().parent
    code = (root / "dynamo" / "extract_snapshot.py").read_text(encoding="utf-8")
    graph = build_graph(code)
    target = root / "dynamo" / "bim_inspector_extract.dyn"
    target.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

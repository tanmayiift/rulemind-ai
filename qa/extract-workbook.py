from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def column_to_index(label: str) -> int:
    value = 0
    for char in label:
        value = value * 26 + (ord(char) - 64)
    return value - 1


def extract_rows(sheet_xml: bytes, shared_strings: list[str]) -> list[list[str]]:
    worksheet = ET.fromstring(sheet_xml)
    rows: list[list[str]] = []

    for row in worksheet.findall(".//a:sheetData/a:row", NS):
        values: dict[int, str] = {}
        for cell in row.findall("a:c", NS):
            ref = cell.attrib["r"]
            col = "".join(char for char in ref if char.isalpha())
            cell_type = cell.attrib.get("t")
            value_node = cell.find("a:v", NS)
            value = ""

            if cell_type == "s" and value_node is not None:
                value = shared_strings[int(value_node.text or "0")]
            elif cell_type == "inlineStr":
                inline = cell.find("a:is", NS)
                if inline is not None:
                    value = "".join(text.text or "" for text in inline.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
            elif value_node is not None:
                value = value_node.text or ""

            values[column_to_index(col)] = value

        if values:
            max_col = max(values)
            rows.append([values.get(index, "") for index in range(max_col + 1)])

    return rows


def main() -> None:
    root = Path("/Users/tanmaykumar/Downloads/RuleMind.AI")
    workbook_path = root / "Test Cases" / "rulemind_test_suite.xlsx"
    results_dir = root / "qa" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    with ZipFile(workbook_path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationship_map = {rel.attrib["Id"]: rel.attrib["Target"].lstrip("/") for rel in relationships}
        shared_strings: list[str] = []

        if "xl/sharedStrings.xml" in archive.namelist():
            sst = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in sst:
                shared_strings.append(
                    "".join(text.text or "" for text in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
                )

        manifest = []
        for sheet in workbook.find("a:sheets", NS):
            target = relationship_map[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
            rows = extract_rows(archive.read(target), shared_strings)
            headers = rows[0] if rows else []
            manifest.append(
                {
                    "name": sheet.attrib["name"],
                    "rows": [
                        {headers[index] if headers[index] else f"col_{index + 1}": value for index, value in enumerate(row)}
                        for row in rows[1:]
                    ],
                }
            )

    output_path = results_dir / "workbook-manifest.json"
    output_path.write_text(
        json.dumps(
            {
                "generatedAt": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "sheets": manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Workbook manifest written to {output_path}")


if __name__ == "__main__":
    main()

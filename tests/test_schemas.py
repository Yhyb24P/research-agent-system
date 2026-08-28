import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]


def test_schema_files_are_valid_draft_2020_12() -> None:
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_examples_validate_against_json_schemas() -> None:
    pairs = (
        ("work_order.schema.json", "sample_work_order.json"),
        ("review_decision.schema.json", "sample_review_decision.json"),
    )
    for schema_name, example_name in pairs:
        schema = json.loads((ROOT / "schemas" / schema_name).read_text())
        example = json.loads((ROOT / "examples" / example_name).read_text())
        Draft202012Validator(schema).validate(example)


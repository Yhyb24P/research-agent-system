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
        ("qualification_plan.schema.json", "qualification_plan.example.json"),
        ("qualification_evidence.schema.json", "qualification_evidence.example.json"),
        ("qualification_acceptance.schema.json", "qualification_acceptance.example.json"),
        ("dq04_offhost_protection.schema.json", "dq04_offhost_protection.example.json"),
    )
    for schema_name, example_name in pairs:
        schema = json.loads((ROOT / "schemas" / schema_name).read_text())
        example = json.loads((ROOT / "examples" / example_name).read_text())
        Draft202012Validator(schema).validate(example)

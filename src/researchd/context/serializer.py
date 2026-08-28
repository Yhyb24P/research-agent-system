import hashlib
import json

from researchd.context.cloud_bundle import CloudContextBundle


def serialize_cloud_bundle(bundle: CloudContextBundle) -> bytes:
    payload = bundle.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def cloud_bundle_sha256(bundle: CloudContextBundle) -> str:
    return hashlib.sha256(serialize_cloud_bundle(bundle)).hexdigest()

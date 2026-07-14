from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from pkm_brain.google_cache import GoogleEvidenceCache
from pkm_brain.operations_http import (
    OperationsHTTPBadRequest,
    OperationsHTTPNotFound,
    operations_evidence_payload,
)
from pkm_brain.operations_policy import operations_policy_path
from pkm_brain.paths import BrainPaths
from pkm_brain.shadow_setup import default_operations_policy_payload


def _configured_paths(tmp_path: Path) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    policy_path = operations_policy_path(paths)
    policy_path.parent.mkdir(parents=True, mode=0o700)
    policy_path.write_text(
        yaml.safe_dump(
            default_operations_policy_payload(
                timezone_name="America/Los_Angeles",
                calendar_email="owner@example.com",
                gmail_email="owner@example.com",
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    policy_path.chmod(0o600)
    return paths


def test_operations_evidence_endpoint_reads_the_cited_revision_only(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)
    cache = GoogleEvidenceCache.for_paths(paths)
    now = datetime.now(timezone.utc)
    cache.write_normalized(
        "gmail",
        "gmail.primary:thread-1",
        {"subject": "First"},
        source_revision="revision-1",
        cached_at=now,
    )
    cache.write_normalized(
        "gmail",
        "gmail.primary:thread-1",
        {"subject": "Second"},
        source_revision="revision-2",
        cached_at=now,
    )

    payload = operations_evidence_payload(
        paths,
        {
            "source_type": ["gmail"],
            "account_key": ["gmail.primary"],
            "source_ref": ["gmail.primary:thread-1"],
            "source_revision": ["revision-1"],
        },
    )
    assert payload["evidence"] == {"subject": "First"}
    assert payload["source_revision"] == "revision-1"

    with pytest.raises(OperationsHTTPNotFound):
        operations_evidence_payload(
            paths,
            {
                "source_type": ["gmail"],
                "account_key": ["gmail.someone-else"],
                "source_ref": ["gmail.someone-else:thread-1"],
                "source_revision": ["revision-1"],
            },
        )


@pytest.mark.parametrize(
    "query",
    (
        {"source_type": ["gmail"], "unknown": ["value"]},
        {"source_type": ["gmail", "calendar"]},
        {"source_type": ["gmail"], "account_key": ["x" * 513]},
        {
            "source_type": ["gmail"],
            "account_key": ["gmail.primary"],
            "source_ref": ["gmail.primary:thread-1"],
            "source_revision": ["x" * 1_025],
        },
    ),
)
def test_operations_evidence_endpoint_rejects_ambiguous_or_unbounded_query(
    tmp_path: Path,
    query: dict[str, list[str]],
) -> None:
    with pytest.raises(OperationsHTTPBadRequest):
        operations_evidence_payload(
            BrainPaths.from_value(tmp_path / "brain"),
            query,
        )

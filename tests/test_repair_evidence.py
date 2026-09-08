import pytest
from test_previews import artifact as saved_artifact  # noqa: F401
from test_previews import bundle, product_factory  # noqa: F401

from tempo.product_repairs import checked_paths, eligibility
from tempo.run_snapshot import snapshot_digest
from tempo_web.artifact_views import verified_candidate_artifact

pytestmark = pytest.mark.django_db(transaction=True)


def test_published_candidate_cannot_be_repaired_even_if_run_is_marked_stopped(saved_artifact):  # noqa: F811
    run = saved_artifact.run
    run.status = "failed"
    run.save(update_fields=["status"])
    with pytest.raises(ValueError, match="checked candidate"):
        eligibility(run)


@pytest.mark.parametrize("change", ["manifest", "checkpoint", "both"])
def test_artifact_gate_rejects_unbacked_repair_receipts(saved_artifact, change):  # noqa: F811
    artifact = saved_artifact
    assert verified_candidate_artifact(artifact) == bytes(artifact.data)
    checkpoint = artifact.run.checkpoints.get(kind="product_candidate")
    forged = [{"action_id": 999, "digest": "a" * 64, "status": "succeeded", "source_sha": "b" * 40}]
    if change in {"manifest", "both"}:
        artifact.manifest["repairs"] = forged
        artifact.manifest_digest = snapshot_digest(artifact.manifest)
        artifact.save(update_fields=["manifest", "manifest_digest"])
        checkpoint.payload["artifact"]["manifest_digest"] = artifact.manifest_digest
    if change in {"checkpoint", "both"}:
        checkpoint.payload["repairs"] = forged
    checkpoint.save(update_fields=["payload"])
    with pytest.raises(ValueError, match="Repair evidence mismatch"):
        verified_candidate_artifact(artifact)


def test_mini_app_repairs_are_confined_to_target_paths():
    context = {"build_profile": {"id": "react-mini-app-v1"}}
    assert checked_paths("mini-app/scripts/\nmini-app/package.json", context) == [
        "mini-app/package.json", "mini-app/scripts/",
    ]
    for path in ("backend/", "mini-app", "mini-app/../backend/", "mini-app/.git/config"):
        with pytest.raises(ValueError):
            checked_paths(path, context)

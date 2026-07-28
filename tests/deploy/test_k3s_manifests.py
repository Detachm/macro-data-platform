from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

DEPLOY_ROOT = Path("deploy/k3s/production")


def _documents() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(DEPLOY_ROOT.rglob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        loaded = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        documents.extend(document for document in loaded if document is not None)
    return documents


def _resource(kind: str, name: str) -> dict[str, Any]:
    for document in _documents():
        if document["kind"] == kind and document["metadata"]["name"] == name:
            return document
    raise AssertionError(f"missing {kind}/{name}")


def _containers(document: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    if document["kind"] == "Pod":
        yield from document["spec"]["containers"]
    elif document["kind"] in {"Deployment", "StatefulSet", "Job"}:
        spec = document["spec"]
        yield from spec["template"]["spec"]["containers"]
    elif document["kind"] == "CronJob":
        spec = document["spec"]
        yield from spec["jobTemplate"]["spec"]["template"]["spec"]["containers"]


def test_kustomizations_reference_only_committed_local_resources() -> None:
    for path in sorted(DEPLOY_ROOT.rglob("kustomization.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for resource in document["resources"]:
            target = path.parent / resource
            assert target.is_file(), f"{path} references missing resource {resource}"
            assert target.parent == path.parent


def test_no_secret_or_unpinned_latest_image_is_committed() -> None:
    documents = _documents()
    assert all(document["kind"] != "Secret" for document in documents)
    for document in documents:
        for container in _containers(document):
            image = container["image"]
            assert ":" in image
            assert not image.endswith(":latest")
            if image.startswith("postgres:"):
                assert "@sha256:" in image

    postgres_example = _env_example("phase-1-infrastructure/postgres.env.example")
    runtime_example = _env_example("phase-1-infrastructure/runtime.env.example")
    assert postgres_example["POSTGRES_PASSWORD"] == ""
    assert postgres_example["DATABASE_URL"] == ""
    assert all(value == "" for value in runtime_example.values())


def test_postgres_uses_a_retained_100_gib_pvc_and_no_empty_dir() -> None:
    stateful_set = _resource("StatefulSet", "postgres")
    spec = stateful_set["spec"]

    assert spec["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    claim = spec["volumeClaimTemplates"][0]["spec"]
    assert claim["storageClassName"] == "local-path"
    assert claim["resources"]["requests"]["storage"] == "100Gi"
    assert all(
        "emptyDir" not in volume
        for volume in stateful_set["spec"]["template"]["spec"].get("volumes", [])
    )


def test_backup_is_atomic_verified_private_and_retained_by_count() -> None:
    cron_job = _resource("CronJob", "postgres-backup")
    assert cron_job["spec"]["schedule"] == "0 9 * * *"
    assert cron_job["spec"]["timeZone"] == "Asia/Shanghai"
    assert cron_job["spec"]["concurrencyPolicy"] == "Forbid"
    pod_spec = cron_job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    host_path = next(volume["hostPath"] for volume in pod_spec["volumes"] if "hostPath" in volume)
    assert host_path == {
        "path": "/archive/macro-data-platform/postgres-backups",
        "type": "Directory",
    }

    script = _resource("ConfigMap", "postgres-backup-script")["data"]["backup.sh"]
    for required in (
        "umask 077",
        ".partial",
        "pg_restore --list",
        "chmod 0600",
        "14",
        "8",
    ):
        assert required in script


def test_archive_capacity_monitor_alerts_on_the_confirmed_thresholds() -> None:
    cron_job = _resource("CronJob", "archive-capacity-monitor")
    assert cron_job["spec"]["schedule"] == "*/15 * * * *"
    assert cron_job["spec"]["timeZone"] == "Asia/Shanghai"
    command = next(_containers(cron_job))["command"]
    assert command[-6:] == [
        "--warning-percent",
        "70",
        "--critical-percent",
        "85",
        "--minimum-total-bytes",
        "20000000000000",
    ]


def test_migration_is_a_non_retrying_release_gate() -> None:
    job = _resource("Job", "macro-data-migration")
    assert job["spec"]["backoffLimit"] == 0
    container = next(_containers(job))
    assert container["command"] == ["alembic", "upgrade", "head"]
    assert container["imagePullPolicy"] == "Never"
    assert container["env"] == [
        {
            "name": "DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "macro-postgres", "key": "DATABASE_URL"}},
        }
    ]


def test_api_and_worker_have_probes_and_worker_has_no_host_mount_or_xtquant_token() -> None:
    api = _resource("Deployment", "macro-data-api")
    worker = _resource("Deployment", "macro-data-worker")
    api_container = next(_containers(api))
    worker_container = next(_containers(worker))

    assert api_container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert api_container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert worker_container["readinessProbe"]["exec"]["command"] == [
        "macro-data-worker",
        "--check-ready",
    ]
    host_env = next(item for item in worker_container["env"] if item["name"] == "HK_XTQUANT_HOST")
    assert host_env["valueFrom"]["fieldRef"]["fieldPath"] == "status.hostIP"
    assert "volumes" not in worker["spec"]["template"]["spec"]
    rendered = yaml.safe_dump(worker)
    assert "XTQUANT_TOKEN" not in rendered


def test_namespace_defaults_to_deny_and_only_worker_gets_provider_ports() -> None:
    default_deny = _resource("NetworkPolicy", "default-deny")
    assert default_deny["spec"] == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
    }
    worker_policy = _resource("NetworkPolicy", "allow-worker-provider-egress")
    ports = {item["port"] for item in worker_policy["spec"]["egress"][0]["ports"]}
    assert ports == {443, 10030, 58615}


def test_restore_drill_is_isolated_from_the_live_pvc() -> None:
    pod = _resource("Pod", "postgres-restore-drill")
    volumes = pod["spec"]["volumes"]
    restore_data = next(volume for volume in volumes if volume["name"] == "restore-data")
    archive = next(volume for volume in volumes if volume["name"] == "archive-backups")

    assert restore_data["emptyDir"]["sizeLimit"] == "20Gi"
    assert archive["hostPath"]["path"] == "/archive/macro-data-platform/postgres-backups"
    assert all("persistentVolumeClaim" not in volume for volume in volumes)


def _env_example(relative_path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (DEPLOY_ROOT / relative_path).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values

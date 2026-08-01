"""Catalog entries must round-trip their distributed topology through the DB.

Regression for a gap found by end-to-end testing on the fleet (2026-07-30):
`ModelCatalogEntry.multi_node` existed on the protocol model and POST
/validator/v1/catalog accepted it, but `upsert_catalog_entry` had no column to
write it to — so the topology was silently DROPPED on save. GET returned
multi_node=null and the distributed reconciler consequently never found a
distributed model to place. The unit tests at the time exercised the pydantic
model directly and never round-tripped through persistence, so nothing caught it.
"""
from greencompute_protocol import ModelCatalogEntry, MultiNodeConfig
from greencompute_validator.infrastructure.repository import ValidatorRepository


def _repo():
    return ValidatorRepository(database_url="sqlite://", bootstrap=True)


def test_multi_node_topology_survives_a_save_and_load():
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(
        model_id="kimi-k3",
        hf_repo="moonshotai/Kimi-K3",
        multi_node=MultiNodeConfig(
            node_count=8, gpus_per_node=8, tensor_parallel_size=8,
            pipeline_parallel_size=8, min_interconnect_gbps=200.0,
        ),
    ))
    loaded = repo.get_catalog_entry("kimi-k3")
    assert loaded is not None
    assert loaded.multi_node is not None, "topology was dropped on persist"
    assert loaded.multi_node.node_count == 8
    assert loaded.multi_node.gpus_per_node == 8
    assert loaded.multi_node.total_gpus == 64
    assert loaded.multi_node.min_interconnect_gbps == 200.0
    assert loaded.multi_node.is_distributed is True


def test_single_node_entries_load_with_no_topology():
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(model_id="qwen-7b", gpu_count=1))
    loaded = repo.get_catalog_entry("qwen-7b")
    assert loaded is not None and loaded.multi_node is None


def test_listing_preserves_topology():
    # The reconciler reads via list_catalog_entries, so that path matters most.
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(
        model_id="big", multi_node=MultiNodeConfig(node_count=2, gpus_per_node=4),
    ))
    entry = next(e for e in repo.list_catalog_entries() if e.model_id == "big")
    assert entry.multi_node is not None and entry.multi_node.node_count == 2


def test_updating_back_to_single_node_clears_the_topology():
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(
        model_id="m", multi_node=MultiNodeConfig(node_count=2, gpus_per_node=2),
    ))
    repo.upsert_catalog_entry(ModelCatalogEntry(model_id="m", multi_node=None))
    assert repo.get_catalog_entry("m").multi_node is None


# --- rebuild deadlock (found on the fleet 2026-07-30) -------------------------


def _flux_workload(repo, name="m"):
    """Minimal catalog workload so create_flux_deployment has something to point at."""
    from greencompute_persistence import session_scope
    from greencompute_persistence.orm import WorkloadORM
    with session_scope(repo.session_factory) as s:
        s.add(WorkloadORM(
            workload_id=f"catalog-{name}", name=name, image="vllm/vllm-openai",
            kind="inference", security_tier="standard", pricing_class="standard",
            requirements={}, runtime={}, lifecycle={}, public=False,
            metadata_json={"managed_by": "flux", "catalog_model_id": name},
        ))
    return f"catalog-{name}"


def test_failed_rank_can_be_retired_so_the_replica_can_rebuild():
    """A rank in `failed` must be force-retirable.

    terminate_flux_deployment deliberately no-ops on `failed` rows to keep them
    as history for single-node replicas. For a distributed replica that caused a
    PERMANENT deadlock: the dead ranks kept their nodes marked busy, so placement
    never found free hardware and the replica rebuilt forever without progress.
    """
    repo = _repo()
    wl = _flux_workload(repo)
    dep = repo.create_flux_deployment(
        hotkey="5A", node_id="n0", workload_id=wl,
        multi_node={"replica_id": "r1", "role": "head", "rank": 0, "model_id": "m"},
    )

    from greencompute_persistence import session_scope
    from greencompute_persistence.orm import DeploymentORM
    with session_scope(repo.session_factory) as s:
        s.get(DeploymentORM, dep).state = "failed"

    # Default behaviour preserved: a failed row is kept as history.
    assert repo.terminate_flux_deployment(dep) is False
    assert repo.list_distributed_replica_rows("m"), "still occupying its node"

    # Forced: the rank is retired and stops occupying the node.
    assert repo.terminate_flux_deployment(dep, force=True) is True
    assert repo.list_distributed_replica_rows("m") == [], "replica can now be re-placed"


def test_force_still_refuses_an_already_terminated_row():
    repo = _repo()
    wl = _flux_workload(repo, "m2")
    dep = repo.create_flux_deployment(
        hotkey="5A", node_id="n0", workload_id=wl,
        multi_node={"replica_id": "r2", "role": "head", "rank": 0, "model_id": "m2"},
    )
    assert repo.terminate_flux_deployment(dep, force=True) is True
    assert repo.terminate_flux_deployment(dep, force=True) is False  # idempotent


# --- image pin must survive persistence too ------------------------------------


def test_image_override_round_trips():
    """Third instance of the same trap: field added to the pydantic model and
    written into workload.runtime, but with no DB column it was silently
    dropped on save, so the pin never reached the node and K3 would have loaded
    on the stable image that cannot parse its architecture."""
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(
        model_id="kimi-k3", hf_repo="moonshotai/Kimi-K3",
        image_override="vllm/vllm-openai:nightly", max_model_len=32768,
    ))
    loaded = repo.get_catalog_entry("kimi-k3")
    assert loaded.image_override == "vllm/vllm-openai:nightly", "pin dropped on persist"
    assert loaded.max_model_len == 32768
    # and via the listing path the reconciler actually uses
    entry = next(e for e in repo.list_catalog_entries() if e.model_id == "kimi-k3")
    assert entry.image_override == "vllm/vllm-openai:nightly"


def test_image_override_defaults_to_none():
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(model_id="plain"))
    assert repo.get_catalog_entry("plain").image_override is None


# --- fourth instance of the trap: engine-arg passthrough -----------------------


def test_extra_engine_args_and_env_round_trip():
    """K3 on sm_120 is unservable without `--moe-backend marlin`, so if these
    are dropped on save the model silently loads down the DeepGEMM path and
    hard-asserts. Same trap as multi_node / max_model_len / image_override."""
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(
        model_id="kimi-k3",
        extra_engine_args=["--moe-backend", "marlin", "--enforce-eager"],
        extra_env={"VLLM_USE_DEEP_GEMM": "0"},
    ))
    loaded = repo.get_catalog_entry("kimi-k3")
    assert loaded.extra_engine_args == ["--moe-backend", "marlin", "--enforce-eager"]
    assert loaded.extra_env == {"VLLM_USE_DEEP_GEMM": "0"}
    # and via the listing path the reconciler actually uses
    entry = next(e for e in repo.list_catalog_entries() if e.model_id == "kimi-k3")
    assert entry.extra_engine_args[:2] == ["--moe-backend", "marlin"]


def test_extra_args_default_empty_and_can_be_cleared():
    repo = _repo()
    repo.upsert_catalog_entry(ModelCatalogEntry(model_id="plain"))
    assert repo.get_catalog_entry("plain").extra_engine_args == []
    assert repo.get_catalog_entry("plain").extra_env == {}
    repo.upsert_catalog_entry(ModelCatalogEntry(model_id="plain", extra_engine_args=["--x"]))
    assert repo.get_catalog_entry("plain").extra_engine_args == ["--x"]
    repo.upsert_catalog_entry(ModelCatalogEntry(model_id="plain"))
    assert repo.get_catalog_entry("plain").extra_engine_args == [], "must clear"

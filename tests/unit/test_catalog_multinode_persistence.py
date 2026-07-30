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

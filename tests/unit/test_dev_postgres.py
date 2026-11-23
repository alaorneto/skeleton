"""Where ``tools/dev_postgres.py`` keeps the disposable server of a checkout (#756), without a server."""

from pathlib import Path

from tools import dev_postgres


def test_data_stays_in_the_checkout_by_default(tmp_path):
    assert dev_postgres.data_dir({}, root=tmp_path) == tmp_path / "var" / "dev-postgres"
    assert dev_postgres.data_dir({dev_postgres.DATA_ENV: ""}, root=tmp_path) == tmp_path / "var" / "dev-postgres"


def test_a_shared_directory_gives_each_checkout_a_server_of_its_own(tmp_path):
    environment = {dev_postgres.DATA_ENV: str(tmp_path / "shared")}
    one = dev_postgres.data_dir(environment, root=tmp_path / "work" / "skeleton")
    other = dev_postgres.data_dir(environment, root=tmp_path / "worktrees" / "skeleton")
    assert one.parent == other.parent == tmp_path / "shared"
    assert one != other, "Two checkouts with the same folder name never share a server."
    assert one.name.startswith("skeleton-") and other.name.startswith("skeleton-")
    assert one == dev_postgres.data_dir(environment, root=tmp_path / "work" / "skeleton"), "Stable between runs."


def test_the_module_resolves_its_directory_once_from_the_environment():
    assert dev_postgres.DATA == dev_postgres.data_dir()
    assert dev_postgres.STATE == dev_postgres.DATA / "state.json"
    assert isinstance(dev_postgres.DATA, Path)

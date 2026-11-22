"""The ``cluster`` key of the agent configuration (#678): roles, refusals, the follower without owner."""

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.coordination import settings_for, validate_cluster  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from tests.unit.agent_helpers import OWNER_DSN, load_config, write_private  # noqa: E402

pytestmark = pytest.mark.posix

AGENT_DSN = "postgres://skeleton_agent:z@127.0.0.1:5432/app"


def candidate(**extra):
    return {
        "node_id": "node-a",
        "role": "candidate",
        "cluster_url": "https://node-a.internal:9443",
        "listen": "10.0.1.5:9443",
        "tls_cert": "/agent/cluster/node.crt",
        "tls_key": "/agent/cluster/node.key",
        "tls_ca": "/agent/cluster/ca.crt",
        "secret_file": "/agent/cluster/secret",
        **extra,
    }


def refused(code, values):
    with pytest.raises(AgentError) as raised:
        validate_cluster(values)
    assert raised.value.code == code


def test_cluster_shapes_are_validated():
    validate_cluster({})  # No cluster: a single node, as before.
    validate_cluster({"cluster": candidate(preferred=True)})
    refused("invalid_cluster_config", {"cluster": "node-a"})
    refused("invalid_cluster_node_id", {"cluster": candidate(node_id="Node A")})
    refused("invalid_cluster_role", {"cluster": candidate(role="leader")})
    refused("invalid_cluster_url", {"cluster": candidate(cluster_url="http://node-a:9443")})
    refused("invalid_cluster_config", {"cluster": candidate(preferred="yes")})
    refused("missing_coordination_credentials", {"cluster": candidate(role="follower")})
    refused("invalid_cluster_channel", {"cluster": candidate(listen="0.0.0.0")})
    refused("invalid_cluster_channel", {"cluster": candidate(tls_ca="ca.crt")})
    refused("invalid_cluster_channel", {"cluster": candidate(cluster_url="")})


def test_a_candidate_coordinates_with_the_owner(tmp_path):
    config = load_config(tmp_path, cluster=candidate())
    settings = settings_for(config)
    assert settings.candidate and settings.database_url == OWNER_DSN and settings.node_id == "node-a"


def test_a_follower_never_has_the_owner(tmp_path):
    coordination = write_private(tmp_path / "coordination.env", f"COORDINATION_DATABASE_URL={AGENT_DSN}\n")
    follower = candidate(role="follower", coordination_env_file=coordination)
    with pytest.raises(AgentError) as raised:
        load_config(tmp_path, cluster=follower)  # The default migration.env carries the owner.
    assert raised.value.code == "follower_has_owner"
    config = load_config(tmp_path, cluster=follower, migration_env_file=write_private(tmp_path / "empty.env", ""))
    settings = settings_for(config)
    assert not settings.candidate and settings.database_url == AGENT_DSN

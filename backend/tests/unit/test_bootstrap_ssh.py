from app.services.bootstrap import ssh
from app.services.bootstrap.ssh import SshClient, SshConnectionInfo


PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-body\n-----END OPENSSH PRIVATE KEY-----"


def test_ssh_client_loads_known_hosts_and_accepts_first_bootstrap_host(monkeypatch) -> None:
    calls: list[object] = []

    class FakeParamikoClient:
        def load_system_host_keys(self) -> None:
            calls.append("load_system_host_keys")

        def set_missing_host_key_policy(self, policy: object) -> None:
            calls.append(policy)

        def connect(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(ssh, "load_private_key", lambda private_key, passphrase: "parsed-key")
    monkeypatch.setattr(ssh.paramiko, "SSHClient", FakeParamikoClient)

    info = SshConnectionInfo(
        host="dev.example.com",
        port=22,
        username="alice",
        private_key=PRIVATE_KEY,
        passphrase=None,
    )

    with SshClient(info):
        pass

    assert calls[0] == "load_system_host_keys"
    assert isinstance(calls[1], ssh.paramiko.AutoAddPolicy)
    assert calls[2] == {
        "hostname": "dev.example.com",
        "port": 22,
        "username": "alice",
        "pkey": "parsed-key",
        "look_for_keys": False,
        "allow_agent": False,
        "timeout": 15,
    }
    assert calls[3] == "close"

import argparse
import asyncio
from contextlib import contextmanager, suppress
import logging
import os
from pathlib import Path
from typing import Iterator

from app.client_agent.config import ClientAgentConfig
from app.client_agent.runner import run_client_agent

logger = logging.getLogger(__name__)


@contextmanager
def client_agent_lock(config: ClientAgentConfig) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:
        yield
        return

    lock_path = config.install_path.expanduser() / "client-agent.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            logger.error(
                "client-agent already running",
                extra={"client_id": str(config.client_id), "lock_path": str(lock_path)},
            )
            raise SystemExit(2) from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        yield
    finally:
        with suppress(Exception):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Web Terminal ACP client agent.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    config = ClientAgentConfig.load(args.config)
    with client_agent_lock(config):
        asyncio.run(run_client_agent(config))


if __name__ == "__main__":
    main()

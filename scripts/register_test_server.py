"""Register the verified existing operator test server. No provider mutations."""

import asyncio

from cloud_platform.core.container import close_container, get_container
from cloud_platform.modules.servers.test_fixture import register_test_server


async def main() -> None:
    try:
        container = await get_container()
        await container.reconcile_superadmins()
        server = await register_test_server(container)
        print(f"Registered shared operator test server: {server.id}")
    finally:
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
from controllers.jks_discovery_controller import JksDiscoveryController

async def main():
    print("Testing JKS Discovery with Pod mapping...")
    controller = JksDiscoveryController()
    res = await controller.run({"mode": "limit", "limit": 20})
    print(f"Total certs found in this test run: {res.get('total_certs')}")

if __name__ == "__main__":
    asyncio.run(main())

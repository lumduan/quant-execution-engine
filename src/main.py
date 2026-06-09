import asyncio
import logging

logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("hello from python-template")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

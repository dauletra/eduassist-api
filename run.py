import sys
import asyncio

# ВАЖНО: Устанавливаем event loop policy ДО любых импортов
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    # Создаем свой event loop до импорта main (который импортирует Azure SDK)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        access_log=True,
        loop="none"
    )
    server = uvicorn.Server(config)

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    finally:
        # Cancel all pending tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()

        # Wait for all tasks to complete cancellation
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        # Shutdown any remaining async generators
        loop.run_until_complete(loop.shutdown_asyncgens())

        # Close the event loop
        loop.close()
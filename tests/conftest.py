import asyncio

import pytest
from asgiref.sync import sync_to_async
from django.db import connections


@pytest.fixture(autouse=True)
def close_async_database_connections(request):
    """Close the async ORM thread's connection before pytest drops its test database."""
    yield
    if request.node.get_closest_marker("django_db"):
        # Django's synchronous test teardown only closes connections in its own thread.
        # Running a new loop here targets asgiref's shared thread-sensitive executor.
        asyncio.run(sync_to_async(connections.close_all, thread_sensitive=True)())

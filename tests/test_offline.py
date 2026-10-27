"""Stage 5 acceptance tests: from inside the stack, the internet cannot be reached.

These only mean something inside a container on the offline network, so they run from
`docker compose run --rm tests`, which sets OFFLINE_EXPECTED=1. On the laptop they are skipped,
because the laptop itself is online. The other half of the stage 5 check, "the gateway still
answers", is test_gateway.py running in the same container in the same run.
Sources: Docker Compose networks page ("internal"); Python docs, socket.create_connection.
"""
import os  # reads OFFLINE_EXPECTED
import socket  # opens raw TCP connections and does DNS lookups

import pytest  # test runner

pytestmark = pytest.mark.skipif(
    os.environ.get("OFFLINE_EXPECTED") != "1",  # set only by the tests service in docker-compose.yml
    reason="only meaningful inside the offline network; run `docker compose run --rm tests`",
)

# Well-known public addresses that are always up, so a failure means "blocked", not "server down"
PUBLIC = [("1.1.1.1", 443), ("8.8.8.8", 53), ("140.82.112.3", 443)]  # Cloudflare, Google DNS, GitHub


@pytest.mark.parametrize("host,port", PUBLIC)
def test_public_internet_is_unreachable(host, port):
    with pytest.raises(OSError):  # any connection error counts as blocked
        socket.create_connection((host, port), timeout=5)  # try to open a TCP connection out


def test_public_names_do_not_resolve():
    with pytest.raises(OSError):  # socket.gaierror is a subclass of OSError
        socket.getaddrinfo("pypi.org", 443)  # no outside DNS either


def test_stack_names_still_resolve():
    # Control: the network itself works, so the failures above are the isolation, not a broken container
    assert socket.getaddrinfo("litellm", 4000)  # Docker's internal DNS still answers for services

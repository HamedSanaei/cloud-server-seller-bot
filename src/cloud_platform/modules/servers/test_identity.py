"""Reserved provider identity; drifting metadata never turns it into a customer VPS."""

from typing import Any

TEST_NAME = "leaseweb-test"
TEST_PROVIDER_KEY = "leaseweb"
TEST_PROVIDER_ID = "12717238"
TEST_IPV4 = "217.20.121.89"


def is_test_resource(server: Any) -> bool:
    return bool(
        server is not None
        and server.provider_key == TEST_PROVIDER_KEY
        and server.provider_server_id == TEST_PROVIDER_ID
    )


def is_test_server(server: Any) -> bool:
    return bool(
        is_test_resource(server)
        and server.ipv4 == TEST_IPV4
        and str(server.funding_source) == "operator"
        and bool(getattr(server, "credential_account_id", None))
        and getattr(server, "funding_operator_id", None) == server.user_id
    )

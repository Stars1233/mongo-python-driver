# Copyright 2018-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test connections to various Atlas cluster types."""
from __future__ import annotations

import os
import sys
import unittest
from collections import defaultdict
from test import PyMongoTestCase

import pytest

sys.path[0:0] = [""]

import pymongo
from pymongo.ssl_support import _has_sni

pytestmark = pytest.mark.atlas_connect


URIS = {
    "ATLAS_REPL": os.environ.get("ATLAS_REPL"),
    "ATLAS_SHRD": os.environ.get("ATLAS_SHRD"),
    "ATLAS_FREE": os.environ.get("ATLAS_FREE"),
    "ATLAS_TLS11": os.environ.get("ATLAS_TLS11"),
    "ATLAS_TLS12": os.environ.get("ATLAS_TLS12"),
    "ATLAS_SRV_REPL": os.environ.get("ATLAS_SRV_REPL"),
    "ATLAS_SRV_SHRD": os.environ.get("ATLAS_SRV_SHRD"),
    "ATLAS_SRV_FREE": os.environ.get("ATLAS_SRV_FREE"),
    "ATLAS_SRV_TLS11": os.environ.get("ATLAS_SRV_TLS11"),
    "ATLAS_SRV_TLS12": os.environ.get("ATLAS_SRV_TLS12"),
    "ATLAS_X509_DEV_WITH_CERT": os.environ.get("ATLAS_X509_DEV_WITH_CERT"),
}


class TestAtlasConnect(PyMongoTestCase):
    def connect(self, uri):
        if not uri:
            raise Exception("Must set env variable to test.")
        client = self.simple_client(uri)
        # No TLS error
        client.admin.command("ping")
        # No auth error
        client.test.test.count_documents({})

    @unittest.skipUnless(_has_sni(True), "Free tier requires SNI support")
    def test_free_tier(self):
        self.connect(URIS["ATLAS_FREE"])

    def test_replica_set(self):
        self.connect(URIS["ATLAS_REPL"])

    def test_sharded_cluster(self):
        self.connect(URIS["ATLAS_SHRD"])

    def test_tls_11(self):
        self.connect(URIS["ATLAS_TLS11"])

    def test_tls_12(self):
        self.connect(URIS["ATLAS_TLS12"])

    def connect_srv(self, uri):
        self.connect(uri)
        self.assertIn("mongodb+srv://", uri)

    @unittest.skipUnless(_has_sni(True), "Free tier requires SNI support")
    def test_srv_free_tier(self):
        self.connect_srv(URIS["ATLAS_SRV_FREE"])

    def test_srv_replica_set(self):
        self.connect_srv(URIS["ATLAS_SRV_REPL"])

    def test_srv_sharded_cluster(self):
        self.connect_srv(URIS["ATLAS_SRV_SHRD"])

    def test_srv_tls_11(self):
        self.connect_srv(URIS["ATLAS_SRV_TLS11"])

    def test_srv_tls_12(self):
        self.connect_srv(URIS["ATLAS_SRV_TLS12"])

    def test_x509_with_cert(self):
        self.connect(URIS["ATLAS_X509_DEV_WITH_CERT"])

    def test_uniqueness(self):
        """Ensure that we don't accidentally duplicate the test URIs."""
        uri_to_names = defaultdict(list)
        for name, uri in URIS.items():
            if uri:
                uri_to_names[uri].append(name)
        duplicates = [names for names in uri_to_names.values() if len(names) > 1]
        self.assertFalse(
            duplicates,
            f"Error: the following env variables have duplicate values: {duplicates}",
        )


if __name__ == "__main__":
    unittest.main()

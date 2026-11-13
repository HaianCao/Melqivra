"""CPU-safe PostgreSQL migration catalog policy tests."""

from __future__ import annotations

import unittest
from importlib import resources

from agent_music.postgres_migrations import (
    POSTGRES_MIGRATIONS,
    PostgresMigration,
    PostgresMigrationError,
    validate_postgres_migration_catalog,
)

EXPECTED_SHA256 = {
    1: "9ebe14df2d5bad6dff3cc7cff07f3d93299f68019726d631b6d09e61c1158149",
    2: "f234f2ff8ad1af4881ebf4a937076e61ca969536339cfe6277fd57527cf65876",
    3: "ff2a956f2d2b635f9df7854a766a6cc65f3993e77e5961b4a309cc79c22cf70e",
    4: "121232d5ae4a6ae554e8081deefb7cc9649002b194c1dfa6aa5e1e08fa37b59d",
    5: "5ef9b936dd9f91764f60002b3566a24f8f571224e50067df02b9c6d97d2df607",
}


class PostgresMigrationCatalogTests(unittest.TestCase):
    def test_catalog_is_ordered_packaged_and_checksum_stable(self) -> None:
        validate_postgres_migration_catalog()
        self.assertEqual([1, 2, 3, 4, 5], [item.version for item in POSTGRES_MIGRATIONS])
        for migration in POSTGRES_MIGRATIONS:
            with self.subTest(version=migration.version):
                resource = resources.files("agent_music").joinpath(migration.resource)
                self.assertTrue(resource.is_file())
                self.assertEqual(EXPECTED_SHA256[migration.version], migration.read()[1])

    def test_catalog_rejects_gaps_duplicates_and_ambiguous_names(self) -> None:
        valid = POSTGRES_MIGRATIONS[0]
        cases = (
            (
                valid,
                PostgresMigration(3, "gap", "migrations/postgresql/0003_add_gap.sql"),
            ),
            (valid, valid),
            (
                PostgresMigration(
                    1,
                    "ambiguous",
                    "migrations/postgresql/0001_job_queue.sql",
                ),
            ),
        )
        for catalog in cases:
            with self.subTest(catalog=catalog), self.assertRaises(PostgresMigrationError):
                validate_postgres_migration_catalog(catalog)


if __name__ == "__main__":
    unittest.main()

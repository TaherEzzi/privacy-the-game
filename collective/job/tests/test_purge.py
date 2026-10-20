import unittest
from pg_harness import PgHarness, SQL_DIR, WEBSITE

PG = PgHarness("rc-sqltest-1")


def setUpModule():
    PG.start()


def tearDownModule():
    PG.stop()


class PurgeTest(unittest.TestCase):
    def setUp(self):
        PG.reset()

    def purge(self):
        PG.run_files(SQL_DIR / "purge.sql", SQL_DIR / "purge_mig.sql", variables={"website": WEBSITE})

    def counter(self, name="opted_out_total"):
        return int(PG.sql(f"SELECT value FROM rc_collective.counters WHERE name = '{name}';").strip())

    def early(self):
        return self.counter("ghosts_early_total")

    def test_opted_out_session_is_gone_from_every_table(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "mission-completed", {"mission": "gmail-recon-breach", "finding": "3plus-breaches"})
        PG.insert_event(sid, "opted-out")
        PG.sql(f"INSERT INTO session_data (session_data_id, website_id, session_id, data_key) VALUES (gen_random_uuid(), '{WEBSITE}', '{sid}', 'k');")
        PG.sql(f"INSERT INTO revenue VALUES (gen_random_uuid(), '{WEBSITE}', '{sid}');")
        PG.sql(f"INSERT INTO session_replay VALUES (gen_random_uuid(), '{WEBSITE}', '{sid}');")
        PG.sql(f"INSERT INTO _rc_mig_sessions SELECT * FROM session WHERE session_id = '{sid}';")
        PG.sql(f"INSERT INTO _rc_mig_events SELECT * FROM website_event WHERE session_id = '{sid}';")
        PG.sql("INSERT INTO _rc_mig_event_data SELECT * FROM event_data;")
        self.purge()
        for table in ("session", "website_event", "event_data", "session_data", "revenue", "session_replay",
                      "_rc_mig_sessions", "_rc_mig_events"):
            self.assertEqual(PG.count(table, sid), 0, table)
        self.assertEqual(int(PG.sql("SELECT count(*) FROM _rc_mig_event_data;").strip()), 0)

    def test_counter_rises_by_sessions_not_events(self):
        a = PG.insert_session()
        b = PG.insert_session()
        PG.insert_event(a, "opted-out")
        PG.insert_event(a, "opted-out")  # double click
        PG.insert_event(b, "opted-out")
        self.purge()
        self.assertEqual(self.counter(), 2)
        self.purge()  # nothing left to purge: counter must not move
        self.assertEqual(self.counter(), 2)

    def test_other_sessions_and_ghosts_are_untouched(self):
        keep = PG.insert_session()
        ghost = PG.insert_session()
        PG.insert_event(keep, "mission-completed", {"mission": "gmail-recon-breach"})
        PG.insert_event(ghost, "went-ghost")
        self.purge()
        self.assertEqual(PG.count("website_event", keep), 1)
        self.assertEqual(PG.count("event_data", keep), 1)
        self.assertEqual(PG.count("website_event", ghost), 1)
        self.assertEqual(self.counter(), 0)

    def test_other_websites_are_never_touched(self):
        other = "00000000-0000-0000-0000-000000000001"
        sid = PG.insert_session(website=other)
        PG.insert_event(sid, "opted-out", website=other)
        self.purge()
        self.assertEqual(PG.count("session", sid), 1)
        self.assertEqual(self.counter(), 0)


T1, T2, T3 = "now() - interval '3 hours'", "now() - interval '2 hours'", "now() - interval '1 hour'"


class EarlyGhostTest(unittest.TestCase):
    def setUp(self):
        PG.reset()

    purge = PurgeTest.purge
    counter = PurgeTest.counter
    early = PurgeTest.early

    def test_early_ghost_is_purged_and_counted_as_a_ghost_not_an_opt_out(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "mission-completed", {"mission": "gmail-recon-breach"}, at=T1)
        PG.insert_event(sid, "went-ghost-early", at=T2)
        self.purge()
        self.assertEqual(PG.count("session", sid), 0)
        self.assertEqual(PG.count("website_event", sid), 0)
        self.assertEqual(self.early(), 1)
        self.assertEqual(self.counter(), 0)
        self.purge()
        self.assertEqual(self.early(), 1)

    def test_cancelled_early_ghost_is_kept_and_not_counted(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "went-ghost-early", {"nonce": "n1"}, at=T1)
        PG.insert_event(sid, "ghost-cancelled", {"nonce": "n1"}, at=T2)
        self.purge()
        self.assertEqual(PG.count("website_event", sid), 2)
        self.assertEqual(self.early(), 0)
        self.assertEqual(self.counter(), 0)

    def test_cancel_then_early_ghost_again_is_purged(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "went-ghost-early", {"nonce": "n1"}, at=T1)
        PG.insert_event(sid, "ghost-cancelled", {"nonce": "n1"}, at=T2)
        PG.insert_event(sid, "went-ghost-early", {"nonce": "n2"}, at=T3)
        self.purge()
        self.assertEqual(PG.count("session", sid), 0)
        self.assertEqual(self.early(), 1)

    def test_early_ghost_who_also_opted_out_counts_once_as_an_opt_out(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "went-ghost-early", at=T1)
        PG.insert_event(sid, "opted-out", at=T2)
        self.purge()
        self.assertEqual(PG.count("session", sid), 0)
        self.assertEqual(self.counter(), 1)
        self.assertEqual(self.early(), 0)

    def test_final_mission_ghost_is_still_kept(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "went-ghost")
        self.purge()
        self.assertEqual(PG.count("website_event", sid), 1)
        self.assertEqual(self.early(), 0)


class CancelAndRestoreTest(unittest.TestCase):
    def setUp(self):
        PG.reset()

    purge = PurgeTest.purge
    counter = PurgeTest.counter
    early = PurgeTest.early

    def test_cancelled_opt_out_is_kept_and_not_counted(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "opted-out", {"nonce": "n1"}, at=T1)
        PG.insert_event(sid, "opt-out-cancelled", {"nonce": "n1"}, at=T2)
        self.purge()
        self.assertEqual(PG.count("website_event", sid), 2)
        self.assertEqual(self.counter(), 0)

    def test_opt_out_again_after_cancelling_is_purged(self):
        sid = PG.insert_session()
        PG.insert_event(sid, "opted-out", {"nonce": "n1"}, at=T1)
        PG.insert_event(sid, "opt-out-cancelled", {"nonce": "n1"}, at=T2)
        PG.insert_event(sid, "opted-out", {"nonce": "n2"}, at=T3)
        self.purge()
        self.assertEqual(PG.count("session", sid), 0)
        self.assertEqual(self.counter(), 1)


if __name__ == "__main__":
    unittest.main()

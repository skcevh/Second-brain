from datetime import UTC, datetime, timedelta
import base64
import email.message
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from brain_core import encoded
from brain_store import database
from mail_oauth import GMAIL_API, GRAPH_API, MailError, connect, load_credentials, provider_endpoints, save_credentials
from mail_sync import gmail_collect, graph_pages
from secondbrain import receive, sync

GRAPH = {"provider": "graph", "email": "graph@example.invalid", "project": "E-mail",
         "initial_since": "2025-01-01T00:00:00Z"}
GMAIL = {"provider": "gmail", "email": "gmail@example.invalid", "project": "E-mail",
         "initial_since": "2025-01-01T00:00:00Z"}


def graph_message(mid, text):
    return {"id": mid, "internetMessageId": f"<{mid}@example.invalid>", "subject": "Synthetic archive message",
            "body": {"contentType": "HTML", "content": f"<p>{text}</p><script>unindexed</script>"},
            "sentDateTime": "2026-11-18T10:00:00Z", "lastModifiedDateTime": "2026-11-18T10:00:00Z"}


def gmail_message(mid):
    message = email.message.EmailMessage()
    message["From"] = "sender@example.invalid"
    message["Date"] = "Wed, 18 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = f"<{mid}@example.invalid>"
    message["Subject"] = "Synthetic Gmail mail"
    message.set_content("GmailSearchMarker")
    return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")}


class MailSyncTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.inbox = self.state / "inbox"
        self.inbox.mkdir()
        self.options = {"inbox": str(self.inbox), "sources": {"outlook": True},
                        "reviewed_drop_projects": ["E-mail"], "mail_accounts": {"ms": GRAPH, "gg": GMAIL}}
        (self.state / "client.json").write_bytes(encoded(self.options))
        (self.state / "server.json").write_bytes(encoded({"inbox": str(self.inbox), "accepted_sources": ["outlook"]}))

    def test_scheduled_pipeline_pages_retries_and_deduplicates(self):
        cutoff = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
        eml = self.state / "imports" / "outlook" / "E-mail" / "known.eml"
        eml.parent.mkdir(parents=True)
        backup = gmail_message("already-archived")["raw"]
        eml.write_bytes(base64.urlsafe_b64decode(backup + "=" * (-len(backup) % 4)))
        fail_history = False

        def response(url, **kwargs):
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            if parsed.path == "/v1.0/me/messages":
                if "$skiptoken" in query:
                    private = graph_message("do-not-store", "PRIVATE")
                    private["singleValueExtendedProperties"] = [{"id": "Integer 0x0036", "value": "2"}]
                    return {"value": [private]}
                self.assertIn("lastModifiedDateTime ge", query["$filter"][0])
                return {"value": [graph_message("already-archived", "Graph copy"), graph_message("new-ms", "GraphSearchMarker")],
                        "@odata.nextLink": GRAPH_API + "/me/messages?$skiptoken=next"}
            if url == GMAIL_API + "/profile":
                return {"historyId": "100"}
            if parsed.path.endswith("/history"):
                if fail_history:
                    raise MailError(503)
                self.assertEqual(query["startHistoryId"], ["100"])
                return {"historyId": "101", "history": [{"messagesAdded": [{"message": {"id": "new-gg"}}]}]}
            if parsed.path.endswith("/messages"):
                self.assertTrue(query["q"][0].startswith("after:"))
                return {"messages": [{"id": "already-archived"}]}
            return gmail_message(parsed.path.rsplit("/", 1)[-1])

        with (patch("mail_sync.access_token", return_value="synthetic-token"),
              patch("mail_sync.request_json", side_effect=response),
              patch("mail_sync.datetime") as time_source):
            time_source.now.return_value = cutoff
            self.assertEqual(sync(self.state)["source_errors"], 0)
            receive(self.state)
            fail_history = True
            time_source.now.return_value = cutoff + timedelta(minutes=5)
            self.assertEqual(sync(self.state)["source_errors"], 1)
            with database(self.state / "client.sqlite") as db:
                self.assertEqual(json.loads(db.execute("SELECT cursor FROM mail_sync WHERE alias='gg'").fetchone()[0]),
                                 {"history": "100"})
            fail_history = False
            time_source.now.return_value = cutoff + timedelta(minutes=10)
            self.assertEqual(sync(self.state)["source_errors"], 0)
            receive(self.state)
        with database(self.state / "archive.sqlite") as db:
            ids = {row[0] for row in db.execute("SELECT source_id FROM archive")}
            self.assertEqual(ids, {"already-archived@example.invalid", "new-ms@example.invalid", "new-gg@example.invalid"})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_fts WHERE archive_fts MATCH 'GraphSearchMarker'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_fts WHERE archive_fts MATCH 'unindexed'").fetchone()[0], 0)

    def test_gmail_recovers_expired_history(self):
        def reply(url, **_kwargs):
            if "/history?" in url:
                raise MailError(404)
            if url.endswith("/profile"):
                return {"historyId": "200"}
            if "/messages?" in url:
                return {"messages": [{"id": "recovered"}]}
            return gmail_message("recovered")

        self.options["sources"]["outlook"] = False
        (self.state / "client.json").write_bytes(encoded(self.options))
        sync(self.state)
        with (patch("mail_sync.request_json", side_effect=reply),
              database(self.state / "client.sqlite") as db):
            queued, cursor = gmail_collect(db, "synthetic-token", GMAIL, "gg", {"history": "1"})
        self.assertEqual((queued, cursor), (1, {"history": "200"}))

    def test_graph_cannot_send_token_to_a_foreign_next_link(self):
        with patch("mail_sync.request_json", return_value={"value": [], "@odata.nextLink": "https://example.invalid/"}) as api:
            with self.assertRaisesRegex(ValueError, "Graph-paginaverwijzing"):
                list(graph_pages("synthetic-token", 1763456400, 1763456700))
            self.assertEqual(api.call_count, 1)

    def test_consent_checks_browser_account_before_saving(self):
        response = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
        with (patch("mail_oauth.interactive_code", return_value=("code", "verifier", "http://localhost:9000/")),
              patch("mail_oauth.token_request", return_value=response),
              patch("mail_oauth.request_json", return_value={"mail": "another@example.invalid"}),
              patch("mail_oauth.save_credentials") as store):
            with self.assertRaisesRegex(ValueError, "komt niet overeen"):
                connect(self.state, "ms", GRAPH, "10000000-0000-0000-0000-000000000000", None)
            store.assert_not_called()

    def test_hotmail_can_use_the_personal_microsoft_authority(self):
        authorize, token, _ = provider_endpoints("graph", "consumers")
        self.assertEqual(urlsplit(authorize).path, "/consumers/oauth2/v2.0/authorize")
        self.assertEqual(urlsplit(token).path, "/consumers/oauth2/v2.0/token")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_windows_credentials_are_encrypted_and_can_be_reopened(self):
        values = {"refresh_token": "fictitious-oauth-value"}
        save_credentials(self.state, "gg", values)
        self.assertNotIn(values["refresh_token"].encode(), (self.state / "mail-gg.dpapi").read_bytes())
        self.assertEqual(load_credentials(self.state, "gg"), values)


if __name__ == "__main__":
    unittest.main()

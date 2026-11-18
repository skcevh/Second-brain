import email.message
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from brain_core import digest, encoded
from brain_sources import pst_body
from brain_store import PACKET

CLI = Path(__file__).resolve().parents[1] / "secondbrain.py"
SESSION = "10101010-abcdef12-0000-0000-000000000000"
SHARED_COPILOT = "12121212-0000-0000-0000-000000000000"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.base = Path(base.name)
        self.client = self.base / "client"
        self.server = self.base / "server"
        self.inbox = self.server / "inbox"
        self.client.mkdir()
        self.server.mkdir()
        self.work = self.base / "workspace"
        self.work.mkdir()
        self.denied = self.work / "denied"
        self.denied.mkdir()
        self.rollouts = self.base / "rollouts"
        self.rollouts.mkdir()
        self.config = {
            "inbox": str(self.inbox),
            "sources": {"codex": True, "chatgpt": True, "outlook": True,
                        "copilot-vscode": True, "copilot-cli": True, "instructions": True},
            "source_roots": {"codex": [str(self.rollouts)], "copilot-cli": [], "copilot-vscode": []},
            "projects": {"Project Test": [str(self.work)]},
            "deny_paths": [str(self.denied)],
            "reviewed_drop_projects": ["Project Test"],
        }
        self.write(self.server / "server.json", {"inbox": str(self.inbox), "accepted_sources": list(self.config["sources"])})
        self.write(self.client / "client.json", self.config)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded(value))

    def cli(self, root, *args):
        command = subprocess.run([sys.executable, CLI, "--state", str(root), *args], check=True,
                                 capture_output=True, text=True, encoding="utf-8")
        return json.loads(command.stdout)

    def codex(self, name, workspace, marker, extra=None):
        path = self.rollouts / f"rollout-{name}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": name, "cwd": str(workspace), "timestamp": "2026-01-01T00:00:00Z"}},
            {"type": "response_item", "timestamp": "2026-01-01T00:01:00Z",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": marker}]}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "internal-only tool output"}},
        ]
        if extra:
            records.extend(extra)
        path.write_bytes(b"\n".join(map(encoded, records)) + b"\n")
        return path

    def test_offline_queue_update_and_search(self):
        path = self.codex(SESSION, self.work, "Indigo timeouts via OData")
        blocked = self.codex("denied", self.denied, "forbidden workspace")
        before = digest(path.read_bytes()), digest(blocked.read_bytes())
        offline = self.cli(self.client, "sync")
        self.assertEqual((offline["offline"], offline["queued"], offline["pending"]), (True, 1, 1))
        self.inbox.mkdir()
        self.assertEqual(self.cli(self.client, "sync")["sent"], 1)
        packet = next(self.inbox.glob("*.json"))
        data = packet.read_bytes()
        self.assertEqual(self.cli(self.server, "receive")["updated"], 1)
        packet.write_bytes(data)
        self.assertEqual(self.cli(self.server, "receive")["duplicates"], 1)
        self.assertEqual(self.cli(self.client, "sync")["sent"], 0)
        hit, = self.cli(self.server, "search", "Wat was de oplossing voor de Indigo timeouts?", "--json")
        self.assertEqual((hit["source"], hit["id"], hit["reference"], hit["project"]),
                         ("codex", SESSION, f"codex:{SESSION}", "Project Test"))
        self.assertEqual(self.cli(self.server, "search", "internal-only forbidden", "--json"), [])
        self.assertEqual((digest(path.read_bytes()), digest(blocked.read_bytes())), before)
        extra = [{
            "type": "response_item", "timestamp": "2026-01-01T00:02:00Z",
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Aangepaste architectuur"}]},
        }]
        self.codex(SESSION, self.work, "Indigo timeouts via OData", extra)
        self.assertEqual(self.cli(self.client, "sync")["sent"], 1)
        self.assertEqual(self.cli(self.server, "receive")["updated"], 1)
        self.assertEqual(len(self.cli(self.server, "search", "architectuur", "--json")), 1)
        self.cli(self.server, "annotate", "codex", SESSION, "oplossing", "Notitie", "Benut de lokale parser")
        hit, = self.cli(self.server, "search", "benut", "--json")
        self.assertEqual(hit["status"], "voorstel")
        backup = self.base / "backup.sqlite"
        recovered = self.base / "recovered.sqlite"
        self.cli(self.server, "backup", str(backup))
        self.cli(self.server, "restore", str(backup), str(recovered))
        self.write(self.server / "server.json", {"inbox": str(self.inbox), "archive": str(recovered)})
        self.assertEqual(self.cli(self.server, "search", "benut", "--json"), [hit])

    def test_export_sources_deduplicate_and_keep_references(self):
        created = 1767225600
        sessions = [{
            "id": "a0000000-0000-0000-0000-000000000001", "title": "Fictieve architectuur",
            "create_time": created,
            "mapping": {"b": {"message": {
                "id": "b", "author": {"role": "assistant"}, "create_time": created,
                "content": {"parts": ["Hercule referentiedocument"]},
                "metadata": {"citations": [{"url": "https://example.invalid/doc?private=ignored"}]},
            }}},
        }]
        drop = self.client / "imports"
        chatgpt = drop / "chatgpt" / "Project Test" / "example.zip"
        chatgpt.parent.mkdir(parents=True)
        with zipfile.ZipFile(chatgpt, "w") as archive:
            archive.writestr("export/conversations-001.json", encoded(sessions))
            archive.writestr("unrelated_account_data.json", "fictief: beslist niet importeren")
        self.write(chatgpt.with_suffix(".json"), sessions)

        cli_root = self.base / "copilot"
        self.config["source_roots"]["copilot-cli"] = [str(cli_root)]
        self.config["approved_exports"] = {"copilot-vscode": {SHARED_COPILOT: "Project Test"}}
        self.write(self.client / "client.json", self.config)
        events = cli_root / SHARED_COPILOT / "events.jsonl"
        events.parent.mkdir(parents=True)
        events.write_bytes(b"\n".join(map(encoded, [
            {"type": "session.start", "timestamp": "2026-01-01T00:00:00Z",
             "data": {"sessionId": SHARED_COPILOT, "context": {"cwd": str(self.work)}}},
            {"type": "user.message", "timestamp": "2026-01-01T00:00:00Z", "data": {"content": "Copilot mijn machine"}},
        ])))
        self.write(drop / "copilot-vscode" / "vscode.json", {
            "sessionId": SHARED_COPILOT, "creationDate": created * 1000,
            "requests": [{"timestamp": created * 1000, "message": {"text": "Copilot mijn machine"},
                          "response": [{"value": "Reanimatieplan"}]}],
        })
        eml = email.message.EmailMessage()
        eml["From"] = "sender@example.invalid"
        eml["To"] = "reader@example.invalid"
        eml["Subject"] = "E-mail over netwerkswitch"
        eml["Message-ID"] = "<testmail@example.invalid>"
        eml["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
        secret = "zyx" + "9876543210"
        eml.set_content("De netwerkswitch staat klaar.\n" + "password" + "=" + secret)
        eml.add_attachment(b"should_not_import_attachment", maintype="text", subtype="plain", filename="secret.txt")
        mail = drop / "outlook" / "Project Test" / "mail.eml"
        mail.parent.mkdir(parents=True)
        mail.write_bytes(eml.as_bytes())

        self.inbox.mkdir()
        self.assertEqual(self.cli(self.client, "sync")["sent"], 4)
        self.cli(self.server, "receive")
        counts = self.cli(self.server, "status")["server"]["records"]
        self.assertEqual(counts, {"chatgpt": 1, "copilot-vscode": 1, "outlook": 1})
        row = self.cli(self.server, "show", "chatgpt", sessions[0]["id"])
        self.assertEqual(row["messages"][0]["refs"], ["https://example.invalid/doc"])
        row = self.cli(self.server, "show", "outlook", "testmail@example.invalid")
        self.assertIn("[GEHEIM GEWEERD]", row["messages"][0]["text"])
        self.assertNotIn(secret, row["messages"][0]["text"])
        self.assertNotIn("should_not_import_attachment", row["messages"][0]["text"])
        self.assertEqual(self.cli(self.client, "sync")["sent"], 0)

    def test_tampered_packet_and_destination_change(self):
        self.codex(SESSION, self.work, "Afgeschermd testbericht")
        self.assertEqual(self.cli(self.client, "sync")["pending"], 1)
        self.inbox.mkdir()
        self.config["deny_paths"].append(str(self.work))
        self.write(self.client / "client.json", self.config)
        self.assertEqual(self.cli(self.client, "sync")["sent"], 0)
        self.assertEqual(list(self.inbox.iterdir()), [])
        bogus = self.inbox / ("0" * 64 + ".json")
        self.assertIsNotNone(PACKET.fullmatch(bogus.name))
        bogus.write_bytes(b'{"tampered":true}')
        self.assertEqual(self.cli(self.server, "receive")["rejected"], 1)
        self.assertTrue(bogus.with_suffix(".rejected").is_file())

    def test_vscode_workspacelog_and_partial_line(self):
        root = self.base / "vscode"
        self.config["source_roots"]["copilot-vscode"] = [str(root)]
        self.write(self.client / "client.json", self.config)
        self.write(root / "workspace" / "workspace.json", {"folder": self.work.as_uri()})
        log = root / "workspace" / "chatSessions" / "vscode.jsonl"
        log.parent.mkdir()
        log.write_bytes(b"\n".join(map(encoded, [
            {"kind": 0, "v": {"sessionId": "vscode", "requests": []}},
            {"kind": 3, "k": ["requests"], "v": [{"message": {"text": "Werkruimte Proef"},
                                                 "response": ["eerste"]}]},
            {"kind": 1, "k": ["requests", 0, "response", 0], "v": "Laatste antwoord"},
        ])) + b'\n{"kind":')
        self.inbox.mkdir()
        self.assertEqual(self.cli(self.client, "sync")["sent"], 1)
        self.cli(self.server, "receive")
        hit, = self.cli(self.server, "search", "laatste antwoord", "--json")
        self.assertEqual(hit["source"], "copilot-vscode")

    def test_outlook_pst_missing_body_and_read_errors(self):
        class ExportedMail:
            plain_text_body = b"synthetic body"

            @property
            def html_body(self):
                raise OSError("libpff_message_get_html_body_size: missing value data.")

        class DamagedMail:
            @property
            def html_body(self):
                raise OSError("PST read failure")

        self.assertEqual(pst_body(ExportedMail(), "plain_text_body"), b"synthetic body")
        self.assertIsNone(pst_body(ExportedMail(), "html_body"))
        with self.assertRaisesRegex(OSError, "PST read failure"):
            pst_body(DamagedMail(), "html_body")


if __name__ == "__main__":
    unittest.main()

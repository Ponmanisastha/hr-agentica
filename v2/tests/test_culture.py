"""Culture and HR activities: events behind budget approval, RSVPs, kudos, awards, anonymous pulse surveys, the API."""

import json
import threading
import unittest
import urllib.request

from tests.common import ADMIN, as_user, bootstrap

from hrai import auth, config, db, engage, triggers
from hrai import tools as T
from hrai.gateway import agent_gateway as G

A, B = "E921", "E922"
TODAY = config.today()


def setUpModule():
    bootstrap()
    for eid, name, dob, joined in ((A, "Meera Culture", "1994-10-20", "2022-10-10"),
                                   (B, "Arjun Culture", "1990-03-02", "2024-11-01")):
        db.x("INSERT OR REPLACE INTO employees (id, name, email, department, level, manager, manager_email, annual, "
             "sick, casual, date_of_birth, joined_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (eid, name, f"{eid.lower()}@example.com", "Engineering", "L3", "Arun Kumar", "arun.kumar@example.com",
              12, 6, 4, dob, joined))
    auth.create_user("hr3", "hr-password-3", "hr")
    auth.create_user("meera", "meera-password-1", "employee", A)


class EventTests(unittest.TestCase):
    def test_a_big_budget_waits_for_a_human(self):
        with as_user(ADMIN):
            e = engage.create_event("Annual day", "2026-11-20", "festival", budget=engage.budget_limit() + 5000)
            self.assertEqual(e["budget_status"], "pending_approval")
            self.assertIn("approval_id", e)
            with self.assertRaises(engage.EngageError):
                engage.announce(e["event_id"])              # nothing goes out while the money is unapproved
        with as_user(auth.User(6, "hr3", "hr")):
            T.decide_approval(e["approval_id"], True)
        self.assertEqual(engage.get(e["event_id"])["budget_status"], "approved")
        with as_user(ADMIN):
            out = engage.announce(e["event_id"])
        self.assertEqual(out["status"], "announced")
        self.assertTrue(db.q1("SELECT COUNT(*) AS n FROM outbox WHERE subject LIKE '%Annual day%'")["n"])

    def test_a_small_budget_goes_straight_through(self):
        with as_user(ADMIN):
            e = engage.create_event("Coffee morning", "2026-10-30", "celebration", budget=2000)
            self.assertEqual(e["budget_status"], "within_limit")
            self.assertEqual(engage.announce(e["event_id"])["status"], "announced")

    def test_spend_cannot_run_away_from_the_budget(self):
        with as_user(ADMIN):
            e = engage.create_event("Team lunch", "2026-10-28", budget=10000)
            engage.update_event(e["event_id"], spent=10500)                  # within the 10% slack
            with self.assertRaises(engage.EngageError) as cm:
                engage.update_event(e["event_id"], spent=14000)
            self.assertIn("budget", str(cm.exception))

    def test_rsvp_counts_and_can_be_changed(self):
        with as_user(ADMIN):
            e = engage.create_event("Town hall", "2026-11-05", "town_hall")
            engage.rsvp(e["event_id"], "yes", A, guests=1)
            engage.rsvp(e["event_id"], "no", B)
            engage.rsvp(e["event_id"], "maybe", A)                            # changes their mind
            a = engage.attendance(e["event_id"])
        self.assertEqual((a["yes"], a["maybe"], a["no"], a["responded"]), (0, 1, 1, 2))
        with as_user(ADMIN):
            self.assertEqual(len(engage.event_details(e["event_id"])["rsvps"]), 2)

    def test_calendar_has_events_holidays_and_occasions(self):
        with as_user(ADMIN):
            engage.create_event("Diwali party", "2026-10-25", "festival", budget=1000)
            rows = engage.calendar(60)
        kinds = {r["type"] for r in rows}
        self.assertIn("event", kinds)
        self.assertIn("occasion", kinds)
        self.assertTrue(any(r["title"].startswith("Meera Culture's birthday") for r in rows))
        self.assertEqual(rows, sorted(rows, key=lambda r: r["day"]))

    def test_occasions_cover_work_anniversaries(self):
        titles = [o["title"] for o in engage.occasions(10)]
        self.assertTrue(any("work anniversary" in t for t in titles))


class RecognitionTests(unittest.TestCase):
    def test_kudos(self):
        with as_user(auth.User(7, "meera", "employee", A)):
            out = T.run("give_kudos", to="Arjun Culture", message="Took the on-call weekend for me")
            self.assertEqual(out["to_id"], B)
            self.assertIn("error", T.run("give_kudos", to=A, message="I am great"))   # not to yourself
        wall = engage.kudos_wall()
        self.assertTrue(any(k["to_name"] == "Arjun Culture" for k in wall["kudos"]))
        self.assertEqual(wall["top"][0]["count"] >= 1, True)

    def test_awards_need_hr_to_decide(self):
        with as_user(auth.User(7, "meera", "employee", A)):
            nom = T.run("nominate_for_award", award="Star of the month", employee=B, reason="Unblocked the release")
            self.assertEqual(nom["status"] if "status" in nom else "nominated", nom.get("status", "nominated"))
            self.assertIn("error", T.run("decide_award", nomination_id=nom["nomination_id"], status="awarded"))
        with as_user(auth.User(6, "hr3", "hr")):
            self.assertEqual(T.run("decide_award", nomination_id=nom["nomination_id"], status="awarded")["status"],
                             "awarded")
        self.assertTrue(db.q1("SELECT COUNT(*) AS n FROM outbox WHERE body LIKE '%Star of the month%'")["n"])


class PulseTests(unittest.TestCase):
    def test_results_stay_hidden_until_enough_people_answer(self):
        with as_user(ADMIN):
            s = engage.start_survey("October pulse", "How is this month going?", closes="2026-10-31")
            engage.answer_survey(s["survey_id"], 4, "Busy but fine")
            with self.assertRaises(engage.EngageError):
                engage.answer_survey(s["survey_id"], 5)               # one answer each
            self.assertIn("note", engage.survey_results(s["survey_id"]))
        for user in (auth.User(6, "hr3", "hr"), auth.User(7, "meera", "employee", A)):
            with as_user(user):
                engage.answer_survey(s["survey_id"], 3)
        res = engage.survey_results(s["survey_id"])
        self.assertEqual((res["answers"], res["average"]), (3, round((4 + 3 + 3) / 3, 2)))
        self.assertEqual(sum(d["count"] for d in res["distribution"]), 3)
        self.assertNotIn("respondent", json.dumps(res))                # nothing points back to a person
        with as_user(ADMIN):
            self.assertEqual(engage.close_survey(s["survey_id"])["status"], "closed")
            with self.assertRaises(engage.EngageError):
                engage.answer_survey(s["survey_id"], 2)

    def test_score_is_checked(self):
        with as_user(ADMIN):
            s = engage.start_survey("Scale check", "Pick a number")
            with self.assertRaises(engage.EngageError):
                engage.answer_survey(s["survey_id"], 9)


class CultureAgentTests(unittest.TestCase):
    # its own gateway user, so these requests do not eat the shared admin's rate limit
    ASKER = auth.User(0, "culture-tester", "admin")

    def test_routing_and_rules_answers(self):
        out = G.handle("What is coming up this month?", user=self.ASKER)
        self.assertEqual(out["agent"], "culture")
        out = G.handle("Kudos to Arjun Culture for fixing the payroll migration", user=self.ASKER)
        self.assertEqual(out["agent"], "culture")
        self.assertIn("Arjun", out["result"])
        out = G.handle("Plan a Christmas lunch on 2026-12-24 with a budget of 40000", user=self.ASKER)
        self.assertIn("approval", out["result"].lower())
        self.assertEqual(G.handle("Whose birthday is coming up?", user=self.ASKER)["agent"], "culture")
        # the other agents keep their work
        self.assertEqual(G.handle("How many casual leave days do we get?", user=self.ASKER)["agent"], "policy")
        self.assertEqual(G.handle("What is slipping?", user=self.ASKER)["agent"], "projects")

    def test_employees_answer_only_for_themselves(self):
        with as_user(ADMIN):
            e = engage.create_event("Volunteering day", "2026-11-12", "volunteering")
        with as_user(auth.User(7, "meera", "employee", A)):
            self.assertEqual(T.run("rsvp_event", event=e["event_id"], answer="yes")["employee_id"], A)
            self.assertIn("error", T.run("rsvp_event", event=e["event_id"], answer="no", employee=B))
            self.assertIn("error", T.run("create_event", title="Party", day="2026-12-01"))
            self.assertIn("error", T.run("start_pulse_survey", title="Mine", question="?"))

    def test_triggers(self):
        self.assertEqual(triggers.fire("culture_calendar")["status"], "ok")
        self.assertEqual(triggers.fire("event_wrap_up")["status"], "ok")


class CultureWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from hrai import web
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, path, token, body=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_api_and_roles(self):
        hr = auth.login("hr3", "hr-password-3")
        code, data = self.call("/api/culture", hr)
        self.assertEqual(code, 200)
        self.assertIn("calendar", data)
        self.assertIn("kudos", data["kudos"])
        self.assertIn("engagement", data)
        code, out = self.call("/api/culture/events", hr, {"title": "Web made event", "day": "2026-11-18",
                                                          "kind": "offsite", "budget": 1000})
        self.assertEqual(code, 200)
        eid = out["event_id"]
        self.assertEqual(self.call("/api/culture/events/announce", hr, {"event": eid})[0], 200)
        code, detail = self.call(f"/api/culture/events/{eid}", hr)
        self.assertEqual((code, detail["event"]["status"]), (200, "announced"))
        employee = auth.login("meera", "meera-password-1")
        self.assertEqual(self.call("/api/culture/rsvp", employee, {"event": eid, "answer": "yes"})[0], 200)
        self.assertEqual(self.call("/api/culture/kudos", employee, {"to": B, "message": "Great demo"})[0], 200)
        code, out = self.call("/api/culture/events", employee, {"title": "Not allowed", "day": "2026-11-19"})
        self.assertEqual((code, "error" in out), (403, True))
        self.assertEqual(self.call("/api/culture", employee)[0], 200)   # everyone can read the calendar


if __name__ == "__main__":
    unittest.main()

import base64
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import server


def record_set():
    state=server.seed()
    patient={"id":"PT-TEST0001","name":"Test Patient","age":35,"gender":"Female","village":"Test Village","created_at":server.now()}
    other={"id":"PT-TEST0002","name":"Other Patient","age":29,"gender":"Male","village":"Other Village","created_at":server.now()}
    state["patients"]=[patient,other]
    fields=server.fields_for()
    fields["chief_complaint"]={"value":"Cough since yesterday","source":"Cough since yesterday","review":"accepted","origin":"literal_match"}
    e={"id":"EN-TEST0001","patient_id":patient["id"],"date":"2026-10-31","type":"Community health visit","language":"English","transcript":"Cough since yesterday. Transcript marker exact.","transcription_mode":"Manual entry","status":"approved","fields":fields,"created_at":server.now(),"updated_at":server.now(),"approved_at":server.now(),"audit":[],"revisions":[]}
    unrelated=dict(e,id="EN-TEST0002",patient_id=other["id"],transcript="Other patient private phrase.")
    state["encounters"]=[e,unrelated]
    return state,patient,other,e,unrelated


class LocalRecordTests(unittest.TestCase):
    def test_remote_bind_is_refused_without_authentication(self):
        with mock.patch.dict("os.environ", {"ASHA_HOST":"0.0.0.0"}):
            with self.assertRaisesRegex(SystemExit,"no authentication"):
                server.configured_host()

    def test_static_file_allowlist_and_security_headers(self):
        http=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base="http://127.0.0.1:"+str(http.server_address[1])
        try:
            with urllib.request.urlopen(base+"/") as response:
                self.assertEqual(response.status,200)
                self.assertEqual(response.headers.get("X-Frame-Options"),"DENY")
                self.assertIn("default-src 'self'",response.headers.get("Content-Security-Policy",""))
                self.assertEqual(response.headers.get("Referrer-Policy"),"no-referrer")
            for private_path in ("/data/store.json","/server.py","/.env.example","/tests/test_server.py"):
                with self.subTest(path=private_path), self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base+private_path)
                self.assertEqual(error.exception.code,404)
                error.exception.close()
            request=urllib.request.Request(base+"/",headers={"Host":"attacker.example"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,421)
            error.exception.close()
        finally:
            http.shutdown();http.server_close();thread.join(timeout=2)

    def test_startup_has_no_preloaded_records(self):
        self.assertEqual(server.seed(),{"patients":[],"encounters":[],"followups":[]})

    def test_phrase_matching_is_literal_and_never_fills_unmentioned_fields(self):
        values=server.extract("Cough since yesterday.")
        self.assertIn("symptoms",values)
        self.assertIn("duration",values)
        self.assertEqual(values["symptoms"]["source"],"Cough since yesterday.")
        self.assertNotIn("allergies",values)
        self.assertNotIn("observations",values)

    def test_report_preserves_empty_values_and_correct_brand(self):
        state,p,_,e,_=record_set()
        content=server.report_html(e,p,[],["patient","clinical"],"comprehensive",[e])
        self.assertIn("ASHA SAHAYAK",content)
        self.assertIn("Not documented",content)
        self.assertIn("Reported allergies",content)
        self.assertIn('class="report-mark"',content)
        encoded_logo=base64.b64encode((server.ROOT/"brand-logo.png").read_bytes()).decode("ascii")
        self.assertIn("data:image/png;base64,"+encoded_logo,content)

    def test_hindi_and_hinglish_report_headings_are_available_without_translating_values(self):
        _,p,_,e,_=record_set()
        hindi=server.report_html(e,p,[],["patient","summary"],"summary",[e],"hi")
        hinglish=server.report_html(e,p,[],["patient","summary"],"summary",[e],"hinglish")
        self.assertIn('<html lang="hi">',hindi)
        self.assertIn('<html lang="en">',hinglish)
        self.assertIn(server.REPORT_TRANSLATIONS["hi"]["patientDetails"],hindi)
        self.assertIn("Patient",hinglish)
        self.assertIn("Cough since yesterday",hindi)
        self.assertNotIn("Other Patient",hindi)

    def test_excluded_transcript_is_not_rendered(self):
        _,p,_,e,_=record_set()
        content=server.report_html(e,p,[],["patient","summary"],"summary",[e])
        self.assertNotIn(e["transcript"],content)
        self.assertIn("excluded by selection",content)

    def test_other_patient_history_is_never_included(self):
        _,p,_,e,unrelated=record_set()
        content=server.report_html(e,p,[],["patient","summary","history"],"history",[e,unrelated])
        self.assertNotIn(unrelated["id"],content)
        self.assertNotIn("Other patient private phrase",content)

    def test_http_flow_starts_empty_then_persists_user_created_records(self):
        original_store=server.STORE
        with tempfile.TemporaryDirectory() as tmp:
            server.STORE=Path(tmp)/"store.json"
            server.save(server.seed())
            http=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base="http://127.0.0.1:"+str(http.server_address[1])
            def post(path,payload):
                request=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"},method="POST")
                try:
                    with urllib.request.urlopen(request) as response:return response.status,json.loads(response.read())
                except urllib.error.HTTPError as error:
                    status=error.code;body=json.loads(error.read());error.close();return status,body
            try:
                self.assertEqual(json.loads(urllib.request.urlopen(base+"/api/bootstrap").read())["state"]["patients"],[])
                status,result=post("/api/patients",[])
                self.assertEqual(status,400)
                status,result=post("/api/patients",{"name":"Invalid age","age":"121","gender":"Female","village":"Ward 2"})
                self.assertEqual(status,400)
                status,result=post("/api/patients",{"name":"A real user entry","age":"0","gender":"Female","village":"Ward 2"})
                self.assertEqual(status,200);pid=result["patient"]["id"]
                draft={"patient_id":pid,"date":"2026-10-31","type":"Home visit","language":"Hindi","transcript":"\u092c\u0941\u0916\u093e\u0930 \u0915\u0932 \u0938\u0947","fields":server.fields_for()}
                status,result=post("/api/encounters",draft);self.assertEqual(status,200);eid=result["encounter"]["id"]
                self.assertEqual(server.load()["encounters"][0]["transcript"],"\u092c\u0941\u0916\u093e\u0930 \u0915\u0932 \u0938\u0947")
                status,result=post("/api/encounters/"+eid+"/approve",{});self.assertEqual(status,409)
                draft["id"]=eid;draft["fields"]={k:{"value":"","source":"","review":"unknown"} for k,_ in server.FIELDS}
                status,result=post("/api/encounters",draft);self.assertEqual(status,200)
                status,result=post("/api/encounters/"+eid+"/approve",{});self.assertEqual(status,200)
                self.assertEqual(result["encounter"]["status"],"approved")
                status,_=post("/api/patients",{"name":"A real user entry","age":"0","gender":"Female","village":"Ward 2"});self.assertEqual(status,409)
            finally:
                http.shutdown();http.server_close();thread.join(timeout=2);server.STORE=original_store

    def test_followup_validation_and_persistence(self):
        original_store=server.STORE
        with tempfile.TemporaryDirectory() as tmp:
            state=server.seed();state["patients"]=[{"id":"PT-TEST","name":"Entry","age":None,"gender":"Not documented","village":"","created_at":server.now()}]
            server.STORE=Path(tmp)/"store.json";server.save(state)
            http=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start();base="http://127.0.0.1:"+str(http.server_address[1])
            def post(path,payload):
                request=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"},method="POST")
                try:
                    with urllib.request.urlopen(request) as response:return response.status,json.loads(response.read())
                except urllib.error.HTTPError as error:
                    status=error.code;body=json.loads(error.read());error.close();return status,body
            try:
                status,result=post("/api/followups",{"patient_id":"PT-TEST","purpose":"Check symptoms again","due_date":"2030-01-15"})
                self.assertEqual(status,200);fid=result["followup"]["id"]
                status,_=post("/api/followups",{"patient_id":"PT-TEST","encounter_id":"EN-OTHER-PATIENT","purpose":"Check symptoms again","due_date":"2030-01-15"})
                self.assertEqual(status,400)
                status,result=post("/api/followups/"+fid+"/update",{"purpose":"Recheck symptoms and hydration","due_date":"2030-01-16"})
                self.assertEqual(status,200);self.assertEqual(result["followup"]["due_date"],"2030-01-16")
                status,_=post("/api/followups/"+fid+"/update",{"purpose":"No","due_date":"invalid"});self.assertEqual(status,400)
            finally:
                http.shutdown();http.server_close();thread.join(timeout=2);server.STORE=original_store

    def test_pdf_is_valid_and_uses_selected_identifier(self):
        if not server.chrome_path():self.skipTest("Chrome or Edge not installed")
        _,p,_,e,_=record_set()
        name,data=server.make_pdf(e,p,[],["patient","summary","clinical","workflow","completeness"],"summary",[e],"en")
        self.assertTrue(data.startswith(b"%PDF-"));self.assertGreater(len(data),500)
        self.assertTrue(name.startswith("ASHA_SAHAYAK_EMR_"+p["id"]+"_"))
        target=server.REPORTS/name
        if target.exists():target.unlink()

    def test_export_endpoint_builds_a_pdf_for_the_requested_saved_encounter(self):
        if not server.chrome_path():self.skipTest("Chrome or Edge not installed")
        original_store=server.STORE
        state,p,_,e,_=record_set()
        with tempfile.TemporaryDirectory() as tmp:
            server.STORE=Path(tmp)/"store.json";server.save(state)
            http=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base="http://127.0.0.1:"+str(http.server_address[1])
            request=urllib.request.Request(base+"/api/export",data=json.dumps({"encounter_id":e["id"],"report_type":"summary","report_language":"en","sections":["patient","summary"]}).encode(),headers={"Content-Type":"application/json"},method="POST")
            name=None
            try:
                with urllib.request.urlopen(request) as response:
                    data=response.read();name=response.headers.get_filename()
                    self.assertEqual(response.status,200);self.assertEqual(response.headers.get_content_type(),"application/pdf")
                self.assertTrue(data.startswith(b"%PDF-"))
                self.assertIn(p["id"],name);self.assertIn(e["id"],name)
            finally:
                http.shutdown();http.server_close();thread.join(timeout=2);server.STORE=original_store
                if name and (server.REPORTS/name).exists():(server.REPORTS/name).unlink()

    def test_long_transcript_paginates_and_hindi_glyphs_are_in_report_html(self):
        if not server.chrome_path():self.skipTest("Chrome or Edge not installed")
        _,p,_,e,_=record_set();e=dict(e);e["transcript"]=(e["transcript"]+" ")*200
        name,data=server.make_pdf(e,p,[],["patient","summary","transcript"],"comprehensive",[e],"hi")
        self.assertTrue(data.startswith(b"%PDF-"))
        pages=len(re.findall(rb"/Type\s*/Page\b",data));self.assertGreater(pages,1)
        html=server.report_html(e,p,[],["patient","summary"],"summary",[e],"hi")
        self.assertIn(server.REPORT_TRANSLATIONS["hi"]["patientDetails"],html)
        target=server.REPORTS/name
        if target.exists():target.unlink()

if __name__=="__main__":unittest.main()

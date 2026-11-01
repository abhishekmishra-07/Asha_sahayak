from __future__ import annotations
import base64, html, json, mimetypes, os, re, sqlite3, subprocess, tempfile, threading, urllib.error, urllib.request, uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT=Path(__file__).resolve().parent
def load_env_file():
    path=ROOT/".env"
    if not path.is_file():return
    for line in path.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:continue
        key,value=line.split("=",1);key=key.strip();value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ("'",'"'):value=value[1:-1]
        if key and not os.environ.get(key):os.environ[key]=value
load_env_file()
DATA=Path(os.environ.get("ASHA_DATA_DIR",str(ROOT/"data"))).expanduser().resolve()
REPORTS=DATA/"reports"; STORE=DATA/"asha_sahayak.sqlite3"; LEGACY_STORE=DATA/"store.json"
DEFAULT_STORE=STORE
DATABASE_URL=os.environ.get("DATABASE_URL","").strip()
GEMINI_API_KEY=os.environ.get("GEMINI_API_KEY","").strip()
GEMINI_UPLOAD_LIMIT=20*1024*1024
DATA.mkdir(exist_ok=True); REPORTS.mkdir(exist_ok=True)
LOCK=threading.RLock()
IMPORTING_LEGACY=False
PUBLIC_FILES={"index.html","app.js","styles.css","brand-logo.png","favicon.png","icons.svg"}
def restrict_permissions(path,mode):
    if os.name!="nt":path.chmod(mode)
if os.name!="nt":
    restrict_permissions(DATA,0o700);restrict_permissions(REPORTS,0o700)
FIELDS=[("chief_complaint","Chief complaint"),("symptoms","Symptoms"),("duration","Onset and duration"),("history","Relevant history"),("medications","Reported medications"),("allergies","Reported allergies"),("observations","Worker observations"),("follow_up","Follow-up needs"),("referral","Referral information")]
def now(): return datetime.now().astimezone().isoformat(timespec="seconds")
def configured_host():
    host=os.environ.get("ASHA_HOST","127.0.0.1")
    if host=="localhost":host="127.0.0.1"
    if host!="127.0.0.1":raise SystemExit("Refusing to bind outside loopback: this build has no authentication or user access controls. Keep ASHA_HOST at 127.0.0.1.")
    return host
def new_id(prefix): return prefix+"-"+uuid.uuid4().hex[:8].upper()
def fields_for():
    return {k:{"value":"","source":"","review":"unreviewed","origin":"worker"} for k,_ in FIELDS}
def seed():
    return {"patients":[],"encounters":[],"followups":[]}
def using_local_store():
    # An unset DATABASE_URL selects the existing on-disk SQLite store.
    # If a PostgreSQL URL is configured but unreachable, keep that error visible
    # rather than silently switching writes to a different database.
    return not DATABASE_URL or STORE!=DEFAULT_STORE
def database():
    if using_local_store():
        STORE.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(STORE,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        return db
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured. Set DATABASE_URL before starting the application.")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as ex:
        raise RuntimeError("PostgreSQL support is missing. Install dependencies with python -m pip install -r requirements.txt.") from ex
    try:
        return psycopg.connect(DATABASE_URL,connect_timeout=10,row_factory=dict_row)
    except psycopg.Error as ex:
        raise RuntimeError("Could not connect to PostgreSQL. Check DATABASE_URL, network access, and the database TLS settings.") from ex
def validate_state(s):
    if not isinstance(s,dict) or any(not isinstance(s.get(k),list) for k in ("patients","encounters","followups")):
        raise RuntimeError("The existing record file has an unsupported format; it has not been changed.")
    for key in ("patients","encounters","followups"):
        if any(not isinstance(item,dict) or not item.get("id") for item in s[key]):
            raise RuntimeError("The existing record file has invalid entries; it has not been changed.")
        ids=[str(item["id"]) for item in s[key]]
        if len(ids)!=len(set(ids)):
            raise RuntimeError("The existing record file contains duplicate identifiers; it has not been changed.")
    patient_ids={str(p["id"]) for p in s["patients"]}
    if any(str(e.get("patient_id","")) not in patient_ids for e in s["encounters"]) or any(str(f.get("patient_id","")) not in patient_ids for f in s["followups"]):
        raise RuntimeError("A saved record refers to a missing patient; the existing record file has not been changed.")
def sqlite_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS patients (
            id TEXT PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL,
            village TEXT NOT NULL DEFAULT '', age INTEGER, gender TEXT NOT NULL,
            created_at TEXT, updated_at TEXT, record_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS encounters (
            id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id),
            position INTEGER NOT NULL, encounter_date TEXT NOT NULL, status TEXT NOT NULL,
            record_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS encounters_by_patient_date ON encounters(patient_id, encounter_date);
        CREATE TABLE IF NOT EXISTS followups (
            id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id),
            encounter_id TEXT, position INTEGER NOT NULL, due_date TEXT NOT NULL,
            status TEXT NOT NULL, record_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS followups_by_due_status ON followups(due_date, status);
    """)
def local_import_state():
    if os.environ.get("ASHA_IMPORT_SQLITE")=="1":
        if not STORE.is_file(): raise RuntimeError("ASHA_IMPORT_SQLITE is set, but data/asha_sahayak.sqlite3 was not found.")
        try:
            source=sqlite3.connect(STORE)
            try:
                state={table:[json.loads(row[0]) for row in source.execute("SELECT record_json FROM "+table+" ORDER BY position")]
                        for table in ("patients","encounters","followups")}
            finally: source.close()
            validate_state(state);return state,"sqlite"
        except (sqlite3.Error,json.JSONDecodeError): raise RuntimeError("The local SQLite database could not be read for migration; it has not been changed.")
    if os.environ.get("ASHA_IMPORT_JSON")=="1":
        if not LEGACY_STORE.is_file(): raise RuntimeError("ASHA_IMPORT_JSON is set, but data/store.json was not found.")
        try: state=json.loads(LEGACY_STORE.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError): raise RuntimeError("The existing JSON store is unreadable; it has not been changed.")
        validate_state(state);return state,"json"
    return None,None
def import_legacy_if_requested():
    global IMPORTING_LEGACY
    if IMPORTING_LEGACY or not (os.environ.get("ASHA_IMPORT_SQLITE")=="1" or os.environ.get("ASHA_IMPORT_JSON")=="1"): return
    db=database()
    try:
        marker=db.execute("SELECT value FROM app_meta WHERE key=%s",("legacy_import_v1",)).fetchone()
        counts=[db.execute("SELECT COUNT(*) FROM "+table).fetchone()["count"] for table in ("patients","encounters","followups")]
    finally: db.close()
    if marker:return
    if any(counts): raise RuntimeError("Legacy import requires an empty PostgreSQL database. Existing server records were left unchanged.")
    state,source=local_import_state()
    if state is None:return
    IMPORTING_LEGACY=True
    try:
        save(state)
        db=database()
        try:
            db.execute("INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING",("legacy_import_v1",source))
            db.commit()
        finally: db.close()
    finally: IMPORTING_LEGACY=False
def initialize_database():
    if using_local_store():
        db=database()
        try: sqlite_schema(db);db.commit()
        finally: db.close()
        restrict_permissions(STORE,0o600)
        return
    db=database()
    try:
        statements=(
            "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY, position BIGINT NOT NULL, name TEXT NOT NULL, village TEXT NOT NULL DEFAULT '', age INTEGER, gender TEXT NOT NULL, created_at TEXT, updated_at TEXT, record_json JSONB NOT NULL)",
            "CREATE TABLE IF NOT EXISTS encounters (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id), position BIGINT NOT NULL, encounter_date TEXT NOT NULL, status TEXT NOT NULL, record_json JSONB NOT NULL)",
            "CREATE INDEX IF NOT EXISTS encounters_by_patient_date ON encounters(patient_id, encounter_date)",
            "CREATE TABLE IF NOT EXISTS followups (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id), encounter_id TEXT, position BIGINT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL, record_json JSONB NOT NULL)",
            "CREATE INDEX IF NOT EXISTS followups_by_due_status ON followups(due_date, status)"
        )
        for statement in statements: db.execute(statement)
        db.commit()
    finally: db.close()
    import_legacy_if_requested()
def load():
    with LOCK:
        initialize_database()
        db=database()
        try:
            state={}
            for table in ("patients","encounters","followups"):
                rows=db.execute("SELECT record_json FROM "+table+" ORDER BY position")
                state[table]=[]
                for row in rows:
                    value=row["record_json"]
                    state[table].append(json.loads(value) if isinstance(value,str) else value)
            return state
        except (sqlite3.Error,json.JSONDecodeError): raise RuntimeError("The database contains unreadable records; preserve a backup before recovery.")
        finally: db.close()
def stored_record(record):
    if using_local_store(): return json.dumps(record,ensure_ascii=False,separators=(",",":"))
    from psycopg.types.json import Jsonb
    return Jsonb(record)
def save(s):
    with LOCK:
        validate_state(s)
        initialize_database()
        db=database()
        try:
            with db:
                db.execute("DELETE FROM followups");db.execute("DELETE FROM encounters");db.execute("DELETE FROM patients")
                mark="?" if using_local_store() else "%s"
                q=lambda n:"("+",".join([mark]*n)+")"
                db.executemany("INSERT INTO patients(id,position,name,village,age,gender,created_at,updated_at,record_json) VALUES"+q(9),[
                    (str(p["id"]),i,str(p.get("name", "")),str(p.get("village", "")),p.get("age"),str(p.get("gender", "Not documented")),p.get("created_at"),p.get("updated_at"),stored_record(p))
                    for i,p in enumerate(s["patients"])])
                db.executemany("INSERT INTO encounters(id,patient_id,position,encounter_date,status,record_json) VALUES"+q(6),[
                    (str(e["id"]),str(e["patient_id"]),i,str(e.get("date", "")),str(e.get("status", "draft")),stored_record(e))
                    for i,e in enumerate(s["encounters"])])
                db.executemany("INSERT INTO followups(id,patient_id,encounter_id,position,due_date,status,record_json) VALUES"+q(7),[
                    (str(f["id"]),str(f["patient_id"]),f.get("encounter_id") or None,i,str(f.get("due_date", "")),str(f.get("status", "pending")),stored_record(f))
                    for i,f in enumerate(s["followups"])])
        finally: db.close()
        if using_local_store(): restrict_permissions(STORE,0o600)
def calc(s):
    d=date.today().isoformat()
    return {"today":sum(e["date"]==d for e in s["encounters"]),"drafts":sum(e["status"]=="draft" for e in s["encounters"]),"review":sum(e["status"]=="review" for e in s["encounters"]),"due":sum(f["status"]=="pending" and f["due_date"]<=d for f in s["followups"]),"overdue":sum(f["status"]=="pending" and f["due_date"]<d for f in s["followups"])}
def extract(text):
    source=text.strip()[:20000]
    low=source.casefold()
    rules=[
        ("chief_complaint",["fever","bukhar","\u092c\u0941\u0916\u093e\u0930","cough","khansi","\u0916\u093e\u0902\u0938\u0940","pain","dard","\u0926\u0930\u094d\u0926","weakness","kamzori","\u0915\u092e\u091c\u094b\u0930\u0940"],"Symptom phrase found; verify wording"),
        ("symptoms",["fever","bukhar","\u092c\u0941\u0916\u093e\u0930","cough","khansi","\u0916\u093e\u0902\u0938\u0940","headache","sir dard","\u0938\u0930 \u0926\u0930\u094d\u0926","pain","dard","\u0926\u0930\u094d\u0926","vomit","ulti","\u0909\u0932\u094d\u091f\u0940","loose motion","diarrhea","dast","\u0926\u0938\u094d\u0924","breathless","saans","\u0938\u093e\u0902\u0938","weakness","kamzori","\u0915\u092e\u091c\u094b\u0930\u0940"],"Symptom phrase found; verify wording"),
        ("duration",["since yesterday","since last night","for ","days","day","weeks","week","months","month","kal se","\u0915\u0932 \u0938\u0947","\u0926\u093f\u0928 \u0938\u0947","hafta","\u0939\u092b\u093c\u094d\u0924\u0947","mahine","\u092e\u0939\u0940\u0928\u0947"],"Duration phrase found; verify exact timing"),
        ("history",["history of","past history","medical history","diabetes","diabetic","sugar","hypertension","high bp","asthma","tb","pregnant","pregnancy","itihas","\u0907\u0924\u093f\u0939\u093e\u0938","madhumeh","\u092e\u0927\u0941\u092e\u0947\u0939","dama","\u0926\u092e\u093e","garbhvati","\u0917\u0930\u094d\u092d\u0935\u0924\u0940"],"History phrase found; confirm with the patient"),
        ("medications",["medicine","medication","tablet","taking","prescribed","dawai","dava","\u0926\u0935\u093e\u0908","\u0926\u0935\u093e","goli","\u0917\u094b\u0932\u0940"],"Medication phrase found; verify name and use"),
        ("allergies",["allergy","allergic","allergies","\u090f\u0932\u0930\u094d\u091c\u0940","reaction to"],"Allergy phrase found; verify details"),
        ("observations",["blood pressure","bp","pulse","temperature","spo2","oxygen level","weight","temperature","\u092c\u0940\u092a\u0940","\u0928\u092c\u094d\u091c","\u0924\u093e\u092a\u092e\u093e\u0928","\u0913\u0915\u094d\u0938\u0940\u091c\u0928","\u0935\u091c\u0928"],"Observation phrase found; check the measured value and unit"),
        ("follow_up",["follow up","follow-up","review after","come back","return in","next visit","dobara aana","phir se aana","\u092b\u093f\u0930 \u0938\u0947 \u0906\u0928\u093e","\u0926\u0941\u092c\u093e\u0930\u093e \u0906\u0928\u093e"],"Follow-up phrase found; verify the plan and date"),
        ("referral",["refer","referral","hospital","clinic","health centre","health center","phc","chc","doctor","aspatal","\u0905\u0938\u094d\u092a\u0924\u093e\u0932","\u0905\u0938\u094d\u092a\u0924\u093e\u0932","\u0915\u094d\u0932\u093f\u0928\u093f\u0915","\u0921\u0949\u0915\u094d\u091f\u0930"],"Referral phrase found; confirm destination and urgency")
    ]
    out={}
    for key,terms,note in rules:
        for term in terms:
            folded=term.casefold()
            if re.fullmatch(r"[a-z0-9]+",folded):
                match=re.search(r"(?<![a-z0-9])"+re.escape(folded)+r"(?![a-z0-9])",low)
                pos=match.start() if match else -1
            else:pos=low.find(folded)
            if pos>=0:
                start=max(0,pos-40);end=min(len(source),pos+len(term)+70)
                excerpt=source[start:end].strip()
                out[key]={"value":excerpt,"source":excerpt,"review":"unreviewed","origin":"literal_match","note":note}
                break
    return out

class GeminiServiceError(RuntimeError):pass

def _gemini_request(url,body=None,headers=None,method="POST",timeout=180):
    request=urllib.request.Request(url,data=body,headers=headers or {},method=method)
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:return response.read(),response.headers
    except urllib.error.HTTPError as ex:
        raw=ex.read(4096)
        message=""
        try:
            detail=json.loads(raw.decode("utf-8"));message=str(detail.get("error",{}).get("message", ""))
        except (UnicodeDecodeError,json.JSONDecodeError,AttributeError):pass
        message=re.sub(r"[\r\n]+"," ",message).strip()[:240]
        raise GeminiServiceError("Gemini API request failed"+(" ("+str(ex.code)+")" if ex.code else "")+(": "+message if message else ". Check the API key and model access.")) from None
    except (urllib.error.URLError,TimeoutError,OSError):
        raise GeminiServiceError("Could not reach the Gemini API. Check internet access and try again.") from None

def _gemini_json(url,payload,timeout=180):
    data,_=_gemini_request(url,json.dumps(payload,ensure_ascii=False).encode("utf-8"),{"Content-Type":"application/json","x-goog-api-key":GEMINI_API_KEY})
    try:return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError):raise GeminiServiceError("Gemini returned an unreadable response. Try again.") from None

def _gemini_delete_file(name):
    if not re.fullmatch(r"files/[A-Za-z0-9-]+",str(name or "")):return
    try:_gemini_request("https://generativelanguage.googleapis.com/v1beta/"+name,headers={"x-goog-api-key":GEMINI_API_KEY},method="DELETE",timeout=30)
    except GeminiServiceError:pass

def _gemini_audio_upload(audio,mime):
    metadata=json.dumps({"file":{"display_name":"asha-sahayak-audio"}}).encode("utf-8")
    start_headers={"Content-Type":"application/json","x-goog-api-key":GEMINI_API_KEY,"X-Goog-Upload-Protocol":"resumable","X-Goog-Upload-Command":"start","X-Goog-Upload-Header-Content-Length":str(len(audio)),"X-Goog-Upload-Header-Content-Type":mime}
    _,headers=_gemini_request("https://generativelanguage.googleapis.com/upload/v1beta/files",metadata,start_headers,timeout=60)
    upload_url=headers.get("x-goog-upload-url")
    if not upload_url:raise GeminiServiceError("Gemini did not provide an audio upload URL. Try again.")
    raw,_=_gemini_request(upload_url,audio,{"Content-Type":mime,"X-Goog-Upload-Offset":"0","X-Goog-Upload-Command":"upload, finalize"},timeout=180)
    try:resource=json.loads(raw.decode("utf-8"));file=resource.get("file",resource)
    except (UnicodeDecodeError,json.JSONDecodeError):raise GeminiServiceError("Gemini could not accept this audio file. Try WAV, MP3, M4A or WebM.") from None
    uri=file.get("uri");name=file.get("name")
    if not uri or not name:raise GeminiServiceError("Gemini did not confirm the audio upload. Try again.")
    return uri,name

def gemini_transcribe_and_extract(audio,mime):
    if not GEMINI_API_KEY:raise GeminiServiceError("Gemini is not configured. Add GEMINI_API_KEY to .env and restart the app.")
    if not audio:raise ValueError("The audio file is empty.")
    if len(audio)>GEMINI_UPLOAD_LIMIT:raise ValueError("Audio is limited to 20 MB.")
    mime=(mime or "").split(";",1)[0].strip().lower()
    mime_aliases={"audio/x-wav":"audio/wav","audio/wave":"audio/wav","audio/x-m4a":"audio/m4a","audio/mp4":"audio/m4a","audio/x-flac":"audio/flac","audio/x-aac":"audio/aac"}
    mime=mime_aliases.get(mime,mime)
    if mime not in {"audio/wav","audio/mp3","audio/mpeg","audio/aiff","audio/aac","audio/ogg","audio/flac","audio/m4a","audio/opus","audio/webm","audio/l16","audio/alaw","audio/mulaw"}:
        raise ValueError("Choose a supported audio file: WAV, MP3, M4A, AAC, OGG, FLAC or WebM.")
    uri,name=_gemini_audio_upload(audio,mime)
    keys=[key for key,_ in FIELDS]
    field_properties={key:{"type":"string"} for key in []}
    field_properties={key:{"type":"object","properties":{"value":{"type":"string"},"source":{"type":"string"}},"required":["value","source"],"additionalProperties":False} for key in keys}
    labels={
        "chief_complaint":"the main concern reported by the patient",
        "symptoms":"symptoms explicitly reported",
        "duration":"explicit onset or duration",
        "history":"explicit relevant medical history",
        "medications":"medicines the patient explicitly says they take",
        "allergies":"explicitly reported allergies or reactions",
        "observations":"measurements or worker observations explicitly stated",
        "follow_up":"an explicitly stated follow-up plan",
        "referral":"an explicitly stated referral or destination",
    }
    field_guide="\n".join("- "+key+": "+labels[key] for key in keys)
    prompt=("Transcribe the supplied community health conversation in its original spoken language(s), preserving English/Hindi code-switching. Then map only explicitly spoken details to the listed documentation fields. Do not diagnose, infer, normalize, add advice, or fill gaps. For every non-empty field, value must be copied exactly from source, and source must be an exact contiguous quote from the transcript that includes value. If the transcript does not explicitly support a field, return empty strings for both value and source.\nFields:\n"+field_guide)
    schema={"type":"object","properties":{"transcript":{"type":"string"},"fields":{"type":"object","properties":field_properties,"required":keys,"additionalProperties":False}},"required":["transcript","fields"],"additionalProperties":False}
    request={"contents":[{"parts":[{"text":prompt},{"fileData":{"mimeType":mime,"fileUri":uri}}]}],"generationConfig":{"responseMimeType":"application/json","responseJsonSchema":schema,"maxOutputTokens":8192}}
    try:
        result=_gemini_json("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",request)
        text="".join(part.get("text","") for candidate in result.get("candidates",[]) for part in candidate.get("content",{}).get("parts",[]) if isinstance(part,dict))
        try:parsed=json.loads(text)
        except json.JSONDecodeError:raise GeminiServiceError("Gemini could not format the transcript. Try a shorter or clearer recording.") from None
        transcript=parsed.get("transcript","")
        if not isinstance(transcript,str) or not transcript.strip():raise GeminiServiceError("Gemini did not hear speech in this recording. Check the audio or enter the transcript manually.")
        transcript=transcript.strip()[:20000]
        supplied=parsed.get("fields") if isinstance(parsed.get("fields"),dict) else {}
        fields={}
        for key in keys:
            item=supplied.get(key)
            if not isinstance(item,dict):continue
            value=item.get("value");source=item.get("source")
            if not isinstance(value,str) or not isinstance(source,str):continue
            value=value.strip();source=source.strip()
            if value and source and value in source and source in transcript:
                fields[key]={"value":value,"source":source,"review":"unreviewed","origin":"gemini_suggestion","note":"Gemini suggestion; compare with the transcript"}
        return {"transcript":transcript,"fields":fields,"mode":"Gemini transcription and source-checked suggestions; review required"}
    finally:
        _gemini_delete_file(name)

def esc(x): return html.escape(str(x if x not in (None,"") else "Not documented"),quote=True)
REPORT_TRANSLATIONS=json.loads((ROOT/"report_translations.json").read_text(encoding="utf-8"))
REPORT_PDF_LABELS={
    "hi":{"internal_id":"\u0906\u0902\u0924\u0930\u093f\u0915 \u092a\u0939\u091a\u093e\u0928 \u0938\u0902\u0916\u094d\u092f\u093e","encounter_id":"\u092e\u0941\u0932\u093e\u0915\u093c\u093e\u0924 ID","recorded_value":"\u0926\u0930\u094d\u091c \u0935\u093f\u0935\u0930\u0923","unreviewed":"\u091c\u093e\u0902\u091a \u0928\u0939\u0940\u0902 \u0939\u0941\u0908","accepted":"\u0938\u094d\u0935\u0940\u0915\u093e\u0930 \u0915\u093f\u092f\u093e","edited":"\u0938\u0902\u0936\u094b\u0927\u093f\u0924","unknown":"\u0905\u091c\u094d\u091e\u093e\u0924 / \u0928\u0939\u0940\u0902 \u092c\u0924\u093e\u092f\u093e","encounter_saved":"\u092e\u0941\u0932\u093e\u0915\u093c\u093e\u0924 \u0938\u0947\u0935","transcript_available":"\u091f\u094d\u0930\u093e\u0902\u0938\u0915\u094d\u0930\u093f\u092a\u094d\u091f \u092e\u094c\u091c\u0942\u0926","worker_review":"\u0938\u094d\u0935\u093e\u0938\u094d\u0925\u094d\u092f\u0915\u0930\u094d\u092e\u0940 \u0915\u0940 \u091c\u093e\u0902\u091a","encounter_finalized":"\u092e\u0941\u0932\u093e\u0915\u093c\u093e\u0924 \u0905\u0902\u0924\u093f\u092e"},
    "hinglish":{"internal_id":"Internal ID","encounter_id":"Visit ID","recorded_value":"Recorded value","unreviewed":"Review baaki","accepted":"Accepted","edited":"Edited","unknown":"Unknown","encounter_saved":"Visit saved","transcript_available":"Transcript available","worker_review":"Worker review","encounter_finalized":"Visit finalized"}
}
REPORT_PDF_LABELS["hi"]["status"]="\u0938\u094d\u0925\u093f\u0924\u093f"
REPORT_PDF_LABELS["hinglish"]["status"]="Status"
REPORT_KEYS={
 "comprehensive":"newReport","summary":"clinicalSummary","followup":"followupTasks","audit":"sourceEvidence","history":"history",
 "patient":"patient","encounter":"encounters","internal_id":"patientDetails","name":"name","age_gender":"ageGender","village":"village","date":"date","type":"encounterType","language":"language","status":"status","source":"transcriptionMode",
 "clinical_summary":"clinicalSummary","chief_complaint":"chiefComplaint","symptoms":"symptoms","clinical_details":"clinicalFields","field":"clinicalReview","value":"patient","review":"review","observations":"observations","original_transcript":"originalTranscript","recognition_note":"browserSpeech","excluded":"originalTranscript","evidence":"sourceEvidence","workflow":"workflow","workflow_note":"statusDisclaimer","completeness":"completeness","followups":"followupTasks","due":"dueDate","purpose":"purpose","completion_note":"completed","history_title":"history","generated":"added","report_version":"reports","privacy":"localStorageNotice","not_documented":"notDocumented","support_note":"documentationAid"
}
def report_html(e,p,tasks,sections,kind,history,language="en"):
    selected=set(sections); language=language if language in ("en","hi","hinglish") else "en"
    if kind=="patient":selected.add("history")
    history=[item for item in history if item.get("patient_id")==p.get("id")]
    trans=REPORT_TRANSLATIONS.get(language,{})
    def label(key,fallback): return html.escape(str(REPORT_PDF_LABELS.get(language,{}).get(key,trans.get(key,fallback))),quote=True)
    def val(x): return html.escape(str(x) if x not in (None,"") else str(trans.get("notDocumented","Not documented")),quote=True)
    def review_status(x):
        value=str(x or "unreviewed").casefold()
        return label(value,value.title())
    def field_name(key,fallback): return html.escape(str(REPORT_TRANSLATIONS.get("field_names",{}).get(language,{}).get(key,fallback)),quote=True)
    types={"comprehensive":"newReport","summary":"clinicalSummary","followup":"followupTasks","audit":"sourceEvidence","history":"history","patient":"history"}
    title=label(types.get(kind,"newReport"),{"comprehensive":"Clinical Encounter Report","summary":"Clinical Summary","followup":"Encounter and Follow-Up Report","audit":"Transcript and Review","history":"Patient History"}.get(kind,"Clinical Encounter Report"))
    L=lambda key,fb:label(key,fb)
    rows=[]
    mark='<span class="report-mark"><img src="data:image/png;base64,'+base64.b64encode((ROOT/"brand-logo.png").read_bytes()).decode("ascii")+'" alt=""></span>'
    parts=['<header>'+mark+'<div><b>ASHA SAHAYAK</b><small>'+L("heroDesc","Your voice. Structured care. Better community health.")+'</small></div><span class="tag">'+L("documentation","CARE RECORD")+'</span></header>','<h1>'+title+'</h1><p class="muted">'+L("documentationAid","Documentation support only. Check each field with the patient.")+'</p>','<p class="warning">'+L("reviewEach","Review this documentation with the patient. It is not a diagnosis or prescription.")+'</p>']
    if "patient" in selected:
        parts.append('<section><h2>'+L("patientDetails","Patient details")+'</h2><div class="grid"><div><small>'+L("internal_id","Internal identifier")+'</small><b>'+val(p.get("id"))+'</b></div><div><small>'+L("name","Name")+'</small><b>'+val(p.get("name"))+'</b></div><div><small>'+L("ageGender","Age / gender")+'</small><b>'+val(p.get("age"))+" / "+val(p.get("gender"))+'</b></div><div><small>'+L("village","Village")+'</small><b>'+val(p.get("village"))+'</b></div></div></section>')
    parts.append('<section><h2>'+L("encounters","Encounter")+'</h2><div class="grid"><div><small>'+L("encounter_id","Encounter ID")+'</small><b>'+val(e.get("id"))+'</b></div><div><small>'+L("date","Date")+'</small><b>'+val(e.get("date"))+'</b></div><div><small>'+L("encounterType","Type")+'</small><b>'+val(e.get("type"))+'</b></div><div><small>'+L("language","Language")+'</small><b>'+val(e.get("language"))+'</b></div><div><small>'+L("status","Status")+'</small><b>'+review_status(e.get("status"))+'</b></div><div><small>'+L("transcriptionMode","Source")+'</small><b>'+val(e.get("transcription_mode"))+'</b></div></div></section>')
    fs=e.get("fields",{})
    if "summary" in selected or kind=="summary":
        parts.append('<section><h2>'+L("clinicalSummary","Clinical summary")+'</h2><p><b>'+field_name("chief_complaint","Chief complaint")+':</b> '+val(fs.get("chief_complaint",{}).get("value"))+'</p><p><b>'+field_name("symptoms","Symptoms")+':</b> '+val(fs.get("symptoms",{}).get("value"))+'</p><p class="muted">'+L("statusDisclaimer","Documentation completeness does not indicate clinical correctness.")+'</p></section>')
    if "clinical" in selected:
        for key,eng in FIELDS:
            rows.append('<tr><td>'+field_name(key,eng)+'</td><td>'+val(fs.get(key,{}).get("value"))+'</td><td>'+review_status(fs.get(key,{}).get("review"))+'</td></tr>')
        parts.append('<section><h2>'+L("clinicalFields","Clinical details")+'</h2><table><thead><tr><th>'+L("clinicalReview","Field")+'</th><th>'+L("recorded_value","Recorded value")+'</th><th>'+L("review","Review")+'</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></section>');rows=[]
    if "observations" in selected and "clinical" not in selected:parts.append('<section><h2>'+L("observations","Observations")+'</h2><p>'+val(fs.get("observations",{}).get("value"))+'</p></section>')
    if "transcript" in selected:parts.append('<section class="transcript"><h2>'+L("originalTranscript","Original transcript")+'</h2><p>'+val(e.get("transcript"))+'</p><p class="muted">'+L("browserSpeech","Recognition may contain errors. Verify against the source.")+'</p></section>')
    else:parts.append('<p class="muted">'+L("originalTranscript","Original transcript")+' - '+L("excluded","excluded by selection")+'</p>')
    if "evidence" in selected:
        for key,eng in FIELDS:rows.append('<tr><td>'+field_name(key,eng)+'</td><td>'+val(fs.get(key,{}).get("source"))+'</td><td>'+review_status(fs.get(key,{}).get("review"))+'</td></tr>')
        parts.append('<section><h2>'+L("sourceEvidence","Source excerpts and review")+'</h2><table><thead><tr><th>'+L("clinicalReview","Field")+'</th><th>'+L("sourceEvidence","Source excerpt")+'</th><th>'+L("review","Status")+'</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></section>');rows=[]
    if "workflow" in selected:
        steps=[("encounter_saved","Encounter saved",True),("transcript_available","Transcript available",bool(e.get("transcript"))),("worker_review","Worker review",all(fs.get(k,{}).get("review") in ("accepted","edited","unknown") for k,_ in FIELDS)),("encounter_finalized","Encounter finalized",e.get("status")=="approved")]
        parts.append('<section><h2>'+L("workflow","Encounter workflow")+'</h2><div class="flow">'+"".join('<div class="step '+("done" if ok else "pending")+'">'+("✓" if ok else "○")+'<small>'+L(key,name)+'</small></div>' for key,name,ok in steps)+'</div><p class="muted">'+L("statusDisclaimer","Workflow reflects the saved encounter state at report generation.")+'</p></section>')
    if "completeness" in selected:
        denominator=len(FIELDS);documented=sum(fs.get(k,{}).get("value") not in ("",None,"Not documented") for k,_ in FIELDS);reviewed=sum(fs.get(k,{}).get("review") in ("accepted","edited","unknown") for k,_ in FIELDS)
        parts.append('<section><h2>'+L("completeness","Documentation completeness")+'</h2><div class="completeness"><b>'+str(documented)+' / '+str(denominator)+'</b><span>'+L("localRecordsNote","fields documented")+'</span><i style="--progress:'+str(round(documented/denominator*100 if denominator else 0))+'%"></i></div><p class="muted">'+str(reviewed)+' / '+str(denominator)+' '+L("allFieldsReviewed","fields reviewed. Completeness is not correctness.")+'</p></section>')
    if "followups" in selected or kind=="followup":
        trs="".join('<tr><td>'+val(f.get("due_date"))+'</td><td>'+val(f.get("purpose"))+'</td><td>'+val(f.get("status"))+'</td><td>'+val(f.get("completion_note"))+'</td></tr>' for f in tasks)
        parts.append('<section><h2>'+L("followupTasks","Follow-up tasks")+'</h2>'+('<table><thead><tr><th>'+L("dueDate","Due")+'</th><th>'+L("purpose","Purpose")+'</th><th>'+L("status","Status")+'</th><th>'+L("completed","Completion note")+'</th></tr></thead><tbody>'+trs+'</tbody></table>' if trs else '<p>'+L("emptyTasks","No follow-up tasks documented.")+'</p>')+'</section>')
    if "history" in selected:
        rows="".join('<p><b>'+val(x.get("date"))+" - "+val(x.get("id"))+'</b> - '+val(x.get("fields",{}).get("chief_complaint",{}).get("value"))+'</p>' for x in history)
        parts.append('<section><h2>'+L("history","Patient encounter history")+'</h2>'+ (rows or '<p>'+L("emptyVisits","No patient history documented.")+'</p>')+'</section>')
    generated=html.escape(now(),quote=True)
    parts.append('<footer>'+L("generated","Generated")+' '+generated+' | ASHA SAHAYAK report v1 | '+L("localStorageNotice","Keep downloaded health information private.")+'</footer>')
    body="".join(parts)
    direction=' lang="hi"' if language=="hi" else ' lang="en"'
    return '<!doctype html><html'+direction+'><head><meta charset="utf-8"><style>@page{size:A4;margin:17mm 16mm 19mm;@bottom-right{content:"ASHA SAHAYAK - Page " counter(page);font:9px Arial;color:#686961}}*{box-sizing:border-box}body{font:10.5pt/1.5 "Nirmala UI","Mangal","Segoe UI",Arial,sans-serif;color:#1d201b;margin:0}header{display:flex;align-items:center;gap:10px;border-bottom:3px solid #d2ff00;padding:0 0 10px}header .report-mark{width:38px;height:38px;flex:none;display:inline-flex;align-items:center;justify-content:center;background:#d2ff00;border-radius:11px;padding:2px}header .report-mark img{width:28px;height:34px;object-fit:contain}header b{font-size:14pt;letter-spacing:.04em}header small{display:block;color:#676960;font-size:8pt}header .tag{margin-left:auto;color:#7b3ca0;font-size:8pt;font-weight:bold}h1{font-size:20pt;line-height:1.15;margin:16px 0 3px}h2{font-size:12pt;margin:0 0 9px;padding-bottom:5px;border-bottom:1px solid #dfdfd7;color:#24251f}section{margin:15px 0;break-inside:auto}section h2,section .grid{break-inside:avoid}p{margin:6px 0 9px;overflow-wrap:anywhere}.muted{color:#66685f;font-size:8.5pt}.warning{border-left:3px solid #bb5cff;background:#f7effb;padding:8px 10px;font-size:9pt}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 16px}.grid small{display:block;color:#66685f;font-size:8pt}.grid b{font-size:10pt;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:8.5pt}thead{display:table-header-group}tr{break-inside:avoid}th,td{border:1px solid #dadbd2;text-align:left;vertical-align:top;padding:6px 7px;overflow-wrap:anywhere}th{background:#f0f0eb;color:#34352f}td{white-space:pre-wrap}.flow{display:flex;flex-wrap:wrap;gap:5px}.step{flex:1 1 105px;padding:8px;border:1px solid #dddcd4;border-radius:7px;font-size:12pt}.step.done{border-color:#b0cb17;background:#f8fce5}.step.pending{color:#777}.step small{display:block;font-size:7.5pt}.completeness b{font-size:18pt}.completeness span{margin-left:8px;color:#666}.completeness i{display:block;height:8px;margin-top:6px;background:linear-gradient(90deg,#d2ff00 var(--progress),#edede8 var(--progress));border-radius:8px}footer{margin-top:22px;border-top:1px solid #ddd;padding-top:8px;color:#666;font-size:8pt}small,b{line-height:1.4}</style></head><body>'+body+'</body></html>'
def chrome_path():
    import shutil as sh
    paths=[Path(os.environ.get("ProgramFiles","C:/Program Files"))/"Google/Chrome/Application/chrome.exe",Path(os.environ.get("ProgramFiles(x86)","C:/Program Files (x86)"))/"Microsoft/Edge/Application/msedge.exe",Path(os.environ.get("ProgramFiles","C:/Program Files"))/"Microsoft/Edge/Application/msedge.exe",Path(os.environ.get("LOCALAPPDATA",""))/"Google/Chrome/Application/chrome.exe"]
    return next((p for p in paths if p.exists()),None) or sh.which("chrome") or sh.which("msedge")
def make_pdf(e,p,tasks,sections,kind,history,language="en"):
    browser=chrome_path()
    if not browser:raise RuntimeError("Install Chrome or Edge to create downloadable PDF reports.")
    pid=re.sub(r"[^A-Za-z0-9_-]","",p["id"])[:40] or "patient"
    day=re.sub(r"[^0-9-]","",e["date"])[:10] or "date"
    eid=re.sub(r"[^A-Za-z0-9_-]","",e["id"])[:40] or "encounter"
    name="ASHA_SAHAYAK_EMR_"+pid+"_"+day+"_"+eid+".pdf"
    with tempfile.TemporaryDirectory(prefix="asha-report-") as td:
        base=Path(td); page=base/"report.html"; out=base/name
        page.write_text(report_html(e,p,tasks,sections,kind,history,language),encoding="utf-8")
        args=[str(browser),"--headless","--no-sandbox","--disable-gpu","--disable-gpu-compositing","--disable-gpu-rasterization","--use-gl=swiftshader","--use-angle=swiftshader","--disable-gpu-sandbox","--disable-features=Vulkan,WebGPU,UseSkiaRenderer,CanvasOopRasterization,SkiaGraphite","--disable-first-run-ui","--no-first-run","--no-pdf-header-footer","--user-data-dir="+str(base/"profile"),"--print-to-pdf="+str(out),page.resolve().as_uri()]
        try:result=subprocess.run(args,capture_output=True,text=True,timeout=45,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except subprocess.TimeoutExpired:raise RuntimeError("PDF generation timed out; try a shorter transcript.")
        if result.returncode or not out.is_file():raise RuntimeError("Browser PDF renderer failed: "+(result.stderr or result.stdout or "no output")[-500:])
        content=out.read_bytes()
        if len(content)<500 or not content.startswith(b"%PDF-"):raise RuntimeError("PDF validation failed.")
        REPORTS.mkdir(exist_ok=True);target=REPORTS/name;target.write_bytes(content);restrict_permissions(target,0o600)
    return name,content
def saved_report_names(s):
    patients={p.get("id"):p for p in s["patients"]}
    names=set()
    for e in s["encounters"]:
        p=patients.get(e.get("patient_id"))
        if not p:continue
        pid=re.sub(r"[^A-Za-z0-9_-]","",p["id"])[:40] or "patient"
        day=re.sub(r"[^0-9-]","",str(e.get("date","")))[:10] or "date"
        eid=re.sub(r"[^A-Za-z0-9_-]","",e["id"])[:40] or "encounter"
        names.add(f"ASHA_SAHAYAK_EMR_{pid}_{day}_{eid}.pdf")
    return names
class Handler(BaseHTTPRequestHandler):
    server_version="ASHA-SAHAYAK"
    def version_string(self):return self.server_version
    def log_message(self,fmt,*args):return
    def end_headers(self):
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Permissions-Policy","microphone=(self), camera=(), geolocation=()")
        self.send_header("Content-Security-Policy","default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'")
        super().end_headers()
    def local_host_request(self):
        port=self.server.server_address[1]
        allowed={f"127.0.0.1:{port}",f"localhost:{port}"}
        if port==80:allowed.update(("127.0.0.1","localhost"))
        return self.headers.get("Host","").lower() in allowed
    def send_json(self,obj,status=200):
        data=json.dumps(obj,ensure_ascii=False).encode("utf-8");self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def body(self):
        try:n=int(self.headers.get("Content-Length","0"))
        except ValueError:raise ValueError("Invalid request size.")
        if n<0 or n>2_000_000:raise ValueError("Request is too large.")
        if self.headers.get_content_type().lower()!="application/json":raise ValueError("Requests must use application/json.")
        try:payload=json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:raise ValueError("Invalid JSON.")
        if not isinstance(payload,dict):raise ValueError("The request body must be a JSON object.")
        return payload
    def audio_body(self):
        try:n=int(self.headers.get("Content-Length","0"))
        except ValueError:raise ValueError("Invalid audio size.")
        if n<1:raise ValueError("Choose an audio file first.")
        if n>GEMINI_UPLOAD_LIMIT:raise ValueError("Audio is limited to 20 MB.")
        mime=self.headers.get_content_type().lower()
        if not mime.startswith("audio/"):raise ValueError("The request must contain an audio file.")
        audio=self.rfile.read(n)
        if len(audio)!=n:raise ValueError("The audio upload was incomplete. Try again.")
        return audio,mime
    def do_GET(self):
        if not self.local_host_request():return self.send_error(421,"Invalid host.")
        path=urlparse(self.path).path
        if path=="/healthz":return self.send_json({"status":"ok"})
        if path=="/api/capabilities":return self.send_json({"gemini_transcription":bool(GEMINI_API_KEY)})
        if path=="/api/bootstrap":
            try:
                s=load();available=saved_report_names(s)
                return self.send_json({"state":s,"metrics":calc(s),"pdfs":sorted(name for name in available if (REPORTS/name).is_file()),"database":"PostgreSQL" if not using_local_store() else "SQLite (local)"})
            except RuntimeError as ex:return self.send_json({"error":str(ex)},503)
            except Exception as ex:
                print("Database bootstrap failed: "+type(ex).__name__)
                return self.send_json({"error":"Could not prepare the configured database. Check the connection and database permissions."},503)
        if path.startswith("/api/reports/"):
            name=unquote(path.rsplit("/",1)[-1])
            if not re.fullmatch(r"ASHA_SAHAYAK_EMR_[A-Za-z0-9_-]+_[0-9-]+(?:_[A-Za-z0-9_-]+)?\.pdf",name):return self.send_error(400)
            try:
                if name not in saved_report_names(load()):return self.send_error(404)
            except RuntimeError as ex:return self.send_json({"error":str(ex)},503)
            except Exception as ex:
                print("Report lookup failed: "+type(ex).__name__)
                return self.send_json({"error":"The configured database is unavailable. Check the connection and try again."},503)
            f=(REPORTS/name).resolve()
            if REPORTS.resolve() not in f.parents or not f.is_file():return self.send_error(404)
            b=f.read_bytes();self.send_response(200);self.send_header("Content-Type","application/pdf");self.send_header("Content-Disposition",'attachment; filename="'+name+'"');self.send_header("Content-Length",str(len(b)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(b);return
        if path=="/":path="/index.html"
        static_name=unquote(path.lstrip("/"))
        if static_name not in PUBLIC_FILES:return self.send_error(404)
        file=ROOT/static_name
        if not file.is_file():return self.send_error(404)
        b=file.read_bytes();typ=mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        self.send_response(200);self.send_header("Content-Type",typ+("; charset=utf-8" if typ.startswith("text/") or typ in ("application/javascript","application/json") else ""));self.send_header("Content-Length",str(len(b)));self.send_header("X-Content-Type-Options","nosniff");self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(b)
    def do_POST(self):
        if not self.local_host_request():return self.send_error(421,"Invalid host.")
        if self.headers.get("Sec-Fetch-Site","").lower()=="cross-site":return self.send_error(403,"Cross-site requests are not allowed.")
        origin=self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower()!=self.headers.get("Host","").lower():return self.send_error(403,"Cross-origin requests are not allowed.")
        path=urlparse(self.path).path
        try:
            if path=="/api/transcribe":
                audio,mime=self.audio_body()
                try:result=gemini_transcribe_and_extract(audio,mime)
                except GeminiServiceError as ex:return self.send_json({"error":str(ex)},502)
                return self.send_json(result)
            d=self.body()
            if path=="/api/extract":
                transcript=d.get("transcript","")
                if not isinstance(transcript,str):return self.send_json({"error":"Transcript must be text."},400)
                return self.send_json({"fields":extract(transcript),"mode":"Literal phrase matching; review every suggestion"})
            with LOCK:
                s=load()
                if path=="/api/patients":
                    name=str(d.get("name","")).strip()
                    if len(name)<2 or len(name)>100:return self.send_json({"error":"Enter a name from 2 to 100 characters."},400)
                    raw_age=str(d.get("age","")).strip()
                    if raw_age and not raw_age.isdigit():return self.send_json({"error":"Age must be a whole number between 0 and 120."},400)
                    age=int(raw_age) if raw_age else None
                    if age is not None and age>120:return self.send_json({"error":"Age must be 120 or younger."},400)
                    gender=str(d.get("gender","Not documented"))
                    if gender not in ("Not documented","Female","Male","Other"):return self.send_json({"error":"Choose a listed gender value."},400)
                    village=str(d.get("village","")).strip()
                    if len(village)>100:return self.send_json({"error":"Village name must be 100 characters or fewer."},400)
                    if any(x["name"].casefold()==name.casefold() and x.get("village","").casefold()==village.casefold() for x in s["patients"]):return self.send_json({"error":"Possible duplicate: same name and village."},409)
                    p={"id":new_id("PT"),"name":name,"age":age,"gender":gender,"village":village,"created_at":now()};s["patients"].append(p);save(s);return self.send_json({"patient":p,"state":s})
                if path.startswith("/api/patients/"):
                    pid=path.rsplit("/",1)[-1];patient=next((x for x in s["patients"] if x["id"]==pid),None)
                    if not patient:return self.send_json({"error":"Patient not found."},404)
                    name=str(d.get("name","")).strip();village=str(d.get("village","")).strip()
                    if len(name)<2 or len(name)>100:return self.send_json({"error":"Enter a name from 2 to 100 characters."},400)
                    if len(village)>100:return self.send_json({"error":"Village name must be 100 characters or fewer."},400)
                    if any(x["id"]!=pid and x["name"].casefold()==name.casefold() and x.get("village","").casefold()==village.casefold() for x in s["patients"]):return self.send_json({"error":"Possible duplicate: another patient has the same name and village."},409)
                    raw_age=str(d.get("age","")).strip()
                    if raw_age and not raw_age.isdigit():return self.send_json({"error":"Age must be a whole number between 0 and 120."},400)
                    age=int(raw_age) if raw_age else None
                    if age is not None and age>120:return self.send_json({"error":"Age must be 120 or younger."},400)
                    gender=str(d.get("gender","Not documented"))
                    if gender not in ("Not documented","Female","Male","Other"):return self.send_json({"error":"Choose a listed gender value."},400)
                    patient.update({"name":name,"age":age,"gender":gender,"village":village,"updated_at":now()});save(s)
                    return self.send_json({"patient":patient,"state":s})
                if path=="/api/encounters":
                    p=next((x for x in s["patients"] if x["id"]==str(d.get("patient_id",""))),None)
                    if not p:return self.send_json({"error":"Choose a saved patient."},400)
                    encounter_date=str(d.get("date",date.today().isoformat()))
                    try:
                        if date.fromisoformat(encounter_date).isoformat()!=encounter_date:raise ValueError
                    except ValueError:return self.send_json({"error":"Choose a valid encounter date."},400)
                    eid=str(d.get("id") or new_id("EN"));old=next((x for x in s["encounters"] if x["id"]==eid),None)
                    if old and old["patient_id"]!=p["id"]:return self.send_json({"error":"An encounter cannot be moved to another patient."},409)
                    if old and old["status"]=="approved":return self.send_json({"error":"Finalized encounter is immutable. Start an amendment first."},409)
                    fs={}
                    supplied_fields=d.get("fields",{})
                    if not isinstance(supplied_fields,dict):return self.send_json({"error":"Clinical fields must be an object."},400)
                    for key,_ in FIELDS:
                        x=supplied_fields.get(key,{})
                        if not isinstance(x,dict):return self.send_json({"error":"Invalid clinical field data."},400)
                        review=x.get("review","unreviewed")
                        if review not in ("unreviewed","accepted","edited","unknown"):review="unreviewed"
                        fs[key]={"value":str(x.get("value",""))[:3000],"source":str(x.get("source",""))[:1000],"review":review,"origin":str(x.get("origin","worker"))}
                    e={"id":eid,"patient_id":p["id"],"date":encounter_date,"type":str(d.get("type","Community health visit"))[:100],"language":str(d.get("language","Not selected"))[:100],"transcript":str(d.get("transcript",""))[:20000],"transcription_mode":str(d.get("transcription_mode","Manual entry"))[:100],"status":"review" if d.get("status")=="review" else "draft","fields":fs,"created_at":old.get("created_at") if old else now(),"updated_at":now(),"approved_at":None,"audit":(old.get("audit",[]) if old else [])+[{"at":now(),"action":"draft saved"}],"revisions":old.get("revisions",[]) if old else []}
                    if old:s["encounters"][s["encounters"].index(old)]=e
                    else:s["encounters"].insert(0,e)
                    save(s);return self.send_json({"encounter":e,"state":s})
                if path.startswith("/api/encounters/") and path.endswith("/approve"):
                    eid=path.split("/")[3];e=next((x for x in s["encounters"] if x["id"]==eid),None)
                    if not e:return self.send_json({"error":"Encounter not found."},404)
                    if e["status"]=="approved":return self.send_json({"error":"Already finalized."},409)
                    if any(e["fields"].get(k,{}).get("review") not in ("accepted","edited","unknown") for k,_ in FIELDS):return self.send_json({"error":"Review each field as accepted, edited, or unknown before finalizing."},409)
                    e["status"]="approved";e["approved_at"]=now();e["audit"].append({"at":now(),"action":"encounter finalized"});save(s);return self.send_json({"encounter":e,"state":s})
                if path.startswith("/api/encounters/") and path.endswith("/amend"):
                    eid=path.split("/")[3];e=next((x for x in s["encounters"] if x["id"]==eid),None);reason=str(d.get("reason","")).strip()
                    if not e or e["status"]!="approved" or len(reason)<4:return self.send_json({"error":"Approved encounter and amendment reason are required."},400)
                    e["revisions"].append({"at":now(),"reason":reason,"snapshot":{"fields":e["fields"],"transcript":e["transcript"],"status":e["status"]}});e["status"]="review";e["approved_at"]=None;e["audit"].append({"at":now(),"action":"amendment started","note":reason});save(s);return self.send_json({"encounter":e,"state":s})
                if path=="/api/followups":
                    purpose=str(d.get("purpose","")).strip();pid=str(d.get("patient_id",""))
                    if not any(p["id"]==pid for p in s["patients"]) or len(purpose)<3:return self.send_json({"error":"Choose a patient and enter a follow-up purpose."},400)
                    encounter_id=str(d.get("encounter_id",""))
                    if encounter_id and not any(e["id"]==encounter_id and e["patient_id"]==pid for e in s["encounters"]):return self.send_json({"error":"The selected encounter does not belong to this patient."},400)
                    due=str(d.get("due_date",date.today().isoformat()))
                    try:
                        if date.fromisoformat(due).isoformat()!=due:raise ValueError
                    except ValueError:return self.send_json({"error":"Choose a valid follow-up date."},400)
                    f={"id":new_id("FU"),"patient_id":pid,"encounter_id":encounter_id,"due_date":due,"purpose":purpose[:500],"status":"pending","completion_note":""};s["followups"].insert(0,f);save(s);return self.send_json({"followup":f,"state":s})
                if path.startswith("/api/followups/") and path.endswith("/update"):
                    fid=path.split("/")[3];f=next((x for x in s["followups"] if x["id"]==fid),None)
                    if not f:return self.send_json({"error":"Follow-up not found."},404)
                    if f["status"]!="pending":return self.send_json({"error":"Completed follow-ups cannot be edited."},409)
                    purpose=str(d.get("purpose","")).strip();due=str(d.get("due_date",""))
                    if len(purpose)<3:return self.send_json({"error":"Enter a follow-up purpose."},400)
                    try:date.fromisoformat(due)
                    except ValueError:return self.send_json({"error":"Choose a valid due date."},400)
                    f.update({"purpose":purpose[:500],"due_date":due,"updated_at":now()});save(s)
                    return self.send_json({"followup":f,"state":s})
                if path.startswith("/api/followups/") and path.endswith("/complete"):
                    fid=path.split("/")[3];f=next((x for x in s["followups"] if x["id"]==fid),None)
                    if not f:return self.send_json({"error":"Follow-up not found."},404)
                    f["status"]="completed";f["completion_note"]=str(d.get("completion_note",""))[:1000];f["completed_at"]=now();save(s);return self.send_json({"followup":f,"state":s})
                if path=="/api/export":
                    kind=str(d.get("report_type","comprehensive"))
                    language=str(d.get("report_language","en"))
                    if kind not in ("comprehensive","summary","followup","audit","history","patient"):return self.send_json({"error":"Choose a supported report type."},400)
                    if language not in ("en","hi","hinglish"):return self.send_json({"error":"Choose a supported report-heading language."},400)
                    e=next((x for x in s["encounters"] if x["id"]==str(d.get("encounter_id",""))),None)
                    if not e:return self.send_json({"error":"Saved encounter not found. Save the draft first."},404)
                    p=next((x for x in s["patients"] if x["id"]==e["patient_id"]),None)
                    if not p:return self.send_json({"error":"Patient record not found."},404)
                    sections=d.get("sections",[]);allowed={"patient","summary","clinical","observations","transcript","evidence","workflow","completeness","followups","history"}
                    if not isinstance(sections,list):return self.send_json({"error":"Invalid report sections."},400)
                    if any(not isinstance(x,str) or x not in allowed for x in sections):return self.send_json({"error":"The report contains an unsupported section."},400)
                    sections=list(dict.fromkeys(sections))
                    if "patient" not in sections or not ({"summary","clinical"}&set(sections)):return self.send_json({"error":"Include patient details and at least one clinical section."},400)
                    history=[x for x in s["encounters"] if x["patient_id"]==p["id"]]
                    tasks=[x for x in s["followups"] if x["patient_id"]==p["id"] and (not x.get("encounter_id") or x["encounter_id"]==e["id"])]
                    try:name,b=make_pdf(e,p,tasks,sections,kind,history,language)
                    except RuntimeError as ex:return self.send_json({"error":str(ex)},500)
                    self.send_response(200);self.send_header("Content-Type","application/pdf");self.send_header("Content-Disposition",'attachment; filename="'+name+'"');self.send_header("Content-Length",str(len(b)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(b);return
                return self.send_error(404)
        except ValueError as ex:return self.send_json({"error":str(ex)},400)
        except Exception as ex:
            print("Request failed: "+type(ex).__name__)
            if type(ex).__module__.startswith("psycopg"):
                return self.send_json({"error":"The configured database is unavailable. Check the connection and try again."},503)
            return self.send_json({"error":"Request failed. Please retry or contact the local administrator."},500)
def main():
    host=configured_host();port=int(os.environ.get("ASHA_PORT","8765"))
    if not 1<=port<=65535:raise SystemExit("ASHA_PORT must be between 1 and 65535.")
    try:server=ThreadingHTTPServer((host,port),Handler)
    except OSError as ex:
        reason=ex.strerror or str(ex)
        raise SystemExit("Could not start ASHA SAHAYAK at http://"+host+":"+str(port)+" ("+reason+"). Stop the existing app from its terminal with Ctrl+C, or set ASHA_PORT to another available port.") from None
    server.daemon_threads=True
    print("ASHA SAHAYAK web service at http://"+host+":"+str(port));print("Database: PostgreSQL (DATABASE_URL configured)" if DATABASE_URL else "Database: SQLite (local file)");print("Web access remains loopback-only. Ctrl+C to stop.")
    try:server.serve_forever()
    except KeyboardInterrupt:print("Stopping.")
    finally:server.server_close()
if __name__=="__main__":main()

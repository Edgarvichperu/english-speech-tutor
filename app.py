import copy
import hmac
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
from io import BytesIO

import pandas as pd
import pypdf
import streamlit as st
from google import genai
from google.genai import types
from gtts import gTTS
from streamlit_gsheets import GSheetsConnection

# ==============================================================================
# 1. CONFIGURACIÓN GENERAL
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "english_progress.db")

st.set_page_config(
    page_title="🎓 Coach Edgarvich - ENGL 107 (A1/A2)",
    layout="wide",
    page_icon="🇬🇧",
)

DEFAULT_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1ZIoxtogVHKBbFApxDVLE1b8Ofx0IxIBdNdPMkE_-mro/edit?usp=sharing"
ROSTER_TAB = "speech"
LEDGER_TAB = "speech_logs"
LEDGER_COLUMNS = [
    "timestamp", "student", "level", "task",
    "transcription", "score", "feedback"
]
MAX_ATTEMPTS = 2
LAB_STUDENT_QUESTIONS_GOAL = 2
LEDGER_BATCH_SIZE = 3
LEDGER_MAX_AGE_SECONDS = 90
STORY_VARIANTS = 4
STORY_CACHE_TTL = 12 * 3600
DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-3.6-flash"

# ------------------------------------------------------------------------------
# Las 5 Reglas de Oro Comunicativas (system_instruction)
# ------------------------------------------------------------------------------
TUTOR_RULES = """
You are Coach Edgarvich, an engaging, supportive conversational English coach for adult A1-A2 learners (Spanish native speakers).

STRICT 2-PART STRUCTURE FOR EVERY TUTOR TURN:
1. PART 1 (MODELING / CONVERSATIONAL RECAST):
   - In your very FIRST sentence, naturally rephrase what the student just said or asked with correct morphology, word order, and tense.
   - Example (Student says "Yesterday I go to market"): "Oh, you went to the market yesterday!"
   - Example (Student says "He wake up at 7am"): "Right, he wakes up early at 6:30 AM in the story!"
2. PART 2 (COMMUNICATIVE FOLLOW-UP & SCAFFOLDING):
   - Ask an open follow-up question directly connected to the situation or the student's life.
   - ALWAYS accompany the question with two scaffolded sentence models (Model A and Model B) so the student has an immediate structure to answer.

ABSOLUTE RESTRICTION - ZERO CORRECTIVE METALANGUAGE:
- NEVER write "Incorrect", "Wrong", "That's not right", "Remember to use past tense", "The rule is...", "You should say...", or "Good grammar".
- Never give dry grammar lessons or explanations of verb tenses unless the student explicitly asks a grammar question.

LANGUAGE POLICY:
English by default. If the student writes or asks in Spanish, help in ONE short Spanish sentence, provide the English communicative equivalent ("In English: ..."), and hand the turn back with Model A and Model B starters.
"""


def _secret(name, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def _clean_sheet_url(value) -> str:
    value = str(value or "").strip().strip('"').strip("'")
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", value)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/edit"
    if re.fullmatch(r"[a-zA-Z0-9_-]{25,}", value):
        return f"https://docs.google.com/spreadsheets/d/{value}/edit"
    return ""


def get_spreadsheet_url() -> str:
    candidates = []
    try:
        candidates.append(st.secrets["connections"]["gsheets"]["spreadsheet"])
    except Exception:
        pass
    candidates.append(_secret("SPREADSHEET_URL"))
    candidates.append(DEFAULT_SPREADSHEET_URL)
    for c in candidates:
        url = _clean_sheet_url(c)
        if url:
            return url
    return _clean_sheet_url(DEFAULT_SPREADSHEET_URL)


SPREADSHEET_URL = get_spreadsheet_url()


def get_gsheets_conn():
    return st.connection("gsheets", type=GSheetsConnection)


MODEL_NAME = _secret("MODEL_NAME", DEFAULT_MODEL)
INSTRUCTOR_KEY = _secret("INSTRUCTOR_KEY")
THINKING_LEVEL = str(_secret("THINKING_LEVEL", "low") or "").strip().lower()


def md_safe(text) -> str:
    return str(text or "").replace("$", "\\$")


# ==============================================================================
# 2. ESTADO DE SESIÓN
# ==============================================================================
@st.cache_resource
def get_latency_log():
    return []


DEFAULTS = {
    "app_mode": "🎯 Guided Missions (A1/A2)",
    "cefr_level": "A1",
    "phase": "onboarding",
    "current_story": "",
    "questions_list": [],
    "current_q_idx": 0,
    "correct_answers_count": 0,
    "attempts": 0,
    "personal_turn": 0,
    "celebrated": False,
    "xp": 0,
    "streak": 0,
    "chat_messages": [],
    "pdf_chat_messages": [],
    "pdf_text_content": "",
    "pdf_name": "",
    "last_processed_audio": None,
    "tts_slow": False,
    "active_user": None,
    "ledger_error": None,
    "active_model": MODEL_NAME,
    "thinking_ok": True,
    "ledger_buffer": [],
    "ledger_last_flush": 0.0,
    "voice_on": True,
    "story_audio": False,
    "personal_question": "",
    "lab_stage": "idle",
    "lab_asked": 0,
    "lab_tutor_turn": 0,
    "lab_role": "",
    "lab_last_student": "",
    "lab_last_recast": "",
    "lab_logged": False,
}
for _k, _v in DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = copy.deepcopy(_v)


def reset_lab():
    s = st.session_state
    s.pdf_chat_messages = []
    s.lab_stage = "idle"
    s.lab_asked = 0
    s.lab_tutor_turn = 0
    s.lab_role = ""
    s.lab_last_student = ""
    s.lab_last_recast = ""
    s.lab_logged = False


def reset_mission(full: bool = False):
    s = st.session_state
    flush_ledger(force=True)
    s.phase = "onboarding"
    s.current_story = ""
    s.questions_list = []
    s.current_q_idx = 0
    s.correct_answers_count = 0
    s.attempts = 0
    s.personal_turn = 0
    s.celebrated = False
    s.chat_messages = []
    s.last_processed_audio = None
    s.personal_question = ""
    s.story_audio = False
    if full:
        s.xp = 0
        s.streak = 0
        s.pdf_text_content = ""
        s.pdf_name = ""
        reset_lab()


# ==============================================================================
# 3. PERSISTENCIA
# ==============================================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn


def init_db():
    conn = get_db_connection()
    with conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS diagnostics
               (id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT, cefr_level TEXT, phase TEXT,
                student_input TEXT, ai_feedback TEXT, score_status TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
        )
    conn.close()


init_db()


def sheets_write_status():
    try:
        conns = st.secrets["connections"]
    except Exception as e:
        return False, f"Missing [connections] in secrets ({type(e).__name__})."
    try:
        gs = conns["gsheets"]
    except Exception:
        return False, f"Missing [connections.gsheets]. Found: {list(conns.keys())}"
    keys = list(gs.keys())
    if str(gs.get("type", "")) != "service_account":
        return False, f'Missing type = "service_account" inside [connections.gsheets]. Found: {keys}'
    if not str(gs.get("spreadsheet", "")).strip() and not DEFAULT_SPREADSHEET_URL:
        return False, "Missing spreadsheet URL in [connections.gsheets]."
    missing = [k for k in ("project_id", "private_key", "client_email") if not gs.get(k)]
    if missing:
        return False, f"Missing service account fields: {missing}"
    return True, "ok"


def sheets_write_enabled() -> bool:
    return sheets_write_status()[0]


def append_to_ledger(row):
    rows = row if isinstance(row, list) else [row]
    target_url = SPREADSHEET_URL or DEFAULT_SPREADSHEET_URL
    if not target_url:
        return "Falta la URL de Google Sheets."
    ok, reason = sheets_write_status()
    if not ok:
        return f"Escritura desactivada: {reason}"
    try:
        conn = get_gsheets_conn()
        try:
            existing = conn.read(spreadsheet=target_url, worksheet=LEDGER_TAB, ttl=0)
        except Exception:
            existing = None

        new_row = pd.DataFrame(rows, columns=LEDGER_COLUMNS)
        if existing is None or existing.dropna(how="all").empty:
            data = new_row
        else:
            data = pd.concat([existing.dropna(how="all"), new_row], ignore_index=True)

        try:
            conn.update(spreadsheet=target_url, worksheet=LEDGER_TAB, data=data)
        except Exception as e_update:
            try:
                conn.create(spreadsheet=target_url, worksheet=LEDGER_TAB, data=data)
            except Exception as e_create:
                raise RuntimeError(f"update: {e_update} | create: {e_create}")
        return None
    except Exception as e:
        return str(e)


def log_diagnostic(name, level, task, user_input, feedback, score):
    try:
        conn = get_db_connection()
        with conn:
            conn.execute(
                """INSERT INTO diagnostics
                   (student_name, cefr_level, phase, student_input, ai_feedback, score_status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, level, task, user_input, feedback, score),
            )
        conn.close()
    except Exception as e:
        print(f"Error SQLite: {e}")

    st.session_state.ledger_buffer.append({
        "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "student": name,
        "level": level,
        "task": task,
        "transcription": user_input,
        "score": score,
        "feedback": (feedback or "")[:250],
    })
    flush_ledger()


def flush_ledger(force: bool = False):
    s = st.session_state
    buf = s.ledger_buffer
    if not buf:
        return
    now = time.time()
    if not s.ledger_last_flush:
        s.ledger_last_flush = now
    due = force or len(buf) >= LEDGER_BATCH_SIZE or (now - s.ledger_last_flush) >= LEDGER_MAX_AGE_SECONDS
    if due and not force and s.ledger_error and (now - s.ledger_last_flush) < 30:
        due = False
    if not due:
        return
    err = append_to_ledger(list(buf))
    s.ledger_error = err
    s.ledger_last_flush = now
    s.ledger_buffer = [] if err is None else buf[-60:]


@st.cache_data(ttl=30, show_spinner=False)
def read_ledger_sheet(url: str):
    return get_gsheets_conn().read(spreadsheet=url, worksheet=LEDGER_TAB, ttl=0)


def load_ledger_df():
    target_url = SPREADSHEET_URL or DEFAULT_SPREADSHEET_URL
    try:
        df = read_ledger_sheet(target_url)
        if df is not None and not df.dropna(how="all").empty:
            return df.dropna(how="all"), "Google Sheets"
    except Exception:
        pass
    conn = get_db_connection()
    df = pd.read_sql_query(
        "SELECT timestamp, student_name as student, cefr_level as level, phase as task, "
        "student_input as transcription, score_status as score, ai_feedback as feedback "
        "FROM diagnostics ORDER BY timestamp DESC",
        conn,
    )
    conn.close()
    return df, "SQLite local"


# ==============================================================================
# 4. ROSTER DESDE GOOGLE SHEETS
# ==============================================================================
def _normalize_roster(df: pd.DataFrame) -> dict:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    name_c = next((c for c in df.columns if any(k in c for k in ["student", "name", "nombre"])), None)
    pin_c = next((c for c in df.columns if c != name_c and any(k in c for k in ["pin", "pass", "clave", "code"])), None)
    if name_c is None or pin_c is None:
        raise ValueError(f"No columns 'student' and 'pin'. Found: {list(df.columns)}")
    df = df.dropna(subset=[name_c, pin_c])
    names = df[name_c].astype(str).str.strip()
    pins = df[pin_c].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    roster = {n: p for n, p in zip(names, pins) if n and n.lower() != "nan" and p and p.lower() != "nan"}
    if not roster:
        raise ValueError("Roster tab is empty.")
    return roster


def _public_csv_url(sheet_url: str, tab: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError("Could not extract ID from URL.")
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"


@st.cache_data(ttl=600, show_spinner=False)
def load_roster(sheet_url: str):
    target_url = _clean_sheet_url(sheet_url) or SPREADSHEET_URL or _clean_sheet_url(DEFAULT_SPREADSHEET_URL)
    errors = []
    try:
        conn = get_gsheets_conn()
        df = conn.read(spreadsheet=target_url, worksheet=ROSTER_TAB, ttl=0)
        return _normalize_roster(df), "Google Sheets (conexión)"
    except Exception as e:
        errors.append(f"conexión: {e}")
    try:
        df = pd.read_csv(_public_csv_url(target_url, ROSTER_TAB), dtype=str)
        return _normalize_roster(df), "Google Sheets (CSV público)"
    except Exception as e:
        errors.append(f"CSV público: {e}")
    raise RuntimeError(" | ".join(errors))


# ==============================================================================
# 5. AUDIO EN INGLÉS (gTTS)
# ==============================================================================
def clean_for_tts(text: str) -> str:
    text = re.sub(r"👉?\s*Model[oa]?\s*[A-B]:?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^\w\s'’?!.,;:-]", " ", text)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


@st.cache_data(show_spinner=False)
def generate_english_audio_bytes(text, slow=False):
    cleaned = clean_for_tts(text)
    if not cleaned:
        return None
    try:
        tts = gTTS(text=cleaned, lang="en", tld="us", slow=slow)
        fp = BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception:
        return None


# ==============================================================================
# 6. BARRA LATERAL
# ==============================================================================
with st.sidebar:
    st.title("🇬🇧 Coach Edgarvich")
    st.caption("ENGL 107 - Communicative Reading & Speech Tutor")

    api_key = _secret("GEMINI_API_KEY")
    if not api_key:
        api_key = st.text_input("🔑 Gemini API Key:", type="password")

    st.divider()
    try:
        roster, roster_source = load_roster(SPREADSHEET_URL)
        roster_error = None
    except Exception as e:
        roster, roster_source, roster_error = {}, None, str(e)

    if roster_error:
        st.error(f"❌ Error loading roster:\n{roster_error}")

    if st.button("🔄 Reload Student Roster"):
        load_roster.clear()
        st.rerun()

    names = ["-- Select your name --"] + sorted(roster.keys())
    student_name = st.selectbox("Student Login:", names)
    entered_pin = st.text_input("PIN:", type="password")

    authenticated = False
    if student_name != "-- Select your name --" and entered_pin:
        expected = str(roster.get(student_name, "")).strip()
        if expected and hmac.compare_digest(expected, entered_pin.strip()):
            authenticated = True
            st.success(f"Welcome, {student_name}!")
        else:
            st.error("Invalid PIN.")

    st.divider()
    st.session_state.app_mode = st.radio(
        "Study Mode:",
        ["🎯 Guided Missions (A1/A2)", "📂 Reading & Speech Lab (PDF/TXT)"],
        index=0 if "Guided" in st.session_state.app_mode else 1,
    )

    if authenticated:
        c1, c2 = st.columns(2)
        c1.metric("⭐ XP", st.session_state.xp)
        c2.metric("🔥 Streak", st.session_state.streak)

    if "Guided" in st.session_state.app_mode:
        st.markdown(f"**CEFR Level:** `{st.session_state.cefr_level}`")
        st.markdown(f"**Current Phase:** `{st.session_state.phase.replace('_', ' ').title()}`")
        if st.button("🔄 Reset Mission"):
            reset_mission()
            st.rerun()

    st.session_state.tts_slow = st.toggle("🐢 Slow Audio Pace", value=st.session_state.tts_slow)
    st.session_state.voice_on = st.toggle(
        "🔊 Coach voice (auto audio)", value=st.session_state.voice_on,
        help="Turn off for faster text replies.",
    )

    # ---------------- Panel del Profesor ----------------
    st.divider()
    instructor_input = st.text_input("Teacher Access:", type="password")
    if not INSTRUCTOR_KEY:
        st.caption("ℹ️ Define INSTRUCTOR_KEY in secrets to enable admin panel.")
    elif instructor_input and hmac.compare_digest(instructor_input, str(INSTRUCTOR_KEY)):
        st.subheader("👨‍🏫 Instructor Control Panel")

        with st.expander("🔌 Connection Status", expanded=True):
            st.write(f"**Spreadsheet:** {SPREADSHEET_URL or '❌ empty'}")
            st.write(f"**Roster Worksheet:** `{ROSTER_TAB}`")
            st.write(f"**Logs Worksheet:** `{LEDGER_TAB}`")
            st.write(f"**Roster Source:** {roster_source or '❌ failed'} ({len(roster)} students)")
            st.write(f"**Active Model:** `{st.session_state.active_model}`")
            st.write(
                "**Sheets Writing:** "
                + ("✅ Service Account connected" if sheets_write_enabled() else "⚠️ Local SQLite only")
            )
            _ok, _reason = sheets_write_status()
            if not _ok:
                st.error(f"Reason: {_reason}")
            if st.button("🧪 Write a test row to the ledger"):
                err = append_to_ledger({
                    "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "student": "TEST_STUDENT",
                    "level": "A1",
                    "task": "Test verification",
                    "transcription": "Testing spreadsheet connection",
                    "score": "100",
                    "feedback": "Connection verified",
                })
                if err is None:
                    st.success("✅ Fila escrita correctamente.")
                else:
                    st.error(err)

        with st.expander("📊 Student Progress Ledger", expanded=True):
            if st.button("🔄 Refresh Analytics"):
                read_ledger_sheet.clear()
                st.rerun()
            df_diag, ledger_source = load_ledger_df()
            st.caption(f"Data source: {ledger_source}")
            if not df_diag.empty:
                students = ["All"] + sorted(df_diag["student"].dropna().astype(str).unique())
                chosen = st.selectbox("Filter by student:", students)
                df_show = df_diag if chosen == "All" else df_diag[df_diag["student"] == chosen]
                st.dataframe(df_show)
                st.download_button(
                    "📥 Download CSV", df_show.to_csv(index=False),
                    "student_diagnostics.csv", "text/csv",
                )


# ==============================================================================
# 7. CLIENTE GEMINI Y COMUNICACIÓN
# ==============================================================================
if not api_key or not authenticated:
    st.info("👈 Please enter your Gemini API Key and log in with your name and PIN in the sidebar to begin.")
    st.stop()

if st.session_state.active_user != student_name:
    reset_mission(full=True)
    st.session_state.active_user = student_name


@st.cache_resource
def get_client(key):
    return genai.Client(api_key=key)


client = get_client(api_key)
student_first_name = student_name.split()[0]


def gen(contents, json_mode=False, temperature=0.2, retries=3, system=None, label="turn"):
    def build_cfg(with_thinking):
        kwargs = {"temperature": temperature}
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        if system:
            kwargs["system_instruction"] = system
        if with_thinking:
            try:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=THINKING_LEVEL)
            except Exception:
                pass
        return types.GenerateContentConfig(**kwargs)

    use_thinking = bool(THINKING_LEVEL) and st.session_state.thinking_ok
    cfg = build_cfg(use_thinking)
    model = st.session_state.active_model or MODEL_NAME
    last_err = None
    for attempt in range(retries):
        started = time.perf_counter()
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=cfg)
            log = get_latency_log()
            log.append({"label": label, "model": model, "seconds": round(time.perf_counter() - started, 2)})
            del log[:-60]
            return resp
        except Exception as e:
            last_err = e
            msg = str(e)
            if use_thinking and "thinking" in msg.lower() and ("400" in msg or "INVALID_ARGUMENT" in msg):
                use_thinking = False
                st.session_state.thinking_ok = False
                cfg = build_cfg(False)
                continue
            if ("404" in msg or "NOT_FOUND" in msg) and model != FALLBACK_MODEL:
                model = FALLBACK_MODEL
                st.session_state.active_model = FALLBACK_MODEL
                continue
            if any(k in msg for k in ("503", "UNAVAILABLE", "429")) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last_err


def parse_json(text: str):
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    data = json.loads(cleaned)
    if isinstance(data, list) and data:
        data = data[0]
    return data


FRIENDLY_ERROR = "⚠️ Technical hiccup connecting with your coach. Please try again in a few seconds."

# ==============================================================================
# 8. COMPONENTES Y LÓGICA DE INTERACCIÓN
# ==============================================================================
def render_chat(messages, max_visible=6):
    last_idx = max([i for i, m in enumerate(messages) if m["role"] == "assistant"], default=-1)
    start = max(0, len(messages) - max_visible)
    if start > 0:
        with st.expander(f"📜 Earlier messages ({start})", expanded=False):
            for old in messages[:start]:
                with st.chat_message(old["role"]):
                    st.markdown(md_safe(old["content"]))
    for idx in range(start, len(messages)):
        msg = messages[idx]
        with st.chat_message(msg["role"]):
            st.markdown(md_safe(msg["content"]))
            if idx == last_idx and msg["role"] == "assistant" and msg.get("tts", True) and st.session_state.voice_on:
                audio = generate_english_audio_bytes(msg["content"], slow=st.session_state.tts_slow)
                if audio:
                    st.audio(audio, format="audio/mp3", autoplay=msg.get("auto_play", False))
            if idx == last_idx and msg["role"] == "assistant":
                msg["auto_play"] = False


def collect_input(mic_key: str, mic_label: str, placeholder: str):
    recorded = st.audio_input(mic_label, key=mic_key)
    typed = st.chat_input(placeholder)
    text, audio = None, None
    if recorded is not None:
        raw = recorded.getvalue()
        if raw != st.session_state.last_processed_audio:
            st.session_state.last_processed_audio = raw
            audio = raw
            text = "🎙️ [Spoken audio answer]"
    if typed:
        text, audio = typed, None
    return text, audio


def generate_story_and_questions(topic: str, variant: int = 1):
    prompt = f"""
    You are an expert English as a Second Language (ESL) pedagogical author.
    Design a short reading passage and 5 COMMUNICATIVE questions for adult A1-A2 learners.
    Topic: {topic}
    Variation #{variant}: invent a different main character and setting.

    REQUIREMENTS:
    1. PASSAGE: 90-110 words in clear, authentic English.
       Use Present Simple, high-frequency verbs, and daily vocabulary.
       Bold key conjugated verbs and core target vocabulary with **double asterisks**.
    2. 5 COMMUNICATIVE QUESTIONS:
       Every question must be meaning-bearing and followed by two communicative answer models (Model A and Model B)
       that give students immediate, natural sentence starters to speak or write.
       - Q1-Q2: Direct story-action questions with scaffolds.
       - Q3-Q4: Character intentions and daily routine choices.
       - Q5: Communicative inference or personal connection to the character's situation.
       
       For each question provide:
       - question: clear communicative prompt.
       - expected_core: target information.
       - context_hint: original sentence containing the information.
       - model_a: complete sentence starter option 1 (e.g. "He wakes up at...").
       - model_b: complete sentence starter option 2 (e.g. "In the morning, he...").

    3. PERSONAL EXTENSION QUESTION:
       One open question comparing the story with the student's own life, with Model A and Model B.

    Return strictly a valid JSON object:
    {{
      "story": "Full English passage with **bolded verbs**...",
      "personal_question": "Do you enjoy mornings like this too? What is your morning routine?",
      "personal_starters": ["In the morning, I usually...", "Honestly, I prefer to..."],
      "questions": [
        {{"q_num": 1, "question": "What time does David wake up to start his day?", "expected_core": "at 6:30 AM", "context_hint": "David **wakes** up at 6:30 AM every morning.", "model_a": "David wakes up at...", "model_b": "He usually gets up at..."}},
        {{"q_num": 2, "question": "...", "expected_core": "...", "context_hint": "...", "model_a": "...", "model_b": "..."}},
        {{"q_num": 3, "question": "...", "expected_core": "...", "context_hint": "...", "model_a": "...", "model_b": "..."}},
        {{"q_num": 4, "question": "...", "expected_core": "...", "context_hint": "...", "model_a": "...", "model_b": "..."}},
        {{"q_num": 5, "question": "...", "expected_core": "...", "context_hint": "...", "model_a": "...", "model_b": "..."}}
      ]
    }}
    """
    resp = gen([types.Part.from_text(text=prompt)], json_mode=True, temperature=0.2, label="story")
    package = parse_json(resp.text)
    if not package.get("story") or not package.get("questions"):
        raise ValueError("Model response missed passage or questions.")
    return package


@st.cache_data(ttl=STORY_CACHE_TTL, show_spinner=False)
def cached_story_package(topic: str, variant: int):
    return generate_story_and_questions(topic, variant)


def build_personal_opener(package) -> str:
    q = str(package.get("personal_question") or "").strip()
    if not q:
        return ""
    starters = [str(x).strip() for x in (package.get("personal_starters") or []) if str(x).strip()][:2]
    a = starters[0] if starters else "I usually..."
    b = starters[1] if len(starters) > 1 else "In my opinion..."
    return f"Now let's talk about you, {student_first_name}! {q}\n\n👉 **Model A:** \"{a}\"\n👉 **Model B:** \"{b}\""


def start_comprehension(q_item: dict, total: int):
    st.session_state.current_q_idx = 0
    st.session_state.correct_answers_count = 0
    st.session_state.attempts = 0
    st.session_state.phase = "comprehension"
    
    first_msg = (
        f"**Question 1 of {total}:** {q_item['question']}\n\n"
        f"👉 **Model A:** \"{q_item.get('model_a', 'He...')}\"\n"
        f"👉 **Model B:** \"{q_item.get('model_b', 'In my view...')}\""
    )
    st.session_state.chat_messages = [{
        "role": "assistant",
        "content": first_msg,
        "auto_play": True,
    }]


def advance_question(recast_part: str):
    s = st.session_state
    s.current_q_idx += 1
    s.attempts = 0
    total = len(s.questions_list)
    if s.current_q_idx < total:
        nxt_item = s.questions_list[s.current_q_idx]
        msg = (
            f"{recast_part}\n\n---\n"
            f"**Question {s.current_q_idx + 1} of {total}:** {nxt_item['question']}\n\n"
            f"👉 **Model A:** \"{nxt_item.get('model_a', '')}\"\n"
            f"👉 **Model B:** \"{nxt_item.get('model_b', '')}\""
        )
    elif s.correct_answers_count >= max(total - 1, 1):
        s.phase = "personal_production"
        s.personal_turn = 0
        s.celebrated = False
        msg = (
            f"{recast_part}\n\n🎉 **Great communicative job! Score: {s.correct_answers_count}/{total}.**\n\n"
            "Now let's connect this story with your own daily experiences!"
        )
    else:
        s.phase = "review"
        msg = f"{recast_part}\n\n📊 **Activity completed! Score: {s.correct_answers_count}/{total}.**"
    s.chat_messages.append({"role": "assistant", "content": msg, "auto_play": True})
    if s.phase != "comprehension":
        flush_ledger(force=True)


# ---------------- Reading Companion (chat junto al PDF/TXT) ----------------
def lab_system_prompt() -> str:
    doc = st.session_state.pdf_text_content[:8000]
    return TUTOR_RULES + f"""

READING COMPANION ROLE:
- You are a friendly, curious conversation partner linked to the reading passage below (e.g. David's friend, a classmate, or a reading buddy).
- Maintain your character across the conversation.
- STRICT 2-PART RULE: (1) Conversational recast modeling correct form, followed by (2) follow-up question + Model A and Model B sentence starters.
- NEVER lecture, never say "Incorrect", never explain grammar theory.

DOCUMENT TEXT:
\"\"\"{doc}\"\"\"
"""


def lab_opening_prompt() -> str:
    return f"""
The student {student_first_name} just opened this document. Start the conversation.
1. Choose ONE in-character role linked to the text (e.g. "David's roommate" or "your curious reading buddy").
2. Write "message" (max 2-3 short sentences): greet {student_first_name}, mention ONE concrete action from the text,
   and ask the student to formulate their first question about the story. Provide two visible question models.
   👉 Model A: "What does ... ?"
   👉 Model B: "Does he ... ?"

Return strictly JSON: {{"role": "...", "message": "..."}}
"""


def lab_turn_prompt(user_text: str, has_audio: bool) -> str:
    s = st.session_state
    history = "\n".join(
        f"{m['role'].upper()}: {m['content'][:350]}" for m in s.pdf_chat_messages[-8:-1]
    )
    asked, goal = s.lab_asked, LAB_STUDENT_QUESTIONS_GOAL

    if s.lab_stage == "ask_me":
        is_last = (asked + 1) >= goal
        if is_last:
            step_instruction = (
                "The student asked their final question. PART 1: recast their question and answer it in character. "
                "PART 2: Announce it is now your turn and ask a direct story comprehension question, followed by "
                "👉 Model A: '...' and 👉 Model B: '...'."
            )
        else:
            step_instruction = (
                "PART 1: recast and answer their question in character. "
                "PART 2: Invite the student to ask ONE more question using: "
                "👉 Model A: 'Where does ... ?' or 👉 Model B: 'Why does ... ?'."
            )
        stage_rules = f"""
STAGE: ROLE REVERSAL (Student asking questions {asked}/{goal}).
- If student asked a question: set student_asked_question=true. {step_instruction}
- If student wrote in Spanish: explain briefly in Spanish, model the question in English ("In English: ..."), and provide models. student_asked_question=false, used_spanish=true.
- If not a question: recast what they said and invite a question with Model A and Model B. student_asked_question=false.
advance=false."""
    elif s.lab_stage == "tutor_asks":
        if s.lab_tutor_turn <= 1:
            step_instruction = (
                "PART 1: Recast their answer naturally without grading words. "
                "PART 2: Ask an INFERENTIAL or PERSONAL-TRANSFER question related to the character's choices, "
                "accompanied by 👉 Model A: '...' and 👉 Model B: '...'."
            )
        else:
            step_instruction = (
                "PART 1: Recast their answer warmly. "
                "PART 2: Ask a closing reflective question about how their experience compares to the story, "
                "with 👉 Model A: '...' and 👉 Model B: '...'."
            )
        stage_rules = f"""
STAGE: TUTOR ASKS (Turn {s.lab_tutor_turn} of 2).
{step_instruction}
advance=true. student_asked_question=false."""
    else:
        stage_rules = """
STAGE: FREE CONVERSATION.
Always maintain the 2-part structure: (1) Conversational recast, (2) Open conversational question with Model A and Model B.
advance=false. student_asked_question=false."""

    audio_note = (
        'Audio clip attached: transcribe what student said in "transcript" and recast naturally.'
        if has_audio else 'No audio attached: leave "transcript" empty.'
    )

    return f"""
Role: {s.lab_role or "reading buddy"}. Student: {student_first_name}.
RECENT DIALOGUE:
{history}

STUDENT UTTERANCE: "{user_text}"
{audio_note}
{stage_rules}

FIELD GUIDE:
- recast: The student's utterance rewritten correctly as ONE natural conversational sentence.
- message: Exactly 2-3 short sentences.
  * Sentence 1 (Part 1): Natural recast and reaction. No "Correct", "Wrong", or grammar rule names.
  * Sentence 2-3 (Part 2): Communicative follow-up question, followed on new lines by:
    👉 Model A: "..."
    👉 Model B: "..."

Return strictly JSON:
{{"recast": "...", "message": "...", "student_asked_question": true or false, "advance": true or false, "used_spanish": true or false, "transcript": "..."}}
"""


def save_lab_log(label: str) -> bool:
    s = st.session_state
    if not s.lab_last_student:
        return False
    log_diagnostic(
        student_name, s.cefr_level, label,
        s.lab_last_student, f"Recast: {s.lab_last_recast}", "LAB_FINAL",
    )
    flush_ledger(force=True)
    s.lab_logged = True
    return True


def handle_lab_turn(user_text: str, user_audio):
    s = st.session_state
    s.pdf_chat_messages.append({"role": "user", "content": user_text})

    parts = [types.Part.from_text(text=lab_turn_prompt(user_text, bool(user_audio)))]
    if user_audio:
        parts.insert(0, types.Part.from_bytes(data=user_audio, mime_type="audio/wav"))

    try:
        with st.spinner("Coach Edgarvich is conversing..."):
            parsed = parse_json(gen(parts, json_mode=True, temperature=0.3, system=lab_system_prompt(), label="lab_chat").text)
    except Exception as e:
        s.pdf_chat_messages.append(
            {"role": "assistant", "content": f"{FRIENDLY_ERROR}\n\n`{e}`", "tts": False}
        )
        st.rerun()

    transcript = (parsed.get("transcript") or "").strip() if user_audio else ""
    if transcript:
        s.pdf_chat_messages[-1]["content"] = f"🎙️ {transcript}"
    student_final = transcript or user_text
    recast = (parsed.get("recast") or "").strip()
    message = (parsed.get("message") or "").strip() or "Tell me more about what you think!"
    used_spanish = bool(parsed.get("used_spanish", False))

    s.lab_last_student = student_final
    s.lab_last_recast = recast or message[:200]

    finished_structured = False
    if s.lab_stage == "ask_me":
        if bool(parsed.get("student_asked_question", False)):
            s.lab_asked += 1
            s.xp += 5
            if s.lab_asked >= LAB_STUDENT_QUESTIONS_GOAL:
                s.lab_stage = "tutor_asks"
                s.lab_tutor_turn = 1
    elif s.lab_stage == "tutor_asks":
        if bool(parsed.get("advance", False)):
            s.xp += 5
            s.lab_tutor_turn += 1
            if s.lab_tutor_turn > 2:
                s.lab_stage = "free"
                finished_structured = True

    s.pdf_chat_messages.append({
        "role": "assistant", "content": message,
        "tts": not used_spanish, "auto_play": not used_spanish,
    })

    if finished_structured:
        save_lab_log("Lab: structured chat complete")
    st.rerun()


def lab_progress():
    s = st.session_state
    if s.lab_stage == "ask_me":
        done = s.lab_asked
        text = f"Step 1 of 3 · You ask the questions · {s.lab_asked}/{LAB_STUDENT_QUESTIONS_GOAL}"
    elif s.lab_stage == "tutor_asks":
        done = LAB_STUDENT_QUESTIONS_GOAL + (s.lab_tutor_turn - 1)
        text = f"Step 2 of 3 · Coach asks · question {min(s.lab_tutor_turn, 2)}/2"
    else:
        done = LAB_STUDENT_QUESTIONS_GOAL + 2
        text = "Step 3 of 3 · Free conversation 🎉"
    st.progress(min(done / (LAB_STUDENT_QUESTIONS_GOAL + 2), 1.0), text=text)


# ==============================================================================
# 9. MODO 1: MISIONES GUIADAS
# ==============================================================================
if "Guided" in st.session_state.app_mode:

    # ---------------- Fase 0: Onboarding ----------------
    if st.session_state.phase == "onboarding":
        welcome_en = f"Hello {student_first_name}! Welcome to your English communicative session."
        instructions = (
            "We will explore an English story, practice natural spoken conversation, "
            "and build your English fluency step by step.\n\n"
            "💡 **Our Golden Rule:** Coach Edgarvich models correct English naturally without grading or testing pressure. "
            "If you need help or feel stuck, feel free to ask in Spanish anytime!"
        )
        st.markdown(f"### 👋 {welcome_en}")
        st.markdown(instructions)

        audio_intro = (
            generate_english_audio_bytes(
                welcome_en + " Let's talk in English together.", slow=st.session_state.tts_slow
            )
            if st.session_state.voice_on else None
        )
        if audio_intro:
            st.audio(audio_intro, format="audio/mp3")

        st.markdown("---")
        st.markdown("#### Choose a conversation topic:")

        col1, col2 = st.columns(2)
        chosen_topic = None
        with col1:
            if st.button("☕ Morning Routine & Coffee Shop"):
                chosen_topic = "Daily morning routine, ordering coffee, and walking through the neighborhood"
            if st.button("🎒 University Life & College Classes"):
                chosen_topic = "A student attending classes, talking to classmates, and studying at the library"
        with col2:
            if st.button("🛒 Farmers Market & Groceries"):
                chosen_topic = "Buying fresh food at the market using numbers and polite requests"
            if st.button("✈️ Travel & Hotel Check-in"):
                chosen_topic = "Arriving in a new city, checking in at a hotel, and asking for suggestions"

        if chosen_topic:
            try:
                with st.spinner("Preparing communicative passage..."):
                    package = cached_story_package(chosen_topic, random.randint(1, STORY_VARIANTS))
                st.session_state.current_story = package["story"]
                st.session_state.questions_list = package["questions"]
                st.session_state.personal_question = build_personal_opener(package)
                start_comprehension(package["questions"][0], len(package["questions"]))
                st.rerun()
            except Exception as e:
                st.error(f"{FRIENDLY_ERROR}\n\nDetail: {e}")

    # ---------------- Fase 1: Comprensión Guiada ----------------
    elif st.session_state.phase == "comprehension":
        total_q = len(st.session_state.questions_list)
        q_idx = st.session_state.current_q_idx

        st.progress(min(q_idx / total_q, 1.0), text=f"Question {min(q_idx + 1, total_q)} of {total_q}")

        col_read, col_chat = st.columns(2, gap="large")
        with col_read:
            st.markdown("#### 📖 Reading (observe the bold actions)")
            with st.container(height=430, border=True):
                st.markdown(md_safe(st.session_state.current_story))
            if st.session_state.story_audio:
                story_audio = generate_english_audio_bytes(
                    st.session_state.current_story, slow=st.session_state.tts_slow
                )
                if story_audio:
                    st.audio(story_audio, format="audio/mp3")
            elif st.button("🔊 Listen to the passage"):
                st.session_state.story_audio = True
                st.rerun()

        with col_chat:
            st.markdown("#### 💬 Coach Edgarvich")
            render_chat(st.session_state.chat_messages)

        q = st.session_state.questions_list[q_idx]
        st.caption("💡 **Tip:** Answer speaking or typing. Use Model A or Model B to express your ideas.")

        incoming_text, incoming_audio = collect_input(
            f"rec_mic_q_{q_idx}",
            "🎙️ Speak your answer in English:",
            "Type your answer in English (or ask in Spanish)...",
        )

        if incoming_text:
            st.session_state.chat_messages.append({"role": "user", "content": incoming_text})

            eval_prompt = f"""
            READING PASSAGE:
            "{st.session_state.current_story}"

            QUESTION IN CONTEXT:
            Question: "{q['question']}"
            Expected Content: "{q['expected_core']}"
            Story Clue: "{q['context_hint']}"

            STUDENT INPUT: "{incoming_text}"

            STRICT 2-PART COMPLIANCE INSTRUCTIONS:
            1. ZERO CORRECTIVE METALANGUAGE:
               - Do NOT say "Incorrect", "That's wrong", "Remember the rule", or mention grammar rules.
            2. PART 1 (MODELING / RECAST):
               - Your FIRST sentence MUST recast what the student said with correct morphology/syntax in a natural conversational voice.
               - Example: "Yes, he wakes up at 6:30 AM every morning!" or "Oh, he lives in a quiet house near Boston!"
            3. PART 2 (FOLLOW-UP OR HINT WITH MODELS):
               - If the meaning is accurate: validate the idea in one friendly conversational sentence.
               - If the meaning is inaccurate or missing: quote the exact story clue gently, and provide two explicit sentence starters:
                 model_a: "Model A: [Starter 1...]"
                 model_b: "Model B: [Starter 2...]"

            Return strictly a JSON object:
            {{
              "intent": "INQUIRY" or "ANSWER",
              "is_correct": true or false,
              "recast": "...",
              "feedback_text": "2-3 short conversational sentences following Part 1 and Part 2",
              "model_a": "Model A: ...",
              "model_b": "Model B: ..."
            }}
            """
            parts = [types.Part.from_text(text=eval_prompt)]
            if incoming_audio:
                parts.insert(0, types.Part.from_bytes(data=incoming_audio, mime_type="audio/wav"))

            try:
                with st.spinner("Coach Edgarvich is listening..."):
                    parsed = parse_json(gen(parts, json_mode=True, temperature=0.1, system=TUTOR_RULES, label="eval").text)
            except Exception as e:
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": f"{FRIENDLY_ERROR}\n\n`{e}`", "tts": False}
                )
                st.rerun()

            intent = parsed.get("intent", "ANSWER")
            fb_text = parsed.get("feedback_text", "")
            recast = (parsed.get("recast") or "").strip()
            logged_fb = f"Recast: {recast}" if recast else fb_text
            is_correct = bool(parsed.get("is_correct", False))
            m_a, m_b = parsed.get("model_a", ""), parsed.get("model_b", "")

            if intent == "INQUIRY":
                st.session_state.chat_messages.append({
                    "role": "assistant", "tts": False, "auto_play": False,
                    "content": (
                        f"💡 **Language Support:**\n{fb_text}\n\n"
                        f"---\n👉 **Back to the question:** {q['question']}\n\n"
                        f"👉 **{q.get('model_a', '')}**\n👉 **{q.get('model_b', '')}**"
                    ),
                })
                log_diagnostic(student_name, "A1", f"Question {q_idx+1} Inquiry", incoming_text, fb_text, "CLARIFICATION")

            elif is_correct:
                points = 10 if st.session_state.attempts == 0 else 5
                st.session_state.xp += points
                st.session_state.streak += 1
                st.session_state.correct_answers_count += 1
                log_diagnostic(student_name, "A1", f"Question {q_idx+1}", incoming_text, logged_fb, "CORRECT")
                advance_question(f"{fb_text}\n\n⭐ **+{points} XP**")

            else:
                st.session_state.attempts += 1
                st.session_state.streak = 0
                log_diagnostic(student_name, "A1", f"Question {q_idx+1}", incoming_text, logged_fb, "INCORRECT")
                if st.session_state.attempts >= MAX_ATTEMPTS:
                    reveal = (
                        f"{fb_text}\n\n📖 *In the story:* {q['context_hint']}\n"
                        f"👉 *One natural way to say it:* \"{q['expected_core']}\"\n\n_Let's keep conversing!_"
                    )
                    log_diagnostic(student_name, "A1", f"Question {q_idx+1}", incoming_text, "Answer modeled", "REVEALED")
                    advance_question(reveal)
                else:
                    m_first = m_a or f"Model A: {q.get('model_a', '')}"
                    m_second = m_b or f"Model B: {q.get('model_b', '')}"
                    st.session_state.chat_messages.append({
                        "role": "assistant", "tts": False, "auto_play": False,
                        "content": (
                            f"{fb_text}\n\n"
                            f"👉 **{m_first}**\n"
                            f"👉 **{m_second}**\n\n"
                            f"_Try once more using one of these conversational models!_"
                        ),
                    })
            st.rerun()

    # ---------------- Fase 1b: Repaso ----------------
    elif st.session_state.phase == "review":
        with st.expander("📖 English Reading Passage", expanded=False):
            st.markdown(md_safe(st.session_state.current_story))
        render_chat(st.session_state.chat_messages)
        st.markdown("---")
        st.info("Great effort practicing! Would you like to repeat this story or explore a new topic?")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔁 Repeat This Story"):
                start_comprehension(st.session_state.questions_list[0], len(st.session_state.questions_list))
                st.rerun()
        with col_b:
            if st.button("📚 New Story"):
                reset_mission()
                st.rerun()

    # ---------------- Fase 2: Producción Personal ----------------
    elif st.session_state.phase == "personal_production":
        with st.expander("📖 Story Reference", expanded=False):
            st.markdown(md_safe(st.session_state.current_story))

        if st.session_state.personal_turn == 0 and st.session_state.personal_question:
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": st.session_state.personal_question, "auto_play": True}
            )
            st.session_state.personal_turn = 1
            st.rerun()

        turn = st.session_state.personal_turn
        finished = turn > 3

        st.markdown(f"### 💬 Personal English Conversation (Turn {min(turn, 3)} of 3)")
        render_chat(st.session_state.chat_messages)

        if finished:
            if not st.session_state.celebrated:
                flush_ledger(force=True)
                st.balloons()
                st.session_state.celebrated = True
            st.success("🏆 CONGRATULATIONS! You completed your communicative English session.")
            st.metric("⭐ Total XP", st.session_state.xp)
            if st.button("Start a New Mission"):
                reset_mission()
                st.rerun()
            st.stop()

        st.markdown("---")
        p_text, p_audio = collect_input(
            f"rec_mic_p_{turn}",
            "🎙️ Record your response (Speak in English):",
            "Type your response in English (or ask in Spanish if needed)...",
        )

        if p_text:
            st.session_state.chat_messages.append({"role": "user", "content": p_text})
            last_q = next(
                (m["content"] for m in reversed(st.session_state.chat_messages[:-1]) if m["role"] == "assistant"), ""
            )

            p_prompt = f"""
            You are conversing with {student_first_name} in personal practice (Turn {turn} of 3).
            Previous coach message: "{last_q}"
            Student utterance: "{p_text}"

            STRICT 2-PART STRUCTURE:
            - PART 1 (Recast): Your very first sentence must rephrase the student's answer with natural, correct grammar.
              NEVER say "Good grammar", "Incorrect", or "Remember the rule".
            - PART 2 (Follow-up + Models):
              * If Turn < 3: Ask a natural, warm follow-up question connecting their life to the theme, ending with:
                👉 Model A: "..."
                👉 Model B: "..."
              * If Turn == 3 (final): Deliver a warm closing celebrating what the student shared.
            - Total length: maximum 2 to 3 sentences.

            Return strictly JSON:
            {{"intent": "INQUIRY" or "ANSWER", "recast": "...", "message": "..."}}
            """
            parts = [types.Part.from_text(text=p_prompt)]
            if p_audio:
                parts.insert(0, types.Part.from_bytes(data=p_audio, mime_type="audio/wav"))

            try:
                with st.spinner("Coach Edgarvich is thinking..."):
                    parsed = parse_json(gen(parts, json_mode=True, temperature=0.3, system=TUTOR_RULES, label="personal").text)
            except Exception as e:
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": f"{FRIENDLY_ERROR}\n\n`{e}`", "tts": False}
                )
                st.rerun()

            intent = parsed.get("intent", "ANSWER")
            message = parsed.get("message", "")
            recast = (parsed.get("recast") or "").strip()
            logged_fb = f"Recast: {recast}" if recast else message

            if intent == "INQUIRY":
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": message, "tts": False, "auto_play": False}
                )
                log_diagnostic(student_name, "A1", f"Personal Turn {turn} Inquiry", p_text, message, "CLARIFICATION")
            else:
                st.session_state.xp += 5
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": f"{message}\n\n⭐ **+5 XP**", "auto_play": True}
                )
                log_diagnostic(student_name, "A1", f"Personal Turn {turn}", p_text, logged_fb, "PRODUCED")
                st.session_state.personal_turn += 1
            st.rerun()

# ==============================================================================
# 10. MODO 2: READING & SPEECH LAB (PDF / TXT)
# ==============================================================================
else:
    st.subheader("📂 Reading & Speech Lab")
    st.caption(
        "Upload a reading passage. Your Reading Companion discusses it with you: first YOU ask questions, "
        "then the coach asks you. Every interaction provides conversational models."
    )

    uploaded_file = st.file_uploader("Upload PDF or TXT document:", type=["pdf", "txt"])

    if uploaded_file is not None and uploaded_file.name != st.session_state.pdf_name:
        try:
            if uploaded_file.name.lower().endswith(".pdf"):
                reader = pypdf.PdfReader(BytesIO(uploaded_file.getvalue()))
                pages = [(p.extract_text() or "") for p in reader.pages]
                st.session_state.pdf_text_content = "\n".join(pages).strip()
            else:
                st.session_state.pdf_text_content = uploaded_file.getvalue().decode("utf-8", errors="ignore")
            st.session_state.pdf_name = uploaded_file.name
            reset_lab()
        except Exception as e:
            st.error(f"Error reading file: {e}")

    if st.session_state.pdf_text_content:
        s = st.session_state
        words = len(s.pdf_text_content.split())

        col_doc, col_chat = st.columns(2, gap="large")

        with col_doc:
            st.markdown("#### 📄 Reading Passage")
            st.caption(f"{s.pdf_name} · {words} words")
            with st.container(height=520, border=True):
                st.markdown(md_safe(s.pdf_text_content[:30000]))
            if st.button("🔊 Listen to beginning"):
                beginning = generate_english_audio_bytes(s.pdf_text_content[:600], slow=s.tts_slow)
                if beginning:
                    st.audio(beginning, format="audio/mp3", autoplay=True)

        with col_chat:
            st.markdown(f"#### 💬 {s.lab_role or 'Reading Companion'}")
            if s.lab_stage == "idle":
                st.info(
                    "Ready to practice? Meet your in-character Reading Companion. "
                    "First you will ask questions about the text, then you will exchange ideas."
                )
                if st.button("💬 Start Conversation", type="primary"):
                    try:
                        with st.spinner("Your companion is reading the passage..."):
                            opening = parse_json(
                                gen(
                                    [types.Part.from_text(text=lab_opening_prompt())],
                                    json_mode=True, temperature=0.4, system=lab_system_prompt(), label="lab_open",
                                ).text
                            )
                        s.lab_role = (opening.get("role") or "Your reading buddy").strip()
                        first_msg = (opening.get("message") or "").strip() or (
                            f"Hi {student_first_name}! What do you want to know about the story? Ask me {LAB_STUDENT_QUESTIONS_GOAL} questions!\n\n"
                            "👉 **Model A:** \"What does ... ?\"\n"
                            "👉 **Model B:** \"Does he ... ?\""
                        )
                        s.pdf_chat_messages = [{"role": "assistant", "content": first_msg, "auto_play": True}]
                        s.lab_stage = "ask_me"
                        st.rerun()
                    except Exception as e:
                        st.error(f"{FRIENDLY_ERROR}\n\nDetail: {e}")
            else:
                lab_progress()
                render_chat(s.pdf_chat_messages)
                if st.button("💾 Finish & save session"):
                    if save_lab_log("Lab: session end"):
                        st.success("✅ Session saved to your ledger.")
                    else:
                        st.info("Exchange a few messages first, then save your session.")

        if s.lab_stage != "idle":
            n_user_msgs = sum(1 for m in s.pdf_chat_messages if m["role"] == "user")
            lab_text, lab_audio = collect_input(
                f"lab_mic_{n_user_msgs}",
                "🎙️ Speak in English:",
                "Type your response in English (or ask in Spanish)...",
            )
            if lab_text:
                handle_lab_turn(lab_text, lab_audio)
    else:
        st.info("👆 Upload a PDF or TXT file above to activate your Reading Companion.")
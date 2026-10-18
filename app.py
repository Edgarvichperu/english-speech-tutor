import copy
import hmac
import json
import os
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

DEFAULT_SPREADSHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1ZIoxtogVHKBbFApxDVLE1b8Ofx0IxIBdNdPMkE_-mro/edit"
)
ROSTER_TAB = "speech"
LEDGER_TAB = "speech_logs"
LEDGER_COLUMNS = [
    "timestamp", "student", "level", "task",
    "transcription", "score", "feedback"
]
MAX_ATTEMPTS = 2


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
    return DEFAULT_SPREADSHEET_URL


SPREADSHEET_URL = get_spreadsheet_url()


def get_gsheets_conn():
    """Conexión estándar a Google Sheets sin argumentos extra en _connect."""
    return st.connection("gsheets", type=GSheetsConnection)


MODEL_NAME = _secret("MODEL_NAME", "gemini-2.5-flash")
INSTRUCTOR_KEY = _secret("INSTRUCTOR_KEY")  # sin valor por defecto: si falta, el panel queda desactivado

# ==============================================================================
# 2. ESTADO DE SESIÓN
# ==============================================================================
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
}
for _k, _v in DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = copy.deepcopy(_v)


def reset_mission(full: bool = False):
    s = st.session_state
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
    if full:
        s.xp = 0
        s.streak = 0
        s.pdf_chat_messages = []
        s.pdf_text_content = ""
        s.pdf_name = ""


# ==============================================================================
# 3. PERSISTENCIA: SQLITE LOCAL + GOOGLE SHEETS
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
    """Devuelve (ok, motivo). Solo menciona NOMBRES de claves, nunca valores."""
    try:
        conns = st.secrets["connections"]
    except Exception as e:
        return False, (
            f"No encuentro la sección [connections] en los secrets ({type(e).__name__}). "
            "Revisa que .streamlit/secrets.toml esté en la carpeta desde donde ejecutas streamlit, "
            "que el archivo no tenga errores de formato y reinicia la app."
        )
    try:
        gs = conns["gsheets"]
    except Exception:
        return False, f"No existe [connections.gsheets]. Secciones encontradas: {list(conns.keys())}"
    keys = list(gs.keys())
    if str(gs.get("type", "")) != "service_account":
        return False, f'Falta type = "service_account" dentro de [connections.gsheets]. Claves encontradas: {keys}'
    missing = [k for k in ("project_id", "private_key", "client_email") if not gs.get(k)]
    if missing:
        return False, f"Faltan campos de la cuenta de servicio: {missing}"
    return True, "ok"


def sheets_write_enabled() -> bool:
    return sheets_write_status()[0]


def append_to_ledger(row: dict):
    if not SPREADSHEET_URL:
        return "Falta la URL de Google Sheets."
    ok, reason = sheets_write_status()
    if not ok:
        return f"Escritura desactivada: {reason}"
    try:
        conn = get_gsheets_conn()
        try:
            existing = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=LEDGER_TAB, ttl=0)
        except Exception:
            existing = None
        new_row = pd.DataFrame([row], columns=LEDGER_COLUMNS)
        if existing is None or existing.dropna(how="all").empty:
            data = new_row
        else:
            data = pd.concat([existing.dropna(how="all"), new_row], ignore_index=True)
        try:
            conn.update(spreadsheet=SPREADSHEET_URL, worksheet=LEDGER_TAB, data=data)
        except Exception as e_update:
            try:
                conn.create(spreadsheet=SPREADSHEET_URL, worksheet=LEDGER_TAB, data=data)
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

    st.session_state.ledger_error = append_to_ledger({
        "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "student": name,
        "level": level,
        "task": task,
        "transcription": user_input,
        "score": score,
        "feedback": (feedback or "")[:250],
    })


def load_ledger_df():
    try:
        conn = get_gsheets_conn()
        df = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=LEDGER_TAB, ttl=0)
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
# 4. ROSTER DESDE GOOGLE SHEETS (pestaña: speech)
# ==============================================================================
def _normalize_roster(df: pd.DataFrame) -> dict:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    name_c = next((c for c in df.columns if any(k in c for k in ["student", "name", "nombre"])), None)
    pin_c = next((c for c in df.columns if c != name_c and any(k in c for k in ["pin", "pass", "clave", "code"])), None)
    if name_c is None or pin_c is None:
        raise ValueError(f"No encontré columnas 'student' y 'pin'. Columnas leídas: {list(df.columns)}")
    df = df.dropna(subset=[name_c, pin_c])
    names = df[name_c].astype(str).str.strip()
    pins = df[pin_c].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    roster = {n: p for n, p in zip(names, pins) if n and n.lower() != "nan" and p and p.lower() != "nan"}
    if not roster:
        raise ValueError("La pestaña 'speech' está vacía o sin registros válidos.")
    return roster


def _public_csv_url(sheet_url: str, tab: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError("No se pudo extraer el ID de la URL.")
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"


@st.cache_data(ttl=120, show_spinner=False)
def load_roster(sheet_url: str):
    errors = []
    try:
        conn = get_gsheets_conn()
        df = conn.read(spreadsheet=sheet_url, worksheet=ROSTER_TAB, ttl=0)
        return _normalize_roster(df), "Google Sheets (conexión)"
    except Exception as e:
        errors.append(f"conexión: {e}")
    try:
        df = pd.read_csv(_public_csv_url(sheet_url, ROSTER_TAB), dtype=str)
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
    st.caption("ENGL 107 - English Speech Tutor (A1/A2)")

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
        st.error(f"❌ Error loading 'speech' roster:\n{roster_error}")

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
            st.write(
                "**Sheets Writing:** "
                + ("✅ Service Account connected" if sheets_write_enabled() else "⚠️ Local SQLite only")
            )
            _ok, _reason = sheets_write_status()
            if not _ok:
                st.error(f"Why writing is disabled: {_reason}")
            try:
                st.caption(f"Top-level secret names found: {list(st.secrets.keys())}")
            except Exception as _e:
                st.caption(f"Could not read secrets at all: {type(_e).__name__}")
            if st.session_state.ledger_error:
                st.warning(f"Last Sheets write warning: {st.session_state.ledger_error}")
            if st.button("🧪 Write a test row to the ledger"):
                err = append_to_ledger({
                    "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "student": "TEST_STUDENT",
                    "level": "A1",
                    "task": "Test verification",
                    "transcription": "Testing spreadsheet write connection",
                    "score": "100",
                    "feedback": "Connection verified successfully",
                })
                if err is None:
                    st.success("✅ Fila escrita correctamente.")
                else:
                    st.error(err)

        with st.expander("📊 Student Progress Ledger", expanded=True):
            if st.button("🔄 Refresh Analytics"):
                st.rerun()
            df_diag, ledger_source = load_ledger_df()
            st.caption(f"Data source: {ledger_source}")
            if not df_diag.empty:
                students = ["All"] + sorted(df_diag["student"].dropna().astype(str).unique())
                chosen = st.selectbox("Filter by student:", students)
                df_show = df_diag if chosen == "All" else df_diag[df_diag["student"] == chosen]
                if "score" in df_show.columns:
                    summary = df_show.groupby(["student", "score"]).size().unstack(fill_value=0)
                    st.dataframe(summary, use_container_width=True)
                st.dataframe(df_show, use_container_width=True)
                st.download_button(
                    "📥 Download CSV", df_show.to_csv(index=False),
                    "english_student_diagnostics.csv", "text/csv",
                )
            else:
                st.info("No records found in speech_logs yet.")

# ==============================================================================
# 7. CLIENTE GEMINI Y CONTROL DE ACCESO
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


def gen(contents, json_mode=False, temperature=0.2, retries=3):
    cfg_kwargs = {"temperature": temperature}
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"
    cfg = types.GenerateContentConfig(**cfg_kwargs)
    last_err = None
    for attempt in range(retries):
        try:
            return client.models.generate_content(model=MODEL_NAME, contents=contents, config=cfg)
        except Exception as e:
            last_err = e
            if any(k in str(e) for k in ("503", "UNAVAILABLE", "429")) and attempt < retries - 1:
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
def render_chat(messages):
    last_idx = max([i for i, m in enumerate(messages) if m["role"] == "assistant"], default=-1)
    for idx, msg in enumerate(messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if idx == last_idx and msg["role"] == "assistant" and msg.get("tts", True):
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


def generate_story_and_questions(topic: str):
    prompt = f"""
    You are an expert English as a Second Language (ESL) instructor.
    Design a short reading passage and 5 comprehension questions for adult A1-A2 learners.
    Topic: {topic}

    REQUIREMENTS:
    1. PASSAGE: 90-110 words in clear, natural English.
       Use Present Simple, high-frequency verbs, and daily vocabulary.
       Bold key conjugated verbs and core target vocabulary with **double asterisks**.
    2. 5 COMPREHENSION QUESTIONS IN ENGLISH: progressive difficulty
       (Q1-Q2 direct recall, Q3-Q4 specific details, Q5 basic inference).
       - expected_core: short target answer in English.
       - context_hint: the exact sentence from the passage where the clue is found.

    Return strictly a valid JSON object:
    {{
      "story": "Full English passage with **bolded verbs**...",
      "questions": [
        {{"q_num": 1, "question": "Where does Sarah live?", "expected_core": "in Boston", "context_hint": "Sarah **lives** in a quiet apartment in Boston."}},
        {{"q_num": 2, "question": "...", "expected_core": "...", "context_hint": "..."}},
        {{"q_num": 3, "question": "...", "expected_core": "...", "context_hint": "..."}},
        {{"q_num": 4, "question": "...", "expected_core": "...", "context_hint": "..."}},
        {{"q_num": 5, "question": "...", "expected_core": "...", "context_hint": "..."}}
      ]
    }}
    """
    resp = gen([types.Part.from_text(text=prompt)], json_mode=True, temperature=0.2)
    package = parse_json(resp.text)
    if not package.get("story") or not package.get("questions"):
        raise ValueError("Model response missed passage or questions.")
    return package


def start_comprehension(first_question: str, total: int):
    st.session_state.current_q_idx = 0
    st.session_state.correct_answers_count = 0
    st.session_state.attempts = 0
    st.session_state.phase = "comprehension"
    st.session_state.chat_messages = [{
        "role": "assistant",
        "content": f"**Question 1 of {total}:** {first_question}",
        "auto_play": True,
    }]


def advance_question(prefix: str):
    s = st.session_state
    s.current_q_idx += 1
    s.attempts = 0
    total = len(s.questions_list)
    if s.current_q_idx < total:
        nxt = s.questions_list[s.current_q_idx]["question"]
        msg = f"{prefix}\n\n---\n**Question {s.current_q_idx + 1} of {total}:** {nxt}"
    elif s.correct_answers_count >= max(total - 1, 1):
        s.phase = "personal_production"
        s.personal_turn = 0
        s.celebrated = False
        msg = (
            f"{prefix}\n\n🎉 **Comprehension check complete! Score: {s.correct_answers_count}/{total}.**\n\n"
            "Now let's practice speaking about your own life in English!"
        )
    else:
        s.phase = "review"
        msg = f"{prefix}\n\n📊 **Final Score: {s.correct_answers_count}/{total}.**"
    s.chat_messages.append({"role": "assistant", "content": msg, "auto_play": True})


# ==============================================================================
# 9. MODO 1: MISIONES GUIADAS
# ==============================================================================
if "Guided" in st.session_state.app_mode:

    # ---------------- Fase 0: Onboarding ----------------
    if st.session_state.phase == "onboarding":
        welcome_en = f"Hello {student_first_name}! Welcome to your English speech session."
        instructions = (
            "Today we will read an English story, listen to natural pronunciation, "
            "and answer 5 questions speaking or writing in English.\n\n"
            "💡 **Language Rule:** I teach and interact entirely in English. "
            "However, if you feel lost or ask a question in Spanish, I will assist and explain in Spanish!"
        )
        st.markdown(f"### 👋 {welcome_en}")
        st.markdown(instructions)

        audio_intro = generate_english_audio_bytes(
            welcome_en + " Let's practice English together step by step.", slow=st.session_state.tts_slow
        )
        if audio_intro:
            st.audio(audio_intro, format="audio/mp3")

        st.markdown("---")
        st.markdown("#### Choose a conversation topic:")

        col1, col2 = st.columns(2)
        chosen_topic = None
        with col1:
            if st.button("☕ Morning Routine & Coffee Shop", use_container_width=True):
                chosen_topic = "Daily morning routine, ordering coffee, and breakfast"
            if st.button("🎒 University Life & College Classes", use_container_width=True):
                chosen_topic = "A university student walking through campus, attending classes, and meeting friends"
        with col2:
            if st.button("🛒 Farmers Market & Groceries", use_container_width=True):
                chosen_topic = "Buying fresh food and fruit at the local market using numbers and polite requests"
            if st.button("✈️ Travel & Hotel Check-in", use_container_width=True):
                chosen_topic = "Arriving at a new city, checking in at a hotel, and asking for directions"

        if chosen_topic:
            try:
                with st.spinner("Creating your interactive reading..."):
                    package = generate_story_and_questions(chosen_topic)
                st.session_state.current_story = package["story"]
                st.session_state.questions_list = package["questions"]
                start_comprehension(package["questions"][0]["question"], len(package["questions"]))
                st.rerun()
            except Exception as e:
                st.error(f"{FRIENDLY_ERROR}\n\nDetail: {e}")

    # ---------------- Fase 1: Comprensión ----------------
    elif st.session_state.phase == "comprehension":
        total_q = len(st.session_state.questions_list)
        q_idx = st.session_state.current_q_idx

        with st.expander("📖 English Reading Passage (Focus on bold words)", expanded=True):
            st.markdown(st.session_state.current_story)
            story_audio = generate_english_audio_bytes(
                st.session_state.current_story, slow=st.session_state.tts_slow
            )
            if story_audio:
                st.audio(story_audio, format="audio/mp3")

        st.progress(min(q_idx / total_q, 1.0), text=f"Comprehension: Question {min(q_idx + 1, total_q)} of {total_q}")

        render_chat(st.session_state.chat_messages)
        st.markdown("---")
        st.caption("💡 **Tip:** Answer speaking or writing in English. (Si necesitas ayuda, puedes preguntar en español).")

        incoming_text, incoming_audio = collect_input(
            f"rec_mic_q_{q_idx}",
            "🎙️ Speak your answer in English:",
            "Type your answer in English (or ask for help in Spanish)...",
        )

        if incoming_text:
            st.session_state.chat_messages.append({"role": "user", "content": incoming_text})
            q = st.session_state.questions_list[q_idx]

            eval_prompt = f"""
            You are Coach Edgarvich, an expert English language coach guiding an adult beginner/intermediate learner.

            PEDAGOGICAL & LANGUAGE POLICY:
            - Default language: ENGLISH. All standard feedback, praise, recast, and models MUST be in English.
            - Exception: If the student asks a question or makes an inquiry in SPANISH, respond to that inquiry in SPANISH to provide comprehensible scaffolding, then guide them back to answering in English.

            READING CONTEXT:
            "{st.session_state.current_story}"

            QUESTION IN PROGRESS:
            Question: "{q['question']}"
            Target/Expected Answer: "{q['expected_core']}"
            Clue from Text: "{q['context_hint']}"

            STUDENT UTTERANCE: "{incoming_text}"
            (If an audio clip is attached, listen to the student's pronunciation and spoken content).

            TASK:
            1. CLASSIFY INTENT:
               - "INQUIRY": Student asks a question, requests translation, or asks grammar/vocab clarification.
               - "ANSWER": Student attempts to answer the comprehension question.
            2. FEEDBACK GENERATION:
               - If INQUIRY: If asked in Spanish, explain kindly in Spanish. If asked in English, explain in English. Provide 1 practical example, then invite them back to the question.
               - If ANSWER:
                 * is_correct = true if the meaning is accurate, even with slight pronunciation, preposition, or verb ending slips.
                 * Provide enthusiastic English praise + a natural recast of their answer.
                 * If incorrect: Provide a helpful English hint pointing to the story sentence, plus two scaffolded sentence starters:
                   Model A: ...
                   Model B: ...

            Return strictly a JSON object:
            {{
              "intent": "INQUIRY" or "ANSWER",
              "is_correct": true or false,
              "feedback_text": "...",
              "model_a": "Model A: ...",
              "model_b": "Model B: ..."
            }}
            """
            parts = [types.Part.from_text(text=eval_prompt)]
            if incoming_audio:
                parts.insert(0, types.Part.from_bytes(data=incoming_audio, mime_type="audio/wav"))

            try:
                with st.spinner("Coach Edgarvich is listening..."):
                    parsed = parse_json(gen(parts, json_mode=True, temperature=0.1).text)
            except Exception as e:
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": f"{FRIENDLY_ERROR}\n\n`{e}`", "tts": False}
                )
                st.rerun()

            intent = parsed.get("intent", "ANSWER")
            fb_text = parsed.get("feedback_text", "")
            is_correct = bool(parsed.get("is_correct", False))
            m_a, m_b = parsed.get("model_a", ""), parsed.get("model_b", "")

            if intent == "INQUIRY":
                st.session_state.chat_messages.append({
                    "role": "assistant", "tts": False, "auto_play": False,
                    "content": (
                        f"💡 **Language Support Note:**\n{fb_text}\n\n"
                        f"---\n👉 **Back to Question {q_idx + 1}:**\n{q['question']}"
                    ),
                })
                log_diagnostic(student_name, "A1", f"Question {q_idx+1} Inquiry", incoming_text, fb_text, "CLARIFICATION")

            elif is_correct:
                points = 10 if st.session_state.attempts == 0 else 5
                st.session_state.xp += points
                st.session_state.streak += 1
                st.session_state.correct_answers_count += 1
                log_diagnostic(student_name, "A1", f"Question {q_idx+1}", incoming_text, fb_text, "CORRECT")
                advance_question(f"{fb_text}\n\n⭐ **+{points} XP**")

            else:
                st.session_state.attempts += 1
                st.session_state.streak = 0
                log_diagnostic(student_name, "A1", f"Question {q_idx+1}", incoming_text, fb_text, "INCORRECT")
                if st.session_state.attempts >= MAX_ATTEMPTS:
                    reveal = (
                        f"{fb_text}\n\n✅ **Expected Answer:** {q['expected_core']}\n"
                        f"📖 *Context:* {q['context_hint']}\n\n_Don't worry! Let's move to the next one._"
                    )
                    log_diagnostic(student_name, "A1", f"Question {q_idx+1}", incoming_text, "Answer revealed", "REVEALED")
                    advance_question(reveal)
                else:
                    st.session_state.chat_messages.append({
                        "role": "assistant", "tts": False, "auto_play": False,
                        "content": (
                            f"🔎 **Coach Hint:** {fb_text}\n\n👉 **{m_a}**\n👉 **{m_b}**\n\n"
                            f"_Attempt {st.session_state.attempts} of {MAX_ATTEMPTS} — give it another try!_"
                        ),
                    })
            st.rerun()

    # ---------------- Fase 1b: Repaso ----------------
    elif st.session_state.phase == "review":
        with st.expander("📖 English Reading Passage", expanded=False):
            st.markdown(st.session_state.current_story)
        render_chat(st.session_state.chat_messages)
        st.markdown("---")
        st.info("Good effort! Would you like to practice this passage again or try a new one?")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔁 Repeat This Story", use_container_width=True):
                total = len(st.session_state.questions_list)
                start_comprehension(st.session_state.questions_list[0]["question"], total)
                st.rerun()
        with col_b:
            if st.button("📚 New Story", use_container_width=True):
                reset_mission()
                st.rerun()

    # ---------------- Fase 2: Producción Personal ----------------
    elif st.session_state.phase == "personal_production":
        with st.expander("📖 Story Reference", expanded=False):
            st.markdown(st.session_state.current_story)

        if st.session_state.personal_turn == 0:
            try:
                with st.spinner("Preparing personal speaking topic..."):
                    personal_prompt = f"""
                    Based on this English passage:
                    "{st.session_state.current_story}"

                    Welcome {student_first_name} to personal conversation practice in ENGLISH.
                    Ask ONE open-ended personal question in ENGLISH connected to their daily life or preferences.
                    Then provide two sentence starter models in ENGLISH:
                    👉 Model A: "I usually..."
                    👉 Model B: "In my opinion, I prefer..."
                    """
                    first_q = gen([types.Part.from_text(text=personal_prompt)], temperature=0.4)
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": first_q.text, "auto_play": True}
                )
                st.session_state.personal_turn = 1
                st.rerun()
            except Exception as e:
                st.error(f"{FRIENDLY_ERROR}\n\nDetail: {e}")
                st.stop()

        turn = st.session_state.personal_turn
        finished = turn > 3

        st.markdown(f"### 💬 Personal English Conversation (Turn {min(turn, 3)} of 3)")
        render_chat(st.session_state.chat_messages)

        if finished:
            if not st.session_state.celebrated:
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
                (m["content"] for m in reversed(st.session_state.chat_messages) if m["role"] == "assistant"), ""
            )

            p_prompt = f"""
            You are Coach Edgarvich, guiding {student_first_name} in conversational English.
            Previous coach message: "{last_q}"
            Student utterance: "{p_text}"
            Turn {turn} of 3.

            RULES:
            1. If student asks a question in SPANISH: explain in SPANISH, then re-prompt in English.
            2. If student answers in ENGLISH:
               - Brief warm praise in English.
               - Gentle recast if there are grammar, word order, or preposition errors.
               - If turn < 3: ONE natural follow-up question in English + Model A and Model B starters.
               - If turn == 3: Warm celebratory feedback and wrap-up (no new questions).

            Return strictly JSON: {{"intent": "INQUIRY" or "ANSWER", "message": "..."}}
            """
            parts = [types.Part.from_text(text=p_prompt)]
            if p_audio:
                parts.insert(0, types.Part.from_bytes(data=p_audio, mime_type="audio/wav"))

            try:
                with st.spinner("Coach Edgarvich is thinking..."):
                    parsed = parse_json(gen(parts, json_mode=True, temperature=0.3).text)
            except Exception as e:
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": f"{FRIENDLY_ERROR}\n\n`{e}`", "tts": False}
                )
                st.rerun()

            intent = parsed.get("intent", "ANSWER")
            message = parsed.get("message", "")

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
                log_diagnostic(student_name, "A1", f"Personal Turn {turn}", p_text, message, "PRODUCED")
                st.session_state.personal_turn += 1
            st.rerun()

# ==============================================================================
# 10. MODO 2: READING & SPEECH LAB (PDF / TXT)
# ==============================================================================
else:
    st.subheader("📂 Reading & Speech Analysis Lab")
    st.caption("Upload articles, reading assignments, or handouts. Practice English reading comprehension and speech!")

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
            st.session_state.pdf_chat_messages = []
        except Exception as e:
            st.error(f"Error reading file: {e}")

    if st.session_state.pdf_text_content:
        words = len(st.session_state.pdf_text_content.split())
        st.success(f"✅ Loaded: {st.session_state.pdf_name} ({words} words)")

        with st.expander("📄 Document Preview", expanded=False):
            preview = st.session_state.pdf_text_content
            st.markdown(preview[:3000] + ("..." if len(preview) > 3000 else ""))

        for msg in st.session_state.pdf_chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        pdf_query = st.chat_input("Ask a question about the text, ask for word meanings, or type 'Quiz me'...")
        if pdf_query:
            st.session_state.pdf_chat_messages.append({"role": "user", "content": pdf_query})
            history = "\n".join(
                f"{m['role'].upper()}: {m['content'][:400]}" for m in st.session_state.pdf_chat_messages[-7:-1]
            )
            pdf_prompt = f"""
            You are Coach Edgarvich, English Language Coach.
            Student Name: {student_first_name}

            DOCUMENT CONTEXT:
            \"\"\"{st.session_state.pdf_text_content[:8000]}\"\"\"

            RECENT DIALOGUE:
            {history}

            STUDENT REQUEST: "{pdf_query}"

            INSTRUCTIONS:
            - Respond in clear, accessible ENGLISH by default.
            - If the student asks specifically in Spanish or asks for Spanish translations, provide the explanation in Spanish along with clear English examples.
            - If they write 'Quiz me', generate 3 comprehension questions in English based on the text.
            - If they ask for a summary, provide 3 key bullet points in English.
            """
            try:
                with st.spinner("Analyzing document..."):
                    reply = gen([types.Part.from_text(text=pdf_prompt)], temperature=0.3).text
            except Exception as e:
                reply = f"{FRIENDLY_ERROR}\n\n`{e}`"
            st.session_state.pdf_chat_messages.append({"role": "assistant", "content": reply})
            st.rerun()
    else:
        st.info("👆 Upload a PDF or TXT file above to activate the Reading & Speech assistant.")
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
    page_title="🧠 Edgarvich Progressive SLA Tutor",
    layout="wide",
    page_icon="🎓",
)

# Archivo en Drive: "speech English107_database"
# Columnas del roster: student_name | student_pin
# El archivo se identifica por su URL/ID (no por su nombre). Define la URL en los Secrets:
#   [connections.gsheets]
#   spreadsheet = "https://docs.google.com/spreadsheets/d/TU_ID/edit"
ROSTER_TAB = "roster"  # si no existe una pestaña con este nombre, se usa la primera pestaña
LEDGER_TAB = "diagnostics_ledger"
LEDGER_COLUMNS = [
    "student_name", "cefr_level", "phase", "student_input",
    "ai_feedback", "score_status", "timestamp",
]
MAX_ATTEMPTS = 2  # intentos por pregunta antes de mostrar la respuesta
LEVELS = ["A1", "A2", "B1", "B2"]


def _secret(name, default=None):
    """Lee de st.secrets o de variables de entorno, sin romper si no existen."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def get_spreadsheet_url() -> str:
    try:
        return st.secrets["connections"]["gsheets"]["spreadsheet"]
    except Exception:
        return _secret("SPREADSHEET_URL", "") or ""


SPREADSHEET_URL = get_spreadsheet_url()
MODEL_NAME = _secret("MODEL_NAME", "gemini-3.8-flash")
INSTRUCTOR_KEY = _secret("INSTRUCTOR_KEY")

# ==============================================================================
# 2. ESTADO DE SESIÓN
# ==============================================================================
DEFAULTS = {
    "app_mode": "🎯 Guided Missions",
    "cefr_level": "A1",
    "phase": "onboarding",  # onboarding, comprehension, review, personal_production
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
        s.cefr_level = "A1"
        s.xp = 0
        s.streak = 0
        s.pdf_chat_messages = []
        s.pdf_text_content = ""
        s.pdf_name = ""


# ==============================================================================
# 3. PERSISTENCIA: SQLITE (local) + GOOGLE SHEETS (remoto)
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


def sheets_write_enabled() -> bool:
    """Escribir en Google Sheets requiere una cuenta de servicio en los secretos."""
    try:
        return str(st.secrets["connections"]["gsheets"].get("type", "")) == "service_account"
    except Exception:
        return False


def append_to_ledger(row: dict):
    """Agrega una fila a la pestaña diagnostics_ledger. Devuelve None si OK o un texto de error."""
    if not SPREADSHEET_URL:
        return "Falta la URL de la hoja de Google Sheets."
    if not sheets_write_enabled():
        return "Escritura en Sheets desactivada: falta la cuenta de servicio en los secretos."
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
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
        except Exception:
            # Si la pestaña no existe, se crea
            conn.create(spreadsheet=SPREADSHEET_URL, worksheet=LEDGER_TAB, data=data)
        return None
    except Exception as e:
        return str(e)


def log_diagnostic(name, level, phase, s_input, feedback, score):
    # 1) SQLite local (siempre)
    try:
        conn = get_db_connection()
        with conn:
            conn.execute(
                """INSERT INTO diagnostics
                   (student_name, cefr_level, phase, student_input, ai_feedback, score_status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, level, phase, s_input, feedback, score),
            )
        conn.close()
    except Exception as e:
        print(f"Error SQLite: {e}")

    # 2) Google Sheets (mejor esfuerzo; el error queda guardado para el panel del profesor)
    st.session_state.ledger_error = append_to_ledger({
        "student_name": name,
        "cefr_level": level,
        "phase": phase,
        "student_input": s_input,
        "ai_feedback": (feedback or "")[:250],
        "score_status": score,
        "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def load_ledger_df():
    """Lee el registro desde Sheets; si no se puede, usa SQLite."""
    if SPREADSHEET_URL:
        try:
            conn = st.connection("gsheets", type=GSheetsConnection)
            df = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=LEDGER_TAB, ttl=0)
            if df is not None and not df.dropna(how="all").empty:
                return df.dropna(how="all"), "Google Sheets"
        except Exception:
            pass
    conn = get_db_connection()
    df = pd.read_sql_query(
        "SELECT student_name, cefr_level, phase, score_status, student_input, "
        "ai_feedback, timestamp FROM diagnostics ORDER BY timestamp DESC",
        conn,
    )
    conn.close()
    return df, "SQLite local"


# ==============================================================================
# 4. ROSTER DESDE GOOGLE SHEETS (columnas: student_name | student_pin)
# ==============================================================================
def _normalize_roster(df: pd.DataFrame) -> dict:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    name_c = next((c for c in df.columns if any(k in c for k in ["name", "nombre", "student"])), None)
    pin_c = next((c for c in df.columns if c != name_c and any(k in c for k in ["pin", "pass", "clave", "code"])), None)
    if name_c is None or pin_c is None:
        raise ValueError(
            f"No encontré las columnas de nombre y PIN (esperadas: 'student_name' y 'student_pin'). "
            f"Columnas leídas: {list(df.columns)}"
        )
    df = df.dropna(subset=[name_c, pin_c])
    names = df[name_c].astype(str).str.strip()
    # Un PIN numérico puede llegar como "6421.0": se normaliza
    pins = df[pin_c].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    roster = {n: p for n, p in zip(names, pins) if n and n.lower() != "nan" and p and p.lower() != "nan"}
    if not roster:
        raise ValueError("La hoja se leyó, pero no contiene estudiantes.")
    return roster


def _public_csv_url(sheet_url: str, tab=None) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError("No pude extraer el ID de la hoja desde la URL.")
    url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/gviz/tq?tqx=out:csv"
    if tab:
        url += f"&sheet={urllib.parse.quote(tab)}"
    return url  # sin 'sheet' devuelve la primera pestaña


@st.cache_data(ttl=120, show_spinner=False)
def load_roster(sheet_url: str):
    """Devuelve (roster, fuente). Si falla todo, lanza excepción (así el error no se guarda en caché)."""
    if not sheet_url:
        raise ValueError("Falta la URL de la hoja de Google Sheets (secret 'spreadsheet').")
    errors = []

    # Intento 1: conexión oficial. Primero la pestaña 'roster'; si no existe, la primera pestaña.
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        last = None
        for ws in (ROSTER_TAB, None):
            try:
                if ws:
                    df = conn.read(spreadsheet=sheet_url, worksheet=ws, ttl=0)
                else:
                    df = conn.read(spreadsheet=sheet_url, ttl=0)
                return _normalize_roster(df), "Google Sheets (conexión)"
            except Exception as e:
                last = e
        raise last
    except Exception as e:
        errors.append(f"conexión: {e}")

    # Intento 2: CSV público (requiere "Cualquier persona con el enlace" como lector)
    for tab in (ROSTER_TAB, None):
        try:
            df = pd.read_csv(_public_csv_url(sheet_url, tab), dtype=str)
            return _normalize_roster(df), "Google Sheets (CSV público)"
        except Exception as e:
            errors.append(f"CSV público ({tab or 'primera pestaña'}): {e}")

    raise RuntimeError(" | ".join(errors))


# ==============================================================================
# 5. SÍNTESIS DE AUDIO (gTTS)
# ==============================================================================
def clean_for_tts(text: str) -> str:
    text = re.sub(r"👉?\s*Model\s*[A-B]:?", "", text, flags=re.IGNORECASE)
    # Quita emojis y símbolos; conserva letras, números y puntuación básica
    text = re.sub(r"[^\w\s?!.,;:'’]", " ", text)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


@st.cache_data(show_spinner=False)
def generate_audio_bytes(text, slow=False):
    cleaned = clean_for_tts(text)
    if not cleaned:
        return None
    try:
        tts = gTTS(text=cleaned, lang="en", tld="com", slow=slow)
        fp = BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception:
        return None


# ==============================================================================
# 6. BARRA LATERAL (AUTENTICACIÓN, MODO DE USO & PANEL DEL PROFESOR)
# ==============================================================================
with st.sidebar:
    st.title("🧠 Edgarvich SLA Engine")

    api_key = _secret("GEMINI_API_KEY")
    if not api_key:
        api_key = st.text_input("🔑 Gemini API Key:", type="password")

    if not SPREADSHEET_URL:
        SPREADSHEET_URL = st.text_input(
            "📄 URL de la hoja de Google Sheets:",
            help="Mejor defínela en Secrets: [connections.gsheets] spreadsheet = \"...\"",
        ).strip()

    st.divider()
    try:
        roster, roster_source = load_roster(SPREADSHEET_URL)
        roster_error = None
    except Exception as e:
        roster, roster_source, roster_error = {}, None, str(e)

    if roster_error:
        st.error(
            "❌ No pude leer la lista de estudiantes en Google Sheets.\n\n"
            f"Detalle: {roster_error}"
        )
    if st.button("🔄 Reload student list"):
        load_roster.clear()
        st.rerun()

    names = ["-- Select your name --"] + sorted(roster.keys())
    student_name = st.selectbox("Select Your Name:", names)
    entered_pin = st.text_input("PIN:", type="password")

    authenticated = False
    if student_name != "-- Select your name --" and entered_pin:
        expected = str(roster.get(student_name, "")).strip()
        if expected and hmac.compare_digest(expected, entered_pin.strip()):
            authenticated = True
            st.success(f"Verified: {student_name}")
        else:
            st.error("PIN incorrecto.")

    st.divider()
    st.session_state.app_mode = st.radio(
        "Navigation Mode:",
        ["🎯 Guided Missions", "📂 PDF Reading Lab"],
        index=0 if st.session_state.app_mode == "🎯 Guided Missions" else 1,
    )

    if authenticated:
        c1, c2 = st.columns(2)
        c1.metric("⭐ XP", st.session_state.xp)
        c2.metric("🔥 Streak", st.session_state.streak)

    if st.session_state.app_mode == "🎯 Guided Missions":
        st.markdown(f"**Current CEFR Stage:** `{st.session_state.cefr_level}`")
        st.markdown(f"**Active Phase:** `{st.session_state.phase.replace('_', ' ').title()}`")
        if st.button("🔄 Reset Current Mission"):
            reset_mission()
            st.rerun()

    st.session_state.tts_slow = st.toggle("🐢 Slow Audio Speed", value=st.session_state.tts_slow)

    # ---------------- Panel del profesor ----------------
    st.divider()
    instructor_input = st.text_input("Instructor Access:", type="password")
    if not INSTRUCTOR_KEY:
        st.caption("ℹ️ Define INSTRUCTOR_KEY en los Secrets para activar el panel del profesor.")
    elif instructor_input and hmac.compare_digest(instructor_input, str(INSTRUCTOR_KEY)):
        st.subheader("👨‍🏫 Instructor Control Panel")

        with st.expander("🔌 Google Sheets connection status", expanded=True):
            st.write(f"**Hoja (URL):** {SPREADSHEET_URL or '❌ no configurada'}")
            st.write(f"**Lectura del roster:** {roster_source or '❌ fallida'} ({len(roster)} estudiantes)")
            st.write(
                "**Escritura en el registro:** "
                + ("✅ cuenta de servicio configurada" if sheets_write_enabled() else "⚠️ desactivada (solo SQLite local)")
            )
            if st.session_state.ledger_error:
                st.warning(f"Último error al escribir en Sheets: {st.session_state.ledger_error}")
            if st.button("🧪 Write a test row to the ledger"):
                err = append_to_ledger({
                    "student_name": "TEST", "cefr_level": "A1", "phase": "test",
                    "student_input": "test", "ai_feedback": "test", "score_status": "TEST",
                    "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                if err is None:
                    st.success("✅ Fila escrita correctamente.")
                else:
                    st.error(err)

        with st.expander("📊 Student Progress Ledger", expanded=True):
            if st.button("🔄 Refresh Analytics"):
                st.rerun()
            df_diag, ledger_source = load_ledger_df()
            st.caption(f"Fuente de datos: {ledger_source}")
            if not df_diag.empty:
                students = ["All"] + sorted(df_diag["student_name"].dropna().astype(str).unique())
                chosen = st.selectbox("Filter by student:", students)
                df_show = df_diag if chosen == "All" else df_diag[df_diag["student_name"] == chosen]
                if "score_status" in df_show.columns:
                    summary = df_show.groupby(["student_name", "score_status"]).size().unstack(fill_value=0)
                    st.dataframe(summary, use_container_width=True)
                st.dataframe(df_show, use_container_width=True)
                st.download_button(
                    "📥 Download Diagnostics CSV", df_show.to_csv(index=False),
                    "engl107_student_diagnostics.csv", "text/csv",
                )
            else:
                st.info("No diagnostic records logged yet.")

# ==============================================================================
# 7. PUERTA DE ACCESO + CLIENTE GEMINI
# ==============================================================================
if not api_key or not authenticated:
    st.info("👈 Ingresa tu Gemini API Key e inicia sesión en la barra lateral para comenzar.")
    st.stop()

# Si cambia el estudiante, se reinicia el progreso (evita mezclar sesiones)
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


FRIENDLY_ERROR = (
    "⚠️ There was a technical problem contacting the tutor. Please try again in a few seconds. "
    "(Hubo un problema técnico; intenta de nuevo.)"
)


# ==============================================================================
# 8. COMPONENTES REUTILIZABLES
# ==============================================================================
def render_chat(messages):
    last_idx = max([i for i, m in enumerate(messages) if m["role"] == "assistant"], default=-1)
    for idx, msg in enumerate(messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if idx == last_idx and msg["role"] == "assistant" and msg.get("tts", True):
                audio = generate_audio_bytes(msg["content"], slow=st.session_state.tts_slow)
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
            text = "🎙️ [Audio Submitted]"
    if typed:
        text, audio = typed, None
    return text, audio


def generate_story_and_questions(topic: str, level: str):
    specs = {
        "A1": "Word count: 100-120 words. Grammar focus: Present Simple & Daily Habits. Syntax: S-V-O simple structures.",
        "A2": "Word count: 130-150 words. Grammar focus: Simple Past (regular/irregular) & Connectors (then, because, after).",
        "B1": "Word count: 180-200 words. Grammar focus: Present Perfect vs Simple Past, Past Continuous, opinion adverbs.",
        "B2": "Word count: 230-260 words. Grammar focus: Conditionals, Passive Voice, Relative clauses, and idiomatic expressions.",
    }
    prompt = f"""
    You are an expert SLA curriculum designer.
    Generate a short narrative and 5 comprehension questions for an adult L2 English learner.
    Level: {level} ({specs.get(level, specs['A1'])})
    Topic: {topic}

    REQUIREMENTS:
    1. STORY (Textual Enhancement):
       - Apply deliberate textual enhancement by bolding target conjugated verbs and key vocabulary with **double asterisks**.
       - Keep it contextual, mature, and meaningful.
    2. 5 COMPREHENSION QUESTIONS:
       - Progressive difficulty: Q1-Q2 (Direct recall), Q3-Q4 (Detail identification), Q5 (Short inference).
       - context_hint: Provide the specific line from the text that points to the answer.
       - expected_core: The key factual answer.

    Return strictly a valid JSON object:
    {{
      "story": "The complete enhanced story with **bolded verbs**...",
      "questions": [
        {{"q_num": 1, "question": "Question text...", "expected_core": "answer core", "context_hint": "Quote or paragraph reference"}},
        {{"q_num": 2, "question": "Question text...", "expected_core": "answer core", "context_hint": "Quote or paragraph reference"}},
        {{"q_num": 3, "question": "Question text...", "expected_core": "answer core", "context_hint": "Quote or paragraph reference"}},
        {{"q_num": 4, "question": "Question text...", "expected_core": "answer core", "context_hint": "Quote or paragraph reference"}},
        {{"q_num": 5, "question": "Question text...", "expected_core": "answer core", "context_hint": "Quote or paragraph reference"}}
      ]
    }}
    """
    resp = gen([types.Part.from_text(text=prompt)], json_mode=True, temperature=0.2)
    package = parse_json(resp.text)
    if not package.get("story") or not package.get("questions"):
        raise ValueError("The model response did not include a story or questions.")
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
            f"{prefix}\n\n🎉 **Comprehension check complete! You scored {s.correct_answers_count}/{total}.**\n\n"
            "Connecting this story to your real life..."
        )
    else:
        s.phase = "review"
        msg = f"{prefix}\n\n📊 **Score: {s.correct_answers_count}/{total}.**"
    s.chat_messages.append({"role": "assistant", "content": msg, "auto_play": True})


# ==============================================================================
# 9. MODO 1: MISIÓN GUIADA
# ==============================================================================
if st.session_state.app_mode == "🎯 Guided Missions":
    level = st.session_state.cefr_level

    # ---------------- Fase 0: onboarding ----------------
    if st.session_state.phase == "onboarding":
        welcome_msg = (
            f"Hello {student_first_name}! Welcome back to your learning space. "
            f"Today we will read a short story together at your {level} level. "
            "First, you will listen and read. Then, we will work through 5 quick questions in our chat. "
            "If you ever have a doubt about a word or grammar, just ask me directly in the chat! "
            "Choose a story below to begin."
        )
        st.markdown(f"### 👋 Welcome, {student_first_name}!")
        st.markdown(welcome_msg)

        welcome_audio = generate_audio_bytes(welcome_msg, slow=st.session_state.tts_slow)
        if welcome_audio:
            st.audio(welcome_audio, format="audio/mp3")

        st.markdown("---")
        st.markdown("#### Choose your story for today:")

        col1, col2 = st.columns(2)
        chosen_topic = None
        with col1:
            if st.button("🍳 The Kitchen Mystery (Food, Routines & Actions)", use_container_width=True):
                chosen_topic = "Cooking at home and morning food choices"
            if st.button("✈️ The Unexpected Journey (Travel & Places)", use_container_width=True):
                chosen_topic = "A business trip with flight delays"
        with col2:
            if st.button("💼 A Busy Day at Work (Projects & Deadlines)", use_container_width=True):
                chosen_topic = "Managing tasks and unexpected phone calls"
            if st.button("🌆 City Explorers (Weekend Adventures)", use_container_width=True):
                chosen_topic = "Visiting a new neighborhood and finding a cafe"

        if chosen_topic:
            try:
                with st.spinner("Preparing your enhanced reading and tasks..."):
                    package = generate_story_and_questions(chosen_topic, level)
                st.session_state.current_story = package["story"]
                st.session_state.questions_list = package["questions"]
                start_comprehension(package["questions"][0]["question"], len(package["questions"]))
                st.rerun()
            except Exception as e:
                st.error(f"{FRIENDLY_ERROR}\n\nDetail: {e}")

    # ---------------- Fase 1: comprensión ----------------
    elif st.session_state.phase == "comprehension":
        total_q = len(st.session_state.questions_list)
        q_idx = st.session_state.current_q_idx

        with st.expander("📖 Target Reading (Notice the bolded forms)", expanded=True):
            st.markdown(st.session_state.current_story)
            story_audio = generate_audio_bytes(st.session_state.current_story, slow=st.session_state.tts_slow)
            if story_audio:
                st.audio(story_audio, format="audio/mp3")

        st.progress(min(q_idx / total_q, 1.0), text=f"Comprehension Check: Question {min(q_idx + 1, total_q)} of {total_q}")

        render_chat(st.session_state.chat_messages)
        st.markdown("---")
        st.caption("💡 **Tip:** You can answer the question OR ask me any doubt about vocabulary or grammar in English or Spanish!")

        incoming_text, incoming_audio = collect_input(
            f"rec_mic_q_{q_idx}",
            "🎙️ Speak your answer or question:",
            "Type your answer or ask a question about the text...",
        )

        if incoming_text:
            st.session_state.chat_messages.append({"role": "user", "content": incoming_text})
            q = st.session_state.questions_list[q_idx]

            eval_prompt = f"""
            You are Edgarvich, an expert SLA communicative tutor.
            Learner Level: {level}

            CONTEXT STORY:
            "{st.session_state.current_story}"

            CURRENT ACTIVE TASK:
            Question: "{q['question']}"
            Expected Content: "{q['expected_core']}"
            Context Hint: "{q['context_hint']}"

            STUDENT INPUT: "{incoming_text}"
            (If an audio clip is attached, it is the student's spoken input: listen to it and evaluate what was said.)

            STEP 1: INTENT CLASSIFICATION:
            - Is the student ASKING a question, requesting clarification, vocabulary meaning, translation, or expressing confusion? -> intent = "INQUIRY"
            - Is the student TRYING TO ANSWER the comprehension question? -> intent = "ANSWER"

            STEP 2: ACTIONS:
            - IF intent == "INQUIRY":
                * Answer their doubt warmly and thoroughly (in clear English with brief Spanish support if helpful).
                * Do NOT count this as correct or incorrect.
                * Gently bring them back to the active question at the end.
            - IF intent == "ANSWER":
                * is_correct = true if the MEANING is right, even with minor grammar or spelling slips.
                * If correct: praise warmly and recast the full target sentence correctly.
                * If incorrect: give a SCAFFOLDING HINT pointing to the text (DO NOT GIVE THE ANSWER), and give Model A & Model B starters.

            Return strictly a valid JSON object:
            {{
              "intent": "INQUIRY" or "ANSWER",
              "is_correct": true or false,
              "feedback_text": "Your pedagogical response...",
              "model_a": "Model A starter (if ANSWER and incorrect)...",
              "model_b": "Model B starter (if ANSWER and incorrect)..."
            }}
            """
            parts = [types.Part.from_text(text=eval_prompt)]
            if incoming_audio:
                parts.insert(0, types.Part.from_bytes(data=incoming_audio, mime_type="audio/wav"))

            try:
                with st.spinner("Edgarvich is thinking..."):
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
                    "role": "assistant", "auto_play": True,
                    "content": (
                        f"💬 **Teacher Explanation:**\n{fb_text}\n\n"
                        f"---\n👉 **Let's continue with Question {q_idx + 1}:** {q['question']}"
                    ),
                })
                log_diagnostic(student_name, level, "comprehension_inquiry", incoming_text, fb_text, "CLARIFICATION")

            elif is_correct:
                points = 10 if st.session_state.attempts == 0 else 5
                st.session_state.xp += points
                st.session_state.streak += 1
                st.session_state.correct_answers_count += 1
                log_diagnostic(student_name, level, "comprehension", incoming_text, fb_text, "CORRECT")
                advance_question(f"{fb_text}\n\n⭐ **+{points} XP**")

            else:
                st.session_state.attempts += 1
                st.session_state.streak = 0
                log_diagnostic(student_name, level, "comprehension", incoming_text, fb_text, "INCORRECT")
                if st.session_state.attempts >= MAX_ATTEMPTS:
                    reveal = (
                        f"{fb_text}\n\n✅ **Answer:** {q['expected_core']}\n"
                        f"📖 {q['context_hint']}\n\n_No worries — let's move on!_"
                    )
                    log_diagnostic(student_name, level, "comprehension", incoming_text, "answer revealed", "REVEALED")
                    advance_question(reveal)
                else:
                    st.session_state.chat_messages.append({
                        "role": "assistant", "auto_play": True,
                        "content": (
                            f"💡 **Hint:** {fb_text}\n\n👉 **{m_a}**\n👉 **{m_b}**\n\n"
                            f"_Attempt {st.session_state.attempts} of {MAX_ATTEMPTS} — you can do it!_"
                        ),
                    })
            st.rerun()

    # ---------------- Fase 1b: repaso (puntaje bajo) ----------------
    elif st.session_state.phase == "review":
        with st.expander("📖 Target Reading", expanded=False):
            st.markdown(st.session_state.current_story)
        render_chat(st.session_state.chat_messages)
        st.markdown("---")
        st.info("Let's reinforce these structures. What would you like to do?")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔁 Repeat this story", use_container_width=True):
                total = len(st.session_state.questions_list)
                start_comprehension(st.session_state.questions_list[0]["question"], total)
                st.rerun()
        with col_b:
            if st.button("📚 Choose a new story", use_container_width=True):
                reset_mission()
                st.rerun()

    # ---------------- Fase 2: producción personal ----------------
    elif st.session_state.phase == "personal_production":
        with st.expander("📖 Story Reference", expanded=False):
            st.markdown(st.session_state.current_story)

        if st.session_state.personal_turn == 0:
            try:
                with st.spinner("Preparing your personalized mission question..."):
                    personal_prompt = f"""
                    Based on this story:
                    "{st.session_state.current_story}"
                    Welcome {student_first_name} to the personal production stage.
                    Ask ONE personal communicative question connecting the story's theme directly to their own daily routine or habits.
                    Level: {level}.
                    Provide two model starters:
                    👉 Model A: "..."
                    👉 Model B: "..."
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

        st.markdown(f"### 💬 Personal Practice — Stage {level} (Turn {min(turn, 3)} of 3)")
        render_chat(st.session_state.chat_messages)

        if finished:
            if not st.session_state.celebrated:
                st.balloons()
                st.session_state.celebrated = True
            st.success(f"🏆 MISSION COMPLETED! You mastered Stage {level}!")
            st.metric("⭐ Total XP", st.session_state.xp)
            c_idx = LEVELS.index(level) if level in LEVELS else 0
            if c_idx < len(LEVELS) - 1:
                next_lvl = LEVELS[c_idx + 1]
                if st.button(f"🚀 Advance to Level {next_lvl}"):
                    st.session_state.cefr_level = next_lvl
                    reset_mission()
                    st.rerun()
            else:
                st.success("🌟 Congratulations! You have completed all tiers up to B2!")
                if st.button("Start a new mission"):
                    reset_mission()
                    st.rerun()
            st.stop()

        st.markdown("---")
        p_text, p_audio = collect_input(
            f"rec_mic_p_{turn}",
            "🎙️ Speak your response:",
            "Type your response or ask a clarification question...",
        )

        if p_text:
            st.session_state.chat_messages.append({"role": "user", "content": p_text})
            last_q = next(
                (m["content"] for m in reversed(st.session_state.chat_messages) if m["role"] == "assistant"), ""
            )

            p_prompt = f"""
            You are Edgarvich, communicative ESL coach.
            Learner: {student_first_name} | CEFR Level: {level}
            Previous tutor message: "{last_q}"
            Student response: "{p_text}"
            (If an audio clip is attached, it is the student's spoken response: listen to it and evaluate it.)
            The student is answering turn {turn} of 3.

            Return strictly a JSON object: {{"intent": "INQUIRY" or "ANSWER", "message": "..."}}
            - INQUIRY (the student asks for clarification or a language question): message = concise clarification
              (English, with brief Spanish support if helpful), then repeat the invitation for the current turn.
            - ANSWER (the student speaks about their experience): message =
                * brief warm praise,
                * a reactive recast with the corrected sentence if there is any grammatical/morphological slip,
                * if turn < 3: ONE natural follow-up question plus "👉 Model A: ..." and "👉 Model B: ...";
                * if turn == 3: warm congratulations on completing the cycle, and NO new question.
            """
            parts = [types.Part.from_text(text=p_prompt)]
            if p_audio:
                parts.insert(0, types.Part.from_bytes(data=p_audio, mime_type="audio/wav"))

            try:
                with st.spinner("Edgarvich is thinking..."):
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
                    {"role": "assistant", "content": message, "auto_play": True}
                )
                log_diagnostic(student_name, level, "personal_inquiry", p_text, message, "CLARIFICATION")
            else:
                st.session_state.xp += 5
                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": f"{message}\n\n⭐ **+5 XP**", "auto_play": True}
                )
                log_diagnostic(student_name, level, "personal_production", p_text, message, "PRODUCED")
                st.session_state.personal_turn += 1
            st.rerun()

# ==============================================================================
# 10. MODO 2: LABORATORIO DE LECTURA DE PDFS
# ==============================================================================
else:
    st.subheader("📂 Interactive Document & PDF Reading Lab")
    st.caption("Upload any PDF, article, or class handout. Read the text, ask questions, explore vocabulary, or request a customized quiz!")

    uploaded_file = st.file_uploader("Upload your PDF or Text File:", type=["pdf", "txt"])

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
            st.error(f"Could not read the file: {e}")

    if st.session_state.pdf_text_content:
        words = len(st.session_state.pdf_text_content.split())
        st.success(f"✅ Loaded: {st.session_state.pdf_name} ({words} words)")

        with st.expander("📄 Document Content Preview", expanded=False):
            preview = st.session_state.pdf_text_content
            st.markdown(preview[:3000] + ("..." if len(preview) > 3000 else ""))

        for msg in st.session_state.pdf_chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        pdf_query = st.chat_input("Ask a question about this document, request vocabulary help, or ask: 'Quiz me'...")
        if pdf_query:
            st.session_state.pdf_chat_messages.append({"role": "user", "content": pdf_query})
            history = "\n".join(
                f"{m['role'].upper()}: {m['content'][:400]}" for m in st.session_state.pdf_chat_messages[-7:-1]
            )
            pdf_prompt = f"""
            You are Edgarvich, an ESL reading specialist and tutor.
            Learner: {student_first_name} | Target Level: {st.session_state.cefr_level}

            DOCUMENT CONTENT:
            \"\"\"{st.session_state.pdf_text_content[:8000]}\"\"\"

            RECENT CONVERSATION:
            {history}

            STUDENT REQUEST: "{pdf_query}"

            CAPABILITIES:
            1. Vocabulary: define words simply, give example sentences, and mention the Spanish equivalent if helpful.
            2. Summary: summarize in calibrated {st.session_state.cefr_level} English using bullet points.
            3. 'Quiz me' / 'Ask me questions': generate 3 comprehension questions from the document with Model A and Model B starters.
            4. Factual question: answer referencing the document directly.

            Tone: Supportive, communicative, concise.
            """
            try:
                with st.spinner("Thinking..."):
                    reply = gen([types.Part.from_text(text=pdf_prompt)], temperature=0.3).text
            except Exception as e:
                reply = f"{FRIENDLY_ERROR}\n\n`{e}`"
            st.session_state.pdf_chat_messages.append({"role": "assistant", "content": reply})
            st.rerun()
    else:
        st.info("👆 Please upload a PDF or TXT file above to activate the Reading Lab.")
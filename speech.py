import streamlit as st
import sqlite3
import os
import re
import time
import pandas as pd
import streamlit.components.v1 as components
import json
import threading
from streamlit_gsheets import GSheetsConnection
from google import genai
from google.genai import types
from pydantic import BaseModel

# ==============================================================================
# 1. CONFIGURACIÓN & RUTAS DE ALMACENAMIENTO
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "spanglish_adaptive_coach.db")
st.set_page_config(page_title="🗣️ Edgarvich says", layout="wide", page_icon="☕")

# ==============================================================================
# 2. BASE DE DATOS LOCAL (DIAGNÓSTICO Y REGISTRO EN SEGUNDO PLANO)
# ==============================================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_db_connection()
    with conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS diagnostics 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       student_name TEXT, 
                       student_input TEXT, 
                       ai_feedback TEXT, 
                       grammar_topic TEXT, 
                       student_strengths TEXT, 
                       student_errors TEXT, 
                       mastery_status TEXT, 
                       used_pdf TEXT, 
                       timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.close()

init_db()

def log_diagnostic_result(name, inp, feedback, topic, strengths, errors, mastery_status, used_pdf):
    try:
        conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("""INSERT INTO diagnostics 
                          (student_name, student_input, ai_feedback, grammar_topic, student_strengths, student_errors, mastery_status, used_pdf) 
                          VALUES (?,?,?,?,?,?,?,?)""",
                          (name, inp, feedback, topic, strengths, errors, mastery_status, used_pdf))
            inserted_id = cursor.lastrowid
        conn.close()
        return inserted_id
    except sqlite3.OperationalError:
        return None

def get_student_unmastered_weaknesses(name, limit=3):
    try:
        conn = get_db_connection()
        query = """
            SELECT grammar_topic, student_errors 
            FROM diagnostics 
            WHERE student_name = ? AND grammar_topic != 'Analyzing...' AND mastery_status != 'mastered'
            ORDER BY id DESC LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(name, limit))
        conn.close()
        if df.empty:
            return "No recorded persistent gaps. Focus on natural scaffolding and clear English expressions."
        
        weakness_lines = []
        for _, row in df.iterrows():
            weakness_lines.append(f"- Implicit Target: {row['grammar_topic']} | Slip to recast: {row['student_errors']}")
        return "\n".join(weakness_lines)
    except Exception:
        return "Learner profile currently unavailable."

class PedagogicalDiagnosis(BaseModel):
    grammar_topic: str
    student_strengths: str
    student_errors: str
    mastery_status: str

# ==============================================================================
# 3. GESTIÓN DE ROSTER Y LECTURA SEGURA DE GOOGLE SHEETS
# ==============================================================================
def get_authorized_students() -> dict:
    try:
        gsheet_conn = st.connection("gsheets", type=GSheetsConnection)
        df = gsheet_conn.read(ttl=0)
    except Exception:
        df = None

    if df is None or df.empty:
        try:
            raw_url = None
            try:
                raw_url = st.secrets["connections"]["gsheets"]["spreadsheet"]
            except Exception:
                raw_url = None
                
            if raw_url and "/d/" in raw_url:
                sheet_id = raw_url.split("/d/")[1].split("/")[0]
                csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
                df = pd.read_csv(csv_url)
            else:
                return {"Edgar": "peru2026"}
        except Exception:
            return {"Edgar": "peru2026"}

    try:
        df.columns = [str(c).strip().lower() for c in df.columns]
        name_col = next((c for c in df.columns if any(k in c for k in ["name", "nombre", "student"])), df.columns[0])
        pin_col = next((c for c in df.columns if any(k in c for k in ["pin", "pass", "clave", "code"])), df.columns[1])

        df = df.dropna(subset=[name_col, pin_col])
        df[name_col] = df[name_col].astype(str).str.strip()
        df[pin_col] = df[pin_col].astype(str).str.strip()
        return dict(zip(df[name_col], df[pin_col]))
    except Exception:
        return {"Edgar": "peru2026"}

# ==============================================================================
# 4. MOTOR DE AUDIO 100% INGLÉS (VOZ NATIVA EN-US)
# ==============================================================================
def clean_for_speech(text):
    clean = re.sub(r'[\_\-\(\)\[\]\{\}\*\#\«\»\<\>\/\\]', ' ', text)
    clean = clean.replace('❌', '').replace('✅', '').replace('🎯', '').replace('💡', '').replace('🔍', '').replace('💬', '').replace('📖', '')
    clean = re.sub(r'^\s*\d+\.\s*', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'(?:https?|ftp)://\S+', '', clean)
    return re.sub(r'\s+', ' ', clean).strip()

def render_communicative_audio(full_response_text, auto_play=True, key_suffix=""):
    paragraphs = [p.strip() for p in full_response_text.split("\n") if p.strip()]
    cleaned_chunks = [clean_for_speech(p) for p in paragraphs if clean_for_speech(p)]
    payload = json.dumps(cleaned_chunks)
    auto_flag = "true" if auto_play else "false"

    html_code = f"""
    <div style="margin-top: 8px; margin-bottom: 6px;">
        <button id="btn_{key_suffix}" style="
            background-color: #0f172a;
            color: #38bdf8;
            border: 1px solid #0284c7;
            border-radius: 6px;
            padding: 6px 14px;
            font-size: 13px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-weight: 500;
        ">
            🔊 Listen to Audio
        </button>
    </div>
    <script>
    (function() {{
        var chunks = {payload};
        var shouldAutoPlay = {auto_flag};
        var btn = document.getElementById("btn_{key_suffix}");

        function playFullSequence() {{
            var synth = (window.top && window.top.speechSynthesis) || window.speechSynthesis;
            if (!synth) return;
            synth.cancel();

            var voices = synth.getVoices();
            var enVoice = voices.find(function(v) {{ 
                return v.lang && (v.lang === "en-US" || v.lang === "en_US" || v.lang.startsWith("en")); 
            }}) || voices[0];

            if (!chunks || chunks.length === 0) return;

            var utterances = [];
            for (var i = 0; i < chunks.length; i++) {{
                var text = chunks[i];
                var utt = new SpeechSynthesisUtterance(text);
                utt.voice = enVoice;
                utt.lang = "en-US";
                utt.rate = 0.95;
                utterances.push(utt);
            }}

            for (var j = 0; j < utterances.length - 1; j++) {{
                (function(index) {{
                    utterances[index].onend = function() {{ synth.speak(utterances[index + 1]); }};
                }})(j);
            }}

            synth.speak(utterances[0]);
        }}

        if (btn) {{
            btn.onclick = function() {{ playFullSequence(); }};
        }}

        if (shouldAutoPlay) {{
            var synth = (window.top && window.top.speechSynthesis) || window.speechSynthesis;
            if (synth && synth.getVoices().length > 0) {{
                playFullSequence();
            }} else if (synth) {{
                synth.onvoiceschanged = function() {{
                    playFullSequence();
                    synth.onvoiceschanged = null;
                }};
            }}
        }}
    }})();
    </script>
    """
    components.html(html_code, height=45)

# ==============================================================================
# 5. CONTROLADOR RESILIENTE DE LLAMADAS GEMINI
# ==============================================================================
def stream_with_retry(client_obj, model_name, contents_list, cfg, retries=3, delay=2.5):
    for attempt in range(retries):
        try:
            return client_obj.models.generate_content_stream(
                model=model_name,
                contents=contents_list,
                config=cfg
            )
        except Exception as e:
            err_msg = str(e)
            if ("503" in err_msg or "UNAVAILABLE" in err_msg) and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                continue
            raise e

def generate_with_retry(client_obj, model_name, contents_val, cfg, retries=3, delay=2.5):
    for attempt in range(retries):
        try:
            return client_obj.models.generate_content(
                model=model_name,
                contents=contents_val,
                config=cfg
            )
        except Exception as e:
            err_msg = str(e)
            if ("503" in err_msg or "UNAVAILABLE" in err_msg) and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                continue
            raise e

# ==============================================================================
# 6. BARRA LATERAL (AUTENTICACIÓN, SECRETS Y ANALÍTICA)
# ==============================================================================
with st.sidebar:
    st.title("🗣️ Edgarvich says")
    
    gemini_api_key = None
    try:
        if "GEMINI_API_KEY" in st.secrets:
            gemini_api_key = st.secrets["GEMINI_API_KEY"]
    except Exception:
        gemini_api_key = None

    if not gemini_api_key:
        gemini_api_key = os.environ.get("GEMINI_API_KEY")

    if not gemini_api_key:
        api_key_input = st.text_input("🔑 Gemini API Key:", type="password")
        gemini_api_key = api_key_input

    st.divider()
    st.subheader("🔐 Student Login (Google Sheets)")
    students_dict = get_authorized_students()
    roster_names = ["-- Select your name --"] + sorted(list(students_dict.keys()))
    
    selected_student = st.selectbox("Select Your Name:", roster_names)
    entered_pin = st.text_input("PIN / Student ID:", type="password")
    
    authenticated = False
    if selected_student != "-- Select your name --" and entered_pin:
        if str(students_dict.get(selected_student, "")).strip() == entered_pin.strip():
            authenticated = True
            st.success(f"Welcome, {selected_student}!")
        else:
            st.error("Incorrect PIN.")

    st.divider()
    st.subheader("📚 Supplementary Context (Optional)")
    uploaded_file = st.file_uploader("Upload Prompts / Scenarios (PDF, TXT, MD)", type=["pdf", "txt", "md"])
    pdf_bytes = None
    text_content = ""
    if uploaded_file:
        if uploaded_file.name.lower().endswith(".pdf"):
            pdf_bytes = uploaded_file.getvalue()
        else:
            text_content = uploaded_file.getvalue().decode("utf-8", errors="ignore")
        st.sidebar.success(f"✅ Material loaded: {uploaded_file.name}")

    st.divider()
    if st.button("🔄 Reset Conversation"):
        st.session_state.messages = []
        st.session_state.last_audio_bytes = None
        st.rerun()

    instructor_key = st.text_input("Instructor Access:", type="password")
    if instructor_key == "peru2026":
        st.subheader("👨‍🏫 Instructor Analytics")
        with st.expander("📊 Student Error Ledger", expanded=True):
            conn = get_db_connection()
            df = pd.read_sql_query("""
                SELECT student_name, grammar_topic, student_errors, mastery_status, student_input, timestamp 
                FROM diagnostics 
                ORDER BY timestamp DESC
            """, conn)
            conn.close()
            st.dataframe(df, use_container_width=True)

# ==============================================================================
# 7. SESIÓN PRINCIPAL & ONBOARDING
# ==============================================================================
st.title("🗣️ Edgarvich says")

if not gemini_api_key:
    st.warning("👈 Please enter or configure your GEMINI_API_KEY in the sidebar to begin.")
    st.stop()

if not authenticated:
    st.warning("👈 Please select your name and enter your PIN in the sidebar to access the session.")
    st.stop()

@st.cache_resource
def get_gemini_client(key):
    return genai.Client(api_key=key)

client = get_gemini_client(gemini_api_key)
student_first_name = selected_student.split()[0]

INITIAL_GREETING = (
    f"Hello {student_first_name}! Welcome to your spoken English conversation practice! ☕\n\n"
    "We are going to practice real everyday spoken English in a relaxed chat. "
    "**You can speak using the microphone below or type your answer in the chat box!** "
    "If you ever don't know how to say something, just ask in Spanish and I will show you how to say it in English.\n\n"
    "To begin, **what would you like to talk about today?**\n\n"
    "1. **Your Daily Routine** (what time you wake up, classes, what you do each day)\n"
    "2. **A Memorable Trip or Vacation** (a place you visited in the past and what you did)\n"
    "3. **Your Future Plans** (places you want to travel to, upcoming goals)\n\n"
    "👉 **Your Turn:** Speak or write **1, 2, or 3** to start!"
)

if 'messages' not in st.session_state or st.session_state.get("active_user") != selected_student or not st.session_state.get("messages"):
    st.session_state.active_user = selected_student
    st.session_state.messages = [{
        "role": "assistant",
        "content": INITIAL_GREETING,
        "auto_play": True
    }]
    st.session_state.last_audio_bytes = None

# Renderizar el historial de conversación
for idx, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("audio_data"):
            st.audio(m["audio_data"], format="audio/wav")
        if m["role"] == "assistant" and m.get("content"):
            render_communicative_audio(
                m["content"], 
                auto_play=m.get("auto_play", False), 
                key_suffix=f"hist_{idx}"
            )
            m["auto_play"] = False

# ==============================================================================
# 8. ENTRADA DE USUARIO (MICRÓFONO DIRECTO O TEXTO)
# ==============================================================================
st.write("---")
col_audio, col_info = st.columns([1, 2])
with col_audio:
    recorded_audio = st.audio_input("🎙️ Speak your answer (Record & Send):")
with col_info:
    st.caption("💡 **Tip:** Press record to speak in English or ask in Spanish. Press stop to send directly!")

text_input = st.chat_input("Or type your message here if you prefer typing...")

incoming_text = None
incoming_audio_bytes = None

if recorded_audio is not None:
    audio_bytes = recorded_audio.getvalue()
    if audio_bytes != st.session_state.get("last_audio_bytes"):
        st.session_state.last_audio_bytes = audio_bytes
        incoming_audio_bytes = audio_bytes
        incoming_text = "🎙️ [Audio Voice Message]"

if text_input:
    incoming_text = text_input

# ==============================================================================
# 9. MOTOR PEDAGÓGICO: ENFOQUE EN LA TRADUCCIÓN/SCAFFOLDING DEL TUTOR
# ==============================================================================
if incoming_text:
    user_entry = {"role": "user", "content": incoming_text}
    if incoming_audio_bytes:
        user_entry["audio_data"] = incoming_audio_bytes
    st.session_state.messages.append(user_entry)

    with st.chat_message("user"):
        st.markdown(incoming_text)
        if incoming_audio_bytes:
            st.audio(incoming_audio_bytes, format="audio/wav")

    learner_weaknesses = get_student_unmastered_weaknesses(selected_student)

    system_instruction = (
        f"You are a friendly, expert English conversation partner helping an adult learner ({selected_student}, called '{student_first_name}') build spoken English fluency.\n\n"
        f"DIAGNOSTIC TARGETS (RECYCLE IMPLICITLY WITHOUT EXPLANATION):\n"
        f"{learner_weaknesses}\n\n"
        "STRICT PEDAGOGICAL & SCAFFOLDING RULES (TARGET CEFR A2–B1):\n"
        "1. THE TUTOR'S ENGLISH TRANSLATION & MODELING IS CRUCIAL:\n"
        "   - The student is fully welcome to speak or write in Spanish or Spanglish whenever they are stuck or asking 'cómo se dice...'.\n"
        "   - Whenever the student uses Spanish or expresses a doubt, IMMEDIATELY show them how to express that in natural English.\n"
        "     Example: Student says 'Ayer me quedé dormido y no sé cómo se dice.'\n"
        "     Tutor: 'In English, you can say: \"Yesterday, I overslept!\" Did your alarm not go off?'\n"
        "   - Always provide the natural, communicative English translation/expression right at the beginning of your response.\n\n"
        "2. ACCESSIBLE & CLEAR INTERNATIONAL VOCABULARY (NO HARD IDIOMS):\n"
        "   - Strictly avoid obscure American idioms or slang (no 'early bird', 'hit the snooze button', 'under the weather', 'spill the tea').\n"
        "   - Use transparent, clear English vocabulary that is easy to understand and speak aloud.\n\n"
        "3. VOICE SYNTHESIS READY (NO SPANISH PUNCTUATION IN AUDIO):\n"
        "   - Your spoken audio must sound natural in English (en-US). Do NOT use inverted punctuation like '¡' or '¿'.\n\n"
        "4. SHORT TURNS & NATURAL RECASTING:\n"
        "   - Keep your responses concise (2 to 3 sentences).\n"
        "   - If the student makes an English slip, recast it naturally into your response.\n"
        "   - Always finish with ONE simple, direct question so the student can reply immediately."
    )

    with st.chat_message("assistant"):
        try:
            contents = []
            for msg in st.session_state.messages[-4:]:
                content_text = msg.get("content", "")
                if content_text and not content_text.startswith("🚨"):
                    role = "user" if msg["role"] == "user" else "model"
                    parts = []
                    if msg.get("audio_data"):
                        parts.append(types.Part.from_bytes(data=msg["audio_data"], mime_type="audio/wav"))
                        parts.append(types.Part.from_text(text="[Listen to the student's spoken audio. If they spoke in Spanish or expressed doubt, provide the exact English expression/translation immediately, recast any slips, and ask a follow-up question.]"))
                    else:
                        parts.append(types.Part.from_text(text=content_text))
                    contents.append(types.Content(role=role, parts=parts))

            if pdf_bytes:
                contents[-1].parts.insert(0, types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
            elif text_content:
                contents[-1].parts.insert(0, types.Part.from_text(text=f"[MATERIAL CONTEXT]:\n{text_content[:8000]}\n"))

            fast_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=400,
                temperature=0.6
            )

            def stream_response():
                response = stream_with_retry(
                    client_obj=client,
                    model_name='gemini-3.8-flash',
                    contents_list=contents,
                    cfg=fast_config,
                    retries=3,
                    delay=2.5
                )
                for chunk in response:
                    if chunk.text:
                        yield chunk.text.replace("<", "«").replace(">", "»")

            student_response = st.write_stream(stream_response)

            record_id = log_diagnostic_result(
                selected_student, incoming_text, student_response, 
                "Analyzing...", "Analyzing...", "Analyzing...", "emerging",
                "YES" if uploaded_file else "NO"
            )

            # Diagnóstico pedagógico invisible en segundo plano (para la analítica docente)
            def run_background_analysis(rec_id, query_content, resp, student_name):
                if not rec_id:
                    return
                try:
                    analysis_prompt = (
                        f"Analyze this oral language interaction strictly for instructor analytics.\n"
                        f"Student input summary: '{query_content}'\n"
                        f"Tutor response: '{resp}'\n\n"
                        "Extract JSON with keys:\n"
                        "- grammar_topic: Target structure or tense.\n"
                        "- student_strengths: Oral fluency strengths or vocabulary used.\n"
                        "- student_errors: Slip or transfer error made (or 'None' if accurate).\n"
                        "- mastery_status: 'needs_practice', 'emerging', or 'mastered'."
                    )
                    analysis_res = generate_with_retry(
                        client_obj=client,
                        model_name='gemini-3.8-flash',
                        contents_val=analysis_prompt,
                        cfg=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=PedagogicalDiagnosis,
                            max_output_tokens=250
                        ),
                        retries=3,
                        delay=2.5
                    )
                    diag_data = json.loads(analysis_res.text)
                    conn = get_db_connection()
                    with conn:
                        conn.execute("""
                            UPDATE diagnostics 
                            SET grammar_topic = ?, student_strengths = ?, student_errors = ?, mastery_status = ?
                            WHERE id = ?
                        """, (
                            diag_data.get("grammar_topic", "Oral Fluency"), 
                            diag_data.get("student_strengths", "Spoken Interaction"), 
                            diag_data.get("student_errors", "Minor oral slip"), 
                            diag_data.get("mastery_status", "needs_practice"),
                            rec_id
                        ))
                    conn.close()
                except Exception:
                    pass

            threading.Thread(
                target=run_background_analysis,
                args=(record_id, incoming_text, student_response, selected_student),
                daemon=True
            ).start()

            new_idx = len(st.session_state.messages)
            render_communicative_audio(student_response, auto_play=True, key_suffix=f"dyn_{new_idx}")

            st.session_state.messages.append({
                "role": "assistant",
                "content": student_response,
                "auto_play": False
            })

        except Exception as e:
            st.error(f"🚨 Connection error: {e}")
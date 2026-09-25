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
# 1. CONFIGURATION & PATHS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "spanglish_adaptive_coach.db")
st.set_page_config(page_title="🗣️ Spanglish Adaptive Coach", layout="wide", page_icon="☕")

# ==============================================================================
# 2. LOCAL RESEARCH DATABASE (DIAGNOSTICS & ADAPTIVE ERROR RECYCLING)
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
    """Retrieves unresolved linguistic slips to inject into the tutor prompt."""
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
            return "No recorded persistent gaps. Maintain natural conversational immersion."
        
        weakness_lines = []
        for _, row in df.iterrows():
            weakness_lines.append(f"- Focus: {row['grammar_topic']} | Growth Area: {row['student_errors']}")
        return "\n".join(weakness_lines)
    except Exception:
        return "Learner profile currently unavailable."

class PedagogicalDiagnosis(BaseModel):
    grammar_topic: str
    student_strengths: str
    student_errors: str
    mastery_status: str  # 'needs_practice', 'emerging', 'mastered'

# ==============================================================================
# 3. STUDENT ROSTER INTEGRATION (GOOGLE SHEETS WITH LOCAL FALLBACK)
# ==============================================================================
gsheet_conn = st.connection("gsheets", type=GSheetsConnection)

def get_authorized_students() -> dict:
    try:
        df = gsheet_conn.read(ttl=0)
    except Exception:
        try:
            raw_url = st.secrets["connections"]["gsheets"]["spreadsheet"]
            sheet_id = raw_url.split("/d/")[1].split("/")[0]
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
            df = pd.read_csv(csv_url)
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

def add_student_to_gsheet(name: str, pin: str) -> tuple[bool, str]:
    try:
        name_clean = str(name).strip()
        pin_clean = str(pin).strip()
        if not name_clean or not pin_clean:
            return False, "Name and PIN are required."

        df = gsheet_conn.read(ttl=0)
        col_names = [str(c).strip().lower() for c in df.columns]
        name_col_idx = next(i for i, c in enumerate(col_names) if any(k in c for k in ["name", "nombre", "student"]))
        pin_col_idx = next(i for i, c in enumerate(col_names) if any(k in c for k in ["pin", "pass", "clave", "code"]))

        actual_name_col = df.columns[name_col_idx]
        actual_pin_col = df.columns[pin_col_idx]

        existing = df[actual_name_col].dropna().astype(str).str.strip().str.lower().values
        if name_clean.lower() in existing:
            return False, f"Student '{name_clean}' is already registered."

        new_row = pd.DataFrame([{actual_name_col: name_clean, actual_pin_col: pin_clean}])
        updated_df = pd.concat([df, new_row], ignore_index=True)
        gsheet_conn.update(data=updated_df)
        return True, f"Student '{name_clean}' registered successfully!"
    except Exception as e:
        return False, f"Google Sheets write error: {e}"

# ==============================================================================
# 4. AUDIO ENGINE (BILINGUAL NATIVE WEB SPEECH API)
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
            🔊 Listen to Full Audio
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
            var enVoice = voices.find(function(v) {{ return v.lang && (v.lang === "en-US" || v.lang.startsWith("en")); }}) || voices[0];
            var esVoice = voices.find(function(v) {{ return v.lang && v.lang.toLowerCase().startsWith("es"); }}) || voices[0];

            if (!chunks || chunks.length === 0) return;

            var utterances = [];
            for (var i = 0; i < chunks.length; i++) {{
                var text = chunks[i];
                var utt = new SpeechSynthesisUtterance(text);
                var spanishIndicators = text.match(/[áéíóúüñ¿¡]|\\b(el|la|los|las|un|una|ayer|hoy|mientras|vamos|amigos|casa)\\b/i);
                
                if (spanishIndicators) {{
                    utt.voice = esVoice;
                    utt.lang = "es-ES";
                    utt.rate = 0.90;
                }} else {{
                    utt.voice = enVoice;
                    utt.lang = "en-US";
                    utt.rate = 0.94;
                }}
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
# 5. RESILIENT GEMINI CALL HANDLERS
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
# 6. SIDEBAR (AUTHENTICATION & RESEARCH DASHBOARD)
# ==============================================================================
with st.sidebar:
    st.title("🗣️ Spanglish Coach")
    
    if "GEMINI_API_KEY" in st.secrets:
        gemini_api_key = st.secrets["GEMINI_API_KEY"]
    else:
        api_key_input = st.text_input("🔑 Gemini API Key:", type="password")
        gemini_api_key = api_key_input or os.environ.get("GEMINI_API_KEY")

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
# 7. MAIN SESSION & ONBOARDING STARTER
# ==============================================================================
st.title("🗣️ Spanglish Communicative Roleplay")

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
    f"¡Hola, {student_first_name}! Welcome to your Spanglish conversation practice! ☕✨\n\n"
    "I'm your communicative coach, and we are going to practice real-life conversational fluency together.\n\n"
    "To begin, **which tense would you like to practice today?**\n\n"
    "1. **Present Tense** *(Daily routines, campus life, habits)*\n"
    "2. **Past Tense** *(Memorable trips, weekend stories, past experiences)*\n"
    "3. **Future Tense** *(Upcoming plans, future travels, study goals)*\n\n"
    "👉 **Your Turn:** Reply with **1, 2, or 3** (or tell me your choice) to jump right into our scenario!"
)

if 'messages' not in st.session_state or st.session_state.get("active_user") != selected_student or not st.session_state.get("messages"):
    st.session_state.active_user = selected_student
    st.session_state.messages = [{
        "role": "assistant",
        "content": INITIAL_GREETING,
        "auto_play": True
    }]

for idx, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m["role"] == "assistant" and m.get("content"):
            render_communicative_audio(
                m["content"], 
                auto_play=m.get("auto_play", False), 
                key_suffix=f"hist_{idx}"
            )
            m["auto_play"] = False

st.info("💡 **Fluency Mode:** Natural Spanglish roleplay (A2–B2). If you need help, just ask for a clue or translation!")

# ==============================================================================
# 8. ADAPTIVE PEDAGOGICAL ENGINE
# ==============================================================================
user_input = st.chat_input("Write your response or ask for clarification...")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    learner_weaknesses = get_student_unmastered_weaknesses(selected_student)

    system_instruction = (
        f"You are an expert language educator facilitating Spanglish conversation practice with adaptive scaffolding.\n"
        f"Active Student: {selected_student} ('{student_first_name}').\n\n"
        f"LEARNER DIAGNOSTIC PROFILE (RECURRENT WEAK SPOTS TO RECYCLE NATURALLY):\n"
        f"{learner_weaknesses}\n\n"
        "PEDAGOGICAL & INTERACTION RULES (CEFR A2 TO B2):\n"
        "1. WELCOME & SELECTION:\n"
        "   - The initial greeting gave three options (1. Present, 2. Past, 3. Future).\n"
        "   - As soon as the student chooses a tense, immediately adopt a supportive conversational persona and launch an everyday, real-world roleplay scenario.\n"
        "2. ROLEPLAY CONTEXTS:\n"
        "   - Strictly restrict scenarios to everyday university life, campus routines, community activities, tourism, studying, dining out, or travel.\n"
        "   - Engage using natural, expressive Spanglish code-switching to model communicative ease.\n"
        "3. ERROR RECYCLING & SCAFFOLDING:\n"
        "   - Implicitly weave opportunities into the dialogue for the student to practice their recorded weak spots.\n"
        "   - Do NOT say 'You made an error on X before.' Instead, guide the situation so the natural reply requires that structure.\n"
        "   - When the student makes an error, praise the communicative attempt first, then provide a gentle recast or sentence model.\n"
        "4. TRANSLATIONS & CLARIFICATIONS:\n"
        "   - If the student asks for a Spanish translation or grammar clarification, provide it briefly in simple English (maximum 1-2 lines).\n"
        "   - Immediately steer the conversation back into English/Spanglish with an open-ended communicative follow-up question.\n"
        "5. CONSTRAINTS:\n"
        "   - Keep language accessible (A2–B2) with zero heavy linguistic meta-jargon.\n"
        "   - Always conclude with an engaging question to keep the roleplay moving."
    )

    with st.chat_message("assistant"):
        try:
            contents = []
            for msg in st.session_state.messages[-4:]:
                content_text = msg.get("content", "")
                if content_text and not content_text.startswith("🚨"):
                    role = "user" if msg["role"] == "user" else "model"
                    contents.append(types.Content(role=role, parts=[types.Part.from_text(text=content_text)]))

            if pdf_bytes:
                contents[-1].parts.insert(0, types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
            elif text_content:
                contents[-1].parts.insert(0, types.Part.from_text(text=f"[MATERIAL CONTEXT]:\n{text_content[:8000]}\n"))

            fast_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=1024,
                temperature=0.6
            )

            def stream_response():
                response = stream_with_retry(
                    client_obj=client,
                    model_name='gemini-2.5-flash',
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
                selected_student, user_input, student_response, 
                "Analyzing...", "Analyzing...", "Analyzing...", "emerging",
                "YES" if uploaded_file else "NO"
            )

            # Asynchronous background pedagogical evaluation
            def run_background_analysis(rec_id, query, resp, student_name):
                if not rec_id:
                    return
                try:
                    analysis_prompt = (
                        f"Analyze this Spanglish learner interaction for diagnostic tracking.\n"
                        f"Student text: '{query}'\n"
                        f"Coach response: '{resp}'\n\n"
                        "Extract JSON with keys:\n"
                        "- grammar_topic: Specific tense or structure addressed.\n"
                        "- student_strengths: What the student did well.\n"
                        "- student_errors: Clear mistake or transfer slip (or 'None' if accurate).\n"
                        "- mastery_status: 'needs_practice' (if clear errors occurred), 'emerging' (if supported), or 'mastered' (accurate free production)."
                    )
                    analysis_res = generate_with_retry(
                        client_obj=client,
                        model_name='gemini-2.5-flash',
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
                            diag_data.get("grammar_topic", "Spanglish Fluency"), 
                            diag_data.get("student_strengths", "Communicative Effort"), 
                            diag_data.get("student_errors", "Minor code-switching slip"), 
                            diag_data.get("mastery_status", "needs_practice"),
                            rec_id
                        ))
                    conn.close()
                except Exception:
                    pass

            threading.Thread(
                target=run_background_analysis,
                args=(record_id, user_input, student_response, selected_student),
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

import streamlit as st
import sqlite3
import os
import re
import time
import pandas as pd
import streamlit.components.v1 as components
import json
from streamlit_gsheets import GSheetsConnection
from google import genai
from google.genai import types

# ==============================================================================
# 1. CONFIGURACIÓN & RUTAS DE ALMACENAMIENTO
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "english_a1_coach.db")

st.set_page_config(
    page_title="🗣️ Edgarvich Tutor de Ingles107", 
    layout="wide", 
    page_icon="🎓"
)

# ==============================================================================
# 2. BASE DE DATOS LOCAL (DIAGNÓSTICO PEDAGÓGICO CON MIGRACIÓN AUTOMÁTICA)
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
        conn.execute('''CREATE TABLE IF NOT EXISTS diagnostics 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       student_name TEXT, 
                       student_input TEXT, 
                       ai_feedback TEXT, 
                       grammar_topic TEXT, 
                       pedagogical_analysis TEXT, 
                       mastery_status TEXT, 
                       used_pdf TEXT, 
                       timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Migración segura: agregar 'pedagogical_analysis' si la tabla ya existía
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(diagnostics)")
        cols = [col[1] for col in cursor.fetchall()]
        if "pedagogical_analysis" not in cols:
            try:
                conn.execute("ALTER TABLE diagnostics ADD COLUMN pedagogical_analysis TEXT")
            except Exception:
                pass
    conn.close()

init_db()

def log_diagnostic_result(name, inp, feedback, topic, analysis, mastery_status, used_pdf):
    try:
        conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(diagnostics)")
            cols = [col[1] for col in cursor.fetchall()]
            
            if "pedagogical_analysis" in cols:
                cursor.execute("""INSERT INTO diagnostics 
                              (student_name, student_input, ai_feedback, grammar_topic, pedagogical_analysis, mastery_status, used_pdf) 
                              VALUES (?,?,?,?,?,?,?)""",
                              (name, inp, feedback, topic, analysis, mastery_status, used_pdf))
            else:
                cursor.execute("""INSERT INTO diagnostics 
                              (student_name, student_input, ai_feedback, grammar_topic, student_strengths, student_errors, mastery_status, used_pdf) 
                              VALUES (?,?,?,?,?,?,?,?)""",
                              (name, inp, feedback, topic, analysis, "None", mastery_status, used_pdf))
            inserted_id = cursor.lastrowid
        conn.close()
        return inserted_id
    except Exception as e:
        print(f"❌ Error al guardar log en SQLite: {e}")
        return None

def get_student_unmastered_weaknesses(name, limit=3):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(diagnostics)")
        cols = [col[1] for col in cursor.fetchall()]
        analysis_col = "pedagogical_analysis" if "pedagogical_analysis" in cols else "student_errors"

        query = f"""
            SELECT grammar_topic, {analysis_col} 
            FROM diagnostics 
            WHERE student_name = ? AND grammar_topic NOT IN ('Analyzing...', 'General Conversation') AND mastery_status != 'mastered'
            ORDER BY id DESC LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(name, limit))
        conn.close()
        if df.empty:
            return "Beginner A1-A2 English learner. Focus on natural communication and clear sentence models."
        
        weakness_lines = []
        for _, row in df.iterrows():
            weakness_lines.append(f"- Focus Target: {row['grammar_topic']} | Note: {row[analysis_col]}")
        return "\n".join(weakness_lines)
    except Exception:
        return "Learner profile currently initializing."

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
# 4. MOTOR DE AUDIO 100% INGLÉS (VOZ NATIVA EN-US, CLARA Y PAUSADA)
# ==============================================================================
def clean_for_speech(text):
    clean = re.sub(r'[\_\-\(\)\[\]\{\}\*\#\«\»\<\>\/\\]', ' ', text)
    clean = clean.replace('❌', '').replace('✅', '').replace('🎯', '').replace('💡', '').replace('🔍', '').replace('💬', '').replace('📖', '').replace('👉', '')
    clean = re.sub(r'^\s*\d+\.\s*', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'(?:https?|ftp)://\S+', '', clean)
    return re.sub(r'\s+', ' ', clean).strip()

def render_communicative_audio(full_response_text, auto_play=True, key_suffix="", button_label="🔊 Listen to Tutor"):
    paragraphs = [p.strip() for p in full_response_text.split("\n") if p.strip()]
    cleaned_chunks = [clean_for_speech(p) for p in paragraphs if clean_for_speech(p)]
    payload = json.dumps(cleaned_chunks)
    auto_flag = "true" if auto_play else "false"

    html_code = f"""
    <div style="margin-top: 2px; margin-bottom: 2px;">
        <button id="btn_{key_suffix}" style="
            background-color: #0f172a;
            color: #38bdf8;
            border: 1px solid #0284c7;
            border-radius: 6px;
            padding: 8px 14px;
            font-size: 14px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-weight: 500;
            width: 100%;
            justify-content: center;
        ">
            {button_label}
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
                utt.rate = 0.88;
                utterances.push(utt);
            }}

            for (var j = 0; j < utterances.length - 1; j++) {{
                (function(index) {{
                    utterances[index].onend = function() {{
                        synth.speak(utterances[index + 1]);
                    }};
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
    components.html(html_code, height=50)

# ==============================================================================
# 5. CONTROLADOR RESILIENTE DE LLAMADAS GEMINI
# ==============================================================================
def stream_with_retry(client_obj, model_name, contents_list, cfg, retries=3, delay=2.0):
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

def generate_with_retry(client_obj, model_name, contents_val, cfg, retries=3, delay=2.0):
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
# 6. BARRA LATERAL (AUTENTICACIÓN, GESTIÓN & ANALÍTICA DOCENTE)
# ==============================================================================
with st.sidebar:
    st.title("🗣️ Edgarvich Tutor de Ingles107")
    
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
    
    selected_student = st.selectbox("Select Your Name / Selecciona tu nombre:", roster_names)
    entered_pin = st.text_input("PIN / Code:", type="password")
    
    authenticated = False
    if selected_student != "-- Select your name --" and entered_pin:
        if str(students_dict.get(selected_student, "")).strip() == entered_pin.strip():
            authenticated = True
            st.success(f"Welcome, {selected_student}!")
        else:
            st.error("Incorrect PIN / PIN incorrecto.")

    st.divider()
    st.subheader("📚 Supplementary Context (Optional)")
    uploaded_file = st.file_uploader("Upload Vocabulary / Worksheet (PDF, TXT, MD)", type=["pdf", "txt", "md"])
    pdf_bytes = None
    text_content = ""
    if uploaded_file:
        if uploaded_file.name.lower().endswith(".pdf"):
            pdf_bytes = uploaded_file.getvalue()
        else:
            text_content = uploaded_file.getvalue().decode("utf-8", errors="ignore")
        st.sidebar.success(f"✅ Material loaded: {uploaded_file.name}")

    st.divider()
    if st.button("🔄 Reset Conversation / Reiniciar"):
        st.session_state.messages = []
        st.session_state.last_audio_bytes = None
        st.rerun()

    st.divider()
    instructor_key = st.text_input("Instructor Access:", type="password")
    if instructor_key == "peru2026":
        st.subheader("👨‍🏫 Instructor Control Panel")
        with st.expander("📊 Student Oral Progress & Diagnostics Ledger", expanded=True):
            if st.button("🔄 Refresh Analytics Table"):
                st.rerun()

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(diagnostics)")
            col_names = [col[1] for col in cursor.fetchall()]

            # Seleccionar las columnas disponibles en la base de datos local
            if "pedagogical_analysis" in col_names:
                query_cols = "id, student_name, grammar_topic, pedagogical_analysis, mastery_status, student_input, ai_feedback, timestamp"
            else:
                query_cols = "id, student_name, grammar_topic, student_strengths AS pedagogical_analysis, mastery_status, student_input, ai_feedback, timestamp"

            df = pd.read_sql_query(f"SELECT {query_cols} FROM diagnostics ORDER BY timestamp DESC", conn)
            conn.close()

            all_students = ["All"] + sorted([s for s in df["student_name"].dropna().unique() if s])
            student_filter = st.selectbox("Filter by student:", all_students)
            if student_filter != "All":
                df_display = df[df["student_name"] == student_filter]
            else:
                df_display = df

            col_m1, col_m2 = st.columns(2)
            with col_m1:
                st.metric("Total Exchanges", len(df_display))
            with col_m2:
                top_topic = df_display["grammar_topic"].mode()[0] if not df_display.empty else "N/A"
                st.metric("Main Focus Topic", top_topic)

            cols_config = {
                "grammar_topic": st.column_config.TextColumn("Target Topic", width="medium"),
                "pedagogical_analysis": st.column_config.TextColumn("Pedagogical Analysis", width="large"),
                "mastery_status": st.column_config.TextColumn("Status", width="small"),
                "student_input": st.column_config.TextColumn("Student Utterance / Question", width="medium"),
                "ai_feedback": st.column_config.TextColumn("Tutor Output", width="large"),
            }

            st.dataframe(
                df_display,
                column_config=cols_config,
                hide_index=True,
                use_container_width=True
            )
            
            st.download_button(
                "📥 Download Research Analytics CSV", 
                df.to_csv(index=False), 
                "engl107_student_oral_diagnostics.csv", 
                mime="text/csv"
            )

# ==============================================================================
# 7. SESIÓN PRINCIPAL & ONBOARDING (A1 BEGINNERS)
# ==============================================================================
st.title("🗣️ Edgarvich Tutor de Ingles107")

if not gemini_api_key:
    st.warning("👈 Please enter or configure your GEMINI_API_KEY in the sidebar to begin.")
    st.stop()

if not authenticated:
    st.warning("👈 Please select your name and enter your PIN in the sidebar to access your session.")
    st.stop()

@st.cache_resource
def get_gemini_client(key):
    return genai.Client(api_key=key)

client = get_gemini_client(gemini_api_key)
student_first_name = selected_student.split()[0]

INITIAL_GREETING_A1 = (
    f"Hello {student_first_name}! Welcome to your English practice! 👋\n\n"
    "We will practice simple English step by step. Don't worry about mistakes!\n"
    "**You can speak using the microphone below or type in the chat.** "
    "*(Puedes hablar o preguntar en español cuando lo necesites, o hacer preguntas sobre cualquier tema gramatical).*\n\n"
    "**What do you want to talk about today?**\n\n"
    "1. **Introduce myself:** Name, city, and age.\n"
    "2. **My favorite things:** Food and hobbies.\n"
    "3. **My plans:** What I will do tomorrow.\n\n"
    "👉 **Choose 1, 2, or 3 (or ask me any question) to begin!**"
)

if 'messages' not in st.session_state or st.session_state.get("active_user") != selected_student or not st.session_state.get("messages"):
    st.session_state.active_user = selected_student
    st.session_state.messages = [{
        "role": "assistant",
        "content": INITIAL_GREETING_A1,
        "auto_play": True
    }]
    st.session_state.last_audio_bytes = None

last_assistant_idx = -1
for idx in range(len(st.session_state.messages) - 1, -1, -1):
    if st.session_state.messages[idx]["role"] == "assistant":
        last_assistant_idx = idx
        break

recorded_audio = None
listen_placeholder = None

for idx, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("audio_data"):
            st.audio(m["audio_data"], format="audio/wav")

        # ==============================================================================
        # 8. POSICIÓN: AUDIO Y MICRÓFONO JUSTO DESPUÉS DE LA ÚLTIMA RESPUESTA DEL TUTOR
        # ==============================================================================
        if idx == last_assistant_idx and m["role"] == "assistant":
            st.markdown("---")
            col_listen, col_mic = st.columns([1, 1])

            with col_listen:
                st.markdown("**🔊 Reescuchar / Listen:**")
                listen_placeholder = st.empty()
                with listen_placeholder:
                    render_communicative_audio(
                        m["content"], 
                        auto_play=m.get("auto_play", False), 
                        key_suffix=f"last_turn_{idx}",
                        button_label="▶️ Replay Tutor Audio"
                    )
                m["auto_play"] = False

            with col_mic:
                st.markdown("**🎙️ Tu respuesta / Your Answer:**")
                recorded_audio = st.audio_input("Record voice / Grabar respuesta:", key=f"audio_input_{idx}")

text_input = st.chat_input("Type in English (or ask in Spanish if you need help)...")

incoming_text = None
incoming_audio_bytes = None

if recorded_audio is not None:
    audio_bytes = recorded_audio.getvalue()
    if audio_bytes != st.session_state.get("last_audio_bytes"):
        st.session_state.last_audio_bytes = audio_bytes
        incoming_audio_bytes = audio_bytes
        incoming_text = "🎙️ [Spoken Voice Message]"

if text_input:
    incoming_text = text_input

# ==============================================================================
# 9. MOTOR PEDAGÓGICO: COMUNICATIVO & ADAPTATIVO A PREGUNTAS Y TEMAS
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
        f"You are Edgarvich, a warm, encouraging English conversation coach helping {selected_student} ('{student_first_name}').\n"
        f"Learner profile context / targets:\n{learner_weaknesses}\n\n"
        "PEDAGOGICAL & INTERACTION RULES:\n"
        "1. TOPIC & QUESTION AWARENESS (IMMEDIATE RECOGNITION):\n"
        "   - If the student asks about a specific grammar topic or tense (e.g., Future with 'will' or 'going to', Past tenses, Conditionals, Prepositions):\n"
        "     * IMMEDIATELY address their specific topic and answer clearly.\n"
        "     * Give 1-2 clear, simple examples.\n"
        "     * Ask an engaging question inviting them to practice that topic.\n"
        "   - If the student selects '1', '2', or '3':\n"
        "     * DO NOT repeat the greetings or ask what they want to practice.\n"
        "     * Launch the chosen topic directly (1: Intro, 2: Favorites, 3: Plans/Future tomorrow).\n\n"
        "2. COMMUNICATIVE FEEDBACK (FOCUS ON FORM):\n"
        "   - When the student converses and makes an error or awkward phrasing:\n"
        "     * Do NOT give lengthy grammar lectures.\n"
        "     * Provide the natural version directly in bold on its own line:\n"
        "       👉 **Natural way to say it:** [Polished natural sentence]\n"
        "     * Acknowledge meaning warmly and keep the dialogue flowing.\n\n"
        "3. COMPLETE SENTENCE MODELS (NEVER TRUNCATE):\n"
        "   - Whenever helpful, provide finished sentence models they can say aloud:\n"
        "     You can say:\n"
        "     • \"[Complete Model 1]\"\n"
        "     • \"[Complete Model 2]\"\n\n"
        "4. NATURAL SPANISH-TO-ENGLISH BRIDGE:\n"
        "   - If the student uses Spanish, translate key parts and bridge into English encouragingly.\n\n"
        "5. FAST & NATURAL AUDIO READY:\n"
        "   - Keep responses within 3 to 5 sentences total to maintain high pace and prevent cuts.\n"
        "   - Avoid inverted Spanish punctuation ('¡', '¿') so en-US TTS reads smoothly."
    )

    with st.chat_message("assistant"):
        try:
            contents = []
            for msg in st.session_state.messages[-10:]:
                content_text = msg.get("content", "")
                if content_text and not content_text.startswith("🚨"):
                    role = "user" if msg["role"] == "user" else "model"
                    parts = []
                    if msg.get("audio_data"):
                        parts.append(types.Part.from_bytes(data=msg["audio_data"], mime_type="audio/wav"))
                        parts.append(types.Part.from_text(text="[Listen to student audio. Answer questions directly, model natural English, and ask a follow-up question.]"))
                    else:
                        parts.append(types.Part.from_text(text=content_text))
                    contents.append(types.Content(role=role, parts=parts))

            if pdf_bytes:
                contents[-1].parts.insert(0, types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
            elif text_content:
                contents[-1].parts.insert(0, types.Part.from_text(text=f"[MATERIAL CONTEXT]:\n{text_content[:8000]}\n"))

            fast_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=650,
            )

            def stream_response():
                response = stream_with_retry(
                    client_obj=client,
                    model_name='gemini-3.8-flash',
                    contents_list=contents,
                    cfg=fast_config,
                    retries=3,
                    delay=2.0
                )
                for chunk in response:
                    if chunk.text:
                        yield chunk.text.replace("<", "«").replace(">", "»")

            student_response = st.write_stream(stream_response)

            # ==================================================================
            # ANÁLISIS PEDAGÓGICO DIRECTO (SIN DEPENDENCIAS RÍGIDAS DE PYDANTIC)
            # ==================================================================
            grammar_topic = "General Conversation"
            pedagogical_analysis = "Communicative interaction"
            mastery_status = "needs_practice"

            try:
                analysis_prompt = (
                    "You are an Applied Linguistics expert analyzing an English learner interaction.\n"
                    "Analyze the following student utterance and tutor response:\n\n"
                    f"Student Utterance: \"{incoming_text}\"\n"
                    f"Tutor Response: \"{student_response}\"\n\n"
                    "Return a JSON object with EXACTLY these three keys:\n"
                    "{\n"
                    "  \"grammar_topic\": \"<Specific topic name. Examples: 'Future (Will / Going to)', 'Present Perfect', 'Past Habitual (Used to)', 'Simple Past', 'Daily Routines', 'Questions with Do/Did'>\",\n"
                    "  \"pedagogical_analysis\": \"<Concise description of the student's attempt, question asked, or specific slip corrected>\",\n"
                    "  \"mastery_status\": \"<needs_practice, emerging, or mastered>\"\n"
                    "}\n"
                    "Do NOT output vague generalities. Detect the real topic of the question or sentence."
                )

                analysis_res = generate_with_retry(
                    client_obj=client,
                    model_name='gemini-3.8-flash',
                    contents_val=analysis_prompt,
                    cfg=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        max_output_tokens=300
                    ),
                    retries=2,
                    delay=1.5
                )

                raw_text = str(analysis_res.text).strip()
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match:
                    parsed_json = json.loads(match.group(0))
                    grammar_topic = parsed_json.get("grammar_topic", grammar_topic)
                    pedagogical_analysis = parsed_json.get("pedagogical_analysis", pedagogical_analysis)
                    mastery_status = parsed_json.get("mastery_status", mastery_status)
            except Exception as ex:
                print(f"❌ Error en extracción JSON de diagnóstico: {ex}")

            # Guardado directo en SQLite
            log_diagnostic_result(
                selected_student, incoming_text, student_response, 
                grammar_topic, pedagogical_analysis, mastery_status,
                "YES" if uploaded_file else "NO"
            )

            # Actualizar estado y refrescar
            st.session_state.messages.append({
                "role": "assistant",
                "content": student_response,
                "auto_play": False
            })

            st.rerun()

        except Exception as e:
            st.error(f"🚨 Connection error: {e}")